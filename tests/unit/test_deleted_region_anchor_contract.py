import unittest

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline.review.evidence_contract import (
    ReviewContractError,
    classify_changed_region_anchor,
)
from lambdas.LlamaPReviewPipeline.review.presentation import (
    compile_presentation_v1,
)
from lambdas.LlamaPReviewPipeline.review.projection import (
    build_v3_review,
)
from lambdas.LlamaPReviewPipeline.review.v3 import (
    finding_evidence_capability,
    validate_raw_v3_review,
)


HEAD = "a" * 40
OTHER_HEAD = "b" * 40
DELETED_PATH = "src/runtime/task-store.ts"
CONSUMER_PATH = "src/runtime/task-consumer.ts"
DELETED_SNIPPET = (
    "const task = room._tasks[taskId];\n"
    "delete room._tasks[taskId];\n"
    "return task;"
)
PR_DETAILS = f"""# Pull Request #2601

## File Changes
### {DELETED_PATH}
```diff
@@ -10,4 +10,3 @@ export function complete(room, taskId) {{
 const task = room._tasks[taskId];
-delete room._tasks[taskId];
 return task;
 }}
```
"""


def _content_event(
    coverage_type="full_file",
    *,
    outcome="hit",
    source_ref=f"pr_head:{HEAD}",
    observed_state="content_observed",
):
    return {
        "id": "ev_surviving_consumer",
        "source_type": "pfr",
        "tool": "search_code" if coverage_type == "search_snippet" else "read_file",
        "outcome": outcome,
        "paths": [CONSUMER_PATH],
        "coverage_type": coverage_type,
        "observed_state": observed_state,
        "source_ref": source_ref,
    }


def _meta(entry=None):
    return {
        "head_sha": HEAD,
        "analyzer_result": {
            "pr_type": "code",
            "risk_domains": ["state"],
        },
        "evidence_catalog": [
            {
                "id": f"path:{DELETED_PATH}",
                "source_type": "diff",
                "outcome": "hit",
                "paths": [DELETED_PATH],
                "coverage_type": "changed_region",
            },
            entry if entry is not None else _content_event(),
        ],
    }


def _deleted_finding():
    return {
        "id": "F1",
        "finding_type": "bug",
        "priority": "P1",
        "confidence": "High",
        "evidence_status": "verified",
        "claim_scope": "bounded_context",
        "blocking": True,
        "visibility": "headline",
        "headline": "Removed task state still has a surviving consumer",
        "file_path": DELETED_PATH,
        "code_snippet": DELETED_SNIPPET,
        "comment": (
            "The deletion removes the state that the current-head consumer "
            "still reads."
        ),
        "suggested_code": (
            "Restore a compatible state source or update the surviving "
            "consumer as part of the same change."
        ),
        "suggestion_type": "CONCEPTUAL_ADVICE",
        "required_evidence_refs": [
            f"path:{DELETED_PATH}",
            "ev_surviving_consumer",
        ],
        "supporting_evidence_refs": [],
        "evidence_refs": [
            f"path:{DELETED_PATH}",
            "ev_surviving_consumer",
        ],
    }


def _raw_review():
    return {
        "schema_version": 3,
        "decision": {
            "verdict": "blocked_findings",
            "public_sentence": (
                "A surviving current-head consumer still depends on the "
                "deleted task state."
            ),
            "confidence": "high",
            "pr_type": "code",
            "risk_domains": [],
            "reasons": [
                {
                    "text": "The deleted state still has a live consumer.",
                    "refs": ["F1"],
                }
            ],
        },
        "owner_action": [
            {
                "text": "Restore the contract or update the surviving consumer.",
                "resolves": ["F1"],
            }
        ],
        "findings": [_deleted_finding()],
        "material_unknowns": [],
        "evidence_scope": [
            f"path:{DELETED_PATH}",
            "ev_surviving_consumer",
        ],
        "diagram": None,
    }


class DeletedRegionAnchorContractTests(unittest.TestCase):
    """Sanitized screeps#2601 deletion-anchor replay and controls."""

    def test_anchor_classification_distinguishes_both_diff_images(self):
        post_change_details = """# Pull Request #7

## File Changes
### src/app.py
```diff
@@ -1 +1 @@
-value = compute(1)
+value = compute(2)
```
"""
        self.assertEqual(
            classify_changed_region_anchor(
                "src/app.py",
                "value = compute(2)",
                post_change_details,
            ),
            "post_change",
        )
        self.assertEqual(
            classify_changed_region_anchor(
                DELETED_PATH,
                DELETED_SNIPPET,
                PR_DETAILS,
            ),
            "deleted_region",
        )

        invalid = (
            "const task = room._tasks[taskId];\n"
            "return task;"
        )
        for snippet in (
            invalid,
            "delete room._tasks[otherTaskId];",
            "@@ -10,4 +10,3 @@\n-delete room._tasks[taskId];",
            "",
        ):
            with self.subTest(snippet=snippet):
                self.assertEqual(
                    classify_changed_region_anchor(
                        DELETED_PATH,
                        snippet,
                        PR_DETAILS,
                    ),
                    "invalid",
                )

    def test_raw_deleted_inline_and_replacement_are_rejected_before_projection(self):
        raw = _raw_review()
        finding = raw["findings"][0]
        finding["visibility"] = "inline"
        finding["suggestion_type"] = "DIRECT_REPLACEMENT"

        with self.assertRaises(ReviewContractError):
            build_v3_review(
                raw,
                PR_DETAILS,
                _meta(),
                strict=True,
            )

    def test_fixed_presentation_locally_degrades_deleted_inline_surface(self):
        result = compile_presentation_v1(
            {
                "version": "presentation_v1",
                "decision": {
                    "verdict": "blocking",
                    "confidence": "High",
                    "summary": (
                        "The removed task state still has a surviving "
                        "current-head consumer."
                    ),
                    "owner_actions": [
                        "Restore the contract or update the surviving consumer."
                    ],
                },
                "findings": [
                    {
                        "headline": (
                            "Removed task state still has a surviving consumer"
                        ),
                        "priority": "P1",
                        "category": "bug",
                        "confidence": "High",
                        "file_path": DELETED_PATH,
                        "code_snippet": DELETED_SNIPPET,
                        "analysis": (
                            "The deletion removes state that the current-head "
                            "consumer still reads."
                        ),
                        "owner_action": (
                            "Restore a compatible state source or update the "
                            "consumer."
                        ),
                        "required_evidence_refs": [
                            f"path:{DELETED_PATH}",
                            "ev_surviving_consumer"
                        ],
                        "supporting_evidence_refs": [],
                        "placement": "inline",
                        "suggestion": {
                            "type": "DIRECT_REPLACEMENT",
                            "content": (
                                "Restore a compatible state source or update "
                                "the consumer."
                            ),
                        },
                    }
                ],
                "material_unknowns": [],
                "confidence_checks": [],
                "diagram": None,
            },
            pr_details=PR_DETAILS,
            context_meta=_meta(),
        )

        self.assertEqual(result.status, "publishable")
        retained = result.review["v3_review"]["findings"][0]
        self.assertTrue(retained["blocking"])
        self.assertEqual(retained["priority"], "P1")
        self.assertEqual(retained["visibility"], "headline")
        self.assertNotIn("suggestion_type", retained)
        self.assertNotIn("suggested_code", retained)
        self.assertEqual(result.review["inline_comments"], [])

    def test_each_positive_current_head_content_shape_can_support_deletion(self):
        for coverage in ("full_file", "file_slice", "search_snippet"):
            with self.subTest(coverage=coverage):
                entry = _content_event(coverage)
                if coverage == "search_snippet":
                    entry.update(
                        {
                            "source_ref": "default_branch_search",
                            "head_reread_outcome": "relocated_at_head",
                            "search_hit_lineage": [
                                {
                                    "path": CONSUMER_PATH,
                                    "outcome": "relocated_at_head",
                                    "head_sha": HEAD,
                                }
                            ],
                        }
                    )
                raw = _raw_review()
                meta = _meta(entry)

                capability = finding_evidence_capability(
                    raw["findings"][0],
                    pr_details=PR_DETAILS,
                    context_meta=meta,
                )
                self.assertEqual(capability["anchor_class"], "deleted_region")
                self.assertTrue(capability["critical_supported"])
                validate_raw_v3_review(
                    raw,
                    context_meta=meta,
                    pr_details=PR_DETAILS,
                )

    def test_non_content_or_non_current_evidence_cannot_support_deletion(self):
        cases = {
            "diff_only": {
                "id": "ev_surviving_consumer",
                "source_type": "diff",
                "outcome": "hit",
                "paths": [DELETED_PATH],
                "coverage_type": "changed_region",
            },
            "directory_inventory": _content_event("directory_inventory"),
            "exact_path_state": {
                **_content_event("exact_path_state"),
                "observed_state": "present",
            },
            "no_hit": _content_event(outcome="no_hit"),
            "wrong_head": _content_event(source_ref=f"pr_head:{OTHER_HEAD}"),
            "content_unobserved": _content_event(
                observed_state="content_unobserved"
            ),
            "missing_head_lineage": _content_event(source_ref=""),
            "pathless_content": {
                **_content_event(),
                "paths": [],
            },
        }

        for label, entry in cases.items():
            with self.subTest(label=label):
                raw = _raw_review()
                meta = _meta(entry)
                capability = finding_evidence_capability(
                    raw["findings"][0],
                    pr_details=PR_DETAILS,
                    context_meta=meta,
                )
                self.assertFalse(capability["critical_supported"])
                with self.assertRaises(ReviewContractError):
                    validate_raw_v3_review(
                        raw,
                        context_meta=meta,
                        pr_details=PR_DETAILS,
                    )

        missing_expected_head = _meta()
        missing_expected_head.pop("head_sha")
        capability = finding_evidence_capability(
            _raw_review()["findings"][0],
            pr_details=PR_DETAILS,
            context_meta=missing_expected_head,
        )
        self.assertFalse(capability["critical_supported"])

    def test_deleted_critical_finding_requires_bounded_scope(self):
        raw = _raw_review()
        raw["findings"][0]["claim_scope"] = "changed_region"

        capability = finding_evidence_capability(
            raw["findings"][0],
            pr_details=PR_DETAILS,
            context_meta=_meta(),
        )
        self.assertFalse(capability["critical_supported"])
        with self.assertRaisesRegex(
            ReviewContractError,
            "claim_scope=bounded_context",
        ):
            validate_raw_v3_review(
                raw,
                context_meta=_meta(),
                pr_details=PR_DETAILS,
            )

    def test_raw_deleted_inline_and_direct_replacement_fail_the_contract(self):
        raw = _raw_review()
        raw["findings"][0]["visibility"] = "inline"
        raw["findings"][0]["suggestion_type"] = "DIRECT_REPLACEMENT"

        with self.assertRaises(ReviewContractError) as caught:
            validate_raw_v3_review(
                raw,
                context_meta=_meta(),
                pr_details=PR_DETAILS,
            )

        codes = {item.code for item in caught.exception.violations}
        self.assertIn("snippet_contract_invalid", codes)
        self.assertIn("cross_field_invariant", codes)

    def test_post_change_critical_behavior_remains_unchanged(self):
        details = """# Pull Request #7

## File Changes
### src/app.py
```diff
@@ -1 +1 @@
-value = 1
+value = 2
```
"""
        raw = _raw_review()
        finding = raw["findings"][0]
        finding.update(
            {
                "file_path": "src/app.py",
                "code_snippet": "value = 2",
                "claim_scope": "changed_region",
                "visibility": "inline",
                "suggested_code": "value = 2",
                "suggestion_type": "DIRECT_REPLACEMENT",
                "required_evidence_refs": ["path:src/app.py"],
                "supporting_evidence_refs": [],
                "evidence_refs": ["path:src/app.py"],
            }
        )
        raw["evidence_scope"] = ["path:src/app.py"]
        meta = {
            "head_sha": HEAD,
            "analyzer_result": {
                "pr_type": "code",
                "risk_domains": ["state"],
            },
            "evidence_catalog": [
                {
                    "id": "path:src/app.py",
                    "source_type": "diff",
                    "outcome": "hit",
                    "paths": ["src/app.py"],
                    "coverage_type": "changed_region",
                }
            ],
        }

        rendered = build_v3_review(
            raw,
            details,
            meta,
            strict=True,
        )
        retained = rendered["v3_review"]["findings"][0]
        self.assertEqual(retained["visibility"], "inline")
        self.assertEqual(retained["suggestion_type"], "DIRECT_REPLACEMENT")
        self.assertEqual(len(rendered["inline_comments"]), 1)


if __name__ == "__main__":
    unittest.main()

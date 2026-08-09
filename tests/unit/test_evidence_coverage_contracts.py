import copy
import unittest

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline.review.evidence_contract import (
    ReviewContractError,
    ReviewContractViolation,
    build_review_evidence_catalog,
    visible_ci_check_labels,
)
from lambdas.LlamaPReviewPipeline.review.v3 import validate_raw_v3_review


HEAD = "a" * 40


def _raw_finding(*, claim_scope="whole_file", evidence_refs=None):
    return {
        "schema_version": 3,
        "decision": {
            "verdict": "clear",
            "public_sentence": (
                "No review blocker found in the cited evidence."
            ),
            "confidence": "medium",
            "pr_type": "code",
            "risk_domains": [],
            "reasons": [],
        },
        "owner_action": [],
        "findings": [
            {
                "id": "F1",
                "finding_type": "note",
                "priority": "P2",
                "confidence": "Medium",
                "evidence_status": "verified",
                "claim_scope": claim_scope,
                "blocking": False,
                "visibility": "collapsed",
                "headline": "A bounded observation",
                "file_path": "src/app.py",
                "code_snippet": "",
                "comment": "This fixture tests only evidence coverage.",
                "required_evidence_refs": list(evidence_refs or ["ev_1"]),
                "supporting_evidence_refs": [],
                "evidence_refs": list(evidence_refs or ["ev_1"]),
            }
        ],
        "material_unknowns": [],
        "evidence_scope": [],
        "diagram": None,
    }


def _catalog(coverage_type, *, outcome="hit", path="src/app.py"):
    return {
        "head_sha": HEAD,
        "evidence_catalog": [
            {
                "id": "ev_1",
                "source_type": "pfr",
                "tool": "read_file",
                "outcome": outcome,
                "paths": [path],
                "coverage_type": coverage_type,
                "source_ref": f"pr_head:{HEAD}",
            }
        ]
    }


class EvidenceCoverageContractsTest(unittest.TestCase):
    def test_changed_region_catalog_does_not_embed_planner_question_bindings(self):
        catalog = build_review_evidence_catalog(
            {
                "file_changes": [
                    {
                        "file_path": "src/app.py",
                        "diff": "@@ -1 +1 @@\n-old_call()\n+new_call()\n",
                    },
                    {
                        "file_path": "assets/logo.png",
                        "diff": "Binary files differ",
                    },
                ]
            },
            evidence_ledger={
                "questions": [
                    {
                        "id": "q_read",
                        "tool": "read_file",
                        "args": {
                            "path": "src/app.py",
                            "mode": "content",
                        },
                    },
                    {
                        "id": "q_search",
                        "tool": "search_code",
                        "args": {"query": "new_call("},
                    },
                    {
                        "id": "q_other",
                        "tool": "read_file",
                        "args": {
                            "path": "src/other.py",
                            "mode": "content",
                        },
                    },
                ]
            },
        )

        by_id = {item["id"]: item for item in catalog}
        self.assertNotIn("question_ids", by_id["path:src/app.py"])
        self.assertNotIn("question_ids", by_id["path:assets/logo.png"])

    def test_catalog_preserves_every_stable_coverage_type(self):
        expected = {
            "changed_region",
            "search_snippet",
            "file_slice",
            "full_file",
            "directory_inventory",
            "non_repository",
        }
        ledger = {
            "evidence_events": [
                {
                    "id": f"ev_{index}",
                    "tool": "read_file",
                    "outcome": "hit",
                    "paths": ["src/app.py"],
                    "coverage_type": coverage,
                }
                for index, coverage in enumerate(sorted(expected))
            ]
        }

        catalog = build_review_evidence_catalog({}, evidence_ledger=ledger)

        self.assertEqual(
            {item["coverage_type"] for item in catalog},
            expected,
        )

    def test_catalog_preserves_content_free_search_head_lineage(self):
        ledger = {
            "evidence_events": [
                {
                    "id": "ev_search",
                    "tool": "search_code",
                    "outcome": "hit",
                    "paths": ["src/app.py"],
                    "source_ref": "default_branch_search",
                    "head_reread_outcome": "relocated_at_head",
                    "coverage_type": "search_snippet",
                    "search_hit_lineage": [
                        {
                            "path": "src/app.py",
                            "outcome": "relocated_at_head",
                            "head_sha": "a" * 40,
                            "snippet": "secret source text",
                            "query": "private query",
                        }
                    ],
                }
            ]
        }

        item = build_review_evidence_catalog({}, evidence_ledger=ledger)[0]

        self.assertEqual(item["head_reread_outcome"], "relocated_at_head")
        self.assertEqual(
            item["search_hit_lineage"],
            [
                {
                    "path": "src/app.py",
                    "outcome": "relocated_at_head",
                    "head_sha": "a" * 40,
                }
            ],
        )
        self.assertNotIn("secret source text", repr(item))
        self.assertNotIn("private query", repr(item))

    def test_only_same_path_full_file_can_support_promoted_whole_file_claim(self):
        accepted = _raw_finding()
        validate_raw_v3_review(accepted, context_meta=_catalog("full_file"))

        cases = (
            ("changed_region", "hit", "src/app.py"),
            ("search_snippet", "hit", "src/app.py"),
            ("search_snippet", "no_hit", "src/app.py"),
            ("file_slice", "hit", "src/app.py"),
            ("directory_inventory", "hit", "src/app.py"),
            ("non_repository", "hit", "src/app.py"),
            ("full_file", "error", "src/app.py"),
            ("full_file", "hit", "src/other.py"),
        )
        for coverage, outcome, path in cases:
            with self.subTest(coverage=coverage, outcome=outcome, path=path):
                with self.assertRaises(ReviewContractError) as caught:
                    validate_raw_v3_review(
                        copy.deepcopy(accepted),
                        context_meta=_catalog(coverage, outcome=outcome, path=path),
                    )
                self.assertTrue(
                    any(
                        item.code in {
                            "claim_scope_coverage_mismatch",
                            "evidence_ref_invalid",
                        }
                        for item in caught.exception.violations
                    )
                )

    def test_partial_evidence_can_support_only_bounded_claim_scope(self):
        for coverage in ("changed_region", "search_snippet", "file_slice"):
            with self.subTest(coverage=coverage):
                validate_raw_v3_review(
                    _raw_finding(claim_scope="bounded_context"),
                    context_meta=_catalog(coverage),
                )

    def test_repository_scope_cannot_be_promoted_under_bounded_retrieval(self):
        with self.assertRaises(ReviewContractError) as caught:
            validate_raw_v3_review(
                _raw_finding(claim_scope="repository"),
                context_meta=_catalog("full_file"),
            )
        violation = next(
            item
            for item in caught.exception.violations
            if item.code == "claim_scope_coverage_mismatch"
        )
        self.assertEqual(violation.location, "$.findings[0].evidence_refs")

    def test_validator_emits_typed_occurrences_without_parsing_error_prose(self):
        raw = _raw_finding(claim_scope="bounded_context")
        raw["findings"][0]["priority"] = "P9"
        with self.assertRaises(ReviewContractError) as caught:
            validate_raw_v3_review(raw, context_meta=_catalog("file_slice"))

        violation = next(
            item for item in caught.exception.violations if item.location.endswith(".priority")
        )
        self.assertIsInstance(violation, ReviewContractViolation)
        self.assertEqual(violation.code, "enum_invalid")
        self.assertEqual(violation.location, "$.findings[0].priority")

    def test_visible_ci_names_group_casefolded_whitespace_but_keep_count(self):
        label = visible_ci_check_labels(
            [
                {"identity": "check:1", "name": "Build   Linux"},
                {"identity": "check:2", "name": "build linux"},
                {"identity": "check:3", "name": "BUILD LINUX"},
                {"identity": "check:4", "name": "Unit tests"},
            ]
        )

        self.assertEqual(label, "`Build Linux` ×3, `Unit tests`")


if __name__ == "__main__":
    unittest.main()

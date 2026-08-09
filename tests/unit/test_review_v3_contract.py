import copy
import unittest

from lambdas.LlamaPReviewPipeline.review.evidence_contract import (
    ReviewContractError,
)
from lambdas.LlamaPReviewPipeline.review.projection import (
    build_v3_review,
    get_internal_review,
    invalidate_v3_changed_ci_evidence,
    project_evidence_scope,
    v3_review_to_raw,
)
from lambdas.LlamaPReviewPipeline.review.render import render_v3_markdown
from lambdas.LlamaPReviewPipeline.review import v3 as v3_contract
from lambdas.LlamaPReviewPipeline.review.v3 import (
    finding_evidence_capability,
    validate_raw_v3_review,
)


HEAD = "a" * 40
OTHER_HEAD = "b" * 40
PR_DETAILS = """# Pull Request #7

## File Changes
### src/app.py
```diff
@@ -1,2 +1,2 @@
-value = 1
+value = 2
 keep = True
```
"""


def context_meta(*, fetch_status="healthy"):
    return {
        "head_sha": HEAD,
        "analyzer_result": {
            "pr_type": "code",
            "risk_domains": ["state"],
        },
        "fetch_health": {"status": fetch_status},
        "evidence_catalog": [
            {
                "id": "path:src/app.py",
                "source_type": "diff",
                "outcome": "hit",
                "paths": ["src/app.py"],
                "coverage_type": "changed_region",
            },
            {
                "id": "ev_read_app",
                "source_type": "pfr",
                "tool": "read_file",
                "outcome": "hit",
                "paths": ["src/app.py"],
                "coverage_type": "full_file",
                "source_ref": f"pr_head:{HEAD}",
            },
            {
                "id": "ev_no_hit",
                "source_type": "pfr",
                "tool": "search_code",
                "outcome": "no_hit",
                "paths": ["src/app.py"],
                "coverage_type": "search_snippet",
                "source_ref": "default_branch:main",
            },
        ],
    }


def raw_v3():
    return {
        "schema_version": 3,
        "decision": {
            "verdict": "clear",
            "public_sentence": "No review blocker found; the change is locally consistent with its visible contract.",
            "confidence": "high",
            "pr_type": "code",
            "risk_domains": [],
            "reasons": [
                {"text": "The changed assignment remains internally consistent.", "refs": ["F1"]}
            ],
        },
        "owner_action": [],
        "findings": [
            {
                "id": "F1",
                "finding_type": "note",
                "priority": "P2",
                "confidence": "High",
                "evidence_status": "verified",
                "claim_scope": "changed_region",
                "blocking": False,
                "visibility": "collapsed",
                "headline": "The local assignment remains consistent",
                "file_path": "src/app.py",
                "code_snippet": "value = 2",
                "comment": "The changed value is used consistently in this region.",
                "suggested_code": None,
                "suggestion_type": None,
                "required_evidence_refs": ["path:src/app.py"],
                "supporting_evidence_refs": [],
                "evidence_refs": ["path:src/app.py"],
            }
        ],
        "material_unknowns": [],
        "evidence_scope": ["ev_read_app"],
        "diagram": None,
    }


def set_finding_evidence(finding, *, required=(), supporting=()):
    finding["required_evidence_refs"] = list(required)
    finding["supporting_evidence_refs"] = list(supporting)
    finding["evidence_refs"] = list(
        dict.fromkeys([*required, *supporting])
    )


def search_context(*, outcome, path="src/app.py", head_sha=HEAD):
    meta = context_meta()
    meta["evidence_catalog"].append(
        {
            "id": "ev_search_app",
            "source_type": "pfr",
            "tool": "search_code",
            "outcome": "hit",
            "paths": [path, "src/other.py"],
            "coverage_type": "search_snippet",
            "source_ref": "default_branch_search",
            "head_reread_outcome": (
                "relocated_at_head"
                if outcome == "relocated_at_head"
                else "partial_head_relocation"
            ),
            "search_hit_lineage": [
                {
                    "path": path,
                    "outcome": outcome,
                    "head_sha": head_sha,
                },
                {
                    "path": "src/other.py",
                    "outcome": "relocated_at_head",
                    "head_sha": HEAD,
                },
            ],
        }
    )
    return meta


class ReviewV3ContractTests(unittest.TestCase):
    def test_public_facade_and_validation_order_are_stable(self):
        expected_constants = {
            "SCHEMA_VERSION": 3,
            "ALLOWED_MODEL_VERDICTS": {
                "blocked_findings",
                "unverified",
                "clear",
            },
            "ALLOWED_DIAGRAM_PURPOSES": {"risk_path", "pr_flow_map"},
            "ALLOWED_FINDING_TYPES": {
                "bug",
                "security",
                "breaking-change",
                "test-gap",
                "question",
                "note",
            },
            "ALLOWED_PRIORITIES": {"P0", "P1", "P2"},
            "BLOCKING_PRIORITIES": {"P0", "P1"},
            "ALLOWED_CONFIDENCE": {"High", "Medium", "Low"},
            "ALLOWED_EVIDENCE_STATUS": {
                "verified",
                "unverified",
                "contradicted",
            },
            "ALLOWED_CLAIM_SCOPES": {
                "changed_region",
                "bounded_context",
                "whole_file",
                "repository",
            },
            "ALLOWED_VISIBILITY": {"inline", "headline", "collapsed"},
            "MAX_DECISION_REASONS": 3,
            "MAX_OWNER_ACTIONS": 2,
            "MAX_HEADLINE_FINDINGS": 2,
            "MAX_INLINE_FINDINGS": 4,
            "MAX_NONBLOCKING_INLINE_FINDINGS": 1,
            "MAX_VISIBLE_SCOPE_ITEMS": 6,
        }
        for name, expected in expected_constants.items():
            self.assertEqual(getattr(v3_contract, name), expected, name)
        self.assertEqual(v3_contract.FINDING_ID_RE.pattern, r"^F[1-9][0-9]*$")
        self.assertEqual(v3_contract.UNKNOWN_ID_RE.pattern, r"^U[1-9][0-9]*$")
        self.assertIs(v3_contract.ReviewContractError, ReviewContractError)
        self.assertIs(
            v3_contract.finding_evidence_capability,
            finding_evidence_capability,
        )
        self.assertIs(
            v3_contract.validate_raw_v3_review,
            validate_raw_v3_review,
        )
        self.assertTrue(callable(v3_contract.relationship_verdict))
        self.assertTrue(
            callable(v3_contract.prepare_raw_v3_for_strict_validation)
        )

        raw = {
            "schema_version": "3",
            "decision": {
                "verdict": "clear",
                "public_sentence": "bad",
                "confidence": "x",
                "pr_type": "x",
                "risk_domains": "bad",
                "reasons": "bad",
                "extra": 1,
            },
            "owner_action": "bad",
            "findings": "bad",
            "material_unknowns": "bad",
            "evidence_scope": "bad",
            "diagram": {
                "purpose": "x",
                "description": "",
                "mermaid": "x",
                "finding_refs": "bad",
                "evidence_refs": "bad",
                "extra": 1,
            },
            "rendering_plan": [],
            "extra": 1,
        }
        with self.assertRaises(ReviewContractError) as caught:
            validate_raw_v3_review(raw)

        self.assertEqual(
            [
                (item.code, item.location, item.message)
                for item in caught.exception.violations
            ],
            [
                (
                    "field_type_invalid",
                    "$.schema_version",
                    "schema_version must be integer 3",
                ),
                (
                    "visibility_contract_invalid",
                    "$.decision.public_sentence",
                    "clear public_sentence must begin 'No review blocker found'",
                ),
                (
                    "enum_invalid",
                    "$.decision.confidence",
                    "decision.confidence has an invalid enum value",
                ),
                (
                    "enum_invalid",
                    "$.decision.pr_type",
                    "decision.pr_type has an invalid enum value",
                ),
                (
                    "field_type_invalid",
                    "$.decision.risk_domains",
                    "$.decision.risk_domains must be an array",
                ),
                (
                    "field_type_invalid",
                    "$.findings",
                    "findings must be an array",
                ),
                (
                    "field_type_invalid",
                    "$.material_unknowns",
                    "material_unknowns must be an array",
                ),
                (
                    "field_type_invalid",
                    "$.decision.reasons",
                    "decision.reasons must be an array",
                ),
                (
                    "field_type_invalid",
                    "$.owner_action",
                    "owner_action must be an array",
                ),
                (
                    "field_type_invalid",
                    "$.evidence_scope",
                    "$.evidence_scope must be an array",
                ),
                (
                    "diagram_contract_invalid",
                    "$.diagram.purpose",
                    "diagram.purpose has an invalid enum value",
                ),
                (
                    "field_type_invalid",
                    "$.diagram.description",
                    "$.diagram.description must be a non-empty string",
                ),
                (
                    "diagram_contract_invalid",
                    "$.diagram.mermaid",
                    "diagram.mermaid is not GitHub-safe sequenceDiagram syntax",
                ),
                (
                    "field_type_invalid",
                    "$.diagram.finding_refs",
                    "$.diagram.finding_refs must be an array",
                ),
                (
                    "field_type_invalid",
                    "$.diagram.evidence_refs",
                    "$.diagram.evidence_refs must be an array",
                ),
                (
                    "diagram_contract_invalid",
                    "$.diagram",
                    "diagram must reference a finding or catalog evidence",
                ),
                (
                    "field_type_invalid",
                    "$.rendering_plan",
                    "rendering_plan must be an object",
                ),
            ],
        )
        self.assertEqual(
            caught.exception.warnings,
            [
                "ignored extra root field: extra",
                "ignored extra decision field: extra",
                "ignored extra diagram field: extra",
            ],
        )

    def test_renderer_uses_code_owned_verdict_headings(self):
        expected = {
            "clear": "### LlamaPReview — No blocking issues found",
            "unverified": "### LlamaPReview — Verification needed",
            "blocked_findings": "### LlamaPReview — Blocking issues found",
        }
        for verdict, heading in expected.items():
            with self.subTest(verdict=verdict):
                rendered = render_v3_markdown(
                    {
                        "visible_verdict": verdict,
                        "decision": {"public_sentence": ""},
                        "findings": [],
                        "material_unknowns": [],
                        "evidence_scope": [],
                        "owner_action": [],
                        "diagram": None,
                    }
                )
                self.assertEqual(rendered.splitlines()[0], heading)
                self.assertNotIn("Recommendation:", rendered)

    def test_nonclear_renderer_does_not_rewrite_model_sentence(self):
        sentence = "Verify the current-head runtime contract before merging."
        rendered = render_v3_markdown(
            {
                "visible_verdict": "unverified",
                "decision": {"public_sentence": sentence},
                "findings": [],
                "material_unknowns": [],
                "evidence_scope": [],
                "owner_action": [],
                "diagram": None,
            }
        )
        self.assertEqual(
            rendered,
            "### LlamaPReview — Verification needed\n\n" + sentence,
        )

    def test_projection_does_not_demote_model_declared_priority(self):
        raw = raw_v3()
        finding = raw["findings"][0]
        finding.update(
            {
                "priority": "P1",
                "blocking": False,
                "visibility": "headline",
                "evidence_status": "unverified",
                "code_snippet": "value = 2",
                "suggested_code": "value = 3",
                "suggestion_type": "DIRECT_REPLACEMENT",
            }
        )

        rendered = build_v3_review(raw, PR_DETAILS, context_meta(), strict=True)
        normalized = rendered["v3_review"]["findings"][0]
        self.assertEqual(normalized["priority"], "P1")
        self.assertEqual(normalized["visibility"], "headline")
        self.assertEqual(normalized["evidence_status"], "unverified")
        self.assertEqual(normalized["code_snippet"], "value = 2")
        self.assertEqual(normalized["suggestion_type"], "CONCEPTUAL_ADVICE")
        self.assertEqual(normalized["suggested_code"], "value = 3")

    def test_nonblocking_verified_p1_with_real_changed_snippet_is_preserved(self):
        raw = raw_v3()
        finding = raw["findings"][0]
        finding.update(
            {
                "priority": "P1",
                "blocking": False,
                "visibility": "headline",
                "evidence_status": "verified",
                "code_snippet": "value = 2",
            }
        )

        rendered = build_v3_review(raw, PR_DETAILS, context_meta(), strict=True)
        normalized = rendered["v3_review"]["findings"][0]
        self.assertEqual(normalized["priority"], "P1")
        self.assertEqual(normalized["visibility"], "headline")

    def test_strict_v3_build_uses_objective_catalog_projection(self):
        rendered = build_v3_review(
            raw_v3(),
            PR_DETAILS,
            context_meta(),
            strict=True,
        )

        self.assertEqual(rendered["schema_version"], 3)
        self.assertIn("v3_review", rendered)
        review = rendered["v3_review"]
        self.assertEqual(review["decision"]["risk_domains"], ["state"])
        self.assertEqual(review["evidence_scope"][0]["paths"], ["src/app.py"])
        self.assertIn("complete PR-head file", review["evidence_scope"][0]["description"])
        self.assertNotIn("ev_read_app", rendered["pr_review_comment"])
        self.assertNotIn("F1", rendered["pr_review_comment"])

    def test_fence_only_suggestion_is_omitted_from_details(self):
        raw = raw_v3()
        raw["findings"][0].update(
            {
                "suggested_code": "```python\n   \n```",
                "suggestion_type": "DIRECT_REPLACEMENT",
            }
        )

        rendered = build_v3_review(
            raw,
            PR_DETAILS,
            context_meta(),
            strict=True,
        )["pr_review_comment"]

        self.assertNotIn("Suggested direct replacement", rendered)
        self.assertNotIn("Conceptual guidance", rendered)

    def test_unknown_ids_and_cross_references_are_hard_contracts(self):
        raw = raw_v3()
        raw["decision"]["verdict"] = "unverified"
        raw["decision"]["reasons"] = [
            {"text": "One release condition remains unresolved.", "refs": ["U1"]}
        ]
        raw["material_unknowns"] = [
            {
                "id": "U1",
                "claim": "The release condition was not executed.",
                "how_to_check": "Run the release smoke test.",
                "affects_merge": True,
                "evidence_refs": [],
            }
        ]
        raw["owner_action"] = [
            {"text": "Run the release smoke test.", "resolves": ["U1"]}
        ]
        validate_raw_v3_review(raw, context_meta=context_meta(), pr_details=PR_DETAILS)

        dangling = copy.deepcopy(raw)
        dangling["owner_action"][0]["resolves"] = ["U2"]
        with self.assertRaisesRegex(ValueError, "unknown id"):
            validate_raw_v3_review(
                dangling, context_meta=context_meta(), pr_details=PR_DETAILS
            )

        duplicate = copy.deepcopy(raw)
        duplicate["material_unknowns"].append(copy.deepcopy(raw["material_unknowns"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate unknown id"):
            validate_raw_v3_review(
                duplicate, context_meta=context_meta(), pr_details=PR_DETAILS
            )

    def test_scope_and_head_are_validated_without_claim_keywords(self):
        whole_file = raw_v3()
        whole_file["findings"][0]["claim_scope"] = "whole_file"
        set_finding_evidence(
            whole_file["findings"][0],
            required=("ev_read_app",),
        )
        validate_raw_v3_review(
            whole_file, context_meta=context_meta(), pr_details=PR_DETAILS
        )

        missing_coverage = copy.deepcopy(whole_file)
        set_finding_evidence(
            missing_coverage["findings"][0],
            required=("path:src/app.py",),
        )
        with self.assertRaisesRegex(ValueError, "exceeds the cited evidence capability"):
            validate_raw_v3_review(
                missing_coverage,
                context_meta=context_meta(),
                pr_details=PR_DETAILS,
            )

        repository = raw_v3()
        repository["findings"][0]["claim_scope"] = "repository"
        with self.assertRaisesRegex(ValueError, "exceeds the cited evidence capability"):
            validate_raw_v3_review(
                repository, context_meta=context_meta(), pr_details=PR_DETAILS
            )

        wrong_head_meta = context_meta()
        wrong_head_meta["evidence_catalog"][1]["source_ref"] = f"pr_head:{OTHER_HEAD}"
        with self.assertRaisesRegex(ValueError, "different PR head"):
            validate_raw_v3_review(
                whole_file,
                context_meta=wrong_head_meta,
                pr_details=PR_DETAILS,
            )

    def test_no_hit_cannot_enter_finding_scope_or_visible_scope(self):
        raw = raw_v3()
        set_finding_evidence(
            raw["findings"][0],
            required=("ev_no_hit",),
        )
        raw["evidence_scope"] = ["ev_no_hit"]
        with self.assertRaisesRegex(ValueError, "non-supporting"):
            validate_raw_v3_review(raw, context_meta=context_meta(), pr_details=PR_DETAILS)
        self.assertEqual(project_evidence_scope(["ev_no_hit"], context_meta()), [])

    def test_repository_evidence_without_head_lineage_fails_closed(self):
        raw = raw_v3()
        meta = context_meta()
        meta["evidence_catalog"][1].pop("source_ref")

        with self.assertRaisesRegex(ValueError, "exact queued-head lineage"):
            validate_raw_v3_review(
                raw,
                context_meta=meta,
                pr_details=PR_DETAILS,
            )

    def test_changed_snippet_preserves_relative_indentation_and_internal_blanks(self):
        cases = (
            (
                "Python",
                "src/app.py",
                "+if enabled:\n+    return allow()\n+\n+return deny()\n",
                "if enabled:\nreturn allow()\n\nreturn deny()",
            ),
            (
                "YAML",
                "config/app.yml",
                "+service:\n+  enabled: true\n+\n+  mode: safe\n",
                "service:\nenabled: true\n\nmode: safe",
            ),
            (
                "Make",
                "Makefile",
                "+build:\n+\tpython -m build\n+\n+\tpython -m twine check dist/*\n",
                "build:\npython -m build\n\npython -m twine check dist/*",
            ),
        )
        for language, path, diff, collapsed in cases:
            with self.subTest(language=language):
                details = (
                    "# Pull Request #7\n\n"
                    "## File Changes\n"
                    f"### {path}\n"
                    "```diff\n"
                    f"@@ -0,0 +1,4 @@\n{diff}"
                    "```\n"
                )
                raw = raw_v3()
                raw["findings"][0]["file_path"] = path
                raw["findings"][0]["code_snippet"] = collapsed
                set_finding_evidence(
                    raw["findings"][0],
                    required=(f"path:{path}",),
                )
                raw["evidence_scope"] = [f"path:{path}"]
                meta = {
                    "head_sha": HEAD,
                    "evidence_catalog": [
                        {
                            "id": f"path:{path}",
                            "source_type": "diff",
                            "outcome": "hit",
                            "paths": [path],
                            "coverage_type": "changed_region",
                        }
                    ],
                }

                with self.assertRaisesRegex(ValueError, "changed region"):
                    validate_raw_v3_review(
                        raw,
                        context_meta=meta,
                        pr_details=details,
                    )

    def test_changed_snippet_may_omit_only_common_enclosing_indentation(self):
        path = "src/allocator.js"
        details = (
            "# Pull Request #7\n\n"
            "## File Changes\n"
            f"### {path}\n"
            "```diff\n"
            "@@ -0,0 +1,5 @@\n"
            "+function allocate(values) {\n"
            "+  const normalized = values.map(Number);\n"
            "+  if (normalized.length === 0) return [];\n"
            "+  return normalized;\n"
            "+}\n"
            "```\n"
        )
        meta = {
            "head_sha": HEAD,
            "evidence_catalog": [
                {
                    "id": f"path:{path}",
                    "source_type": "diff",
                    "outcome": "hit",
                    "paths": [path],
                    "coverage_type": "changed_region",
                }
            ],
        }
        raw = raw_v3()
        raw["findings"][0]["file_path"] = path
        raw["findings"][0]["code_snippet"] = (
            "const normalized = values.map(Number);\n"
            "if (normalized.length === 0) return [];"
        )
        set_finding_evidence(
            raw["findings"][0],
            required=(f"path:{path}",),
        )
        raw["evidence_scope"] = [f"path:{path}"]

        validate_raw_v3_review(raw, context_meta=meta, pr_details=details)

        raw["findings"][0]["code_snippet"] = (
            "const normalized = values.map(Number);\n"
            "  if (normalized.length === 0) return [];"
        )
        with self.assertRaisesRegex(ValueError, "changed region"):
            validate_raw_v3_review(raw, context_meta=meta, pr_details=details)

    def test_verified_search_evidence_requires_every_hit_at_same_head(self):
        raw = raw_v3()
        raw["findings"][0]["claim_scope"] = "bounded_context"
        set_finding_evidence(
            raw["findings"][0],
            required=("ev_search_app",),
        )

        validate_raw_v3_review(
            raw,
            context_meta=search_context(outcome="relocated_at_head"),
            pr_details=PR_DETAILS,
        )

        different_path = search_context(
            outcome="relocated_at_head",
            path="src/elsewhere.py",
        )
        validate_raw_v3_review(
            copy.deepcopy(raw),
            context_meta=different_path,
            pr_details=PR_DETAILS,
        )

        for meta in (
            search_context(outcome="default_branch_only"),
            search_context(outcome="relocated_at_head", head_sha=OTHER_HEAD),
        ):
            with self.subTest(lineage=meta["evidence_catalog"][-1]):
                with self.assertRaisesRegex(
                    ValueError, "not all relocated to the same PR head"
                ):
                    validate_raw_v3_review(
                        copy.deepcopy(raw),
                        context_meta=meta,
                        pr_details=PR_DETAILS,
                    )

    def test_default_branch_search_cannot_support_even_an_unverified_finding(self):
        raw = raw_v3()
        raw["findings"][0].update(
            {
                "claim_scope": "bounded_context",
                "evidence_status": "unverified",
            }
        )
        set_finding_evidence(
            raw["findings"][0],
            required=("ev_search_app",),
        )
        meta = search_context(outcome="default_branch_only")
        meta["evidence_catalog"][-1]["head_reread_outcome"] = "default_branch_only"
        meta["evidence_catalog"][-1]["search_hit_lineage"][1][
            "outcome"
        ] = "default_branch_only"

        with self.assertRaisesRegex(ValueError, "exact queued-head lineage"):
            validate_raw_v3_review(raw, context_meta=meta, pr_details=PR_DETAILS)
        projected = project_evidence_scope(["ev_search_app"], meta)
        self.assertIn("default-branch snippets", projected[0]["description"])
        self.assertIn("PR-head relocation was unavailable", projected[0]["description"])

    def test_aggregate_search_relocation_is_not_exact_head_proof(self):
        raw = raw_v3()
        raw["findings"][0]["claim_scope"] = "bounded_context"
        set_finding_evidence(
            raw["findings"][0],
            required=("ev_search_app",),
        )
        meta = search_context(outcome="relocated_at_head")
        meta["evidence_catalog"][-1].pop("search_hit_lineage")

        with self.assertRaisesRegex(
            ValueError, "not all relocated to the same PR head"
        ):
            validate_raw_v3_review(raw, context_meta=meta, pr_details=PR_DETAILS)

    def test_every_finding_requires_at_least_one_catalog_evidence_ref(self):
        raw = raw_v3()
        set_finding_evidence(raw["findings"][0])

        with self.assertRaisesRegex(ValueError, "reference at least one"):
            validate_raw_v3_review(
                raw,
                context_meta=context_meta(),
                pr_details=PR_DETAILS,
            )

    def test_supporting_green_ci_is_admitted_but_never_carries_capability(self):
        raw = raw_v3()
        meta = context_meta()
        meta["evidence_catalog"].append(
            {
                "id": "ci:green",
                "source_type": "ci",
                "outcome": "success",
                "paths": ["src/app.py"],
                "coverage_type": "changed_region",
            }
        )
        set_finding_evidence(
            raw["findings"][0],
            required=("path:src/app.py",),
            supporting=("ci:green",),
        )

        capability = finding_evidence_capability(
            raw["findings"][0],
            pr_details=PR_DETAILS,
            context_meta=meta,
        )
        self.assertEqual(capability["usable_refs"], ["path:src/app.py"])
        validate_raw_v3_review(
            raw,
            context_meta=meta,
            pr_details=PR_DETAILS,
        )

    def test_required_ci_dependency_needs_independent_code_capability(self):
        raw = raw_v3()
        meta = context_meta()
        meta["evidence_catalog"].append(
            {
                "id": "ci:required",
                "source_type": "ci",
                "outcome": "failure",
                "paths": ["src/app.py"],
                "coverage_type": "changed_region",
            }
        )
        set_finding_evidence(
            raw["findings"][0],
            required=("path:src/app.py", "ci:required"),
        )

        capability = finding_evidence_capability(
            raw["findings"][0],
            pr_details=PR_DETAILS,
            context_meta=meta,
        )
        self.assertIn("ci:required", capability["usable_refs"])
        self.assertNotIn(
            "ci:required",
            capability["critical_independent_refs"],
        )
        validate_raw_v3_review(
            raw,
            context_meta=meta,
            pr_details=PR_DETAILS,
        )

        set_finding_evidence(
            raw["findings"][0],
            required=("ci:required",),
        )
        with self.assertRaises(ReviewContractError):
            validate_raw_v3_review(
                raw,
                context_meta=meta,
                pr_details=PR_DETAILS,
            )

    def test_split_evidence_union_must_be_exact_and_ordered(self):
        raw = raw_v3()
        raw["findings"][0]["supporting_evidence_refs"] = ["ev_read_app"]

        with self.assertRaises(ReviewContractError):
            validate_raw_v3_review(
                raw,
                context_meta=context_meta(),
                pr_details=PR_DETAILS,
            )

    def test_empty_path_is_only_allowed_for_noninline_p2(self):
        broad = raw_v3()
        finding = broad["findings"][0]
        finding.update(
            {
                "file_path": "",
                "code_snippet": "",
                "claim_scope": "bounded_context",
                "visibility": "collapsed",
            }
        )
        set_finding_evidence(finding, required=("ev_read_app",))
        validate_raw_v3_review(
            broad,
            context_meta=context_meta(),
            pr_details=PR_DETAILS,
        )

        for priority, visibility in (("P1", "headline"), ("P2", "inline")):
            with self.subTest(priority=priority, visibility=visibility):
                invalid = copy.deepcopy(broad)
                invalid["findings"][0]["priority"] = priority
                invalid["findings"][0]["visibility"] = visibility
                with self.assertRaises(ReviewContractError):
                    validate_raw_v3_review(
                        invalid,
                        context_meta=context_meta(),
                        pr_details=PR_DETAILS,
                    )

    def test_diagram_refs_and_mermaid_are_hard_contracts(self):
        raw = raw_v3()
        raw["diagram"] = {
            "purpose": "pr_flow_map",
            "description": "The changed assignment flows into the local state.",
            "mermaid": "```mermaid\nsequenceDiagram\n  A->>B: assign\n```",
            "finding_refs": ["F1"],
            "evidence_refs": ["ev_read_app"],
        }
        rendered = build_v3_review(
            raw, PR_DETAILS, context_meta(), strict=True
        )
        self.assertIn("sequenceDiagram", rendered["pr_review_comment"])
        self.assertNotIn("ev_read_app", rendered["pr_review_comment"])
        diagram_index = rendered["pr_review_comment"].index("#### Change flow")
        details_index = rendered["pr_review_comment"].index("<details>")
        self.assertLess(diagram_index, details_index)
        self.assertEqual(rendered["pr_review_comment"].count("sequenceDiagram"), 1)
        self.assertNotIn(
            "sequenceDiagram",
            rendered["pr_review_comment"][details_index:],
        )

        risk = copy.deepcopy(raw)
        risk["diagram"]["purpose"] = "risk_path"
        risk_rendered = build_v3_review(
            risk, PR_DETAILS, context_meta(), strict=True
        )
        self.assertIn("#### Risk path", risk_rendered["pr_review_comment"])
        self.assertNotIn("#### Change flow", risk_rendered["pr_review_comment"])

        dangling = copy.deepcopy(raw)
        dangling["diagram"]["finding_refs"] = ["F2"]
        with self.assertRaisesRegex(ValueError, "unknown id"):
            validate_raw_v3_review(
                dangling, context_meta=context_meta(), pr_details=PR_DETAILS
            )

        unsafe = copy.deepcopy(raw)
        unsafe["diagram"]["mermaid"] = "```mermaid\nflowchart TD\n A-->B\n```"
        with self.assertRaisesRegex(ValueError, "GitHub-safe"):
            validate_raw_v3_review(
                unsafe, context_meta=context_meta(), pr_details=PR_DETAILS
            )

    def test_visibility_caps_are_hard_failures(self):
        raw = raw_v3()
        raw["findings"] = []
        raw["decision"]["reasons"] = []
        for index in range(3):
            finding = copy.deepcopy(raw_v3()["findings"][0])
            finding["id"] = f"F{index + 1}"
            finding["visibility"] = "headline"
            raw["findings"].append(finding)
        with self.assertRaisesRegex(ValueError, "headline finding cap"):
            validate_raw_v3_review(raw, context_meta=context_meta(), pr_details=PR_DETAILS)

    def test_inline_requires_verified_changed_region_and_file_provenance(self):
        raw = raw_v3()
        raw["findings"][0]["visibility"] = "inline"
        validate_raw_v3_review(raw, context_meta=context_meta(), pr_details=PR_DETAILS)

        unverified = copy.deepcopy(raw)
        unverified["findings"][0]["evidence_status"] = "unverified"
        with self.assertRaisesRegex(ValueError, "inline requires verified"):
            validate_raw_v3_review(
                unverified, context_meta=context_meta(), pr_details=PR_DETAILS
            )

        unrelated = copy.deepcopy(raw)
        unrelated["findings"][0]["file_path"] = "src/missing.py"
        with self.assertRaisesRegex(ValueError, "lacks changed or catalog provenance"):
            validate_raw_v3_review(
                unrelated, context_meta=context_meta(), pr_details=PR_DETAILS
            )

    def test_verdict_reason_must_reference_the_deciding_item(self):
        raw = raw_v3()
        raw["decision"]["verdict"] = "unverified"
        raw["decision"]["reasons"] = [
            {"text": "A merge condition remains unresolved.", "refs": ["F1"]}
        ]
        raw["material_unknowns"] = [
            {
                "id": "U1",
                "claim": "The merge condition was not executed.",
                "how_to_check": "Run the declared validation.",
                "affects_merge": True,
                "evidence_refs": [],
            }
        ]
        with self.assertRaisesRegex(ValueError, "determines the verdict"):
            validate_raw_v3_review(raw, context_meta=context_meta(), pr_details=PR_DETAILS)

    def test_contract_does_not_infer_claim_meaning_from_keywords(self):
        raw = raw_v3()
        raw["decision"]["public_sentence"] = (
            "No review blocker found; the dependency, CI, external version, and absence checks are documented by the reviewer."
        )
        raw["decision"]["reasons"][0]["text"] = (
            "A positive inspection note remains useful to the maintainer."
        )
        raw["findings"][0]["headline"] = "Positive dependency inspection note"
        rendered = build_v3_review(
            raw,
            PR_DETAILS,
            context_meta(fetch_status="partial_or_failed_context"),
            strict=True,
        )
        review = rendered["v3_review"]
        self.assertEqual(review["findings"][0]["headline"], "Positive dependency inspection note")
        self.assertEqual(review["material_unknowns"], [])
        self.assertIn("external version", review["decision"]["public_sentence"])
        self.assertIn(
            "1 non-blocking finding retained — highest: "
            "Positive dependency inspection note.",
            rendered["pr_review_comment"],
        )

    def test_generic_ci_truth_is_internal_and_does_not_create_visible_copy(self):
        meta = context_meta()
        meta["ci_snapshot"] = {
            "schema_version": 1,
            "source": "structured_raw",
            "has_ci": True,
            "blocking_checks": [
                {"identity": "check-1", "classification": "failure", "name": "build"}
            ],
            "action_required_checks": [],
            "pending_checks": [],
            "incomplete_checks": [],
            "checks": [],
        }
        rendered = build_v3_review(raw_v3(), PR_DETAILS, meta, strict=True)
        review = rendered["v3_review"]
        self.assertEqual(review["decision"]["verdict"], "clear")
        self.assertEqual(review["visible_verdict"], "clear")
        self.assertNotIn("ci_gate_status", review)
        self.assertNotIn("ci_failed_evidence_refs", review)
        self.assertEqual(review["material_unknowns"], [])
        self.assertEqual(review["owner_action"], [])
        self.assertNotIn("build", rendered["pr_review_comment"].lower())
        self.assertNotIn("ci", rendered["pr_review_comment"].lower())

    def test_renderer_deduplicates_the_required_clear_sentence_prefix(self):
        raw = raw_v3()
        raw["decision"]["public_sentence"] = (
            "No review blocker found. The observed caller contract remains consistent."
        )
        rendered = build_v3_review(raw, PR_DETAILS, context_meta(), strict=True)
        first_screen = rendered["pr_review_comment"].split("<details>", 1)[0]
        self.assertNotIn("No review blocker found", first_screen)
        self.assertIn(
            "### LlamaPReview — No blocking issues found\n\n"
            "The observed caller contract remains consistent.\n\n"
            "1 non-blocking finding retained — highest: "
            "The local assignment remains consistent.",
            first_screen,
        )
        self.assertNotIn("Recommendation:", first_screen)
        self.assertEqual(
            rendered["v3_review"]["decision"]["public_sentence"],
            raw["decision"]["public_sentence"],
        )

    def test_renderer_omits_a_clear_sentence_that_only_repeats_the_label(self):
        raw = raw_v3()
        raw["decision"]["public_sentence"] = (
            "No review blocker found in the reviewed changes."
        )
        rendered = build_v3_review(raw, PR_DETAILS, context_meta(), strict=True)
        first_line = rendered["pr_review_comment"].splitlines()[0]
        self.assertEqual(
            first_line,
            "### LlamaPReview — No blocking issues found",
        )
        self.assertNotIn("Recommendation:", rendered["pr_review_comment"])

    def test_clear_sentence_prefix_requires_a_real_word_boundary(self):
        raw = raw_v3()
        raw["decision"]["public_sentence"] = (
            "No review blocker founded on the available evidence."
        )
        with self.assertRaises(ReviewContractError):
            build_v3_review(raw, PR_DETAILS, context_meta(), strict=True)

    def test_v3_replay_raw_uses_id_scope_not_rendered_projection(self):
        rendered = build_v3_review(
            raw_v3(), PR_DETAILS, context_meta(), strict=True
        )
        replay = v3_review_to_raw(rendered["v3_review"])
        self.assertEqual(replay["evidence_scope"], ["ev_read_app"])
        self.assertNotIn("visible_verdict", replay)
        self.assertNotIn("ci_gate_status", replay)
        validate_raw_v3_review(
            replay,
            context_meta=context_meta(),
            pr_details=PR_DETAILS,
        )

    def test_required_ci_churn_on_last_blocker_is_typed_nonpublishable(self):
        raw = raw_v3()
        finding = raw["findings"][0]
        finding.update(
            {
                "finding_type": "bug",
                "priority": "P2",
                "blocking": True,
                "visibility": "headline",
            }
        )
        set_finding_evidence(
            finding,
            required=("ci:check-1",),
            supporting=("path:src/app.py",),
        )
        raw["decision"].update(
            {
                "verdict": "blocked_findings",
                "public_sentence": "The exact diagnostic blocks merge.",
                "reasons": [
                    {
                        "text": "The exact diagnostic proves the blocker.",
                        "refs": ["F1"],
                    }
                ],
            }
        )
        raw["owner_action"] = [
            {"text": "Fix the blocker.", "resolves": ["F1"]}
        ]

        result = invalidate_v3_changed_ci_evidence(
            raw,
            changed_ci_refs={"ci:check-1"},
        )

        self.assertFalse(result.publishable)
        self.assertEqual(result.status, "nonpublishable")
        self.assertEqual(
            result.reason_code,
            "last_deciding_item_required_ci_changed",
        )
        self.assertEqual(result.invalidated_item_ids, ("F1",))
        self.assertEqual(
            result.review["decision"]["verdict"],
            "blocked_findings",
        )
        self.assertEqual(result.review["findings"], [])
        self.assertEqual(result.review["material_unknowns"], [])

    def test_supporting_ci_churn_drops_only_the_optional_reference(self):
        raw = raw_v3()
        set_finding_evidence(
            raw["findings"][0],
            required=("path:src/app.py",),
            supporting=("ci:check-1",),
        )

        result = invalidate_v3_changed_ci_evidence(
            raw,
            changed_ci_refs={"ci:check-1"},
        )

        self.assertTrue(result.publishable)
        self.assertEqual(result.status, "locally_degraded")
        self.assertEqual(result.invalidated_item_ids, ())
        self.assertEqual(
            result.dropped_supporting_refs,
            (("F1", "ci:check-1"),),
        )
        retained = result.review["findings"][0]
        self.assertEqual(
            retained["required_evidence_refs"],
            ["path:src/app.py"],
        )
        self.assertEqual(retained["supporting_evidence_refs"], [])
        self.assertEqual(retained["evidence_refs"], ["path:src/app.py"])
        self.assertEqual(result.review["decision"], raw["decision"])

    def test_required_ci_churn_invalidates_only_nondeciding_sibling(self):
        raw = raw_v3()
        blocker = raw["findings"][0]
        blocker.update(
            {
                "finding_type": "bug",
                "priority": "P1",
                "blocking": True,
                "visibility": "inline",
            }
        )
        sibling = copy.deepcopy(blocker)
        sibling.update(
            {
                "id": "F2",
                "priority": "P2",
                "blocking": False,
                "visibility": "collapsed",
                "headline": "The CI diagnostic adds a secondary note",
            }
        )
        set_finding_evidence(
            sibling,
            required=("ci:check-1",),
        )
        raw["findings"].append(sibling)
        raw["decision"].update(
            {
                "verdict": "blocked_findings",
                "public_sentence": "The changed assignment blocks merge.",
                "reasons": [
                    {
                        "text": "The changed assignment breaks the contract.",
                        "refs": ["F1"],
                    },
                    {
                        "text": "The CI diagnostic adds a secondary note.",
                        "refs": ["F2"],
                    },
                ],
            }
        )
        raw["owner_action"] = [
            {"text": "Restore the contract.", "resolves": ["F1"]}
        ]

        result = invalidate_v3_changed_ci_evidence(
            raw,
            changed_ci_refs={"ci:check-1"},
        )

        self.assertTrue(result.publishable)
        self.assertEqual(result.status, "locally_degraded")
        self.assertEqual(result.invalidated_item_ids, ("F2",))
        self.assertEqual(
            [item["id"] for item in result.review["findings"]],
            ["F1"],
        )
        self.assertEqual(
            result.review["decision"]["verdict"],
            "blocked_findings",
        )
        self.assertEqual(
            result.review["decision"]["reasons"],
            [
                {
                    "text": "The changed assignment breaks the contract.",
                    "refs": ["F1"],
                }
            ],
        )
        self.assertEqual(
            result.review["owner_action"],
            [{"text": "Restore the contract.", "resolves": ["F1"]}],
        )

    def test_losing_one_of_two_deciding_items_is_explicitly_degraded(self):
        raw = raw_v3()
        first = raw["findings"][0]
        first.update(
            {
                "finding_type": "bug",
                "priority": "P1",
                "blocking": True,
                "visibility": "inline",
            }
        )
        second = copy.deepcopy(first)
        second.update(
            {
                "id": "F2",
                "visibility": "headline",
                "headline": "A second exact diagnostic blocks merge",
            }
        )
        set_finding_evidence(
            second,
            required=("ci:check-1",),
        )
        raw["findings"].append(second)
        raw["decision"].update(
            {
                "verdict": "blocked_findings",
                "public_sentence": "Two evidenced findings block merge.",
                "reasons": [
                    {
                        "text": "The changed assignment breaks the contract.",
                        "refs": ["F1"],
                    },
                    {
                        "text": "The current diagnostic proves a second issue.",
                        "refs": ["F2"],
                    },
                ],
            }
        )
        raw["owner_action"] = [
            {"text": "Restore the changed contract.", "resolves": ["F1"]},
            {"text": "Resolve the current diagnostic.", "resolves": ["F2"]},
        ]

        result = invalidate_v3_changed_ci_evidence(
            raw,
            changed_ci_refs={"ci:check-1"},
        )

        self.assertTrue(result.publishable)
        self.assertEqual(result.status, "deciding_item_degraded")
        self.assertEqual(
            result.reason_code,
            "deciding_item_required_ci_changed",
        )
        self.assertEqual(result.invalidated_item_ids, ("F2",))
        self.assertEqual(
            [item["id"] for item in result.review["findings"]],
            ["F1"],
        )


if __name__ == "__main__":
    unittest.main()

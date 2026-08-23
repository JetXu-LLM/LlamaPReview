import copy
import json
import unittest

from lambdas.LlamaPReviewPipeline.review.presentation import (
    PRESENTATION_VERSION,
    compile_presentation_v1,
    mark_final_response_incomplete,
    parse_presentation_v1,
)
from lambdas.LlamaPReviewPipeline.review.projection import (
    elect_primary_inline,
)
from lambdas.LlamaPReviewPipeline.review.publish import (
    PUBLIC_FOOTER_MARKER,
    build_main_comment,
)


HEAD = "a" * 40
PR_DETAILS = """# Pull Request #17

## File Changes
### src/app.py
```diff
@@ -1,2 +1,2 @@
-value = build(1)
+value = build(2)
 keep = True
```
"""


def context_meta():
    return {
        "head_sha": HEAD,
        "analyzer_result": {
            "pr_type": "code",
            "risk_domains": ["state"],
        },
        "ci_generation_model_payload": {
            "checks": [
                {
                    "identity": "unit:1",
                    "name": "Focused unit check",
                    "status": "completed",
                    "classification": "success",
                    "conclusion": "success",
                },
                {
                    "identity": "lint:1",
                    "name": "Focused lint check",
                    "status": "completed",
                    "classification": "success",
                    "conclusion": "success",
                },
            ]
        },
        "evidence_catalog": [
            {
                "id": "path:src/app.py",
                "source_type": "diff",
                "outcome": "hit",
                "paths": ["src/app.py"],
                "coverage_type": "changed_region",
            },
            {
                "id": "ci:unit:1",
                "source_type": "ci",
                "outcome": "success",
                "paths": ["src/app.py"],
                "coverage_type": "changed_region",
            },
            {
                "id": "ci:lint:1",
                "source_type": "ci",
                "outcome": "success",
                "paths": ["src/app.py"],
                "coverage_type": "changed_region",
            },
            {
                "id": "ev:consumer",
                "source_type": "file",
                "outcome": "hit",
                "paths": ["src/app.py"],
                "coverage_type": "file_slice",
            },
        ],
    }


def finding(
    *,
    priority="P2",
    category="maintainability",
    headline="The changed value remains locally consistent",
    required=None,
    supporting=None,
    placement="inline",
    suggestion=None,
):
    return {
        "headline": headline,
        "priority": priority,
        "category": category,
        "confidence": "High",
        "file_path": "src/app.py",
        "code_snippet": "value = build(2)",
        "analysis": "The changed expression preserves the local call shape.",
        "owner_action": "Keep the existing focused regression coverage.",
        "required_evidence_refs": (
            ["path:src/app.py"] if required is None else required
        ),
        "supporting_evidence_refs": (
            ["ev:consumer"] if supporting is None else supporting
        ),
        "placement": placement,
        "suggestion": suggestion,
    }


def presentation(
    *,
    verdict="clear",
    confidence="High",
    findings=None,
    unknowns=None,
    diagram=None,
):
    return {
        "version": PRESENTATION_VERSION,
        "decision": {
            "verdict": verdict,
            "confidence": confidence,
            "summary": {
                "clear": (
                    "No review blocker found. The changed expression remains "
                    "consistent with the visible contract."
                ),
                "blocking": (
                    "Do not merge until the changed expression preserves the "
                    "required runtime behavior."
                ),
                "verification_needed": (
                    "Verify the current runtime binding before merging."
                ),
            }[verdict],
            "owner_actions": [],
        },
        "findings": [finding()] if findings is None else findings,
        "material_unknowns": [] if unknowns is None else unknowns,
        "confidence_checks": [
            {
                "check": "Focused unit result",
                "result": "The exact-head check completed successfully.",
                "evidence_refs": ["ci:unit:1"],
            }
        ],
        "diagram": diagram,
    }


def material_unknown(*, refs=None):
    return {
        "missing_fact": "The active runtime binding is not visible in the patch.",
        "impact": "That binding decides whether the new branch is reached.",
        "owner_action": "Confirm the binding in the target environment.",
        "evidence_refs": ["path:src/app.py"] if refs is None else refs,
    }


def failing_ci_meta():
    meta = context_meta()
    meta["ci_generation_model_payload"]["checks"][0].update(
        {
            "name": "Unit check",
            "status": "completed",
            "classification": "failure",
            "conclusion": "failure",
        }
    )
    meta["evidence_catalog"][1]["outcome"] = "failure"
    return meta


def actionable_non_source_ci_meta():
    meta = failing_ci_meta()
    meta["ci_actionable_detail_lineage"] = {
        "schema_version": 2,
        "source": "exact_head_refresh",
        "policy": "fresh_output_and_annotations",
        "outcome": "freshly_observed",
    }
    meta["ci_generation_model_payload"]["checks"][0]["annotations"] = [
        {"message": "PR title must be 72 characters or less."}
    ]
    meta["evidence_catalog"][1].update(
        {"paths": [], "coverage_type": "non_repository"}
    )
    return meta


class PresentationParserTests(unittest.TestCase):
    def test_exact_object_and_single_array_are_parser_proven(self):
        raw = presentation()
        exact = parse_presentation_v1(json.dumps(raw))
        wrapped = parse_presentation_v1(json.dumps([raw]))

        self.assertTrue(exact.parsed)
        self.assertEqual(exact.value, raw)
        self.assertTrue(wrapped.parsed)
        self.assertIn(
            "single_object_array_unwrapped",
            wrapped.normalizations,
        )

    def test_one_fence_or_unique_embedded_object_is_recovered(self):
        payload = json.dumps(presentation())
        fenced = parse_presentation_v1(f"```json\n{payload}\n```")
        embedded = parse_presentation_v1(f"Here is the object:\n{payload}\nDone.")

        self.assertTrue(fenced.parsed)
        self.assertIn("json_outer_fence_removed", fenced.normalizations)
        self.assertTrue(embedded.parsed)
        self.assertIn(
            "unique_embedded_json_object_extracted",
            embedded.normalizations,
        )

    def test_multiple_objects_are_ambiguous(self):
        payload = json.dumps(presentation())
        parsed = parse_presentation_v1(f"{payload}\n{payload}")

        self.assertFalse(parsed.parsed)
        self.assertEqual(parsed.error_kind, "ambiguous_json_objects")

    def test_unescaped_natural_language_quotes_are_recovered_locally(self):
        raw = presentation()
        raw["findings"][0]["analysis"] = (
            'The branch hard-codes two "other" outcomes into dispatch.'
        )
        payload = json.dumps(raw).replace(r'\"other\"', '"other"')

        parsed = parse_presentation_v1(payload)

        self.assertTrue(parsed.parsed)
        self.assertEqual(
            parsed.value["findings"][0]["analysis"],
            raw["findings"][0]["analysis"],
        )
        self.assertIn(
            "json_unescaped_inner_quote_escaped",
            parsed.normalizations,
        )

    def test_unescaped_empty_code_quotes_are_recovered_locally(self):
        raw = presentation()
        raw["findings"][0]["analysis"] = (
            'Saving the form persists a blank row with `unitName=""`.'
        )
        payload = json.dumps(raw).replace(r'\"\"', '""')

        parsed = parse_presentation_v1(payload)

        self.assertTrue(parsed.parsed)
        self.assertEqual(
            parsed.value["findings"][0]["analysis"],
            raw["findings"][0]["analysis"],
        )
        self.assertIn(
            "json_unescaped_empty_quote_pair_escaped",
            parsed.normalizations,
        )

    def test_one_mismatched_object_closer_is_recovered_locally(self):
        raw = presentation()
        raw["diagram"] = {
            "purpose": "pr_flow_map",
            "caption": "The changed request path.",
            "mermaid": "sequenceDiagram\nA->>B: request",
            "evidence_refs": ["path:src/app.py"],
        }
        payload = json.dumps(raw)
        root_close = payload.rfind("}")
        diagram_close = payload.rfind("}", 0, root_close)
        malformed = payload[:diagram_close] + "]" + payload[diagram_close + 1 :]

        parsed = parse_presentation_v1(malformed)

        self.assertTrue(parsed.parsed)
        self.assertEqual(parsed.value, raw)
        self.assertIn(
            "json_mismatched_closing_delimiter_replaced",
            parsed.normalizations,
        )


class PresentationCompilationTests(unittest.TestCase):
    def test_exact_ci_diagnostic_retains_non_source_blocker(self):
        blocker = finding(
            priority="P1",
            category="bug",
            required=["ci:unit:1"],
            supporting=[],
            placement="headline",
            headline="The PR title exceeds the repository policy limit",
        )
        blocker.update(
            {
                "file_path": "",
                "code_snippet": "",
                "analysis": (
                    "PR title must be 72 characters or less. The current "
                    "title therefore cannot pass the repository policy check."
                ),
                "owner_action": "Shorten the PR title to 72 characters or less.",
            }
        )
        raw = presentation(verdict="blocking", findings=[blocker])
        raw["decision"]["summary"] = (
            "Do not merge until the PR title satisfies the repository policy."
        )
        raw["decision"]["owner_actions"] = [
            "Shorten the PR title to 72 characters or less."
        ]

        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=actionable_non_source_ci_meta(),
        )

        self.assertTrue(result.publishable)
        retained = result.review["v3_review"]["findings"][0]
        self.assertTrue(retained["blocking"])
        self.assertEqual(retained["file_path"], "")
        self.assertEqual(retained["visibility"], "headline")
        self.assertEqual(retained["required_evidence_refs"], ["ci:unit:1"])
        self.assertIn("`review context`", result.review["pr_review_comment"])
        self.assertEqual(result.review["inline_comments"], [])

    def test_red_ci_without_matching_diagnostic_cannot_be_non_source_blocker(self):
        blocker = finding(
            priority="P1",
            category="bug",
            required=["ci:unit:1"],
            supporting=[],
            placement="headline",
            headline="The PR title may violate repository policy",
        )
        blocker.update(
            {
                "file_path": "",
                "code_snippet": "",
                "analysis": "The policy check is red for an unknown reason.",
                "owner_action": "Investigate the failed policy check.",
            }
        )

        result = compile_presentation_v1(
            presentation(verdict="blocking", findings=[blocker]),
            pr_details=PR_DETAILS,
            context_meta=actionable_non_source_ci_meta(),
        )

        self.assertFalse(result.publishable)
        self.assertEqual(result.failure_kind, "deciding_item_loss")

    def test_clear_review_projects_to_safe_public_v3(self):
        result = compile_presentation_v1(
            presentation(),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertFalse(result.safe_partial)
        self.assertEqual(result.review["review_generation_status"], "complete")
        self.assertIs(result.review["review_publishable"], True)
        self.assertIs(result.review["review_publication_safe"], True)
        self.assertIs(result.review["review_fallback_used"], False)
        self.assertEqual(
            result.review["pr_review_comment"].splitlines()[0],
            "### LlamaPReview — No blocking issues found",
        )
        self.assertNotIn("path:src/app.py", result.review["pr_review_comment"])
        compiled = result.review["v3_review"]["findings"][0]
        self.assertEqual(compiled["finding_type"], "note")
        self.assertEqual(
            compiled["required_evidence_refs"],
            ["path:src/app.py"],
        )
        self.assertEqual(
            compiled["supporting_evidence_refs"],
            ["ev:consumer"],
        )
        self.assertEqual(
            compiled["evidence_refs"],
            ["path:src/app.py", "ev:consumer"],
        )
        self.assertEqual(
            result.review["presentation_v1"],
            result.presentation,
        )
        self.assertEqual(
            result.review["v3_review"]["decision"]["confidence"],
            "high",
        )

    def test_decision_confidence_is_model_owned_and_not_recalibrated(self):
        blocker = finding(
            priority="P1",
            category="bug",
            required=["path:src/app.py"],
            supporting=[],
        )
        result = compile_presentation_v1(
            presentation(
                verdict="blocking",
                confidence="Low",
                findings=[blocker],
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertEqual(result.presentation["decision"]["confidence"], "Low")
        self.assertEqual(
            result.review["v3_review"]["decision"]["confidence"],
            "low",
        )

    def test_finding_level_supporting_ci_is_removed_locally(self):
        raw = presentation()
        raw["findings"][0]["supporting_evidence_refs"] = ["ci:unit:1"]
        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(
            result.review["v3_review"]["findings"][0][
                "supporting_evidence_refs"
            ],
            [],
        )

    def test_terminal_ci_churn_drops_only_optional_confidence_check(self):
        blocker = finding(
            priority="P1",
            category="bug",
            required=["path:src/app.py"],
            supporting=[],
        )
        initial = compile_presentation_v1(
            presentation(verdict="blocking", findings=[blocker]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )
        refreshed = compile_presentation_v1(
            initial.presentation,
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
            changed_ci_refs={"ci:unit:1"},
        )

        self.assertTrue(initial.publishable)
        self.assertEqual(len(initial.presentation["confidence_checks"]), 1)
        self.assertTrue(refreshed.publishable)
        self.assertTrue(refreshed.safe_partial)
        self.assertEqual(len(refreshed.presentation["findings"]), 1)
        self.assertEqual(refreshed.presentation["confidence_checks"], [])
        self.assertNotIn(
            "exact-head check completed successfully",
            refreshed.review["pr_review_comment"].casefold(),
        )

    def test_blocking_supporting_ci_payload_cannot_taint_core_review_prose(self):
        mutations = {
            "decision_summary": (
                "$.decision.summary",
                "Unit check failure confirms this change is ready.",
            ),
            "headline": (
                "$.findings[0].headline",
                "Unit check failure confirms the changed behavior",
            ),
            "analysis": (
                "$.findings[0].analysis",
                "Unit check failure confirms the changed behavior.",
            ),
            "owner_action": (
                "$.findings[0].owner_action",
                "Resolve the unit check failure before proceeding.",
            ),
        }
        for field, (location, value) in mutations.items():
            with self.subTest(field=field):
                raw = presentation(verdict="blocking")
                if field == "decision_summary":
                    raw["decision"]["summary"] = value
                else:
                    raw["findings"][0][field] = value
                result = compile_presentation_v1(
                    raw,
                    pr_details=PR_DETAILS,
                    context_meta=failing_ci_meta(),
                )

                if field in {"decision_summary", "analysis"}:
                    self.assertTrue(result.publishable)
                    self.assertTrue(result.safe_partial)
                    self.assertTrue(
                        any(
                            item.endswith("supporting_ci_surface_contracted")
                            for item in result.normalizations
                        )
                    )
                else:
                    self.assertEqual(result.status, "failure")
                    self.assertEqual(
                        result.failure_kind,
                        "supporting_ci_core_prose_tainted",
                    )
                    self.assertEqual(result.issues[-1].location, location)

    def test_ci_taint_uses_provenance_pairs_not_ordinary_words(self):
        raw = presentation()
        raw["decision"]["summary"] = (
            "No review blocker found. The unit check wrapper is unchanged. "
            "Failure isolation remains local to the existing boundary."
        )
        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=failing_ci_meta(),
        )

        self.assertTrue(result.publishable)

    def test_ci_taint_ignores_common_diagnostic_overlap_and_generic_name(self):
        diagnostic_meta = failing_ci_meta()
        diagnostic_meta["ci_generation_model_payload"]["checks"][0][
            "output"
        ] = {
            "summary": (
                "The changed boundary emitted the wrong tenant identifier."
            )
        }
        raw = presentation()
        raw["decision"]["summary"] = (
            "No review blocker found. The changed boundary preserves the "
            "documented behavior."
        )
        shared_phrase = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=diagnostic_meta,
        )

        generic_name_meta = failing_ci_meta()
        generic_name_meta["ci_generation_model_payload"]["checks"][0][
            "name"
        ] = "Test"
        raw["decision"]["summary"] = (
            "No review blocker found. Test failure handling remains unchanged."
        )
        generic_name = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=generic_name_meta,
        )

        self.assertTrue(shared_phrase.publishable)
        self.assertTrue(generic_name.publishable)

    def test_ci_taint_detects_exact_diagnostic_atom_and_name_state_pair(self):
        diagnostic_meta = failing_ci_meta()
        exact_diagnostic = (
            "The changed boundary emitted the wrong tenant identifier."
        )
        diagnostic_meta["ci_generation_model_payload"]["checks"][0][
            "output"
        ] = {"summary": exact_diagnostic}
        exact_raw = presentation(verdict="blocking")
        exact_raw["decision"]["summary"] = exact_diagnostic

        name_state_raw = presentation(verdict="blocking")
        name_state_raw["decision"]["summary"] = (
            "Unit check failure confirms this change is ready."
        )
        results = (
            compile_presentation_v1(
                exact_raw,
                pr_details=PR_DETAILS,
                context_meta=diagnostic_meta,
            ),
            compile_presentation_v1(
                name_state_raw,
                pr_details=PR_DETAILS,
                context_meta=failing_ci_meta(),
            ),
        )

        for result in results:
            self.assertTrue(result.publishable)
            self.assertTrue(result.safe_partial)
            self.assertTrue(
                any(
                    item.endswith("supporting_ci_surface_contracted")
                    for item in result.normalizations
                )
            )

    def test_clear_terminal_recompile_localizes_generation_ci_taint(self):
        raw = presentation()
        raw["decision"]["summary"] = (
            "Unit check failure confirms this change is ready."
        )
        raw["findings"][0]["analysis"] = (
            "Unit check failure confirms the changed value remains safe."
        )
        current = context_meta()
        current["ci_generation_model_payload"] = failing_ci_meta()[
            "ci_generation_model_payload"
        ]
        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=current,
            changed_ci_refs={"ci:unit:1"},
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(
            result.presentation["decision"]["summary"],
            "No review blocker found.",
        )
        self.assertEqual(result.presentation["findings"], [])
        self.assertTrue(
            any(
                item.endswith("supporting_ci_surface_contracted")
                for item in result.normalizations
            )
        )

    def test_changed_required_ci_cannot_leave_taint_on_surviving_decision(self):
        dependent = finding(
            priority="P1",
            category="bug",
            headline="The CI-dependent blocker must be resolved",
            required=["path:src/app.py", "ci:unit:1"],
            supporting=[],
        )
        dependent["analysis"] = (
            "Unit check failure confirms the CI-dependent blocker."
        )
        independent = finding(
            priority="P1",
            category="bug",
            headline="The independent code blocker must be resolved",
            required=["path:src/app.py"],
            supporting=[],
        )
        raw = presentation(
            verdict="blocking",
            findings=[dependent, independent],
        )
        raw["decision"]["summary"] = (
            "Old failure path confirms merge remains blocked."
        )
        initial_meta = failing_ci_meta()
        initial_meta["ci_generation_model_payload"]["checks"][0]["output"] = {
            "summary": "Old failure path"
        }
        initial = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=initial_meta,
        )
        current_meta = context_meta()
        current_meta["ci_generation_model_payload"] = initial_meta[
            "ci_generation_model_payload"
        ]
        refreshed = compile_presentation_v1(
            initial.presentation,
            pr_details=PR_DETAILS,
            context_meta=current_meta,
            changed_ci_refs={"ci:unit:1"},
        )

        self.assertTrue(initial.publishable)
        self.assertEqual(refreshed.status, "failure")
        self.assertEqual(
            refreshed.failure_kind,
            "changed_ci_core_prose_tainted",
        )
        self.assertIsNone(refreshed.review)

    def test_required_ci_may_remain_in_its_deciding_item_prose(self):
        blocker = finding(
            priority="P1",
            category="bug",
            required=["path:src/app.py", "ci:unit:1"],
            supporting=[],
        )
        blocker["analysis"] = (
            "Unit check failure confirms the changed contract is broken."
        )
        raw = presentation(verdict="blocking", findings=[blocker])
        raw["decision"]["summary"] = (
            "The code-required blocker must be resolved before merge."
        )
        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=failing_ci_meta(),
        )

        self.assertTrue(result.publishable)

    def test_required_ci_can_pair_with_supporting_exact_code_capability(self):
        blocker = finding(
            priority="P2",
            category="bug",
            required=["ci:unit:1"],
            supporting=["path:src/app.py"],
            placement="headline",
        )
        result = compile_presentation_v1(
            presentation(verdict="blocking", findings=[blocker]),
            pr_details=PR_DETAILS,
            context_meta=failing_ci_meta(),
        )

        self.assertTrue(result.publishable)
        retained = result.review["v3_review"]["findings"][0]
        self.assertEqual(retained["required_evidence_refs"], ["ci:unit:1"])
        self.assertEqual(
            retained["supporting_evidence_refs"],
            ["path:src/app.py"],
        )

    def test_changed_ci_diagnostic_helper_never_deletes_required_dependency(
        self,
    ):
        blocker = finding(
            priority="P1",
            category="bug",
            required=["path:src/app.py", "ci:unit:1"],
            supporting=[],
            placement="headline",
        )
        blocker["analysis"] = (
            "Unit check failure confirms the changed contract is broken."
        )
        raw = presentation(verdict="blocking", findings=[blocker])
        raw["decision"]["summary"] = (
            "The code-required blocker must be resolved before merge."
        )
        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=failing_ci_meta(),
        )

        self.assertTrue(result.publishable)
        expected = ["path:src/app.py", "ci:unit:1"]
        self.assertEqual(
            result.presentation["findings"][0]["required_evidence_refs"],
            expected,
        )
        self.assertEqual(
            result.review["v3_review"]["findings"][0][
                "required_evidence_refs"
            ],
            expected,
        )

    def test_primary_inline_election_guards_noneligible_reviews(self):
        base = {
            "finding_type": "bug",
            "priority": "P1",
            "confidence": "High",
            "evidence_status": "verified",
            "claim_scope": "changed_region",
            "blocking": True,
            "visibility": "collapsed",
            "headline": "The changed value violates the guard",
            "file_path": "src/app.py",
            "code_snippet": "value = build(2)",
            "comment": "The changed expression bypasses the required guard.",
            "required_evidence_refs": ["path:src/app.py"],
            "supporting_evidence_refs": [],
            "evidence_refs": ["path:src/app.py"],
        }
        cases = {
            "clear": ("clear", {}),
            "unverified": (
                "blocked_findings",
                {"evidence_status": "unverified"},
            ),
            "low_confidence": (
                "blocked_findings",
                {"confidence": "Low"},
            ),
            "deleted_region": (
                "blocked_findings",
                {"code_snippet": "value = build(1)"},
            ),
            "unanchored": (
                "blocked_findings",
                {"code_snippet": "value = build(3)"},
            ),
        }
        for label, (verdict, mutations) in cases.items():
            with self.subTest(label=label):
                candidate = {**base, **mutations}
                findings = [candidate]
                elected = elect_primary_inline(
                    findings,
                    verdict=verdict,
                    pr_details=PR_DETAILS,
                    context_meta=context_meta(),
                )

                self.assertFalse(elected)
                self.assertEqual(findings[0]["visibility"], "collapsed")

    def test_changed_required_p2_drops_item_and_dependent_clear_prose(self):
        item = finding(
            required=["ci:unit:1"],
            supporting=["path:src/app.py"],
        )
        result = compile_presentation_v1(
            presentation(findings=[item]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
            changed_ci_refs={"ci:unit:1"},
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(result.review["v3_review"]["findings"], [])
        self.assertEqual(result.review["v3_review"]["visible_verdict"], "clear")
        self.assertNotIn(
            "The changed expression remains consistent",
            result.review["pr_review_comment"],
        )
        self.assertEqual(
            result.presentation["decision"]["summary"],
            "No review blocker found.",
        )

    def test_supporting_only_p2_and_nonobjective_ci_degrade_locally(self):
        meta = context_meta()
        meta["evidence_catalog"].append(
            {
                "id": "ci:aggregate",
                "source_type": "ci",
                "outcome": "failure",
                "paths": [],
                "coverage_type": "aggregate",
            }
        )
        blocker = finding(
            priority="P1",
            category="security",
            required=["path:src/app.py"],
            supporting=[],
            placement="headline",
            headline="The changed binding exposes the protected value",
        )
        optional_gap = finding(
            priority="P2",
            category="test-gap",
            required=[],
            supporting=["path:src/app.py"],
            placement="collapsed",
            headline="The handler wiring lacks focused regression coverage",
        )
        raw = presentation(
            verdict="blocking",
            findings=[blocker, optional_gap],
        )
        raw["confidence_checks"] = [
            {
                "check": "Aggregate CI status",
                "result": "The aggregate check failed without a diagnostic.",
                "evidence_refs": ["ci:aggregate"],
            }
        ]

        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=meta,
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        findings = result.review["v3_review"]["findings"]
        self.assertEqual([item["priority"] for item in findings], ["P1", "P2"])
        self.assertEqual(findings[1]["evidence_status"], "unverified")
        self.assertNotIn(
            "ci:aggregate",
            result.review["v3_review"]["evidence_scope"],
        )

    def test_verified_test_gap_can_carry_blocking_but_question_and_note_cannot(self):
        blocking_gap = finding(
            priority="P2",
            category="test-gap",
            required=["path:src/app.py"],
            supporting=[],
            placement="headline",
            headline="The changed branch lacks its required regression proof",
        )
        blocking_gap["owner_action"] = (
            "Add and pass the focused regression test before merge."
        )
        question = finding(
            priority="P2",
            category="question",
            required=["path:src/app.py"],
            supporting=[],
            placement="collapsed",
            headline="Confirm the optional rollout preference",
        )
        note = finding(
            priority="P2",
            category="note",
            required=["path:src/app.py"],
            supporting=[],
            placement="collapsed",
            headline="Keep the local naming convention in mind",
        )

        result = compile_presentation_v1(
            presentation(
                verdict="blocking",
                findings=[blocking_gap, question, note],
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        findings = result.review["v3_review"]["findings"]
        self.assertTrue(findings[0]["blocking"])
        self.assertEqual(findings[0]["finding_type"], "test-gap")
        self.assertFalse(findings[1]["blocking"])
        self.assertEqual(findings[1]["finding_type"], "question")
        self.assertFalse(findings[2]["blocking"])
        self.assertEqual(findings[2]["finding_type"], "note")
        self.assertTrue(
            result.presentation["findings"][0]["owner_action"].startswith(
                "Before merge:"
            )
        )

    def test_post_merge_test_gap_action_cannot_carry_blocking(self):
        blocking_gap = finding(
            priority="P2",
            category="test-gap",
            required=["path:src/app.py"],
            supporting=[],
            placement="headline",
            headline="The changed branch lacks its required regression proof",
        )
        blocking_gap["owner_action"] = (
            "Add this regression test in a follow-up pull request after merge."
        )

        result = compile_presentation_v1(
            presentation(verdict="blocking", findings=[blocking_gap]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertFalse(result.publishable)
        self.assertEqual(result.failure_kind, "deciding_item_loss")

    def test_nonblocking_raw_diff_snippet_is_removed_locally(self):
        item = finding(placement="headline")
        item["code_snippet"] = (
            "-value = build(1)\n+value = build(2)"
        )

        result = compile_presentation_v1(
            presentation(findings=[item]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(
            result.review["v3_review"]["findings"][0]["code_snippet"],
            "",
        )
        self.assertTrue(
            any(
                "invalid_optional_anchor_removed" in item
                for item in result.normalizations
            )
        )

    def test_nonblocking_p2_with_weak_required_ref_becomes_unverified(self):
        meta = context_meta()
        meta["evidence_catalog"].append(
            {
                "id": "pr_body",
                "source_type": "pr_body",
                "coverage_type": "non_repository",
            }
        )
        item = finding(
            category="note",
            required=["pr_body"],
            supporting=[],
            placement="collapsed",
        )
        item["code_snippet"] = ""

        result = compile_presentation_v1(
            presentation(findings=[item]),
            pr_details=PR_DETAILS,
            context_meta=meta,
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        projected = result.review["v3_review"]["findings"][0]
        self.assertEqual(projected["evidence_status"], "unverified")
        self.assertEqual(projected["visibility"], "collapsed")
        self.assertEqual(projected["required_evidence_refs"], [])
        self.assertEqual(projected["supporting_evidence_refs"], ["pr_body"])

    def test_nonblocking_p2_without_any_usable_evidence_is_removed(self):
        item = finding(
            category="note",
            required=["ci:unit:1"],
            supporting=[],
            placement="collapsed",
        )
        item["file_path"] = ""
        item["code_snippet"] = ""

        result = compile_presentation_v1(
            presentation(findings=[item]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(result.review["v3_review"]["findings"], [])
        self.assertTrue(
            any(
                "unsupported_nonblocking_finding_removed" in item
                for item in result.normalizations
            )
        )

    def test_nonblocking_p2_without_any_evidence_reference_is_removed(self):
        blocker = finding(
            category="bug",
            required=["path:src/app.py"],
            supporting=[],
            placement="headline",
        )
        unanchored_note = finding(
            category="test-gap",
            required=[],
            supporting=[],
            placement="collapsed",
        )

        result = compile_presentation_v1(
            presentation(
                verdict="blocking",
                findings=[blocker, unanchored_note],
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(len(result.review["v3_review"]["findings"]), 1)
        self.assertTrue(
            any(
                "unsupported_nonblocking_finding_removed" in item
                for item in result.normalizations
            )
        )

    def test_last_unsupported_blocker_fails_and_never_becomes_clear(self):
        blocker = finding(
            priority="P1",
            category="bug",
            required=["ci:unit:1"],
            supporting=[],
            headline="The changed binding bypasses the required guard",
        )
        result = compile_presentation_v1(
            presentation(verdict="blocking", findings=[blocker]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
            changed_ci_refs={"ci:unit:1"},
        )

        self.assertEqual(result.status, "failure")
        self.assertIsNone(result.review)
        self.assertEqual(result.failure_kind, "deciding_item_loss")

    def test_first_blocking_p2_restores_same_path_exact_anchor_role(self):
        meta = context_meta()
        meta["evidence_catalog"].append(
            {
                "id": "ev:full-app",
                "source_type": "pfr",
                "outcome": "hit",
                "paths": ["src/app.py"],
                "coverage_type": "full_file",
                "source_ref": f"pr_head:{HEAD}",
                "head_reread_outcome": "hit",
            }
        )
        blocker = finding(
            priority="P2",
            category="bug",
            required=[],
            supporting=["ev:full-app"],
            headline="The changed value breaks the runtime contract",
        )

        result = compile_presentation_v1(
            presentation(verdict="blocking", findings=[blocker]),
            pr_details=PR_DETAILS,
            context_meta=meta,
        )

        self.assertTrue(result.publishable)
        retained = result.review["v3_review"]["findings"][0]
        self.assertTrue(retained["blocking"])
        self.assertEqual(retained["required_evidence_refs"], ["ev:full-app"])
        self.assertEqual(retained["supporting_evidence_refs"], [])
        self.assertTrue(
            any(
                "exact_anchor_role_restored" in item
                for item in result.normalizations
            )
        )

    def test_later_blocking_review_p2_restores_evidence_not_merge_gate(self):
        meta = context_meta()
        meta["evidence_catalog"].append(
            {
                "id": "ev:full-app",
                "source_type": "pfr",
                "outcome": "hit",
                "paths": ["src/app.py"],
                "coverage_type": "full_file",
                "source_ref": f"pr_head:{HEAD}",
                "head_reread_outcome": "hit",
            }
        )
        first = finding(
            priority="P2",
            category="bug",
            required=["path:src/app.py"],
            supporting=[],
            headline="The first changed behavior requires repair",
        )
        later = finding(
            priority="P2",
            category="bug",
            required=[],
            supporting=["ev:full-app"],
            headline="The second changed behavior also requires attention",
        )

        result = compile_presentation_v1(
            presentation(verdict="blocking", findings=[first, later]),
            pr_details=PR_DETAILS,
            context_meta=meta,
        )

        self.assertTrue(result.publishable)
        retained = result.review["v3_review"]["findings"]
        self.assertTrue(retained[0]["blocking"])
        self.assertFalse(retained[1]["blocking"])
        self.assertEqual(retained[1]["evidence_status"], "verified")
        self.assertEqual(
            retained[1]["required_evidence_refs"],
            ["ev:full-app"],
        )

    def test_blocking_p2_role_is_not_restored_without_exact_anchor(self):
        controls = []
        no_anchor = finding(
            priority="P2",
            category="bug",
            required=[],
            supporting=["ev:consumer"],
        )
        no_anchor["code_snippet"] = ""
        controls.append((no_anchor, context_meta()))

        wrong_path_meta = context_meta()
        wrong_path_meta["evidence_catalog"].append(
            {
                "id": "ev:other-file",
                "source_type": "pfr",
                "outcome": "hit",
                "paths": ["src/other.py"],
                "coverage_type": "full_file",
                "source_ref": f"pr_head:{HEAD}",
                "head_reread_outcome": "hit",
            }
        )
        wrong_path = finding(
            priority="P2",
            category="bug",
            required=[],
            supporting=["ev:other-file"],
        )
        controls.append((wrong_path, wrong_path_meta))

        ci_only = finding(
            priority="P2",
            category="bug",
            required=[],
            supporting=["ci:unit:1"],
        )
        controls.append((ci_only, context_meta()))

        for blocker, meta in controls:
            with self.subTest(supporting=blocker["supporting_evidence_refs"]):
                result = compile_presentation_v1(
                    presentation(verdict="blocking", findings=[blocker]),
                    pr_details=PR_DETAILS,
                    context_meta=meta,
                )
                self.assertEqual(result.status, "failure")
                self.assertEqual(result.failure_kind, "deciding_item_loss")

    def test_one_surviving_blocker_keeps_safe_partial_blocking_decision(self):
        lost = finding(
            priority="P1",
            category="bug",
            required=["ci:unit:1"],
            supporting=[],
            headline="The first changed path violates its guard",
        )
        retained = finding(
            priority="P1",
            category="bug",
            required=["path:src/app.py"],
            supporting=[],
            headline="The retained changed path violates its guard",
        )
        result = compile_presentation_v1(
            presentation(verdict="blocking", findings=[lost, retained]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
            changed_ci_refs={"ci:unit:1"},
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(
            result.review["v3_review"]["visible_verdict"],
            "blocked_findings",
        )
        self.assertEqual(len(result.review["v3_review"]["findings"]), 1)

    def test_ci_only_p1_is_removed_when_exact_code_p1_retains_the_decision(self):
        retained = finding(
            priority="P1",
            category="security",
            required=["path:src/app.py"],
            supporting=[],
            headline="The changed command executes untrusted input",
        )
        unsupported = finding(
            priority="P1",
            category="bug",
            required=["ci:unit:1"],
            supporting=["path:src/app.py"],
            placement="headline",
            headline="The failed job proves an unknown integration defect",
        )
        unsupported["code_snippet"] = ""
        raw = presentation(
            verdict="blocking",
            findings=[retained, unsupported],
        )
        raw["decision"]["summary"] = (
            "Do not merge: the changed command is unsafe, and the failed job "
            "proves an unknown integration defect."
        )
        raw["decision"]["owner_actions"] = [
            "Fix the unknown job failure before merge."
        ]

        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        review = result.review["v3_review"]
        self.assertEqual(len(review["findings"]), 1)
        self.assertTrue(review["findings"][0]["blocking"])
        self.assertEqual(
            result.presentation["decision"]["summary"],
            "Changes are needed before merge: "
            "The changed command executes untrusted input",
        )
        self.assertEqual(
            result.presentation["decision"]["owner_actions"],
            [retained["owner_action"]],
        )
        self.assertNotIn("unknown integration defect", result.review["pr_review_comment"])
        self.assertTrue(
            any(
                "unsupported_blocker_contracted" in item
                for item in result.normalizations
            )
        )

    def test_sole_ci_only_p1_still_fails_closed(self):
        unsupported = finding(
            priority="P1",
            category="bug",
            required=["ci:unit:1"],
            supporting=["path:src/app.py"],
            placement="headline",
            headline="The failed job proves an unknown integration defect",
        )
        unsupported["code_snippet"] = ""

        result = compile_presentation_v1(
            presentation(verdict="blocking", findings=[unsupported]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertEqual(result.status, "failure")
        self.assertEqual(result.failure_kind, "deciding_item_loss")
        self.assertIsNone(result.review)

    def test_out_of_catalog_deciding_ref_fails_without_repair(self):
        forged = finding(
            priority="P2",
            category="bug",
            required=["ev_not_supplied"],
            supporting=[],
        )
        result = compile_presentation_v1(
            presentation(verdict="blocking", findings=[forged]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertEqual(result.status, "failure")
        self.assertIsNone(result.review)
        self.assertEqual(
            result.failure_kind,
            "out_of_catalog_material_evidence",
        )

    def test_out_of_catalog_nondeciding_p2_is_removed_locally(self):
        unsupported = finding(
            required=["ev_not_supplied"],
            supporting=[],
        )
        raw = presentation(findings=[unsupported])
        raw["decision"]["summary"] = (
            "No review blocker found. The unsupported optional premise also "
            "proves the changed behavior is safe."
        )
        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(result.presentation["findings"], [])
        self.assertEqual(
            result.presentation["decision"]["summary"],
            "No review blocker found.",
        )
        self.assertNotIn(
            "unsupported optional premise",
            result.review["pr_review_comment"],
        )
        self.assertTrue(
            any(
                "optional_dependency_contracted" in item
                for item in result.normalizations
            )
        )
        self.assertEqual(result.review["v3_review"]["visible_verdict"], "clear")

    def test_no_hit_path_on_nondeciding_p2_is_removed_locally(self):
        meta = context_meta()
        meta["evidence_catalog"].append(
            {
                "id": "ev_missing_path",
                "source_type": "pfr",
                "outcome": "no_hit",
                "paths": ["src/not-observed.py"],
                "coverage_type": "file_slice",
            }
        )
        unsupported = finding(
            required=[],
            supporting=["path:src/app.py"],
        )
        unsupported["file_path"] = "src/not-observed.py"
        raw = presentation(findings=[unsupported])
        raw["decision"]["summary"] = (
            "No review blocker found. The unobserved path is safe."
        )

        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=meta,
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(result.presentation["findings"], [])
        self.assertEqual(
            result.presentation["decision"]["summary"],
            "No review blocker found.",
        )
        self.assertTrue(
            any(
                "clear_decision_dependency_removed" in item
                for item in result.normalizations
            )
        )

    def test_contracted_clear_summary_uses_only_admitted_non_ci_check(self):
        unsupported = finding(
            required=["ev_not_supplied"],
            supporting=[],
        )
        raw = presentation(findings=[unsupported])
        raw["decision"]["summary"] = (
            "No review blocker found. The unsupported optional premise "
            "proves the change is safe."
        )
        raw["confidence_checks"].insert(
            0,
            {
                "check": "Exact changed branch",
                "result": "The retained fallback remains reachable.",
                "evidence_refs": ["path:src/app.py"],
            },
        )

        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        first_screen = result.review["pr_review_comment"].split("<details>", 1)[0]
        self.assertIn(
            "Exact changed branch: The retained fallback remains reachable.",
            first_screen,
        )
        self.assertNotIn(
            "unsupported optional premise",
            result.review["pr_review_comment"],
        )
        self.assertTrue(
            any(
                "supported_check_first_screen_retained" in item
                for item in result.normalizations
            )
        )

    def test_contracted_clear_summary_does_not_promote_search_absence(self):
        meta = context_meta()
        meta["evidence_catalog"].append(
            {
                "id": "ev:search-no-hit",
                "source_type": "pfr",
                "outcome": "hit",
                "paths": ["src/app.py"],
                "coverage_type": "search_snippet",
                "tool": "search_code",
                "source_ref": "default_branch_search",
                "head_reread_outcome": "relocated_at_head",
                "search_hit_lineage": [
                    {
                        "outcome": "relocated_at_head",
                        "head_sha": HEAD,
                    }
                ],
            }
        )
        unsupported = finding(
            required=["ev_not_supplied"],
            supporting=[],
        )
        raw = presentation(findings=[unsupported])
        raw["decision"]["summary"] = (
            "No review blocker found. The unsupported optional premise "
            "proves the change is safe."
        )
        raw["confidence_checks"] = [
            {
                "check": "Repository-wide caller search",
                "result": "No remaining callers exist.",
                "evidence_refs": ["ev:search-no-hit"],
            }
        ]

        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=meta,
        )

        self.assertTrue(result.publishable)
        first_screen = result.review["pr_review_comment"].split("<details>", 1)[0]
        self.assertNotIn("No remaining callers", first_screen)
        self.assertNotIn(
            "unsupported optional premise",
            result.review["pr_review_comment"],
        )
        self.assertFalse(
            any(
                "supported_check_first_screen_retained" in item
                for item in result.normalizations
            )
        )

    def test_explicit_no_action_clear_findings_are_removed_locally(self):
        for owner_action in (
            "None.",
            "No action required; merge as-is.",
            "None required beyond the standard merge confirmation.",
            "No pre-merge action is required.",
        ):
            item = finding()
            item["owner_action"] = owner_action
            result = compile_presentation_v1(
                presentation(findings=[item]),
                pr_details=PR_DETAILS,
                context_meta=context_meta(),
            )

            with self.subTest(owner_action=owner_action):
                self.assertTrue(result.publishable)
                self.assertTrue(result.safe_partial)
                self.assertEqual(result.presentation["findings"], [])
                self.assertEqual(
                    result.review["v3_review"]["visible_verdict"], "clear"
                )

    def test_concrete_clear_owner_action_is_retained(self):
        result = compile_presentation_v1(
            presentation(findings=[finding()]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertEqual(len(result.presentation["findings"]), 1)

    def test_unique_one_edit_opaque_evidence_id_is_restored(self):
        meta = context_meta()
        meta["evidence_catalog"].append(
            {
                "id": "ev_646df699e023",
                "source_type": "file",
                "outcome": "hit",
                "paths": ["src/app.py"],
                "coverage_type": "changed_region",
            }
        )
        typo = finding(
            required=["ev_646dfe699023"],
            supporting=[],
        )

        result = compile_presentation_v1(
            presentation(findings=[typo]),
            pr_details=PR_DETAILS,
            context_meta=meta,
        )

        self.assertTrue(result.publishable)
        self.assertEqual(
            result.presentation["findings"][0]["required_evidence_refs"],
            ["ev_646df699e023"],
        )
        self.assertTrue(
            any(
                "unique_catalog_ref_restored" in item
                for item in result.normalizations
            )
        )

    def test_ambiguous_one_edit_evidence_id_still_fails_closed(self):
        meta = context_meta()
        for identity in ("ev_alpha1", "ev_alpha2"):
            meta["evidence_catalog"].append(
                {
                    "id": identity,
                    "source_type": "file",
                    "outcome": "hit",
                    "paths": ["src/app.py"],
                    "coverage_type": "changed_region",
                }
            )

        result = compile_presentation_v1(
            presentation(
                verdict="blocking",
                findings=[
                    finding(
                        priority="P1",
                        category="bug",
                        required=["ev_alpha"],
                        supporting=[],
                    )
                ]
            ),
            pr_details=PR_DETAILS,
            context_meta=meta,
        )

        self.assertFalse(result.publishable)
        self.assertEqual(
            result.failure_kind,
            "out_of_catalog_material_evidence",
        )

    def test_out_of_catalog_supporting_ref_is_removed_locally(self):
        optional = finding(
            required=["path:src/app.py"],
            supporting=["ev_not_supplied"],
        )
        result = compile_presentation_v1(
            presentation(findings=[optional]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(
            result.review["v3_review"]["findings"][0][
                "supporting_evidence_refs"
            ],
            [],
        )
        self.assertTrue(
            any("optional_ref_removed" in item for item in result.normalizations)
        )

    def test_out_of_catalog_confidence_check_is_dropped_locally(self):
        raw = presentation()
        raw["confidence_checks"][0]["evidence_refs"] = ["ev_not_supplied"]
        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(result.presentation["confidence_checks"], [])

    def test_nonarray_confidence_checks_degrade_locally(self):
        raw = presentation()
        raw["confidence_checks"] = {"check": "not an array"}
        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(result.presentation["confidence_checks"], [])
        self.assertTrue(
            any(
                item.code == "confidence_checks_shape_invalid"
                for item in result.issues
            )
        )

    def test_malformed_suggestions_degrade_only_the_optional_surface(self):
        wrong_shape = finding()
        wrong_shape["suggestion"] = "replace it"
        wrong_value = finding(placement="collapsed")
        wrong_value["suggestion"] = {
            "type": "UNKNOWN",
            "content": "",
            "extra": True,
        }
        result = compile_presentation_v1(
            presentation(findings=[wrong_shape, wrong_value]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(
            [item.get("suggested_code") for item in result.review["v3_review"]["findings"]],
            [None],
        )
        self.assertEqual(
            {
                item.code
                for item in result.issues
                if item.code.startswith("suggestion_")
            },
            {"suggestion_shape_invalid", "suggestion_value_invalid"},
        )

    def test_unsupported_placement_collapses_without_losing_finding(self):
        item = finding()
        item["placement"] = "sidebar"
        result = compile_presentation_v1(
            presentation(findings=[item]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(
            result.review["v3_review"]["findings"][0]["visibility"],
            "collapsed",
        )

    def test_exact_structural_duplicates_are_removed_without_semantic_merge(self):
        first = finding(placement="inline")
        duplicate = copy.deepcopy(first)
        duplicate["placement"] = "headline"
        raw = presentation(findings=[first, duplicate])
        raw["confidence_checks"].append(copy.deepcopy(raw["confidence_checks"][0]))
        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(len(result.review["v3_review"]["findings"]), 1)
        self.assertEqual(len(result.presentation["confidence_checks"]), 1)
        self.assertNotIn(
            "2 non-blocking findings retained",
            result.review["pr_review_comment"],
        )
        self.assertTrue(
            any("exact_duplicate_removed" in item for item in result.normalizations)
        )

    def test_exact_duplicate_material_unknown_keeps_one_decider(self):
        unknown = material_unknown()
        result = compile_presentation_v1(
            presentation(
                verdict="verification_needed",
                findings=[],
                unknowns=[unknown, copy.deepcopy(unknown)],
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertEqual(len(result.review["v3_review"]["material_unknowns"]), 1)
        self.assertEqual(result.review["v3_review"]["visible_verdict"], "unverified")

    def test_small_item_overflow_is_validated_without_silent_truncation(self):
        findings = []
        unknowns = []
        for index in range(9):
            item = finding(placement="collapsed")
            item["headline"] = f"Distinct supported observation {index}"
            findings.append(item)
            unknown = material_unknown()
            unknown["missing_fact"] = f"Distinct merge fact {index}"
            unknowns.append(unknown)
        extra_findings = compile_presentation_v1(
            presentation(findings=findings),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )
        extra_unknowns = compile_presentation_v1(
            presentation(
                verdict="verification_needed",
                findings=[],
                unknowns=unknowns,
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(extra_findings.publishable)
        self.assertTrue(extra_unknowns.publishable)
        self.assertEqual(len(extra_findings.presentation["findings"]), 9)
        self.assertEqual(len(extra_unknowns.presentation["material_unknowns"]), 9)

    def test_transport_cap_still_fails_closed_without_truncating(self):
        too_many_findings = compile_presentation_v1(
            presentation(findings=[finding(placement="collapsed") for _ in range(13)]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )
        too_many_unknowns = compile_presentation_v1(
            presentation(
                verdict="verification_needed",
                findings=[],
                unknowns=[material_unknown() for _ in range(13)],
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        for result in (too_many_findings, too_many_unknowns):
            self.assertEqual(result.status, "failure")
            self.assertEqual(
                result.failure_kind,
                "presentation_item_cap_exceeded",
            )
            self.assertIsNone(result.review)

    def test_repository_wide_p2_can_have_no_path_or_inline_anchor(self):
        broad = finding(placement="headline")
        broad["file_path"] = ""
        broad["code_snippet"] = ""
        result = compile_presentation_v1(
            presentation(findings=[broad]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        retained = result.review["v3_review"]["findings"][0]
        self.assertEqual(retained["file_path"], "")
        self.assertEqual(retained["visibility"], "headline")

    def test_verification_decision_requires_retained_material_unknown(self):
        missing = compile_presentation_v1(
            presentation(
                verdict="verification_needed",
                findings=[],
                unknowns=[],
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )
        retained = compile_presentation_v1(
            presentation(
                verdict="verification_needed",
                findings=[],
                unknowns=[material_unknown()],
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertEqual(missing.status, "failure")
        self.assertEqual(missing.failure_kind, "deciding_item_loss")
        self.assertTrue(retained.publishable)
        self.assertEqual(
            retained.review["v3_review"]["visible_verdict"],
            "unverified",
        )
        self.assertIn(
            "active runtime binding",
            retained.review["pr_review_comment"],
        )

    def test_nonblocking_decisions_cannot_retain_p0_or_p1_findings(self):
        blocker = finding(
            priority="P1",
            category="bug",
            required=["path:src/app.py"],
            supporting=[],
        )
        clear = compile_presentation_v1(
            presentation(verdict="clear", findings=[blocker]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )
        unverified = compile_presentation_v1(
            presentation(
                verdict="verification_needed",
                findings=[blocker],
                unknowns=[material_unknown()],
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        for result in (clear, unverified):
            self.assertEqual(result.status, "failure")
            self.assertEqual(
                result.failure_kind,
                "decision_finding_contradiction",
            )
            self.assertIsNone(result.review)

    def test_blocking_decision_can_be_carried_by_verified_p2(self):
        for category in (
            "security",
            "performance",
            "architecture",
            "maintainability",
        ):
            with self.subTest(category=category):
                blocker = finding(
                    priority="P2",
                    category=category,
                    required=["path:src/app.py"],
                    supporting=[],
                    headline="The changed lookup violates its runtime contract",
                )
                result = compile_presentation_v1(
                    presentation(verdict="blocking", findings=[blocker]),
                    pr_details=PR_DETAILS,
                    context_meta=context_meta(),
                )

                self.assertTrue(result.publishable)
                review = result.review["v3_review"]
                self.assertEqual(
                    review["visible_verdict"],
                    "blocked_findings",
                )
                self.assertEqual(review["findings"][0]["priority"], "P2")
                self.assertTrue(review["findings"][0]["blocking"])
                self.assertEqual(
                    review["decision"]["reasons"][0]["refs"],
                    ["F1"],
                )

        primary = finding(
            priority="P2",
            category="performance",
            required=["path:src/app.py"],
            supporting=[],
            headline="The changed lookup stalls the primary request path",
        )
        incidental = finding(
            priority="P2",
            category="bug",
            required=["path:src/app.py"],
            supporting=[],
            headline="The changed fallback returns the wrong local value",
        )
        mixed = compile_presentation_v1(
            presentation(
                verdict="blocking",
                findings=[primary, incidental],
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(mixed.publishable)
        review = mixed.review["v3_review"]
        self.assertEqual(
            [item["blocking"] for item in review["findings"]],
            [True, False],
        )
        self.assertEqual(
            [reason["refs"] for reason in review["decision"]["reasons"]],
            [["F1"]],
        )
        self.assertEqual(review["owner_action"][0]["resolves"], ["F1"])

    def test_clear_keeps_nondeciding_unknown_without_rejudging_final(self):
        raw = presentation(unknowns=[material_unknown()])
        raw["decision"]["summary"] = "The dependency refresh is safe to merge."
        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        review = result.review["v3_review"]
        self.assertEqual(review["visible_verdict"], "clear")
        self.assertEqual(len(review["material_unknowns"]), 1)
        self.assertFalse(review["material_unknowns"][0]["affects_merge"])
        self.assertEqual(review["owner_action"], [])
        self.assertTrue(
            review["decision"]["public_sentence"].startswith(
                "No review blocker found. The dependency refresh"
            )
        )

    def test_bad_optional_diagram_and_nonlocal_inline_degrade_locally(self):
        item = finding(placement="inline")
        item["code_snippet"] = ""
        result = compile_presentation_v1(
            presentation(
                findings=[item],
                diagram={
                    "purpose": "risk_path",
                    "caption": "Risk path",
                    "mermaid": "not a diagram",
                    "evidence_refs": ["not_in_catalog"],
                },
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(
            result.review["v3_review"]["findings"][0]["visibility"],
            "collapsed",
        )
        self.assertIsNone(result.review["v3_review"]["diagram"])
        self.assertEqual(result.review["inline_comments"], [])

    def test_direct_replacement_requires_model_declaration_and_local_fit(self):
        committable = finding(
            suggestion={
                "type": "DIRECT_REPLACEMENT",
                "content": "value = build(3)",
            }
        )
        conceptual = finding(
            suggestion={
                "type": "DIRECT_REPLACEMENT",
                "content": "replace the surrounding subsystem",
            }
        )
        result = compile_presentation_v1(
            presentation(findings=[committable, conceptual]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        first, second = result.review["v3_review"]["findings"]
        self.assertEqual(first["suggestion_type"], "DIRECT_REPLACEMENT")
        self.assertEqual(second["suggestion_type"], "CONCEPTUAL_ADVICE")
        self.assertTrue(
            any(
                "direct_replacement_downgraded" in item
                for item in result.normalizations
            )
        )

    def test_valid_diagram_is_purpose_gated_and_rendered_once(self):
        result = compile_presentation_v1(
            presentation(
                diagram={
                    "purpose": "pr_flow_map",
                    "caption": "Why the guard matters",
                    "mermaid": (
                        "sequenceDiagram\n"
                        "    participant Caller\n"
                        "    participant Guard\n"
                        "    Caller->>Guard: validate"
                    ),
                    "evidence_refs": ["path:src/app.py"],
                }
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertEqual(
            result.review["pr_review_comment"].count("```mermaid"),
            1,
        )

    def test_parser_invalid_diagram_is_dropped_without_losing_review_prose(self):
        invalid_diagrams = (
            (
                "sequenceDiagram\n"
                "participant U\n"
                "participant A\n"
                "participant B\n"
                "Note over U,A,B: changed path\n"
                "U->>A: call"
            ),
            (
                "sequenceDiagram\n"
                "participant Poll\n"
                "participant Actor\n"
                "Poll->>Actor: wait"
            ),
            (
                "sequenceDiagram\n"
                "participant Poll\n"
                "Note right of Actor: wait\n"
                "Poll->>Poll: continue"
            ),
            (
                "sequenceDiagram\n"
                "participant Poll\n"
                "Note left of Actor: wait\n"
                "Poll->>Poll: continue"
            ),
            (
                "sequenceDiagram\n"
                "participant A\n"
                "Note left A: missing of\n"
                "A->>A: continue"
            ),
            (
                "sequenceDiagram\n"
                "participant A\n"
                "Note right A: missing of\n"
                "A->>A: continue"
            ),
            (
                "sequenceDiagram\n"
                "participant A\n"
                "Note of A: standalone of\n"
                "A->>A: continue"
            ),
        )
        for mermaid in invalid_diagrams:
            with self.subTest(mermaid=mermaid):
                result = compile_presentation_v1(
                    presentation(
                        diagram={
                            "purpose": "pr_flow_map",
                            "caption": "This optional diagram cannot render.",
                            "mermaid": mermaid,
                            "evidence_refs": ["path:src/app.py"],
                        }
                    ),
                    pr_details=PR_DETAILS,
                    context_meta=context_meta(),
                )

                self.assertTrue(result.publishable)
                self.assertTrue(result.safe_partial)
                self.assertIsNone(result.presentation["diagram"])
                body = result.review["pr_review_comment"]
                self.assertNotIn("```mermaid", body)
                self.assertIn(
                    "The changed value remains locally consistent",
                    body,
                )
                self.assertIn(
                    "The changed expression remains consistent with the visible contract.",
                    body,
                )

    def test_one_bad_diagram_ref_is_removed_without_losing_the_diagram(self):
        result = compile_presentation_v1(
            presentation(
                diagram={
                    "purpose": "pr_flow_map",
                    "caption": "Why the guard matters",
                    "mermaid": (
                        "sequenceDiagram\n"
                        "    participant Caller\n"
                        "    participant Guard\n"
                        "    Caller->>Guard: validate"
                    ),
                    "evidence_refs": [
                        "path:src/app.py",
                        "path:not-in-the-catalog.py",
                    ],
                }
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        self.assertEqual(
            result.review["v3_review"]["diagram"]["evidence_refs"],
            ["path:src/app.py"],
        )
        self.assertEqual(
            result.review["pr_review_comment"].count("```mermaid"),
            1,
        )

    def test_blocking_risk_diagram_keeps_emphasis_before_details(self):
        blocker = finding(
            priority="P1",
            category="bug",
            required=["path:src/app.py"],
            supporting=[],
        )
        mermaid = (
            "sequenceDiagram\n"
            "    participant Caller\n"
            "    participant Router\n"
            "    participant Worker\n"
            "    participant Store\n"
            "    Caller->>Router: submit request\n"
            "    note over Router,Worker: PR change — route before validation\n"
            "    alt input is valid\n"
            "        Router->>Worker: dispatch work\n"
            "        Worker->>Store: persist result\n"
            "    else input is invalid\n"
            "        critical unsafe changed path\n"
            "            Router->>Worker: dispatch unvalidated work\n"
            "            Worker->>Store: persist invalid result\n"
            "            note over Worker,Store: Impact — invalid state becomes durable\n"
            "        end\n"
            "    end"
        )
        result = compile_presentation_v1(
            presentation(
                verdict="blocking",
                findings=[blocker],
                diagram={
                    "purpose": "risk_path",
                    "caption": "The changed ordering makes invalid state durable.",
                    "mermaid": mermaid,
                    "evidence_refs": ["path:src/app.py"],
                },
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        body = result.review["pr_review_comment"]
        self.assertEqual(body.count("```mermaid"), 1)
        self.assertEqual(body.count("critical unsafe changed path"), 1)
        self.assertEqual(body.count("PR change — route before validation"), 1)
        self.assertEqual(body.count("Impact — invalid state becomes durable"), 1)
        self.assertLess(body.index("#### Risk path"), body.index("<details>"))
        self.assertNotIn("sequenceDiagram", body[body.index("<details>") :])

    def test_risk_path_requires_blocking_verdict_and_retained_blocker(self):
        diagram = {
            "purpose": "risk_path",
            "caption": "Why the guard matters",
            "mermaid": (
                "sequenceDiagram\n"
                "    participant Caller\n"
                "    participant Guard\n"
                "    Caller->>Guard: validate"
            ),
            "evidence_refs": ["path:src/app.py"],
        }
        clear = compile_presentation_v1(
            presentation(diagram=diagram),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )
        unverified = compile_presentation_v1(
            presentation(
                verdict="verification_needed",
                findings=[],
                unknowns=[material_unknown()],
                diagram=diagram,
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )
        blocker = finding(
            priority="P1",
            category="bug",
            required=["path:src/app.py"],
            supporting=[],
        )
        blocking = compile_presentation_v1(
            presentation(
                verdict="blocking",
                findings=[blocker],
                diagram=diagram,
            ),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        for result in (clear, unverified):
            self.assertTrue(result.publishable)
            self.assertTrue(result.safe_partial)
            self.assertIsNone(result.presentation["diagram"])
            self.assertNotIn("```mermaid", result.review["pr_review_comment"])
        self.assertTrue(blocking.publishable)
        self.assertEqual(
            blocking.review["pr_review_comment"].count("```mermaid"),
            1,
        )

    def test_count_details_inline_footer_and_private_identity_sanitation(self):
        first = finding(
            headline="The first retained note stays actionable",
        )
        first["analysis"] = (
            "The path:src/app.py evidence supports the bounded local note."
        )
        second = finding(
            headline="The second retained note stays actionable",
            placement="collapsed",
        )
        result = compile_presentation_v1(
            presentation(findings=[first, second]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        body = result.review["pr_review_comment"]
        self.assertIn("2 non-blocking findings retained", body)
        self.assertIn("Review details and evidence", body)
        self.assertNotIn("path:src/app.py", body)
        self.assertEqual(len(result.review["inline_comments"]), 1)
        published = build_main_comment(result.review)
        self.assertEqual(published.count(PUBLIC_FOOTER_MARKER), 1)

class PresentationRepresentationTests(unittest.TestCase):
    def test_parse_failure_is_typed_and_has_no_public_fallback(self):
        result = compile_presentation_v1(
            "{not-json",
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertEqual(result.status, "failure")
        self.assertEqual(result.failure_kind, "json_parse_error")
        self.assertIsNone(result.review)
        self.assertIsNone(result.presentation)

    def test_literal_control_character_is_escaped_locally(self):
        raw = json.dumps(presentation())
        raw = raw.replace("No review blocker found.", "No review\tblocker found.")

        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertIn(
            "json_literal_control_character_escaped",
            result.normalizations,
        )

    def test_invalid_json_escape_preserves_one_literal_backslash(self):
        expected_analysis = (
            r"The changed path src\widget.py remains exact after decoding."
        )
        payload = presentation()
        payload["findings"][0]["analysis"] = expected_analysis
        raw = json.dumps(payload)
        raw = raw.replace(r"src\\widget.py", r"src\widget.py")

        result = compile_presentation_v1(
            raw,
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertIn("json_invalid_escape_escaped", result.normalizations)
        self.assertEqual(
            result.presentation["findings"][0]["analysis"],
            expected_analysis,
        )

    def test_incomplete_provider_envelope_fails_closed(self):
        compiled = compile_presentation_v1(
            presentation(),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )
        forced = mark_final_response_incomplete(
            compiled,
            failure_kind="output_truncated",
        )

        self.assertTrue(compiled.publishable)
        self.assertEqual(forced.status, "failure")
        self.assertEqual(forced.failure_kind, "output_truncated")
        self.assertIsNone(forced.review)

    def test_blocking_finding_with_invalid_anchor_retains_decision(self):
        blocker = finding(
            priority="P1",
            category="bug",
            required=["path:src/app.py"],
            supporting=[],
        )
        blocker["code_snippet"] = "missing = True"
        blocker["placement"] = "inline"

        result = compile_presentation_v1(
            presentation(verdict="blocking", findings=[blocker]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertTrue(result.safe_partial)
        retained = result.review["v3_review"]["findings"][0]
        self.assertEqual(retained["code_snippet"], "")
        self.assertNotEqual(retained["visibility"], "inline")
        self.assertEqual(
            result.review["v3_review"]["visible_verdict"],
            "blocked_findings",
        )

    def test_unique_changed_region_anchor_is_restored(self):
        blocker = finding(
            priority="P1",
            category="bug",
            required=["path:src/app.py"],
            supporting=[],
        )
        blocker["code_snippet"] = "value = build(2)"

        result = compile_presentation_v1(
            presentation(verdict="blocking", findings=[blocker]),
            pr_details=PR_DETAILS,
            context_meta=context_meta(),
        )

        self.assertTrue(result.publishable)
        self.assertEqual(
            result.presentation["findings"][0]["code_snippet"],
            "value = build(2)",
        )
if __name__ == "__main__":
    unittest.main()

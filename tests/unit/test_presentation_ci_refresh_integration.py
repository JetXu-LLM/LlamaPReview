import copy
import unittest
from unittest.mock import patch

from tests.unit.fakes import (
    ensure_repo_root_on_path,
    install_fake_requests_module,
    set_default_env,
)

ensure_repo_root_on_path()
set_default_env()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline import pipeline_ci
from lambdas.LlamaPReviewPipeline.errors import HeadSuperseded
from lambdas.LlamaPReviewPipeline.review.presentation import (
    compile_presentation_v1,
)


PR_DETAILS = """# Pull Request #204

## File Changes
### deploy.yml
```diff
@@ -1 +1 @@
-mode: old
+mode: new
```
"""


def _check(
    *,
    conclusion: str,
    output_summary: str = "",
    annotation_message: str = "",
) -> dict:
    check = {
        "identity": "integration",
        "status": "completed",
        "classification": (
            "success" if conclusion == "success" else "failure"
        ),
        "conclusion": conclusion,
    }
    if output_summary:
        check["output"] = {"summary": output_summary}
    if annotation_message:
        check["annotations"] = [
            {
                "path": "deploy.yml",
                "start_line": 1,
                "end_line": 1,
                "annotation_level": "failure",
                "message": annotation_message,
            }
        ]
    return check


def _meta(
    *,
    conclusion: str,
    output_summary: str = "",
    annotation_message: str = "",
) -> dict:
    meta = {
        "head_sha": "a" * 40,
        "ci_snapshot": {
            "schema_version": 1,
            "retrieval_outcome": "ok",
            "has_ci": True,
            "checks": [
                _check(
                    conclusion=conclusion,
                    output_summary=output_summary,
                    annotation_message=annotation_message,
                )
            ]
        },
        "evidence_catalog": [
            {
                "id": "path:deploy.yml",
                "source_type": "diff",
                "outcome": "hit",
                "paths": ["deploy.yml"],
                "coverage_type": "changed_region",
            },
            {
                "id": "ci:integration",
                "source_type": "ci",
                "outcome": conclusion,
                "paths": ["deploy.yml"],
                "coverage_type": "changed_region",
            },
        ],
    }
    meta["ci_generation_model_payload"] = (
        pipeline_ci.model_ci_snapshot_payload(meta["ci_snapshot"])
    )
    return meta


def _generated_review() -> dict:
    return {
        "pr_review_comment": "The integration result supports this review.",
        "inline_comments": [],
        "presentation_v1": {
            "version": "presentation_v1",
            "decision": {
                "verdict": "blocking",
                "confidence": "High",
                "summary": "The integration result exposes a blocker.",
                "owner_actions": ["Fix the deployment boundary."],
            },
            "findings": [
                {
                    "headline": "Deployment boundary fails",
                    "priority": "P1",
                    "category": "bug",
                    "confidence": "High",
                    "file_path": "deploy.yml",
                    "code_snippet": "mode: new",
                    "analysis": "The exact-head integration result fails.",
                    "owner_action": "Fix the deployment boundary.",
                    "required_evidence_refs": [
                        "path:deploy.yml",
                        "ci:integration",
                    ],
                    "supporting_evidence_refs": [],
                    "placement": "headline",
                    "suggestion": None,
                }
            ],
            "material_unknowns": [],
            "confidence_checks": [],
            "diagram": None,
        },
        "v3_review": {
            "schema_version": 3,
            "decision": {"verdict": "blocked_findings"},
        },
        "review_generation_status": "complete",
        "review_fallback_used": False,
        "review_publishable": True,
        "review_publication_safe": True,
        "review_presentation_normalizations": [],
        "quality_scoreable": True,
        "quality_exclusion_reasons": [],
    }


class PresentationCIRefreshIntegrationTests(unittest.TestCase):
    def test_global_retrieval_fact_refreshes_first_screen_without_check_churn(self):
        generated = _generated_review()
        generated["presentation_v1"]["decision"].update(
            {
                "verdict": "clear",
                "summary": "No review blocker found. The new mode uses the same guard.",
                "owner_actions": [],
            }
        )
        generated["presentation_v1"]["findings"] = []
        before = _meta(conclusion="success")
        before["ci_snapshot"]["retrieval_outcome"] = "partial"
        after = _meta(conclusion="success")

        refreshed = pipeline_ci.reapply_latest_ci_guard(
            generated,
            PR_DETAILS,
            after,
            generation_context_meta=before,
        )

        self.assertTrue(after["ci_snapshot_changed_after_generation"])
        self.assertEqual(after["ci_changed_evidence_refs"], [])
        self.assertTrue(refreshed["review_publishable"])
        first_screen = refreshed["pr_review_comment"].split("<details>", 1)[0]
        self.assertNotIn("Conditional code-review clear", first_screen)
        self.assertNotIn("retrieval partial", first_screen)

    def test_irrelevant_refresh_metadata_does_not_recompile(self):
        generated = _generated_review()
        before = _meta(conclusion="success")
        after = copy.deepcopy(before)
        after["ci_snapshot"]["actionable_detail_retrieval"] = {
            "attempted_check_count": 1,
            "annotation_omitted_count": 2,
        }
        after["ci_refreshed_at"] = "later"

        with patch.object(pipeline_ci, "compile_presentation_v1") as compile_result:
            refreshed = pipeline_ci.reapply_latest_ci_guard(
                generated,
                PR_DETAILS,
                after,
                generation_context_meta=before,
            )

        self.assertIs(refreshed, generated)
        self.assertFalse(after["ci_snapshot_changed_after_generation"])
        compile_result.assert_not_called()

    def test_changed_head_ci_refresh_is_rejected_before_publication(self):
        class WrongHeadRuntime:
            def get_ci_results_for_head(self, repo, head_sha, **kwargs):
                return {"head_sha": "b" * 40, "checks": []}

        with self.assertRaises(HeadSuperseded):
            pipeline_ci.refresh_review_ci_context(
                WrongHeadRuntime(),
                "owner/repo",
                "a" * 40,
                PR_DETAILS,
                _meta(conclusion="success"),
                stage="review.ci_before_finalize",
            )

    def test_unchanged_ci_returns_the_exact_generated_review(self):
        generated = _generated_review()
        generation_meta = _meta(conclusion="failure")
        current_meta = copy.deepcopy(generation_meta)

        with patch.object(
            pipeline_ci,
            "compile_presentation_v1",
        ) as compile_presentation:
            refreshed = pipeline_ci.reapply_latest_ci_guard(
                generated,
                PR_DETAILS,
                current_meta,
                generation_context_meta=generation_meta,
            )

        self.assertIs(refreshed, generated)
        compile_presentation.assert_not_called()
        self.assertFalse(
            current_meta["ci_snapshot_changed_after_generation"]
        )

    def test_changed_ci_without_presentation_is_typed_nonpublishable(self):
        generated = _generated_review()
        generated.pop("presentation_v1")
        current_meta = _meta(conclusion="success")

        refreshed = pipeline_ci.reapply_latest_ci_guard(
            generated,
            PR_DETAILS,
            current_meta,
            generation_context_meta=_meta(conclusion="failure"),
        )

        self.assertFalse(refreshed["review_publishable"])
        self.assertFalse(refreshed["review_publication_safe"])
        self.assertEqual(
            refreshed["review_failure_kind"],
            "ci_refresh_requires_presentation_v1",
        )
        self.assertEqual(
            refreshed["v3_review"]["decision"]["verdict"],
            "blocked_findings",
        )
        self.assertNotIn("No review blocker found", refreshed["pr_review_comment"])

    def test_new_failure_keeps_code_clear_and_shows_unassessed_ci_red(self):
        generated = _generated_review()
        generated["presentation_v1"]["decision"].update(
            {
                "verdict": "clear",
                "summary": "No review blocker found in the reviewed change.",
                "owner_actions": [],
            }
        )
        generated["presentation_v1"]["findings"] = []
        generated["v3_review"]["decision"]["verdict"] = "clear"
        generated["pr_review_comment"] = "No blocking issues found."

        refreshed = pipeline_ci.reapply_latest_ci_guard(
            generated,
            PR_DETAILS,
            _meta(conclusion="failure"),
            generation_context_meta=_meta(conclusion="success"),
        )

        self.assertTrue(refreshed["review_publishable"])
        self.assertTrue(refreshed["review_publication_safe"])
        self.assertEqual(refreshed["v3_review"]["decision"]["verdict"], "clear")
        first_screen = refreshed["pr_review_comment"].split("<details>", 1)[0]
        self.assertIn("Conditional code-review clear", first_screen)
        self.assertIn("1 failed", first_screen)
        self.assertIn("no CI-dependent merge-safety claim", first_screen)
        self.assertNotIn("safe to merge", first_screen.casefold())

    def test_new_failure_retains_independent_material_unknown(self):
        generated = _generated_review()
        presentation = generated["presentation_v1"]
        presentation["decision"].update(
            {
                "verdict": "verification_needed",
                "summary": "Deployment ownership has not been verified.",
                "owner_actions": ["Confirm the deployment owner."],
            }
        )
        presentation["findings"] = []
        presentation["material_unknowns"] = [
            {
                "missing_fact": "Deployment ownership is unverified.",
                "impact": "The owner must confirm the handoff.",
                "owner_action": "Confirm the deployment owner.",
                "evidence_refs": ["path:deploy.yml"],
            }
        ]

        refreshed = pipeline_ci.reapply_latest_ci_guard(
            generated,
            PR_DETAILS,
            _meta(conclusion="failure"),
            generation_context_meta=_meta(conclusion="success"),
        )

        self.assertTrue(refreshed["review_publishable"])
        self.assertEqual(
            refreshed["v3_review"]["decision"]["verdict"],
            "unverified",
        )
        first_screen = refreshed["pr_review_comment"].split("<details>", 1)[0]
        self.assertIn("Deployment ownership is unverified", first_screen)
        self.assertIn("1 failed", first_screen)
        self.assertNotIn("safe to merge", first_screen.casefold())

    def test_new_failure_retries_when_clear_prose_depended_on_prior_pass(self):
        generated = _generated_review()
        presentation = generated["presentation_v1"]
        presentation["decision"].update(
            {
                "verdict": "clear",
                "summary": (
                    "No review blocker found. Build Verification success "
                    "confirms the changed deployment mode."
                ),
                "owner_actions": [],
            }
        )
        presentation["findings"] = []
        before = _meta(conclusion="success")
        after = _meta(conclusion="failure")
        for meta in (before, after):
            meta["ci_snapshot"]["checks"][0]["name"] = "Build Verification"
            meta["ci_generation_model_payload"] = (
                pipeline_ci.model_ci_snapshot_payload(meta["ci_snapshot"])
            )

        refreshed = pipeline_ci.reapply_latest_ci_guard(
            generated,
            PR_DETAILS,
            after,
            generation_context_meta=before,
        )

        self.assertFalse(refreshed["review_publishable"])
        self.assertTrue(refreshed["review_failure_retryable"])
        self.assertEqual(
            refreshed["review_failure_kind"],
            "ci_refresh_changed_ci_core_prose_tainted",
        )

    def test_deciding_basis_loss_never_synthesizes_clear(self):
        generated = _generated_review()
        current_meta = _meta(conclusion="success")
        refreshed = pipeline_ci.reapply_latest_ci_guard(
            generated,
            PR_DETAILS,
            current_meta,
            generation_context_meta=_meta(conclusion="failure"),
        )

        self.assertFalse(refreshed["review_publishable"])
        self.assertFalse(refreshed["review_publication_safe"])
        self.assertEqual(
            refreshed["review_failure_kind"],
            "ci_refresh_deciding_item_loss",
        )
        self.assertTrue(refreshed["review_failure_retryable"])
        self.assertEqual(
            refreshed["v3_review"]["decision"]["verdict"],
            "blocked_findings",
        )
        self.assertEqual(
            refreshed["pr_review_comment"],
            generated["pr_review_comment"],
        )

    def test_same_status_changed_diagnostics_invalidate_required_ci(self):
        cases = (
            (
                "output",
                _meta(
                    conclusion="failure",
                    output_summary="Old failure path.",
                ),
                _meta(
                    conclusion="failure",
                    output_summary="New failure path.",
                ),
            ),
            (
                "annotation",
                _meta(
                    conclusion="failure",
                    annotation_message="Old line-level failure.",
                ),
                _meta(
                    conclusion="failure",
                    annotation_message="New line-level failure.",
                ),
            ),
        )
        for label, generation_meta, current_meta in cases:
            with self.subTest(label=label):
                generated = _generated_review()

                refreshed = pipeline_ci.reapply_latest_ci_guard(
                    generated,
                    PR_DETAILS,
                    current_meta,
                    generation_context_meta=generation_meta,
                )

                self.assertFalse(refreshed["review_publishable"])
                self.assertFalse(refreshed["review_publication_safe"])
                self.assertEqual(
                    refreshed["review_failure_kind"],
                    "ci_refresh_deciding_item_loss",
                )
                self.assertTrue(refreshed["review_failure_retryable"])
                self.assertEqual(
                    refreshed["pr_review_comment"],
                    generated["pr_review_comment"],
                )

    def test_confidence_check_ci_churn_is_dropped_without_losing_finding(self):
        generated = _generated_review()
        generated["review_model_finish_reason"] = "stop"
        presentation = generated["presentation_v1"]
        presentation["findings"][0]["required_evidence_refs"] = [
            "path:deploy.yml"
        ]
        presentation["confidence_checks"] = [
            {
                "check": "Exact-head integration result",
                "result": "The integration check failed.",
                "evidence_refs": ["ci:integration"],
            }
        ]
        current_meta = _meta(conclusion="success")
        refreshed = pipeline_ci.reapply_latest_ci_guard(
            generated,
            PR_DETAILS,
            current_meta,
            generation_context_meta=_meta(conclusion="failure"),
        )

        self.assertTrue(refreshed["review_publishable"])
        self.assertTrue(refreshed["review_publication_safe"])
        self.assertEqual(
            len(refreshed["presentation_v1"]["findings"]),
            1,
        )
        self.assertEqual(
            refreshed["presentation_v1"]["confidence_checks"],
            [],
        )
        self.assertEqual(refreshed["review_model_finish_reason"], "stop")
        self.assertFalse(refreshed["quality_scoreable"])
        self.assertIn(
            "ci_evidence_changed_after_generation",
            refreshed["quality_exclusion_reasons"],
        )

    def test_safe_local_recompile_preserves_telemetry_and_marks_quality(self):
        generated = _generated_review()
        dependent = generated["presentation_v1"]["findings"][0]
        dependent["placement"] = "inline"
        survivor = copy.deepcopy(dependent)
        survivor.update(
            {
                "headline": "Deployment mode bypasses the required guard",
                "analysis": (
                    "The changed deployment mode bypasses the local guard."
                ),
                "owner_action": "Restore the deployment guard before merging.",
                "required_evidence_refs": ["path:deploy.yml"],
                "placement": "collapsed",
            }
        )
        generated["presentation_v1"]["findings"] = [
            dependent,
            survivor,
        ]
        selected_phases = [
            {
                "phase": "deep_judgment",
                "thinking": True,
                "reasoning_effort": "max",
                "finish_reason": "stop",
            },
            {
                "phase": "final_presentation",
                "thinking": True,
                "reasoning_effort": "high",
                "finish_reason": "stop",
            },
        ]
        generated.update(
            {
                "review_presentation_selected_phase": "final_presentation",
                "review_model_finish_reason": "stop",
                "review_final_thinking": True,
                "review_final_reasoning_effort": "high",
                "review_model_phases": selected_phases,
            }
        )

        refreshed = pipeline_ci.reapply_latest_ci_guard(
            generated,
            PR_DETAILS,
            _meta(conclusion="success"),
            generation_context_meta=_meta(conclusion="failure"),
        )

        self.assertTrue(refreshed["review_publishable"])
        self.assertTrue(refreshed["review_publication_safe"])
        retained = refreshed["v3_review"]["findings"]
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            retained[0]["headline"],
            survivor["headline"],
        )
        self.assertEqual(retained[0]["visibility"], "inline")
        self.assertEqual(len(refreshed["inline_comments"]), 1)
        self.assertEqual(
            refreshed["review_presentation_selected_phase"],
            "final_presentation",
        )
        self.assertEqual(refreshed["review_model_finish_reason"], "stop")
        self.assertTrue(refreshed["review_final_thinking"])
        self.assertEqual(refreshed["review_final_reasoning_effort"], "high")
        self.assertEqual(refreshed["review_model_phases"], selected_phases)
        self.assertFalse(refreshed["quality_scoreable"])
        self.assertIn(
            "ci_evidence_changed_after_generation",
            refreshed["quality_exclusion_reasons"],
        )

    def test_finding_supporting_ci_is_removed_before_refresh(self):
        generated = _generated_review()
        generated["presentation_v1"]["findings"][0][
            "required_evidence_refs"
        ] = ["path:deploy.yml"]
        generated["presentation_v1"]["findings"][0][
            "supporting_evidence_refs"
        ] = ["ci:integration"]

        compiled = compile_presentation_v1(
            generated["presentation_v1"],
            pr_details=PR_DETAILS,
            context_meta=_meta(conclusion="failure"),
        )

        self.assertTrue(compiled.publishable)
        self.assertTrue(compiled.safe_partial)
        self.assertEqual(
            compiled.review["v3_review"]["findings"][0][
                "supporting_evidence_refs"
            ],
            [],
        )


if __name__ == "__main__":
    unittest.main()

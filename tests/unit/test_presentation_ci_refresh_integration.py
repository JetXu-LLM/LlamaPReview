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

    def test_new_failure_holds_clear_review_for_existing_bounded_retry(self):
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

        with patch.object(
            pipeline_ci,
            "compile_presentation_v1",
        ) as compile_presentation:
            refreshed = pipeline_ci.reapply_latest_ci_guard(
                generated,
                PR_DETAILS,
                _meta(conclusion="failure"),
                generation_context_meta=_meta(conclusion="success"),
            )

        compile_presentation.assert_not_called()
        self.assertFalse(refreshed["review_publishable"])
        self.assertTrue(refreshed["review_failure_retryable"])
        self.assertEqual(
            refreshed["review_failure_kind"],
            "ci_blocking_evidence_changed_after_generation",
        )
        self.assertEqual(
            refreshed["review_failure_class"],
            "CINewBlockingEvidence",
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

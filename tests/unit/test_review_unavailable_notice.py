import unittest
from unittest.mock import patch

from tests.unit.fakes import (
    FakeDynamoResource,
    ensure_repo_root_on_path,
    install_fake_aws_modules,
    install_fake_jwt_module,
    install_fake_requests_module,
    set_default_env,
)

ensure_repo_root_on_path()
set_default_env()
install_fake_aws_modules(FakeDynamoResource())
install_fake_jwt_module()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline import orchestrator, pipeline_admission
from lambdas.LlamaPReviewPipeline.errors import (
    HeadSuperseded,
    ReviewGenerationIncomplete,
)
from lambdas.LlamaPReviewPipeline.pipeline_publication import (
    PublicationContext,
)
from lambdas.LlamaPReviewPipeline.review.publish import PUBLIC_FOOTER_MARKER
from lambdas.LlamaPReviewPipeline.review.terminal_messages import (
    REVIEW_UNAVAILABLE_NOTICE,
)


HEAD = "a" * 40


def _disposition(*, kind="open_same_head", locked=False):
    enum = pipeline_admission.PRDispositionKind(kind)
    actual = "b" * 40 if kind.endswith("new_head") else HEAD
    state = "closed" if kind.startswith(("merged", "closed")) else "open"
    return pipeline_admission.PRLifecycleDisposition(
        kind=enum,
        expected_head_sha=HEAD,
        actual_head_sha=actual,
        current_state=state,
        merged=kind.startswith("merged"),
        stage="review.after_final",
        locked=locked,
    )


def _publication_context(*, dry_run=False):
    return PublicationContext(
        repo="owner/repo",
        pr_number=7,
        head_sha=HEAD,
        expected_status="CONTEXT_READY",
        phase="review",
        run_id="run-7",
        generation_attempt=2,
        runtime_identity={"source_commit": "c" * 40},
        phase_claim={
            "phase": "review",
            "attempt": 2,
            "owner_id": "request-2",
            "stream_event_id": "event-2",
        },
        dry_run=dry_run,
    )


def _review_json(*, retryable=False, stage="final_presentation"):
    return {
        "review_generation_status": "incomplete",
        "review_publishable": False,
        "review_publication_safe": False,
        "review_failure_kind": "presentation_invalid",
        "review_failure_stage": stage,
        "review_failure_retryable": retryable,
        "review_failure_message": "private failure detail",
        "quality_scoreable": False,
        "quality_exclusion_reasons": ["presentation_invalid"],
    }


def _usage(*, completed=True):
    phases = (
        [
            {
                "phase": "deep_judgment",
                "pipeline_phase": "review",
                "pipeline_attempt": 2,
                "model": "deepseek-v4-flash",
                "status": "completed",
                "usage_state": "reported",
                "usage": {
                    "prompt_tokens": 80,
                    "completion_tokens": 20,
                    "total_tokens": 100,
                },
            }
        ]
        if completed
        else []
    )
    return {
        "deepseek_model_phases": phases,
        "deepseek_discarded_model_phases": [],
        "deepseek_all_attempt_model_phases": phases,
        "deepseek_usage_total": (
            {
                "prompt_tokens": 80,
                "completion_tokens": 20,
                "total_tokens": 100,
            }
            if completed
            else {}
        ),
        "deepseek_winning_usage_total": {},
        "deepseek_discarded_usage_total": {},
        "deepseek_usage_accounting": {
            "complete_numeric_usage": completed,
            "all_call_count": len(phases),
        },
    }


def _call(
    *,
    disposition=None,
    usage=None,
    dry_run=False,
    attempt=2,
    stage="final_presentation",
):
    return orchestrator._persist_terminal_nonpublishable_review(
        repo="owner/repo",
        pr_number=7,
        head_sha=HEAD,
        run_id="run-7",
        review_mode="high",
        attempt=attempt,
        item={"context_runtime_identity": {"source_commit": "d" * 40}},
        review_json=_review_json(stage=stage),
        context_meta={},
        runtime=object(),
        disposition=disposition or _disposition(),
        publication_context=_publication_context(dry_run=dry_run),
        deadline=None,
        table=object(),
        phase_started=0.0,
        usage_accounting=usage if usage is not None else _usage(),
        review_runtime_identity={"source_commit": "c" * 40},
        phase_claim=_publication_context().phase_claim,
    )


class ReviewUnavailableNoticeTests(unittest.TestCase):
    def test_paid_terminal_failure_uses_exact_notice_and_preserves_accounting(self):
        captured = {}

        def commit(prepared, terminal_attributes, **kwargs):
            captured["prepared"] = prepared
            captured["terminal"] = terminal_attributes
            captured["context"] = kwargs["context"]
            return True

        with patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
            side_effect=commit,
        ), patch.object(
            orchestrator.persistence,
            "store_review_failure",
        ) as store_failure:
            self.assertTrue(_call())

        store_failure.assert_not_called()
        prepared = captured["prepared"]
        terminal = captured["terminal"]
        self.assertEqual(prepared.main_body, REVIEW_UNAVAILABLE_NOTICE)
        self.assertEqual(prepared.comments, ())
        self.assertEqual(prepared.request_payload()["event"], "COMMENT")
        self.assertNotIn(PUBLIC_FOOTER_MARKER, prepared.main_body)
        self.assertNotIn("mermaid", prepared.main_body.casefold())
        self.assertNotIn("deepseek", prepared.main_body.casefold())
        self.assertEqual(prepared.artifact["review_mode"], "failed")
        self.assertEqual(
            prepared.artifact["review_generation_status"], "failed"
        )
        self.assertFalse(prepared.artifact["quality_scoreable"])
        self.assertEqual(
            prepared.artifact["review_failure_kind"],
            "presentation_invalid",
        )
        self.assertEqual(
            prepared.artifact["deepseek_usage_total"]["total_tokens"],
            100,
        )
        self.assertEqual(terminal["review_mode"], "failed")
        self.assertFalse(terminal["quality_scoreable"])
        self.assertEqual(captured["context"].publication_kind, "ordinary_review")

    def test_dry_run_keeps_the_same_candidate_but_no_alternate_write_path(self):
        captured = {}

        def commit(prepared, terminal_attributes, **kwargs):
            captured["prepared"] = prepared
            captured["context"] = kwargs["context"]
            return True

        with patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
            side_effect=commit,
        ):
            self.assertTrue(_call(dry_run=True))

        self.assertTrue(captured["context"].dry_run)
        self.assertEqual(captured["prepared"].main_body, REVIEW_UNAVAILABLE_NOTICE)

    def test_no_completed_paid_work_remains_private(self):
        with patch.object(
            orchestrator.pipeline_admission,
            "assert_current_head",
            return_value=HEAD,
        ), patch.object(
            orchestrator.persistence,
            "store_review_failure",
            return_value=True,
        ) as store_failure, patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
        ) as commit:
            self.assertTrue(_call(usage=_usage(completed=False)))

        store_failure.assert_called_once()
        commit.assert_not_called()

    def test_paid_deep_failure_remains_private(self):
        with patch.object(
            orchestrator.pipeline_admission,
            "assert_current_head",
            return_value=HEAD,
        ), patch.object(
            orchestrator.persistence,
            "store_review_failure",
            return_value=True,
        ) as store_failure, patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
        ) as commit:
            self.assertTrue(_call(stage="deep_judgment"))

        store_failure.assert_called_once()
        commit.assert_not_called()

    def test_locked_open_pr_remains_private(self):
        with patch.object(
            orchestrator.pipeline_admission,
            "assert_current_head",
            return_value=HEAD,
        ), patch.object(
            orchestrator.persistence,
            "store_review_failure",
            return_value=True,
        ), patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
        ) as commit:
            self.assertTrue(_call(disposition=_disposition(locked=True)))

        commit.assert_not_called()

    def test_unknown_lock_state_remains_private(self):
        with patch.object(
            orchestrator.pipeline_admission,
            "assert_current_head",
            return_value=HEAD,
        ), patch.object(
            orchestrator.persistence,
            "store_review_failure",
            return_value=True,
        ), patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
        ) as commit:
            self.assertTrue(
                _call(disposition=_disposition(locked=None))
            )

        commit.assert_not_called()

    def test_ended_pr_never_uses_the_failure_notice(self):
        for kind in ("merged_same_head", "closed_same_head"):
            with self.subTest(kind=kind), patch.object(
                orchestrator.pipeline_admission,
                "assert_current_head",
                return_value=HEAD,
            ), patch.object(
                orchestrator.persistence,
                "store_review_failure",
                return_value=True,
            ), patch.object(
                orchestrator.pipeline_publication,
                "commit_prepared",
            ) as commit:
                self.assertTrue(_call(disposition=_disposition(kind=kind)))
                commit.assert_not_called()

    def test_new_head_never_uses_the_failure_notice(self):
        with patch.object(
            orchestrator.pipeline_admission,
            "assert_current_head",
            side_effect=HeadSuperseded(HEAD, "b" * 40, stage="review.failure"),
        ), patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
        ) as commit:
            with self.assertRaises(HeadSuperseded):
                _call(disposition=_disposition(kind="open_new_head"))

        commit.assert_not_called()

    def test_retryable_failure_raises_before_any_notice(self):
        with patch.object(orchestrator.config, "MAX_ATTEMPTS", 2), patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
        ) as commit:
            with self.assertRaises(ReviewGenerationIncomplete):
                orchestrator._persist_terminal_nonpublishable_review(
                    repo="owner/repo",
                    pr_number=7,
                    head_sha=HEAD,
                    run_id="run-7",
                    review_mode="high",
                    attempt=1,
                    item={},
                    review_json=_review_json(retryable=True),
                    context_meta={},
                    runtime=object(),
                    disposition=_disposition(),
                    publication_context=_publication_context(),
                    deadline=None,
                    table=object(),
                    phase_started=0.0,
                    usage_accounting=_usage(),
                    review_runtime_identity={},
                    phase_claim=_publication_context().phase_claim,
                )

        commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

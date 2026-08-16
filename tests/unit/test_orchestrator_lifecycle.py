import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
from lambdas.LlamaPReviewPipeline.errors import HeadVerificationUnavailable
from lambdas.LlamaPReviewPipeline.review.publish import PreparedGitHubReview


REPO = "owner/repo"
PR = 7
HEAD_A = "a" * 40
HEAD_B = "b" * 40
RUN = "delivery-a"


class SequenceRuntime:
    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)
        self.last = snapshots[-1]

    def get_pr_head_snapshot(self, _repo, _pr_number):
        if self.snapshots:
            self.last = self.snapshots.pop(0)
        return dict(self.last)


def _snapshot(head=HEAD_A, *, state="open", merged=False):
    return {"head_sha": head, "state": state, "merged": merged}


def _claim(phase="context"):
    return {
        "phase": phase,
        "owner_id": f"{phase}-owner",
        "stream_event_id": f"{phase}-stream",
        "attempt": 1,
        "expires_at_epoch": 9999999999,
    }


def _item(status="PENDING", *, marker=False):
    item = {
        "repo": REPO,
        "pr_number": PR,
        "status": status,
        "installation_id": 123,
        "head_sha": HEAD_A,
        "run_id": RUN,
        "default_branch": "main",
        "context_attempt": 1,
        "review_attempt": 1 if status == "CONTEXT_READY" else 0,
    }
    if marker:
        item["initial_admission"] = {
            "schema_version": 1,
            "disposition": "open_same_head",
            "head_sha": HEAD_A,
            "run_id": RUN,
            "admitted_at": "2026-08-17T00:00:00+00:00",
        }
    return item


def _admission(item, phase="context"):
    return pipeline_admission.PhaseAdmission(
        current_item=dict(item),
        phase_claim=_claim(phase),
        attempt=1,
    )


class ContextLifecycleOrchestrationTests(unittest.TestCase):
    def test_context_claim_binds_exact_stream_head_and_run(self):
        item = _item()
        with patch.object(
            orchestrator.pipeline_admission,
            "claim_phase_delivery",
            return_value=None,
        ) as claim:
            orchestrator.run_context_phase(
                item,
                stream_event_id="stream-event",
            )
        self.assertEqual(claim.call_args.kwargs["stream_head_sha"], HEAD_A)
        self.assertEqual(claim.call_args.kwargs["stream_run_id"], RUN)

    def test_open_new_head_at_ingest_requeues_once_before_provider_work(self):
        item = _item()
        runtime = SequenceRuntime(_snapshot(HEAD_B))
        with patch.object(
            orchestrator.pipeline_admission,
            "claim_phase_delivery",
            return_value=_admission(item),
        ), patch.object(
            orchestrator.pipeline_admission,
            "installation_token",
            return_value="token",
        ), patch.object(
            orchestrator.pipeline_publication,
            "recover_pending",
            return_value=False,
        ), patch.object(
            orchestrator.persistence,
            "get_item",
            return_value=item,
        ), patch.object(
            orchestrator.persistence,
            "requeue_head_successor",
            return_value=True,
        ) as requeue, patch.object(
            orchestrator,
            "prepare_provider_source",
        ) as provider_source:
            orchestrator.run_context_phase(
                item,
                runtime=runtime,
                stream_event_id="context-stream",
            )

        requeue.assert_called_once()
        self.assertEqual(requeue.call_args.kwargs["actual_head_sha"], HEAD_B)
        self.assertEqual(requeue.call_args.kwargs["stage"], "context.ingest")
        provider_source.assert_not_called()

    def test_unverified_ingest_is_retryable_and_performs_no_work(self):
        item = _item()
        runtime = SequenceRuntime({"head_sha": HEAD_A, "state": "open"})
        with patch.object(
            orchestrator.pipeline_admission,
            "claim_phase_delivery",
            return_value=_admission(item),
        ), patch.object(
            orchestrator.pipeline_admission,
            "installation_token",
            return_value="token",
        ), patch.object(
            orchestrator.pipeline_publication,
            "recover_pending",
            return_value=False,
        ), patch.object(
            orchestrator.persistence,
            "get_item",
            return_value=item,
        ), patch.object(
            orchestrator.persistence,
            "record_initial_admission",
        ) as admission, patch.object(
            orchestrator,
            "prepare_provider_source",
        ) as provider_source, patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
        ) as commit:
            with self.assertRaises(HeadVerificationUnavailable):
                orchestrator.run_context_phase(
                    item,
                    runtime=runtime,
                    stream_event_id="context-stream",
                )

        admission.assert_not_called()
        provider_source.assert_not_called()
        commit.assert_not_called()

    def test_initial_ended_pr_is_silent_but_admitted_retry_cancels(self):
        ended = _snapshot(state="closed", merged=True)
        for marker, expected in ((False, "silent"), (True, "cancel")):
            with self.subTest(expected=expected):
                item = _item(marker=marker)
                runtime = SequenceRuntime(ended)
                with patch.object(
                    orchestrator.pipeline_admission,
                    "claim_phase_delivery",
                    return_value=_admission(item),
                ), patch.object(
                    orchestrator.pipeline_admission,
                    "installation_token",
                    return_value="token",
                ), patch.object(
                    orchestrator.pipeline_publication,
                    "recover_pending",
                    return_value=False,
                ), patch.object(
                    orchestrator.persistence,
                    "get_item",
                    return_value=item,
                ), patch.object(
                    orchestrator.persistence,
                    "mark_superseded",
                    return_value=True,
                ) as superseded, patch.object(
                    orchestrator.pipeline_publication,
                    "commit_lifecycle_cancellation",
                    return_value=True,
                ) as cancellation:
                    orchestrator.run_context_phase(
                        item,
                        runtime=runtime,
                        stream_event_id="context-stream",
                    )
                if marker:
                    cancellation.assert_called_once()
                    superseded.assert_not_called()
                else:
                    cancellation.assert_not_called()
                    superseded.assert_called_once()

    def test_normal_context_forwards_pre_reconcile_boundary(self):
        callback = Mock()
        with patch.object(
            orchestrator,
            "collect_context",
            return_value=("context", {}),
        ) as collect:
            orchestrator._context_for_mode(
                review_mode="high",
                runtime=object(),
                token="token",
                repo=REPO,
                pr_number=PR,
                pr_content={},
                pr_details="details",
                head_sha=HEAD_A,
                default_branch="main",
                trace_metadata={},
                route_plan={},
                deadline=Mock(),
                before_first_reconcile=callback,
            )
        self.assertIs(
            collect.call_args.kwargs["before_first_reconcile"], callback
        )


class ReviewLifecycleOrchestrationTests(unittest.TestCase):
    def _run_review_patches(self, item, runtime):
        return (
            patch.object(
                orchestrator.pipeline_admission,
                "claim_phase_delivery",
                return_value=_admission(item, "review"),
            ),
            patch.object(
                orchestrator.pipeline_admission,
                "installation_token",
                return_value="token",
            ),
            patch.object(
                orchestrator.pipeline_publication,
                "recover_pending",
                return_value=False,
            ),
            patch.object(
                orchestrator.persistence,
                "get_item",
                return_value=item,
            ),
            patch.object(
                orchestrator.persistence,
                "load_context_bundle_from_item",
                return_value=(
                    "context",
                    "details",
                    {"review_mode": "high", "ci_snapshot": {}},
                ),
            ),
        )

    def test_merge_before_final_cancels_and_never_returns_final(self):
        item = _item("CONTEXT_READY", marker=True)
        runtime = SequenceRuntime(_snapshot(), _snapshot(state="closed", merged=True))
        patches = self._run_review_patches(item, runtime)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(
            orchestrator,
            "refresh_review_ci_context",
            side_effect=lambda _runtime, _repo, _head, details, meta, **_kw: (
                details,
                meta,
            ),
        ), patch.object(
            orchestrator, "bind_provider_call_accounting"
        ), patch.object(
            orchestrator,
            "generate_review",
            side_effect=lambda *_args, **kwargs: kwargs["before_final"](),
        ) as generate, patch.object(
            orchestrator.pipeline_publication,
            "commit_lifecycle_cancellation",
            return_value=True,
        ) as cancellation:
            orchestrator.run_review_phase(
                item,
                runtime=runtime,
                deepseek_client=SimpleNamespace(
                    provider_call_records=lambda: []
                ),
                stream_event_id="review-stream",
            )
        generate.assert_called_once()
        cancellation.assert_called_once()
        self.assertEqual(
            cancellation.call_args.kwargs["context"].required_disposition,
            "merged_same_head",
        )

    def test_publishable_final_on_merged_head_uses_post_merge_projection(self):
        item = _item("CONTEXT_READY", marker=True)
        runtime = SequenceRuntime(
            _snapshot(),
            _snapshot(state="closed", merged=True),
            _snapshot(state="closed", merged=True),
            _snapshot(state="closed", merged=True),
        )
        final_review = {
            "review_generation_status": "complete",
            "review_publishable": True,
            "review_publication_safe": True,
            "review_fallback_used": False,
            "inline_comments": [],
            "review_model_phases": [],
        }
        prepared = PreparedGitHubReview(
            head_sha=HEAD_A,
            main_body="post merge",
            comments=(),
            artifact={"main_comment": "post merge", "review_mode": "high"},
            publication_kind="post_merge_follow_up",
            required_disposition="merged_same_head",
        )
        projected = SimpleNamespace(
            prepared=prepared,
            terminal_attributes={"deepseek_usage_accounting": {}},
            generation_fields={
                "review_generation_status": "complete",
                "review_failure_kind": None,
                "quality_scoreable": True,
            },
        )
        patches = self._run_review_patches(item, runtime)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(
            orchestrator,
            "refresh_review_ci_context",
            side_effect=lambda _runtime, _repo, _head, details, meta, **_kw: (
                details,
                meta,
            ),
        ), patch.object(
            orchestrator, "bind_provider_call_accounting"
        ), patch.object(
            orchestrator, "generate_review", return_value=final_review
        ), patch.object(
            orchestrator,
            "_persist_terminal_nonpublishable_review",
            return_value=False,
        ), patch.object(
            orchestrator.result_artifact,
            "is_nonpublishable",
            return_value=False,
        ), patch.object(
            orchestrator, "reapply_latest_ci_guard", return_value=final_review
        ), patch.object(
            orchestrator,
            "_fetch_pr_files_and_contents",
            return_value=(object(), [], {}, {}),
        ), patch.object(
            orchestrator,
            "prepare_review_publication",
            return_value=prepared,
        ) as prepare, patch.object(
            orchestrator.result_artifact,
            "build_publishable_result",
            return_value=projected,
        ), patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
            return_value=True,
        ) as commit:
            orchestrator.run_review_phase(
                item,
                runtime=runtime,
                deepseek_client=SimpleNamespace(
                    provider_call_records=lambda: []
                ),
                stream_event_id="review-stream",
            )

        self.assertEqual(
            prepare.call_args.kwargs["publication_kind"],
            "post_merge_follow_up",
        )
        self.assertEqual(
            commit.call_args.kwargs["context"].required_disposition,
            "merged_same_head",
        )


if __name__ == "__main__":
    unittest.main()

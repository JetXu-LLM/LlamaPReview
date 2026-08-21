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
from lambdas.LlamaPReviewPipeline.errors import (
    HeadVerificationUnavailable,
    PRLifecycleSuperseded,
)
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


def _snapshot(head=HEAD_A, *, state="open", merged=False, locked=False):
    return {
        "head_sha": head,
        "state": state,
        "merged": merged,
        "locked": locked,
    }


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
    def test_locked_unavailable_uses_distinct_terminal_metric(self):
        disposition = pipeline_admission.PRLifecycleDisposition(
            kind=pipeline_admission.PRDispositionKind.MERGED_SAME_HEAD,
            expected_head_sha=HEAD_A,
            actual_head_sha=HEAD_A,
            current_state="closed",
            merged=True,
            stage="context.ingest",
            locked=True,
        )
        context = orchestrator.pipeline_publication.PublicationContext(
            repo=REPO,
            pr_number=PR,
            head_sha=HEAD_A,
            expected_status="PENDING",
            phase="context",
            run_id=RUN,
            generation_attempt=1,
            runtime_identity={},
            phase_claim=_claim(),
            dry_run=False,
        )
        accounting = {
            "deepseek_usage_accounting": {
                "complete_numeric_usage": True,
                "unreported_usage_call_count": 0,
            }
        }
        with patch.object(
            orchestrator.persistence,
            "mark_superseded",
            return_value=True,
        ) as superseded, patch.object(
            orchestrator,
            "_emit_pipeline_metric",
        ) as metric:
            stored = orchestrator._mark_lifecycle_publication_unavailable(
                disposition,
                context=context,
                publication_kind="lifecycle_cancellation",
                accounting=accounting,
                table=object(),
            )

        self.assertTrue(stored)
        attrs = superseded.call_args.kwargs["extra_attrs"]
        self.assertEqual(
            superseded.call_args.kwargs["superseded_kind"],
            "publication_unavailable_locked",
        )
        self.assertEqual(attrs["publication_status"], "unavailable_locked")
        self.assertTrue(attrs["publication_unavailable_locked"])
        self.assertTrue(
            attrs["deepseek_usage_accounting"]["complete_numeric_usage"]
        )
        self.assertEqual(
            metric.call_args.args[0],
            "lifecycle_publication_unavailable",
        )
        self.assertNotEqual(
            metric.call_args.args[0],
            "lifecycle_publication_complete",
        )

    def test_second_precheck_unavailable_does_not_emit_cancellation_complete(self):
        disposition = pipeline_admission.PRLifecycleDisposition(
            kind=pipeline_admission.PRDispositionKind.MERGED_SAME_HEAD,
            expected_head_sha=HEAD_A,
            actual_head_sha=HEAD_A,
            current_state="closed",
            merged=True,
            stage="context.pre_reconcile",
            locked=False,
        )
        context = orchestrator.pipeline_publication.PublicationContext(
            repo=REPO,
            pr_number=PR,
            head_sha=HEAD_A,
            expected_status="PENDING",
            phase="context",
            run_id=RUN,
            generation_attempt=1,
            runtime_identity={},
            phase_claim=_claim(),
            dry_run=False,
        )
        accounting = {
            "deepseek_usage_accounting": {
                "complete_numeric_usage": True,
                "unreported_usage_call_count": 0,
            }
        }

        def locked_after_intent(**kwargs):
            kwargs["lifecycle_unavailable_observer"](
                {
                    "phase": "context",
                    "stage": "publication.pre_publish_disposition",
                    "publication_kind": "lifecycle_cancellation",
                    "lifecycle": "merged",
                    "reason": "locked",
                    "stored": True,
                    **accounting,
                }
            )
            return True

        with patch.object(
            orchestrator.pipeline_publication,
            "commit_lifecycle_cancellation",
            side_effect=locked_after_intent,
        ), patch.object(
            orchestrator,
            "_emit_pipeline_metric",
        ) as metric:
            orchestrator._commit_lifecycle_cancellation(
                disposition,
                context=context,
                runtime=object(),
                deadline=object(),
                accounting=accounting,
                table=object(),
            )

        names = [call.args[0] for call in metric.call_args_list]
        self.assertEqual(names.count("lifecycle_publication_unavailable"), 1)
        self.assertNotIn("lifecycle_publication_complete", names)

    def test_locked_prepared_recovery_retains_candidate_accounting(self):
        item = _item(marker=True)
        item["publication_intent"] = {
            "state": "prepared",
            "publication_key": "1" * 32,
            "publication_kind": "post_merge_follow_up",
            "required_disposition": "merged_same_head",
        }
        stopped = PRLifecycleSuperseded(
            HEAD_A,
            HEAD_A,
            current_state="closed",
            merged=True,
            stage="publication.pre_publish_disposition",
            superseded_kind="publication_unavailable_locked",
        )
        accounting = {
            "deepseek_all_attempt_model_phases": [{"phase": "route"}],
            "deepseek_usage_total": {"total_tokens": 29},
            "deepseek_usage_accounting": {
                "complete_numeric_usage": True,
                "unreported_usage_call_count": 0,
            },
        }
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
            side_effect=stopped,
        ), patch.object(
            orchestrator.pipeline_publication.persistence,
            "get_item",
            return_value=item,
        ), patch.object(
            orchestrator.pipeline_publication,
            "load_candidate",
            return_value={"terminal_attributes": accounting},
        ), patch.object(
            orchestrator.persistence,
            "mark_superseded",
            return_value=True,
        ) as superseded, patch.object(
            orchestrator,
            "_emit_pipeline_metric",
        ) as metric:
            orchestrator.run_context_phase(
                item,
                runtime=SequenceRuntime(
                    _snapshot(state="closed", merged=True, locked=True)
                ),
                stream_event_id="context-stream",
            )

        attrs = superseded.call_args.kwargs["extra_attrs"]
        self.assertEqual(attrs["publication_status"], "aborted_before_dispatch")
        self.assertEqual(attrs["deepseek_usage_total"]["total_tokens"], 29)
        self.assertTrue(
            attrs["deepseek_usage_accounting"]["complete_numeric_usage"]
        )
        self.assertFalse(
            any(call.args and call.args[0] == "lifecycle_publication_complete"
                for call in metric.call_args_list)
        )

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

    def test_successor_policy_off_supersedes_instead_of_requeueing(self):
        item = _item()
        runtime = SequenceRuntime(_snapshot(HEAD_B))
        with patch.object(
            orchestrator.pipeline_capacity.config,
            "PIPELINE_CAPACITY_POLICY",
            "successor=off",
        ), patch.object(
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
        ) as requeue, patch.object(
            orchestrator.persistence,
            "mark_superseded",
            return_value=True,
        ) as superseded, patch.object(
            orchestrator,
            "prepare_provider_source",
        ) as provider_source:
            orchestrator.run_context_phase(
                item,
                runtime=runtime,
                stream_event_id="context-stream",
            )

        requeue.assert_not_called()
        superseded.assert_called_once()
        self.assertEqual(
            superseded.call_args.kwargs["superseded_kind"],
            "head_changed",
        )
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

    def test_locked_admitted_merge_is_silent_unavailable_not_cancellation(self):
        item = _item(marker=True)
        runtime = SequenceRuntime(
            _snapshot(state="closed", merged=True, locked=True)
        )
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
            orchestrator,
            "_mark_lifecycle_publication_unavailable",
            return_value=True,
        ) as unavailable, patch.object(
            orchestrator.pipeline_publication,
            "commit_lifecycle_cancellation",
        ) as cancellation:
            orchestrator.run_context_phase(
                item,
                runtime=runtime,
                stream_event_id="context-stream",
            )

        unavailable.assert_called_once()
        self.assertEqual(
            unavailable.call_args.kwargs["publication_kind"],
            "lifecycle_cancellation",
        )
        cancellation.assert_not_called()

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

    def test_locked_merge_after_final_stops_before_projection_and_dispatch(self):
        item = _item("CONTEXT_READY", marker=True)
        runtime = SequenceRuntime(
            _snapshot(),
            _snapshot(state="closed", merged=True, locked=True),
        )
        final_review = {
            "review_generation_status": "complete",
            "review_publishable": True,
            "review_publication_safe": True,
            "review_fallback_used": False,
            "inline_comments": [],
            "review_model_phases": [],
        }
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
            orchestrator.result_artifact,
            "is_nonpublishable",
            return_value=False,
        ), patch.object(
            orchestrator,
            "_lifecycle_accounting",
            return_value={
                "deepseek_usage_accounting": {
                    "complete_numeric_usage": True,
                    "unreported_usage_call_count": 0,
                }
            },
        ), patch.object(
            orchestrator,
            "_mark_lifecycle_publication_unavailable",
            return_value=True,
        ) as unavailable, patch.object(
            orchestrator,
            "prepare_review_publication",
        ) as prepare, patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
        ) as commit:
            orchestrator.run_review_phase(
                item,
                runtime=runtime,
                deepseek_client=SimpleNamespace(
                    provider_call_records=lambda: []
                ),
                stream_event_id="review-stream",
            )

        unavailable.assert_called_once()
        self.assertEqual(
            unavailable.call_args.kwargs["publication_kind"],
            "post_merge_follow_up",
        )
        prepare.assert_not_called()
        commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

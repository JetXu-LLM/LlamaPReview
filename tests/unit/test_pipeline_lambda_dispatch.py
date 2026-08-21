import unittest
from unittest.mock import patch

from tests.unit.fakes import (
    FakeDynamoResource,
    build_stream_record,
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

from lambdas.LlamaPReviewPipeline import lambda_function
from lambdas.LlamaPReviewPipeline.errors import PhaseClaimUnavailable


class TestPipelineLambdaDispatch(unittest.TestCase):
    def test_dispatch_pending_to_context_phase(self):
        record = build_stream_record(
            repo="owner/repo",
            pr_number=1,
            new_status="PENDING",
            extra_new_image={
                "installation_id": {"N": "123"},
                "head_sha": {"S": "abcdef"},
            },
        )
        with patch("lambdas.LlamaPReviewPipeline.lambda_function.orchestrator.run_context_phase") as mocked:
            lambda_function.process_record(record)
        mocked.assert_called_once_with(
            lambda_function.from_dynamodb_image(
                record["dynamodb"]["NewImage"]
            ),
            lambda_context=None,
            stream_event_id="stream-event-1",
        )

    def test_dispatch_context_ready_to_review_phase(self):
        record = build_stream_record(repo="owner/repo", pr_number=1, new_status="CONTEXT_READY", old_status="PENDING")
        with patch("lambdas.LlamaPReviewPipeline.lambda_function.orchestrator.run_review_phase") as mocked:
            lambda_function.process_record(record)
        mocked.assert_called_once_with(
            lambda_function.from_dynamodb_image(
                record["dynamodb"]["NewImage"]
            ),
            lambda_context=None,
            stream_event_id="stream-event-1",
        )

    def test_lambda_context_is_forwarded_to_phase_deadline(self):
        record = build_stream_record(
            repo="owner/repo",
            pr_number=1,
            new_status="PENDING",
            extra_new_image={"installation_id": {"N": "123"}, "head_sha": {"S": "abcdef"}},
        )
        context = object()
        with patch("lambdas.LlamaPReviewPipeline.lambda_function.orchestrator.run_context_phase") as mocked:
            lambda_function.lambda_handler({"Records": [record]}, context)
        self.assertIs(mocked.call_args.kwargs["lambda_context"], context)

    def test_phase_exception_is_not_swallowed_so_stream_can_retry(self):
        record = build_stream_record(
            repo="owner/repo",
            pr_number=1,
            new_status="PENDING",
            extra_new_image={"installation_id": {"N": "123"}, "head_sha": {"S": "abcdef"}},
        )
        with patch(
            "lambdas.LlamaPReviewPipeline.lambda_function.orchestrator.run_context_phase",
            side_effect=RuntimeError("retry me"),
        ):
            with self.assertRaisesRegex(RuntimeError, "retry me"):
                lambda_function.lambda_handler({"Records": [record]}, None)

    def test_live_phase_record_without_event_id_fails_closed(self):
        phase_transitions = (
            ("PENDING", None),
            ("CONTEXT_READY", "PENDING"),
        )
        for status, old_status in phase_transitions:
            for missing in (None, ""):
                with self.subTest(status=status, event_id=missing):
                    record = build_stream_record(
                        repo="owner/repo",
                        pr_number=1,
                        new_status=status,
                        old_status=old_status,
                    )
                    if missing is None:
                        record.pop("eventID")
                    else:
                        record["eventID"] = missing
                    with self.assertRaises(PhaseClaimUnavailable):
                        lambda_function.process_record(record)

    def test_status_unchanged_skips(self):
        record = build_stream_record(repo="owner/repo", pr_number=1, new_status="PENDING", old_status="PENDING")
        with patch("lambdas.LlamaPReviewPipeline.lambda_function.orchestrator.run_context_phase") as mocked:
            lambda_function.process_record(record)
        mocked.assert_not_called()

    def test_capacity_sentinel_never_dispatches_pipeline_work(self):
        record = build_stream_record(
            repo="!llamapreview-capacity:2026-08-20",
            pr_number=-1,
            new_status="PENDING",
        )
        with patch(
            "lambdas.LlamaPReviewPipeline.lambda_function.orchestrator.run_context_phase"
        ) as context_phase, patch(
            "lambdas.LlamaPReviewPipeline.lambda_function.orchestrator.run_review_phase"
        ) as review_phase:
            lambda_function.process_record(record)

        context_phase.assert_not_called()
        review_phase.assert_not_called()


if __name__ == "__main__":
    unittest.main()

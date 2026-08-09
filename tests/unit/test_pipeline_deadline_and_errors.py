import unittest
import os
from types import SimpleNamespace
from unittest.mock import patch

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline.deadline import Deadline, DeadlineExceeded
from lambdas.LlamaPReviewPipeline import config
from lambdas.LlamaPReviewPipeline.errors import (
    HeadSuperseded,
    PRLifecycleSuperseded,
    TerminalPipelineFailure,
    classify_failure,
)


class _Clock:
    def __init__(self, value=100.0):
        self.value = float(value)

    def __call__(self):
        return self.value


class TestPipelineDeadline(unittest.TestCase):
    def test_lambda_remaining_time_caps_phase_and_preserves_reserve(self):
        clock = _Clock()
        context = SimpleNamespace(get_remaining_time_in_millis=lambda: 120_000)
        deadline = Deadline.from_lambda_context(
            context,
            phase_limit_seconds=300,
            reserve_seconds=30,
            clock=clock,
        )
        self.assertEqual(deadline.hard_remaining_seconds(), 120)
        self.assertEqual(deadline.remaining_seconds(), 90)
        self.assertEqual(deadline.timeout_for(460, stage="model"), 90)

    def test_phase_limit_caps_longer_lambda_remaining_time(self):
        clock = _Clock()
        context = SimpleNamespace(get_remaining_time_in_millis=lambda: 900_000)
        deadline = Deadline.from_lambda_context(
            context,
            phase_limit_seconds=240,
            reserve_seconds=30,
            clock=clock,
        )
        self.assertEqual(deadline.hard_remaining_seconds(), 240)
        self.assertEqual(deadline.remaining_seconds(), 210)

    def test_deadline_exception_is_not_socket_timeout_or_os_error(self):
        self.assertFalse(issubclass(DeadlineExceeded, TimeoutError))
        self.assertFalse(issubclass(DeadlineExceeded, OSError))

    def test_check_fails_before_state_write_reserve_is_consumed(self):
        clock = _Clock()
        deadline = Deadline.for_seconds(31, reserve_seconds=30, clock=clock)
        clock.value += 1
        with self.assertRaises(DeadlineExceeded) as raised:
            deadline.check("review.final_output")
        self.assertEqual(raised.exception.stage, "review.final_output")


class TestFailureClassification(unittest.TestCase):
    def test_wall_deadline_is_precisely_classified(self):
        classified = classify_failure(DeadlineExceeded("pfr.fetch"), stage="context")
        self.assertEqual(classified.kind, "wall_timeout")
        self.assertTrue(classified.retryable)
        self.assertEqual(classified.stage, "pfr.fetch")

    def test_superseded_is_terminal_without_message_matching(self):
        classified = classify_failure(HeadSuperseded("old", "new", stage="pre_publish"), stage="review")
        self.assertEqual(classified.kind, "head_superseded")
        self.assertFalse(classified.retryable)

        lifecycle = classify_failure(
            PRLifecycleSuperseded(
                "same",
                "same",
                current_state="closed",
                merged=True,
                stage="review.start",
            ),
            stage="review",
        )
        self.assertEqual(lifecycle.kind, "pr_lifecycle_superseded")
        self.assertEqual(lifecycle.stage, "review.start")
        self.assertFalse(lifecycle.retryable)

    def test_typed_terminal_failure_stays_terminal(self):
        classified = classify_failure(TerminalPipelineFailure("bad config", stage="bootstrap"), stage="context")
        self.assertFalse(classified.retryable)
        self.assertEqual(classified.stage, "bootstrap")

    def test_unknown_runtime_failure_is_bounded_retryable(self):
        classified = classify_failure(RuntimeError("sdk glitch"), stage="context")
        self.assertEqual(classified.kind, "unclassified_runtime_error")
        self.assertTrue(classified.retryable)

    def test_aws_throttling_code_is_retryable_without_message_matching(self):
        class AwsServiceError(Exception):
            response = {
                "Error": {"Code": "ThrottlingException", "Message": "opaque"},
                "ResponseMetadata": {"HTTPStatusCode": 400},
            }

        classified = classify_failure(AwsServiceError("opaque"), stage="persistence")

        self.assertEqual(classified.kind, "service_throttled")
        self.assertTrue(classified.retryable)
        self.assertEqual(classified.stage, "persistence")

    def test_github_rate_limit_403_is_retryable_but_plain_403_is_terminal(self):
        limited = SimpleNamespace(
            status=403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "123"},
        )
        plain = SimpleNamespace(status=403, headers={"X-RateLimit-Remaining": "42"})

        rate_limited = classify_failure(limited, stage="github")
        ordinary_forbidden = classify_failure(plain, stage="github")

        self.assertEqual(rate_limited.kind, "rate_limited")
        self.assertTrue(rate_limited.retryable)
        self.assertEqual(ordinary_forbidden.kind, "http_terminal")
        self.assertFalse(ordinary_forbidden.retryable)

    def test_nested_typed_http_status_is_classified_without_message_parsing(self):
        root = RuntimeError("outer message mentions 503 but is not a typed status")
        cause = RuntimeError("opaque")
        context = RuntimeError("opaque")
        response = SimpleNamespace(status=503)
        root.__cause__ = cause
        cause.__context__ = context
        context.reason = SimpleNamespace(response=response)

        classified = classify_failure(root, stage="review")
        message_only = classify_failure(RuntimeError("503"), stage="review")

        self.assertEqual(classified.kind, "http_transient")
        self.assertTrue(classified.retryable)
        self.assertEqual(message_only.kind, "unclassified_runtime_error")

    def test_numeric_typed_reason_status_is_classified_without_message_parsing(self):
        root = RuntimeError("opaque")
        root.reason = "503"

        classified = classify_failure(root, stage="review")

        self.assertEqual(classified.kind, "http_transient")
        self.assertTrue(classified.retryable)

    def test_nested_status_walk_is_cycle_safe_and_bounded(self):
        cyclic = RuntimeError("cyclic")
        cyclic.reason = cyclic
        self.assertEqual(
            classify_failure(cyclic, stage="context").kind,
            "unclassified_runtime_error",
        )

        root = RuntimeError("root")
        current = root
        for _ in range(9):
            nested = RuntimeError("nested")
            current.__cause__ = nested
            current = nested
        current.response = SimpleNamespace(status_code=503)

        self.assertEqual(
            classify_failure(root, stage="context").kind,
            "unclassified_runtime_error",
        )


class TestFailClosedConfigParsing(unittest.TestCase):
    def test_explicit_false_values_do_not_enable_dry_run_or_publish_flags(self):
        for value in ("0", "false", "no", "off"):
            with self.subTest(value=value), patch.dict(os.environ, {"FLAG": value}):
                self.assertFalse(config.env_bool("FLAG", True))

    def test_invalid_boolean_is_configuration_error(self):
        with patch.dict(os.environ, {"FLAG": "flase"}):
            with self.assertRaisesRegex(ValueError, "explicit boolean"):
                config.env_bool("FLAG")

if __name__ == "__main__":
    unittest.main()

import json
import unittest
from types import SimpleNamespace

from tests.unit.fakes import ensure_repo_root_on_path

ensure_repo_root_on_path()

from lambdas.LlamaPReviewPipeline.runtime_identity import (
    capture_runtime_identity,
)


class RuntimeIdentityTests(unittest.TestCase):
    def test_capture_is_bounded_serializable_and_allowlisted(self):
        identity = capture_runtime_identity(
            SimpleNamespace(aws_request_id="request-123"),
            phase="context",
            environ={
                "AWS_LAMBDA_FUNCTION_VERSION": "42",
                "AWS_LAMBDA_LOG_STREAM_NAME": "2026/07/30/[42]abcdef",
                "DEEPSEEK_API_KEY": "must-not-leak",
            },
        )

        self.assertEqual(
            identity,
            {
                "schema_version": 1,
                "phase": "context",
                "function_version": "42",
                "log_stream_name": "2026/07/30/[42]abcdef",
                "aws_request_id": "request-123",
            },
        )
        self.assertNotIn("must-not-leak", json.dumps(identity))

    def test_capture_omits_non_scalar_context_and_bounds_values(self):
        identity = capture_runtime_identity(
            SimpleNamespace(aws_request_id={"secret": "value"}),
            phase="review-extra-long-phase",
            environ={
                "AWS_LAMBDA_FUNCTION_VERSION": object(),
                "AWS_LAMBDA_LOG_STREAM_NAME": "x" * 700,
            },
        )

        self.assertEqual(identity["function_version"], "")
        self.assertEqual(identity["aws_request_id"], "")
        self.assertEqual(len(identity["phase"]), 16)
        self.assertEqual(len(identity["log_stream_name"]), 512)
        json.dumps(identity)


if __name__ == "__main__":
    unittest.main()

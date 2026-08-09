"""Adversarial recovery tests for the durable provider dispatch boundary."""

from __future__ import annotations

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
install_fake_requests_module()
install_fake_jwt_module()
fake_dynamo = FakeDynamoResource()
install_fake_aws_modules(fake_dynamo)

from lambdas.LlamaPReviewPipeline import (  # noqa: E402
    orchestrator,
    persistence,
    pipeline_accounting,
)
from lambdas.LlamaPReviewPipeline.deepseek_client import (  # noqa: E402
    DeepSeekClient,
    ProviderCallFenceError,
    ProviderCallLedgerError,
    ProviderDispatchOutcomeUnknown,
)


class _Response:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.headers = {}

    @staticmethod
    def json():
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        }


def _claim(owner: str, attempt: int) -> dict:
    return {
        "phase": "context",
        "owner_id": owner,
        "stream_event_id": "same-stream-event",
        "attempt": attempt,
    }


def _trace(attempt: int) -> dict:
    return {
        "run_id": "owner/repo#21@" + "a" * 40,
        "head_sha": "a" * 40,
        "pipeline_phase": "context",
        "pipeline_attempt": attempt,
    }


class TestProviderDispatchFence(unittest.TestCase):
    def setUp(self):
        self.table = fake_dynamo.Table("llamapreview-pipeline-test")
        self.table.reset()
        self.table.put_item(
            Item={
                "repo": "owner/repo",
                "pr_number": 21,
                "status": "PENDING",
                "run_id": _trace(1)["run_id"],
                "head_sha": _trace(1)["head_sha"],
                "context_attempt": 1,
                "context_claim": _claim("owner-1", 1),
            }
        )

    def _bound_client(self, *, owner: str) -> DeepSeekClient:
        client = DeepSeekClient(api_key="key")
        pipeline_accounting.bind_provider_call_accounting(
            client,
            repo="owner/repo",
            pr_number=21,
            expected_status="PENDING",
            phase_claim=_claim(
                owner,
                1 if owner == "owner-1" else 2,
            ),
            table=self.table,
        )
        return client

    def test_binding_rejects_terminal_only_provider_client(self):
        class TerminalOnlyClient:
            @staticmethod
            def set_provider_call_sink(_sink):
                return None

        with self.assertRaisesRegex(TypeError, "pre-dispatch fence"):
            pipeline_accounting.bind_provider_call_accounting(
                TerminalOnlyClient(),
                repo="owner/repo",
                pr_number=21,
                expected_status="PENDING",
                phase_claim=_claim("owner-1", 1),
                table=self.table,
            )

    def test_fence_binds_exact_event_attempt_run_and_head_before_http(self):
        cases = (
            (
                {**_claim("owner-1", 1), "stream_event_id": "wrong"},
                _trace(1),
            ),
            (_claim("owner-1", 2), _trace(1)),
            (
                _claim("owner-1", 1),
                {**_trace(1), "run_id": "wrong-run"},
            ),
            (
                _claim("owner-1", 1),
                {**_trace(1), "head_sha": "b" * 40},
            ),
        )
        for claim, trace in cases:
            with self.subTest(claim=claim, trace=trace):
                client = DeepSeekClient(api_key="key")
                pipeline_accounting.bind_provider_call_accounting(
                    client,
                    repo="owner/repo",
                    pr_number=21,
                    expected_status="PENDING",
                    phase_claim=claim,
                    table=self.table,
                )
                with patch(
                    "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post"
                ) as post:
                    with self.assertRaises(ProviderCallFenceError):
                        client.chat(
                            [{"role": "user", "content": "route"}],
                            max_retries=1,
                            trace_phase="route",
                            trace_metadata=trace,
                        )
                post.assert_not_called()

    def test_double_persistence_failure_leaves_fence_and_blocks_redelivery(self):
        first_client = self._bound_client(owner="owner-1")
        original_finalize = persistence.record_provider_call

        with patch.object(
            persistence,
            "record_provider_call",
            side_effect=RuntimeError("final sink unavailable"),
        ), patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(),
        ) as post:
            with self.assertRaises(ProviderCallLedgerError) as raised:
                first_client.chat(
                    [{"role": "user", "content": "route"}],
                    max_retries=1,
                    trace_phase="route",
                    trace_metadata=_trace(1),
                )

        self.assertEqual(post.call_count, 1)
        after_final_sink_failure = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 21}
        )["Item"]
        durable = persistence.provider_call_records(
            after_final_sink_failure
        )
        self.assertEqual(len(durable), 1)
        self.assertEqual(durable[0]["status"], "dispatching")
        self.assertEqual(
            durable[0]["call_id"],
            raised.exception.provider_call_record["call_id"],
        )

        captured_terminal_attrs = {}

        def terminal_write_fails(*_args, **kwargs):
            captured_terminal_attrs.update(kwargs.get("extra_attrs") or {})
            raise RuntimeError("terminal write unavailable")

        with patch.object(
            persistence,
            "mark_error",
            side_effect=terminal_write_fails,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "terminal write unavailable",
            ):
                orchestrator._handle_phase_failure(
                    repo="owner/repo",
                    pr_number=21,
                    expected_status="PENDING",
                    phase="context",
                    attempt=1,
                    exc=raised.exception,
                    phase_claim=_claim("owner-1", 1),
                    table=self.table,
                )

        # Durable-first accounting must retain the locally completed result for
        # the same call id instead of the incomplete dispatching skeleton.
        self.assertEqual(
            captured_terminal_attrs[
                "deepseek_all_attempt_model_phases"
            ][0]["status"],
            "completed",
        )

        # Simulate same-event lease takeover after the terminal DDB write also
        # failed.  Attempt 2 has a distinct operation/call id, but it must see
        # the unresolved attempt-1 fence before crossing into HTTP.
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 21}
        )["Item"]
        current["context_attempt"] = 2
        current["context_claim"] = _claim("owner-2", 2)
        stale_terminal = {
            **durable[0],
            "status": "completed",
            "finish_reason": "stop",
            "elapsed_seconds": 1,
            "last_attempt_elapsed_seconds": 1,
            "usage_state": "reported",
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        }
        self.assertFalse(
            persistence.record_provider_call(
                "owner/repo",
                21,
                expected_status="PENDING",
                record=stale_terminal,
                phase_claim=_claim("owner-1", 1),
                table=self.table,
            )
        )
        self.assertEqual(
            persistence.provider_call_records(current),
            durable,
        )
        second_client = self._bound_client(owner="owner-2")
        with patch.object(
            persistence,
            "record_provider_call",
            wraps=original_finalize,
        ), patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post"
        ) as second_post:
            with self.assertRaises(
                ProviderDispatchOutcomeUnknown
            ) as unknown:
                second_client.chat(
                    [{"role": "user", "content": "route"}],
                    max_retries=1,
                    trace_phase="route",
                    trace_metadata=_trace(2),
                )
        second_post.assert_not_called()
        self.assertEqual(
            unknown.exception.provider_call_record["call_id"],
            durable[0]["call_id"],
        )

        orchestrator._handle_phase_failure(
            repo="owner/repo",
            pr_number=21,
            expected_status="PENDING",
            phase="context",
            attempt=2,
            exc=unknown.exception,
            phase_claim=_claim("owner-2", 2),
            table=self.table,
        )
        terminal = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 21}
        )["Item"]
        self.assertEqual(terminal["status"], "ERROR")
        self.assertEqual(
            terminal["error_kind"],
            "provider_dispatch_outcome_unknown",
        )
        self.assertFalse(terminal["error_retryable"])
        terminal_calls = persistence.provider_call_records(terminal)
        self.assertEqual(terminal_calls[0]["status"], "dispatching")
        self.assertEqual(
            [
                (record["call_id"], record["status"])
                for record in terminal[
                    "deepseek_all_attempt_model_phases"
                ]
            ],
            [(terminal_calls[0]["call_id"], "dispatching")],
        )
        self.assertEqual(
            terminal["deepseek_discarded_model_phases"],
            terminal["deepseek_all_attempt_model_phases"],
        )
        self.assertEqual(terminal["deepseek_model_phases"], [])
        accounting = terminal["deepseek_usage_accounting"]
        self.assertEqual(accounting["all_call_count"], 1)
        self.assertEqual(accounting["discarded_call_count"], 1)
        self.assertEqual(accounting["unreported_usage_call_count"], 1)
        self.assertEqual(accounting["unresolved_dispatch_fence_count"], 1)
        self.assertFalse(accounting["complete_numeric_usage"])
        self.assertFalse(accounting["durable_call_ledger_complete"])
        self.assertEqual(
            terminal["provider_dispatch_fence_terminal"],
            {
                "schema_version": 1,
                "call_ids": [terminal_calls[0]["call_id"]],
                "outcome": "unknown",
                "second_dispatch_withheld": True,
            },
        )

    def test_bound_retry_finalizes_first_fence_before_second_fence(self):
        client = self._bound_client(owner="owner-1")
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            side_effect=[_Response(429), _Response(200)],
        ) as post, patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.time.sleep"
        ):
            client.chat(
                [{"role": "user", "content": "route"}],
                max_retries=2,
                trace_phase="route",
                trace_metadata=_trace(1),
            )

        self.assertEqual(post.call_count, 2)
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 21}
        )["Item"]
        records = persistence.provider_call_records(current)
        self.assertEqual(
            [record["status"] for record in records],
            ["http_retry", "completed"],
        )
        self.assertEqual(
            [record["transport_attempt_index"] for record in records],
            [1, 2],
        )
        self.assertFalse(
            any(record["status"] == "dispatching" for record in records)
        )

    def test_successful_bound_call_leaves_no_dispatching_fence(self):
        client = self._bound_client(owner="owner-1")
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(),
        ) as post:
            client.chat(
                [{"role": "user", "content": "route"}],
                max_retries=1,
                trace_phase="route",
                trace_metadata=_trace(1),
            )
        post.assert_called_once()
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 21}
        )["Item"]
        records = persistence.provider_call_records(current)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "completed")

    def test_crash_after_fence_before_http_blocks_every_later_http(self):
        first_client = self._bound_client(owner="owner-1")
        operation = first_client._begin_provider_operation(
            trace_phase="route",
            trace_metadata=_trace(1),
        )
        payload = first_client.build_payload(
            [{"role": "user", "content": "route"}]
        )
        payload.update(
            {
                "_llamapreview_logical_model": "deepseek-v4-pro",
                "_llamapreview_billed_model": "deepseek-v4-flash",
            }
        )
        fence = first_client._build_provider_dispatch_fence(
            payload=payload,
            operation=operation,
            transport_attempt_index=1,
        )
        first_client._persist_provider_dispatch_fence(fence)
        # Process loss occurs here: no transport function has been entered and
        # no terminal record exists.

        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 21}
        )["Item"]
        self.assertEqual(
            persistence.provider_call_records(current),
            [fence],
        )
        current["context_attempt"] = 2
        current["context_claim"] = _claim("owner-2", 2)
        second_client = self._bound_client(owner="owner-2")
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post"
        ) as post:
            with self.assertRaises(ProviderDispatchOutcomeUnknown):
                second_client.chat(
                    [{"role": "user", "content": "route"}],
                    max_retries=1,
                    trace_phase="route",
                    trace_metadata=_trace(2),
                )
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()

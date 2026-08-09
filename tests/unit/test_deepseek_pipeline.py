import gzip
import hashlib
import json
import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from tests.unit.fakes import FakeS3Client, ensure_repo_root_on_path, install_fake_requests_module, set_default_env

ensure_repo_root_on_path()
set_default_env()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline.deadline import (
    Deadline,
    DeadlineExceeded,
)
from lambdas.LlamaPReviewPipeline.errors import classify_failure
from lambdas.LlamaPReviewPipeline import deepseek_client as deepseek_client_module
from lambdas.LlamaPReviewPipeline.deepseek_client import (
    DeepSeekClient,
    DeepSeekHTTPError,
    DeepSeekTimeoutError,
    DeepSeekTransportError,
    PROVIDER_CALL_RECORD_KEY,
    ProviderCallFenceError,
    ProviderCallLedgerError,
    ProviderDispatchOutcomeUnknown,
)
from lambdas.LlamaPReviewPipeline.review.analyzer import analyze_pr_complexity
from lambdas.LlamaPReviewPipeline.review.judgment import (
    PresentationRepresentationError,
    ReviewOutputTruncated,
    failure_retryable,
)


class _Response:
    def __init__(self, status_code=200, payload=None, text="ok"):
        self.status_code = status_code
        self._payload = payload or {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 0,
                "total_tokens": 1,
            },
        }
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload


class _FakeChatClient:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        content = self.contents.pop(0)
        if isinstance(content, dict):
            return content
        return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {"total_tokens": 1}}


class TestDeepSeekPipeline(unittest.TestCase):
    def test_dispatch_fence_failure_blocks_http(self):
        client = DeepSeekClient(api_key="key")

        def reject(_record):
            raise RuntimeError("ddb unavailable")

        client.set_provider_dispatch_fence_sink(reject)
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post"
        ) as post:
            with self.assertRaises(ProviderCallFenceError) as raised:
                client.chat(
                    [{"role": "user", "content": "review"}],
                    max_retries=1,
                    trace_phase="deep_judgment",
                    trace_metadata={
                        "run_id": "fence-failure",
                        "head_sha": "a" * 40,
                        "pipeline_phase": "review",
                        "pipeline_attempt": 1,
                    },
                )

        post.assert_not_called()
        self.assertEqual(client.provider_call_records(), [])
        self.assertEqual(
            raised.exception.provider_call_record["status"],
            "dispatching",
        )
        classified = classify_failure(
            raised.exception,
            stage="review.deep_judgment",
        )
        self.assertEqual(
            classified.kind,
            "provider_dispatch_fence_unavailable",
        )
        self.assertTrue(classified.retryable)

    def test_retry_finalizes_each_fence_before_authorizing_next_http(self):
        client = DeepSeekClient(api_key="key")
        events = []
        client.set_provider_dispatch_fence_sink(
            lambda record: events.append(("fence", record))
        )
        client.set_provider_call_sink(
            lambda record: events.append(("terminal", record))
        )
        first = _Response(status_code=429)
        second = _Response()
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            side_effect=[first, second],
        ) as post, patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.time.sleep"
        ):
            client.chat(
                [{"role": "user", "content": "review"}],
                max_retries=2,
                trace_phase="deep_judgment",
                trace_metadata={
                    "run_id": "retry-fences",
                    "head_sha": "a" * 40,
                    "pipeline_phase": "review",
                    "pipeline_attempt": 1,
                },
            )

        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            [(kind, record["status"]) for kind, record in events],
            [
                ("fence", "dispatching"),
                ("terminal", "http_retry"),
                ("fence", "dispatching"),
                ("terminal", "completed"),
            ],
        )
        self.assertEqual(
            [record["transport_attempt_index"] for _, record in events],
            [1, 1, 2, 2],
        )
        self.assertNotEqual(
            events[0][1]["call_id"],
            events[2][1]["call_id"],
        )

    def test_unresolved_fence_after_pre_http_crash_blocks_later_http(self):
        first_client = DeepSeekClient(api_key="key")
        fences = []
        first_client.set_provider_dispatch_fence_sink(fences.append)
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            side_effect=KeyboardInterrupt("crash after fence"),
        ) as first_post:
            with self.assertRaises(KeyboardInterrupt):
                first_client.chat(
                    [{"role": "user", "content": "review"}],
                    max_retries=1,
                    trace_phase="deep_judgment",
                    trace_metadata={
                        "run_id": "crash-after-fence",
                        "head_sha": "a" * 40,
                        "pipeline_phase": "review",
                        "pipeline_attempt": 1,
                    },
                )
        self.assertEqual(first_post.call_count, 1)
        self.assertEqual(len(fences), 1)

        second_client = DeepSeekClient(api_key="key")

        def unresolved(_record):
            raise ProviderDispatchOutcomeUnknown(
                "prior outcome unknown",
                provider_call_record=fences[0],
            )

        second_client.set_provider_dispatch_fence_sink(unresolved)
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post"
        ) as second_post:
            with self.assertRaises(ProviderDispatchOutcomeUnknown) as raised:
                second_client.chat(
                    [{"role": "user", "content": "review"}],
                    max_retries=1,
                    trace_phase="deep_judgment",
                    trace_metadata={
                        "run_id": "crash-after-fence",
                        "head_sha": "a" * 40,
                        "pipeline_phase": "review",
                        "pipeline_attempt": 2,
                    },
                )
        second_post.assert_not_called()
        classified = classify_failure(
            raised.exception,
            stage="review.deep_judgment",
        )
        self.assertEqual(
            classified.kind,
            "provider_dispatch_outcome_unknown",
        )
        self.assertFalse(classified.retryable)

    def test_live_retryability_distinguishes_transport_from_contract_failure(self):
        self.assertTrue(
            failure_retryable(DeepSeekTimeoutError("content-free"))
        )
        self.assertTrue(
            failure_retryable(ReviewOutputTruncated("content-free"))
        )
        self.assertFalse(
            failure_retryable(
                PresentationRepresentationError("content-free")
            )
        )

    def test_payload_uses_v4_thinking_max_without_temperature(self):
        client = DeepSeekClient(api_key="key")
        payload = client.build_payload([{"role": "user", "content": "hi"}])
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)

    def test_transport_override_preserves_logical_pro_and_dispatches_flash(self):
        client = DeepSeekClient(api_key="key")
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(),
        ) as post:
            result = client.chat(
                [{"role": "user", "content": "review"}],
                model="deepseek-v4-pro",
                trace_phase="deep_judgment",
                trace_metadata={
                    "run_id": "run-identity-1",
                    "head_sha": "a" * 40,
                    "pipeline_phase": "review",
                    "pipeline_attempt": 2,
                },
            )

        self.assertEqual(post.call_args.kwargs["json"]["model"], "deepseek-v4-flash")
        record = result[PROVIDER_CALL_RECORD_KEY]
        self.assertEqual(record["model"], "deepseek-v4-pro")
        self.assertEqual(record["logical_model"], "deepseek-v4-pro")
        self.assertEqual(record["billed_model"], "deepseek-v4-flash")
        self.assertEqual(record["run_id"], "run-identity-1")
        self.assertEqual(record["head_sha"], "a" * 40)
        operation_identity = {
            "run_id": "run-identity-1",
            "head_sha": "a" * 40,
            "pipeline_phase": "review",
            "pipeline_attempt": 2,
            "phase": "deep_judgment",
            "call_index": 1,
        }
        self.assertEqual(
            record["operation_id"],
            hashlib.sha256(
                json.dumps(
                    operation_identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )

    def test_empty_transport_override_restores_logical_model_dispatch(self):
        client = DeepSeekClient(api_key="key", transport_model_override="")
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(),
        ) as post:
            result = client.chat(
                [{"role": "user", "content": "review"}],
                model="deepseek-v4-pro",
            )

        self.assertEqual(post.call_args.kwargs["json"]["model"], "deepseek-v4-pro")
        self.assertEqual(
            result[PROVIDER_CALL_RECORD_KEY]["billed_model"],
            "deepseek-v4-pro",
        )

    def test_unknown_or_ambiguous_transport_models_fail_before_http(self):
        for logical_model, override in (
            ("unknown", "deepseek-v4-flash"),
            ("", "deepseek-v4-flash"),
            ("deepseek-v4-pro", "unknown"),
            ("deepseek-v4-pro", "   "),
        ):
            with self.subTest(logical_model=logical_model, override=override):
                client = DeepSeekClient(
                    api_key="key",
                    transport_model_override=override,
                )
                with patch(
                    "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post"
                ) as post, self.assertRaises(ValueError):
                    client.chat(
                        [{"role": "user", "content": "review"}],
                        model=logical_model,
                    )
                post.assert_not_called()

    def test_payload_can_disable_thinking_for_bounded_non_final_calls(self):
        client = DeepSeekClient(api_key="key")

        payload = client.build_payload([{"role": "user", "content": "serialize"}], thinking=False)

        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", payload)

    def test_strict_beta_payload_supports_thinking_without_explicit_tool_choice(self):
        client = DeepSeekClient(api_key="key")
        tool = {
            "type": "function",
            "function": {
                "name": "strict_transport_probe",
                "description": "Submit the final judgment.",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
            },
        }

        payload = client.build_payload(
            [{"role": "user", "content": "review"}],
            tools=[tool],
            thinking=True,
            reasoning_effort="high",
            api_variant="beta",
        )

        self.assertEqual(payload["_llamapreview_api_variant"], "beta")
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertTrue(payload["tools"][0]["function"]["strict"])
        self.assertNotIn("tool_choice", payload)

    def test_tool_choice_is_transported_without_freezing_beta_compatibility_in_client(self):
        client = DeepSeekClient(api_key="key")
        named_choice = {
            "type": "function",
            "function": {"name": "strict_transport_probe"},
        }

        thinking_payload = client.build_payload(
            [{"role": "user", "content": "review"}],
            thinking=True,
            tool_choice=named_choice,
            api_variant="beta",
        )
        retry_payload = client.build_payload(
            [{"role": "user", "content": "repair the protocol"}],
            thinking=False,
            tool_choice=named_choice,
            api_variant="beta",
        )

        # The 2026-07-22 live beta probe rejected explicit tool_choice with
        # thinking, so Final deliberately omits it on the thinking turn. That
        # observed limitation is not frozen as a generic client-side API rule.
        self.assertEqual(thinking_payload["tool_choice"], named_choice)
        self.assertEqual(retry_payload["tool_choice"], named_choice)
        self.assertEqual(retry_payload["thinking"], {"type": "disabled"})

    def test_beta_variant_changes_endpoint_without_leaking_internal_marker(self):
        client = DeepSeekClient(api_key="key")
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(),
        ) as post:
            client.chat(
                [{"role": "user", "content": "review"}],
                thinking=False,
                api_variant="beta",
            )

        self.assertEqual(
            post.call_args.args[0],
            "https://api.deepseek.com/beta/chat/completions",
        )
        self.assertNotIn("_llamapreview_api_variant", post.call_args.kwargs["json"])

    def test_provider_boundary_records_stable_content_free_recursive_usage(self):
        response = _Response(
            payload={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"complexity":"low"}',
                            "reasoning_content": "must never enter accounting",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                    "prompt_cache_details": {
                        "hit_tokens": 5,
                        "miss_tokens": 6,
                        "provider_note": "drop me",
                        "estimated": False,
                    },
                    "invalid-negative": -1,
                },
            }
        )
        metadata = {
            "run_id": "run-7",
            "head_sha": "abc123",
            "pipeline_phase": "context",
            "pipeline_attempt": 2,
        }
        sink = []
        client = DeepSeekClient(api_key="key")
        client.set_provider_call_sink(sink.append)
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=response,
        ):
            result = client.chat(
                [{"role": "user", "content": "route"}],
                trace_phase="pr_analyzer",
                trace_metadata=metadata,
            )

        record = result[PROVIDER_CALL_RECORD_KEY]
        self.assertEqual(sink, [record])
        self.assertEqual(client.provider_call_records(), [record])
        self.assertRegex(record["call_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(record["phase"], "route")
        self.assertEqual(record["pipeline_phase"], "context")
        self.assertEqual(record["pipeline_attempt"], 2)
        self.assertEqual(record["call_index"], 1)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["model"], "deepseek-v4-pro")
        self.assertEqual(record["logical_model"], "deepseek-v4-pro")
        self.assertEqual(record["billed_model"], "deepseek-v4-flash")
        self.assertEqual(record["finish_reason"], "stop")
        self.assertEqual(record["transport_attempt_count"], 1)
        self.assertEqual(record["usage_state"], "reported")
        self.assertEqual(
            record["usage"],
            {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "prompt_cache_details": {
                    "hit_tokens": 5,
                    "miss_tokens": 6,
                },
            },
        )
        self.assertNotIn("messages", record)
        self.assertNotIn("choices", record)
        self.assertNotIn("reasoning_content", json.dumps(record))

        # The identity is deterministic across a fresh replay of the same
        # logical call; the client-local ordinal keeps later same-phase calls
        # distinct without including prompt or response content.
        replay_client = DeepSeekClient(api_key="key")
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=response,
        ):
            replay = replay_client.chat(
                [{"role": "user", "content": "different private content"}],
                trace_phase="pr_analyzer",
                trace_metadata=metadata,
            )
        self.assertEqual(
            replay[PROVIDER_CALL_RECORD_KEY]["call_id"],
            record["call_id"],
        )

    def test_provider_boundary_normalizes_decimal_usage_without_content(self):
        response = _Response(
            payload={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": Decimal("12"),
                    "completion_tokens": Decimal("3"),
                    "total_tokens": Decimal("15"),
                    "details": {
                        "cached_tokens": Decimal("2"),
                        "invalid": Decimal("NaN"),
                    },
                    "negative": Decimal("-1"),
                },
            }
        )
        client = DeepSeekClient(api_key="key")

        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=response,
        ):
            result = client.chat(
                [{"role": "user", "content": "route"}],
                trace_phase="route",
                trace_metadata={
                    "run_id": "run-decimal",
                    "head_sha": "abc123",
                    "pipeline_phase": "context",
                    "pipeline_attempt": 1,
                },
            )

        record = result[PROVIDER_CALL_RECORD_KEY]
        self.assertEqual(
            record["usage"],
            {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "total_tokens": 15,
                "details": {"cached_tokens": 2},
            },
        )
        self.assertEqual(record["usage_state"], "reported")

    def test_analyzer_uses_flash_high_thinking_json_without_temperature(self):
        client = _FakeChatClient(
            [
                json.dumps(
                    {
                        "reviewable_semantic_delta": True,
                        "minimum_evidence_boundary": "diff_only",
                        "complexity": "low",
                        "reason": "Contained",
                        "pr_type": "code",
                        "risk_domains": [],
                    }
                )
            ]
        )

        result = analyze_pr_complexity("small pr", client=client)

        self.assertEqual(result["complexity"], "low")
        call = client.calls[0]
        self.assertEqual(call["model"], "deepseek-v4-flash")
        self.assertEqual(call["reasoning_effort"], "high")
        self.assertTrue(call["thinking"])
        self.assertEqual(call["response_format"], {"type": "json_object"})
        self.assertNotIn("temperature", call)
        self.assertNotIn("top_p", call)

    def test_analyzer_route_commitments_have_one_admissible_mapping(self):
        valid = (
            (False, "none", "skip"),
            (True, "diff_only", "low"),
            (True, "diff_only", "high"),
            (True, "bounded_repo", "normal"),
            (True, "bounded_repo", "high"),
            (True, "systemic_repo", "high"),
        )
        for reviewable, boundary, complexity in valid:
            with self.subTest(valid=(reviewable, boundary, complexity)):
                client = _FakeChatClient(
                    [
                        json.dumps(
                            {
                                "reviewable_semantic_delta": reviewable,
                                "minimum_evidence_boundary": boundary,
                                "complexity": complexity,
                                "reason": "Evidence boundary is explicit.",
                                "pr_type": "mixed",
                                "risk_domains": [],
                            }
                        )
                    ]
                )
                result = analyze_pr_complexity("small pr", client=client)
                self.assertEqual(result["complexity"], complexity)
                self.assertEqual(result["reviewable_semantic_delta"], reviewable)
                self.assertEqual(result["minimum_evidence_boundary"], boundary)
                self.assertFalse(result["_route_plan_meta"]["parse_fallback"])
                self.assertNotIn("semantic_closure_version", result)
                self.assertNotIn("primary_review_obligation", result)
                self.assertEqual(
                    len(result["_route_plan_meta"]["route_input_sha256"]),
                    64,
                )

        invalid = (
            (True, "bounded_repo", "low"),
            (True, "diff_only", "normal"),
            (True, "systemic_repo", "normal"),
            (False, "none", "low"),
            (False, "diff_only", "skip"),
        )
        for reviewable, boundary, complexity in invalid:
            with self.subTest(invalid=(reviewable, boundary, complexity)):
                client = _FakeChatClient(
                    [
                        json.dumps(
                            {
                                "reviewable_semantic_delta": reviewable,
                                "minimum_evidence_boundary": boundary,
                                "complexity": complexity,
                                "reason": "Contradictory route.",
                                "pr_type": "mixed",
                                "risk_domains": [],
                            }
                        )
                    ]
                )
                result = analyze_pr_complexity("small pr", client=client)
                self.assertEqual(result["complexity"], "high")
                self.assertEqual(
                    result["_route_plan_meta"]["contract_failure_kind"],
                    "route_commitment_inconsistent",
                )
                self.assertFalse(result["_route_plan_meta"]["continuation_available"])

    def test_analyzer_rejects_missing_or_loosely_typed_route_commitments(self):
        malformed = (
            {
                "minimum_evidence_boundary": "diff_only",
                "complexity": "low",
            },
            {
                "reviewable_semantic_delta": "true",
                "minimum_evidence_boundary": "diff_only",
                "complexity": "low",
            },
            {
                "reviewable_semantic_delta": True,
                "minimum_evidence_boundary": "diff_only",
                "complexity": "medium",
            },
        )
        for overrides in malformed:
            with self.subTest(overrides=overrides):
                payload = {
                    "reviewable_semantic_delta": True,
                    "minimum_evidence_boundary": "diff_only",
                    "complexity": "low",
                    "reason": "Route.",
                    "pr_type": "code",
                    "risk_domains": [],
                    **overrides,
                }
                if "reviewable_semantic_delta" not in overrides:
                    payload.pop("reviewable_semantic_delta")
                result = analyze_pr_complexity(
                    "small pr",
                    client=_FakeChatClient([json.dumps(payload)]),
                )
                self.assertEqual(result["complexity"], "high")
                self.assertEqual(
                    result["_route_plan_meta"]["contract_failure_kind"],
                    "route_commitment_invalid",
                )
                self.assertNotIn("primary_review_obligation", result)
                self.assertNotIn("semantic_closure_version", result)

    def test_analyzer_non_object_json_fails_safe(self):
        for content in ("[]", '"route"'):
            with self.subTest(content=content):
                client = _FakeChatClient([content])

                result = analyze_pr_complexity("small pr", client=client)

                self.assertEqual(len(client.calls), 1)
                self.assertEqual(result["complexity"], "high")
                self.assertEqual(result["pr_type"], "mixed")
                self.assertTrue(result["_route_plan_meta"]["parse_fallback"])
                self.assertEqual(
                    result["_route_plan_meta"]["contract_failure_kind"],
                    "json_root_type_invalid",
                )

    def test_analyzer_truncation_fails_safe(self):
        client = _FakeChatClient(
            [
                {
                    "choices": [
                        {
                            "message": {"content": '{"complexity":"low"'},
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"total_tokens": 9},
                }
            ]
        )

        result = analyze_pr_complexity("small pr", client=client)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["complexity"], "high")
        self.assertEqual(
            result["_route_plan_meta"]["contract_failure_kind"],
            "output_truncated",
        )

    def test_analyzer_wrong_role_fails_safe_without_silent_success(self):
        response = {
            "choices": [
                {
                    "message": {
                        "role": "user",
                        "content": '{"complexity":"skip","reason":"wrong role"}',
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 1},
        }

        result = analyze_pr_complexity(
            "small pr", client=_FakeChatClient([response])
        )

        self.assertEqual(result["complexity"], "high")
        self.assertEqual(
            result["_route_plan_meta"]["contract_failure_kind"],
            "model_response_invalid",
        )

    def test_deepseek_retry_on_5xx(self):
        client = DeepSeekClient(api_key="key")
        responses = [_Response(status_code=500, text="server"), _Response(status_code=200)]
        with patch("lambdas.LlamaPReviewPipeline.deepseek_client.requests.post", side_effect=responses) as post, patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.time.sleep"
        ):
            result = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(post.call_count, 2)
        records = client.provider_call_records()
        self.assertEqual(len(records), 2)
        self.assertEqual(
            [record["transport_attempt_index"] for record in records],
            [1, 2],
        )
        self.assertEqual(
            [record["status"] for record in records],
            ["http_retry", "completed"],
        )
        self.assertEqual(
            len({record["operation_id"] for record in records}),
            1,
        )
        self.assertEqual(records[0]["usage_state"], "unreported")
        self.assertEqual(records[1]["usage_state"], "reported")

    def test_requests_timeout_maps_to_typed_transport_failure_and_ledger(self):
        timeout = deepseek_client_module.requests.Timeout("socket timed out")
        client = DeepSeekClient(api_key="key")
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            side_effect=timeout,
        ) as post, patch("lambdas.LlamaPReviewPipeline.deepseek_client.time.sleep"):
            with self.assertRaises(DeepSeekTimeoutError):
                client.chat(
                    [{"role": "user", "content": "review"}],
                    trace_phase="deep_judgment",
                )

        self.assertEqual(post.call_count, 3)
        records = client.provider_call_records()
        self.assertEqual(len(records), 3)
        self.assertEqual(
            [record["status"] for record in records],
            ["transport_error", "transport_error", "transport_error"],
        )
        self.assertTrue(
            all(record["usage_state"] == "unreported" for record in records)
        )

    def test_external_wall_timeout_after_dispatch_is_recorded(self):
        class ExternalWallTimeout(Exception):
            pass

        class RequestsSurface:
            class Timeout(Exception):
                pass

            class RequestException(Exception):
                pass

            @staticmethod
            def post(*_args, **_kwargs):
                raise ExternalWallTimeout("caller timer fired")

        client = DeepSeekClient(api_key="key")
        with patch.object(
            deepseek_client_module,
            "requests",
            RequestsSurface,
        ):
            with self.assertRaises(ExternalWallTimeout):
                client.chat(
                    [{"role": "user", "content": "review"}],
                    max_retries=1,
                    trace_phase="deep_judgment",
                    trace_metadata={
                        "run_id": "run-7",
                        "head_sha": "a" * 40,
                        "pipeline_phase": "review",
                        "pipeline_attempt": 1,
                    },
                )

        records = client.provider_call_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "transport_error")
        self.assertEqual(records[0]["usage_state"], "unreported")
        self.assertEqual(records[0]["transport_attempt_count"], 1)
        self.assertEqual(
            records[0]["error_class"],
            "ExternalWallTimeout",
        )

    def test_request_exception_maps_to_typed_transport_failure_and_ledger(self):
        transport_error = deepseek_client_module.requests.RequestException("connection reset")
        client = DeepSeekClient(api_key="key")
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            side_effect=transport_error,
        ) as post, patch("lambdas.LlamaPReviewPipeline.deepseek_client.time.sleep"):
            with self.assertRaises(DeepSeekTransportError):
                client.chat(
                    [{"role": "user", "content": "review"}],
                    trace_phase="deep_judgment",
                )

        self.assertEqual(post.call_count, 3)
        records = client.provider_call_records()
        self.assertEqual(len(records), 3)
        self.assertEqual(
            [record["status"] for record in records],
            ["transport_error", "transport_error", "transport_error"],
        )
        self.assertTrue(
            all(record["usage_state"] == "unreported" for record in records)
        )

    def test_backoff_deadline_keeps_the_dispatched_transport_record(self):
        transport_error = deepseek_client_module.requests.RequestException(
            "connection reset"
        )
        client = DeepSeekClient(api_key="key")
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            side_effect=transport_error,
        ), patch.object(
            client,
            "_bounded_backoff",
            side_effect=DeadlineExceeded(
                "deepseek.deep_judgment.backoff",
                remaining_seconds=0,
            ),
        ):
            with self.assertRaises(DeadlineExceeded):
                client.chat(
                    [{"role": "user", "content": "review"}],
                    trace_phase="deep_judgment",
                )

        records = client.provider_call_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "transport_error")
        self.assertEqual(records[0]["transport_attempt_index"], 1)
        self.assertEqual(records[0]["usage_state"], "unreported")

    def test_client_http_error_never_exposes_raw_response_body(self):
        client = DeepSeekClient(api_key="key")
        secret_body = "provider-secret-payload"
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(status_code=400, text=secret_body),
        ):
            with self.assertRaises(DeepSeekHTTPError) as raised:
                client.chat(
                    [{"role": "user", "content": "review"}],
                    max_retries=1,
                )

        self.assertNotIn(secret_body, str(raised.exception))
        self.assertEqual(
            client.provider_call_records()[0]["error_class"],
            "DeepSeekHTTPError",
        )

    def test_retry_exhaustion_never_exposes_raw_response_body(self):
        client = DeepSeekClient(api_key="key")
        secret_body = "provider-secret-retry-payload"

        class _RequestException(Exception):
            pass

        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(status_code=500, text=secret_body),
        ), patch.object(
            deepseek_client_module.requests,
            "RequestException",
            _RequestException,
        ), patch("lambdas.LlamaPReviewPipeline.deepseek_client.time.sleep"):
            with self.assertRaises(DeepSeekHTTPError) as raised:
                client.chat(
                    [{"role": "user", "content": "review"}],
                    max_retries=1,
                )

        self.assertNotIn(secret_body, str(raised.exception))
        record = client.provider_call_records()[0]
        self.assertEqual(record["status"], "http_error")
        self.assertEqual(record["usage_state"], "unreported")
        self.assertEqual(record["error_class"], "DeepSeekHTTPError")

    def test_provider_ledger_sink_failure_is_not_swallowed(self):
        client = DeepSeekClient(api_key="key")

        def reject(_record):
            raise RuntimeError("durable ledger unavailable")

        client.set_provider_call_sink(reject)
        with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
            os.environ,
            {
                "DEEPSEEK_TRACE_MODE": "summary",
                "DEEPSEEK_TRACE_DIR": trace_dir,
            },
        ), patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(),
        ):
            with self.assertRaises(ProviderCallLedgerError) as raised:
                client.chat(
                    [{"role": "user", "content": "review"}],
                    max_retries=1,
                    trace_phase="route",
                    trace_metadata={"run_id": "ledger-failure"},
                )

            with gzip.open(
                os.path.join(trace_dir, "ledger-failure.jsonl.gz"),
                "rt",
                encoding="utf-8",
            ) as handle:
                trace = json.loads(handle.readline())

        records = client.provider_call_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(raised.exception.provider_call_record, records[0])
        self.assertEqual(raised.exception.telemetry, records[0])
        self.assertEqual(trace["summary"]["logical_model"], "deepseek-v4-pro")
        self.assertEqual(trace["summary"]["billed_model"], "deepseek-v4-flash")
        self.assertEqual(trace["summary"]["call_id"], records[0]["call_id"])
        self.assertEqual(
            trace["summary"]["operation_id"],
            records[0]["operation_id"],
        )
        self.assertEqual(
            trace["summary"]["transport_attempt_index"],
            records[0]["transport_attempt_index"],
        )
        for field in (
            "pipeline_phase",
            "pipeline_attempt",
            "phase",
            "call_index",
        ):
            self.assertEqual(trace["summary"][field], records[0][field])
        self.assertNotIn("request", trace)
        self.assertNotIn("response", trace)
        for stage in (
            "context.route",
            "context.pfr_plan",
            "context.pfr_reconcile",
            "review.deep_judgment",
        ):
            classified = classify_failure(raised.exception, stage=stage)
            self.assertEqual(classified.kind, "provider_call_ledger_error")
            self.assertFalse(classified.retryable)

    def test_optional_local_trace_failure_never_replays_paid_dispatch(self):
        sink = []
        client = DeepSeekClient(api_key="key")
        client.set_provider_call_sink(sink.append)
        with patch.dict(
            os.environ,
            {"DEEPSEEK_TRACE_MODE": "summary"},
        ), patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(),
        ) as post, patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client._write_local_trace",
            side_effect=OSError("trace path unavailable"),
        ):
            result = client.chat(
                [{"role": "user", "content": "review"}],
                max_retries=3,
                trace_phase="route",
            )

        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(sink), 1)
        self.assertEqual(
            result[PROVIDER_CALL_RECORD_KEY]["call_id"],
            sink[0]["call_id"],
        )

    def test_retry_attempts_share_one_operation_timeout_budget(self):
        client = DeepSeekClient(api_key="key")
        now = [100.0]
        observed_timeouts = []

        def post(*_args, **kwargs):
            observed_timeouts.append(kwargs["timeout"])
            if len(observed_timeouts) == 1:
                now[0] += 7
                return _Response(status_code=500, text="server")
            return _Response(status_code=200)

        def sleep(seconds):
            now[0] += seconds

        with patch("lambdas.LlamaPReviewPipeline.deepseek_client.time.monotonic", side_effect=lambda: now[0]), patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.time.sleep", side_effect=sleep
        ), patch("lambdas.LlamaPReviewPipeline.deepseek_client.requests.post", side_effect=post):
            result = client.chat([{"role": "user", "content": "hi"}], timeout_seconds=10)

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(len(observed_timeouts), 2)
        self.assertEqual(observed_timeouts[0], 10)
        self.assertLessEqual(observed_timeouts[1], 2)

    def test_retry_success_trace_separates_total_operation_from_last_attempt_latency(self):
        client = DeepSeekClient(api_key="key")
        now = [100.0]
        attempt = [0]

        def post(*_args, **_kwargs):
            attempt[0] += 1
            if attempt[0] == 1:
                now[0] += 2.0
                return _Response(status_code=500, text="server")
            now[0] += 0.25
            return _Response(status_code=200)

        def sleep(seconds):
            now[0] += seconds

        with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
            os.environ,
            {
                "DEEPSEEK_TRACE_MODE": "summary",
                "DEEPSEEK_TRACE_DIR": trace_dir,
            },
        ), patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.time.monotonic",
            side_effect=lambda: now[0],
        ), patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.time.sleep",
            side_effect=sleep,
        ), patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            side_effect=post,
        ):
            client.chat(
                [{"role": "user", "content": "hi"}],
                timeout_seconds=10,
                trace_phase="deep_judgment",
                trace_metadata={"run_id": "retry-latency"},
            )
            with gzip.open(
                os.path.join(trace_dir, "retry-latency.jsonl.gz"),
                "rt",
                encoding="utf-8",
            ) as handle:
                event = json.loads(handle.readline())

        self.assertEqual(event["summary"]["attempt_count"], 2)
        self.assertEqual(event["summary"]["elapsed_seconds"], 3.25)
        self.assertEqual(event["summary"]["last_attempt_elapsed_seconds"], 0.25)

    def test_full_trace_writes_redacted_gzip_jsonl(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "final",
                        "reasoning_content": "thinking deeply",
                        "tool_calls": [{"function": {"name": "finish_context"}}],
                    }
                }
            ],
            "usage": {"total_tokens": 12},
        }
        with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
            os.environ,
            {
                "DEEPSEEK_TRACE_MODE": "full",
                "DEEPSEEK_TRACE_DIR": trace_dir,
            },
        ), patch("lambdas.LlamaPReviewPipeline.deepseek_client.requests.post", return_value=_Response(payload=payload)):
            DeepSeekClient(api_key="secret-key").chat(
                [{"role": "user", "content": "hi"}],
                trace_phase="deep_judgment",
                trace_metadata={"run_id": "trace-test", "api_key": "should-not-log"},
            )

            files = os.listdir(trace_dir)
            self.assertEqual(files, ["trace-test.jsonl.gz"])
            with gzip.open(os.path.join(trace_dir, files[0]), "rt", encoding="utf-8") as handle:
                event = json.loads(handle.readline())

        serialized = json.dumps(event)
        self.assertEqual(event["mode"], "full")
        self.assertEqual(event["phase"], "deep_judgment")
        self.assertEqual(event["summary"]["model"], "deepseek-v4-pro")
        self.assertEqual(event["summary"]["logical_model"], "deepseek-v4-pro")
        self.assertEqual(event["summary"]["billed_model"], "deepseek-v4-flash")
        self.assertEqual(event["request"]["model"], "deepseek-v4-flash")
        self.assertEqual(event["request"]["messages"][0]["content"], "hi")
        self.assertEqual(event["response"]["choices"][0]["message"]["reasoning_content"], "thinking deeply")
        self.assertEqual(event["summary"]["tool_call_count"], 1)
        self.assertNotIn("secret-key", serialized)
        self.assertNotIn("should-not-log", serialized)

    def test_trace_off_writes_no_local_file(self):
        with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
            os.environ,
            {
                "DEEPSEEK_TRACE_MODE": "off",
                "DEEPSEEK_TRACE_DIR": trace_dir,
            },
        ), patch("lambdas.LlamaPReviewPipeline.deepseek_client.requests.post", return_value=_Response()):
            DeepSeekClient(api_key="key").chat([{"role": "user", "content": "hi"}])
            self.assertEqual(os.listdir(trace_dir), [])

    def test_summary_trace_excludes_full_request_and_response(self):
        with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
            os.environ,
            {
                "DEEPSEEK_TRACE_MODE": "summary",
                "DEEPSEEK_TRACE_DIR": trace_dir,
            },
        ), patch("lambdas.LlamaPReviewPipeline.deepseek_client.requests.post", return_value=_Response()):
            DeepSeekClient(api_key="key").chat(
                [{"role": "user", "content": "hi"}],
                trace_phase="final_presentation",
                trace_metadata={"run_id": "summary-test"},
            )

            with gzip.open(os.path.join(trace_dir, "summary-test.jsonl.gz"), "rt", encoding="utf-8") as handle:
                event = json.loads(handle.readline())

        self.assertEqual(event["mode"], "summary")
        self.assertEqual(event["phase"], "final_presentation")
        self.assertIn("summary", event)
        self.assertNotIn("request", event)
        self.assertNotIn("response", event)

    def test_summary_trace_records_only_safe_strict_protocol_shape(self):
        tool = {
            "type": "function",
            "function": {
                "name": "strict_transport_probe",
                "description": "private dynamic capability",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "enum": ["private/file.py"]}
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }
        with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
            os.environ,
            {
                "DEEPSEEK_TRACE_MODE": "summary",
                "DEEPSEEK_TRACE_DIR": trace_dir,
            },
        ), patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(),
        ):
            DeepSeekClient(api_key="key").chat(
                [{"role": "user", "content": "review"}],
                tools=[tool],
                thinking=True,
                reasoning_effort="high",
                api_variant="beta",
                trace_phase="final_presentation",
                trace_metadata={"run_id": "strict-summary"},
            )
            with gzip.open(
                os.path.join(trace_dir, "strict-summary.jsonl.gz"),
                "rt",
                encoding="utf-8",
            ) as handle:
                event = json.loads(handle.readline())

        summary = event["summary"]
        self.assertEqual(summary["api_variant"], "beta")
        self.assertEqual(summary["strict_tool_count"], 1)
        self.assertIsNone(summary["tool_choice_kind"])
        serialized = json.dumps(event)
        self.assertNotIn("private/file.py", serialized)
        self.assertNotIn("private dynamic capability", serialized)

    def test_cloudwatch_receives_summary_only_even_when_full_trace_is_enabled(self):
        big_text = "x" * 5000
        payload = {"choices": [{"message": {"content": big_text, "reasoning_content": big_text}}], "usage": {"total_tokens": 1}}
        with patch.dict(os.environ, {"DEEPSEEK_TRACE_MODE": "full", "DEEPSEEK_TRACE_CHUNK_CHARS": "1000", "DEEPSEEK_TRACE_DIR": ""}), patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(payload=payload),
        ), patch("lambdas.LlamaPReviewPipeline.deepseek_client.logger.info") as log_info:
            DeepSeekClient(api_key="key").chat(
                [{"role": "user", "content": big_text}],
                trace_phase="pfr_reconcile",
                trace_metadata={"run_id": "chunk-test"},
            )

        trace_logs = [call for call in log_info.call_args_list if call.args and call.args[0] == "DeepSeek trace summary: %s"]
        self.assertEqual(len(trace_logs), 1)
        cloudwatch_event = json.loads(trace_logs[0].args[1])
        self.assertEqual(cloudwatch_event["phase"], "pfr_reconcile")
        self.assertNotIn("request", cloudwatch_event)
        self.assertNotIn("response", cloudwatch_event)
        self.assertNotIn(big_text, trace_logs[0].args[1])

    def test_full_trace_is_written_to_encrypted_s3_object(self):
        fake_s3 = FakeS3Client()
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_TRACE_MODE": "full",
                "DEEPSEEK_TRACE_DIR": "",
                "DEEPSEEK_TRACE_S3_BUCKET": "trace-bucket",
            },
        ), patch("lambdas.LlamaPReviewPipeline.deepseek_client._get_s3_client", return_value=fake_s3), patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(),
        ):
            DeepSeekClient(api_key="key").chat(
                [{"role": "user", "content": "private repository context"}],
                trace_phase="final_presentation",
                trace_metadata={
                    "repo": "owner/repo",
                    "pr_number": 7,
                    "head_sha": "abc",
                    "run_id": "delivery-1",
                },
            )

        self.assertEqual(len(fake_s3.put_calls), 1)
        call = fake_s3.put_calls[0]
        self.assertEqual(call["Bucket"], "trace-bucket")
        self.assertEqual(call["ServerSideEncryption"], "AES256")
        self.assertIn("deepseek-traces", call["Key"])
        event = json.loads(gzip.decompress(call["Body"]).decode("utf-8"))
        self.assertEqual(event["request"]["messages"][0]["content"], "private repository context")

    def test_request_timeout_is_clamped_to_absolute_deadline(self):
        deadline = Deadline.for_seconds(2, reserve_seconds=1)
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_Response(),
        ) as post:
            DeepSeekClient(api_key="key").chat(
                [{"role": "user", "content": "hi"}],
                timeout_seconds=460,
                deadline=deadline,
            )
        self.assertGreater(post.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(post.call_args.kwargs["timeout"], 1)

    def test_full_trace_redacts_secret_like_content_inside_messages(self):
        secret_text = (
            "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY----- "
            "Bearer sk-live-secret-token-value github_pat_abcdefghijklmnopqrstuvwxyz"
        )
        with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
            os.environ,
            {
                "DEEPSEEK_TRACE_MODE": "full",
                "DEEPSEEK_TRACE_DIR": trace_dir,
            },
        ), patch("lambdas.LlamaPReviewPipeline.deepseek_client.requests.post", return_value=_Response()):
            DeepSeekClient(api_key="key").chat(
                [{"role": "user", "content": secret_text}],
                trace_metadata={"run_id": "redact-test"},
            )

            with gzip.open(os.path.join(trace_dir, "redact-test.jsonl.gz"), "rt", encoding="utf-8") as handle:
                event = json.loads(handle.readline())

        serialized = json.dumps(event)
        self.assertNotIn("BEGIN PRIVATE KEY", serialized)
        self.assertNotIn("sk-live-secret-token-value", serialized)
        self.assertNotIn("github_pat_abcdefghijklmnopqrstuvwxyz", serialized)
        self.assertIn("[REDACTED_SECRET]", serialized)


if __name__ == "__main__":
    unittest.main()

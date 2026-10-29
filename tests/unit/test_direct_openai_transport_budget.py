"""Synthetic HTTP and atomic-store adversarial tests; no external requests."""

from __future__ import annotations

from copy import deepcopy
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from tests.unit.fakes import (
    FakeTable,
    FakeDynamoResource,
    ensure_repo_root_on_path,
    set_default_env,
    install_fake_aws_modules,
    install_fake_requests_module,
    install_fake_jwt_module,
)

ensure_repo_root_on_path()
set_default_env()
install_fake_requests_module()
install_fake_jwt_module()
install_fake_aws_modules(FakeDynamoResource())
from lambdas.LlamaPReviewPipeline import (
    deepseek_client as transport,
    persistence,
    pipeline_accounting,
)
from lambdas.LlamaPReviewPipeline.direct_openai import build_request, normalize_response
from lambdas.LlamaPReviewPipeline.direct_openai_budget import (
    BudgetUnavailable,
    PRICING_VERSION,
    actual_cost,
    load_policy,
    reservation,
)

from pathlib import Path
from lambdas.LlamaPReviewPipeline.provider_model_routing import parse_direct_openai_profile

PROFILE_JSON = (
    Path(__file__).resolve().parents[2] / "config/direct_openai_profile.example.json"
).read_text()
PROFILE_CONFIG = parse_direct_openai_profile(PROFILE_JSON)
PROFILE = PROFILE_CONFIG.sha256


def policy(**changes):
    now = int(time.time())
    return {
        "cycle_id": "synthetic-grant-1",
        "valid_from": now - 1000,
        "expires_at": now + 86400,
        "allocation_nanousd": 150_000_000_000,
        "verified_available_nanousd": 180_000_000_000,
        "organization_id": "org-synthetic",
        "project_id": "proj-synthetic",
        "profile_sha256": PROFILE,
        "pricing_version": PRICING_VERSION,
        "pricing_verified_until": now + 86400,
        "authorization": "approved",
        **changes,
    }


def response(**changes):
    return {
        "id": "resp_synthetic",
        "model": "gpt-6-luna",
        "service_tier": "default",
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": '{"ok":true}'}],
            },
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 17},
        },
        **changes,
    }


class Response:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self.headers = {}
        self.body = response() if body is None else body
        self.content = json.dumps(self.body).encode()

    def json(self):
        return deepcopy(self.body)


class AtomicStore:
    """Single-lock fake enforces BOTH CAS conditions before applying writes."""

    def __init__(self, table):
        self.table = table
        self.lock = threading.Lock()
        self.calls = []

    def __call__(self, table, updates):
        assert table is self.table
        with self.lock:
            snapshot = deepcopy(table.items)
            self.calls.append(deepcopy(updates))
            try:
                first, counter = updates
                key = tuple(sorted(counter["Key"].items()))
                item = table.items.get(key, {})
                vals = counter["ExpressionAttributeValues"]
                policy_hash = item.get("policy_sha256")
                reserve = vals[":reserve"]
                if ":headroom" in vals:
                    if (
                        policy_hash not in (None, vals[":policy"])
                        or item.get("charged_nanousd", 0) > vals[":headroom"]
                    ):
                        raise persistence.ConditionalWriteLost("budget exhausted or changed")
                    next_counter = {
                        **counter["Key"],
                        **item,
                        "policy_sha256": vals[":policy"],
                        "cycle_id": vals[":cycle"],
                        "allocation_nanousd": vals[":cap"],
                        "expires_at": vals[":expires"],
                        "charged_nanousd": item.get("charged_nanousd", 0) + reserve,
                        "reserved_nanousd": item.get("reserved_nanousd", 0) + reserve,
                    }
                else:
                    if (
                        policy_hash != vals[":policy"]
                        or item.get("reserved_nanousd", 0) < reserve
                        or item.get("charged_nanousd", 0) < reserve
                    ):
                        raise persistence.ConditionalWriteLost("invalid settlement")
                    next_counter = {
                        **item,
                        "charged_nanousd": item["charged_nanousd"] + vals[":delta"],
                        "reserved_nanousd": item["reserved_nanousd"] + vals[":release"],
                        "settled_nanousd": item.get("settled_nanousd", 0) + vals[":cost"],
                    }
                table.update_item(**first)
                table.items[key] = next_counter
            except Exception:
                table.items = snapshot
                raise


class DirectOpenAITransportBudgetTests(unittest.TestCase):
    def setUp(self):
        self.policy = policy()
        self.env = patch.dict(
            os.environ,
            {
                "DIRECT_OPENAI_BUDGET": json.dumps(self.policy),
                "DEEPSEEK_TRACE_MODE": "off",
                "DIRECT_OPENAI_PROFILE": PROFILE_JSON,
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.table = FakeTable("synthetic")
        self.atomic = AtomicStore(self.table)
        self.tx = patch.object(persistence, "_provider_transact_write", side_effect=self.atomic)
        self.tx.start()
        self.addCleanup(self.tx.stop)
        self.claim = {
            "phase": "context",
            "owner_id": "worker",
            "stream_event_id": "event",
            "attempt": 1,
        }
        self.metadata = {
            "run_id": "run",
            "head_sha": "b" * 40,
            "pipeline_phase": "context",
            "pipeline_attempt": 1,
            "profile_version": PROFILE_CONFIG.version,
            "profile_sha256": PROFILE,
            "stage": "route",
            "tier": "",
        }
        self.table.put_item(
            Item={
                "repo": "owner/public",
                "pr_number": 1,
                "status": "CONTEXT_RUNNING",
                "run_id": "run",
                "head_sha": "b" * 40,
                "context_attempt": 1,
                "context_claim": self.claim,
                "ttl_epoch": int(time.time()) + 86400,
            }
        )

    def client(self, *, number=1, bound=True):
        client = transport.DeepSeekClient(
            api_key="synthetic",
            provider="direct_openai",
            model="gpt-6-luna",
            reasoning_effort="max",
            transport_model_override="",
        )
        if bound:
            pipeline_accounting.bind_provider_call_accounting(
                client,
                repo="owner/public",
                pr_number=number,
                expected_status="CONTEXT_RUNNING",
                phase_claim=self.claim,
                table=self.table,
            )
        return client

    def chat(self, client=None, **kwargs):
        return (client or self.client()).chat(
            [{"role": "user", "content": "JSON synthetic"}],
            max_tokens=100,
            max_retries=kwargs.pop("max_retries", 1),
            trace_phase="route",
            trace_metadata=kwargs.pop("trace_metadata", self.metadata),
            **kwargs,
        )

    def records(self):
        return persistence.provider_call_records(
            persistence.get_item("owner/public", 1, table=self.table)
        )

    def counter(self):
        return next(
            value
            for value in self.table.items.values()
            if str(value.get("repo", "")).startswith("_OPENAI_GRANT_")
        )

    def test_real_adapter_deep_final_render_and_serialized_github_surface(self):
        from lambdas.LlamaPReviewPipeline import config
        from lambdas.LlamaPReviewPipeline.review.generate import generate_review
        from lambdas.LlamaPReviewPipeline.review.publish import prepare_github_review_request
        from tests.unit.test_review_generation_v1 import (
            REAL_PR_DETAILS,
            _real_context_meta,
            _real_presentation,
        )

        item = persistence.get_item("owner/public", 1, table=self.table)
        item.update(
            status="REVIEW_RUNNING",
            review_attempt=1,
            review_claim={**self.claim, "phase": "review"},
        )
        client = self.client(bound=False)
        pipeline_accounting.bind_provider_call_accounting(
            client,
            repo="owner/public",
            pr_number=1,
            expected_status="REVIEW_RUNNING",
            phase_claim={**self.claim, "phase": "review"},
            table=self.table,
        )
        metadata = {**self.metadata, "pipeline_phase": "review", "review_mode": "normal"}
        deep = response()
        deep["output"][1]["content"][0]["text"] = (
            "Complete staff-level judgment with admitted evidence."
        )
        final = response()
        final["output"][1]["content"][0]["text"] = json.dumps(
            _real_presentation(supporting_ci=True)
        )
        with (
            patch.multiple(
                config, MODEL_PROVIDER="direct_openai", DIRECT_OPENAI_PROFILE=PROFILE_CONFIG
            ),
            patch.object(
                transport.requests, "post", side_effect=[Response(body=deep), Response(body=final)]
            ) as post,
        ):
            result = generate_review(
                REAL_PR_DETAILS,
                "Exact-head evidence",
                client=client,
                context_meta=_real_context_meta(),
                trace_metadata=metadata,
            )
        self.assertTrue(result["review_publishable"])
        self.assertEqual(post.call_count, 2)
        self.assertEqual([record["stage"] for record in self.records()], ["deep", "final"])
        self.assertEqual(self.counter()["reserved_nanousd"], 0)
        surface = prepare_github_review_request(
            result["pr_review_comment"], [], head_sha=self.metadata["head_sha"]
        ).request_payload()
        encoded = json.dumps(surface, sort_keys=True)
        self.assertEqual(surface["event"], "COMMENT")
        self.assertEqual(surface["head_sha"], self.metadata["head_sha"])
        self.assertIn("LlamaPReview", surface["body"])
        self.assertNotIn("gpt-6", encoded)
        self.assertNotIn("reserved_nanousd", encoded)

    def test_responses_wire_and_exact_settlement(self):
        with patch.object(transport.requests, "post", return_value=Response()) as post:
            result = self.chat()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args, ("https://api.openai.com/v1/responses",))
        wire = post.call_args.kwargs["json"]
        self.assertEqual(wire["prompt_cache_options"], {"mode": "explicit"})
        self.assertEqual(wire["service_tier"], "default")
        for key in ("store", "stream", "background"):
            self.assertIs(wire[key], False)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertNotIn("messages", wire)
        self.assertNotIn("thinking", wire)
        self.assertEqual(wire["max_output_tokens"], 100)
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        record = self.records()[0]
        self.assertEqual(record["budget"]["state"], "settled")
        self.assertEqual(
            record["budget"]["actual_nanousd"], 20_000
        )  # 17 reasoning already inside 20 output
        self.assertEqual(self.counter()["charged_nanousd"], 20_000)
        self.assertEqual(self.counter()["reserved_nanousd"], 0)
        self.assertEqual(len(self.atomic.calls), 2)
        self.assertNotIn("ttl_epoch", self.counter())
        self.assertIn("ttl_epoch", persistence.get_item("owner/public", 1, table=self.table))

    def test_duplicate_settlement_and_original_coordinates(self):
        with patch.object(transport.requests, "post", return_value=Response()):
            result = self.chat()
        record = result[transport.PROVIDER_CALL_RECORD_KEY]
        before = deepcopy(self.counter())
        # Terminal callback cannot steer a refund to a new cycle or lower tariff.
        record["budget"] = {
            "counter_repo": "attacker",
            "reserved_nanousd": 999_999_999_999,
            "price": {},
        }
        self.assertTrue(
            persistence.record_provider_call(
                "owner/public", 1, expected_status="irrelevant", record=record, table=self.table
            )
        )
        self.assertEqual(before, self.counter())
        self.assertEqual(len(self.atomic.calls), 2)

    def test_late_settlement_after_takeover_and_expiry(self):
        def post(*args, **kwargs):
            item = persistence.get_item("owner/public", 1, table=self.table)
            item["status"] = "SUPERSEDED"
            item["context_claim"] = {**self.claim, "owner_id": "new-worker"}
            os.environ["DIRECT_OPENAI_BUDGET"] = json.dumps(
                policy(cycle_id="new-cycle", expires_at=1)
            )
            return Response()

        with patch.object(transport.requests, "post", side_effect=post):
            self.chat()
        self.assertEqual(self.counter()["cycle_id"], "synthetic-grant-1")
        self.assertEqual(self.counter()["charged_nanousd"], 20_000)
        with (
            patch.object(transport.requests, "post") as no_post,
            self.assertRaises(BudgetUnavailable),
        ):
            self.chat()
        no_post.assert_not_called()

    def test_retry_reserves_each_http_attempt_unknown_first_retained(self):
        limited = Response(
            429, {"error": {"type": "rate_limit_error", "code": "rate_limit_exceeded"}}
        )
        limited.headers["Retry-After"] = "1"
        with (
            patch.object(transport.requests, "post", side_effect=[limited, Response()]) as post,
            patch.object(transport.time, "sleep") as sleep,
        ):
            self.chat(max_retries=2)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)
        records = self.records()
        self.assertEqual([r["transport_attempt_index"] for r in records], [1, 2])
        self.assertEqual(len({r["operation_id"] for r in records}), 1)
        self.assertEqual(records[0]["budget"]["state"], "reserved")
        self.assertEqual(
            self.counter()["reserved_nanousd"], records[0]["budget"]["reserved_nanousd"]
        )
        self.assertEqual(
            self.counter()["charged_nanousd"], self.counter()["reserved_nanousd"] + 20_000
        )

    def test_typed_quota_rejections_never_retry_or_refund_unknown_cost(self):
        from lambdas.LlamaPReviewPipeline.errors import classify_failure

        errors = [{"code": code} for code in transport._BILLING_ERROR_CODES]
        errors.append({"type": "insufficient_quota"})
        client = self.client()
        for error in errors:
            with self.subTest(error=error):
                error["message"] = "private-response-canary-do-not-retain"
                with (
                    patch.object(
                        transport.requests, "post", return_value=Response(429, {"error": error})
                    ) as post,
                    patch.object(transport.time, "sleep") as sleep,
                    self.assertLogs(transport.logger, level="WARNING") as logs,
                    self.assertRaises(transport.ProviderBillingError) as raised,
                ):
                    self.chat(client, max_retries=3)
                post.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(raised.exception.status_code, 429)
                self.assertTrue(raised.exception.provider_call_control_failure)
                self.assertFalse(classify_failure(raised.exception, stage="context").retryable)
                record = self.records()[-1]
                self.assertEqual(record["http_status"], 429)
                self.assertEqual(record["budget"]["state"], "reserved")
                self.assertEqual(
                    self.counter()["charged_nanousd"], self.counter()["reserved_nanousd"]
                )
                self.assertNotIn(
                    "private-response-canary",
                    json.dumps(self.records(), default=str)
                    + str(raised.exception)
                    + str(logs.output),
                )

    def test_error_decode_is_bounded_typed_and_never_matches_message_words(self):
        for body in (
            b"not-json",
            b"[]",
            b'{"error":"insufficient_quota"}',
            b'{"error":{"message":"insufficient_quota credit_balance_exhausted"}}',
            b'{"error":{"code":[]}}',
            json.dumps({"error": {"code": "insufficient_quota", "message": "x" * 65_536}}).encode(),
        ):
            with self.subTest(body_size=len(body)):
                reply = Response(429)
                reply.content = body
                self.assertFalse(transport._direct_billing_rejection(reply))
        with (
            patch.object(
                transport.requests,
                "post",
                side_effect=[Response(429, {"error": {"message": "quota"}}), Response()],
            ) as post,
            patch.object(transport.time, "sleep"),
        ):
            self.chat(max_retries=2)
        self.assertEqual(post.call_count, 2)

    def test_payment_required_is_terminal_for_legacy_and_direct_transport(self):
        from lambdas.LlamaPReviewPipeline.errors import classify_failure

        for provider in ("deepseek", "direct_openai"):
            client = (
                self.client()
                if provider == "direct_openai"
                else transport.DeepSeekClient(
                    api_key="synthetic", provider="deepseek", transport_model_override=""
                )
            )
            with (
                self.subTest(provider=provider),
                patch.object(transport.requests, "post", return_value=Response(402)) as post,
                patch.object(transport.time, "sleep") as sleep,
                self.assertRaises(transport.ProviderBillingError) as raised,
            ):
                self.chat(client, max_retries=3)
            post.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(raised.exception.status_code, 402)
            self.assertFalse(classify_failure(raised.exception, stage="context").retryable)

    def test_unresolved_dispatch_redelivery_never_reposts(self):
        def fail_after_fence(*args, **kwargs):
            raise KeyboardInterrupt("synthetic process death")

        with (
            patch.object(transport.requests, "post", side_effect=fail_after_fence),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.chat()
        amount = self.counter()["reserved_nanousd"]
        with (
            patch.object(transport.requests, "post") as post,
            self.assertRaises(transport.ProviderDispatchOutcomeUnknown),
        ):
            self.chat()
        post.assert_not_called()
        self.assertEqual(self.counter()["reserved_nanousd"], amount)

    def test_budget_rejection_and_atomic_fence_failure_make_zero_posts(self):
        for change in (
            {"allocation_nanousd": 1},
            {"expires_at": 1},
            {"authorization": "unapproved"},
            {"profile_sha256": "c" * 64},
        ):
            with (
                self.subTest(change=change),
                patch.dict(os.environ, {"DIRECT_OPENAI_BUDGET": json.dumps(policy(**change))}),
                patch.object(transport.requests, "post") as post,
            ):
                with self.assertRaises((BudgetUnavailable, transport.ProviderCallFenceError)):
                    self.chat()
                post.assert_not_called()
                self.assertEqual(self.records(), [])
        with (
            patch.object(
                persistence, "_provider_transact_write", side_effect=RuntimeError("store down")
            ),
            patch.object(transport.requests, "post") as post,
        ):
            with self.assertRaises(transport.ProviderCallFenceError):
                self.chat()
            post.assert_not_called()

    def test_contradictory_incomplete_envelope_never_clears_but_settles_usage(self):
        for details in ({"reason": "max_output_tokens"}, "invalid-details"):
            client = self.client()
            body = response(incomplete_details=details)
            with patch.object(transport.requests, "post", return_value=Response(body=body)):
                result = self.chat(client)
            self.assertNotEqual(result["choices"][0]["finish_reason"], "stop")
            self.assertEqual(self.counter()["reserved_nanousd"], 0)
            self.assertEqual(self.records()[0]["budget"]["actual_nanousd"], 20_000)
            # A fresh operation in this same client gets a different call ID.
            self.table.items.clear()
            self.table.put_item(
                Item={
                    "repo": "owner/public",
                    "pr_number": 1,
                    "status": "CONTEXT_RUNNING",
                    "run_id": "run",
                    "head_sha": "b" * 40,
                    "context_attempt": 1,
                    "context_claim": self.claim,
                }
            )
        invalid = normalize_response(
            response(status="incomplete", incomplete_details="not-an-object")
        )
        self.assertEqual(invalid["choices"][0]["finish_reason"], "error")

    def test_unknown_usage_refusal_truncation_and_error_never_clear(self):
        variants = [
            response(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
            response(status="failed", error={"code": "synthetic"}),
            response(usage={}),
            response(
                output=[
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "refusal", "refusal": "No"}],
                    }
                ]
            ),
            response(service_tier="priority"),
            response(model="gpt-6.1-sol"),
        ]
        for index, body in enumerate(variants):
            with self.subTest(index=index):
                self.setUp()
                with patch.object(transport.requests, "post", return_value=Response(body=body)):
                    result = self.chat()
                self.assertNotEqual(result["choices"][0]["finish_reason"], "stop")
                self.assertEqual(result["choices"][0]["message"]["content"], "")

    def test_changed_profile_or_provider_rejects_new_purchase(self):
        with patch.object(transport.requests, "post", return_value=Response()):
            self.chat()
        with (
            patch.dict(
                os.environ, {"DIRECT_OPENAI_BUDGET": json.dumps(policy(profile_sha256="c" * 64))}
            ),
            patch.object(transport.requests, "post") as post,
        ):
            changed = {**self.metadata, "profile_sha256": "c" * 64}
            with self.assertRaises(ValueError):
                self.chat(trace_metadata=changed)
            post.assert_not_called()
        record = deepcopy(self.records()[0])
        record["provider"] = "deepseek"
        with self.assertRaises(persistence.ArtifactIntegrityError):
            persistence.record_provider_call(
                "owner/public",
                1,
                expected_status="CONTEXT_RUNNING",
                record=record,
                table=self.table,
            )

    def test_missing_original_fence_cannot_refund(self):
        with patch.object(transport.requests, "post", return_value=Response()):
            result = self.chat()
        item = persistence.get_item("owner/public", 1, table=self.table)
        record = result[transport.PROVIDER_CALL_RECORD_KEY]
        del item[persistence._provider_call_attr(record["call_id"])]
        before = deepcopy(self.counter())
        with self.assertRaises(persistence.ArtifactIntegrityError):
            persistence.record_provider_call(
                "owner/public",
                1,
                expected_status="CONTEXT_RUNNING",
                record=record,
                table=self.table,
            )
        self.assertEqual(before, self.counter())

    def test_explicit_binding_required_and_payload_restrictions(self):
        with patch.object(transport.requests, "post") as post, self.assertRaises(ValueError):
            self.chat(self.client(bound=False))
        post.assert_not_called()
        base = {"model": "gpt-6-luna", "effort": "max", "max_tokens": 100}
        for change in (
            {"model": "gpt-6.1-luna"},
            {"model": "gpt-6.1-sol"},
            {"effort": "high"},
            {"max_tokens": None},
            {"max_tokens": True},
            {"tools": [{"type": "web_search"}]},
            {"tool_choice": "auto"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                build_request([{"role": "user", "content": "test"}], **{**base, **change})

    def test_transport_byte_bound_preserves_existing_unicode_character_budget(self):
        build_request(
            [{"role": "user", "content": "界" * 400_000}],
            model="gpt-6-luna",
            effort="max",
            max_tokens=100,
        )
        with self.assertRaises(ValueError):
            build_request(
                [{"role": "user", "content": "界" * 1_333_334}],
                model="gpt-6-luna",
                effort="max",
                max_tokens=100,
            )

    def test_price_and_token_boundary_arithmetic(self):
        bound = reservation(self.policy, model="gpt-6.1-terra", max_output_tokens=128000)
        record = {
            "usage_state": "reported",
            "service_tier": "default",
            "response_model": "gpt-6.1-terra",
            "billed_model": "gpt-6.1-terra",
        }
        for input_tokens, expected in ((272000, 272010000), (272001, 544017000)):
            usage = normalize_response(response())["usage"]
            usage.update(
                input_tokens=input_tokens,
                prompt_tokens=input_tokens,
                output_tokens=2,
                completion_tokens=2,
                total_tokens=input_tokens + 2,
            )
            usage["output_tokens_details"]["reasoning_tokens"] = 2
            self.assertEqual(actual_cost({**record, "usage": usage}, bound), expected)
        usage["input_tokens_details"]["cache_write_tokens"] = 10
        self.assertIsNone(actual_cost({**record, "usage": usage}, bound))
        for raw in (
            "",
            "{}",
            json.dumps(policy(allocation_nanousd=151_000_000_000)),
            json.dumps(policy(verified_available_nanousd=1)),
            '{"cycle_id":"a","cycle_id":"b"}',
        ):
            with self.subTest(raw=raw), self.assertRaises(BudgetUnavailable):
                load_policy(raw)

    def test_two_workers_compete_for_last_reservation_atomically(self):
        bound = reservation(self.policy, model="gpt-6-luna", max_output_tokens=100)[
            "reserved_nanousd"
        ]
        self.policy.update(allocation_nanousd=bound)
        os.environ["DIRECT_OPENAI_BUDGET"] = json.dumps(self.policy)
        original = persistence.get_item("owner/public", 1, table=self.table)
        self.table.put_item(Item={**deepcopy(original), "pr_number": 2})
        barrier = threading.Barrier(2)
        errors = []
        posts = []

        # Missing usage keeps winner's amount; loser cannot admit after settlement.
        def worker(number):
            try:
                barrier.wait()
                self.chat(self.client(number=number))
            except transport.ProviderCallFenceError:
                errors.append(number)

        with patch.object(
            transport.requests,
            "post",
            side_effect=lambda *a, **k: posts.append(1) or Response(body=response(usage={})),
        ):
            threads = [threading.Thread(target=worker, args=(n,)) for n in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.counter()["charged_nanousd"], bound)
        self.assertNotIn("call_ids", self.counter())

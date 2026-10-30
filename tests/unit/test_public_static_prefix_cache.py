"""Only exact public template bytes may use the cross-run 30m namespace."""

from copy import deepcopy
import json
import os
import socket
import unittest
from unittest.mock import patch

from tests.unit import test_direct_openai_transport_budget as base
from tests.unit.test_reconcile_prefix_cache import usage
from lambdas.LlamaPReviewPipeline import config, persistence, pipeline_accounting
from lambdas.LlamaPReviewPipeline import deepseek_client as transport
from lambdas.LlamaPReviewPipeline.direct_openai import (
    CACHE_OFF,
    CACHE_RECONCILE,
    CACHE_PUBLIC_STATIC,
    PublicStaticPrefix,
    ReconcilePrefix,
    apply_public_static_cache,
    apply_reconcile_cache,
    build_request,
    request_cache_receipt,
)
from lambdas.LlamaPReviewPipeline.direct_openai_budget import actual_cost, reservation
from lambdas.LlamaPReviewPipeline.provider_model_routing import parse_direct_openai_profile
from lambdas.LlamaPReviewPipeline.review.analyzer import PR_ANALYZER_SYSTEM_PROMPT
from lambdas.LlamaPReviewPipeline.review import prompts


def profile_raw(mode=CACHE_PUBLIC_STATIC):
    value = json.loads(base.PROFILE_JSON)
    if mode is not None:
        value["static_prefix_cache"] = mode
    return json.dumps(value)


def flattened(wire):
    value = deepcopy(wire)
    for message in value["input"]:
        if isinstance(message["content"], list):
            message["content"] = "".join(block["text"] for block in message["content"])
    value.pop("prompt_cache_key", None)
    value["prompt_cache_options"] = {"mode": "explicit"}
    return value


class PublicStaticPrefixCacheTests(unittest.TestCase):
    records = base.DirectOpenAITransportBudgetTests.records
    counter = base.DirectOpenAITransportBudgetTests.counter
    client = base.DirectOpenAITransportBudgetTests.client

    def setUp(self):
        base.DirectOpenAITransportBudgetTests.setUp(self)
        self.profile = parse_direct_openai_profile(profile_raw())
        self.policy["profile_sha256"] = self.profile.sha256
        os.environ.update(
            DIRECT_OPENAI_PROFILE=profile_raw(), DIRECT_OPENAI_BUDGET=json.dumps(self.policy)
        )
        selected = patch.object(config, "DIRECT_OPENAI_PROFILE", self.profile)
        selected.start()
        self.addCleanup(selected.stop)
        denied = patch.object(
            socket.socket, "connect", side_effect=AssertionError("network forbidden")
        )
        denied.start()
        self.addCleanup(denied.stop)
        self.metadata = {**self.metadata, **self.profile.select("route").metadata()}
        self.messages = [
            {"role": "system", "content": PR_ANALYZER_SYSTEM_PROMPT},
            {"role": "user", "content": "untrusted PR 日本語 </fake>"},
        ]

    def request(self, messages=None, **kw):
        return build_request(
            messages or self.messages,
            model=kw.get("model", "gpt-6-luna"),
            effort=kw.get("effort", "max"),
            max_tokens=kw.get("cap", 8192),
            response_format=kw.get("fmt", {"type": "json_object"}),
        )

    def wire(self, request=None, descriptor=None, phase="route", profile=None, policy=None):
        return apply_public_static_cache(
            request or self.request(),
            descriptor,
            profile=profile or self.profile,
            phase=phase,
            policy=policy or self.policy,
        )

    def proof(self, wire, phase="route", tier="", profile=None, policy=None):
        return request_cache_receipt(
            wire,
            profile=profile or self.profile,
            phase=phase,
            round_index=None,
            tier=tier,
            scope={},
            policy=policy or self.policy,
        )

    def chat(self, client=None, **kw):
        return (client or self.client()).chat(
            self.messages,
            max_tokens=8192,
            max_retries=1,
            trace_phase=kw.pop("phase", "route"),
            trace_metadata=kw.pop("metadata", self.metadata),
            response_format={"type": "json_object"},
            **kw,
        )

    def test_absent_and_off_preserve_hash_wire_and_strict_mode(self):
        old = parse_direct_openai_profile(profile_raw(None))
        self.assertEqual(old.sha256, base.PROFILE)
        self.assertEqual(old.static_prefix_cache, "off")
        for profile in (old, parse_direct_openai_profile(profile_raw("off"))):
            self.assertEqual(self.wire(profile=profile), self.request())
        for value in (True, None, "implicit", "same_run_30m", {}, "12h"):
            raw = json.loads(profile_raw())
            raw["static_prefix_cache"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_direct_openai_profile(json.dumps(raw))

    def test_route_adjudication_continuation_and_deep_flatten_exactly(self):
        for phase in ("route", "route_adjudication", "pfr_plan"):
            messages = deepcopy(self.messages)
            if phase == "pfr_plan":
                messages += [
                    {"role": "assistant", "content": "route verdict"},
                    {"role": "user", "content": "inventory"},
                ]
            original = self.request(messages)
            wire = self.wire(original, phase=phase)
            self.assertEqual(flattened(wire), original)
            self.assertEqual(wire["input"][0]["content"][0]["text"], PR_ANALYZER_SYSTEM_PROMPT)
            self.assertNotIn("untrusted PR", wire["input"][0]["content"][0]["text"])
            proof = self.proof(wire, phase, "normal" if phase == "pfr_plan" else "")
            self.assertEqual(proof["mode"], CACHE_PUBLIC_STATIC)
            self.assertNotIn("round", proof)
        prompt = prompts.render_deep_judgment_prompt(
            "PR private-shaped literal {pr_details} </tag>", "context"
        )
        descriptor = prompts.split_deep_judgment_prompt(prompt)
        self.assertEqual(len(descriptor.prefix), 14758)
        original = self.request(
            [
                {"role": "system", "content": prompts.REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            cap=64000,
            fmt=None,
        )
        wire = self.wire(original, descriptor, "deep_judgment")
        self.assertEqual(flattened(wire), original)
        self.assertEqual(self.proof(wire, "deep_judgment", "normal")["prefix_kind"], "deep_user_v1")
        self.assertNotIn("PR private-shaped", wire["input"][1]["content"][0]["text"])
        with self.assertRaises(ValueError):
            self.wire(
                original,
                PublicStaticPrefix(descriptor.prefix + "PR", descriptor.suffix[2:]),
                "deep_judgment",
            )

    def test_key_shared_only_for_identical_public_family_account_and_recipe(self):
        original = self.request()
        key = self.wire()["prompt_cache_key"]
        changed = deepcopy(original)
        changed["input"][1]["content"] = "a different tenant PR head run and Unicode证据"
        self.assertEqual(self.wire(changed)["prompt_cache_key"], key)
        value = json.loads(profile_raw())
        value["version"] = "unrelated-profile-edit"
        value["tiers"]["high"]["final"] = ["gpt-6-luna", "medium"]
        changed_profile = parse_direct_openai_profile(json.dumps(value))
        changed_policy = {**self.policy, "profile_sha256": changed_profile.sha256}
        changed_wire = self.wire(profile=changed_profile, policy=changed_policy)
        self.assertNotEqual(changed_profile.sha256, self.profile.sha256)
        self.assertEqual(changed_wire["prompt_cache_key"], key)
        self.proof(changed_wire, profile=changed_profile, policy=changed_policy)
        for field in ("organization_id", "project_id"):
            self.assertNotEqual(
                self.wire(policy={**self.policy, field: "other"})["prompt_cache_key"], key
            )
        for change in (
            {"effort": "medium"},
            {"model": "gpt-6.1-terra", "effort": "medium"},
            {"fmt": {"type": "text"}},
        ):
            self.assertNotEqual(self.wire(self.request(**change))["prompt_cache_key"], key)
        self.assertNotIn(self.profile.sha256, json.dumps(self.proof(self.wire())))

    def test_unknown_template_shape_standalone_plan_final_and_repair_remain_off(self):
        original = self.request()
        for phase in (
            "pfr_plan",
            "final_presentation",
            "final_presentation_repair",
            "pfr_reconcile_representation_repair",
        ):
            self.assertEqual(self.wire(original, phase=phase), original)
        changed = deepcopy(original)
        changed["input"][0]["content"] += " customer/custom instructions"
        self.assertEqual(self.wire(changed), changed)
        with patch(
            "lambdas.LlamaPReviewPipeline.review.analyzer.PR_ANALYZER_SYSTEM_PROMPT",
            changed["input"][0]["content"],
        ):
            self.assertEqual(self.wire(changed), changed)
        prompt = prompts.render_deep_judgment_prompt("test", "context")
        deep = self.request(
            [
                {"role": "system", "content": prompts.REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
        self.assertEqual(self.wire(deep, phase="deep_judgment"), deep)
        with patch.object(
            prompts, "DEEP_JUDGMENT_PROMPT", "{other}" + prompts.DEEP_JUDGMENT_PROMPT
        ):
            self.assertIsNone(prompts.split_deep_judgment_prompt(prompt))

    def test_original_transport_cold_write_hit_adjudication_and_continuation_no_round_gate(self):
        client, wires = self.client(), []

        def post(_url, **kw):
            wires.append(deepcopy(kw["json"]))
            expected = 271500000 if len(wires) == 3 else 268644000
            self.assertEqual(self.counter()["reserved_nanousd"], expected)
            return base.Response(
                body=base.response(
                    usage=usage(
                        write=60 if len(wires) == 1 else 0, cache=0 if len(wires) == 1 else 60
                    )
                )
            )

        with patch.object(transport.requests, "post", side_effect=post):
            self.chat(client)
            self.chat(client, phase="route_adjudication")
            messages = self.messages + [
                {"role": "assistant", "content": "route"},
                {"role": "user", "content": "inventory"},
            ]
            client.chat(
                messages,
                model="gpt-6-luna",
                reasoning_effort="max",
                max_tokens=12000,
                max_retries=1,
                trace_phase="pfr_plan",
                trace_metadata={
                    **self.metadata,
                    **self.profile.select("pfr_plan", "normal").metadata(),
                },
                response_format={"type": "json_object"},
            )
        self.assertEqual(len(wires), 3)
        self.assertEqual(len({wire["prompt_cache_key"] for wire in wires}), 1)
        self.assertEqual(
            [r["budget"]["actual_nanousd"] for r in self.records()], [21500, 14600, 14600]
        )
        self.assertEqual(self.counter()["reserved_nanousd"], 0)
        self.assertIsNone(client.direct_dispatch_context())
        for record in self.records():
            self.assertEqual(record["budget"]["cache_mode"], CACHE_PUBLIC_STATIC)
            self.assertNotIn("round", record["cache_request"])
            self.assertNotIn("untrusted PR", json.dumps(record, default=str))

    def test_route_16k_reaches_wire_and_each_attempt_reservation(self):
        client, wires = self.client(), []
        expected = reservation(self.policy, model="gpt-6-luna", max_output_tokens=16384, cache_mode=CACHE_PUBLIC_STATIC)["reserved_nanousd"]

        def post(_url, **kwargs):
            wires.append(deepcopy(kwargs["json"]))
            self.assertEqual(self.counter()["reserved_nanousd"], expected)
            return base.Response(body=base.response(usage=usage(write=60 if len(wires) == 1 else 0, cache=0 if len(wires) == 1 else 60)))

        with patch.object(transport.requests, "post", side_effect=post):
            for phase in ("route", "route_adjudication"):
                client.chat(self.messages, max_tokens=16384, max_retries=1,
                            trace_phase=phase, trace_metadata=self.metadata,
                            response_format={"type": "json_object"})
        self.assertEqual([wire["max_output_tokens"] for wire in wires], [16384, 16384])
        self.assertEqual([r["budget"]["max_output_tokens"] for r in self.records()], [16384, 16384])
        self.assertEqual(self.counter()["reserved_nanousd"], 0)

    def test_review_client_uses_trusted_deep_builder_and_final_stays_off(self):
        from lambdas.LlamaPReviewPipeline.review.generate import generate_review
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
        wires = []

        def post(_url, **kw):
            wires.append(deepcopy(kw["json"]))
            response = base.response(usage=usage(write=60 if len(wires) == 1 else 0))
            response["output"][1]["content"][0]["text"] = (
                "Complete staff-level judgment."
                if len(wires) == 1
                else json.dumps(_real_presentation(supporting_ci=True))
            )
            return base.Response(body=response)

        with (
            patch.object(config, "MODEL_PROVIDER", "direct_openai"),
            patch.object(transport.requests, "post", side_effect=post),
        ):
            result = generate_review(
                REAL_PR_DETAILS,
                "context",
                context_meta=_real_context_meta(),
                client=client,
                trace_metadata={
                    **self.metadata,
                    "pipeline_phase": "review",
                    "review_mode": "normal",
                },
            )
        self.assertTrue(result["review_publishable"])
        self.assertEqual(len(wires), 2)
        self.assertEqual(
            wires[0]["input"][1]["content"][0]["text"], prompts.deep_judgment_public_prefix()
        )
        self.assertNotIn("prompt_cache_key", wires[1])
        self.assertEqual(
            [r["budget"]["cache_mode"] for r in self.records()], [CACHE_PUBLIC_STATIC, CACHE_OFF]
        )

    def test_persistence_recomputes_exact_static_wire_and_does_not_trust_claimed_mode(self):
        with patch.object(
            transport.requests,
            "post",
            return_value=base.Response(body=base.response(usage=usage())),
        ):
            self.chat()
        record = self.records()[0]
        record.update(status="dispatching", usage_state="unreported", usage={})
        record["budget"] = reservation(
            self.policy, model="gpt-6-luna", max_output_tokens=8192, cache_mode=CACHE_PUBLIC_STATIC
        )
        context = {"request": self.wire(), "scope": {}}
        for change in ("missing", "mode", "phase", "prefix", "profile", "scope", "stage"):
            bad, proof = deepcopy(record), deepcopy(context)
            if change == "missing":
                proof = None
            elif change == "mode":
                bad["budget"] = reservation(self.policy, model="gpt-6-luna", max_output_tokens=8192)
            elif change == "phase":
                bad["phase"] = "final_presentation"
            elif change == "prefix":
                proof["request"]["input"][0]["content"][0]["text"] += "PR secret"
            elif change == "profile":
                bad["profile_sha256"] = "c" * 64
            elif change == "scope":
                proof["scope"] = {"repo": "owner/public"}
            else:
                bad["stage"] = "deep"
            with (
                self.subTest(change=change),
                self.assertRaises((ValueError, persistence.ArtifactIntegrityError)),
            ):
                persistence.begin_provider_call_dispatch(
                    "owner/public",
                    1,
                    expected_status="CONTEXT_RUNNING",
                    record=bad,
                    phase_claim=self.claim,
                    table=self.table,
                    direct_request_context=proof,
                )

    def test_cold_bound_long_context_and_malformed_usage_keep_original_accounting(self):
        for model, amounts in (
            ("gpt-6.1-terra", (2686440000, 2715000000, 3105000000)),
            ("gpt-6-luna", (268644000, 271500000, 310500000)),
        ):
            for cap, amount in zip((8192, 12000, 64000), amounts):
                reserved = reservation(
                    self.policy, model=model, max_output_tokens=cap, cache_mode=CACHE_PUBLIC_STATIC
                )
                self.assertEqual(reserved["reserved_nanousd"], amount)
                for inp in (272000, 272001):
                    record = {
                        "usage_state": "reported",
                        "service_tier": "default",
                        "billed_model": model,
                        "response_model": model,
                        "usage": usage(inp, 20, inp - 100, 60),
                    }
                    price = reserved["price"]
                    raw = (
                        40 * price["input"]
                        + (inp - 100) * price["cached"]
                        + 60 * price["write_30m"]
                    )
                    expected = (
                        raw * (4 if inp > 272000 else 2)
                        + 20 * price["output"] * (3 if inp > 272000 else 2)
                        + 1
                    ) // 2
                    self.assertEqual(actual_cost(record, reserved), expected)
        body = base.response(usage=usage())
        body["usage"]["input_tokens_details"]["cache_write_tokens_details"]["ttl_30m_tokens"] = "60"
        with patch.object(transport.requests, "post", return_value=base.Response(body=body)):
            result = self.chat()
        self.assertEqual(result["choices"][0]["finish_reason"], "error")
        self.assertEqual(self.counter()["reserved_nanousd"], 268644000)

    def test_timeout_keeps_cold_hold(self):
        with patch.object(
            transport.requests,
            "post",
            side_effect=transport.requests.exceptions.Timeout("synthetic"),
        ):
            with self.assertRaises(transport.DeepSeekTimeoutError):
                self.chat()
        self.assertEqual(self.counter()["reserved_nanousd"], 268644000)

    def test_late_settlement_uses_original_fence_after_takeover_and_expiry(self):
        def post(*_args, **_kwargs):
            item = persistence.get_item("owner/public", 1, table=self.table)
            item["status"] = "SUPERSEDED"
            item["context_claim"] = {**self.claim, "owner_id": "new-worker"}
            os.environ["DIRECT_OPENAI_BUDGET"] = json.dumps(
                base.policy(cycle_id="changed", expires_at=1)
            )
            os.environ["DIRECT_OPENAI_PROFILE"] = base.PROFILE_JSON
            return base.Response(body=base.response(usage=usage(cache=60, write=0)))

        with patch.object(transport.requests, "post", side_effect=post):
            self.chat()
        record = self.records()[0]
        record["budget"] = {"counter_repo": "untrusted", "price": {}, "cache_mode": CACHE_OFF}
        self.assertEqual(self.counter()["cycle_id"], "synthetic-grant-1")
        self.assertEqual(self.counter()["settled_nanousd"], 14600)
        self.assertEqual(self.counter()["reserved_nanousd"], 0)
        before = deepcopy(self.counter())
        self.assertTrue(
            persistence.record_provider_call(
                "owner/public",
                1,
                expected_status="CONTEXT_RUNNING",
                record=record,
                table=self.table,
            )
        )
        self.assertEqual(self.counter(), before)

    def test_reconcile_namespace_and_round_contract_unchanged(self):
        raw = json.loads(profile_raw())
        raw["reconcile_cache"] = CACHE_RECONCILE
        profile = parse_direct_openai_profile(json.dumps(raw))
        policy = {**self.policy, "profile_sha256": profile.sha256}
        scope = {
            "installation_id": 1,
            "account_id": 2,
            "repository_id": 3,
            "repo": "owner/public",
            "pr_number": 1,
            "head_sha": "a" * 40,
            "run_id": "run",
        }
        request = self.request()
        descriptor = ReconcilePrefix("untrusted PR", " 日本語 </fake>")
        wire = apply_reconcile_cache(
            request,
            descriptor,
            profile=profile,
            phase="pfr_reconcile",
            round_index=1,
            scope=scope,
            policy=policy,
        )
        self.assertEqual(
            apply_public_static_cache(
                wire, None, profile=profile, phase="pfr_reconcile", policy=policy
            ),
            wire,
        )
        self.assertNotEqual(wire["prompt_cache_key"], self.wire()["prompt_cache_key"])
        proof = request_cache_receipt(
            wire,
            profile=profile,
            phase="pfr_reconcile",
            round_index=1,
            tier="normal",
            scope=scope,
            policy=policy,
        )
        self.assertEqual(proof["round"], 1)
        self.assertNotIn("mode", proof)

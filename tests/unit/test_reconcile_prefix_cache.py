"""Opt-in Reconcile cache seams, synthetic HTTP and durable accounting only."""

from copy import deepcopy
from decimal import Decimal
import json
import os
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.unit import test_direct_openai_transport_budget as base
from lambdas.LlamaPReviewPipeline import config, persistence, deepseek_client as transport
from lambdas.LlamaPReviewPipeline.context_engine.pfr.reconcile_contract import _reconcile
from lambdas.LlamaPReviewPipeline.context_engine.pfr.prompts import RECONCILE_SYSTEM_PROMPT
from lambdas.LlamaPReviewPipeline.direct_openai import (
    CACHE_OFF,
    CACHE_RECONCILE,
    ReconcilePrefix,
    apply_reconcile_cache,
    build_request,
    request_cache_receipt,
)
from lambdas.LlamaPReviewPipeline.direct_openai_budget import actual_cost, reservation
from lambdas.LlamaPReviewPipeline.provider_model_routing import parse_direct_openai_profile
from lambdas.LlamaPReviewPipeline.pr_ingest import GitHubRuntime


def profile_raw(mode="same_run_30m"):
    value = json.loads(base.PROFILE_JSON)
    if mode is not None:
        value["reconcile_cache"] = mode
    return json.dumps(value)


def usage(inp=100, out=20, cache=0, write=60):
    return {
        "input_tokens": inp,
        "prompt_tokens": inp,
        "output_tokens": out,
        "completion_tokens": out,
        "total_tokens": inp + out,
        "input_tokens_details": {
            "cached_tokens": cache,
            "cache_write_tokens": write,
            "cache_write_tokens_details": {"ttl_30m_tokens": write, "ttl_12h_tokens": 0},
        },
        "output_tokens_details": {"reasoning_tokens": min(out, 17)},
    }


class ReconcilePrefixCacheTests(unittest.TestCase):
    records = base.DirectOpenAITransportBudgetTests.records
    counter = base.DirectOpenAITransportBudgetTests.counter

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
        self.scope = {
            "installation_id": 10,
            "repository_id": 20,
            "account_id": 30,
            "repo": "owner/public",
            "pr_number": 1,
            "head_sha": "b" * 40,
            "run_id": "run",
        }
        self.table.items[next(iter(self.table.items))]["installation_id"] = 10
        self.metadata = {
            **self.metadata,
            **self.profile.select("pfr_reconcile", "normal").metadata(),
            "repo": "owner/public",
            "pr_number": 1,
            "round": 1,
        }
        self.messages = [
            {"role": "system", "content": "fixed developer"},
            {"role": "user", "content": "固定 prefix\n\ntail"},
        ]
        self.descriptor = ReconcilePrefix("固定 prefix", "\n\ntail")

    def client(self):
        client = base.DirectOpenAITransportBudgetTests.client(self)
        client.set_reconcile_cache_scope(self.scope)
        return client

    def chat(self, client=None, **kw):
        return (client or self.client()).chat(
            self.messages,
            reconcile_prefix=kw.pop("descriptor", self.descriptor),
            max_tokens=kw.pop("max_tokens", 12000),
            max_retries=kw.pop("max_retries", 1),
            trace_phase=kw.pop("phase", "pfr_reconcile"),
            trace_metadata=kw.pop("metadata", self.metadata),
            **kw,
        )

    def body(self, **kw):
        return base.response(usage=usage(**kw))

    def test_profile_missing_and_off_keep_old_wire_and_default_off_reserve(self):
        missing = parse_direct_openai_profile(profile_raw(None))
        self.assertEqual(missing.sha256, base.PROFILE)
        old = build_request(self.messages, model="gpt-6-luna", effort="max", max_tokens=12000)
        for profile in (missing, parse_direct_openai_profile(profile_raw("off")), self.profile):
            scope = {} if profile is self.profile else self.scope
            new = apply_reconcile_cache(
                old,
                self.descriptor,
                profile=profile,
                phase="pfr_reconcile",
                round_index=1,
                scope=scope,
                policy=self.policy,
            )
            self.assertEqual(new, old)
        self.assertEqual(
            reservation(self.policy, model="gpt-6-luna", max_output_tokens=12000)[
                "reserved_nanousd"
            ],
            219000000,
        )
        for value in (True, None, "12h", {}, "implicit"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_direct_openai_profile(
                    profile_raw(value)
                ) if value is not None else parse_direct_openai_profile(
                    profile_raw().replace('"same_run_30m"', "null")
                )

    def test_one_write_then_hit_preserves_text_and_binds_same_key(self):
        wires = []

        def post(_url, **kw):
            wires.append(deepcopy(kw["json"]))
            self.assertEqual(self.counter()["reserved_nanousd"], 271500000)
            return base.Response(
                body=self.body(cache=60, write=0) if len(wires) == 2 else self.body()
            )

        client = self.client()
        with patch.object(transport.requests, "post", side_effect=post) as send:
            self.chat(client)
            self.messages[1]["content"] = "固定 prefix\n\nsecond evidence"
            self.chat(
                client,
                metadata={**self.metadata, "round": 2},
                descriptor=ReconcilePrefix("固定 prefix", "\n\nsecond evidence"),
            )
        self.assertEqual(send.call_count, 2)
        self.assertIsNone(client.direct_dispatch_context())
        self.assertEqual(wires[0]["prompt_cache_key"], wires[1]["prompt_cache_key"])
        self.assertEqual(wires[0]["input"][0], {"role": "developer", "content": "fixed developer"})
        self.assertEqual(
            "".join(b["text"] for b in wires[0]["input"][1]["content"]), "固定 prefix\n\ntail"
        )
        self.assertEqual(self.records()[0]["budget"]["actual_nanousd"], 21500)
        self.assertEqual(self.records()[1]["budget"]["actual_nanousd"], 14600)
        saved = json.dumps(self.records(), default=str)
        for forbidden in (
            "固定",
            "fixed developer",
            "installation_id",
            "repository_id",
            "organization_id",
        ):
            self.assertNotIn(forbidden, saved)
        self.assertEqual(self.counter()["reserved_nanousd"], 0)

    def test_scope_profile_model_prefix_isolation_and_missing_scope_off(self):
        original = build_request(self.messages, model="gpt-6-luna", effort="max", max_tokens=12000)

        def wire(scope=None, profile=None, request=None, policy=None):
            return apply_reconcile_cache(
                request or original,
                self.descriptor,
                profile=profile or self.profile,
                phase="pfr_reconcile",
                round_index=1,
                scope=self.scope if scope is None else scope,
                policy=policy or self.policy,
            )

        key = wire()["prompt_cache_key"]
        for field, value in {
            "installation_id": 11,
            "repository_id": 21,
            "account_id": 31,
            "repo": "owner/other",
            "pr_number": 2,
            "head_sha": "c" * 40,
            "run_id": "other",
        }.items():
            self.assertNotEqual(wire(scope={**self.scope, field: value})["prompt_cache_key"], key)
        for field in ("organization_id", "project_id"):
            self.assertNotEqual(
                wire(policy={**self.policy, field: "other"})["prompt_cache_key"], key
            )
        value = json.loads(profile_raw())
        value["version"] = "other"
        self.assertNotEqual(
            wire(profile=parse_direct_openai_profile(json.dumps(value)))["prompt_cache_key"], key
        )
        for model, effort in (("gpt-6-luna", "medium"), ("gpt-6.1-terra", "medium")):
            changed = build_request(self.messages, model=model, effort=effort, max_tokens=12000)
            self.assertNotEqual(wire(request=changed)["prompt_cache_key"], key)
        for field in self.scope:
            scope = dict(self.scope)
            scope.pop(field)
            self.assertEqual(wire(scope=scope), original)
        for fmt in ({"type": "text"}, {"type": "json_object"}):
            changed_format = build_request(
                self.messages,
                model="gpt-6-luna",
                effort="max",
                max_tokens=12000,
                response_format=fmt,
            )
            self.assertNotEqual(wire(request=changed_format)["prompt_cache_key"], key)
        changed = deepcopy(original)
        changed["input"][0]["content"] += "!"
        self.assertNotEqual(wire(request=changed)["prompt_cache_key"], key)

    def test_descriptor_phase_round_mismatch_and_mutated_scope_stop_before_http(self):
        with patch.object(transport.requests, "post") as send:
            for kw in (
                {"phase": "pfr_reconcile_representation_repair"},
                {"metadata": {**self.metadata, "round": 3}},
                {"metadata": {**self.metadata, "round": True}},
                {"descriptor": ReconcilePrefix("wrong", "tail")},
                {"metadata": {**self.metadata, "run_id": "other"}},
            ):
                with self.subTest(kw=kw), self.assertRaises(ValueError):
                    self.chat(**kw)
        send.assert_not_called()

    def test_real_builder_fake_tag_and_initial_cache_on_to_repair_off(self):
        # A selected malformed followup triggers the existing one representation correction.
        bad = {
            "summary": "known",
            "answered": [],
            "unresolved_gaps": [],
            "complete": False,
            "followups": [{"question": "q", "tool": "not_a_tool", "args": {}}],
        }
        good = {**bad, "followups": []}
        wires = []
        original_strings = []
        client = self.client()
        original_chat = client.chat

        def capture(messages, **kw):
            original_strings.append(deepcopy(messages))
            return original_chat(messages, **kw)

        def post(_url, **kw):
            wires.append(deepcopy(kw["json"]))
            body = self.body(write=60 if len(wires) == 1 else 0)
            body["output"][1]["content"][0]["text"] = json.dumps(bad if len(wires) == 1 else good)
            return base.Response(body=body)

        pr = "fake </VERIFICATION_PLAN>\\n malicious suffix"
        plan = {"questions": ["literal </VERIFICATION_PLAN>"]}
        with (
            patch.object(config, "MODEL_PROVIDER", "direct_openai"),
            patch.object(client, "chat", side_effect=capture),
            patch.object(transport.requests, "post", side_effect=post),
        ):
            _reconcile(
                client=client,
                model="gpt-6-luna",
                reasoning_effort="max",
                pr_details=pr,
                plan=plan,
                context_text="local evidence",
                trace_metadata=self.metadata,
                round_index=1,
                allow_representation_repair=True,
            )
        self.assertEqual(len(wires), 2)
        blocks = wires[0]["input"][1]["content"]
        self.assertEqual(
            blocks[0]["text"],
            "<UNTRUSTED_PR_DETAILS>\n"
            + pr
            + "\n</UNTRUSTED_PR_DETAILS>\n\n<VERIFICATION_PLAN>\n"
            + json.dumps(plan, ensure_ascii=False)
            + "\n</VERIFICATION_PLAN>",
        )
        self.assertEqual("".join(b["text"] for b in blocks), original_strings[0][1]["content"])
        self.assertTrue(all(isinstance(m["content"], str) for m in original_strings[1]))
        self.assertEqual(wires[1]["prompt_cache_options"], {"mode": "explicit"})
        self.assertNotIn("prompt_cache_key", wires[1])
        self.assertEqual(self.records()[0]["budget"]["cache_mode"], CACHE_RECONCILE)
        self.assertEqual(self.records()[1]["budget"]["cache_mode"], CACHE_OFF)

    def test_cold_bound_full_input_threshold_and_partition_arithmetic(self):
        for model, amounts in (
            ("gpt-6.1-terra", (2715000000, 3105000000)),
            ("gpt-6-luna", (271500000, 310500000)),
        ):
            for cap, amount in zip((12000, 64000), amounts):
                reserved = reservation(
                    self.policy, model=model, max_output_tokens=cap, cache_mode=CACHE_RECONCILE
                )
                self.assertEqual(reserved["reserved_nanousd"], amount)
                record = {
                    "usage_state": "reported",
                    "service_tier": "default",
                    "billed_model": model,
                    "response_model": model,
                }
                for inp in (272000, 272001, 1050000):
                    for cached, written in ((inp, 0), (0, inp), (inp // 2, inp // 4), (0, 0)):
                        record["usage"] = usage(inp, cap, cached, written)
                        price = reserved["price"]
                        base_cost = (
                            (inp - cached - written) * price["input"]
                            + cached * price["cached"]
                            + written * price["write_30m"]
                        )
                        expected = (
                            base_cost * (4 if inp > 272000 else 2)
                            + cap * price["output"] * (3 if inp > 272000 else 2)
                            + 1
                        ) // 2
                        self.assertEqual(actual_cost(record, reserved), expected)
                        decimal_record = deepcopy(record)
                        decimal_record["usage"]["input_tokens"] = Decimal(inp)
                        self.assertEqual(actual_cost(decimal_record, reserved), expected)

    def test_raw_malformed_cache_usage_never_refunds_after_lossy_projection(self):
        for mutate in (
            lambda u: u["input_tokens_details"]["cache_write_tokens_details"].update(
                ttl_12h_tokens=False
            ),
            lambda u: u["input_tokens_details"]["cache_write_tokens_details"].update(
                ttl_12h_tokens=-1
            ),
            lambda u: u["input_tokens_details"]["cache_write_tokens_details"].update(
                ttl_30m_tokens=59
            ),
            lambda u: u["input_tokens_details"].pop("cache_write_tokens_details"),
            lambda u: u["input_tokens_details"]["cache_write_tokens_details"].update(extra=-1),
            lambda u: u["input_tokens_details"].update(cached_tokens=99),
            lambda u: u.update(input_tokens=True),
        ):
            with self.subTest(mutate=mutate):
                # Each isolated table uses the real client and same atomic owner.
                self.table.items.clear()
                self.table.put_item(
                    Item={
                        "repo": "owner/public",
                        "pr_number": 1,
                        "status": "CONTEXT_RUNNING",
                        "run_id": "run",
                        "head_sha": "b" * 40,
                        "installation_id": 10,
                        "context_attempt": 1,
                        "context_claim": self.claim,
                    }
                )
                body = self.body()
                mutate(body["usage"])
                with patch.object(
                    transport.requests, "post", return_value=base.Response(body=body)
                ):
                    result = self.chat()
                    self.assertEqual(result["choices"][0]["finish_reason"], "error")
                    self.assertEqual(result["choices"][0]["message"]["content"], "")
                self.assertEqual(self.counter()["reserved_nanousd"], 271500000)
                self.assertEqual(self.records()[0]["usage_state"], "unreported")

    def test_timeout_retains_cold_write_and_each_retry_reserves_independently(self):
        client = self.client()
        with (
            patch.object(
                transport.requests,
                "post",
                side_effect=[
                    transport.requests.exceptions.Timeout("synthetic"),
                    base.Response(body=self.body(cache=0, write=0)),
                ],
            ) as send,
            patch.object(transport.time, "sleep"),
        ):
            self.chat(client, max_retries=2)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(self.counter()["reserved_nanousd"], 271500000)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(len({r["call_id"] for r in self.records()}), 2)

    def test_persistence_recomputes_profile_phase_wire_and_cold_mode(self):
        client = self.client()
        with patch.object(transport.requests, "post", return_value=base.Response(body=self.body())):
            self.chat(client)
        record = self.records()[0]
        # Reconstruct dispatch receipt; every alteration fails before transaction.
        record.update(status="dispatching", usage_state="unreported", usage={})
        record["budget"] = reservation(
            self.policy, model="gpt-6-luna", max_output_tokens=12000, cache_mode=CACHE_RECONCILE
        )
        request = apply_reconcile_cache(
            build_request(self.messages, model="gpt-6-luna", effort="max", max_tokens=12000),
            self.descriptor,
            profile=self.profile,
            phase="pfr_reconcile",
            round_index=1,
            scope=self.scope,
            policy=self.policy,
        )
        context = {"request": request, "scope": self.scope}
        for alteration in (
            "missing",
            "self_report_off",
            "wire_ttl",
            "phase",
            "round",
            "model",
            "scope",
            "extra_tool",
            "cap",
            "profile",
        ):
            bad, proof = deepcopy(record), deepcopy(context)
            if alteration == "missing":
                proof = None
            if alteration == "self_report_off":
                bad["budget"] = reservation(
                    self.policy, model="gpt-6-luna", max_output_tokens=12000
                )
            if alteration == "wire_ttl":
                proof["request"]["prompt_cache_options"]["ttl"] = "12h"
            if alteration == "phase":
                bad["phase"] = "pfr_reconcile_representation_repair"
            if alteration == "round":
                bad["cache_request"]["round"] = 2
            if alteration == "model":
                bad["billed_model"] = "gpt-6.1-terra"
            if alteration == "scope":
                proof["scope"]["installation_id"] += 1
            if alteration == "extra_tool":
                proof["request"]["tools"] = []
            if alteration == "cap":
                bad["budget"]["max_output_tokens"] = 64000
            if alteration == "profile":
                bad["profile_sha256"] = "0" * 64
            with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                persistence.begin_provider_call_dispatch(
                    "owner/public",
                    1,
                    expected_status="CONTEXT_RUNNING",
                    record=bad,
                    phase_claim=self.claim,
                    table=self.table,
                    direct_request_context=proof,
                )

    def test_native_existing_pr_ids_never_lookup_or_trust_text_scope(self):
        raw = {
            "number": 1,
            "base": {
                "repo": {
                    "id": 20,
                    "full_name": "owner/public",
                    "private": False,
                    "owner": {"id": 30},
                }
            },
        }
        pull = SimpleNamespace(
            raw_data=raw,
            completed=True,
            head=SimpleNamespace(sha="b" * 40),
            base=SimpleNamespace(sha="a" * 40),
        )
        repo = Mock()
        repo.repo.get_pull.return_value = pull
        repo.get_pr_content.return_value = {"file_changes": []}
        runtime = GitHubRuntime.__new__(GitHubRuntime)
        runtime.get_repository = Mock(return_value=repo)
        runtime.get_pr_content("owner/public", 1)
        identity = runtime.reconcile_cache_identity("owner/public", 1, "b" * 40)
        self.assertEqual(
            identity,
            {k: v for k, v in self.scope.items() if k not in {"installation_id", "run_id"}},
        )
        runtime.get_repository.assert_called_once()
        repo.repo.get_pull.assert_called_once_with(1)
        self.assertEqual(runtime.reconcile_cache_identity("owner/public", 1, "c" * 40), {})
        raw["base"]["repo"]["full_name"] = "other/repo"
        runtime.get_pr_content("owner/public", 1)
        self.assertEqual(runtime.reconcile_cache_identity("owner/public", 1, "b" * 40), {})

    def test_cache_settlement_uses_original_fence_after_expiry_and_takeover(self):
        def post(_url, **kw):
            current = self.table.items[
                next(k for k, v in self.table.items.items() if v.get("repo") == "owner/public")
            ]
            current.update(status="SUPERSEDED", run_id="new-owner")
            os.environ["DIRECT_OPENAI_BUDGET"] = "expired-and-replaced"
            os.environ["DIRECT_OPENAI_PROFILE"] = "changed-after-dispatch"
            return base.Response(body=self.body(cache=60, write=0))

        with patch.object(transport.requests, "post", side_effect=post):
            self.chat()
        durable = self.records()[0]
        self.assertEqual(durable["budget"]["actual_nanousd"], 14600)
        self.assertEqual(durable["budget"]["cache_ttl"], "30m")
        self.assertEqual(self.counter()["reserved_nanousd"], 0)
        self.assertTrue(
            persistence.record_provider_call(
                "owner/public", 1, expected_status="ignored", record=durable, table=self.table
            )
        )
        self.assertEqual(self.counter()["settled_nanousd"], 14600)

    def test_cache0_invalid_raw_ttl_leaf_is_not_washed_into_refund(self):
        client = self.client()
        client.set_reconcile_cache_scope({})
        body = self.body(write=0)
        body["usage"]["input_tokens_details"]["cache_write_tokens_details"]["ttl_12h_tokens"] = -1
        with patch.object(
            transport.requests, "post", return_value=base.Response(body=body)
        ) as send:
            result = self.chat(client)
        self.assertEqual(
            send.call_args.kwargs["json"]["prompt_cache_options"], {"mode": "explicit"}
        )
        self.assertEqual(result["choices"][0]["finish_reason"], "error")
        self.assertEqual(self.counter()["reserved_nanousd"], 219000000)

    def test_durable_admission_mismatch_rejects_cache_before_http(self):
        # Even internally consistent client scope/key cannot escape the admitted installation.
        self.scope["installation_id"] = 11
        with (
            patch.object(transport.requests, "post") as send,
            self.assertRaises(transport.ProviderCallFenceError),
        ):
            self.chat()
        send.assert_not_called()
        self.assertEqual(self.records(), [])

    def test_real_sdk_cache_fence_serialization_and_decimal_settlement(self):
        import subprocess
        import sys
        from pathlib import Path

        code = r"""
import json, os, socket, time
from pathlib import Path
from unittest.mock import patch
import boto3
from moto import mock_aws
from lambdas.LlamaPReviewPipeline import persistence, pipeline_accounting, deepseek_client
from lambdas.LlamaPReviewPipeline.direct_openai import ReconcilePrefix
from lambdas.LlamaPReviewPipeline.direct_openai_budget import PRICING_VERSION
from lambdas.LlamaPReviewPipeline.provider_model_routing import parse_direct_openai_profile
value=json.loads(Path("config/direct_openai_profile.example.json").read_text())
value["reconcile_cache"]="same_run_30m"
raw=json.dumps(value); profile=parse_direct_openai_profile(raw); now=int(time.time())
policy={"cycle_id":"synthetic", "valid_from":now-1, "expires_at":now+86400,
"allocation_nanousd":150_000_000_000,"verified_available_nanousd":180_000_000_000,
"organization_id":"synthetic-org","project_id":"synthetic-project","profile_sha256":profile.sha256,
"pricing_version":PRICING_VERSION,"pricing_verified_until":now+86400,"authorization":"approved"}
os.environ.update(DIRECT_OPENAI_PROFILE=raw,DIRECT_OPENAI_BUDGET=json.dumps(policy),DEEPSEEK_TRACE_MODE="off")
scope={"repo":"owner/public","pr_number":1,"head_sha":"b"*40,"run_id":"run","installation_id":1,"repository_id":2,"account_id":3}
metadata={**{k:scope[k] for k in ("repo","pr_number","head_sha","run_id")},"pipeline_phase":"context","pipeline_attempt":1,
**profile.select("pfr_reconcile","normal").metadata()}
claim={"phase":"context","owner_id":"w","stream_event_id":"e","attempt":1}
with patch.object(socket.socket,"connect",side_effect=AssertionError("network forbidden")), mock_aws():
 table=boto3.resource("dynamodb",region_name="us-east-1").create_table(TableName="cache-offline",BillingMode="PAY_PER_REQUEST",
 KeySchema=[{"AttributeName":"repo","KeyType":"HASH"},{"AttributeName":"pr_number","KeyType":"RANGE"}],
 AttributeDefinitions=[{"AttributeName":"repo","AttributeType":"S"},{"AttributeName":"pr_number","AttributeType":"N"}])
 table.put_item(Item={**{k:scope[k] for k in ("repo","pr_number","head_sha","run_id","installation_id")},
 "status":"CONTEXT_RUNNING","context_attempt":1,"context_claim":claim})
 client=deepseek_client.DeepSeekClient(api_key="synthetic",provider="direct_openai",model="gpt-6-luna",reasoning_effort="max",transport_model_override="")
 client.set_reconcile_cache_scope(scope)
 pipeline_accounting.bind_provider_call_accounting(client,repo="owner/public",pr_number=1,expected_status="CONTEXT_RUNNING",phase_claim=claim,table=table)
 class Response:
  status_code=200; headers={}
  def __init__(self, read): self.read=read
  def json(self):
   return {"id":"synthetic","model":"gpt-6-luna","status":"completed","service_tier":"default",
   "output":[{"type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":"{}"}]}],
   "usage":{"input_tokens":100,"output_tokens":20,"total_tokens":120,
   "input_tokens_details":{"cached_tokens":60 if self.read else 0,"cache_write_tokens":0 if self.read else 60,
   "cache_write_tokens_details":{"ttl_30m_tokens":0 if self.read else 60,"ttl_12h_tokens":0}},"output_tokens_details":{"reasoning_tokens":17}}}
 with patch.object(deepseek_client.requests,"post",side_effect=[Response(False),Response(True)]) as send:
  for round_index in (1,2):
   client.chat([{"role":"system","content":"developer"},{"role":"user","content":"前缀 tail"}],max_tokens=12000,
   max_retries=1,trace_phase="pfr_reconcile",trace_metadata={**metadata,"round":round_index},reconcile_prefix=ReconcilePrefix("前缀"," tail"))
 assert send.call_count==2
 records=persistence.provider_call_records(persistence.get_item("owner/public",1,table=table))
 assert len(records)==2
 assert {r["budget"]["actual_nanousd"] for r in records} == {21500,14600}
 assert all(r["budget"]["reserved_nanousd"]==271500000 and r["budget"]["cache_ttl"]=="30m" for r in records)
 counter=table.get_item(Key={"repo":records[0]["budget"]["counter_repo"],"pr_number":0})["Item"]
 assert counter["charged_nanousd"]==counter["settled_nanousd"]==36100 and counter["reserved_nanousd"]==0
 assert "前缀" not in json.dumps(records,default=str,ensure_ascii=False)
print("cache real SDK offline passed")
"""
        env = {
            **os.environ,
            "MODEL_PROVIDER": "deepseek",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "synthetic",
            "AWS_SECRET_ACCESS_KEY": "synthetic",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cache real SDK offline passed", result.stdout)

    def test_locked_pygithub_lazy_raw_data_is_never_completed_for_cache_scope(self):
        import subprocess
        import sys
        from pathlib import Path

        code = r"""
import socket
from unittest.mock import Mock, patch
from github.PullRequest import PullRequest
from lambdas.LlamaPReviewPipeline.pr_ingest import GitHubRuntime
attrs={"number":1,"head":{"sha":"b"*40},"base":{"sha":"a"*40,"repo":{
"id":20,"full_name":"owner/public","private":False,"owner":{"id":30}}}}
with patch.object(socket.socket,"connect",side_effect=AssertionError("network forbidden")):
 for completed in (False,True):
  requester=Mock(); requester.is_lazy=True
  pull=PullRequest(requester,headers={},attributes=attrs,completed=completed)
  repository=Mock(); repository.repo.get_pull.return_value=pull
  repository.get_pr_content.return_value={"file_changes":[]}
  runtime=GitHubRuntime.__new__(GitHubRuntime); runtime.get_repository=Mock(return_value=repository)
  runtime.get_pr_content("owner/public",1)
  assert bool(runtime.reconcile_cache_identity("owner/public",1,"b"*40)) == completed
  assert requester.mock_calls == []
print("locked PyGithub no additional read passed")
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "MODEL_PROVIDER": "deepseek"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("locked PyGithub no additional read passed", result.stdout)

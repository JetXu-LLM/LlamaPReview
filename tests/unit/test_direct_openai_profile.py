"""Offline stage selection and unchanged bounded pipeline behavior."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.unit.fakes import (
    FakeDynamoResource, ensure_repo_root_on_path, set_default_env,
    install_fake_aws_modules, install_fake_jwt_module, install_fake_requests_module,
)

ensure_repo_root_on_path()
set_default_env()
install_fake_jwt_module()
install_fake_requests_module()
install_fake_aws_modules(FakeDynamoResource())

from lambdas.LlamaPReviewPipeline import config, orchestrator
from lambdas.LlamaPReviewPipeline.provider_model_routing import (
    parse_direct_openai_profile, resolve_provider_model,
)
from lambdas.LlamaPReviewPipeline.context_engine.pfr import collect_context_pfr
from lambdas.LlamaPReviewPipeline.context_engine.pfr.plan_contract import _plan_model_selection
from lambdas.LlamaPReviewPipeline.context_engine.pfr.reconcile_contract import _reconcile
from lambdas.LlamaPReviewPipeline.context_engine.repo_structure import RepoInventory
from lambdas.LlamaPReviewPipeline.review.analyzer import analyze_pr_complexity
from lambdas.LlamaPReviewPipeline.review import generate as generation
from tests.unit.test_route_and_retrieval_contract import (
    _ConversationClient, _Runtime, _pr_content, _tree,
)
from tests.unit.test_review_generation_v1 import (
    _Client, _response, _presentation_failure, _publishable,
    REAL_PR_DETAILS, _real_context_meta, _real_presentation,
)


def profile_value():
    """Deliberately distinct test cells; not an experimentally selected winner."""
    return {
        "version": "synthetic-v1",
        "route": ["gpt-6.1-terra", "medium"],
        "tiers": {
            "low": {"deep": ["gpt-6-luna", "medium"], "final": ["gpt-6-luna", "medium"]},
            "normal": {
                "pfr_plan": ["gpt-6.1-terra", "high"],
                "pfr_reconcile": ["gpt-6-luna", "max"],
                "deep": ["gpt-6.1-terra", "xhigh"],
                "final": ["gpt-6-luna", "max"],
            },
            "high": {
                "pfr_plan": ["gpt-6.1-terra", "max"],
                "pfr_reconcile": ["gpt-6.1-terra", "high"],
                "deep": ["gpt-6.1-terra", "max"],
                "final": ["gpt-6.1-terra", "low"],
            },
        },
    }


class DirectOpenAIProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = parse_direct_openai_profile(json.dumps(profile_value()))
        self.selected = patch.multiple(config, MODEL_PROVIDER="direct_openai", DIRECT_OPENAI_PROFILE=self.profile)
        self.selected.start()
        self.addCleanup(self.selected.stop)

    def test_profile_is_exact_immutable_complete_and_canonical(self):
        self.assertEqual(self.profile.sha256, parse_direct_openai_profile(json.dumps(profile_value(), sort_keys=True)).sha256)
        self.assertEqual(len(self.profile.entries), 11)
        with self.assertRaises(FrozenInstanceError):
            self.profile.version = "changed"
        for stage, tier in (("deep", "skip"), ("pfr_plan", "low"), ("route", "normal"), ("second_review", "high")):
            with self.subTest(stage=stage, tier=tier), self.assertRaises(ValueError):
                self.profile.select(stage, tier)
        self.assertEqual(orchestrator._review_model_for_mode("low"), {"model": "gpt-6-luna", "reasoning_effort": "medium"})
        self.assertEqual(_plan_model_selection({"complexity": "normal", "risk_domains": ["security"]}), ("gpt-6.1-terra", "high"))

    def test_luna_intermediate_efforts_are_explicit_and_profile_bound(self):
        digests = set()
        for effort in ("medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                value = profile_value()
                value["tiers"]["normal"]["pfr_plan"] = ["gpt-6-luna", effort]
                value["tiers"]["normal"]["pfr_reconcile"] = ["gpt-6-luna", effort]
                selected = parse_direct_openai_profile(json.dumps(value))
                for stage in ("pfr_plan", "pfr_reconcile"):
                    cell = selected.select(stage, "normal")
                    self.assertEqual((cell.model, cell.reasoning_effort), ("gpt-6-luna", effort))
                    self.assertEqual(cell.metadata()["profile_sha256"], selected.sha256)
                digests.add(selected.sha256)
        self.assertEqual(len(digests), 4)

    def test_profile_rejects_unknown_models_efforts_cells_and_duplicates(self):
        for pair in (["gpt-6.1-sol", "high"], ["gpt-6.1-luna", "max"], ["gpt-6-luna", "ultra"], ["gpt-6-luna", "low"], ["gpt-6-luna", "none"], ["gpt-6.1-terra", "none"], ["gpt-6-luna ", "max"]):
            value = profile_value(); value["route"] = pair
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                parse_direct_openai_profile(json.dumps(value))
        for mutate in (lambda v: v["tiers"].pop("high"), lambda v: v["tiers"]["low"].update(pfr_plan=["gpt-6-luna", "max"]), lambda v: v.update(fallback="deepseek")):
            value = profile_value(); mutate(value)
            with self.assertRaises(ValueError):
                parse_direct_openai_profile(json.dumps(value))
        with self.assertRaises(ValueError):
            parse_direct_openai_profile('{"version":"one","version":"two"}')
        with self.assertRaises(ValueError):
            resolve_provider_model("gpt-6-luna", "gpt-6.1-terra", provider="direct_openai")
        self.assertEqual(resolve_provider_model("gpt-6.1-terra", "", provider="direct_openai").billed_model, "gpt-6.1-terra")

    def test_config_requires_explicit_profile_and_ignores_legacy_overrides(self):
        env = {**os.environ, "MODEL_PROVIDER": "direct_openai", "DIRECT_OPENAI_PROFILE": ""}
        failed = subprocess.run([sys.executable, "-c", "from lambdas.LlamaPReviewPipeline import config"], env=env, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        env["DIRECT_OPENAI_PROFILE"] = json.dumps(profile_value())
        env["DEEPSEEK_MODEL"] = "unapproved-stale-value"
        good = subprocess.run([sys.executable, "-c", "from lambdas.LlamaPReviewPipeline import config as c; print(c.DEEPSEEK_MODEL, c.DEEPSEEK_TRANSPORT_MODEL_OVERRIDE, c.NORMAL_REVIEW_MODEL)"], env=env, capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(good.stdout.strip(), "gpt-6.1-terra  gpt-6.1-terra")

    def test_route_adjudication_inherits_route_without_new_call(self):
        low = {"reviewable_semantic_delta": True, "minimum_evidence_boundary": "diff_only", "reason": "Visible change", "complexity": "low", "pr_type": "code", "risk_domains": []}
        normal = {**low, "minimum_evidence_boundary": "bounded_repo", "complexity": "normal"}
        client = _ConversationClient([low, normal])
        inventory = RepoInventory(repository="owner/repo", requested_sha="head123456789", status="complete", discoverable_files={"src/changed.py", "scripts/bootstrap.sh"})
        deadline = object()
        result = analyze_pr_complexity("details", pr_content=_pr_content(diff='+run("scripts/bootstrap.sh")\n'), repo_inventory=inventory, client=client, deadline=deadline)
        self.assertEqual(result["complexity"], "normal")
        self.assertEqual(len(client.calls), 2)
        for call in client.calls:
            self.assertEqual((call["model"], call["reasoning_effort"], call["max_tokens"]), ("gpt-6.1-terra", "medium", 16384))
            self.assertIs(call["deadline"], deadline)
            self.assertEqual(call["trace_metadata"]["stage"], "route")
            self.assertEqual(call["trace_metadata"]["profile_sha256"], self.profile.sha256)

    def test_route_16k_truncation_keeps_single_call_high_fallback(self):
        class TruncatedClient(_ConversationClient):
            def chat(self, messages, **kwargs):
                result = super().chat(messages, **kwargs)
                result["choices"][0]["finish_reason"] = "length"
                return result

        client = TruncatedClient(["partial"])
        result = analyze_pr_complexity("details", pr_content=_pr_content(), client=client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["max_tokens"], 16384)
        self.assertEqual(result["complexity"], "high")
        self.assertEqual(result["_route_plan_meta"]["contract_failure_kind"], "output_truncated")

    def test_route_plan_reconcile_reuses_existing_conversation_and_call_count(self):
        route = {"reviewable_semantic_delta": True, "minimum_evidence_boundary": "bounded_repo", "reason": "Caller evidence required", "complexity": "normal", "pr_type": "code", "risk_domains": ["api"]}
        plan = {"verification_plan": [{"question": "Inspect caller", "tool": "read_file", "args": {"path": "src/caller.py"}}]}
        reconcile = {"summary": "Bounded files checked", "answered": [], "unresolved_gaps": [], "followups": [], "complete": True}
        client = _ConversationClient([route, plan, reconcile])
        content = _pr_content(path="src/caller.py")
        accepted = analyze_pr_complexity("details", pr_content=content, client=client)
        runtime = _Runtime(files={"src/caller.py": "def value():\n    return 1\n"})
        with patch("lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm", return_value=_tree("src/caller.py")), patch("lambdas.LlamaPReviewPipeline.context_engine.pfr.evidence_execution._safety_sweep", return_value=0):
            collect_context_pfr(runtime=runtime, github_token="synthetic", repo_full_name="owner/repo", pr_content=content, pr_details="details", head_sha="head123456789", default_branch="main", client=client, route_plan=accepted)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[1]["messages"][:2], client.calls[0]["messages"])
        self.assertEqual([(c["model"], c["reasoning_effort"]) for c in client.calls], [("gpt-6.1-terra", "medium"), ("gpt-6.1-terra", "high"), ("gpt-6-luna", "max")])
        self.assertEqual([c["max_tokens"] for c in client.calls], [16384, 12000, 12000])
        self.assertEqual([c["trace_metadata"]["stage"] for c in client.calls], ["route", "pfr_plan", "pfr_reconcile"])

    def test_reconcile_representation_repair_inherits_model_effort_and_stage(self):
        initial = {"summary": "The file was read.", "answered": "not-an-array", "unresolved_gaps": [], "followups": [], "complete": False}
        repaired = {**initial, "answered": []}
        client = _ConversationClient([initial, repaired])
        selected = self.profile.select("pfr_reconcile", "high")
        result = _reconcile(client=client, model=selected.model, reasoning_effort=selected.reasoning_effort, pr_details="details", plan={}, context_text="context", trace_metadata=selected.metadata(), round_index=1, allow_representation_repair=True)
        self.assertTrue(result["_representation_repair"]["attempted"])
        self.assertEqual(len(client.calls), 2)
        for call in client.calls:
            self.assertEqual((call["model"], call["reasoning_effort"], call["max_tokens"]), ("gpt-6.1-terra", "high", 12000))
            self.assertEqual(call["trace_metadata"]["stage"], "pfr_reconcile")
            self.assertTrue(call["thinking"])

    def test_deep_final_serialized_surface_uses_independent_cells_and_no_extra_call(self):
        client = _Client([_response("complete staff-level memo"), _response(json.dumps(_real_presentation(supporting_ci=True)))])
        result = generation.generate_review(REAL_PR_DETAILS, "Exact-head evidence", client=client, context_meta=_real_context_meta(), trace_metadata={"review_mode": "normal"})
        self.assertTrue(result["review_publishable"])
        self.assertIn("LlamaPReview", result["pr_review_comment"])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([(c["model"], c["reasoning_effort"], c["trace_metadata"]["stage"]) for c in client.calls], [("gpt-6.1-terra", "xhigh", "deep"), ("gpt-6-luna", "max", "final")])

    def test_final_representation_correction_inherits_final_cell(self):
        client = _Client([_response("memo"), _response('{"version":"presentation_v1"}'), _response('{"version":"presentation_v1"}')])
        with patch.object(generation, "compile_presentation_v1", side_effect=[_presentation_failure(), _publishable()]):
            generation.generate_review("details", "evidence", client=client, trace_metadata={"review_mode": "high"})
        self.assertEqual(len(client.calls), 3)
        for call in client.calls[1:]:
            self.assertEqual((call["model"], call["reasoning_effort"], call["trace_metadata"]["stage"], call["trace_metadata"]["tier"]), ("gpt-6.1-terra", "low", "final", "high"))
            self.assertTrue(call["thinking"])


if __name__ == "__main__":
    unittest.main()


"""Billing rejection is terminal across the existing bounded review stages."""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from tests.unit.test_context_engine_pipeline import (
    _FakePfrClient,
    _Runtime,
    _pfr_pr_content,
    _repo_tree,
)
from tests.unit.test_review_generation_v1 import (
    _Client,
    _presentation_failure,
    _response,
)
from tests.unit.test_route_and_retrieval_contract import _pr_content

from lambdas.LlamaPReviewPipeline.context_engine import initialization
from lambdas.LlamaPReviewPipeline.context_engine.pfr import collect_context_pfr
from lambdas.LlamaPReviewPipeline.context_engine.repo_structure import RepoInventory
from lambdas.LlamaPReviewPipeline.deepseek_client import (
    DeepSeekHTTPError,
    ProviderBillingError,
    ProviderCallFenceError,
)
from lambdas.LlamaPReviewPipeline.direct_openai_budget import BudgetUnavailable
from lambdas.LlamaPReviewPipeline.errors import classify_failure
from lambdas.LlamaPReviewPipeline.review import generate as generation
from lambdas.LlamaPReviewPipeline.review.analyzer import analyze_pr_complexity
from lambdas.LlamaPReviewPipeline.review.judgment import failure_retryable


class ProviderBillingPropagationTests(unittest.TestCase):
    def setUp(self):
        network_guard = patch(
            "socket.socket.connect",
            side_effect=AssertionError("offline billing tests forbid network"),
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def test_failure_classification_keeps_billing_and_budget_terminal(self):
        cases = (
            (
                ProviderBillingError("quota rejected", status_code=429),
                "provider_billing_rejected",
                False,
            ),
            (DeepSeekHTTPError("payment required", status_code=402), "http_terminal", False),
            (BudgetUnavailable("grant unavailable"), "provider_budget_unavailable", False),
            (
                ProviderCallFenceError(
                    "direct admission unavailable",
                    provider_call_record={"provider": "direct_openai"},
                ),
                "provider_budget_unavailable",
                False,
            ),
            (
                ProviderCallFenceError("legacy fence unavailable", provider_call_record={}),
                "provider_dispatch_fence_unavailable",
                True,
            ),
            (DeepSeekHTTPError("ordinary rate limit", status_code=429), "http_transient", True),
        )
        for error, kind, retryable in cases:
            with self.subTest(kind=kind, error=type(error).__name__):
                result = classify_failure(error, stage="context.route")
                self.assertEqual(result.kind, kind)
                self.assertEqual(result.stage, "context.route")
                self.assertIs(result.retryable, retryable)
                self.assertIs(failure_retryable(error), retryable)

    def test_route_billing_propagates_from_initial_and_adjudication(self):
        low = {
            "reviewable_semantic_delta": True,
            "minimum_evidence_boundary": "diff_only",
            "reason": "The visible edit appears locally closed.",
            "complexity": "low",
            "pr_type": "code",
            "risk_domains": [],
        }
        inventory = RepoInventory(
            repository="owner/repo",
            requested_sha="head123456789",
            status="complete",
            discoverable_files={"src/changed.py", "scripts/bootstrap.sh"},
        )
        for adjudication in (False, True):
            with self.subTest(adjudication=adjudication):
                error = ProviderBillingError("quota rejected", status_code=429)
                responses = [_response(json.dumps(low))] if adjudication else []
                client = _Client([*responses, error])
                with self.assertRaises(ProviderBillingError) as raised:
                    analyze_pr_complexity(
                        "details",
                        pr_content=_pr_content(diff='+run("scripts/bootstrap.sh")\n'),
                        repo_inventory=inventory,
                        client=client,
                    )
                self.assertIs(raised.exception, error)
                self.assertEqual(
                    [call["trace_phase"] for call in client.calls],
                    ["pr_analyzer", "pr_analyzer_adjudication"]
                    if adjudication
                    else ["pr_analyzer"],
                )

    def _assert_pfr_stops(self, *, reconcile, repair=False):
        for error in (
            ProviderBillingError("quota rejected", status_code=429),
            BudgetUnavailable("grant unavailable"),
        ):
            with self.subTest(error=type(error).__name__):
                responses = (
                    [
                        {
                            "complexity": "normal",
                            "pr_type": "code",
                            "risk_domains": [],
                            "verification_plan": [],
                        }
                    ]
                    if reconcile
                    else []
                )
                if repair:
                    responses.append(
                        {
                            "summary": "The file was read.",
                            "answered": "not-an-array",
                            "unresolved_gaps": [],
                            "followups": [],
                            "complete": False,
                        }
                    )
                client = _FakePfrClient([*responses, error])
                with (
                    patch.object(
                        initialization,
                        "get_repo_structure_for_llm",
                        return_value=_repo_tree("src/service.py"),
                    ),
                    self.assertRaises(type(error)) as raised,
                ):
                    collect_context_pfr(
                        runtime=_Runtime(),
                        github_token="synthetic-token",
                        repo_full_name="owner/repo",
                        pr_content=_pfr_pr_content(),
                        pr_details="# PR\n",
                        head_sha="abcdef123456",
                        default_branch="main",
                        client=client,
                        max_search_calls=1,
                        max_read_calls=2,
                    )
                self.assertIs(raised.exception, error)
                self.assertEqual(len(client.calls), 1 + int(reconcile) + int(repair))

    def test_pfr_plan_billing_or_budget_does_not_use_fallback(self):
        self._assert_pfr_stops(reconcile=False)

    def test_pfr_reconcile_billing_or_budget_does_not_repair_or_continue(self):
        self._assert_pfr_stops(reconcile=True)

    def test_pfr_correction_billing_or_budget_does_not_continue(self):
        self._assert_pfr_stops(reconcile=True, repair=True)

    def _assert_review_stops(self, *, fail_at):
        responses = []
        if fail_at != "deep_judgment":
            responses.append(_response("complete staff-level memo"))
        if fail_at == "final_presentation_repair":
            responses.append(_response('{"version":"presentation_v1"}'))
        responses.append(ProviderBillingError("quota rejected", status_code=429))
        client = _Client(responses)
        before_final = Mock()
        with patch.object(
            generation,
            "compile_presentation_v1",
            return_value=_presentation_failure(),
        ) as compiler:
            result = generation.generate_review(
                "PR intent",
                "## PFR Review Context\nExact-head evidence",
                client=client,
                context_meta={},
                before_final=before_final,
            )
        expected_phases = ["deep_judgment"]
        if fail_at != "deep_judgment":
            expected_phases.append("final_presentation")
        if fail_at == "final_presentation_repair":
            expected_phases.append("final_presentation_repair")
        self.assertEqual([call["trace_phase"] for call in client.calls], expected_phases)
        self.assertEqual(
            [phase["phase"] for phase in result["review_model_phases"]], expected_phases
        )
        self.assertEqual(compiler.call_count, int(fail_at == "final_presentation_repair"))
        self.assertEqual(before_final.call_count, len(expected_phases) - 1)
        self.assertFalse(result["review_publishable"])
        self.assertFalse(result["review_publication_safe"])
        self.assertFalse(result["review_fallback_used"])
        self.assertFalse(result["review_failure_retryable"])
        self.assertEqual(result["review_failure_kind"], "provider_billing_rejected")
        self.assertEqual(result["review_generation_status"], "failed")

    def test_deep_billing_is_terminal_without_final(self):
        self._assert_review_stops(fail_at="deep_judgment")

    def test_final_billing_is_terminal_without_correction(self):
        self._assert_review_stops(fail_at="final_presentation")

    def test_billing_during_final_correction_does_not_retry(self):
        self._assert_review_stops(fail_at="final_presentation_repair")


if __name__ == "__main__":
    unittest.main()

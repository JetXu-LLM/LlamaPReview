import json
import unittest

from tests.unit.fakes import (
    ensure_repo_root_on_path,
    install_fake_requests_module,
    set_default_env,
)

ensure_repo_root_on_path()
set_default_env()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline.review.context_projection import (
    acceptance_criteria_for_deep,
    changed_delta_for_deep,
    evidence_catalog_for_deep,
    evidence_gaps_for_deep,
)
from lambdas.LlamaPReviewPipeline.review.judgment import (
    ReviewModelResponseError,
    ReviewOutputTruncated,
    cap_context_for_review,
    run_model_phase,
)


class _Client:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.response


def _response(content="judgment", finish_reason="stop"):
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3},
    }


class JudgmentBoundaryTests(unittest.TestCase):
    def test_deep_evidence_catalog_excludes_default_branch_only_observations(self):
        head = "a" * 40
        projected = evidence_catalog_for_deep(
            {
                "head_sha": head,
                "evidence_catalog": [
                    {
                        "id": "path:service.py",
                        "source_type": "diff",
                        "outcome": "hit",
                        "paths": ["service.py"],
                        "coverage_type": "changed_region",
                    },
                    {
                        "id": "ev_head",
                        "source_type": "pfr",
                        "outcome": "hit",
                        "paths": ["service.py"],
                        "coverage_type": "full_file",
                        "source_ref": f"pr_head:{head}",
                        "question_id": "q_private",
                    },
                    {
                        "id": "ev_default_only",
                        "source_type": "pfr",
                        "outcome": "hit",
                        "paths": ["caller.py"],
                        "coverage_type": "search_snippet",
                        "source_ref": "default_branch_search",
                    },
                    {
                        "id": "ev_relocated",
                        "source_type": "pfr",
                        "outcome": "hit",
                        "paths": ["caller.py"],
                        "coverage_type": "search_snippet",
                        "source_ref": "default_branch_search",
                        "head_reread_outcome": "relocated_at_head",
                        "search_hit_lineage": [
                            {
                                "path": "caller.py",
                                "outcome": "relocated_at_head",
                                "head_sha": head,
                            }
                        ],
                    },
                    {
                        "id": "registry:package",
                        "source_type": "non_repository",
                        "outcome": "hit",
                        "coverage_type": "non_repository",
                    },
                    {
                        "id": "ev_wrong_head",
                        "source_type": "pfr",
                        "outcome": "hit",
                        "paths": ["service.py"],
                        "coverage_type": "full_file",
                        "source_ref": f"pr_head:{'b' * 40}",
                    },
                ],
            }
        )

        self.assertEqual(
            [item["id"] for item in projected],
            [
                "path:service.py",
                "ev_head",
                "ev_relocated",
                "registry:package",
            ],
        )
        self.assertNotIn("question_id", projected[1])

    def test_deep_projection_excludes_private_pfr_ledgers(self):
        meta = {
            "pfr_author_acceptance_criteria": [
                {"criterion": "The retry remains idempotent.", "id": "obl_1"}
            ],
            "pfr_unresolved_gaps": [
                {
                    "claim": "The remote timeout is not documented.",
                    "how_to_check": "Read the service contract.",
                    "affects_merge": True,
                    "reconcile_label": "marked material by Reconcile",
                    "resolution_id": "res_1",
                }
            ],
            "pfr_evidence_coverage": {
                "reconcile_complete": True,
                "fetch_health": {"read_errors": 0},
            },
            "pfr_plan": {"question_id": "q_private"},
            "pfr_reconcile": {"resolution_id": "res_private"},
            "evidence_ledger": {"events": [{"id": "ev_private"}]},
            "changed_delta_focus": {
                "files": [
                    {
                        "path": "service.py",
                        "change_type": "modified",
                        "patch": "+retry()",
                        "diff_coverage": "complete",
                    }
                ],
                "packing": {"source_file_count": 1},
            },
        }

        self.assertEqual(
            acceptance_criteria_for_deep(meta),
            [{"criterion": "The retry remains idempotent."}],
        )
        gaps = evidence_gaps_for_deep(meta)
        self.assertEqual(
            gaps["unresolved_gaps"][0]["claim"],
            "The remote timeout is not documented.",
        )
        self.assertNotIn("resolution_id", gaps["unresolved_gaps"][0])
        self.assertNotIn("affects_merge", gaps["unresolved_gaps"][0])
        self.assertNotIn("pfr_plan", gaps)
        deep_boundary = json.dumps(
            {
                "acceptance_criteria": acceptance_criteria_for_deep(meta),
                "evidence_gaps": gaps,
            }
        )
        self.assertIn("The retry remains idempotent.", deep_boundary)
        self.assertIn("The remote timeout is not documented.", deep_boundary)
        self.assertNotIn("affects_merge", deep_boundary)
        self.assertNotIn("marked material by Reconcile", deep_boundary)
        self.assertEqual(
            changed_delta_for_deep(meta)["files"][0]["patch"],
            "+retry()",
        )

    def test_context_cap_preserves_required_truth_sections(self):
        context = "\n".join(
            [
                "# PR Review Context",
                "identity",
                "## Changed Files (PR head)",
                "changed",
                "## PFR Review Context",
                "retrieved",
                "## Incidental",
                "x" * 20_000,
            ]
        )
        capped = cap_context_for_review(
            "details",
            context,
            max_input_chars=6000,
        )
        self.assertIn("# PR Review Context", capped)
        self.assertIn("## Changed Files (PR head)", capped)
        self.assertIn("## PFR Review Context", capped)
        self.assertLessEqual(len(capped) + len("details"), 6000)

    def test_model_phase_uses_fixed_no_tools_transport_and_records_usage(self):
        client = _Client(_response())
        result = run_model_phase(
            client,
            phase="deep_judgment",
            messages=[{"role": "user", "content": "review"}],
            model="deepseek-v4-pro",
            reasoning_effort="high",
            thinking=True,
            max_tokens=24000,
            timeout_seconds=30,
            deadline=None,
            trace_metadata={"run_id": "run"},
        )
        self.assertEqual(result.content, "judgment")
        self.assertEqual(result.telemetry["phase"], "deep_judgment")
        self.assertEqual(result.telemetry["usage"]["prompt_tokens"], 2)
        call = client.calls[0]
        self.assertNotIn("tools", call)
        self.assertNotIn("tool_choice", call)
        self.assertNotIn("response_format", call)
        self.assertTrue(call["thinking"])
        self.assertEqual(call["max_tokens"], 24000)

    def test_length_and_empty_responses_fail_typed(self):
        with self.assertRaises(ReviewOutputTruncated) as truncated:
            run_model_phase(
                _Client(_response("partial", "length")),
                phase="final_presentation",
                messages=[{"role": "user", "content": "present"}],
                model="deepseek-v4-pro",
                reasoning_effort="high",
                thinking=True,
                max_tokens=18000,
                timeout_seconds=30,
                deadline=None,
                trace_metadata={},
                response_format={"type": "json_object"},
            )
        self.assertEqual(truncated.exception.visible_content, "partial")
        self.assertEqual(
            truncated.exception.telemetry["finish_reason"],
            "length",
        )
        self.assertEqual(
            truncated.exception.telemetry["usage"]["completion_tokens"],
            3,
        )
        with self.assertRaises(ReviewModelResponseError):
            run_model_phase(
                _Client(_response("", "stop")),
                phase="final_presentation",
                messages=[{"role": "user", "content": "present"}],
                model="deepseek-v4-pro",
                reasoning_effort="high",
                thinking=True,
                max_tokens=18000,
                timeout_seconds=30,
                deadline=None,
                trace_metadata={},
                response_format={"type": "json_object"},
            )


if __name__ == "__main__":
    unittest.main()

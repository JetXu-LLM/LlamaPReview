import unittest
from unittest.mock import patch

from tests.unit.fakes import (
    ensure_repo_root_on_path,
    install_fake_requests_module,
    set_default_env,
)

ensure_repo_root_on_path()
set_default_env()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline.deadline import Deadline
from lambdas.LlamaPReviewPipeline.context_engine.pfr import collect_context_pfr
from lambdas.LlamaPReviewPipeline.review import generate as generation
from tests.unit.test_context_engine_pipeline import (
    _FakePfrClient,
    _Runtime,
    _pfr_pr_content,
    _repo_tree,
)
from tests.unit.test_review_generation_v1 import (
    _Client,
    _publishable,
    _response,
)


class PhaseCallbackTests(unittest.TestCase):
    def _collect_pfr(self, client, callback):
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization."
            "get_repo_structure_for_llm",
            return_value=_repo_tree("src/service.py", "AGENTS.md"),
        ), patch(
            "lambdas.LlamaPReviewPipeline.config.PFR_MAX_RECONCILE_ROUNDS",
            1,
        ):
            return collect_context_pfr(
                runtime=self.runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
                deadline=Deadline.for_seconds(60),
                before_first_reconcile=callback,
            )

    def setUp(self):
        self.runtime = _Runtime()

    def test_before_first_reconcile_runs_after_initial_tools_exactly_once(self):
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "It contains the changed behavior.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        }
                    ],
                },
                {
                    "summary": "The exact-head file was checked.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
            ]
        )
        callback_observations = []

        def before_first_reconcile():
            callback_observations.append(
                {
                    "phases": [call["trace_phase"] for call in client.calls],
                    "reads": list(self.runtime.read_calls),
                }
            )

        _context, meta = self._collect_pfr(client, before_first_reconcile)

        self.assertEqual(len(callback_observations), 1)
        self.assertEqual(callback_observations[0]["phases"], ["pfr_plan"])
        self.assertIn(
            ("owner/repo", "src/service.py", "abcdef123456"),
            callback_observations[0]["reads"],
        )
        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile"],
        )
        self.assertEqual(len(meta["pfr_reconcile_dispatches"]), 1)
        dispatch = meta["pfr_reconcile_dispatches"][0]
        self.assertEqual(dispatch["round"], 1)
        self.assertGreater(dispatch["deadline_remaining_seconds"], 0)
        self.assertGreaterEqual(dispatch["deadline_elapsed_seconds"], 0)
        self.assertGreaterEqual(dispatch["elapsed_seconds"], 0)

    def test_before_first_reconcile_failure_propagates_before_provider_call(self):
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "It contains the changed behavior.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        }
                    ],
                }
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "lifecycle changed"):
            self._collect_pfr(
                client,
                lambda: (_ for _ in ()).throw(
                    RuntimeError("lifecycle changed")
                ),
            )

        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan"],
        )
        self.assertIn(
            ("owner/repo", "src/service.py", "abcdef123456"),
            self.runtime.read_calls,
        )

    def test_before_final_runs_after_deep_and_before_final_exactly_once(self):
        client = _Client(
            [
                _response("complete staff-level memo"),
                _response('{"version":"presentation_v1"}'),
            ]
        )
        observations = []

        def before_final():
            observations.append(len(client.calls))

        with patch.object(
            generation,
            "compile_presentation_v1",
            return_value=_publishable(),
        ):
            result = generation.generate_review(
                "PR intent",
                "## PFR Review Context\nExact-head evidence",
                client=client,
                context_meta={},
                before_final=before_final,
            )

        self.assertTrue(result["review_publishable"])
        self.assertEqual(observations, [1])
        self.assertEqual(len(client.calls), 2)

    def test_before_final_failure_propagates_without_constructing_or_calling_final(self):
        client = _Client([_response("complete staff-level memo")])

        with patch.object(generation, "_final_messages") as final_messages:
            with self.assertRaisesRegex(RuntimeError, "review lifecycle ended"):
                generation.generate_review(
                    "PR intent",
                    "## PFR Review Context\nExact-head evidence",
                    client=client,
                    context_meta={},
                    before_final=lambda: (_ for _ in ()).throw(
                        RuntimeError("review lifecycle ended")
                    ),
                )

        final_messages.assert_not_called()
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()

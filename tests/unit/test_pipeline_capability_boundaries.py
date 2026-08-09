"""Characterize the stable capability boundaries around the phase runner."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
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
install_fake_jwt_module()
install_fake_requests_module()
install_fake_aws_modules(FakeDynamoResource())

from lambdas.LlamaPReviewPipeline import (  # noqa: E402
    orchestrator,
    pipeline_admission,
    pipeline_publication,
)
from lambdas.LlamaPReviewPipeline.errors import (  # noqa: E402
    PhaseClaimUnavailable,
)
from lambdas.LlamaPReviewPipeline.review.publish import (  # noqa: E402
    PreparedGitHubReview,
)
from lambdas.LlamaPReviewPipeline.review import (  # noqa: E402
    github_publication_surface,
    publication,
    publication_candidate,
    publish,
)


class _Runtime:
    def __init__(self) -> None:
        self.repository = object()

    def get_pr_head_snapshot(self, _repo, _pr_number):
        return {"head_sha": "head", "state": "open", "merged": False}

    def get_repository(self, _repo):
        return self.repository


def _publication_context(*, dry_run: bool):
    return pipeline_publication.PublicationContext(
        repo="owner/repo",
        pr_number=7,
        head_sha="head",
        expected_status="PENDING",
        phase="context",
        run_id="run",
        generation_attempt=1,
        runtime_identity={"request_id": "request"},
        phase_claim={
            "owner_id": "owner",
            "stream_event_id": "event",
            "attempt": 1,
        },
        dry_run=dry_run,
    )


class PipelineCapabilityBoundariesTest(unittest.TestCase):
    def test_admission_forwards_exact_delivery_fence(self):
        delivery = {
            "eligible": True,
            "current_item": {"status": "PENDING"},
            "phase_claim": {
                "owner_id": "owner",
                "stream_event_id": "event",
                "attempt": 3,
            },
            "claim_valid": True,
        }
        with patch.object(
            pipeline_admission.persistence,
            "claim_current_phase_delivery",
            return_value=delivery,
        ) as claim:
            admitted = pipeline_admission.claim_phase_delivery(
                "owner/repo",
                7,
                phase="context",
                expected_status="PENDING",
                runtime_identity={"request_id": "request"},
                stream_event_id="event",
                table="table",
            )

        self.assertEqual(admitted.attempt, 3)
        self.assertEqual(admitted.phase_claim["stream_event_id"], "event")
        claim.assert_called_once_with(
            "owner/repo",
            7,
            "context",
            expected_status="PENDING",
            runtime_identity={"request_id": "request"},
            stream_event_id="event",
            table="table",
        )

    def test_admission_rejects_an_active_foreign_owner(self):
        delivery = {
            "eligible": True,
            "current_item": {"status": "PENDING"},
            "phase_claim": None,
            "claim_valid": False,
        }
        with patch.object(
            pipeline_admission.persistence,
            "claim_current_phase_delivery",
            return_value=delivery,
        ):
            with self.assertRaises(PhaseClaimUnavailable):
                pipeline_admission.claim_phase_delivery(
                    "owner/repo",
                    7,
                    phase="context",
                    expected_status="PENDING",
                    runtime_identity={},
                    stream_event_id="event",
                )

    def test_publication_context_rejects_phase_status_mismatch(self):
        with self.assertRaisesRegex(ValueError, "do not agree"):
            pipeline_publication.PublicationContext(
                repo="owner/repo",
                pr_number=7,
                head_sha="head",
                expected_status="CONTEXT_READY",
                phase="context",
                run_id="run",
                generation_attempt=1,
                runtime_identity={},
                phase_claim={},
                dry_run=False,
            )

    def test_dry_run_stores_without_entering_live_transaction(self):
        runtime = _Runtime()
        prepared = PreparedGitHubReview(
            head_sha="head",
            main_body="body",
            comments=(),
            artifact={"main_comment": "body", "review_mode": "final"},
        )
        with patch.object(
            pipeline_publication,
            "publish_prepared_transaction",
        ) as live, patch.object(
            pipeline_publication.persistence,
            "store_review_result",
            return_value=True,
        ) as store:
            committed = pipeline_publication.commit_prepared(
                prepared,
                {"result_kind": "terminal"},
                context=_publication_context(dry_run=True),
                runtime=runtime,
                deadline=None,
                pre_persist_stage="context.pre_persist",
            )

        self.assertTrue(committed)
        live.assert_not_called()
        store.assert_called_once()

    def test_live_result_enters_the_single_publication_transaction(self):
        runtime = _Runtime()
        prepared = PreparedGitHubReview(
            head_sha="head",
            main_body="body",
            comments=(),
            artifact={"main_comment": "body", "review_mode": "final"},
        )
        with patch.object(
            pipeline_publication,
            "publish_prepared_transaction",
            return_value=True,
        ) as live:
            committed = pipeline_publication.commit_prepared(
                prepared,
                {},
                context=_publication_context(dry_run=False),
                runtime=runtime,
                deadline=None,
                pre_persist_stage="context.pre_persist",
            )

        self.assertTrue(committed)
        live.assert_called_once()
        self.assertIs(live.call_args.kwargs["repo_obj"], runtime.repository)

    def test_orchestrator_owns_only_the_two_phase_control_paths(self):
        source_path = Path(orchestrator.__file__).resolve()
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        public_phase_runners = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("run_")
        ]
        self.assertEqual(
            public_phase_runners,
            ["run_context_phase", "run_review_phase"],
        )
        for displaced_owner in (
            "publish_prepared_transaction",
            "recover_publication_transaction",
            "post_prepared_review",
            "store_publication_intent",
            "def _current_pr_snapshot",
            "def _review_generation_fields",
        ):
            with self.subTest(displaced_owner=displaced_owner):
                self.assertNotIn(displaced_owner, source)

    def test_live_github_effect_has_one_private_source_owner(self):
        package_root = Path(orchestrator.__file__).resolve().parent
        source_files = sorted(package_root.rglob("*.py"))
        create_calls = []
        dispatch_calls = []
        for path in source_files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "create_review"
                ):
                    create_calls.append((path, node.lineno))
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "_dispatch_exact_review"
                ):
                    dispatch_calls.append((path, node.lineno))

        surface_path = Path(
            github_publication_surface.__file__
        ).resolve()
        coordinator_path = Path(publication.__file__).resolve()
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0][0], surface_path)

        surface_tree = ast.parse(
            surface_path.read_text(encoding="utf-8")
        )
        private_dispatch = next(
            node
            for node in surface_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_dispatch_exact_review"
        )
        self.assertLessEqual(
            private_dispatch.lineno,
            create_calls[0][1],
        )
        self.assertLessEqual(
            create_calls[0][1],
            private_dispatch.end_lineno,
        )
        self.assertEqual(len(dispatch_calls), 1)
        self.assertEqual(dispatch_calls[0][0], coordinator_path)

        publish_tree = ast.parse(
            Path(publish.__file__).read_text(encoding="utf-8")
        )
        top_level_functions = {
            node.name
            for node in publish_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertTrue(
            {
                "post_pr_review",
                "post_prepared_review",
                "publish_main_comment",
                "publish",
            }.isdisjoint(top_level_functions)
        )

        def imported_modules(module) -> set[str]:
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            return {
                node.module or ""
                for node in tree.body
                if isinstance(node, ast.ImportFrom)
            }

        self.assertNotIn(
            "github_publication_surface",
            imported_modules(publication_candidate),
        )
        self.assertNotIn(
            "publication",
            imported_modules(github_publication_surface),
        )


if __name__ == "__main__":
    unittest.main()

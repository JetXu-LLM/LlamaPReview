"""Production-shaped local proof for the hosted public publication boundary.

No network or AWS service is used.  The test starts from the real Webhook
router, carries its item through both pipeline phases, and asserts the actual
GitHub publication seam rather than a prompt or status substring.
"""

from __future__ import annotations

import copy
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
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
fake_dynamo = FakeDynamoResource()
install_fake_aws_modules(fake_dynamo)

from lambdas.LlamaPReviewPipeline import orchestrator
from lambdas.LlamaPReviewPipeline.context_engine.repo_structure import RepoInventory
from lambdas.LlamaPReviewWebhookHandler import lambda_function as webhook


HEAD = "abc123def456"


def _reported_review_phase() -> dict:
    operation = {
        "run_id": f"owner/repo#42@{HEAD}",
        "head_sha": HEAD,
        "pipeline_phase": "review",
        "pipeline_attempt": 1,
        "phase": "deep_judgment",
        "call_index": 1,
    }
    from lambdas.LlamaPReviewPipeline.provider_accounting import sha256_value

    operation_id = sha256_value(operation)
    return {
        **operation,
        "schema_version": 2,
        "operation_id": operation_id,
        "call_id": sha256_value(
            {"operation_id": operation_id, "transport_attempt_index": 1}
        ),
        "transport_attempt_index": 1,
        "transport_dispatch_count": 1,
        "transport_attempt_count": 1,
        "model": "deepseek-v4-pro",
        "logical_model": "deepseek-v4-pro",
        "billed_model": "deepseek-v4-flash",
        "status": "completed",
        "finish_reason": "stop",
        "usage_state": "reported",
        "usage": {
            "prompt_tokens": 80,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 80,
            "completion_tokens": 20,
            "total_tokens": 100,
        },
    }


def _payload():
    return {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "state": "open",
            "draft": False,
            "title": "Change one contract",
            "head": {"sha": HEAD, "ref": "feature"},
            "base": {"ref": "main"},
        },
        "repository": {
            "full_name": "owner/repo",
            "private": False,
            "default_branch": "main",
        },
        "installation": {"id": 999},
    }


def _content():
    return {
        "pr_metadata": {
            "number": 42,
            "title": "Change one contract",
            "head_sha": HEAD,
        },
        "file_changes": [
            {
                "file_path": "src/app.py",
                "change_type": "modified",
                "language": "Python",
                "additions": 1,
                "deletions": 1,
                "changes": 2,
                "diff": "@@ -1 +1 @@\n-old\n+new_value = 1\n",
            }
        ],
        "interactions": [],
        "ci_cd_results": {"head_sha": HEAD, "check_runs": []},
    }


class _Pull:
    def __init__(self):
        self.reviews = []
        self.head = SimpleNamespace(sha=HEAD)
        self.state = "open"
        self.merged = False
        self.locked = False

    def get_files(self):
        return []

    def get_reviews(self):
        return list(self.reviews)

    def get_review_comments(self):
        return []

    def create_review(self, **kwargs):
        review_id = len(self.reviews) + 1
        self.reviews.append(
            {
                **kwargs,
                "id": review_id,
                "commit_id": kwargs["commit"].sha,
                "user": {"login": "llamapreview[bot]"},
                "submitted_at": datetime.now(timezone.utc),
                "state": "COMMENTED",
            }
        )
        return SimpleNamespace(
            id=review_id,
            commit_id=kwargs["commit"].sha,
        )


class _Runtime:
    def __init__(self):
        self.content = _content()
        self.pull = _Pull()

    def get_pr_content(self, *_args, **_kwargs):
        content = copy.deepcopy(self.content)
        if self.pull.reviews:
            content["interactions"] = [
                {
                    "author": "llamapreview[bot]",
                    "body": self.pull.reviews[-1].get("body", ""),
                }
            ]
        return content

    def get_pr_head_snapshot(self, *_args, **_kwargs):
        return {"head_sha": HEAD, "state": "open", "merged": False}

    def get_ci_results_for_head(
        self, _repo, head_sha, *, include_actionable_details=True
    ):
        return {"head_sha": head_sha, "check_runs": []}

    def get_repository(self, _repo):
        return SimpleNamespace(
            description="fixture repository",
            repo=SimpleNamespace(
                description="fixture repository",
                get_pull=lambda _number: self.pull,
                get_commit=lambda *, sha: SimpleNamespace(sha=sha),
            ),
        )

    def get_file_content(self, *_args, **_kwargs):
        return "new_value = 1\n"


class HostedPublicPathTest(unittest.TestCase):
    def setUp(self):
        # unittest discovery imports every test module before executing the
        # classes.  Each module installs its own fake boto3 resource, so never
        # rely on the table captured when the shared Webhook module happened to
        # be imported.  Rebind explicitly and restore it after every test.
        original_dynamodb = webhook.dynamodb
        original_table = webhook.table
        webhook.dynamodb = fake_dynamo
        webhook.table = fake_dynamo.Table("llamapreview-pipeline-test")
        self.addCleanup(setattr, webhook, "table", original_table)
        self.addCleanup(setattr, webhook, "dynamodb", original_dynamodb)
        artifact_bucket = patch.object(
            orchestrator.config,
            "PUBLICATION_ARTIFACT_BUCKET",
            "pipeline-publication-artifacts",
        )
        artifact_bucket.start()
        self.addCleanup(artifact_bucket.stop)
        webhook.table.reset()

    def _run_pipeline(self, item, *, runtime):
        inventory = RepoInventory(
            repository="owner/repo",
            requested_sha=HEAD,
            status="complete",
            discoverable_files={"src/app.py"},
        )
        with patch.object(
            orchestrator.config, "DRY_RUN", False
        ), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "fetch_repo_inventory", return_value=inventory
        ), patch.object(
            orchestrator,
            "analyze_pr_complexity",
            return_value={
                "complexity": "low",
                "reason": "The changed region is self-contained.",
                "pr_type": "code",
                "risk_domains": [],
            },
        ):
            orchestrator.run_context_phase(
                item,
                table=webhook.table,
                runtime=runtime,
                stream_event_id="event-context",
            )

        context_ready = webhook.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 42}
        )["Item"]
        generated = {
            "pr_review_comment": "No review blocker found in the reviewed change.",
            "inline_comments": [],
            "review_generation_status": "complete",
            "review_fallback_used": False,
            "review_publishable": True,
            "review_publication_safe": True,
            "quality_scoreable": True,
            "quality_exclusion_reasons": [],
            "review_model_phases": [_reported_review_phase()],
        }
        with patch.object(
            orchestrator.config, "DRY_RUN", False
        ), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "generate_review", return_value=generated
        ):
            orchestrator.run_review_phase(
                context_ready,
                table=webhook.table,
                runtime=runtime,
                stream_event_id="event-review",
            )
        return webhook.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 42}
        )["Item"]

    def test_public_webhook_reaches_exactly_one_create_review(self):
        runtime = _Runtime()
        item = webhook.process_public_pull_request(
            _payload(), delivery_id="delivery-public"
        )
        self.assertIsNotNone(item)
        self.assertNotIn("dry_run", item)

        terminal = self._run_pipeline(item, runtime=runtime)

        self.assertEqual(terminal["status"], "PROCESSED")
        self.assertEqual(len(runtime.pull.reviews), 1)

    def test_explicit_item_dry_run_runs_pipeline_with_zero_github_write(self):
        runtime = _Runtime()
        item = webhook.build_pipeline_item(
            _payload(), delivery_id="delivery-dry-run"
        )
        item["dry_run"] = True
        webhook.save_to_dynamodb(item)

        terminal = self._run_pipeline(item, runtime=runtime)

        self.assertEqual(terminal["status"], "PROCESSED_DRYRUN")
        self.assertEqual(runtime.pull.reviews, [])

    def test_terminal_live_results_recover_without_republication_after_persist_failure(self):
        cases = ("empty", "policy_skip")
        for terminal_kind in cases:
            with self.subTest(terminal_kind=terminal_kind):
                webhook.table.reset()
                runtime = _Runtime()
                if terminal_kind == "empty":
                    runtime.content["file_changes"] = []
                item = webhook.process_public_pull_request(
                    _payload(), delivery_id=f"delivery-{terminal_kind}"
                )

                original_store = orchestrator.persistence.store_review_result
                with patch.object(
                    orchestrator.config, "DRY_RUN", False
                ), patch.object(
                    orchestrator.pipeline_admission, "installation_token", return_value="token"
                ), patch.object(
                    orchestrator,
                    "fetch_repo_inventory",
                    return_value=RepoInventory(
                        repository="owner/repo",
                        requested_sha=HEAD,
                        status="complete",
                        discoverable_files={"src/app.py"},
                    ),
                ), patch.object(
                    orchestrator,
                    "analyze_pr_complexity",
                    return_value={
                        "complexity": "skip",
                        "reason": "A bounded policy skip.",
                        "pr_type": "docs",
                        "verification_plan": [],
                    },
                ), patch.object(
                    orchestrator.persistence,
                    "store_review_result",
                    side_effect=RuntimeError("simulated terminal persist failure"),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "simulated terminal persist failure"
                    ):
                        orchestrator.run_context_phase(
                            item,
                            table=webhook.table,
                            runtime=runtime,
                            stream_event_id=f"event-{terminal_kind}",
                        )

                self.assertEqual(len(runtime.pull.reviews), 1)
                current = webhook.table.get_item(
                    Key={"repo": "owner/repo", "pr_number": 42}
                )["Item"]
                self.assertEqual(current["status"], "PENDING")
                with patch.object(
                    orchestrator.config, "DRY_RUN", False
                ), patch.object(
                    orchestrator.pipeline_admission, "installation_token", return_value="token"
                ), patch.object(
                    orchestrator.persistence,
                    "store_review_result",
                    original_store,
                ):
                    orchestrator.run_context_phase(
                        current,
                        table=webhook.table,
                        runtime=runtime,
                        stream_event_id=f"event-{terminal_kind}",
                    )

                self.assertEqual(len(runtime.pull.reviews), 1)
                terminal = webhook.table.get_item(
                    Key={"repo": "owner/repo", "pr_number": 42}
                )["Item"]
                self.assertEqual(terminal["status"], "PROCESSED")
                self.assertEqual(terminal["publication_status"], "published")
                self.assertEqual(terminal["github_review_id"], 1)
                self.assertEqual(
                    terminal["publication_receipt"]["outcome"],
                    "adopted",
                )

    def test_oversized_nonempty_pr_uses_normal_exactly_once_publication_path(self):
        runtime = _Runtime()
        runtime.content["file_changes"][0]["diff"] = (
            "@@ -1 +1 @@\n-old\n"
            + "\n".join(
                f"+changed_{index} = {index}" for index in range(900)
            )
        )
        item = webhook.process_public_pull_request(
            _payload(), delivery_id="delivery-oversized"
        )

        with patch.object(
            orchestrator.config, "PR_DETAILS_MAX_CHARS", 4_000
        ), patch.object(
            orchestrator.config, "LARGE_PR_MAX_CHARS", 8_000
        ):
            terminal = self._run_pipeline(item, runtime=runtime)

        self.assertEqual(terminal["status"], "PROCESSED")
        self.assertTrue(terminal["context_meta"]["pr_details_compacted"])
        self.assertEqual(len(runtime.pull.reviews), 1)
        self.assertNotIn("Review not run", runtime.pull.reviews[0]["body"])


if __name__ == "__main__":
    unittest.main()

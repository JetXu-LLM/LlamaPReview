from contextlib import redirect_stdout
from decimal import Decimal
from datetime import datetime, timezone
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

from lambdas.LlamaPReviewPipeline import (
    orchestrator,
    persistence,
    pipeline_accounting,
    pipeline_ci,
)
from lambdas.LlamaPReviewPipeline.errors import (
    GitHubAuthConfigurationError,
    HeadVerificationUnavailable,
    PhaseClaimUnavailable,
    PublicationStateConflict,
    ReviewGenerationIncomplete,
)
from lambdas.LlamaPReviewPipeline.deepseek_client import (
    DeepSeekClient,
    ProviderCallLedgerError,
)
from lambdas.LlamaPReviewPipeline.provider_source_identity import (
    PROVIDER_SOURCE_SCHEMA,
    provider_source_receipt_sha256,
    sha256_value,
)
from lambdas.LlamaPReviewPipeline.review.projection import build_v3_review
from lambdas.LlamaPReviewPipeline.review import (
    github_publication_surface,
)


def _pr_content(path="src/app.py", diff="@@ -1 +1 @@\n-old\n+new_value = 1\n", title="change"):
    return {
        "pr_metadata": {"number": 7, "title": title, "head_sha": "abcdef123456"},
        "file_changes": [
            {
                "file_path": path,
                "change_type": "modified",
                "language": "Python",
                "additions": 1,
                "deletions": 1,
                "changes": 2,
                "change_categories": ["logic"],
                "diff": diff,
            }
        ],
        "interactions": [],
    }


def _reported_review_phase(*, pipeline_attempt: int = 1) -> dict:
    operation = {
        "run_id": "owner/repo#7@abcdef123456",
        "head_sha": "abcdef123456",
        "pipeline_phase": "review",
        "pipeline_attempt": pipeline_attempt,
        "phase": "deep_judgment",
        "call_index": 1,
    }
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


def _publishable_review(
    comment: str = "summary",
    *,
    pipeline_attempt: int = 1,
) -> dict:
    return {
        "pr_review_comment": comment,
        "inline_comments": [],
        "review_generation_status": "complete",
        "review_fallback_used": False,
        "review_publishable": True,
        "review_publication_safe": True,
        "review_model_phases": [
            _reported_review_phase(pipeline_attempt=pipeline_attempt)
        ],
    }


def _dependency_bundle_content(*, status="completed", conclusion="success"):
    content = _pr_content(title="bump application dependencies and build toolchain")
    content["file_changes"] = [
        {
            "file_path": path,
            "change_type": "modified",
            "additions": 1,
            "deletions": 1,
            "diff": diff,
        }
        for path, diff in (
            (
                "mobile/app/build.gradle.kts",
                '@@ -1 +1 @@\n-implementation("runtime-lib:1.2.3")\n+implementation("runtime-lib:1.3.0")\n',
            ),
            (
                "mobile/benchmark/build.gradle.kts",
                '@@ -1 +1 @@\n-implementation("test-lib:2.1.0")\n+implementation("test-lib:2.2.0")\n',
            ),
            (
                "mobile/build.gradle.kts",
                '@@ -1 +1 @@\n-id("compiler.plugin") version "3.1.0"\n+id("compiler.plugin") version "3.2.0"\n',
            ),
            (
                "mobile/gradle/wrapper/gradle-wrapper.jar",
                "[SKIPPED] File type not suitable for diff analysis",
            ),
            (
                "mobile/gradle/wrapper/gradle-wrapper.properties",
                "@@ -1 +1 @@\n-distributionUrl=tool-4.1.0.zip\n+distributionUrl=tool-4.2.0.zip\n",
            ),
            (
                "mobile/gradlew",
                "@@ -1 +1 @@\n-old generated launcher\n+new generated launcher\n",
            ),
            (
                "mobile/gradlew.bat",
                "@@ -1 +1 @@\n-old generated launcher\n+new generated launcher\n",
            ),
        )
    ]
    content["ci_cd_results"] = {
        "head_sha": "abcdef123456",
        "check_runs": [
            {
                "id": 901,
                "name": "validation",
                "status": status,
                "conclusion": conclusion,
                "details_url": "https://example.test/check/901",
            }
        ],
        "_retrieval_meta": {"ci_aggregate": {"outcome": "ok"}},
    }
    return content


class _Pull:
    def __init__(self):
        self.reviews = []
        self.review_resources = []
        self.review_comments = []

    def get_files(self):
        return []

    def create_review(self, **kwargs):
        self.reviews.append(kwargs)
        review_id = len(self.reviews)
        review = SimpleNamespace(
            id=review_id,
            commit_id=kwargs["commit"].sha,
            body=kwargs["body"],
            user=SimpleNamespace(login="llamapreview[bot]"),
            submitted_at=datetime.now(timezone.utc),
            state="COMMENTED",
        )
        inline_ids = []
        for index, comment in enumerate(kwargs.get("comments") or []):
            comment_id = review_id * 1000 + index + 1
            inline_ids.append(comment_id)
            self.review_comments.append(
                SimpleNamespace(
                    id=comment_id,
                    pull_request_review_id=review_id,
                    user=SimpleNamespace(
                        login="llamapreview[bot]"
                    ),
                    **comment,
                )
            )
        review.raw_data = {
            "comments": [
                {"id": comment_id} for comment_id in inline_ids
            ]
        }
        self.review_resources.append(review)
        return review

    def get_reviews(self):
        return list(self.review_resources)

    def get_review_comments(self):
        return list(self.review_comments)


class _Runtime:
    def __init__(self, content=None, *, head_sha="abcdef123456"):
        self.content = content or _pr_content()
        self.head_sha = head_sha
        self.pull = _Pull()

    def get_pr_content(self, repo_full_name, pr_number, *, context_lines=10, force_update=True):
        return self.content

    def get_repository(self, repo_full_name):
        return SimpleNamespace(
            repo=SimpleNamespace(
                description="Repo",
                get_pull=lambda _number: self.pull,
                get_commit=lambda *, sha: SimpleNamespace(sha=sha),
            )
        )

    def get_file_content(self, repo_full_name, path, *, sha=None):
        return "new_value = 1\n"

    def get_pr_head_sha(self, repo_full_name, pr_number):
        return self.head_sha

    def get_pr_head_snapshot(self, repo_full_name, pr_number):
        return {
            "head_sha": self.get_pr_head_sha(repo_full_name, pr_number),
            "state": "open",
            "merged": False,
        }

    def get_ci_results_for_head(
        self, repo_full_name, head_sha, *, include_actionable_details=True
    ):
        snapshot = dict(self.content.get("ci_cd_results") or {})
        snapshot.setdefault("head_sha", head_sha)
        return snapshot

    def search_code(self, query, repo_full_name):
        return []


class _ChangingHeadRuntime(_Runtime):
    def __init__(self, heads, content=None):
        super().__init__(content=content)
        self.heads = list(heads)

    def get_pr_head_sha(self, repo_full_name, pr_number):
        if len(self.heads) > 1:
            return self.heads.pop(0)
        return self.heads[0]


class _PostWriteChangingHeadRuntime(_Runtime):
    def get_pr_head_sha(self, repo_full_name, pr_number):
        return "new-head-after-write" if self.pull.reviews else self.head_sha


class _LifecycleRuntime(_Runtime):
    def __init__(self, *, state="open", merged=False, head_sha="abcdef123456", content=None):
        super().__init__(content=content, head_sha=head_sha)
        self.state = state
        self.merged = merged
        self.ci_calls = 0

    def get_pr_head_snapshot(self, repo_full_name, pr_number):
        return {
            "head_sha": self.head_sha,
            "state": self.state,
            "merged": self.merged,
        }

    def get_ci_results_for_head(
        self, repo_full_name, head_sha, *, include_actionable_details=True
    ):
        self.ci_calls += 1
        return super().get_ci_results_for_head(
            repo_full_name,
            head_sha,
            include_actionable_details=include_actionable_details,
        )


class _SequenceLifecycleRuntime(_Runtime):
    def __init__(self, snapshots, *, content=None):
        super().__init__(content=content)
        self.snapshots = [dict(snapshot) for snapshot in snapshots]

    def get_pr_head_snapshot(self, repo_full_name, pr_number):
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return dict(self.snapshots[0])


class TestOrchestratorPipeline(unittest.TestCase):
    def setUp(self):
        self.table = fake_dynamo.Table("llamapreview-pipeline-test")
        self.table.reset()

    def test_canonical_phase_ledger_and_all_attempt_usage_accounting(self):
        route = [
            {
                "phase": "pr_analyzer",
                "attempt": 1,
                "usage": {"total_tokens": 5},
            }
        ]
        pfr = [
            {
                "phase": "route",
                "attempt": 1,
                "usage": {"total_tokens": 10},
            },
            {
                "phase": "pfr_plan",
                "attempt": 1,
                "usage": {"total_tokens": 20},
            },
        ]

        self.assertEqual(
            [
                item["phase"]
                for item in pipeline_accounting.canonical_context_model_phases(
                    "high",
                    route_model_phases=route,
                    pfr_model_phases=pfr,
                )
            ],
            ["route", "pfr_plan"],
        )
        for mode in ("low", "skip"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    [
                        item["phase"]
                        for item in pipeline_accounting.canonical_context_model_phases(
                            mode,
                            route_model_phases=route,
                            pfr_model_phases=pfr,
                        )
                    ],
                    ["route"],
                )

        def call(
            suffix,
            *,
            pipeline_phase,
            pipeline_attempt,
            phase,
            tokens=None,
        ):
            return {
                "call_id": suffix * 64,
                "pipeline_phase": pipeline_phase,
                "pipeline_attempt": pipeline_attempt,
                "phase": phase,
                "call_index": 1,
                "usage_state": "reported" if tokens is not None else "unreported",
                "usage": (
                    {
                        "prompt_tokens": tokens,
                        "completion_tokens": 0,
                        "total_tokens": tokens,
                    }
                    if tokens is not None
                    else {}
                ),
            }

        calls = [
            call(
                "a",
                pipeline_phase="context",
                pipeline_attempt=1,
                phase="route",
                tokens=5,
            ),
            call(
                "b",
                pipeline_phase="context",
                pipeline_attempt=2,
                phase="route",
                tokens=10,
            ),
            call(
                "c",
                pipeline_phase="context",
                pipeline_attempt=2,
                phase="pfr_plan",
                tokens=20,
            ),
            call(
                "d",
                pipeline_phase="review",
                pipeline_attempt=1,
                phase="deep_thinking",
                tokens=30,
            ),
            call(
                "e",
                pipeline_phase="review",
                pipeline_attempt=2,
                phase="deep_thinking",
                tokens=40,
            ),
            call(
                "f",
                pipeline_phase="review",
                pipeline_attempt=2,
                phase="final_output",
                tokens=None,
            ),
        ]
        accounting = pipeline_accounting.provider_usage_accounting(
            provider_calls=calls,
            fallback_winning_phases=[],
            context_attempt=2,
            review_attempt=2,
        )

        self.assertEqual(
            accounting["deepseek_usage_total"]["total_tokens"],
            105,
        )
        self.assertEqual(
            accounting["deepseek_winning_usage_total"]["total_tokens"],
            70,
        )
        self.assertEqual(
            accounting["deepseek_discarded_usage_total"]["total_tokens"],
            35,
        )
        self.assertEqual(
            [
                item["call_id"]
                for item in accounting["deepseek_model_phases"]
            ],
            ["b" * 64, "c" * 64, "e" * 64, "f" * 64],
        )
        self.assertEqual(
            [
                item["call_id"]
                for item in accounting[
                    "deepseek_discarded_model_phases"
                ]
            ],
            ["a" * 64, "d" * 64],
        )
        self.assertEqual(
            accounting["deepseek_usage_accounting"],
            {
                "schema_version": 2,
                "all_call_count": 6,
                "transport_operation_count": 6,
                "winning_call_count": 4,
                "discarded_call_count": 2,
                "unreported_usage_call_count": 1,
                "usage_merge_conflicts": [],
                "complete_numeric_usage": False,
            },
        )

    def test_representation_repair_durable_call_and_fallback_phase_count_once(self):
        durable_repair = {
            "call_id": "a" * 64,
            "pipeline_phase": "context",
            "pipeline_attempt": 2,
            "phase": "pfr_reconcile_representation_repair",
            "call_index": 1,
            "model": "pro",
            "usage_state": "reported",
            "usage": {
                "prompt_tokens": 70,
                "completion_tokens": 30,
                "total_tokens": 100,
            },
        }
        fallback_repair = {
            "phase": "pfr_reconcile_representation_repair",
            "round": 1,
            "attempt": 2,
            "model": "pro",
            "usage": {
                "prompt_tokens": 70,
                "completion_tokens": 30,
                "total_tokens": 100,
            },
        }

        accounting = pipeline_accounting.provider_usage_accounting(
            provider_calls=[durable_repair],
            fallback_winning_phases=[fallback_repair],
            context_attempt=2,
            review_attempt=None,
        )

        self.assertEqual(
            [
                item["phase"]
                for item in accounting["deepseek_all_attempt_model_phases"]
            ],
            ["pfr_reconcile_representation_repair"],
        )
        self.assertEqual(
            accounting["deepseek_usage_total"],
            {
                "prompt_tokens": 70,
                "completion_tokens": 30,
                "total_tokens": 100,
            },
        )
        self.assertEqual(
            accounting["deepseek_winning_usage_total"],
            accounting["deepseek_usage_total"],
        )
        self.assertEqual(accounting["deepseek_discarded_usage_total"], {})
        self.assertEqual(
            accounting["deepseek_usage_accounting"],
            {
                "schema_version": 2,
                "all_call_count": 1,
                "transport_operation_count": 1,
                "winning_call_count": 1,
                "discarded_call_count": 0,
                "unreported_usage_call_count": 0,
                "usage_merge_conflicts": [],
                "complete_numeric_usage": True,
            },
        )

    def test_phase_ledger_orders_modern_judgment_before_presentation_repair(self):
        phases = [
            {"phase": "final_presentation_repair", "call_id": "f" * 64},
            {"phase": "final_output", "call_id": "c" * 64},
            {"phase": "deep_judgment", "call_id": "b" * 64},
            {"phase": "final_presentation", "call_id": "d" * 64},
            {"phase": "deep_thinking", "call_id": "a" * 64},
        ]

        self.assertEqual(
            [
                item["phase"]
                for item in pipeline_accounting.sort_model_phases(phases)
            ],
            [
                "deep_thinking",
                "deep_judgment",
                "final_output",
                "final_presentation",
                "final_presentation_repair",
            ],
        )

    def test_provider_usage_supplements_partial_durable_ledger_without_duplication(
        self,
    ):
        provider_deep = {
            "call_id": "a" * 64,
            "pipeline_phase": "review",
            "pipeline_attempt": 2,
            "phase": "deep_thinking",
            "call_index": 1,
            "model": "pro",
            "usage_state": "reported",
            "usage": {"total_tokens": 30},
        }
        fallback_route = {
            "phase": "route",
            "attempt": 1,
            "model": "flash",
            "usage": {"total_tokens": 10},
        }
        fallback_deep = {
            "phase": "deep_thinking",
            "attempt": 1,
            "model": "pro",
            "usage": {"total_tokens": 30},
        }

        accounting = pipeline_accounting.provider_usage_accounting(
            provider_calls=[provider_deep],
            fallback_winning_phases=[fallback_route, fallback_deep],
            context_attempt=2,
            review_attempt=2,
        )

        self.assertEqual(
            accounting["deepseek_usage_total"]["total_tokens"],
            40,
        )
        self.assertEqual(
            accounting["deepseek_winning_usage_total"]["total_tokens"],
            40,
        )
        self.assertEqual(
            accounting["deepseek_discarded_usage_total"],
            {},
        )
        self.assertEqual(
            [
                (item["pipeline_phase"], item["phase"])
                for item in accounting["deepseek_model_phases"]
            ],
            [
                ("context", "route"),
                ("review", "deep_thinking"),
            ],
        )
        self.assertEqual(
            accounting["deepseek_usage_accounting"]["all_call_count"],
            2,
        )

    def test_provider_usage_aggregates_dynamodb_decimal_ledger_records(self):
        calls = [
            {
                "call_id": "a" * 64,
                "pipeline_phase": "context",
                "pipeline_attempt": Decimal("1"),
                "phase": "route",
                "call_index": Decimal("1"),
                "usage_state": "reported",
                "usage": {
                    "prompt_tokens": Decimal("11036"),
                    "completion_tokens": Decimal("1836"),
                    "total_tokens": Decimal("12872"),
                    "prompt_tokens_details": {
                        "cached_tokens": Decimal("2432"),
                    },
                },
            },
            {
                "call_id": "b" * 64,
                "pipeline_phase": "review",
                "pipeline_attempt": Decimal("1"),
                "phase": "final_output",
                "call_index": Decimal("1"),
                "usage_state": "reported",
                "usage": {
                    "prompt_tokens": Decimal("20221"),
                    "completion_tokens": Decimal("3935"),
                    "total_tokens": Decimal("24156"),
                    "prompt_tokens_details": {
                        "cached_tokens": Decimal("1408"),
                    },
                },
            },
        ]

        accounting = pipeline_accounting.provider_usage_accounting(
            provider_calls=calls,
            fallback_winning_phases=[],
            context_attempt=1,
            review_attempt=1,
        )

        self.assertEqual(
            accounting["deepseek_usage_total"],
            {
                "prompt_tokens": 31257,
                "completion_tokens": 5771,
                "total_tokens": 37028,
                "prompt_tokens_details": {"cached_tokens": 3840},
            },
        )
        self.assertEqual(
            accounting["deepseek_winning_usage_total"],
            accounting["deepseek_usage_total"],
        )
        self.assertEqual(accounting["deepseek_discarded_usage_total"], {})
        self.assertEqual(
            accounting["deepseek_usage_accounting"],
            {
                "schema_version": 2,
                "all_call_count": 2,
                "transport_operation_count": 2,
                "winning_call_count": 2,
                "discarded_call_count": 0,
                "unreported_usage_call_count": 0,
                "usage_merge_conflicts": [],
                "complete_numeric_usage": True,
            },
        )

    def test_provider_usage_downgrades_reported_state_without_numeric_usage(self):
        accounting = pipeline_accounting.provider_usage_accounting(
            provider_calls=[
                {
                    "call_id": "a" * 64,
                    "pipeline_phase": "context",
                    "pipeline_attempt": Decimal("1"),
                    "phase": "route",
                    "call_index": Decimal("1"),
                    "usage_state": "reported",
                    "usage": {
                        "total_tokens": Decimal("NaN"),
                        "provider_note": "not numeric usage",
                    },
                }
            ],
            fallback_winning_phases=[],
            context_attempt=1,
            review_attempt=None,
        )

        self.assertEqual(accounting["deepseek_usage_total"], {})
        self.assertEqual(
            accounting["deepseek_model_phases"][0]["usage_state"],
            "unreported",
        )
        self.assertEqual(
            accounting["deepseek_usage_accounting"],
            {
                "schema_version": 2,
                "all_call_count": 1,
                "transport_operation_count": 1,
                "winning_call_count": 1,
                "discarded_call_count": 0,
                "unreported_usage_call_count": 1,
                "usage_merge_conflicts": [],
                "complete_numeric_usage": False,
            },
        )

    def test_provider_usage_rejects_partial_required_token_classes(self):
        accounting = pipeline_accounting.provider_usage_accounting(
            provider_calls=[
                {
                    "call_id": "a" * 64,
                    "pipeline_phase": "context",
                    "pipeline_attempt": 1,
                    "phase": "route",
                    "call_index": 1,
                    "usage_state": "reported",
                    "usage": {"total_tokens": 10},
                }
            ],
            fallback_winning_phases=[],
            context_attempt=1,
            review_attempt=None,
        )

        phase = accounting["deepseek_model_phases"][0]
        self.assertEqual(phase["usage_state"], "unreported")
        self.assertIn(
            "prompt_tokens_missing_or_non_integral",
            phase["usage_validation_errors"],
        )
        self.assertFalse(
            accounting["deepseek_usage_accounting"][
                "complete_numeric_usage"
            ]
        )

    def test_provider_usage_shape_conflict_is_explicitly_incomplete(self):
        calls = [
            {
                "call_id": "a" * 64,
                "pipeline_phase": "context",
                "pipeline_attempt": 1,
                "phase": "route",
                "call_index": 1,
                "usage_state": "reported",
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 1,
                    "total_tokens": 5,
                    "details": 2,
                },
            },
            {
                "call_id": "b" * 64,
                "pipeline_phase": "review",
                "pipeline_attempt": 1,
                "phase": "final_output",
                "call_index": 1,
                "usage_state": "reported",
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                    "details": {"cached_tokens": 1},
                },
            },
        ]
        accounting = pipeline_accounting.provider_usage_accounting(
            provider_calls=calls,
            fallback_winning_phases=[],
            context_attempt=1,
            review_attempt=1,
        )

        self.assertEqual(
            accounting["deepseek_usage_accounting"][
                "usage_merge_conflicts"
            ],
            ["details"],
        )
        self.assertFalse(
            accounting["deepseek_usage_accounting"][
                "complete_numeric_usage"
            ]
        )

    def test_provider_usage_supplements_a_non_prefix_call_hole(self):
        durable_round_two = {
            "call_id": "b" * 64,
            "pipeline_phase": "context",
            "pipeline_attempt": 2,
            "phase": "pfr_reconcile",
            "call_index": 2,
            "model": "pro",
            "usage_state": "reported",
            "usage": {"total_tokens": 20},
        }
        fallback_round_one = {
            "phase": "pfr_reconcile",
            "round": 1,
            "attempt": 1,
            "model": "pro",
            "usage": {"total_tokens": 10},
        }
        fallback_round_two = {
            "phase": "pfr_reconcile",
            "round": 2,
            "attempt": 1,
            "model": "pro",
            "usage": {"total_tokens": 20},
        }

        accounting = pipeline_accounting.provider_usage_accounting(
            provider_calls=[durable_round_two],
            fallback_winning_phases=[
                fallback_round_one,
                fallback_round_two,
            ],
            context_attempt=2,
            review_attempt=None,
        )

        self.assertEqual(
            accounting["deepseek_usage_total"]["total_tokens"],
            30,
        )
        self.assertEqual(
            [
                item.get("round") or item.get("call_index")
                for item in accounting["deepseek_model_phases"]
            ],
            [1, 2],
        )
        self.assertEqual(
            accounting["deepseek_usage_accounting"]["all_call_count"],
            2,
        )

    def _pending_item(self, pr_number=7, **extra):
        item = {
            "repo": "owner/repo",
            "pr_number": pr_number,
            "status": "PENDING",
            "installation_id": 123,
            "head_sha": "abcdef123456",
            "default_branch": "main",
        }
        item.update(extra)
        self.table.put_item(Item=item)
        return item

    def test_stale_stream_images_reread_current_state_without_explicit_table(self):
        cases = (
            (
                orchestrator.run_context_phase,
                {
                    "repo": "owner/repo",
                    "pr_number": 7,
                    "status": "PENDING",
                },
            ),
            (
                orchestrator.run_review_phase,
                {
                    "repo": "owner/repo",
                    "pr_number": 7,
                    "status": "CONTEXT_READY",
                },
            ),
        )
        for runner, stale_stream_item in cases:
            with self.subTest(phase=runner.__name__), patch.object(
                persistence,
                "get_item",
                return_value={
                    "repo": "owner/repo",
                    "pr_number": 7,
                    "status": "PROCESSED_DRYRUN",
                },
            ) as get_item, patch.object(
                persistence,
                "claim_phase_attempt",
            ) as claim_attempt:
                runner(stale_stream_item)

            get_item.assert_called_once_with(
                "owner/repo",
                7,
                table=None,
                consistent_read=True,
            )
            claim_attempt.assert_not_called()

    def test_overlapping_phase_delivery_exits_behind_active_lease(self):
        pending = self._pending_item()
        context_claim = persistence.claim_phase_attempt(
            "owner/repo",
            7,
            "context",
            expected_status="PENDING",
            runtime_identity={"aws_request_id": "context-owner"},
            owner_id="context-owner",
            table=self.table,
        )
        self.assertIsNotNone(context_claim)
        with patch.object(
            orchestrator.pipeline_admission,
            "installation_token",
        ) as get_token:
            with self.assertRaises(PhaseClaimUnavailable):
                orchestrator.run_context_phase(
                    pending,
                    table=self.table,
                    runtime=_Runtime(),
                    stream_event_id="foreign-context-event",
                )
        get_token.assert_not_called()
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["context_attempt"], 1)
        self.assertEqual(
            current["context_claim"]["owner_id"],
            "context-owner",
        )

        self.table.reset()
        self._pending_item()
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={},
            table=self.table,
        )
        context_ready = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        review_claim = persistence.claim_phase_attempt(
            "owner/repo",
            7,
            "review",
            expected_status="CONTEXT_READY",
            runtime_identity={"aws_request_id": "review-owner"},
            owner_id="review-owner",
            table=self.table,
        )
        self.assertIsNotNone(review_claim)
        with patch.object(orchestrator, "generate_review") as generate:
            with self.assertRaises(PhaseClaimUnavailable):
                orchestrator.run_review_phase(
                    context_ready,
                    table=self.table,
                    runtime=_Runtime(),
                    stream_event_id="foreign-review-event",
                )
        generate.assert_not_called()
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["review_attempt"], 1)
        self.assertEqual(
            current["review_claim"]["owner_id"],
            "review-owner",
        )

    def test_terminal_error_emf_is_dimensionless_and_contains_no_repository_identity(self):
        output = io.StringIO()

        with redirect_stdout(output), patch.object(orchestrator.time, "time", return_value=123.456):
            orchestrator._emit_terminal_error_metric(
                phase="review.finalize",
                kind="schema_validation_error",
            )

        event = json.loads(output.getvalue())
        self.assertEqual(
            set(event),
            {"_aws", "Phase", "FailureKind", "TerminalErrors"},
        )
        metric = event["_aws"]["CloudWatchMetrics"][0]
        self.assertEqual(metric["Namespace"], "LlamaPReview/Pipeline")
        self.assertEqual(metric["Dimensions"], [[]])
        self.assertEqual(metric["Metrics"], [{"Name": "TerminalErrors", "Unit": "Count"}])
        self.assertEqual(event["TerminalErrors"], 1)
        serialized = output.getvalue().lower()
        for private_key in ("repo", "repository", "pr_number", "head_sha", "run_id"):
            self.assertNotIn(private_key, serialized)

    def test_pre_execution_retry_budget_exhaustion_persists_and_emits_terminal_error(self):
        cases = (
            ("context", "PENDING", "context_attempt", orchestrator.run_context_phase),
            ("review", "CONTEXT_READY", "review_attempt", orchestrator.run_review_phase),
        )
        for phase, status, attempt_field, runner in cases:
            with self.subTest(phase=phase):
                self.table.reset()
                item = {
                    "repo": "owner/repo",
                    "pr_number": 7,
                    "status": status,
                    attempt_field: 3,
                }
                self.table.put_item(Item=item)
                with patch.object(
                    orchestrator.config, "MAX_ATTEMPTS", 3
                ), patch.object(
                    orchestrator, "_emit_terminal_error_metric"
                ) as emit:
                    runner(item, table=self.table)

                current = self.table.get_item(
                    Key={"repo": "owner/repo", "pr_number": 7}
                )["Item"]
                self.assertEqual(current["status"], "ERROR")
                self.assertEqual(current["error_kind"], "retry_budget_exhausted")
                self.assertTrue(current["error_retry_exhausted"])
                emit.assert_called_once_with(
                    phase=f"{phase}.start",
                    kind="retry_budget_exhausted",
                )

    def test_github_signing_configuration_failure_is_terminal_on_first_attempt(self):
        item = self._pending_item()
        failure = GitHubAuthConfigurationError(
            "GitHub App JWT signing failed (InvalidKeyError)",
            stage="github.auth.jwt_signing",
        )
        with patch.object(
            orchestrator.pipeline_admission,
            "installation_token",
            side_effect=failure,
        ), patch.object(orchestrator, "_emit_terminal_error_metric") as emit:
            orchestrator.run_context_phase(item, table=self.table)

        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["status"], "ERROR")
        self.assertEqual(current["context_attempt"], 1)
        self.assertEqual(current["error_kind"], "github_auth_configuration_error")
        self.assertEqual(current["error_stage"], "github.auth.jwt_signing")
        self.assertFalse(current["error_retryable"])
        self.assertFalse(current["error_retry_exhausted"])
        emit.assert_called_once_with(
            phase="github.auth.jwt_signing",
            kind="github_auth_configuration_error",
        )

    def test_paid_dispatch_ledger_failure_is_terminal_and_preserves_discarded_call(self):
        self._pending_item()
        phase_claim = persistence.claim_phase_attempt(
            "owner/repo",
            7,
            "context",
            expected_status="PENDING",
            runtime_identity={"aws_request_id": "context-owner"},
            owner_id="context-owner",
            stream_event_id="context-event-1",
            table=self.table,
        )
        record = {
            "schema_version": 2,
            "call_id": "a" * 64,
            "operation_id": "b" * 64,
            "phase": "route",
            "pipeline_phase": "context",
            "pipeline_attempt": 1,
            "call_index": 1,
            "transport_attempt_index": 1,
            "transport_dispatch_count": 1,
            "model": "deepseek-v4-pro",
            "logical_model": "deepseek-v4-pro",
            "billed_model": "deepseek-v4-flash",
            "thinking": True,
            "reasoning_effort": "high",
            "status": "completed",
            "finish_reason": "stop",
            "elapsed_seconds": 1.25,
            "last_attempt_elapsed_seconds": 1.25,
            "transport_attempt_count": 1,
            "usage_state": "reported",
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
            },
        }
        failure = ProviderCallLedgerError(
            "Provider-call ledger persistence failed",
            provider_call_record=record,
        )

        with patch.object(
            orchestrator,
            "_emit_terminal_error_metric",
        ) as emit:
            orchestrator._handle_phase_failure(
                repo="owner/repo",
                pr_number=7,
                expected_status="PENDING",
                phase="context",
                attempt=1,
                exc=failure,
                phase_claim=phase_claim,
                table=self.table,
            )

        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["status"], "ERROR")
        self.assertEqual(
            current["error_kind"],
            "provider_call_ledger_error",
        )
        self.assertFalse(current["error_retryable"])
        self.assertEqual(current["deepseek_model_phases"], [])
        self.assertEqual(
            current["deepseek_all_attempt_model_phases"],
            [record],
        )
        self.assertEqual(
            current["deepseek_discarded_model_phases"],
            [record],
        )
        accounting = current["deepseek_usage_accounting"]
        self.assertEqual(accounting["winning_call_count"], 0)
        self.assertEqual(accounting["discarded_call_count"], 1)
        self.assertTrue(accounting["complete_numeric_usage"])
        self.assertFalse(accounting["durable_call_ledger_complete"])
        self.assertEqual(accounting["terminal_fallback_call_count"], 1)
        self.assertEqual(
            current["provider_call_ledger_terminal_fallback"],
            {
                "schema_version": 1,
                "call_id": "a" * 64,
                "operation_id": "b" * 64,
                "transport_attempt_index": 1,
                "per_call_sink_persisted": False,
                "terminal_fallback_persisted": True,
            },
        )
        emit.assert_called_once_with(
            phase="context",
            kind="provider_call_ledger_error",
        )

    def test_review_ledger_failure_reaches_terminal_fallback_without_redelivery(self):
        self._pending_item(dry_run=True)
        persistence.store_context(
            "owner/repo",
            7,
            context_text="exact-head context",
            pr_details_text="details",
            meta={"context_strategy": "pfr"},
            review_mode="high",
            table=self.table,
        )
        context_ready = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        record = {
            "schema_version": 2,
            "call_id": "c" * 64,
            "operation_id": "d" * 64,
            "run_id": "owner/repo#7@abcdef123456",
            "head_sha": "abcdef123456",
            "phase": "final_presentation",
            "pipeline_phase": "review",
            "pipeline_attempt": 1,
            "call_index": 1,
            "transport_attempt_index": 1,
            "transport_dispatch_count": 1,
            "model": "deepseek-v4-pro",
            "logical_model": "deepseek-v4-pro",
            "billed_model": "deepseek-v4-flash",
            "thinking": True,
            "reasoning_effort": "high",
            "status": "completed",
            "finish_reason": "stop",
            "elapsed_seconds": 1.0,
            "last_attempt_elapsed_seconds": 1.0,
            "transport_attempt_count": 1,
            "usage_state": "reported",
            "usage": {
                "prompt_tokens": 20,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
            },
        }
        failure = ProviderCallLedgerError(
            "Provider-call ledger persistence failed",
            provider_call_record=record,
        )
        client = DeepSeekClient(api_key="key")

        with patch.object(
            orchestrator.pipeline_admission,
            "installation_token",
            return_value="token",
        ), patch.object(
            orchestrator,
            "generate_review",
            side_effect=failure,
        ) as generate:
            orchestrator.run_review_phase(
                context_ready,
                table=self.table,
                runtime=_Runtime(),
                deepseek_client=client,
                stream_event_id="review-ledger-event",
            )
            orchestrator.run_review_phase(
                context_ready,
                table=self.table,
                runtime=_Runtime(),
                deepseek_client=client,
                stream_event_id="review-ledger-event",
            )

        generate.assert_called_once()
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["status"], "ERROR")
        self.assertEqual(
            current["error_kind"],
            "provider_call_ledger_error",
        )
        self.assertFalse(current["error_retryable"])
        self.assertEqual(
            current["deepseek_discarded_model_phases"],
            [record],
        )
        accounting = current["deepseek_usage_accounting"]
        self.assertFalse(accounting["durable_call_ledger_complete"])
        self.assertEqual(accounting["terminal_fallback_call_count"], 1)
        self.assertEqual(
            current["provider_call_ledger_terminal_fallback"]["call_id"],
            "c" * 64,
        )

    def test_context_phase_publishes_docs_only_skip_result_after_pr_ingest(self):
        item = self._pending_item()
        runtime = _Runtime(_pr_content(path="README.md", diff="@@ -1 +1 @@\n-Old docs\n+New docs\n", title="docs"))
        with patch.object(orchestrator.config, "DRY_RUN", True), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator,
            "analyze_pr_complexity",
            return_value={
                "complexity": "skip",
                "reason": "documentation changes",
                "pr_type": "docs",
                "verification_plan": [],
            },
        ) as analyzer, patch.object(
            orchestrator, "collect_context"
        ) as collect_context:
            orchestrator.run_context_phase(item, table=self.table, runtime=runtime)

        analyzer.assert_called_once()
        collect_context.assert_not_called()
        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["status"], "PROCESSED_DRYRUN")
        self.assertEqual(current["review_mode"], "skip")
        self.assertIn("documentation changes", current["skip_reason"])
        self.assertEqual(current["review_generation_status"], "complete")
        self.assertFalse(current["review_fallback_used"])
        self.assertFalse(current["quality_scoreable"])
        self.assertEqual(current["quality_exclusion_reasons"], ["skipped_by_policy"])
        artifact = persistence.load_review_artifact_from_item(current)
        self.assertIn(
            "### LlamaPReview — Review skipped",
            artifact["main_comment"],
        )
        self.assertIn(
            "No model-driven code review was run",
            artifact["main_comment"],
        )
        self.assertEqual(
            artifact["main_comment"].count("code review was run"),
            1,
        )
        self.assertNotIn("GitHub Discussions", artifact["main_comment"])
        self.assertNotIn("Automated review by", artifact["main_comment"])
        self.assertEqual(artifact["review_generation_status"], "complete")
        self.assertFalse(artifact["review_fallback_used"])
        self.assertFalse(artifact["quality_scoreable"])
        self.assertEqual(artifact["quality_exclusion_reasons"], ["skipped_by_policy"])

    def test_terminal_skip_preserves_non_green_ci_state_without_visible_notice(self):
        cases = (
            (31, "in_progress", None, "ci_pending"),
            (32, "completed", "action_required", "ci_action_required"),
            (33, "completed", "cancelled", "ci_incomplete"),
        )
        for pr_number, status, conclusion, expected_gate in cases:
            with self.subTest(expected_gate=expected_gate):
                item = self._pending_item(pr_number=pr_number)
                content = _pr_content(
                    path="README.md",
                    diff="@@ -1 +1 @@\n-Old docs\n+New docs\n",
                    title="docs",
                )
                content["pr_metadata"]["number"] = pr_number
                content["ci_cd_results"] = {
                    "head_sha": "abcdef123456",
                    "check_runs": [
                        {
                            "name": "docs-check",
                            "status": status,
                            "conclusion": conclusion,
                            "details_url": f"https://example.test/check/{pr_number}",
                        }
                    ],
                    "_retrieval_meta": {"ci_aggregate": {"outcome": "ok"}},
                }
                runtime = _Runtime(content)
                with patch.object(
                    orchestrator.config, "DRY_RUN", True
                ), patch.object(
                    orchestrator.pipeline_admission, "installation_token", return_value="token"
                ), patch.object(
                    orchestrator,
                    "analyze_pr_complexity",
                    return_value={
                        "complexity": "skip",
                        "reason": "documentation changes",
                        "pr_type": "docs",
                        "verification_plan": [],
                    },
                ):
                    orchestrator.run_context_phase(
                        item, table=self.table, runtime=runtime
                    )

                current = self.table.get_item(
                    Key={"repo": "owner/repo", "pr_number": pr_number}
                )["Item"]
                self.assertEqual(current["status"], "PROCESSED_DRYRUN")
                self.assertEqual(current["ci_gate_status"], expected_gate)
                self.assertEqual(current["visible_projection_source"], "terminal_policy")
                artifact = persistence.load_review_artifact_from_item(current)
                self.assertNotIn("Current-head CI", artifact["main_comment"])
                self.assertEqual(artifact["visible_projection_source"], "terminal_policy")
                self.assertEqual(
                    artifact["ci_snapshot"]["checks"][0]["identity"],
                    (
                        f"check_run:https://example.test/check/{pr_number}"
                        "|docs-check||"
                    ),
                )
                self.assertEqual(
                    artifact["ci_snapshot"]["checks"][0]["classification"],
                    expected_gate.removeprefix("ci_"),
                )

    def test_terminal_ci_gate_classifies_untrusted_check_names_without_rendering_them(self):
        snapshot = {
            "blocking_checks": [
                {"name": "lint)\n\n## Fake @team `danger`"}
            ]
        }
        gate = orchestrator.pipeline_publication.terminal_ci_gate_status(
            snapshot
        )

        self.assertEqual(gate, "ci_failure")

    def test_context_phase_publishes_ai_skip_result(self):
        item = self._pending_item()
        raw_reason = "Routine docs sync; all CI checks passed."
        with patch.object(orchestrator.config, "DRY_RUN", True), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator,
            "analyze_pr_complexity",
            return_value={
                "complexity": "skip",
                "reason": raw_reason,
                "pr_type": "docs",
                "verification_plan": [],
            },
        ), patch.object(orchestrator, "collect_context") as collect_context:
            orchestrator.run_context_phase(item, table=self.table, runtime=_Runtime())

        collect_context.assert_not_called()
        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["status"], "PROCESSED_DRYRUN")
        self.assertEqual(current["review_mode"], "skip")
        self.assertEqual(current["skip_reason"], raw_reason)
        self.assertIn("documentation-only", current["skip_public_reason"])
        self.assertEqual(current["review_generation_status"], "complete")
        self.assertFalse(current["review_fallback_used"])
        self.assertFalse(current["quality_scoreable"])
        self.assertEqual(current["quality_exclusion_reasons"], ["skipped_by_policy"])
        artifact = persistence.load_review_artifact_from_item(current)
        self.assertIn(current["skip_public_reason"], artifact["main_comment"])
        self.assertNotIn(raw_reason, artifact["main_comment"])
        self.assertNotIn("all CI checks passed", artifact["main_comment"])
        self.assertEqual(artifact["review_generation_status"], "complete")
        self.assertFalse(artifact["review_fallback_used"])
        self.assertFalse(artifact["quality_scoreable"])
        self.assertEqual(artifact["quality_exclusion_reasons"], ["skipped_by_policy"])

    def test_policy_skip_records_route_usage_and_delta_provenance(self):
        """A terminal skip still records Route and its immutable compare."""

        item = self._pending_item()
        content = _pr_content()
        content["pr_metadata"]["base_sha"] = "base123456789"
        route_input_sha = "a" * 64
        with patch.object(orchestrator.config, "DRY_RUN", True), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator,
            "analyze_pr_complexity",
            return_value={
                "complexity": "skip",
                "reason": "Routine docs sync.",
                "pr_type": "docs",
                "_route_plan_meta": {
                    "model_phases": [
                        {
                            "phase": "pr_analyzer",
                            "model": "route-model",
                            "thinking": True,
                            "reasoning_effort": "low",
                            "attempt": 1,
                            "elapsed_seconds": 1.5,
                            "finish_reason": "stop",
                            "usage": {
                                "prompt_tokens": 7000,
                                "completion_tokens": 1000,
                                "total_tokens": 8000,
                            },
                        }
                    ],
                    "route_input_sha256": route_input_sha,
                    "route_input_schema": "llamapreview.route_digest.v3",
                    "digest_truncation": {
                        "overall_compacted": False,
                        "omitted_file_count": 0,
                    },
                },
            },
        ), patch.object(orchestrator, "collect_context"):
            orchestrator.run_context_phase(
                item,
                table=self.table,
                runtime=_Runtime(content),
            )

        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(
            int(current["deepseek_usage_total"]["total_tokens"]), 8000
        )
        self.assertEqual(
            [phase["phase"] for phase in current["deepseek_model_phases"]],
            ["route"],
        )
        provenance = current["route_delta_provenance"]
        self.assertEqual(provenance["base_sha"], "base123456789")
        self.assertEqual(provenance["head_sha"], current["head_sha"])
        self.assertEqual(
            provenance["compare_identity_sha256"],
            hashlib.sha256(
                b"base123456789\nabcdef123456"
            ).hexdigest(),
        )
        self.assertEqual(provenance["route_input_sha256"], route_input_sha)
        self.assertEqual(
            provenance["route_input_schema"],
            "llamapreview.route_digest.v3",
        )
        self.assertEqual(
            provenance["route_input_truncation"],
            {"overall_compacted": False, "omitted_file_count": 0},
        )
        self.assertGreaterEqual(int(provenance["changed_path_count"]), 1)
        self.assertEqual(len(provenance["changed_path_set_digest"]), 64)
        self.assertNotIn("changed_path_digest", provenance)

    def test_dependency_change_always_reaches_route_and_route_normal_is_honored(self):
        item = self._pending_item()
        content = _pr_content(
            path="package-lock.json",
            diff='@@ -1 +1 @@\n-"lib": "1.2.3"\n+"lib": "2.0.0"\n',
            title="major dependency bump",
        )
        route = {
            "complexity": "normal",
            "reason": "Dependency compatibility needs bounded context.",
            "source": "model_route_plan",
            "pr_type": "dependency",
            "verification_plan": [],
        }
        client = object()
        with patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "analyze_pr_complexity", return_value=route
        ) as analyzer, patch.object(
            orchestrator, "collect_context", return_value=("ctx", {"tokens": 2})
        ) as collect_context:
            orchestrator.run_context_phase(
                item,
                table=self.table,
                runtime=_Runtime(content),
                deepseek_client=client,
            )

        analyzer.assert_called_once()
        self.assertIs(analyzer.call_args.kwargs["client"], client)
        collect_context.assert_called_once()
        self.assertIs(collect_context.call_args.kwargs["client"], client)
        self.assertIs(collect_context.call_args.kwargs["route_plan"], route)
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["review_mode"], "normal")
        self.assertNotIn("minimum_complexity_reason", current["analyzer_result"])
        self.assertNotIn("routing_signals", current["analyzer_result"])

    def test_context_handoff_preserves_facts_without_adding_semantic_envelope(self):
        item = self._pending_item()
        route = {
            "complexity": "normal",
            "reason": "The changed boundary needs context.",
            "pr_type": "code",
            "risk_domains": [],
            "acceptance_criteria": ["The boundary rejects failed calls."],
        }
        captured_route_plan = {}

        def collect_context_side_effect(*_args, **kwargs):
            captured_route_plan.update(kwargs["route_plan"])
            return (
                "ctx",
                {
                    "material_unknowns": ["Runtime behavior was not observed."],
                    "tokens": 2,
                },
            )

        with patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "analyze_pr_complexity", return_value=route
        ), patch.object(
            orchestrator,
            "collect_context",
            side_effect=collect_context_side_effect,
        ):
            orchestrator.run_context_phase(
                item,
                table=self.table,
                runtime=_Runtime(),
            )

        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        meta = current["context_meta"]
        self.assertNotIn("semantic_closure_version", meta)
        self.assertNotIn("semantic_closure", meta)
        self.assertEqual(
            meta["material_unknowns"],
            ["Runtime behavior was not observed."],
        )
        self.assertEqual(
            captured_route_plan["acceptance_criteria"],
            ["The boundary rejects failed calls."],
        )

    def test_default_context_client_carries_route_prefix_into_pfr(self):
        item = self._pending_item()
        shared_client = SimpleNamespace()
        prefix = [
            {"role": "system", "content": "route"},
            {"role": "assistant", "content": '{"complexity":"normal"}'},
        ]

        def route_side_effect(_details, *, client, **_kwargs):
            self.assertIs(client, shared_client)
            setattr(client, "_llamapreview_route_conversation", list(prefix))
            return {
                "complexity": "normal",
                "reason": "Needs bounded context.",
                "pr_type": "code",
                "verification_plan": [],
            }

        def context_side_effect(*_args, **kwargs):
            self.assertIs(kwargs["client"], shared_client)
            consumed = getattr(
                kwargs["client"],
                "_llamapreview_route_conversation",
                None,
            )
            delattr(kwargs["client"], "_llamapreview_route_conversation")
            return "ctx", {
                "tokens": 2,
                "same_conversation_prefix_used": consumed == prefix,
            }

        with patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "DeepSeekClient", return_value=shared_client
        ) as client_factory, patch.object(
            orchestrator,
            "analyze_pr_complexity",
            side_effect=route_side_effect,
        ), patch.object(
            orchestrator,
            "collect_context",
            side_effect=context_side_effect,
        ):
            orchestrator.run_context_phase(
                item,
                table=self.table,
                runtime=_Runtime(),
            )

        client_factory.assert_called_once_with(
            model=orchestrator.config.ANALYZER_MODEL,
            reasoning_effort=orchestrator.config.ANALYZER_EFFORT,
        )
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertTrue(
            current["context_meta"]["same_conversation_prefix_used"]
        )
        self.assertFalse(
            hasattr(shared_client, "_llamapreview_route_conversation")
        )

    def test_dependency_non_green_ci_route_skip_is_not_overridden(self):
        item = self._pending_item()
        content = _dependency_bundle_content(status="completed", conclusion="failure")
        route = {
            "complexity": "skip",
            "reason": "No substantive code-review target.",
            "pr_type": "dependency",
            "verification_plan": [],
        }
        with patch.object(orchestrator.config, "DRY_RUN", True), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "analyze_pr_complexity", return_value=route
        ) as analyzer, patch.object(orchestrator, "collect_context") as collect_context:
            orchestrator.run_context_phase(
                item,
                table=self.table,
                runtime=_Runtime(content),
            )

        analyzer.assert_called_once()
        collect_context.assert_not_called()
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["status"], "PROCESSED_DRYRUN")
        self.assertEqual(current["review_mode"], "skip")
        self.assertEqual(current["analyzer_result"], route)

    def test_clean_dependency_route_low_calls_analyzer_and_skips_pfr(self):
        item = self._pending_item()
        content = _pr_content(
            path="requirements-dev.txt",
            diff="@@ -1 +1 @@\n-lib==1.2.3\n+lib==1.2.4\n",
            title="patch dependency bump",
        )
        route = {
            "complexity": "low",
            "reason": "Contained dependency update.",
            "pr_type": "dependency",
            "semantic_closure_version": 1,
            "primary_review_obligation": {
                "obligation_id": "obl_route_0001",
                "proposition": "The dependency update remains compatible.",
                "consequence_if_false": "The updated dependency may fail.",
                "evidence_need": "diff_sufficient",
            },
            "verification_plan": [],
        }
        with patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "analyze_pr_complexity", return_value=route
        ) as analyzer, patch.object(orchestrator, "collect_context") as collect_context:
            orchestrator.run_context_phase(
                item,
                table=self.table,
                runtime=_Runtime(content),
            )

        analyzer.assert_called_once()
        collect_context.assert_not_called()
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["review_mode"], "low")
        self.assertEqual(current["context_meta"]["pfr_plan_source"], "not_required_low_route")
        self.assertNotIn(
            "semantic_closure_version",
            current["context_meta"],
        )

    def test_hard_empty_diff_boundary_skips_without_route(self):
        item = self._pending_item()
        content = _pr_content()
        content["file_changes"] = []
        with patch.object(orchestrator.config, "DRY_RUN", True), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(orchestrator, "analyze_pr_complexity") as analyzer:
            orchestrator.run_context_phase(
                item,
                table=self.table,
                runtime=_Runtime(content),
            )

        analyzer.assert_not_called()
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["status"], "PROCESSED_DRYRUN")
        self.assertIn("Empty PR", current["skip_reason"])
        self.assertEqual(
            current["visible_projection_source"],
            "terminal_input_boundary",
        )

    def test_nonempty_zero_text_change_still_reaches_route(self):
        cases = (
            {
                "file_path": "src/new.py",
                "previous_filename": "src/old.py",
                "change_type": "renamed",
                "additions": 0,
                "deletions": 0,
                "changes": 0,
                "diff": "",
            },
            {
                "file_path": "scripts/run.sh",
                "change_type": "modified",
                "additions": 0,
                "deletions": 0,
                "changes": 0,
                "diff": "",
            },
        )
        for file_change in cases:
            with self.subTest(path=file_change["file_path"]):
                self.table.reset()
                item = self._pending_item()
                content = _pr_content()
                content["file_changes"] = [file_change]
                with patch.object(
                    orchestrator.pipeline_admission, "installation_token", return_value="token"
                ), patch.object(
                    orchestrator,
                    "analyze_pr_complexity",
                    return_value={"complexity": "low", "reason": "bounded"},
                ) as analyzer, patch.object(
                    orchestrator, "collect_context"
                ) as collect_context:
                    orchestrator.run_context_phase(
                        item,
                        table=self.table,
                        runtime=_Runtime(content),
                    )

                analyzer.assert_called_once()
                collect_context.assert_not_called()
                current = self.table.get_item(
                    Key={"repo": "owner/repo", "pr_number": 7}
                )["Item"]
                self.assertEqual(current["status"], "CONTEXT_READY")
                self.assertEqual(current["review_mode"], "low")

    def test_opaque_or_whitespace_sensitive_change_still_reaches_route(self):
        cases = (
            {
                "file_path": "artifact.bin",
                "additions": 1,
                "deletions": 0,
                "changes": 1,
                "diff": "[SKIPPED] File type not suitable for diff analysis",
            },
            {
                "file_path": "src/app.py",
                "additions": 1,
                "deletions": 1,
                "changes": 2,
                "diff": "@@ -1 +1 @@\n-    value\n+ value\n",
            },
        )
        for file_change in cases:
            with self.subTest(path=file_change["file_path"]):
                self.table.reset()
                item = self._pending_item()
                content = _pr_content()
                content["file_changes"] = [file_change]
                with patch.object(
                    orchestrator.pipeline_admission, "installation_token", return_value="token"
                ), patch.object(
                    orchestrator,
                    "analyze_pr_complexity",
                    return_value={"complexity": "low", "reason": "bounded"},
                ) as analyzer:
                    orchestrator.run_context_phase(
                        item,
                        table=self.table,
                        runtime=_Runtime(content),
                    )
                analyzer.assert_called_once()

    def test_context_phase_routes_oversized_nonempty_pr_through_bounded_input(self):
        item = self._pending_item()
        runtime = _Runtime(
            _pr_content(
                diff=(
                    "@@ -1 +1 @@\n-old\n"
                    + "\n".join(f"+changed_{index} = {index}" for index in range(900))
                )
            )
        )
        inventory = orchestrator.RepoInventory(
            repository="owner/repo",
            requested_sha="abcdef123456",
            status="complete",
            items=[{"path": "src/app.py", "type": "blob", "size": 20_000}],
            discoverable_files={"src/app.py"},
            readable_files={"src/app.py"},
        )
        with patch.object(orchestrator.config, "DRY_RUN", True), patch.object(
            orchestrator.config, "PR_DETAILS_MAX_CHARS", 4_000
        ), patch.object(orchestrator.config, "LARGE_PR_MAX_CHARS", 8_000), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "fetch_repo_inventory", return_value=inventory
        ), patch.object(
            orchestrator,
            "analyze_pr_complexity",
            return_value={"complexity": "low", "reason": "bounded delta"},
        ) as analyzer:
            orchestrator.run_context_phase(item, table=self.table, runtime=runtime)

        analyzer.assert_called_once()
        routed_details = analyzer.call_args.args[0]
        self.assertLess(len(routed_details), 8_000)
        self.assertIn("Bounded changed-region view", routed_details)
        self.assertIn("Diff coverage:** partial", routed_details)
        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["status"], "CONTEXT_READY")
        self.assertEqual(current["review_mode"], "low")
        _context_text, _pr_details, context_meta = (
            persistence.load_context_bundle_from_item(current)
        )
        self.assertTrue(context_meta["pr_details_compacted"])
        self.assertGreater(
            context_meta["pr_details_chars"],
            context_meta["model_pr_details_chars"],
        )

    def test_low_route_uses_bounded_same_file_collector_without_pfr(self):
        item = self._pending_item()
        with patch.object(orchestrator.pipeline_admission, "installation_token", return_value="token"), patch.object(
            orchestrator, "analyze_pr_complexity", return_value={"complexity": "low", "reason": "Contained diff"}
        ), patch.object(orchestrator, "collect_context") as collect_context:
            orchestrator.run_context_phase(item, table=self.table, runtime=_Runtime())

        collect_context.assert_not_called()
        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["status"], "CONTEXT_READY")
        self.assertEqual(current["review_mode"], "low")
        self.assertEqual(
            current["context_meta"]["context_strategy"],
            "low_same_file",
        )
        self.assertEqual(
            current["context_meta"]["low_same_file_context_file_count"],
            0,
        )

    def test_low_same_file_collector_reads_two_small_exact_head_files(self):
        contents = {
            "pyproject.toml": "[project]\nrequires-python = \">=3.11,<3.14\"\n",
            "src/app.py": "value = 2\n",
            "src/third.py": "ignored = True\n",
        }

        class Runtime:
            def __init__(self):
                self.calls = []

            def read_text_file_bounded(
                self,
                repo,
                path,
                *,
                sha=None,
                opt_in=None,
            ):
                self.calls.append((repo, path, sha, opt_in))
                content = contents[path]
                size = len(content.encode())
                return {
                    "outcome": "success",
                    "content": content,
                    "source_size_bytes": size,
                    "bytes_read": size,
                }

        changes = [
            {
                "file_path": path,
                "change_type": "modified",
                "diff": "+changed\n",
            }
            for path in contents
        ]
        inventory = orchestrator.RepoInventory(
            repository="owner/repo",
            requested_sha="abcdef123456",
            status="complete",
            items=[
                {
                    "path": path,
                    "type": "blob",
                    "size": len(content.encode()),
                }
                for path, content in contents.items()
            ],
            discoverable_files=set(contents),
            readable_files=set(contents),
        )
        runtime = Runtime()

        context, meta = orchestrator._collect_low_same_file_context(
            runtime=runtime,
            repo="owner/repo",
            pr_content={"file_changes": changes},
            head_sha="abcdef123456",
            repo_inventory=inventory,
            initial_evidence_ledger=None,
        )

        self.assertEqual(len(runtime.calls), 2)
        self.assertIn("requires-python", context)
        self.assertIn("value = 2", context)
        self.assertNotIn("ignored = True", context)
        self.assertEqual(meta["context_strategy"], "low_same_file")
        self.assertEqual(meta["low_same_file_context_file_count"], 2)
        events = meta["evidence_ledger"]["evidence_events"]
        self.assertEqual(
            [event["coverage_type"] for event in events],
            ["full_file", "full_file"],
        )
        self.assertTrue(all(event["outcome"] == "hit" for event in events))

    def test_low_same_file_collector_respects_file_and_total_byte_caps(self):
        inventory = orchestrator.RepoInventory(
            repository="owner/repo",
            requested_sha="abcdef123456",
            status="complete",
            items=[
                {"path": "large.py", "type": "blob", "size": 50 * 1024 + 1},
                {"path": "first.py", "type": "blob", "size": 45 * 1024},
                {"path": "second.py", "type": "blob", "size": 40 * 1024},
            ],
            discoverable_files={"large.py", "first.py", "second.py"},
            readable_files={"large.py", "first.py", "second.py"},
        )
        calls = []

        class Runtime:
            def read_text_file_bounded(
                self,
                repo,
                path,
                *,
                sha=None,
                opt_in=None,
            ):
                calls.append(path)
                content = "x" * (45 * 1024)
                return {
                    "outcome": "success",
                    "content": content,
                    "source_size_bytes": len(content),
                    "bytes_read": len(content),
                }

        _context, meta = orchestrator._collect_low_same_file_context(
            runtime=Runtime(),
            repo="owner/repo",
            pr_content={
                "file_changes": [
                    {"file_path": path, "change_type": "modified"}
                    for path in ("large.py", "first.py", "second.py")
                ]
            },
            head_sha="abcdef123456",
            repo_inventory=inventory,
            initial_evidence_ledger=None,
        )

        self.assertEqual(calls, ["first.py"])
        self.assertEqual(meta["low_same_file_context_file_count"], 1)
        self.assertEqual(
            meta["low_same_file_context_outcomes"]["oversize_preflight"],
            2,
        )

    def test_low_same_file_policy_skip_does_not_starve_a_later_text_file(self):
        inventory = orchestrator.RepoInventory(
            repository="owner/repo",
            requested_sha="abcdef123456",
            status="complete",
            items=[
                {"path": "dist/app.min.js", "type": "blob", "size": 100},
                {"path": "src/app.py", "type": "blob", "size": 10},
                {"path": "assets/blob.dat", "type": "blob", "size": 10},
                {"path": "src/later.py", "type": "blob", "size": 10},
            ],
            discoverable_files={
                "dist/app.min.js",
                "src/app.py",
                "assets/blob.dat",
                "src/later.py",
            },
            readable_files={
                "dist/app.min.js",
                "src/app.py",
                "assets/blob.dat",
                "src/later.py",
            },
        )
        calls = []

        class Runtime:
            def read_text_file_bounded(
                self,
                repo,
                path,
                *,
                sha=None,
                opt_in=None,
            ):
                calls.append(path)
                if path == "dist/app.min.js":
                    return {"outcome": "excluded_by_policy"}
                if path == "assets/blob.dat":
                    return {"outcome": "binary_or_non_utf8"}
                return {
                    "outcome": "success",
                    "content": "value = 2\n",
                    "source_size_bytes": 10,
                    "bytes_read": 10,
                }

        context, meta = orchestrator._collect_low_same_file_context(
            runtime=Runtime(),
            repo="owner/repo",
            pr_content={
                "file_changes": [
                    {"file_path": path, "change_type": "modified"}
                    for path in (
                        "dist/app.min.js",
                        "src/app.py",
                        "assets/blob.dat",
                        "src/later.py",
                    )
                ]
            },
            head_sha="abcdef123456",
            repo_inventory=inventory,
            initial_evidence_ledger=None,
        )

        self.assertEqual(
            calls,
            ["dist/app.min.js", "src/app.py", "assets/blob.dat"],
        )
        self.assertIn("value = 2", context)
        self.assertEqual(meta["low_same_file_context_attempted_count"], 2)
        self.assertEqual(meta["low_same_file_context_file_count"], 1)
        self.assertEqual(
            meta["low_same_file_context_outcomes"],
            {
                "binary_or_non_utf8": 1,
                "excluded_by_policy": 1,
                "success": 1,
            },
        )

    def test_low_same_file_skips_sensitive_and_removed_paths_and_records_errors(self):
        paths = {".env", "src/removed.py", "src/error.py", "src/good.py"}
        inventory = orchestrator.RepoInventory(
            repository="owner/repo",
            requested_sha="abcdef123456",
            status="complete",
            items=[
                {"path": path, "type": "blob", "size": 10}
                for path in paths
            ],
            discoverable_files=paths,
            readable_files=paths,
        )
        calls = []

        class Runtime:
            def read_text_file_bounded(
                self,
                repo,
                path,
                *,
                sha=None,
                opt_in=None,
            ):
                calls.append(path)
                if path == "src/error.py":
                    raise RuntimeError("bounded read unavailable")
                return {
                    "outcome": "success",
                    "content": "value = 2\n",
                    "source_size_bytes": 10,
                    "bytes_read": 10,
                }

        context, meta = orchestrator._collect_low_same_file_context(
            runtime=Runtime(),
            repo="owner/repo",
            pr_content={
                "file_changes": [
                    {"file_path": ".env", "change_type": "modified"},
                    {"file_path": "src/removed.py", "change_type": "removed"},
                    {"file_path": "src/error.py", "change_type": "modified"},
                    {"file_path": "src/good.py", "change_type": "modified"},
                ]
            },
            head_sha="abcdef123456",
            repo_inventory=inventory,
            initial_evidence_ledger=None,
        )

        self.assertEqual(calls, ["src/error.py", "src/good.py"])
        self.assertIn("value = 2", context)
        self.assertEqual(meta["low_same_file_context_attempted_count"], 2)
        self.assertEqual(meta["low_same_file_context_file_count"], 1)
        self.assertEqual(
            meta["low_same_file_context_outcomes"],
            {"error": 1, "success": 1},
        )

    def test_normal_route_uses_flash_light_context_budget(self):
        item = self._pending_item()
        with patch.object(orchestrator.pipeline_admission, "installation_token", return_value="token"), patch.object(
            orchestrator, "analyze_pr_complexity", return_value={"complexity": "normal", "reason": "Needs light context"}
        ), patch.object(orchestrator, "collect_context", return_value=("ctx", {"tokens": 2})) as collect_context:
            orchestrator.run_context_phase(item, table=self.table, runtime=_Runtime())

        kwargs = collect_context.call_args.kwargs
        self.assertEqual(kwargs["model"], "deepseek-v4-flash")
        self.assertEqual(kwargs["reasoning_effort"], "high")
        self.assertEqual(kwargs["max_tool_rounds"], 3)
        self.assertEqual(kwargs["max_search_calls"], 6)
        self.assertEqual(kwargs["max_read_calls"], 6)
        self.assertEqual(kwargs["max_context_chars"], 150000)
        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["review_mode"], "normal")
        self.assertEqual(current["context_meta"]["context_strategy"], "pfr")

    def test_per_item_dry_run_applies_to_review_phase_without_global_dry_run(self):
        item = self._pending_item(dry_run=True)
        ok, _attrs = persistence.store_context(
            "owner/repo",
            7,
            context_text="",
            pr_details_text="details",
            meta={"context_strategy": "pfr"},
            review_mode="low",
            table=self.table,
        )
        self.assertTrue(ok)
        context_ready = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        runtime = _Runtime()
        with patch.object(orchestrator.config, "DRY_RUN", False), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator,
            "generate_review",
            return_value=_publishable_review(),
        ):
            orchestrator.run_review_phase(context_ready, table=self.table, runtime=runtime)

        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["status"], "PROCESSED_DRYRUN")
        self.assertEqual(runtime.pull.reviews, [])

    def test_runtime_identities_are_private_phase_specific_artifact_fields(self):
        self._pending_item(dry_run=True)
        context_identity = {
            "schema_version": 1,
            "phase": "context",
            "function_version": "41",
            "log_stream_name": "context-stream",
            "aws_request_id": "context-request",
        }
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr"},
            review_mode="low",
            context_runtime_identity=context_identity,
            table=self.table,
        )
        context_ready = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        runtime = _Runtime()
        with patch.dict(
            "os.environ",
            {
                "AWS_LAMBDA_FUNCTION_VERSION": "42",
                "AWS_LAMBDA_LOG_STREAM_NAME": "review-stream",
            },
        ), patch.object(
            orchestrator.pipeline_admission,
            "installation_token",
            return_value="token",
        ), patch.object(
            orchestrator,
            "generate_review",
            return_value=_publishable_review(),
        ) as generate:
            orchestrator.run_review_phase(
                context_ready,
                table=self.table,
                runtime=runtime,
                lambda_context=SimpleNamespace(
                    aws_request_id="review-request"
                ),
            )

        terminal = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        artifact = persistence.load_review_artifact_from_item(terminal)
        review_identity = {
            "schema_version": 1,
            "phase": "review",
            "function_version": "42",
            "log_stream_name": "review-stream",
            "aws_request_id": "review-request",
        }
        self.assertEqual(
            terminal["context_runtime_identity"],
            context_identity,
        )
        self.assertEqual(
            terminal["review_runtime_identity"],
            review_identity,
        )
        self.assertEqual(
            artifact["context_runtime_identity"],
            context_identity,
        )
        self.assertEqual(
            artifact["review_runtime_identity"],
            review_identity,
        )
        model_context_meta = generate.call_args.kwargs["context_meta"]
        self.assertNotIn(
            "context_runtime_identity",
            model_context_meta,
        )
        self.assertNotIn(
            "review_runtime_identity",
            model_context_meta,
        )

    def test_redelivered_context_ready_stream_image_does_not_repeat_review(self):
        self._pending_item(dry_run=True)
        ok, _attrs = persistence.store_context(
            "owner/repo",
            7,
            context_text="",
            pr_details_text="details",
            meta={"context_strategy": "pfr"},
            review_mode="low",
            table=self.table,
        )
        self.assertTrue(ok)
        stale_context_ready = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        runtime = _Runtime()
        generated = {
            "pr_review_comment": "summary",
            "inline_comments": [],
            "review_generation_status": "complete",
            "review_fallback_used": False,
            "review_publishable": True,
            "review_publication_safe": True,
        }
        with patch.object(
            persistence,
            "get_table",
            return_value=self.table,
        ), patch.object(
            orchestrator.pipeline_admission,
            "installation_token",
            return_value="token",
        ), patch.object(
            orchestrator,
            "generate_review",
            return_value=generated,
        ) as generate:
            orchestrator.run_review_phase(
                stale_context_ready,
                runtime=runtime,
            )
            orchestrator.run_review_phase(
                stale_context_ready,
                runtime=runtime,
            )

        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["status"], "PROCESSED_DRYRUN")
        self.assertEqual(current["review_attempt"], 1)
        generate.assert_called_once()

    def test_retired_route_mode_cannot_enable_dry_run(self):
        with patch.object(orchestrator.config, "DRY_RUN", False):
            self.assertFalse(
                orchestrator.pipeline_admission.effective_dry_run(
                    {"dry_run": False, "route_mode": "parallel_dryrun"}
                )
            )

    def test_live_retry_observes_successful_github_publish_before_model_work(self):
        self._pending_item(dry_run=False)
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr", "evidence_catalog": []},
            review_mode="high",
            table=self.table,
        )
        context_ready = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        runtime = _Runtime()
        generated = {
            **_publishable_review("complete review"),
            "quality_scoreable": True,
            "quality_exclusion_reasons": [],
        }

        with patch.object(orchestrator.config, "DRY_RUN", False), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "generate_review", return_value=generated
        ) as generate, patch.object(
            persistence,
            "store_review_result",
            side_effect=RuntimeError("terminal persistence unavailable"),
        ), patch.object(
            orchestrator,
            "emit_discarded_attempt_usage",
        ) as discarded:
            with self.assertRaisesRegex(RuntimeError, "persistence unavailable"):
                orchestrator.run_review_phase(
                    context_ready,
                    table=self.table,
                    runtime=runtime,
                    stream_event_id="event-review-terminal-crash",
                )
        discarded.assert_not_called()

        self.assertEqual(len(runtime.pull.reviews), 1)
        runtime.content["interactions"].append(
            {
                "author": "llamapreview[bot]",
                "body": "## Auto Pull Request Review",
            }
        )
        retry_item = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        retry_item["review_attempt"] = int(orchestrator.config.MAX_ATTEMPTS)
        with patch.object(orchestrator.config, "DRY_RUN", False), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "generate_review", return_value=generated
        ) as retry_generate:
            with patch.object(
                persistence,
                "load_context_bundle_from_item",
                side_effect=RuntimeError("corrupt context bundle"),
            ) as load_context:
                orchestrator.run_review_phase(
                    context_ready,
                    table=self.table,
                    runtime=runtime,
                    stream_event_id="event-review-terminal-crash",
                )

        generate.assert_called_once()
        retry_generate.assert_not_called()
        load_context.assert_not_called()
        self.assertEqual(len(runtime.pull.reviews), 1)
        terminal = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(terminal["status"], "PROCESSED")
        self.assertEqual(
            terminal["review_attempt"],
            int(orchestrator.config.MAX_ATTEMPTS) + 1,
        )
        self.assertEqual(terminal["pipeline_attempt"], 1)
        self.assertEqual(terminal["review_generation_attempt"], 1)
        self.assertEqual(
            terminal["publication_recovery_attempt"],
            int(orchestrator.config.MAX_ATTEMPTS) + 1,
        )
        self.assertEqual(
            terminal["publication_receipt"]["outcome"],
            "adopted",
        )

    def test_review_crash_before_intent_discards_attempt_and_regenerates_once(self):
        """A pre-intent crash retries generation but never revives its owner."""

        self._pending_item(dry_run=False)
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr", "evidence_catalog": []},
            review_mode="high",
            table=self.table,
        )
        stale_context_ready = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        runtime = _Runtime()
        stream_event_id = "event-review-crash-before-intent"
        item_key = tuple(
            sorted({"repo": "owner/repo", "pr_number": 7}.items())
        )
        generated_records = []
        captured_commits = []
        stale_publish_rejections = []
        candidate_store_calls = 0
        original_commit = orchestrator.pipeline_publication.commit_prepared
        original_candidate_store = persistence.store_publication_candidate

        def provider_record(attempt):
            prompt_tokens = 20 * attempt
            completion_tokens = 5 * attempt
            operation = {
                "run_id": "owner/repo#7@abcdef123456",
                "head_sha": "abcdef123456",
                "phase": "deep_judgment",
                "pipeline_phase": "review",
                "pipeline_attempt": attempt,
                "call_index": 1,
            }
            operation_id = sha256_value(operation)
            return {
                **operation,
                "schema_version": 2,
                "operation_id": operation_id,
                "call_id": sha256_value(
                    {
                        "operation_id": operation_id,
                        "transport_attempt_index": 1,
                    }
                ),
                "transport_attempt_index": 1,
                "transport_dispatch_count": 1,
                "model": "deepseek-v4-pro",
                "logical_model": "deepseek-v4-pro",
                "billed_model": "deepseek-v4-flash",
                "thinking": True,
                "reasoning_effort": "high",
                "status": "completed",
                "finish_reason": "stop",
                "elapsed_seconds": 1.0,
                "last_attempt_elapsed_seconds": 1.0,
                "transport_attempt_count": 1,
                "usage_state": "reported",
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }

        def generate(*_args, **_kwargs):
            current = self.table.get_item(
                Key={"repo": "owner/repo", "pr_number": 7}
            )["Item"]
            attempt = int(current["review_attempt"])
            record = provider_record(attempt)
            generated_records.append(record)
            # Model the already-durable response-boundary provider ledger. The
            # provider fence/ledger mechanics have their own focused contract;
            # this test owns attempt classification across orchestration.
            self.table.items[item_key][
                f"_deepseek_call_{record['call_id']}"
            ] = record
            if attempt == 2:
                self.assertEqual(len(captured_commits), 1)
                stale_args, stale_kwargs = captured_commits[0]
                try:
                    original_commit(*stale_args, **stale_kwargs)
                except PublicationStateConflict as exc:
                    stale_publish_rejections.append(exc)
                else:
                    self.fail("The stale generation owner regained publication")
                self.assertEqual(runtime.pull.reviews, [])
            return {
                **_publishable_review(f"review attempt {attempt}"),
                "quality_scoreable": True,
                "quality_exclusion_reasons": [],
                "review_model_phases": [record],
            }

        def commit_and_capture(*args, **kwargs):
            captured_commits.append((args, kwargs))
            return original_commit(*args, **kwargs)

        def crash_first_candidate_store(*args, **kwargs):
            nonlocal candidate_store_calls
            candidate_store_calls += 1
            if candidate_store_calls == 1:
                raise RuntimeError(
                    "publication candidate storage unavailable before intent"
                )
            return original_candidate_store(*args, **kwargs)

        with patch.object(
            orchestrator.config, "DRY_RUN", False
        ), patch.object(
            orchestrator.pipeline_admission,
            "installation_token",
            return_value="token",
        ), patch.object(
            orchestrator,
            "generate_review",
            side_effect=generate,
        ) as generate_review, patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
            side_effect=commit_and_capture,
        ), patch.object(
            persistence,
            "store_publication_candidate",
            side_effect=crash_first_candidate_store,
        ), patch.object(
            orchestrator,
            "emit_discarded_attempt_usage",
        ) as emit_discarded:
            with self.assertRaisesRegex(
                RuntimeError,
                "candidate storage unavailable before intent",
            ):
                orchestrator.run_review_phase(
                    stale_context_ready,
                    table=self.table,
                    runtime=runtime,
                    lambda_context=SimpleNamespace(
                        aws_request_id="review-request-1"
                    ),
                    stream_event_id=stream_event_id,
                )

            after_first = self.table.get_item(
                Key={"repo": "owner/repo", "pr_number": 7}
            )["Item"]
            self.assertEqual(after_first["status"], "CONTEXT_READY")
            self.assertEqual(after_first["review_attempt"], 1)
            self.assertNotIn("review_claim", after_first)
            self.assertNotIn("publication_intent", after_first)
            self.assertNotIn("publication_receipt", after_first)
            self.assertEqual(runtime.pull.reviews, [])
            emit_discarded.assert_called_once_with(
                repo="owner/repo",
                pr_number=7,
                attempt=1,
                phases=[generated_records[0]],
            )

            orchestrator.run_review_phase(
                stale_context_ready,
                table=self.table,
                runtime=runtime,
                lambda_context=SimpleNamespace(
                    aws_request_id="review-request-2"
                ),
                stream_event_id=stream_event_id,
            )

        self.assertEqual(generate_review.call_count, 2)
        self.assertEqual(len(generated_records), 2)
        self.assertEqual(len(stale_publish_rejections), 1)
        self.assertEqual(
            getattr(stale_publish_rejections[0], "stage", ""),
            "publication.intent",
        )
        self.assertEqual(len(runtime.pull.reviews), 1)
        terminal = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        artifact = persistence.load_review_artifact_from_item(terminal)
        receipt = terminal["publication_receipt"]
        self.assertEqual(terminal["status"], "PROCESSED")
        self.assertEqual(terminal["review_attempt"], 2)
        self.assertEqual(terminal["pipeline_attempt"], 2)
        self.assertEqual(terminal["review_generation_attempt"], 2)
        self.assertEqual(terminal["publication_generation_attempt"], 2)
        self.assertEqual(terminal["publication_recovery_attempt"], 0)
        self.assertEqual(receipt["outcome"], "created")
        self.assertEqual(receipt["publication_generation_phase"], "review")
        self.assertEqual(receipt["publication_generation_attempt"], 2)
        self.assertEqual(receipt["review_generation_attempt"], 2)
        self.assertEqual(receipt["publication_attempt"], 1)
        self.assertEqual(receipt["publication_recovery_attempt"], 0)
        self.assertEqual(receipt["commit_id"], "abcdef123456")
        self.assertEqual(
            terminal["publication_intent"]["owner_event_id"],
            stream_event_id,
        )
        self.assertEqual(
            terminal["publication_intent"]["owner_request_id"],
            "review-request-2",
        )
        accounting = terminal["deepseek_usage_accounting"]
        self.assertEqual(accounting["all_call_count"], 2)
        self.assertEqual(accounting["winning_call_count"], 1)
        self.assertEqual(accounting["discarded_call_count"], 1)
        self.assertEqual(accounting["unreported_usage_call_count"], 0)
        self.assertTrue(accounting["complete_numeric_usage"])
        self.assertEqual(
            terminal["deepseek_discarded_usage_total"]["total_tokens"],
            25,
        )
        self.assertEqual(
            terminal["deepseek_winning_usage_total"]["total_tokens"],
            50,
        )
        self.assertEqual(
            [
                phase["pipeline_attempt"]
                for phase in artifact["deepseek_all_attempt_model_phases"]
            ],
            [1, 2],
        )
        self.assertEqual(
            artifact["publication_receipt"],
            receipt,
        )
        self.assertEqual(
            artifact["deepseek_usage_accounting"],
            accounting,
        )

    def test_context_terminal_write_crash_recovers_without_route_or_redispatch(self):
        item = self._pending_item(dry_run=False)
        runtime = _Runtime(
            content={
                "pr_metadata": {
                    "number": 7,
                    "title": "empty change",
                    "head_sha": "abcdef123456",
                },
                "file_changes": [],
                "interactions": [],
            }
        )
        event_id = "event-context-terminal-crash"
        with patch.object(
            orchestrator.config, "DRY_RUN", False
        ), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "analyze_pr_complexity"
        ) as analyze, patch.object(
            persistence,
            "store_review_result",
            side_effect=RuntimeError("context terminal persistence unavailable"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "context terminal persistence unavailable"
            ):
                orchestrator.run_context_phase(
                    item,
                    table=self.table,
                    runtime=runtime,
                    stream_event_id=event_id,
                )
        analyze.assert_not_called()
        self.assertEqual(len(runtime.pull.reviews), 1)

        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        current["context_attempt"] = int(orchestrator.config.MAX_ATTEMPTS)
        with patch.object(
            orchestrator.config, "DRY_RUN", False
        ), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "analyze_pr_complexity"
        ) as retry_analyze:
            orchestrator.run_context_phase(
                item,
                table=self.table,
                runtime=runtime,
                stream_event_id=event_id,
            )

        retry_analyze.assert_not_called()
        self.assertEqual(len(runtime.pull.reviews), 1)
        terminal = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(terminal["status"], "PROCESSED")
        self.assertEqual(
            terminal["context_attempt"],
            int(orchestrator.config.MAX_ATTEMPTS) + 1,
        )
        self.assertEqual(terminal["publication_generation_phase"], "context")
        self.assertEqual(terminal["publication_generation_attempt"], 1)
        self.assertNotIn("review_generation_attempt", terminal)
        self.assertEqual(
            terminal["publication_receipt"]["outcome"], "adopted"
        )

    def test_post_create_head_change_preserves_exact_receipt(self):
        self._pending_item(dry_run=False)
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr", "evidence_catalog": []},
            review_mode="high",
            table=self.table,
        )
        item = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        runtime = _PostWriteChangingHeadRuntime()
        with patch.object(
            orchestrator.config, "DRY_RUN", False
        ), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator,
            "generate_review",
            return_value=_publishable_review("exact head review"),
        ):
            orchestrator.run_review_phase(
                item,
                table=self.table,
                runtime=runtime,
                stream_event_id="event-review-post-write-head-change",
            )

        terminal = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(terminal["status"], "PROCESSED")
        self.assertEqual(
            terminal["publication_receipt"]["commit_id"],
            "abcdef123456",
        )
        self.assertEqual(
            terminal["publication_post_write_head_sha"],
            "new-head-after-write",
        )
        self.assertTrue(terminal["publication_head_changed_after_dispatch"])

    def test_live_retryable_review_failure_never_posts_and_then_publishes_once(self):
        self._pending_item(dry_run=False)
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr", "evidence_catalog": []},
            review_mode="high",
            table=self.table,
        )
        runtime = _Runtime()
        fallback = {
            "pr_review_comment": "internal service fallback",
            "inline_comments": [],
            "review_generation_status": "fallback",
            "review_fallback_used": True,
            "review_publishable": False,
            "review_failure_retryable": True,
            "review_failure_kind": "model_transport_timeout",
            "review_failure_stage": "deep_thinking",
            "quality_scoreable": False,
            "quality_exclusion_reasons": [
                "review_fallback:model_transport_timeout"
            ],
            "review_model_phases": [],
        }
        complete = {
            **_publishable_review(
                "No review blocker found.",
                pipeline_attempt=2,
            ),
            "review_failure_retryable": False,
            "quality_scoreable": True,
            "quality_exclusion_reasons": [],
        }
        with patch.object(orchestrator.config, "DRY_RUN", False), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator,
            "generate_review",
            side_effect=[fallback, complete],
        ) as generate:
            first = self.table.get_item(
                Key={"repo": "owner/repo", "pr_number": 7}
            )["Item"]
            with self.assertRaises(ReviewGenerationIncomplete):
                orchestrator.run_review_phase(
                    first,
                    table=self.table,
                    runtime=runtime,
                    stream_event_id="event-review-generation-retry",
                )
            after_first = self.table.get_item(
                Key={"repo": "owner/repo", "pr_number": 7}
            )["Item"]
            self.assertEqual(after_first["status"], "CONTEXT_READY")
            self.assertNotIn("review_claim", after_first)
            self.assertEqual(runtime.pull.reviews, [])

            orchestrator.run_review_phase(
                after_first,
                table=self.table,
                runtime=runtime,
                stream_event_id="event-review-generation-retry",
            )

        self.assertEqual(generate.call_count, 2)
        self.assertEqual(len(runtime.pull.reviews), 1)
        terminal = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(terminal["status"], "PROCESSED")

    def test_live_terminal_review_failure_persists_private_artifact_without_post(self):
        for retryable, prior_attempts in ((False, 0), (True, 2)):
            with self.subTest(
                retryable=retryable,
                prior_attempts=prior_attempts,
            ):
                self.table.reset()
                self._pending_item(dry_run=False)
                persistence.store_context(
                    "owner/repo",
                    7,
                    context_text="ctx",
                    pr_details_text="details",
                    meta={"context_strategy": "pfr", "evidence_catalog": []},
                    review_mode="high",
                    table=self.table,
                )
                if prior_attempts:
                    key = tuple(
                        sorted(
                            {
                                "repo": "owner/repo",
                                "pr_number": 7,
                            }.items()
                        )
                    )
                    self.table.items[key]["review_attempt"] = prior_attempts
                runtime = _Runtime()
                generated = {
                    "pr_review_comment": "internal service fallback",
                    "inline_comments": [],
                    "review_generation_status": "fallback",
                    "review_fallback_used": True,
                    "review_publishable": False,
                    "review_failure_retryable": retryable,
                    "review_failure_kind": (
                        "model_transport_timeout"
                        if retryable
                        else "schema_validation_error"
                    ),
                    "review_failure_stage": "final_output",
                    "review_failure_message": "private provider response detail",
                    "quality_scoreable": False,
                    "quality_exclusion_reasons": ["review_fallback"],
                    "review_model_phases": [],
                }
                with patch.object(
                    orchestrator.config,
                    "DRY_RUN",
                    False,
                ), patch.object(
                    orchestrator.pipeline_admission,
                    "installation_token",
                    return_value="token",
                ), patch.object(
                    orchestrator,
                    "generate_review",
                    return_value=generated,
                ), patch.object(
                    orchestrator,
                    "_emit_terminal_error_metric",
                ) as emit:
                    item = self.table.get_item(
                        Key={"repo": "owner/repo", "pr_number": 7}
                    )["Item"]
                    orchestrator.run_review_phase(
                        item,
                        table=self.table,
                        runtime=runtime,
                    )

                terminal = self.table.get_item(
                    Key={"repo": "owner/repo", "pr_number": 7}
                )["Item"]
                self.assertEqual(terminal["status"], "ERROR")
                self.assertEqual(runtime.pull.reviews, [])
                self.assertTrue(terminal["review_artifact_complete"])
                self.assertFalse(terminal["review_publishable"])
                self.assertNotIn("review_failure_message", terminal)
                self.assertEqual(
                    terminal["error_retry_exhausted"],
                    retryable,
                )
                artifact = persistence.load_review_artifact_from_item(terminal)
                self.assertFalse(artifact["review_publishable"])
                self.assertEqual(
                    artifact["review_failure_message"],
                    "private provider response detail",
                )
                self.assertEqual(
                    artifact["deepseek_trace_pointer"]["retention_days"],
                    7,
                )
                self.assertIn(
                    "/deepseek-traces/owner/repo/pr-7/",
                    artifact["deepseek_trace_pointer"]["prefix"],
                )
                self.assertEqual(
                    terminal["deepseek_trace_pointer"],
                    artifact["deepseek_trace_pointer"],
                )
                self.assertEqual(
                    artifact["placement_fetch"]["skipped_reason"],
                    "review_nonpublishable",
                )
                emit.assert_called_once_with(
                    phase="final_output",
                    kind=generated["review_failure_kind"],
                )

    def test_live_github_terminal_publish_statuses_never_retry_or_duplicate(self):
        for status in (403, 422):
            with self.subTest(status=status):
                self.table.reset()
                self._pending_item(dry_run=False)
                persistence.store_context(
                    "owner/repo",
                    7,
                    context_text="ctx",
                    pr_details_text="details",
                    meta={"context_strategy": "pfr", "evidence_catalog": []},
                    review_mode="high",
                    table=self.table,
                )
                item = self.table.get_item(
                    Key={"repo": "owner/repo", "pr_number": 7}
                )["Item"]
                runtime = _Runtime()
                error = RuntimeError("typed GitHub publish failure")
                error.status = status
                runtime.pull.create_review = Mock(side_effect=error)
                generated = _publishable_review()
                with patch.object(
                    orchestrator.config, "DRY_RUN", False
                ), patch.object(
                    orchestrator.pipeline_admission, "installation_token", return_value="token"
                ), patch.object(
                    orchestrator, "generate_review", return_value=generated
                ):
                    orchestrator.run_review_phase(
                        item,
                        table=self.table,
                        runtime=runtime,
                        stream_event_id=f"event-review-http-{status}",
                    )

                terminal = self.table.get_item(
                    Key={"repo": "owner/repo", "pr_number": 7}
                )["Item"]
                self.assertEqual(terminal["status"], "ERROR")
                self.assertEqual(terminal["error_kind"], "http_terminal")
                self.assertEqual(runtime.pull.create_review.call_count, 1)

    def test_live_review_missing_explicit_publishability_never_posts(self):
        self._pending_item(dry_run=False)
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr", "evidence_catalog": []},
            review_mode="high",
            table=self.table,
        )
        item = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        runtime = _Runtime()
        generated = {
            "pr_review_comment": "summary",
            "inline_comments": [],
            "review_generation_status": "complete",
            "review_fallback_used": False,
            # Deliberately no review_publishable=True.
        }

        with patch.object(
            orchestrator.config, "DRY_RUN", False
        ), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "generate_review", return_value=generated
        ):
            orchestrator.run_review_phase(
                item,
                table=self.table,
                runtime=runtime,
            )

        terminal = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(terminal["status"], "ERROR")
        self.assertEqual(
            terminal["error_kind"],
            "review_generation_incomplete",
        )
        self.assertEqual(runtime.pull.reviews, [])

    def test_live_github_503_never_redispatches_when_no_review_appears(self):
        self._pending_item(dry_run=False)
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr", "evidence_catalog": []},
            review_mode="high",
            table=self.table,
        )
        runtime = _Runtime()
        attempts = 0

        def create_review(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                error = RuntimeError("typed GitHub transient failure")
                error.status = 503
                raise error
            runtime.pull.reviews.append(kwargs)
            return SimpleNamespace(
                id=len(runtime.pull.reviews),
                commit_id=kwargs["commit"].sha,
            )

        runtime.pull.create_review = create_review
        generated = _publishable_review()
        with patch.object(orchestrator.config, "DRY_RUN", False), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "generate_review", return_value=generated
        ) as generate, patch.object(
            github_publication_surface.time, "sleep", return_value=None
        ):
            first = self.table.get_item(
                Key={"repo": "owner/repo", "pr_number": 7}
            )["Item"]
            orchestrator.run_review_phase(
                first,
                table=self.table,
                runtime=runtime,
                stream_event_id="event-review-ambiguous-503",
            )

        self.assertEqual(generate.call_count, 1)
        self.assertEqual(attempts, 1)
        self.assertEqual(len(runtime.pull.reviews), 0)
        terminal = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(terminal["status"], "ERROR")
        self.assertEqual(
            terminal["error_kind"],
            "publication_outcome_unknown",
        )
        self.assertEqual(terminal["review_attempt"], 1)
        self.assertEqual(terminal["publication_attempt"], 1)
        self.assertEqual(terminal["publication_generation_attempt"], 1)
        self.assertEqual(terminal["publication_status"], "outcome_unknown")

    def test_pre_publish_status_race_stops_before_github_write(self):
        self._pending_item(dry_run=False)
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr"},
            review_mode="high",
            table=self.table,
        )
        item_key = tuple(sorted({"repo": "owner/repo", "pr_number": 7}.items()))
        self.table.items[item_key]["status"] = "PROCESSED"
        runtime = _Runtime()
        publication = orchestrator.pipeline_publication.PublicationContext(
            repo="owner/repo",
            pr_number=7,
            head_sha="abcdef123456",
            expected_status="CONTEXT_READY",
            phase="review",
            run_id="run-7",
            generation_attempt=1,
            runtime_identity={},
            phase_claim={},
            dry_run=False,
        )
        check = orchestrator.pipeline_publication.make_pre_publish_check(
            publication,
            runtime,
            table=self.table,
        )

        with self.assertRaisesRegex(RuntimeError, "status changed"):
            check()

        self.assertEqual(runtime.pull.reviews, [])

    def test_pre_publish_missing_state_fails_closed(self):
        runtime = _Runtime()
        publication = orchestrator.pipeline_publication.PublicationContext(
            repo="owner/repo",
            pr_number=7,
            head_sha="abcdef123456",
            expected_status="CONTEXT_READY",
            phase="review",
            run_id="run-7",
            generation_attempt=1,
            runtime_identity={},
            phase_claim={},
            dry_run=False,
        )
        check = orchestrator.pipeline_publication.make_pre_publish_check(
            publication,
            runtime,
            table=self.table,
        )

        with self.assertRaisesRegex(RuntimeError, "state is missing"):
            check()

        self.assertEqual(runtime.pull.reviews, [])

    def test_pfr_context_stores_and_reuses_repo_fact_sheet(self):
        with patch.object(
            orchestrator, "collect_context", return_value=("ctx", {"context_strategy": "pfr", "repo_fact_sheet": "facts-v1"})
        ) as collect_context:
            orchestrator._context_for_mode(
                review_mode="high",
                runtime=_Runtime(),
                token="token",
                repo="owner/repo",
                pr_number=7,
                pr_content=_pr_content(),
                pr_details="details",
                head_sha="abcdef123456",
                default_branch="main",
                trace_metadata={},
                route_plan={"complexity": "high", "verification_plan": []},
                deadline=orchestrator.Deadline.for_seconds(30),
                table=self.table,
            )

        cached = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 0})["Item"]
        self.assertEqual(cached["status"], "REPO_FACT_SHEET")
        self.assertEqual(cached["fact_sheet"], "facts-v1")
        self.assertEqual(
            cached["fact_sheet_schema_version"],
            persistence.REPO_FACT_SHEET_SCHEMA_VERSION,
        )

        with patch.object(
            orchestrator, "collect_context", return_value=("ctx", {"context_strategy": "pfr", "repo_fact_sheet": "facts-v1"})
        ) as collect_context:
            orchestrator._context_for_mode(
                review_mode="high",
                runtime=_Runtime(),
                token="token",
                repo="owner/repo",
                pr_number=8,
                pr_content=_pr_content(),
                pr_details="details",
                head_sha="abcdef123456",
                default_branch="main",
                trace_metadata={},
                route_plan={"complexity": "high", "verification_plan": []},
                deadline=orchestrator.Deadline.for_seconds(30),
                table=self.table,
            )

        self.assertEqual(collect_context.call_args.kwargs["repo_fact_sheet"], "facts-v1")

    def test_pfr_context_rebuilds_legacy_unversioned_repo_fact_sheet(self):
        self.table.put_item(
            Item={
                "repo": "owner/repo",
                "pr_number": 0,
                "status": "REPO_FACT_SHEET",
                "fact_sheet": "legacy-facts",
                "fact_sheet_head_sha": "abcdef123456",
            }
        )
        with patch.object(
            orchestrator,
            "collect_context",
            return_value=(
                "ctx",
                {"context_strategy": "pfr", "repo_fact_sheet": "rebuilt-facts"},
            ),
        ) as collect_context:
            orchestrator._context_for_mode(
                review_mode="high",
                runtime=_Runtime(),
                token="token",
                repo="owner/repo",
                pr_number=9,
                pr_content=_pr_content(),
                pr_details="details",
                head_sha="abcdef123456",
                default_branch="main",
                trace_metadata={},
                route_plan={"complexity": "high", "verification_plan": []},
                deadline=orchestrator.Deadline.for_seconds(30),
                table=self.table,
            )

        self.assertNotIn("repo_fact_sheet", collect_context.call_args.kwargs)
        rebuilt = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 0}
        )["Item"]
        self.assertEqual(rebuilt["fact_sheet"], "rebuilt-facts")
        self.assertEqual(
            rebuilt["fact_sheet_schema_version"],
            persistence.REPO_FACT_SHEET_SCHEMA_VERSION,
        )

    def test_large_but_supported_pr_uses_bounded_route_plan_instead_of_size_default_high(self):
        item = self._pending_item()
        runtime = _Runtime(_pr_content(diff="@@ -1 +1 @@\n-old\n+" + ("x" * 31_000)))
        with patch.object(orchestrator.pipeline_admission, "installation_token", return_value="token"), patch.object(
            orchestrator,
            "analyze_pr_complexity",
            return_value={"complexity": "normal", "reason": "bounded digest", "pr_type": "code", "verification_plan": []},
        ) as analyzer, patch.object(orchestrator, "collect_context", return_value=("ctx", {"tokens": 2})) as collect_context:
            orchestrator.run_context_phase(item, table=self.table, runtime=runtime)

        analyzer.assert_called_once()
        self.assertEqual(collect_context.call_args.kwargs["model"], "deepseek-v4-flash")
        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["review_mode"], "normal")

    def test_blocking_ci_workflow_route_low_is_not_overridden(self):
        item = self._pending_item()
        content = _pr_content(path=".github/workflows/release.yml", title="fix release CI")
        content["ci_cd_results"] = {
            "check_runs": [{"id": 1, "name": "workflow", "status": "completed", "conclusion": "failure"}]
        }
        with patch.object(orchestrator.pipeline_admission, "installation_token", return_value="token"), patch.object(
            orchestrator,
            "analyze_pr_complexity",
            return_value={"complexity": "low", "reason": "small diff", "pr_type": "ci", "verification_plan": []},
        ), patch.object(orchestrator, "collect_context", return_value=("ctx", {"tokens": 2})) as collect_context:
            orchestrator.run_context_phase(item, table=self.table, runtime=_Runtime(content))

        collect_context.assert_not_called()
        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["review_mode"], "low")
        self.assertNotIn("minimum_complexity_reason", current["analyzer_result"])

    def test_context_models_and_stored_bundle_receive_unambiguous_ci_snapshot(self):
        item = self._pending_item()
        content = _pr_content(path=".github/workflows/release.yml", title="fix release CI")
        content["ci_cd_results"] = {
            "state": "success",
            "check_runs": [
                {
                    "id": 1,
                    "name": "workflow",
                    "status": "completed",
                    "conclusion": "failure",
                    "output": {"summary": "Pin the external action."},
                    "annotations": [
                        {
                            "path": ".github/workflows/release.yml",
                            "start_line": 12,
                            "end_line": 12,
                            "annotation_level": "failure",
                            "message": "Use a full commit SHA.",
                        }
                    ],
                }
            ],
        }
        with patch.object(
            orchestrator.pipeline_admission,
            "installation_token",
            return_value="token",
        ), patch.object(
            orchestrator,
            "analyze_pr_complexity",
            return_value={
                "complexity": "normal",
                "reason": "CI workflow needs context",
                "pr_type": "ci",
                "verification_plan": [],
            },
        ) as analyzer, patch.object(
            orchestrator,
            "collect_context",
            return_value=("ctx", {"tokens": 2}),
        ) as collect_context:
            orchestrator.run_context_phase(item, table=self.table, runtime=_Runtime(content))

        analyzer_details = analyzer.call_args.args[0]
        pfr_details = collect_context.call_args.kwargs["pr_details"]
        for model_input in (analyzer_details, pfr_details):
            self.assertEqual(model_input.count("<CURRENT_HEAD_CI_SNAPSHOT>"), 1)
            self.assertIn('"aggregate_classification":"failure"', model_input)
            self.assertIn('"commit_status_state":"success"', model_input)
            self.assertIn('"message":"Use a full commit SHA."', model_input)
            self.assertNotIn('"overall_state"', model_input)

        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        _context, stored_details, meta = persistence.load_context_bundle_from_item(current)
        self.assertEqual(stored_details, pfr_details)
        self.assertEqual(meta["ci_snapshot"]["aggregate_classification"], "failure")
        self.assertEqual(meta["ci_snapshot"]["commit_status_state"], "success")
        self.assertGreater(meta["model_pr_details_chars"], meta["pr_details_chars"])

    def test_review_phase_uses_low_flash_profile(self):
        item = self._pending_item()
        ok, _attrs = persistence.store_context(
            "owner/repo",
            7,
            context_text="",
            pr_details_text="details",
            meta={"context_strategy": "none"},
            review_mode="low",
            table=self.table,
        )
        self.assertTrue(ok)
        context_ready = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        with patch.object(orchestrator.config, "DRY_RUN", True), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator,
            "generate_review",
            return_value=_publishable_review(),
        ) as generate:
            orchestrator.run_review_phase(context_ready, table=self.table, runtime=_Runtime())

        kwargs = generate.call_args.kwargs
        self.assertEqual(kwargs["model"], "deepseek-v4-flash")
        self.assertEqual(kwargs["reasoning_effort"], "high")
        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["status"], "PROCESSED_DRYRUN")
        self.assertEqual(current["review_mode"], "low")

    def test_context_head_mismatch_becomes_superseded_without_model_work(self):
        item = self._pending_item()
        content = _pr_content()
        content["pr_metadata"]["head_sha"] = "new-head"
        runtime = _Runtime(content, head_sha="new-head")
        with patch.object(orchestrator.pipeline_admission, "installation_token", return_value="token"), patch.object(
            orchestrator, "analyze_pr_complexity"
        ) as analyzer:
            orchestrator.run_context_phase(item, table=self.table, runtime=runtime)

        analyzer.assert_not_called()
        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["status"], "SUPERSEDED")
        self.assertEqual(current["queued_head_sha"], "abcdef123456")
        self.assertEqual(current["current_head_sha"], "new-head")

    def test_merged_pr_is_superseded_before_ci_or_model_work(self):
        item = self._pending_item()
        runtime = _LifecycleRuntime(state="closed", merged=True)
        with patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "analyze_pr_complexity"
        ) as analyzer, patch.object(
            orchestrator, "collect_context"
        ) as collect_context:
            orchestrator.run_context_phase(item, table=self.table, runtime=runtime)

        analyzer.assert_not_called()
        collect_context.assert_not_called()
        self.assertEqual(runtime.ci_calls, 0)
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["status"], "SUPERSEDED")
        self.assertEqual(current["superseded_kind"], "pr_merged")
        self.assertEqual(current["current_pr_state"], "closed")
        self.assertTrue(current["current_pr_merged"])
        self.assertEqual(current["queued_head_sha"], current["current_head_sha"])

    def test_incomplete_lifecycle_snapshot_fails_closed_and_retries(self):
        item = self._pending_item()
        runtime = _SequenceLifecycleRuntime(
            [{"head_sha": "abcdef123456", "state": "open"}]
        )
        with patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(orchestrator, "analyze_pr_complexity") as analyzer:
            with self.assertRaisesRegex(
                HeadVerificationUnavailable,
                "complete PR head/lifecycle snapshot",
            ):
                orchestrator.run_context_phase(
                    item,
                    table=self.table,
                    runtime=runtime,
                )

        analyzer.assert_not_called()
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["status"], "PENDING")
        self.assertEqual(current["context_attempt"], 1)

    def test_head_change_during_context_prevents_context_ready_transition(self):
        item = self._pending_item()
        runtime = _ChangingHeadRuntime(
            ["abcdef123456", "abcdef123456", "new-head"]
        )
        with patch.object(orchestrator.pipeline_admission, "installation_token", return_value="token"), patch.object(
            orchestrator, "analyze_pr_complexity", return_value={"complexity": "normal", "reason": "review", "verification_plan": []}
        ), patch.object(orchestrator, "collect_context", return_value=("ctx", {"context_strategy": "pfr"})):
            orchestrator.run_context_phase(item, table=self.table, runtime=runtime)

        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["status"], "SUPERSEDED")
        self.assertEqual(current["superseded_stage"], "context.pre_persist")

    def test_review_head_mismatch_becomes_superseded_before_generation(self):
        item = self._pending_item()
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr"},
            review_mode="high",
            table=self.table,
        )
        context_ready = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        with patch.object(orchestrator.pipeline_admission, "installation_token", return_value="token"), patch.object(
            orchestrator, "generate_review"
        ) as generate:
            orchestrator.run_review_phase(
                context_ready,
                table=self.table,
                runtime=_Runtime(head_sha="new-head"),
            )
        generate.assert_not_called()
        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["status"], "SUPERSEDED")
        self.assertEqual(current["superseded_stage"], "review.start")

    def test_closed_pr_is_superseded_before_review_generation(self):
        self._pending_item()
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr"},
            review_mode="high",
            table=self.table,
        )
        context_ready = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        runtime = _LifecycleRuntime(state="closed", merged=False)
        with patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(orchestrator, "generate_review") as generate:
            orchestrator.run_review_phase(
                context_ready,
                table=self.table,
                runtime=runtime,
            )

        generate.assert_not_called()
        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        self.assertEqual(current["status"], "SUPERSEDED")
        self.assertEqual(current["superseded_kind"], "pr_closed")
        self.assertEqual(current["superseded_stage"], "review.start")

    def test_pr_merged_after_generation_becomes_post_merge_follow_up(self):
        self._pending_item()
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr"},
            review_mode="high",
            table=self.table,
        )
        context_ready = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        runtime = _SequenceLifecycleRuntime(
            [
                {"head_sha": "abcdef123456", "state": "open", "merged": False},
                {"head_sha": "abcdef123456", "state": "closed", "merged": True},
            ]
        )
        generated = {
            "pr_review_comment": "summary",
            "inline_comments": [],
            "review_generation_status": "complete",
            "review_fallback_used": False,
            "review_publishable": True,
            "review_publication_safe": True,
            "v3_review": {
                "schema_version": 3,
                "decision": {
                    "verdict": "clear",
                    "public_sentence": "No review blocker found.",
                    "confidence": "high",
                    "pr_type": "code",
                    "risk_domains": [],
                    "reasons": [],
                },
                "owner_action": [],
                "findings": [],
                "material_unknowns": [],
                "evidence_scope": [],
                "diagram": None,
            },
        }
        provider_calls = [
            {
                "call_id": "context-call",
                "pipeline_phase": "context",
                "pipeline_attempt": 0,
                "phase": "route",
                "usage_state": "reported",
                "usage": {
                    "prompt_tokens": 80,
                    "completion_tokens": 20,
                    "total_tokens": 100,
                },
            },
            {
                "call_id": "review-call",
                "pipeline_phase": "review",
                "pipeline_attempt": 1,
                "phase": "deep_thinking",
                "usage_state": "reported",
                "usage": {
                    "prompt_tokens": 160,
                    "completion_tokens": 40,
                    "total_tokens": 200,
                },
            },
        ]
        with patch.object(
            orchestrator.config, "DRY_RUN", True
        ), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "generate_review", return_value=generated
        ) as generate, patch.object(
            orchestrator,
            "provider_call_records",
            return_value=provider_calls,
        ), patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
        ) as commit_prepared:
            orchestrator.run_review_phase(
                context_ready,
                table=self.table,
                runtime=runtime,
            )

        generate.assert_called_once()
        commit_prepared.assert_called_once()
        prepared = commit_prepared.call_args.args[0]
        context = commit_prepared.call_args.kwargs["context"]
        self.assertEqual(prepared.publication_kind, "post_merge_follow_up")
        self.assertEqual(prepared.comments, ())
        self.assertEqual(context.required_disposition, "merged_same_head")

    def test_live_pr_closed_after_finalize_uses_cancellation_publication(self):
        self._pending_item()
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr"},
            review_mode="high",
            table=self.table,
        )
        context_ready = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        runtime = _SequenceLifecycleRuntime(
            [
                {"head_sha": "abcdef123456", "state": "open", "merged": False},
                {"head_sha": "abcdef123456", "state": "open", "merged": False},
                {"head_sha": "abcdef123456", "state": "closed", "merged": False},
            ]
        )
        generated = {
            "pr_review_comment": "summary",
            "inline_comments": [],
            "review_generation_status": "complete",
            "review_fallback_used": False,
            "review_publishable": True,
            "review_publication_safe": True,
        }
        with patch.object(
            orchestrator.config, "DRY_RUN", False
        ), patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "generate_review", return_value=generated
        ), patch.object(
            orchestrator.pipeline_publication,
            "commit_prepared",
        ) as commit_prepared:
            orchestrator.run_review_phase(
                context_ready,
                table=self.table,
                runtime=runtime,
            )

        commit_prepared.assert_called_once()
        prepared = commit_prepared.call_args.args[0]
        context = commit_prepared.call_args.kwargs["context"]
        self.assertEqual(prepared.publication_kind, "lifecycle_cancellation")
        self.assertEqual(prepared.required_disposition, "closed_same_head")
        self.assertEqual(prepared.comments, ())
        self.assertEqual(context.required_disposition, "closed_same_head")
        self.assertEqual(runtime.pull.reviews, [])

    def test_retryable_review_error_rethrows_then_terminates_at_phase_cap(self):
        self._pending_item()
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={"context_strategy": "pfr"},
            review_mode="high",
            table=self.table,
        )
        context_ready = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        with patch.object(orchestrator.pipeline_admission, "installation_token", return_value="token"), patch.object(
            orchestrator, "generate_review", side_effect=RuntimeError("transient sdk error")
        ):
            with self.assertRaisesRegex(RuntimeError, "transient sdk error"):
                orchestrator.run_review_phase(context_ready, table=self.table, runtime=_Runtime())
            with self.assertRaisesRegex(RuntimeError, "transient sdk error"):
                orchestrator.run_review_phase(context_ready, table=self.table, runtime=_Runtime())
            orchestrator.run_review_phase(context_ready, table=self.table, runtime=_Runtime())

        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(current["status"], "ERROR")
        self.assertEqual(current["review_attempt"], 3)
        self.assertEqual(current["error_kind"], "unclassified_runtime_error")
        self.assertTrue(current["error_retryable"])
        self.assertTrue(current["error_retry_exhausted"])

    def test_discarded_review_attempt_reports_its_provider_usage(self):
        """A retried attempt loses its review, never its cost accounting."""

        records = []
        phases = [
            {
                "phase": "deep_thinking",
                "usage": {
                    "prompt_tokens": 20000,
                    "completion_tokens": 4240,
                    "total_tokens": 24240,
                },
            }
        ]
        with patch.object(
            pipeline_accounting,
            "_emit_pipeline_metric",
        ) as emit:
            emit.side_effect = lambda name, **fields: records.append(
                (name, fields)
            )
            pipeline_accounting.emit_discarded_attempt_usage(
                repo="owner/repo",
                pr_number=7,
                attempt=1,
                phases=phases,
            )
            pipeline_accounting.emit_discarded_attempt_usage(
                repo="owner/repo",
                pr_number=7,
                attempt=1,
                phases=[],
            )

        self.assertEqual(len(records), 1)
        name, fields = records[0]
        self.assertEqual(name, "discarded_attempt_usage")
        self.assertEqual(fields["total_tokens"], 24240)
        self.assertEqual(fields["phases"], "deep_thinking")

    def test_dry_run_artifact_keeps_normalized_review_and_typed_generation_fields(self):
        self._pending_item(dry_run=True)
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta={
                "context_strategy": "pfr",
                "evidence_catalog": [],
                "route_model_phases": [
                    {
                        "phase": "pr_analyzer",
                        "model": "deepseek-v4-flash",
                        "thinking": True,
                        "reasoning_effort": "low",
                        "attempt": 1,
                        "elapsed_seconds": 1.0,
                        "finish_reason": "stop",
                        "usage": {"prompt_tokens": 5, "total_tokens": 5},
                    }
                ],
                "pfr_model_phases": [
                    {
                        "phase": "route",
                        "model": "deepseek-v4-flash",
                        "thinking": True,
                        "reasoning_effort": "high",
                        "attempt": 1,
                        "elapsed_seconds": 1.0,
                        "finish_reason": "stop",
                        "usage": {"prompt_tokens": 10, "total_tokens": 10},
                    }
                ],
            },
            review_mode="high",
            table=self.table,
        )
        context_ready = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        generated = {
            "pr_review_comment": "summary",
            "inline_comments": [],
            "presentation_v1": {
                "version": "presentation_v1",
                "verdict": "unverified",
                "summary": "summary",
                "findings": [],
            },
            "v3_review": {
                "schema_version": 3,
                "decision": {"verdict": "unverified"},
            },
            "review_generation_status": "complete",
            "review_fallback_used": False,
            "review_publishable": True,
            "review_publication_safe": True,
            "review_model_finish_reason": "stop",
            "review_stage_finish_reasons": {
                "deep_judgment": "stop",
                "final_presentation": "stop",
            },
            "review_presentation_version": "presentation_v1",
            "review_presentation_selected_phase": "final_presentation",
            "review_presentation_normalizations": [
                "supporting_ci_ref_removed",
            ],
            "review_final_thinking": False,
            "review_final_reasoning_effort": "",
            "review_model_phases": [
                {
                    "phase": "deep_judgment",
                    "model": "deepseek-v4-pro",
                    "thinking": True,
                    "reasoning_effort": "max",
                    "attempt": 1,
                    "elapsed_seconds": 2.0,
                    "finish_reason": "stop",
                    "usage": {"completion_tokens": 20, "total_tokens": 20},
                },
                {
                    "phase": "final_presentation",
                    "model": "deepseek-v4-pro",
                    "thinking": False,
                    "reasoning_effort": "",
                    "attempt": 1,
                    "elapsed_seconds": 3.0,
                    "finish_reason": "stop",
                    "usage": {"completion_tokens": 30, "total_tokens": 30},
                },
            ],
            # This review-only subtotal must not survive as the table-level
            # end-to-end total once the PFR phase ledger is available.
            "deepseek_usage_total": {
                "completion_tokens": 50,
                "total_tokens": 50,
            },
            "quality_scoreable": True,
            "quality_exclusion_reasons": [],
            "review_quality_warnings": [],
        }
        with patch.object(orchestrator.pipeline_admission, "installation_token", return_value="token"), patch.object(
            orchestrator, "generate_review", return_value=generated
        ):
            orchestrator.run_review_phase(context_ready, table=self.table, runtime=_Runtime())

        current = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        artifact = persistence.load_review_artifact_from_item(current)
        self.assertEqual(current["status"], "PROCESSED_DRYRUN")
        self.assertEqual(current["review_generation_status"], "complete")
        self.assertTrue(current["review_publication_safe"])
        self.assertTrue(current["quality_scoreable"])
        self.assertEqual(artifact["v3_review"]["decision"]["verdict"], "unverified")
        self.assertEqual(artifact["presentation_v1"], generated["presentation_v1"])
        self.assertEqual(artifact["review_generation_status"], "complete")
        self.assertTrue(artifact["quality_scoreable"])
        self.assertEqual(current["pipeline_attempt"], 1)
        self.assertEqual(artifact["pipeline_attempt"], 1)
        self.assertEqual(current["publication_status"], "not_published")
        self.assertEqual(artifact["publication_status"], "not_published")
        self.assertNotIn("github_review_id", current)
        self.assertNotIn("github_review_id", artifact)
        self.assertEqual(current["deepseek_usage_total"]["total_tokens"], 60)
        self.assertEqual(artifact["deepseek_usage_total"]["total_tokens"], 60)
        self.assertEqual(
            [phase["phase"] for phase in artifact["deepseek_model_phases"]],
            [
                "route",
                "deep_judgment",
                "final_presentation",
            ],
        )
        for key in (
            "review_stage_finish_reasons",
            "review_presentation_version",
            "review_presentation_selected_phase",
            "review_presentation_normalizations",
            "review_final_thinking",
            "review_final_reasoning_effort",
        ):
            self.assertEqual(current[key], generated[key])
            self.assertEqual(artifact[key], generated[key])

    def test_dry_run_artifact_persists_v3_as_the_only_internal_review_shape(self):
        self._pending_item(dry_run=True)
        context_meta = {
            "context_strategy": "pfr",
            "evidence_catalog": [],
            "head_sha": "abcdef123456",
        }
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta=context_meta,
            review_mode="high",
            table=self.table,
        )
        context_ready = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        generated = build_v3_review(
            {
                "schema_version": 3,
                "decision": {
                    "verdict": "clear",
                    "public_sentence": "No review blocker found.",
                    "confidence": "high",
                    "pr_type": "code",
                    "risk_domains": [],
                    "reasons": [],
                },
                "owner_action": [],
                "findings": [],
                "material_unknowns": [],
                "evidence_scope": [],
                "diagram": None,
            },
            "details",
            context_meta=context_meta,
            strict=True,
        )
        generated.update(
            {
                "presentation_v1": {
                    "version": "presentation_v1",
                    "verdict": "clear",
                    "summary": "No review blocker found.",
                    "findings": [],
                },
                "review_generation_status": "complete",
                "review_fallback_used": False,
                "review_publishable": True,
                "review_publication_safe": True,
                "quality_scoreable": True,
                "quality_exclusion_reasons": [],
            }
        )

        with patch.object(
            orchestrator.pipeline_admission, "installation_token", return_value="token"
        ), patch.object(
            orchestrator, "generate_review", return_value=generated
        ):
            orchestrator.run_review_phase(
                context_ready,
                table=self.table,
                runtime=_Runtime(),
            )

        current = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        artifact = persistence.load_review_artifact_from_item(current)
        self.assertEqual(current["status"], "PROCESSED_DRYRUN")
        self.assertEqual(artifact["v3_review"]["schema_version"], 3)
        self.assertNotIn("v2_review", artifact)
        self.assertEqual(
            artifact["presentation_v1"],
            generated["presentation_v1"],
        )
        self.assertTrue(current["review_publication_safe"])
        self.assertNotIn("semantic_closure_version", current)
        self.assertNotIn("review_semantic_closure", artifact)

    def test_publishability_requires_explicit_safe_complete_presentation(self):
        valid = {
            "pr_review_comment": "No review blocker found.",
            "inline_comments": [],
            "review_generation_status": "complete",
            "review_fallback_used": False,
            "review_publishable": True,
            "review_publication_safe": True,
        }
        self.assertFalse(
            orchestrator.result_artifact.is_nonpublishable(valid)
        )

        for key, replacement in (
            ("review_generation_status", "incomplete"),
            ("review_fallback_used", True),
            ("review_publishable", False),
            ("review_publication_safe", False),
            ("pr_review_comment", ""),
            ("inline_comments", None),
        ):
            with self.subTest(key=key):
                candidate = {**valid, key: replacement}
                self.assertTrue(
                    orchestrator.result_artifact.is_nonpublishable(candidate)
                )

        closure_noise = {
            **valid,
            "semantic_closure_version": 1,
            "review_semantic_closure": {"terminal_valid": False},
        }
        self.assertFalse(
            orchestrator.result_artifact.is_nonpublishable(closure_noise)
        )

    def test_live_post_refresh_deciding_basis_loss_never_posts(self):
        self._pending_item(dry_run=False)
        context_meta = {
            "context_strategy": "pfr",
            "evidence_catalog": [],
            "head_sha": "abcdef123456",
        }
        persistence.store_context(
            "owner/repo",
            7,
            context_text="ctx",
            pr_details_text="details",
            meta=context_meta,
            review_mode="high",
            table=self.table,
        )
        context_ready = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        runtime = _Runtime()
        generated = {
            "pr_review_comment": "The generated finding blocks merge.",
            "inline_comments": [],
            "presentation_v1": {
                "version": "presentation_v1",
                "verdict": "block",
                "summary": "The generated finding blocks merge.",
                "findings": [
                    {
                        "id": "finding_1",
                        "required_evidence_refs": ["ci:integration"],
                    }
                ],
            },
            "v3_review": {
                "schema_version": 3,
                "decision": {"verdict": "block"},
            },
            "review_generation_status": "complete",
            "review_fallback_used": False,
            "review_publishable": True,
            "review_publication_safe": True,
            "quality_scoreable": True,
            "quality_exclusion_reasons": [],
            "review_model_phases": [],
        }
        invalidated = pipeline_ci.mark_ci_basis_change_nonpublishable(
            generated,
            invalidated_item_ids=["finding_1"],
        )

        with patch.object(
            orchestrator.config,
            "DRY_RUN",
            False,
        ), patch.object(
            orchestrator.pipeline_admission,
            "installation_token",
            return_value="token",
        ), patch.object(
            orchestrator,
            "generate_review",
            return_value=generated,
        ), patch.object(
            orchestrator,
            "reapply_latest_ci_guard",
            return_value=invalidated,
        ) as refresh:
            orchestrator.run_review_phase(
                context_ready,
                table=self.table,
                runtime=runtime,
            )

        refresh.assert_called_once()
        terminal = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 7}
        )["Item"]
        artifact = persistence.load_review_artifact_from_item(terminal)
        self.assertEqual(terminal["status"], "ERROR")
        self.assertEqual(
            terminal["error_kind"],
            "ci_deciding_evidence_changed_after_generation",
        )
        self.assertEqual(terminal["error_stage"], "ci_refresh")
        self.assertEqual(runtime.pull.reviews, [])
        self.assertFalse(artifact["review_publishable"])
        self.assertEqual(
            artifact["ci_evidence_invalidated_item_ids"],
            ["finding_1"],
        )
        self.assertNotIn("review_semantic_closure", artifact)

    def test_removed_paths_are_not_reread_for_inline_placement(self):
        runtime = _Runtime()
        runtime.pull.get_files = lambda: [SimpleNamespace(filename="deleted.py", status="removed")]
        runtime.get_file_content = Mock(side_effect=AssertionError("removed path must not be read"))
        _repo, _files, contents, meta = orchestrator._fetch_pr_files_and_contents(
            runtime,
            "owner/repo",
            7,
            "abcdef123456",
            target_paths={"deleted.py"},
        )
        self.assertEqual(contents, {})
        self.assertEqual(meta["removed_path_skip_count"], 1)
        self.assertEqual(meta["read_error_count"], 0)
        runtime.get_file_content.assert_not_called()

    def test_owned_github_runtime_pool_is_closed(self):
        item = self._pending_item()
        owned_runtime = _Runtime()
        owned_runtime.close = Mock()
        with patch.object(orchestrator.pipeline_admission, "installation_token", return_value="token"), patch.object(
            orchestrator, "GitHubRuntime", return_value=owned_runtime
        ), patch.object(
            orchestrator, "analyze_pr_complexity", return_value={"complexity": "low", "reason": "contained", "verification_plan": []}
        ):
            orchestrator.run_context_phase(item, table=self.table)
        owned_runtime.close.assert_called_once_with()

if __name__ == "__main__":
    unittest.main()

import unittest
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline import config
from lambdas.LlamaPReviewPipeline.pr_ingest import (
    GitHubRuntime,
    PR_INGEST_SOURCE_FILE_MAX_BYTES,
    PR_INGEST_SOURCE_TOTAL_MAX_BYTES,
    fetch_pr_details,
    has_existing_llamapreview_review,
    json_to_markdown,
    sanitize_pr_content_for_review,
    trim_pr_data,
)


class _RepoWrapper:
    repo = SimpleNamespace(description="A precise service toolkit.")

    def __str__(self):
        return "owner/repo - A precise service toolkit."


class _Runtime:
    def get_pr_content(self, repo_full_name, pr_number, *, context_lines=10, force_update=True):
        return {
            "pr_metadata": {"number": pr_number, "title": "Improve service", "description": "Human summary"},
            "commits": [
                {
                    "sha": "abcdef123456",
                    "author": "alice",
                    "date": "2026-01-01T00:00:00Z",
                    "stats": {"additions": 2, "deletions": 1, "total": 3},
                    "message": "Improve service\n\nBody",
                    "files": ["src/service.py"],
                }
            ],
            "related_issues": [{"issue_number": 9, "issue_content": "This is a Github Issue related to repo\n\nFix service bug"}],
            "ci_cd_results": {
                "state": "failure",
                "statuses": [{"context": "lint", "state": "success", "description": "ok", "target_url": "", "created_at": "c", "updated_at": "u"}],
                "check_runs": [{"name": "build", "status": "completed", "conclusion": "failure", "started_at": "s", "completed_at": "e", "details_url": "url"}],
            },
            "file_changes": [
                {
                    "file_path": "src/service.py",
                    "change_type": "modified",
                    "language": "Python",
                    "additions": 2,
                    "deletions": 1,
                    "changes": 3,
                    "change_categories": ["logic"],
                    "diff": "@@ -1 +1 @@\n-old\n+new\n",
                }
            ],
            "interactions": [
                {"author": "alice", "body": "Could we add a smoke test?"},
                {"author": "llamapreview[bot]", "body": "Auto Pull Request Review"},
                {"author": "dependabot[bot]", "body": "dependency notice"},
            ],
        }

    def get_repository(self, repo_full_name):
        return _RepoWrapper()


class TestPRIngestPipeline(unittest.TestCase):
    def test_github_runtime_enforces_bounded_changed_source_ingestion(self):
        repository = MagicMock()
        pull = SimpleNamespace(
            head=SimpleNamespace(sha="head-sha"),
            base=SimpleNamespace(sha="base-sha"),
        )
        repository.repo = SimpleNamespace(
            get_pull=MagicMock(return_value=pull)
        )
        repository.get_pr_content.return_value = {"file_changes": []}
        runtime = GitHubRuntime.__new__(GitHubRuntime)
        runtime.get_repository = MagicMock(return_value=repository)

        result = runtime.get_pr_content(
            "owner/repo",
            42,
            context_lines=10,
            force_update=True,
        )

        self.assertEqual(
            result,
            {
                "file_changes": [],
                "pr_metadata": {
                    "head_sha": "head-sha",
                    "base_sha": "base-sha",
                },
            },
        )
        repository.repo.get_pull.assert_called_once_with(42)
        repository.get_pr_content.assert_called_once_with(
            number=42,
            context_lines=10,
            force_update=True,
            source_file_max_bytes=PR_INGEST_SOURCE_FILE_MAX_BYTES,
            source_total_max_bytes=PR_INGEST_SOURCE_TOTAL_MAX_BYTES,
            pr=pull,
        )

    def test_fetch_pr_details_preserves_content_free_source_budget_telemetry(self):
        runtime = MagicMock()
        runtime.get_pr_content.return_value = {
            "pr_metadata": {
                "number": 42,
                "title": "Bound generated source",
                "description": "",
            },
            "file_changes": [
                {
                    "file_path": "generated/results.json",
                    "change_type": "modified",
                    "changes": 2,
                    "diff": (
                        "[SKIPPED] Bounded source unavailable and GitHub did not "
                        "provide a patch"
                    ),
                }
            ],
            "_retrieval_meta": {
                "file_content_budget": {
                    "mode": "bounded",
                    "file_max_bytes": PR_INGEST_SOURCE_FILE_MAX_BYTES,
                    "total_max_bytes": PR_INGEST_SOURCE_TOTAL_MAX_BYTES,
                    "attempted_reads": 2,
                    "retained_reads": 0,
                    "retained_source_bytes": 0,
                    "unavailable_or_oversize_reads": 2,
                    "budget_exhausted_reads": 0,
                    "api_patch_fallbacks": 0,
                    "missing_patch_fallbacks": 1,
                    "outcome": "partial",
                }
            },
        }
        runtime.get_repository.return_value = SimpleNamespace(
            repo=SimpleNamespace(description="Repository description")
        )

        pr_content, markdown = fetch_pr_details(runtime, "owner/repo", 42)

        budget = pr_content["_operational_facts"]["retrieval_meta"][
            "file_content_budget"
        ]
        self.assertEqual(budget["outcome"], "partial")
        self.assertEqual(budget["missing_patch_fallbacks"], 1)
        self.assertNotIn("file_content_budget", markdown)
        self.assertIn(
            "**Diff coverage:** unavailable (Bounded source unavailable",
            markdown,
        )
        self.assertNotIn("```diff\n[SKIPPED]", markdown)
        self.assertNotIn("[SKIPPED]", markdown)

    def test_github_runtime_head_snapshot_returns_head_and_lifecycle_from_one_pull(self):
        pull = SimpleNamespace(
            head=SimpleNamespace(sha="abcdef123456"),
            state="closed",
            merged=True,
            merged_at="2026-07-16T02:04:45Z",
            locked=True,
        )
        repo = SimpleNamespace(repo=SimpleNamespace(get_pull=MagicMock(return_value=pull)))
        runtime = GitHubRuntime.__new__(GitHubRuntime)
        runtime.get_repository = MagicMock(return_value=repo)

        snapshot = runtime.get_pr_head_snapshot("owner/repo", 42)

        self.assertEqual(
            snapshot,
            {
                "head_sha": "abcdef123456",
                "state": "closed",
                "merged": True,
                "locked": True,
            },
        )
        runtime.get_repository.assert_called_once_with("owner/repo")
        repo.repo.get_pull.assert_called_once_with(42)

    def test_github_runtime_head_snapshot_does_not_guess_missing_lock_state(self):
        pull = SimpleNamespace(
            head=SimpleNamespace(sha="abcdef123456"),
            state="closed",
            merged=True,
            merged_at="2026-07-16T02:04:45Z",
        )
        repo = SimpleNamespace(
            repo=SimpleNamespace(get_pull=MagicMock(return_value=pull))
        )
        runtime = GitHubRuntime.__new__(GitHubRuntime)
        runtime.get_repository = MagicMock(return_value=repo)

        snapshot = runtime.get_pr_head_snapshot("owner/repo", 42)

        self.assertIsNone(snapshot["locked"])

    def test_github_runtime_head_snapshot_does_not_coerce_malformed_lock_state(self):
        pull = SimpleNamespace(
            head=SimpleNamespace(sha="abcdef123456"),
            state="closed",
            merged=True,
            merged_at="2026-07-16T02:04:45Z",
            locked="true",
        )
        repo = SimpleNamespace(
            repo=SimpleNamespace(get_pull=MagicMock(return_value=pull))
        )
        runtime = GitHubRuntime.__new__(GitHubRuntime)
        runtime.get_repository = MagicMock(return_value=repo)

        snapshot = runtime.get_pr_head_snapshot("owner/repo", 42)

        self.assertIsNone(snapshot["locked"])

    def test_github_runtime_avoids_cleanup_thread_and_closes_client(self):
        client = MagicMock()
        pool_close = MagicMock()
        init_contract = {}

        class FakeAuthManager:
            def authenticate_with_token(self, installation_token):
                init_contract["token"] = installation_token
                return client

        class FakeRepositoryPool:
            def __init__(self, github, *, cleanup_enabled):
                init_contract["github"] = github
                init_contract["cleanup_enabled"] = cleanup_enabled
                self.close = pool_close

            def get_repository(self, *args, **kwargs):
                raise AssertionError("not used by this lifecycle test")

        class FakeGitHubAPIHandler:
            def __init__(self, github, *, pool):
                init_contract["api_github"] = github
                init_contract["api_pool"] = pool

        modules = {
            "llama_github": ModuleType("llama_github"),
            "llama_github.data_retrieval": ModuleType(
                "llama_github.data_retrieval"
            ),
            "llama_github.github_integration": ModuleType(
                "llama_github.github_integration"
            ),
            "llama_github.data_retrieval.github_api": ModuleType(
                "llama_github.data_retrieval.github_api"
            ),
            "llama_github.data_retrieval.github_entities": ModuleType(
                "llama_github.data_retrieval.github_entities"
            ),
            "llama_github.github_integration.github_auth_manager": ModuleType(
                "llama_github.github_integration.github_auth_manager"
            ),
        }
        modules[
            "llama_github.data_retrieval.github_api"
        ].GitHubAPIHandler = FakeGitHubAPIHandler
        modules[
            "llama_github.data_retrieval.github_entities"
        ].RepositoryPool = FakeRepositoryPool
        modules[
            "llama_github.github_integration.github_auth_manager"
        ].GitHubAuthManager = FakeAuthManager

        with patch.dict(sys.modules, modules):
            runtime = GitHubRuntime("installation-token")
            runtime.close()

        self.assertEqual(init_contract["token"], "installation-token")
        self.assertIs(init_contract["github"], client)
        self.assertFalse(init_contract["cleanup_enabled"])
        pool_close.assert_called_once_with()
        client.close.assert_called_once_with()

    def test_sanitized_pr_details_filters_bot_interactions_but_keeps_duplicate_guard(self):
        pr_content, markdown = fetch_pr_details(_Runtime(), "owner/repo", 42)

        self.assertTrue(has_existing_llamapreview_review(pr_content))
        self.assertIn("alice", str(pr_content.get("interactions")))
        self.assertNotIn("[bot]", str(pr_content.get("interactions")))
        self.assertIn("repo_description", pr_content["pr_metadata"])
        self.assertEqual(pr_content["pr_metadata"]["repo_description"], "owner/repo - A precise service toolkit.")

        self.assertIn("owner/repo - A precise service toolkit.", markdown)
        self.assertIn("Could we add a smoke test?", markdown)
        self.assertNotIn("llamapreview[bot]", markdown)
        self.assertNotIn("dependabot[bot]", markdown)
        self.assertIn("## CI/CD Results", markdown)
        self.assertIn("Conclusion: failure", markdown)
        self.assertIn("## Related Issues", markdown)
        self.assertIn("Fix service bug", markdown)
        self.assertIn("### Commit [abcdef1]", markdown)
        self.assertIn("**Modified files:**", markdown)
        self.assertFalse(pr_content["_ingest_meta"]["pr_details_compacted"])
        self.assertEqual(markdown, json_to_markdown(pr_content))

    def test_sanitize_preserves_human_interactions_and_records_filtered_count(self):
        raw = {
            "pr_metadata": {
                "number": 1,
                "description": "Intro\n\n## Auto Pull Request Review\nold bot review\n\n## Human Notes\nkeep me",
            },
            "interactions": [
                {"author": "reviewer", "body": "Looks risky."},
                {"author": "renovate[bot]", "body": "lockfile update"},
            ],
        }

        sanitized = sanitize_pr_content_for_review(raw, repo_description="Repo description")

        self.assertEqual(sanitized["interactions"], [{"author": "reviewer", "body": "Looks risky."}])
        self.assertEqual(sanitized["_ingest_meta"]["filtered_bot_interaction_count"], 1)
        self.assertEqual(sanitized["_ingest_meta"]["cleaned_bot_generated_block_count"], 1)
        self.assertIn("Human Notes", sanitized["pr_metadata"]["description"])
        self.assertNotIn("old bot review", sanitized["pr_metadata"]["description"])
        self.assertFalse(sanitized["_ingest_meta"]["raw_llamapreview_review_present"])
        self.assertFalse(has_existing_llamapreview_review(sanitized))

    def test_pr_details_max_chars_default_stays_expanded(self):
        self.assertEqual(config.PR_DETAILS_MAX_CHARS, 250000)

    def test_fetch_compacts_large_multifile_markdown_without_losing_retrieval_source(self):
        runtime = MagicMock()
        changes = [
            {
                "file_path": f"generated/part_{index:02d}.txt",
                "change_type": "modified",
                "additions": 560,
                "deletions": 1,
                "diff": (
                    "@@ -1 +1,560 @@\n-old\n"
                    + "\n".join(
                        f"+part_{index}_{line} = {line}" for line in range(560)
                    )
                ),
            }
            for index in range(68)
        ]
        runtime.get_pr_content.return_value = {
            "pr_metadata": {
                "number": 42,
                "title": "Regenerate bounded artifacts",
                "description": "Keep the generated corpus current.",
            },
            "file_changes": changes,
            "interactions": [],
        }
        runtime.get_repository.return_value = SimpleNamespace(
            repo=SimpleNamespace(description="Repository description")
        )

        pr_content, markdown = fetch_pr_details(
            runtime,
            "owner/repo",
            42,
            max_chars=20_000,
        )

        packing = pr_content["_ingest_meta"]
        self.assertEqual(len(pr_content["file_changes"]), 68)
        self.assertEqual(
            pr_content["file_changes"][-1]["diff"],
            changes[-1]["diff"],
        )
        self.assertLessEqual(len(markdown), 20_000)
        self.assertTrue(packing["pr_details_compacted"])
        self.assertGreater(
            packing["source_pr_details_chars"],
            packing["model_pr_details_chars"],
        )
        self.assertGreater(
            packing["source_pr_details_chars"],
            config.LARGE_PR_MAX_CHARS,
        )
        self.assertIn("### generated/part_00.txt", markdown)
        self.assertIn("### generated/part_67.txt", markdown)
        self.assertIn("**Diff coverage:** partial", markdown)
        self.assertNotIn("Review not run", markdown)

    def test_markdown_emits_description_once_outside_metadata(self):
        description = "Dependency release notes " + ("x" * 2000)
        markdown = json_to_markdown(
            {
                "pr_metadata": {
                    "number": 7,
                    "title": "Update dependencies",
                    "description": description,
                    "author": "dependabot[bot]",
                },
                "file_changes": [],
            }
        )

        metadata, rendered_description = markdown.split("## Description\n", 1)
        self.assertNotIn(description, metadata)
        self.assertEqual(rendered_description.count(description), 1)
        self.assertIn("- **author**: dependabot[bot]", metadata)

    def test_pr_details_trim_does_not_hard_truncate_large_diff(self):
        large_added_line = "x" * (config.PR_DETAILS_MAX_CHARS + 1000)
        raw = {
            "pr_metadata": {"number": 9, "title": "large diff"},
            "commits": [{"sha": "abc"}],
            "related_issues": [{"id": 1}],
            "ci_cd_results": {"status": "ok"},
            "file_changes": [
                {
                    "file_path": "src/large.py",
                    "change_type": "modified",
                    "language": "Python",
                    "additions": 1,
                    "deletions": 1,
                    "changes": 2,
                    "change_categories": ["logic"],
                    "diff": "@@ -1 +1 @@\n-old\n+" + large_added_line,
                }
            ],
            "interactions": [],
        }

        trimmed = trim_pr_data(raw, max_size=10)
        markdown = json_to_markdown(trimmed)

        self.assertEqual(trimmed["commits"], [])
        self.assertEqual(trimmed["related_issues"], [])
        self.assertEqual(trimmed["ci_cd_results"], [])
        self.assertGreater(len(markdown), config.PR_DETAILS_MAX_CHARS)
        self.assertIn(large_added_line[-100:], markdown)


if __name__ == "__main__":
    unittest.main()

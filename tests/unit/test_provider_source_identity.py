from __future__ import annotations

from copy import deepcopy
import unittest

from tests.unit.fakes import (
    ensure_repo_root_on_path,
    install_fake_jwt_module,
    install_fake_requests_module,
    set_default_env,
)

ensure_repo_root_on_path()
set_default_env()
install_fake_requests_module()
install_fake_jwt_module()

from lambdas.LlamaPReviewPipeline.context_engine.repo_structure import (
    RepoInventory,
)
from lambdas.LlamaPReviewPipeline.errors import (
    ProviderSourceIdentityMismatch,
)
from lambdas.LlamaPReviewPipeline.pipeline_ci import with_current_ci_snapshot
from lambdas.LlamaPReviewPipeline.pr_ingest import json_to_markdown
from lambdas.LlamaPReviewPipeline.provider_source import (
    PreparedProviderSource,
    build_provider_source_identity,
)
from lambdas.LlamaPReviewPipeline.provider_source_identity import sha256_value
from lambdas.LlamaPReviewPipeline.review.analyzer import (
    analyze_pr_complexity,
    build_route_digest,
)
from lambdas.LlamaPReviewPipeline.review.evidence_contract import (
    build_ci_snapshot,
)


REPO = "owner/repo"
PR_NUMBER = 41
HEAD_SHA = "head123456789"
DEFAULT_BRANCH = "main"
ROUTE_DIGEST_MAX_CHARS = 60_000


def _ci_results() -> dict:
    return {
        "head_sha": HEAD_SHA,
        "state": "failure",
        "statuses": [],
        "check_runs": [
            {
                "id": 101,
                "name": "unit-tests",
                "status": "completed",
                "conclusion": "failure",
                "details_url": "https://example.test/checks/101",
                "completed_at": "2026-08-01T01:02:03Z",
                "output": {
                    "title": "Tests failed",
                    "summary": "One assertion failed",
                    "text": "Inspect the failing assertion.",
                },
                "annotations": [
                    {
                        "path": "src/app.py",
                        "start_line": 9,
                        "end_line": 9,
                        "annotation_level": "failure",
                        "title": "Assertion failure",
                        "message": "Expected 2 but observed 3.",
                    }
                ],
            }
        ],
        "_retrieval_meta": {
            "ci_aggregate": {"outcome": "ok"},
            "ci_actionable_details": {
                "outcome": "ok",
                "attempted_check_count": 1,
                "enriched_check_count": 1,
                "annotation_count": 1,
                "error_count": 0,
            },
        },
    }


def _pr_content() -> dict:
    return {
        "pr_metadata": {
            "number": PR_NUMBER,
            "title": "Preserve source identity",
            "body": "The structured pull-request body.",
            "description": "The human-facing pull-request description.",
            "repo_description": "A deterministic review service.",
            "base_branch": DEFAULT_BRANCH,
            "head_branch": "provider-source",
            "head_sha": HEAD_SHA,
            "draft": False,
        },
        "related_issues": [
            {
                "issue_number": 19,
                "issue_content": "The provider must see the exact issue contract.",
            }
        ],
        "interactions": [
            {
                "author": "reviewer",
                "body": "Please preserve exact-head behavior.",
            }
        ],
        "file_changes": [
            {
                "file_path": "src/app.py",
                "change_type": "modified",
                "additions": 1,
                "deletions": 1,
                "diff": "@@ -9 +9 @@\n-return 2\n+return 3\n",
            }
        ],
        "_operational_facts": {
            "ci_cd_results": _ci_results(),
            "retrieval_meta": {"pr": {"outcome": "ok"}},
            "canonical_set_probe": {"alpha", "beta"},
        },
    }


def _inventory() -> RepoInventory:
    return RepoInventory(
        repository=REPO,
        requested_sha=HEAD_SHA,
        status="complete",
        tree_truncated=False,
        items=[
            {"path": "src/app.py", "type": "blob", "size": 120},
            {"path": "docs/contract.md", "type": "blob", "size": 80},
        ],
        discoverable_files={"src/app.py", "docs/contract.md"},
        excluded_sensitive={"secrets.pem", ".env"},
    )


def _prepared(content: dict) -> PreparedProviderSource:
    raw_ci = content["_operational_facts"]["ci_cd_results"]
    ci_snapshot = build_ci_snapshot(raw_ci)
    base_pr_details = json_to_markdown(content)
    return PreparedProviderSource(
        pr_content=content,
        base_pr_details=base_pr_details,
        model_pr_details=with_current_ci_snapshot(base_pr_details, ci_snapshot),
        ci_snapshot=ci_snapshot,
    )


def _receipt(
    content: dict | None = None,
    inventory: RepoInventory | None = None,
    *,
    prepared: PreparedProviderSource | None = None,
) -> dict:
    source = prepared or _prepared(content or _pr_content())
    return build_provider_source_identity(
        source,
        inventory or _inventory(),
        repo=REPO,
        pr_number=PR_NUMBER,
        head_sha=HEAD_SHA,
        default_branch=DEFAULT_BRANCH,
        route_digest_max_chars=ROUTE_DIGEST_MAX_CHARS,
    )


def _set_nested(value: dict, path: tuple[object, ...], replacement: object) -> None:
    target = value
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = replacement  # type: ignore[index]


def _reverse_mapping_order(value):
    if isinstance(value, dict):
        return {
            key: _reverse_mapping_order(value[key])
            for key in reversed(list(value))
        }
    if isinstance(value, list):
        return [_reverse_mapping_order(item) for item in value]
    if isinstance(value, set):
        return set(reversed(sorted(value)))
    return value


class _CountingClient:
    def __init__(self) -> None:
        self.chat_calls = 0

    def chat(self, _messages, **_kwargs):
        self.chat_calls += 1
        raise AssertionError("provider dispatch must not occur")


class ProviderSourceIdentityTest(unittest.TestCase):
    def test_each_provider_visible_component_mutation_changes_top_identity(self):
        baseline = _receipt()["sha256"]
        content_mutations = (
            (
                "pr title",
                ("pr_metadata", "title"),
                "A different title",
            ),
            (
                "pr body",
                ("pr_metadata", "body"),
                "A different structured body.",
            ),
            (
                "repository description",
                ("pr_metadata", "repo_description"),
                "A different repository purpose.",
            ),
            (
                "human interaction",
                ("interactions", 0, "body"),
                "This interaction changes the review evidence.",
            ),
            (
                "related issue",
                ("related_issues", 0, "issue_content"),
                "A different issue contract.",
            ),
            (
                "changed-file diff",
                ("file_changes", 0, "diff"),
                "@@ -9 +9 @@\n-return 2\n+return 4\n",
            ),
            (
                "actionable CI output",
                (
                    "_operational_facts",
                    "ci_cd_results",
                    "check_runs",
                    0,
                    "output",
                    "summary",
                ),
                "A different failing assertion was reported.",
            ),
            (
                "actionable CI annotation",
                (
                    "_operational_facts",
                    "ci_cd_results",
                    "check_runs",
                    0,
                    "annotations",
                    0,
                    "message",
                ),
                "The exact failing line changed.",
            ),
            (
                "actionable CI retrieval metadata",
                (
                    "_operational_facts",
                    "ci_cd_results",
                    "_retrieval_meta",
                    "ci_actionable_details",
                    "error_count",
                ),
                1,
            ),
        )

        for label, path, replacement in content_mutations:
            with self.subTest(component=label):
                content = _pr_content()
                _set_nested(content, path, replacement)
                self.assertNotEqual(_receipt(content=content)["sha256"], baseline)

        inventory_mutations = (
            ("inventory path", "path"),
            ("inventory status", "status"),
            ("inventory truncation", "tree_truncated"),
        )
        for label, mutation in inventory_mutations:
            with self.subTest(component=label):
                inventory = _inventory()
                if mutation == "path":
                    inventory.items[0]["path"] = "src/renamed.py"
                    inventory.discoverable_files.remove("src/app.py")
                    inventory.discoverable_files.add("src/renamed.py")
                elif mutation == "status":
                    inventory.status = "partial"
                else:
                    inventory.tree_truncated = True
                self.assertNotEqual(
                    _receipt(inventory=inventory)["sha256"], baseline
                )

    def test_mapping_set_and_inventory_order_do_not_change_identity(self):
        prepared = _prepared(_pr_content())
        reordered_prepared = PreparedProviderSource(
            pr_content=_reverse_mapping_order(deepcopy(prepared.pr_content)),
            base_pr_details=prepared.base_pr_details,
            model_pr_details=prepared.model_pr_details,
            ci_snapshot=_reverse_mapping_order(deepcopy(prepared.ci_snapshot)),
        )
        inventory = _inventory()
        reordered_inventory = RepoInventory(
            repository=inventory.repository,
            requested_sha=inventory.requested_sha,
            status=inventory.status,
            tree_truncated=inventory.tree_truncated,
            items=[
                _reverse_mapping_order(item)
                for item in reversed(inventory.items)
            ],
            discoverable_files=set(
                reversed(sorted(inventory.discoverable_files))
            ),
            excluded_sensitive=set(
                reversed(sorted(inventory.excluded_sensitive))
            ),
            error=inventory.error,
        )

        self.assertEqual(
            _receipt(prepared=prepared, inventory=inventory),
            _receipt(
                prepared=reordered_prepared,
                inventory=reordered_inventory,
            ),
        )

    def test_route_input_hash_is_the_exact_build_route_digest_hash(self):
        content = _pr_content()
        prepared = _prepared(content)
        inventory = _inventory()
        receipt = _receipt(prepared=prepared, inventory=inventory)
        route_digest = build_route_digest(
            prepared.model_pr_details,
            prepared.pr_content,
            max_chars=ROUTE_DIGEST_MAX_CHARS,
            repo_inventory=inventory,
            head_sha=HEAD_SHA,
        )

        self.assertEqual(
            receipt["route_input_sha256"],
            sha256_value(route_digest),
        )
        self.assertEqual(
            receipt["route_input_sha256"],
            receipt["component_sha256"]["route_digest"],
        )

    def test_route_input_mismatch_raises_before_client_chat(self):
        content = _pr_content()
        inventory = _inventory()
        client = _CountingClient()

        with self.assertRaises(ProviderSourceIdentityMismatch):
            analyze_pr_complexity(
                "provider-visible PR details",
                pr_content=content,
                repo_inventory=inventory,
                client=client,
                expected_route_input_sha256=sha256_value(
                    {"not": "the route digest"}
                ),
            )

        self.assertEqual(client.chat_calls, 0)


if __name__ == "__main__":
    unittest.main()

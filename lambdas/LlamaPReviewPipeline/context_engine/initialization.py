"""Initialize one exact-head bounded repository collection."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .. import config
from .repo_structure import RepoInventory, get_repo_structure_for_llm
from .state import CollectionState


def initialize_collection(
    *,
    runtime: Any,
    github_token: str,
    repo_full_name: str,
    pr_content: Dict[str, Any],
    pr_details: str,
    head_sha: str,
    default_branch: str,
    time_budget: int = config.PFR_HIGH_TIME_BUDGET_SECONDS,
    token_budget: int = config.PFR_HIGH_TOKEN_BUDGET,
    max_tool_rounds: int = config.PFR_HIGH_MAX_TOOL_ROUNDS,
    max_search_calls: int = config.PFR_MAX_SEARCH_CALLS,
    max_read_calls: int = config.PFR_MAX_READ_CALLS,
    max_context_chars: int = config.PFR_HIGH_MAX_CONTEXT_CHARS,
    deadline: Any = None,
    repo_inventory: Optional[RepoInventory] = None,
    initial_evidence_ledger: Optional[Dict[str, Any]] = None,
) -> CollectionState:
    """Build the shared exact-head inventory and bounded collection state."""

    full_tree: Dict[str, Any] = {}
    inventory = repo_inventory
    if not isinstance(inventory, RepoInventory):
        full_tree = get_repo_structure_for_llm(
            repo_full_name,
            token=github_token,
            sha=head_sha,
            max_depth=99,
            include_file_list=True,
            include_summary=False,
            exclude_hidden=False,
            deadline=deadline,
        )
        inventory = full_tree.get("_inventory")
        if not isinstance(inventory, RepoInventory):
            # Injected adapters may still return the older tree shape.
            fallback_paths = {
                item["path"]
                for item in full_tree.get("files", [])
                if item.get("path")
            }
            inventory = RepoInventory(
                repository=repo_full_name,
                requested_sha=head_sha,
                status="complete" if "error" not in full_tree else "error",
                discoverable_files=set(fallback_paths),
                error=str(full_tree.get("error") or ""),
            )

    shallow = (
        inventory.render_tree(
            max_depth=2,
            include_summary=False,
            include_file_list=False,
        )
        if inventory.items
        else {"tree": full_tree.get("tree", "")}
    )
    removed_paths = {
        str(change.get("file_path"))
        for change in pr_content.get("file_changes") or []
        if isinstance(change, dict)
        and str(
            change.get("change_type") or change.get("status") or ""
        ).lower()
        in {"removed", "deleted"}
        and change.get("file_path")
    }
    state = CollectionState(
        pr_details=pr_details,
        pr_content=pr_content,
        repo_full_name=repo_full_name,
        head_sha=head_sha,
        default_branch=default_branch,
        runtime=runtime,
        deadline=deadline,
        root_tree=shallow.get("tree", "") or full_tree.get("tree", ""),
        repo_inventory=inventory,
        accessible_files=set(inventory.discoverable_files),
        removed_paths=removed_paths,
        time_budget=time_budget,
        token_budget=token_budget,
        max_tool_rounds=max_tool_rounds,
        max_search_calls=max_search_calls,
        max_read_calls=max_read_calls,
        max_context_chars=max_context_chars,
    )
    state.evidence_ledger.ingest_meta(initial_evidence_ledger)
    return state

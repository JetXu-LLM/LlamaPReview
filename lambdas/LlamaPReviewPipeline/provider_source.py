"""Canonical identity for the exact source facts that can shape provider work.

The Context phase records one content-free receipt for exact-head provenance;
full source material remains in private context and trace artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from . import config, pipeline_admission
from .context_engine.repo_structure import RepoInventory, fetch_repo_inventory
from .errors import (
    CIRefreshUnavailable,
    HeadSuperseded,
)
from .pipeline_ci import with_current_ci_snapshot
from .pr_ingest import fetch_pr_details
from .provider_source_identity import (
    PROVIDER_SOURCE_SCHEMA,
    provider_source_receipt_sha256,
    sha256_value,
)
from .review.analyzer import build_route_digest
from .review.evidence_contract import build_ci_snapshot


@dataclass(frozen=True, slots=True)
class PreparedProviderSource:
    """One production-shaped PR/CI observation before repository inventory."""

    pr_content: Dict[str, Any]
    base_pr_details: str
    model_pr_details: str
    ci_snapshot: Dict[str, Any]


def prepare_provider_source(
    runtime: Any,
    repo: str,
    pr_number: int,
    head_sha: str,
    *,
    pr_details_max_chars: Optional[int] = None,
) -> PreparedProviderSource:
    """Read the same sanitized PR and actionable CI used by Context."""

    pr_content, base_pr_details = fetch_pr_details(
        runtime,
        repo,
        int(pr_number),
        max_chars=pr_details_max_chars,
    )
    pipeline_admission.assert_current_head(
        runtime,
        repo,
        int(pr_number),
        head_sha,
        pr_content=pr_content,
        stage="context.ingest",
    )
    ci_getter = getattr(runtime, "get_ci_results_for_head", None)
    if not callable(ci_getter):
        raise CIRefreshUnavailable(
            "Runtime does not support exact-head CI retrieval during context ingest",
            stage="context.ci_refresh",
        )
    raw_ci = ci_getter(
        repo,
        head_sha,
        include_actionable_details=True,
    )
    if not isinstance(raw_ci, dict):
        raise CIRefreshUnavailable(
            "Exact-head CI retrieval returned an invalid payload during context ingest",
            stage="context.ci_refresh",
        )
    sampled_head = str(raw_ci.get("head_sha") or head_sha)
    if sampled_head != head_sha:
        raise HeadSuperseded(
            head_sha, sampled_head, stage="context.ci_refresh"
        )
    ci_snapshot = build_ci_snapshot(raw_ci)
    if ci_snapshot.get("retrieval_outcome") == "error":
        raise CIRefreshUnavailable(
            "Current-head CI evidence retrieval failed during context ingest",
            stage="context.ci_refresh",
        )
    operational_facts = pr_content.get("_operational_facts")
    if not isinstance(operational_facts, dict):
        operational_facts = {}
        pr_content["_operational_facts"] = operational_facts
    operational_facts["ci_cd_results"] = raw_ci
    operational_facts["ci_snapshot"] = ci_snapshot
    return PreparedProviderSource(
        pr_content=pr_content,
        base_pr_details=base_pr_details,
        model_pr_details=with_current_ci_snapshot(
            base_pr_details, ci_snapshot
        ),
        ci_snapshot=ci_snapshot,
    )


def repository_inventory_projection(
    inventory: RepoInventory,
) -> Dict[str, Any]:
    """Project only exact-head facts available before the first model call."""

    if not isinstance(inventory, RepoInventory):
        raise TypeError("provider source requires a RepoInventory")
    items = [
        {
            "path": str(item.get("path") or ""),
            "type": str(item.get("type") or ""),
            "size": int(item.get("size") or 0),
        }
        for item in inventory.items
        if isinstance(item, Mapping)
    ]
    items.sort(key=lambda item: (item["path"], item["type"], item["size"]))
    return {
        "repository": str(inventory.repository or ""),
        "requested_sha": str(inventory.requested_sha or ""),
        "status": str(inventory.status or ""),
        "tree_truncated": bool(inventory.tree_truncated),
        "items": items,
        "discoverable_files": sorted(inventory.discoverable_files),
        "excluded_sensitive_paths": sorted(inventory.excluded_sensitive),
        "error": str(inventory.error or ""),
    }


def build_provider_source_identity(
    prepared: PreparedProviderSource,
    inventory: RepoInventory,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    default_branch: str,
    route_digest_max_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Return a content-free digest of every deterministic provider source."""

    inventory_projection = repository_inventory_projection(inventory)
    route_digest = build_route_digest(
        prepared.model_pr_details,
        prepared.pr_content,
        max_chars=(
            config.PFR_PLAN_DIGEST_MAX_CHARS
            if route_digest_max_chars is None
            else int(route_digest_max_chars)
        ),
        repo_inventory=inventory,
        head_sha=head_sha,
    )
    components: Dict[str, Any] = {
        "identity": {
            "repo": str(repo),
            "pr_number": int(pr_number),
            "head_sha": str(head_sha),
            "default_branch": str(default_branch),
        },
        "pr_content": prepared.pr_content,
        "model_pr_details": prepared.model_pr_details,
        "ci_snapshot": prepared.ci_snapshot,
        "repo_inventory": inventory_projection,
        "route_digest": route_digest,
    }
    component_sha256 = {
        name: sha256_value(value) for name, value in components.items()
    }
    return {
        "schema": PROVIDER_SOURCE_SCHEMA,
        "sha256": provider_source_receipt_sha256(component_sha256),
        "component_sha256": component_sha256,
        "route_input_sha256": component_sha256["route_digest"],
    }


def capture_provider_source_identity(
    runtime: Any,
    *,
    token: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    default_branch: str,
    pr_details_max_chars: Optional[int] = None,
    route_digest_max_chars: Optional[int] = None,
    deadline: Optional[Any] = None,
) -> Dict[str, Any]:
    """Use the production read/projection path for an external preflight."""

    prepared = prepare_provider_source(
        runtime,
        repo,
        int(pr_number),
        head_sha,
        pr_details_max_chars=pr_details_max_chars,
    )
    inventory = fetch_repo_inventory(
        repo,
        token=token,
        sha=head_sha,
        deadline=deadline,
    )
    return build_provider_source_identity(
        prepared,
        inventory,
        repo=repo,
        pr_number=int(pr_number),
        head_sha=head_sha,
        default_branch=default_branch,
        route_digest_max_chars=route_digest_max_chars,
    )

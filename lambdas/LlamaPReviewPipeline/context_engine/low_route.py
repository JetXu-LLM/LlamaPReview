"""Bounded exact-head context collection for low-complexity changes."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..repository_paths import bounded_read_opt_in
from .evidence import EvidenceLedger
from .repo_structure import (
    RepoInventory,
    is_sensitive_repo_path,
    normalize_repo_path,
)


__all__ = ["collect_low_same_file_context"]


_MAX_FILES = 2
_MAX_FILE_BYTES = 50 * 1024
_MAX_TOTAL_BYTES = 80 * 1024


def collect_low_same_file_context(
    *,
    runtime: Any,
    repo: str,
    pr_content: Dict[str, Any],
    head_sha: str,
    repo_inventory: Optional[RepoInventory],
    initial_evidence_ledger: Optional[Dict[str, Any]],
) -> tuple[str, Dict[str, Any]]:
    """Read at most two small changed files without turning low into PFR."""

    ledger = EvidenceLedger(expected_head_sha=head_sha)
    ledger.ingest_meta(initial_evidence_ledger or {})
    parts: list[str] = []
    outcomes: Dict[str, int] = {}
    backend_attempted = 0
    collected = 0
    total_bytes = 0

    for change in pr_content.get("file_changes") or []:
        if collected >= _MAX_FILES or backend_attempted >= _MAX_FILES:
            break
        if not isinstance(change, dict):
            continue
        path = normalize_repo_path(str(change.get("file_path") or ""))
        if (
            not path
            or is_sensitive_repo_path(path)
            or str(change.get("change_type") or "").strip().lower()
            in {"deleted", "removed"}
        ):
            continue
        if (
            repo_inventory is None
            or repo_inventory.exact_path_state(path) != "present"
        ):
            continue
        advertised_size = repo_inventory.file_size_bytes(path)
        if (
            isinstance(advertised_size, int)
            and (
                advertised_size > _MAX_FILE_BYTES
                or total_bytes + advertised_size > _MAX_TOTAL_BYTES
            )
        ):
            outcomes["oversize_preflight"] = (
                outcomes.get("oversize_preflight", 0) + 1
            )
            continue

        question_id = ledger.register_question(
            question=f"Read the exact-head changed file {path}.",
            tool="read_file",
            args={"path": path, "mode": "content"},
        )
        try:
            result = runtime.read_text_file_bounded(
                repo,
                path,
                sha=head_sha,
                opt_in=bounded_read_opt_in(path),
            )
        except Exception:
            result = {
                "outcome": "error",
                "error_kind": "bounded_read_error",
            }
        outcome = str(result.get("outcome") or "error")
        if outcome != "excluded_by_policy":
            backend_attempted += 1
        content = result.get("content")
        source_size = result.get("source_size_bytes")
        bytes_read = result.get("bytes_read")
        content_bytes = (
            len(content.encode("utf-8"))
            if isinstance(content, str)
            else 0
        )
        complete = bool(
            outcome == "success"
            and isinstance(content, str)
            and isinstance(source_size, int)
            and not isinstance(source_size, bool)
            and isinstance(bytes_read, int)
            and not isinstance(bytes_read, bool)
            and source_size <= _MAX_FILE_BYTES
            and bytes_read >= source_size
            and content_bytes >= source_size
            and total_bytes + source_size <= _MAX_TOTAL_BYTES
        )
        if complete:
            total_bytes += source_size
            collected += 1
            outcomes["success"] = outcomes.get("success", 0) + 1
            ledger.record_event(
                question_id=question_id,
                tool="read_file",
                args={"path": path, "mode": "content"},
                outcome="hit",
                paths=[path],
                source_ref=f"pr_head:{head_sha}",
                head_reread_outcome="success",
                coverage_type="full_file",
                observed_state="content_observed",
            )
            safe_content = content.replace(
                "</LOW_SAME_FILE_CONTEXT_UNTRUSTED>",
                "</LOW_SAME_FILE_CONTEXT_UNTRUSTED_ESCAPED>",
            )
            parts.append(
                f"### {path}\n"
                "<LOW_SAME_FILE_CONTEXT_UNTRUSTED>\n"
                f"{safe_content}\n"
                "</LOW_SAME_FILE_CONTEXT_UNTRUSTED>"
            )
            continue

        normalized_outcome = (
            outcome
            if outcome
            in {
                "excluded_by_policy",
                "not_found",
                "oversize",
                "binary_or_non_utf8",
                "directory",
                "error",
            }
            else "error"
        )
        outcomes[normalized_outcome] = (
            outcomes.get(normalized_outcome, 0) + 1
        )
        ledger.record_event(
            question_id=question_id,
            tool="read_file",
            args={"path": path, "mode": "content"},
            outcome=normalized_outcome,
            paths=[path],
            source_ref=f"pr_head:{head_sha}",
            head_reread_outcome=normalized_outcome,
            error_kind=str(
                result.get("error_kind")
                or result.get("error_type")
                or normalized_outcome
            ),
            observed_state="content_unobserved",
        )

    context = (
        "## Low-route exact-head changed-file context\n\n"
        + "\n\n".join(parts)
        if parts
        else ""
    )
    return context, {
        "review_mode": "low",
        "context_strategy": "low_same_file",
        "finish_reason": (
            "low_same_file_collected" if collected else "low_no_context"
        ),
        "pfr_rounds": 0,
        "pfr_plan_source": "not_required_low_route",
        "repo_inventory": (
            repo_inventory.to_meta()
            if isinstance(repo_inventory, RepoInventory)
            else {"status": "unavailable"}
        ),
        "evidence_ledger": ledger.to_meta(),
        "low_same_file_context_attempted_count": backend_attempted,
        "low_same_file_context_file_count": collected,
        "low_same_file_context_bytes": total_bytes,
        "low_same_file_context_outcomes": dict(sorted(outcomes.items())),
    }

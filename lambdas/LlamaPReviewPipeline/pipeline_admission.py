"""Exact-head and owner-bound admission for Pipeline phase work.

This module owns only the deterministic boundary that decides whether one
stream delivery may begin a Context or Review attempt.  It does not choose or
run phase work, retry failures, or publish results.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import config, persistence
from .deadline import Deadline
from .errors import (
    HeadSuperseded,
    HeadVerificationUnavailable,
    PhaseClaimUnavailable,
    PRLifecycleSuperseded,
)
from .github_app_auth import get_installation_token
from .pr_ingest import extract_pr_head_sha

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PhaseAdmission:
    """The current item and exact owner-bound claim admitted for one phase."""

    current_item: Dict[str, Any]
    phase_claim: Dict[str, Any]
    attempt: int


def require_item_field(item: Mapping[str, Any], key: str) -> Any:
    value = item.get(key)
    if value in (None, ""):
        raise ValueError(f"Missing required pipeline item field: {key}")
    return value


def effective_dry_run(item: Mapping[str, Any]) -> bool:
    return bool(config.DRY_RUN or item.get("dry_run") is True)


def run_id(item: Mapping[str, Any], repo: str, pr_number: int) -> str:
    return str(
        item.get("run_id")
        or item.get("delivery_id")
        or f"{repo.replace('/', '_')}_{pr_number}"
    )


def installation_token(
    installation_id: int,
    *,
    deadline: Optional[Deadline] = None,
) -> str:
    """Acquire the GitHub credential required for exact-head admission."""

    if deadline is not None:
        deadline.check(
            "github.installation_token.first_attempt",
            minimum_seconds=0.1,
        )
    first_timeout = (
        deadline.timeout_for(
            15,
            stage="github.installation_token.first_attempt",
        )
        if deadline is not None
        else 15
    )
    token = get_installation_token(
        int(installation_id),
        timeout_seconds=first_timeout,
    )
    if token:
        return token
    if deadline is not None:
        wait = min(
            2.0,
            deadline.check(
                "github.installation_token.backoff",
                minimum_seconds=0.1,
            ),
        )
        if wait > 0:
            time.sleep(wait)
        deadline.check(
            "github.installation_token.second_attempt",
            minimum_seconds=0.1,
        )
    else:
        time.sleep(2)
    second_timeout = (
        deadline.timeout_for(
            15,
            stage="github.installation_token.second_attempt",
        )
        if deadline is not None
        else 15
    )
    token = get_installation_token(
        int(installation_id),
        timeout_seconds=second_timeout,
    )
    if not token:
        raise RuntimeError("Failed to get GitHub installation token")
    return token


def claim_phase_delivery(
    repo: str,
    pr_number: int,
    *,
    phase: str,
    expected_status: str,
    runtime_identity: Dict[str, Any],
    stream_event_id: str,
    table=None,
) -> Optional[PhaseAdmission]:
    """Consistently reread and claim one current stream-delivered phase.

    A stale status image is an idempotent no-op.  An eligible item with a
    foreign active owner fails retryably instead of silently executing or
    selecting the wrong attempt.
    """

    if phase not in {"context", "review"}:
        raise ValueError(f"Unsupported pipeline phase: {phase}")
    delivery = persistence.claim_current_phase_delivery(
        repo,
        pr_number,
        phase,
        expected_status=expected_status,
        runtime_identity=runtime_identity,
        stream_event_id=stream_event_id,
        table=table,
    )
    if not delivery["eligible"]:
        current = delivery.get("current_item") or {}
        logger.info(
            "%s phase idempotency skip: %s#%s status=%s",
            phase.capitalize(),
            repo,
            pr_number,
            current.get("status") or "missing",
        )
        return None
    phase_claim = delivery.get("phase_claim")
    if phase_claim is None or not delivery["claim_valid"]:
        raise PhaseClaimUnavailable(
            f"{phase.capitalize()} phase has an active foreign stream-record owner.",
            stage=f"{phase}.claim",
        )
    claim = dict(phase_claim)
    return PhaseAdmission(
        current_item=dict(delivery["current_item"]),
        phase_claim=claim,
        attempt=int(claim["attempt"]),
    )


def current_pr_snapshot(
    runtime: Any,
    repo: str,
    pr_number: int,
    *,
    pr_content: Optional[Dict[str, Any]] = None,
    stage: str,
) -> Dict[str, Any]:
    snapshot_getter = getattr(runtime, "get_pr_head_snapshot", None)
    if callable(snapshot_getter):
        snapshot = snapshot_getter(repo, pr_number)
        if not isinstance(snapshot, dict):
            raise HeadVerificationUnavailable(
                "GitHub returned an invalid PR head/lifecycle snapshot",
                stage=stage,
            )
        head_sha = str(snapshot.get("head_sha") or "")
        state = str(snapshot.get("state") or "").strip().lower()
        merged = snapshot.get("merged")
        if not head_sha or state not in {"open", "closed"} or type(merged) is not bool:
            raise HeadVerificationUnavailable(
                "GitHub did not return a complete PR head/lifecycle snapshot",
                stage=stage,
            )
        return {"head_sha": head_sha, "state": state, "merged": merged}

    head_sha = extract_pr_head_sha(pr_content or {})
    if not head_sha:
        getter = getattr(runtime, "get_pr_head_sha", None)
        if callable(getter):
            head_sha = str(getter(repo, pr_number) or "")
    if not head_sha:
        raise HeadVerificationUnavailable(
            "GitHub did not return a current PR head SHA",
            stage=stage,
        )
    # Compatibility for unit/runtime adapters that predate lifecycle snapshots.
    # Production GitHubRuntime always uses the fresh typed branch above.
    return {"head_sha": head_sha, "state": "", "merged": False}


def assert_current_head(
    runtime: Any,
    repo: str,
    pr_number: int,
    expected_head_sha: str,
    *,
    pr_content: Optional[Dict[str, Any]] = None,
    stage: str,
) -> str:
    """Require the queued exact head to remain open and unmerged."""

    snapshot = current_pr_snapshot(
        runtime,
        repo,
        pr_number,
        pr_content=pr_content,
        stage=stage,
    )
    actual = str(snapshot["head_sha"])
    if actual != expected_head_sha:
        raise HeadSuperseded(expected_head_sha, actual, stage=stage)
    state = str(snapshot.get("state") or "")
    merged = bool(snapshot.get("merged"))
    if state and (state != "open" or merged):
        raise PRLifecycleSuperseded(
            expected_head_sha,
            actual,
            current_state=state,
            merged=merged,
            stage=stage,
        )
    return actual


def hard_input_skip_reason(file_changes: Any) -> str:
    """Return a terminal reason only when the compare proves an empty diff."""

    if not isinstance(file_changes, list):
        return ""
    if not file_changes:
        return "Empty PR - no files to review"
    return ""

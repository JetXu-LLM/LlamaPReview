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
from enum import Enum
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


class PRDispositionKind(str, Enum):
    """Exact current PR lifecycle relative to one expected review head."""

    OPEN_SAME_HEAD = "open_same_head"
    OPEN_NEW_HEAD = "open_new_head"
    MERGED_SAME_HEAD = "merged_same_head"
    MERGED_NEW_HEAD = "merged_new_head"
    CLOSED_SAME_HEAD = "closed_same_head"
    CLOSED_NEW_HEAD = "closed_new_head"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class PRLifecycleDisposition:
    """Typed, content-safe lifecycle result owned by exact-head admission."""

    kind: PRDispositionKind
    expected_head_sha: str
    actual_head_sha: str
    current_state: str
    merged: bool
    stage: str
    locked: Optional[bool] = None

    @property
    def same_head(self) -> bool:
        return bool(
            self.actual_head_sha
            and self.actual_head_sha == self.expected_head_sha
        )

    @property
    def ended(self) -> bool:
        return self.kind in {
            PRDispositionKind.MERGED_SAME_HEAD,
            PRDispositionKind.MERGED_NEW_HEAD,
            PRDispositionKind.CLOSED_SAME_HEAD,
            PRDispositionKind.CLOSED_NEW_HEAD,
        }


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
    stream_head_sha: str = "",
    stream_run_id: str = "",
    table=None,
) -> Optional[PhaseAdmission]:
    """Consistently reread and claim one current stream-delivered phase.

    A stale status image is an idempotent no-op.  An eligible item with a
    foreign active owner fails retryably instead of silently executing or
    selecting the wrong attempt.
    """

    if phase not in {"context", "review"}:
        raise ValueError(f"Unsupported pipeline phase: {phase}")
    identity_fence: Dict[str, str] = {}
    if stream_head_sha:
        identity_fence["expected_head_sha"] = str(stream_head_sha)
    if stream_run_id:
        identity_fence["expected_run_id"] = str(stream_run_id)
    delivery = persistence.claim_current_phase_delivery(
        repo,
        pr_number,
        phase,
        expected_status=expected_status,
        runtime_identity=runtime_identity,
        stream_event_id=stream_event_id,
        **identity_fence,
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
        locked = snapshot.get("locked")
        if locked is not None and type(locked) is not bool:
            raise HeadVerificationUnavailable(
                "GitHub returned an invalid PR conversation-lock state",
                stage=stage,
            )
        return {
            "head_sha": head_sha,
            "state": state,
            "merged": merged,
            "locked": locked,
        }

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
    return {
        "head_sha": head_sha,
        "state": "",
        "merged": False,
        "locked": None,
    }


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

    disposition = current_pr_disposition(
        runtime,
        repo,
        pr_number,
        expected_head_sha,
        pr_content=pr_content,
        stage=stage,
    )
    if disposition.kind is PRDispositionKind.UNVERIFIED:
        if disposition.actual_head_sha and not disposition.current_state:
            if disposition.actual_head_sha != expected_head_sha:
                raise HeadSuperseded(
                    expected_head_sha,
                    disposition.actual_head_sha,
                    stage=stage,
                )
            return disposition.actual_head_sha
        raise HeadVerificationUnavailable(
            "GitHub did not return a complete PR head/lifecycle snapshot",
            stage=stage,
        )
    # Lifecycle is intentionally classified before an ended PR's head change.
    # This prevents a merged/closed item from entering the open-head successor
    # path merely because its final head differs from the queued revision.
    if disposition.ended:
        raise PRLifecycleSuperseded(
            expected_head_sha,
            disposition.actual_head_sha,
            current_state=disposition.current_state,
            merged=disposition.merged,
            stage=stage,
        )
    if disposition.kind is PRDispositionKind.OPEN_NEW_HEAD:
        raise HeadSuperseded(
            expected_head_sha,
            disposition.actual_head_sha,
            stage=stage,
        )
    return disposition.actual_head_sha


def current_pr_disposition(
    runtime: Any,
    repo: str,
    pr_number: int,
    expected_head_sha: str,
    *,
    pr_content: Optional[Dict[str, Any]] = None,
    stage: str,
) -> PRLifecycleDisposition:
    """Classify lifecycle and exact-head identity from one current snapshot.

    Production adapters return the lifecycle fields plus structural lock
    state. Compatibility
    adapters that expose only a head SHA remain usable by the legacy
    ``assert_current_head`` boundary when that SHA is present, while the typed
    lifecycle result is explicitly unverified rather than guessing open.
    """

    try:
        snapshot = current_pr_snapshot(
            runtime,
            repo,
            pr_number,
            pr_content=pr_content,
            stage=stage,
        )
    except HeadVerificationUnavailable:
        return PRLifecycleDisposition(
            kind=PRDispositionKind.UNVERIFIED,
            expected_head_sha=str(expected_head_sha),
            actual_head_sha="",
            current_state="",
            merged=False,
            stage=str(stage),
        )
    actual = str(snapshot["head_sha"])
    state = str(snapshot.get("state") or "").strip().lower()
    merged = bool(snapshot.get("merged"))
    locked = snapshot.get("locked")
    same_head = bool(actual and actual == str(expected_head_sha))
    if not actual:
        kind = PRDispositionKind.UNVERIFIED
    elif not state:
        # Legacy exact-head-only runtime adapter. assert_current_head preserves
        # its historic behavior; lifecycle-aware callers see unverified.
        kind = PRDispositionKind.UNVERIFIED
    elif (merged or state == "closed") and type(locked) is not bool:
        # An ended pull request may reject the sole native review surface when
        # its conversation is locked. Do not guess publication availability.
        kind = PRDispositionKind.UNVERIFIED
    elif merged:
        kind = (
            PRDispositionKind.MERGED_SAME_HEAD
            if same_head
            else PRDispositionKind.MERGED_NEW_HEAD
        )
    elif state == "closed":
        kind = (
            PRDispositionKind.CLOSED_SAME_HEAD
            if same_head
            else PRDispositionKind.CLOSED_NEW_HEAD
        )
    elif state == "open":
        kind = (
            PRDispositionKind.OPEN_SAME_HEAD
            if same_head
            else PRDispositionKind.OPEN_NEW_HEAD
        )
    else:
        kind = PRDispositionKind.UNVERIFIED
    return PRLifecycleDisposition(
        kind=kind,
        expected_head_sha=str(expected_head_sha),
        actual_head_sha=actual,
        current_state=state,
        merged=merged,
        stage=str(stage),
        locked=locked if type(locked) is bool else None,
    )


def hard_input_skip_reason(file_changes: Any) -> str:
    """Return a terminal reason only when the compare proves an empty diff."""

    if not isinstance(file_changes, list):
        return ""
    if not file_changes:
        return "Empty PR - no files to review"
    return ""

"""Atomic, idempotent free-review admission for the hosted service."""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import config
from .persistence import _is_conditional_failure, get_table, iso_now, ttl_epoch


CAPACITY_PR_NUMBER = -1
CAPACITY_SENTINEL_PREFIX = "!llamapreview-capacity:"

DEFAULT_REPO_DAILY = 3
DEFAULT_GLOBAL_DAILY = 100
MAX_GLOBAL_DAILY = 512

BLOCK_REPO_DAILY = "repo_daily_capacity"
BLOCK_GLOBAL_DAILY = "global_daily_capacity"


@dataclass(frozen=True)
class CapacityPolicy:
    """Operator-tunable bounds; the defaults are the hosted contract."""

    repo_daily: int = DEFAULT_REPO_DAILY
    global_daily: int = DEFAULT_GLOBAL_DAILY
    successor_enabled: bool = True

    def __post_init__(self) -> None:
        if self.repo_daily < 0 or self.global_daily < 0:
            raise ValueError("capacity bounds must be non-negative")
        if self.repo_daily > MAX_GLOBAL_DAILY:
            raise ValueError(
                f"repo_daily cannot exceed {MAX_GLOBAL_DAILY}"
            )
        if self.global_daily > MAX_GLOBAL_DAILY:
            raise ValueError(
                f"global_daily cannot exceed {MAX_GLOBAL_DAILY}"
            )
        if self.repo_daily > 0 and self.global_daily == 0:
            raise ValueError(
                "repo_daily requires a bounded global_daily capacity"
            )

    @property
    def enabled(self) -> bool:
        return self.repo_daily > 0 or self.global_daily > 0


@dataclass(frozen=True)
class CapacityDecision:
    """Outcome of one exact review-run admission."""

    allowed: bool
    block_reason: str = ""
    should_notify: bool = False
    used: int = 0
    limit: int = 0
    window: str = ""
    resets_at: str = ""
    admission_id: str = ""

    def telemetry(self) -> Dict[str, Any]:
        return {
            "capacity_allowed": self.allowed,
            "capacity_block_reason": self.block_reason,
            "capacity_used": self.used,
            "capacity_limit": self.limit,
            "capacity_window": self.window,
            "capacity_notified": self.should_notify,
            "capacity_admission_id": self.admission_id,
        }


def _parse_int(raw: str, *, key: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def parse_policy(raw: str = "") -> CapacityPolicy:
    """Read the single compact policy string."""

    policy = CapacityPolicy()
    text = (raw if raw is not None else "").strip()
    if not text:
        return policy
    if text.lower() == "off":
        return CapacityPolicy(repo_daily=0, global_daily=0, successor_enabled=True)

    repo_daily = policy.repo_daily
    global_daily = policy.global_daily
    successor_enabled = policy.successor_enabled
    seen: set[str] = set()
    for chunk in text.split(";"):
        key, separator, value = chunk.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not separator or not key or key in seen:
            raise ValueError("capacity policy keys must be unique key=value pairs")
        seen.add(key)
        if key == "repo_daily":
            repo_daily = _parse_int(value, key=key)
        elif key == "global_daily":
            global_daily = _parse_int(value, key=key)
        elif key == "successor":
            normalized = value.lower()
            if normalized in {"on", "true", "1", "yes"}:
                successor_enabled = True
            elif normalized in {"off", "false", "0", "no"}:
                successor_enabled = False
            else:
                raise ValueError("successor must be on or off")
        else:
            raise ValueError(f"unknown capacity policy key: {key}")
    return CapacityPolicy(
        repo_daily=repo_daily,
        global_daily=global_daily,
        successor_enabled=successor_enabled,
    )


def active_policy() -> CapacityPolicy:
    return parse_policy(config.PIPELINE_CAPACITY_POLICY)


def successor_enabled() -> bool:
    """Return the active operator decision for one-time head succession."""

    return active_policy().successor_enabled


def _window_for(now: datetime.datetime) -> str:
    return now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d")


def _next_window_start(now: datetime.datetime) -> str:
    utc = now.astimezone(datetime.timezone.utc)
    tomorrow = (utc + datetime.timedelta(days=1)).date()
    return datetime.datetime.combine(
        tomorrow, datetime.time.min, tzinfo=datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def capacity_admission_id(
    repo: str,
    pr_number: int,
    run_id: str,
    head_sha: str,
    *,
    is_successor: bool,
) -> str:
    """Bind one capacity charge to the exact durable review-run identity."""

    payload = {
        "head_sha": str(head_sha),
        "is_successor": bool(is_successor),
        "pr_number": int(pr_number),
        "repo": str(repo),
        "run_id": str(run_id),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sentinel_key(window: str) -> Dict[str, Any]:
    return {
        "repo": f"{CAPACITY_SENTINEL_PREFIX}{window}",
        "pr_number": CAPACITY_PR_NUMBER,
    }


def _repo_counter_attribute(repo: str) -> str:
    digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()
    return f"capacity_repo_{digest}"


def _notice_owner_attribute(repo: str) -> str:
    digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()
    return f"capacity_notice_owner_{digest}"


def _read_sentinel(window: str, *, table) -> Dict[str, Any]:
    response = table.get_item(
        Key=_sentinel_key(window),
        ConsistentRead=True,
    )
    item = response.get("Item") or {}
    return dict(item) if isinstance(item, dict) else {}


def _admission_ids(item: Dict[str, Any]) -> set[str]:
    value = item.get("capacity_admission_ids") or set()
    if isinstance(value, set):
        return {str(entry) for entry in value}
    if isinstance(value, (list, tuple)):
        return {str(entry) for entry in value}
    return set()


def _claim_notice(
    repo: str,
    window: str,
    admission_id: str,
    *,
    table,
) -> bool:
    """Bind the day's one repository notice to its exact blocked admission."""

    try:
        table.update_item(
            Key=_sentinel_key(window),
            UpdateExpression=(
                "SET #notice_owner = :admission_id, #updated_at = :now, #ttl = :ttl"
            ),
            ConditionExpression=(
                "attribute_not_exists(#notice_owner) "
                "OR #notice_owner = :admission_id"
            ),
            ExpressionAttributeNames={
                "#notice_owner": _notice_owner_attribute(repo),
                "#updated_at": "updated_at",
                "#ttl": "ttl_epoch",
            },
            ExpressionAttributeValues={
                ":admission_id": admission_id,
                ":now": iso_now(),
                ":ttl": ttl_epoch(config.TTL_DAYS),
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 - re-raised unless the owner lost CAS
        if not _is_conditional_failure(exc):
            raise
        return False


def _admit_once(
    repo: str,
    window: str,
    admission_id: str,
    *,
    policy: CapacityPolicy,
    table,
) -> tuple[bool, Dict[str, Any]]:
    """Atomically charge both active bounds and remember the admission."""

    names = {
        "#admission_ids": "capacity_admission_ids",
        "#updated_at": "updated_at",
        "#ttl": "ttl_epoch",
        "#window": "capacity_window",
    }
    values: Dict[str, Any] = {
        ":admission_id": admission_id,
        ":admission_ids": {admission_id},
        ":now": iso_now(),
        ":one": 1,
        ":ttl": ttl_epoch(config.TTL_DAYS),
        ":window": window,
    }
    add_parts = ["#admission_ids :admission_ids"]
    conditions = [
        "(attribute_not_exists(#admission_ids) "
        "OR NOT contains(#admission_ids, :admission_id))"
    ]
    if policy.repo_daily > 0:
        names["#repo_count"] = _repo_counter_attribute(repo)
        values[":repo_limit"] = int(policy.repo_daily)
        add_parts.append("#repo_count :one")
        conditions.append(
            "(attribute_not_exists(#repo_count) OR #repo_count < :repo_limit)"
        )
    if policy.global_daily > 0:
        names["#global_count"] = "capacity_global_count"
        values[":global_limit"] = int(policy.global_daily)
        add_parts.append("#global_count :one")
        conditions.append(
            "(attribute_not_exists(#global_count) OR #global_count < :global_limit)"
        )

    try:
        response = table.update_item(
            Key=_sentinel_key(window),
            UpdateExpression=(
                f"ADD {', '.join(add_parts)} "
                "SET #window = :window, #updated_at = :now, #ttl = :ttl"
            ),
            ConditionExpression=" AND ".join(conditions),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="UPDATED_NEW",
        )
        return True, dict(response.get("Attributes") or {})
    except Exception as exc:  # noqa: BLE001 - re-raised unless admission lost CAS
        if not _is_conditional_failure(exc):
            raise
        return False, _read_sentinel(window, table=table)


def consume(
    repo: str,
    pr_number: int,
    run_id: str,
    head_sha: str,
    *,
    table=None,
    policy: Optional[CapacityPolicy] = None,
    now: Optional[datetime.datetime] = None,
    is_successor: bool = False,
) -> CapacityDecision:
    """Admit one exact run once, before any paid model call."""

    policy = policy or active_policy()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    window = _window_for(now)
    admission_id = capacity_admission_id(
        repo,
        pr_number,
        run_id,
        head_sha,
        is_successor=is_successor,
    )
    if not policy.enabled:
        return CapacityDecision(
            allowed=True,
            window=window,
            admission_id=admission_id,
        )

    table = table or get_table()
    resets_at = _next_window_start(now)
    admitted, state = _admit_once(
        repo,
        window,
        admission_id,
        policy=policy,
        table=table,
    )
    repo_used = int(state.get(_repo_counter_attribute(repo)) or 0)
    global_used = int(state.get("capacity_global_count") or 0)
    already_admitted = admission_id in _admission_ids(state)
    if admitted or already_admitted:
        used = repo_used if policy.repo_daily > 0 else global_used
        limit = policy.repo_daily if policy.repo_daily > 0 else policy.global_daily
        return CapacityDecision(
            allowed=True,
            used=used,
            limit=limit,
            window=window,
            resets_at=resets_at,
            admission_id=admission_id,
        )

    if policy.repo_daily > 0 and repo_used >= policy.repo_daily:
        should_notify = (not is_successor) and _claim_notice(
            repo,
            window,
            admission_id,
            table=table,
        )
        return CapacityDecision(
            allowed=False,
            block_reason=BLOCK_REPO_DAILY,
            should_notify=should_notify,
            used=repo_used,
            limit=policy.repo_daily,
            window=window,
            resets_at=resets_at,
            admission_id=admission_id,
        )

    return CapacityDecision(
        allowed=False,
        block_reason=BLOCK_GLOBAL_DAILY,
        should_notify=False,
        used=global_used,
        limit=policy.global_daily,
        window=window,
        resets_at=resets_at,
        admission_id=admission_id,
    )


def capacity_notice_reason(decision: CapacityDecision) -> str:
    """Public wording for a repository that used its free daily capacity."""

    return (
        f"This repository has used its free review capacity for today "
        f"({decision.limit} reviews per UTC day). Capacity resets at "
        f"{decision.resets_at}.\n\n"
        "LlamaPReview is free for public repositories and funded personally, so a "
        "daily bound keeps one high-volume repository from consuming the shared "
        "budget. Nothing about this pull request was judged. You can also run "
        "LlamaPReview yourself with your own provider key — see "
        "[Self-hosting](https://github.com/JetXu-LLM/LlamaPReview/blob/main/docs/HOSTING.md)."
    )

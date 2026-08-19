"""Bounded free review capacity for the hosted public service.

The hosted App is funded personally, so a single high-velocity repository can
consume the whole shared budget: measured over thirty days, one repository was
22.5% of provider spend while the median repository asked for one review a day.
This module allocates that shared budget deterministically, before the first
paid model call, and never in the Webhook.

Two counters live on reserved sentinel items in the existing table. Real pull
request numbers start at 1 and ``pr_number = 0`` already stores the repository
fact sheet, so capacity uses ``pr_number = -1``. The global row additionally
uses a repository key GitHub cannot issue, because owner names are restricted to
alphanumerics and hyphens.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import config
from .persistence import (
    _is_conditional_failure,
    get_table,
    iso_now,
    ttl_epoch,
)

CAPACITY_PR_NUMBER = -1
GLOBAL_CAPACITY_REPO = "!llamapreview-global-capacity"

DEFAULT_REPO_DAILY = 3
DEFAULT_GLOBAL_DAILY = 100

BLOCK_REPO_DAILY = "repo_daily_capacity"
BLOCK_GLOBAL_DAILY = "global_daily_capacity"


@dataclass(frozen=True)
class CapacityPolicy:
    """Operator-tunable bounds; the defaults are the reviewed contract."""

    repo_daily: int = DEFAULT_REPO_DAILY
    global_daily: int = DEFAULT_GLOBAL_DAILY
    successor_enabled: bool = True

    @property
    def enabled(self) -> bool:
        return self.repo_daily > 0 or self.global_daily > 0


@dataclass(frozen=True)
class CapacityDecision:
    """Outcome of one admission attempt against the shared budget."""

    allowed: bool
    block_reason: str = ""
    should_notify: bool = False
    used: int = 0
    limit: int = 0
    window: str = ""
    resets_at: str = ""

    def telemetry(self) -> Dict[str, Any]:
        return {
            "capacity_allowed": self.allowed,
            "capacity_block_reason": self.block_reason,
            "capacity_used": self.used,
            "capacity_limit": self.limit,
            "capacity_window": self.window,
            "capacity_notified": self.should_notify,
        }


def _parse_int(raw: str, fallback: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value >= 0 else fallback


def parse_policy(raw: str = "") -> CapacityPolicy:
    """Read the single compact policy string.

    Lambda's 4KB environment budget is nearly consumed, and this file already
    keeps safety caps as code-owned constants rather than one variable each.
    """

    policy = CapacityPolicy()
    text = (raw if raw is not None else "").strip()
    if not text:
        return policy
    if text.lower() in {"off", "disabled", "none"}:
        return CapacityPolicy(repo_daily=0, global_daily=0, successor_enabled=True)

    repo_daily = policy.repo_daily
    global_daily = policy.global_daily
    successor_enabled = policy.successor_enabled
    for chunk in text.split(";"):
        key, _, value = chunk.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "repo_daily":
            repo_daily = _parse_int(value, policy.repo_daily)
        elif key == "global_daily":
            global_daily = _parse_int(value, policy.global_daily)
        elif key == "successor":
            successor_enabled = value.lower() not in {"off", "false", "0", "no"}
    return CapacityPolicy(
        repo_daily=repo_daily,
        global_daily=global_daily,
        successor_enabled=successor_enabled,
    )


def active_policy() -> CapacityPolicy:
    return parse_policy(config.PIPELINE_CAPACITY_POLICY)


def _window_for(now: datetime.datetime) -> str:
    return now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d")


def _next_window_start(now: datetime.datetime) -> str:
    utc = now.astimezone(datetime.timezone.utc)
    tomorrow = (utc + datetime.timedelta(days=1)).date()
    return datetime.datetime.combine(
        tomorrow, datetime.time.min, tzinfo=datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _consume_counter(repo: str, window: str, *, table) -> int:
    """Return this run's position in the window, resetting on a window change.

    Two writers can race the reset; the loser falls through to the additive
    branch, so the returned positions stay unique.
    """

    key = {"repo": repo, "pr_number": CAPACITY_PR_NUMBER}
    values = {
        ":window": window,
        ":one": 1,
        ":now": iso_now(),
        ":ttl": ttl_epoch(config.TTL_DAYS),
    }
    try:
        table.update_item(
            Key=key,
            UpdateExpression=(
                "SET capacity_window = :window, capacity_count = :one, "
                "updated_at = :now, ttl_epoch = :ttl"
            ),
            ConditionExpression=(
                "attribute_not_exists(capacity_window) OR capacity_window <> :window"
            ),
            ExpressionAttributeValues=values,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - re-raised unless it is the race
        if not _is_conditional_failure(exc):
            raise
    response = table.update_item(
        Key=key,
        UpdateExpression="ADD capacity_count :one SET updated_at = :now, ttl_epoch = :ttl",
        ExpressionAttributeValues=values,
        ReturnValues="UPDATED_NEW",
    )
    return int(response.get("Attributes", {}).get("capacity_count", 1))


def _claim_notice(repo: str, window: str, *, table) -> bool:
    """Let exactly one blocked run per repository per window speak publicly."""

    try:
        table.update_item(
            Key={"repo": repo, "pr_number": CAPACITY_PR_NUMBER},
            UpdateExpression=(
                "SET capacity_notice_window = :window, updated_at = :now, ttl_epoch = :ttl"
            ),
            ConditionExpression=(
                "attribute_not_exists(capacity_notice_window) "
                "OR capacity_notice_window <> :window"
            ),
            ExpressionAttributeValues={
                ":window": window,
                ":now": iso_now(),
                ":ttl": ttl_epoch(config.TTL_DAYS),
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 - re-raised unless it is the race
        if not _is_conditional_failure(exc):
            raise
        return False


def consume(
    repo: str,
    *,
    table=None,
    policy: Optional[CapacityPolicy] = None,
    now: Optional[datetime.datetime] = None,
    is_successor: bool = False,
) -> CapacityDecision:
    """Charge one admitted run against the shared budget before any paid call."""

    policy = policy or active_policy()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    window = _window_for(now)
    if not policy.enabled:
        return CapacityDecision(allowed=True, window=window)

    table = table or get_table()
    resets_at = _next_window_start(now)

    if policy.repo_daily > 0:
        used = _consume_counter(repo, window, table=table)
        if used > policy.repo_daily:
            # A successor re-reviews a pull request that already had its say, so a
            # second public notice for it would only confuse the maintainer.
            should_notify = (not is_successor) and _claim_notice(
                repo, window, table=table
            )
            return CapacityDecision(
                allowed=False,
                block_reason=BLOCK_REPO_DAILY,
                should_notify=should_notify,
                used=used,
                limit=policy.repo_daily,
                window=window,
                resets_at=resets_at,
            )
    else:
        used = 0

    if policy.global_daily > 0:
        total = _consume_counter(GLOBAL_CAPACITY_REPO, window, table=table)
        if total > policy.global_daily:
            # The global bound is an operator circuit breaker, not a repository
            # policy, so it never speaks on a pull request.
            return CapacityDecision(
                allowed=False,
                block_reason=BLOCK_GLOBAL_DAILY,
                should_notify=False,
                used=total,
                limit=policy.global_daily,
                window=window,
                resets_at=resets_at,
            )

    return CapacityDecision(
        allowed=True,
        used=used,
        limit=policy.repo_daily,
        window=window,
        resets_at=resets_at,
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

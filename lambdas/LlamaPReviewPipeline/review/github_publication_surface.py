"""Exact GitHub review surface for crash-safe publication.

The module owns complete surface enumeration, exact-effect observation,
bounded reconciliation and the single private GitHub write primitive.  It
does not own DynamoDB transaction state or terminal receipts.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from ..deadline import Deadline, DeadlineExceeded
from ..errors import (
    HeadSuperseded,
    HeadVerificationUnavailable,
    PublicationIdentityUnavailable,
    PublicationIntegrityFailure,
    PublicationOutcomeUnknown,
    PublicationPreDispatchAbort,
    PublicationPreflightUnavailable,
    PublicationStateConflict,
)
from .publication_candidate import prepared_from_candidate
from .publish import GITHUB_REVIEW_COMMENT_FIELDS, PreparedGitHubReview


BOT_LOGIN = "llamapreview[bot]"
RECONCILIATION_CLOCK_SKEW_SECONDS = 60
RECONCILIATION_MAX_OBSERVATIONS = 4
RECONCILIATION_POLL_SECONDS = 0.5
# The deployed GitHub client uses a 30-second read timeout.  Every new SDK
# operation or pagination step must begin with enough usable phase time to
# finish without consuming the separate durable-state-write reserve.
GITHUB_SURFACE_OPERATION_BUDGET_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class GitHubPublicationEffect:
    """Exact externally observable identity of one GitHub review effect."""

    outcome: str
    review_id: int | str
    commit_id: str
    inline_comment_ids: tuple[int | str, ...]


def _value(resource: Any, field: str) -> Any:
    if isinstance(resource, Mapping):
        return resource.get(field)
    direct = getattr(resource, field, None)
    if direct is not None:
        return direct
    raw = getattr(resource, "raw_data", None)
    if isinstance(raw, Mapping):
        return raw.get(field)
    return None


def _identity(resource: Any, field: str) -> str:
    value = _value(resource, field)
    return str(value).strip() if value not in (None, "") else ""


def _valid_github_identity(value: Any) -> bool:
    """Accept only non-boolean, non-empty GitHub integer/string identities."""

    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    return isinstance(value, str) and bool(value.strip())


def _require_surface_budget(
    deadline: Optional[Deadline],
    *,
    stage: str,
) -> None:
    if deadline is not None:
        deadline.check(
            stage,
            minimum_seconds=GITHUB_SURFACE_OPERATION_BUDGET_SECONDS,
        )


def _native_pull(
    repo: Any,
    pr_number: int,
    *,
    preflight: bool,
    deadline: Optional[Deadline] = None,
    stage: str = "publication.surface.pull",
) -> Any:
    failure = (
        PublicationPreflightUnavailable
        if preflight
        else PublicationOutcomeUnknown
    )
    native_repo = getattr(repo, "repo", repo)
    getter = getattr(native_repo, "get_pull", None)
    if not callable(getter):
        raise failure(
            "GitHub repository cannot enumerate pull-request reviews.",
            stage=stage,
        )
    _require_surface_budget(deadline, stage=stage)
    try:
        return getter(int(pr_number))
    except DeadlineExceeded:
        raise
    except Exception as exc:
        raise failure(
            "GitHub pull request is temporarily unavailable.",
            stage=stage,
        ) from exc


def _complete_list(
    values: Iterable[Any],
    *,
    stage: str,
    preflight: bool,
    deadline: Optional[Deadline] = None,
) -> list[Any]:
    try:
        iterator = iter(values)
        completed: list[Any] = []
        while True:
            _require_surface_budget(deadline, stage=stage)
            try:
                completed.append(next(iterator))
            except StopIteration:
                return completed
    except DeadlineExceeded:
        raise
    except Exception as exc:
        failure = (
            PublicationPreflightUnavailable
            if preflight
            else PublicationOutcomeUnknown
        )
        raise failure(
            "GitHub publication surfaces could not be enumerated completely.",
            stage=stage,
        ) from exc


def _author_login(resource: Any) -> str:
    user = _value(resource, "user")
    return _identity(user, "login").casefold()


def _bot_reviews(
    pull: Any,
    *,
    stage: str,
    preflight: bool,
    deadline: Optional[Deadline] = None,
) -> list[Any]:
    getter = getattr(pull, "get_reviews", None)
    if not callable(getter):
        failure = (
            PublicationPreflightUnavailable
            if preflight
            else PublicationOutcomeUnknown
        )
        raise failure(
            "GitHub pull request cannot enumerate reviews.",
            stage=stage,
        )
    _require_surface_budget(deadline, stage=stage)
    try:
        values = getter()
    except DeadlineExceeded:
        raise
    except Exception as exc:
        failure = (
            PublicationPreflightUnavailable
            if preflight
            else PublicationOutcomeUnknown
        )
        raise failure(
            "GitHub pull-request reviews are temporarily unavailable.",
            stage=stage,
        ) from exc
    return [
        review
        for review in _complete_list(
            values,
            stage=stage,
            preflight=preflight,
            deadline=deadline,
        )
        if _author_login(review) == BOT_LOGIN
    ]


def assert_no_existing_bot_review(
    repo: Any,
    pr_number: int,
    *,
    deadline: Optional[Deadline] = None,
) -> str:
    """Prove a complete zero-bot-review precondition before a dispatch."""

    pull = _native_pull(
        repo,
        pr_number,
        preflight=True,
        deadline=deadline,
        stage="publication.preflight.pull",
    )
    if _bot_reviews(
        pull,
        stage="publication.preflight",
        preflight=True,
        deadline=deadline,
    ):
        raise PublicationIntegrityFailure(
            "A bot review already exists before publication intent.",
            stage="publication.preflight",
        )
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def _normalized_comment(resource: Any) -> Dict[str, Any]:
    return {
        field: _value(resource, field)
        for field in GITHUB_REVIEW_COMMENT_FIELDS
        if _value(resource, field) is not None
    }


def _all_review_comments(
    pull: Any,
    *,
    stage: str,
    deadline: Optional[Deadline] = None,
) -> list[Any]:
    getter = getattr(pull, "get_review_comments", None)
    if not callable(getter):
        raise PublicationOutcomeUnknown(
            "GitHub pull request cannot enumerate review comments.",
            stage=stage,
        )
    _require_surface_budget(deadline, stage=stage)
    try:
        values = getter()
    except DeadlineExceeded:
        raise
    except Exception as exc:
        raise PublicationOutcomeUnknown(
            "GitHub review comments are temporarily unavailable.",
            stage=stage,
        ) from exc
    return _complete_list(
        values,
        stage=stage,
        preflight=False,
        deadline=deadline,
    )


def _parse_timestamp(value: Any, *, stage: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        rendered = str(value or "").strip()
        if rendered.endswith("Z"):
            rendered = rendered[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(rendered)
        except (TypeError, ValueError) as exc:
            raise PublicationIntegrityFailure(
                "GitHub review submission time is missing or invalid.",
                stage=stage,
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_exact_review_commit(
    repo: Any,
    head_sha: str,
    *,
    deadline: Optional[Deadline] = None,
) -> tuple[Any, Any]:
    """Resolve and attest the immutable commit used by GitHub's review API."""

    expected = str(head_sha or "").strip()
    if not expected:
        raise HeadVerificationUnavailable(
            "Review publication requires a non-empty exact head SHA.",
            stage="review.publish.commit_resolution",
        )
    native_repo = getattr(repo, "repo", repo)
    get_commit = getattr(native_repo, "get_commit", None)
    get_pull = getattr(native_repo, "get_pull", None)
    if not callable(get_commit) or not callable(get_pull):
        raise HeadVerificationUnavailable(
            "GitHub repository cannot resolve an exact commit and pull request.",
            stage="review.publish.commit_resolution",
        )
    _require_surface_budget(
        deadline,
        stage="review.publish.commit_resolution",
    )
    try:
        commit = get_commit(sha=expected)
    except DeadlineExceeded:
        raise
    except Exception as exc:
        raise HeadVerificationUnavailable(
            "GitHub exact-commit resolution is temporarily unavailable.",
            stage="review.publish.commit_resolution",
        ) from exc
    resolved = _identity(commit, "sha")
    if not resolved:
        raise HeadVerificationUnavailable(
            "GitHub exact-commit resolution returned no commit identity.",
            stage="review.publish.commit_resolution",
        )
    if resolved.casefold() != expected.casefold():
        raise HeadSuperseded(
            expected,
            resolved,
            stage="review.publish.commit_resolution",
        )
    return get_pull, commit


def _require_fresh_dispatch_disposition(
    pull: Any,
    prepared: PreparedGitHubReview,
) -> None:
    """Reject a confirmed lifecycle mismatch before GitHub POST begins."""

    expected_head = str(prepared.head_sha or "").strip()
    head = _value(pull, "head")
    actual_head = _identity(head, "sha") or _identity(pull, "head_sha")
    state = _identity(pull, "state").casefold()
    merged_raw = _value(pull, "merged")
    locked_raw = _value(pull, "locked")
    merged = merged_raw if isinstance(merged_raw, bool) else None
    locked = locked_raw if isinstance(locked_raw, bool) else None
    if not actual_head or not state:
        raise HeadVerificationUnavailable(
            "GitHub pull request omitted fresh head or lifecycle state.",
            stage="review.publish.pre_dispatch_disposition",
        )

    abort_reason = ""
    if actual_head.casefold() != expected_head.casefold():
        abort_reason = "head_changed"
    elif prepared.required_disposition == "open_same_head":
        if state != "open":
            abort_reason = "pr_merged" if merged is True else "pr_closed"
    elif prepared.required_disposition == "merged_same_head":
        if state != "closed" or merged is not True:
            abort_reason = "merged_disposition_changed"
    elif prepared.required_disposition == "closed_same_head":
        if state != "closed" or merged is not False:
            abort_reason = "closed_disposition_changed"
    if (
        not abort_reason
        and prepared.artifact.get("review_mode") == "failed"
    ):
        if locked is None:
            raise HeadVerificationUnavailable(
                "GitHub pull request omitted the fresh lock state required "
                "for a failed-review notice.",
                stage="review.publish.pre_dispatch_disposition",
            )
        if locked:
            abort_reason = "publication_unavailable_locked"
    if abort_reason:
        raise PublicationPreDispatchAbort(
            expected_head,
            actual_head,
            current_state=state,
            merged=bool(merged),
            locked=locked,
            abort_reason=abort_reason,
            stage="review.publish.pre_dispatch_disposition",
        )


def _dispatch_exact_review(
    repo: Any,
    pr_number: int,
    prepared: PreparedGitHubReview,
    *,
    deadline: Optional[Deadline] = None,
) -> None:
    """Issue the sole write; the complete GitHub surface remains authoritative."""

    get_pull, commit = _resolve_exact_review_commit(
        repo,
        prepared.head_sha,
        deadline=deadline,
    )
    _require_surface_budget(
        deadline,
        stage="review.publish.create_review",
    )
    try:
        pull = get_pull(int(pr_number))
    except DeadlineExceeded:
        raise
    except Exception as exc:
        raise HeadVerificationUnavailable(
            "GitHub pull request is temporarily unavailable immediately "
            "before review dispatch.",
            stage="review.publish.pre_dispatch_disposition",
        ) from exc
    _require_fresh_dispatch_disposition(pull, prepared)
    _require_surface_budget(
        deadline,
        stage="review.publish.create_review",
    )
    review = pull.create_review(
        body=prepared.main_body,
        event="COMMENT",
        comments=[dict(comment) for comment in prepared.comments],
        commit=commit,
    )
    published_commit = _identity(review, "commit_id")
    if not published_commit:
        raise PublicationIdentityUnavailable(
            "GitHub created a review without returning its commit identity.",
            stage="review.publish.create_review",
        )
    if published_commit.casefold() != prepared.head_sha.casefold():
        raise PublicationIntegrityFailure(
            "GitHub returned a review commit that differs from the dispatched exact head.",
            stage="review.publish.create_review",
        )
    review_id = _value(review, "id")
    if not _valid_github_identity(review_id):
        raise PublicationIdentityUnavailable(
            "GitHub created a review without returning its exact identity.",
            stage="review.publish.create_review",
        )


def _observe_dispatching(
    repo: Any,
    pr_number: int,
    *,
    intent: Mapping[str, Any],
    candidate: Mapping[str, Any],
    deadline: Optional[Deadline] = None,
) -> GitHubPublicationEffect:
    if str(intent.get("state") or "") != "dispatching":
        raise PublicationStateConflict(
            "Only a dispatching intent may reconcile GitHub.",
            stage="publication.reconcile",
        )
    prepared = prepared_from_candidate(candidate)
    pull = _native_pull(
        repo,
        pr_number,
        preflight=False,
        deadline=deadline,
        stage="publication.reconcile.pull",
    )
    reviews = _bot_reviews(
        pull,
        stage="publication.reconcile.reviews",
        preflight=False,
        deadline=deadline,
    )
    if not reviews:
        raise PublicationOutcomeUnknown(
            "No exact GitHub review is observable for a dispatching intent.",
            stage="publication.reconcile",
        )
    preflight = _parse_timestamp(
        intent.get("preflight_completed_at"),
        stage="publication.reconcile",
    )
    earliest = preflight - timedelta(
        seconds=RECONCILIATION_CLOCK_SKEW_SECONDS
    )
    latest = datetime.now(timezone.utc) + timedelta(
        seconds=RECONCILIATION_CLOCK_SKEW_SECONDS
    )
    exact: list[Any] = []
    for review in reviews:
        submitted = _parse_timestamp(
            _value(review, "submitted_at"),
            stage="publication.reconcile",
        )
        if (
            _identity(review, "commit_id") == prepared.head_sha
            and str(_value(review, "body") or "")
            == prepared.main_body
            and _identity(review, "state").upper() == "COMMENTED"
            and submitted >= earliest
            and submitted <= latest
        ):
            exact.append(review)
    if len(exact) != 1 or len(reviews) != 1:
        raise PublicationIntegrityFailure(
            "GitHub bot reviews do not map uniquely to the dispatching intent.",
            stage="publication.reconcile",
        )
    review = exact[0]
    review_id = _value(review, "id")
    if not _valid_github_identity(review_id):
        raise PublicationIntegrityFailure(
            "Reconciled GitHub review has no identity.",
            stage="publication.reconcile",
        )
    all_comments = _all_review_comments(
        pull,
        stage="publication.reconcile.comments",
        deadline=deadline,
    )
    comments = [
        comment
        for comment in all_comments
        if str(_value(comment, "pull_request_review_id") or "")
        == str(review_id)
    ]
    actual_comments = [
        _normalized_comment(comment) for comment in comments
    ]
    expected_comments = [dict(comment) for comment in prepared.comments]
    if expected_comments and any(
        _author_login(comment) == BOT_LOGIN
        and _normalized_comment(comment) in expected_comments
        and str(_value(comment, "pull_request_review_id") or "")
        != str(review_id)
        for comment in all_comments
    ):
        raise PublicationIntegrityFailure(
            "A canonical GitHub review comment is parented to another review.",
            stage="publication.reconcile.comments",
        )
    if (
        len(actual_comments) < len(expected_comments)
        and actual_comments == expected_comments[: len(actual_comments)]
    ):
        raise PublicationOutcomeUnknown(
            "GitHub review comments are not yet completely observable.",
            stage="publication.reconcile.comments",
        )
    if (
        actual_comments != expected_comments
        or any(_author_login(comment) != BOT_LOGIN for comment in comments)
    ):
        raise PublicationIntegrityFailure(
            "GitHub review comments differ from the dispatching intent.",
            stage="publication.reconcile",
        )
    inline_ids = tuple(_value(comment, "id") for comment in comments)
    if any(
        not _valid_github_identity(value) for value in inline_ids
    ):
        raise PublicationIntegrityFailure(
            "Reconciled GitHub review comment identity is unavailable.",
            stage="publication.reconcile",
        )
    return GitHubPublicationEffect(
        outcome="adopted",
        review_id=review_id,
        commit_id=prepared.head_sha,
        inline_comment_ids=inline_ids,
    )


def reconcile_dispatching(
    repo: Any,
    pr_number: int,
    *,
    intent: Mapping[str, Any],
    candidate: Mapping[str, Any],
    deadline: Optional[Deadline] = None,
    max_observations: int = RECONCILIATION_MAX_OBSERVATIONS,
    poll_seconds: float = RECONCILIATION_POLL_SECONDS,
    sleeper=None,
) -> GitHubPublicationEffect:
    """Boundedly reconcile one dispatching intent without ever dispatching."""

    observations = max(1, int(max_observations))
    sleep = sleeper or time.sleep
    last_unknown: Optional[PublicationOutcomeUnknown] = None
    for index in range(observations):
        _require_surface_budget(
            deadline,
            stage="publication.reconcile",
        )
        try:
            return _observe_dispatching(
                repo,
                pr_number,
                intent=intent,
                candidate=candidate,
                deadline=deadline,
            )
        except PublicationOutcomeUnknown as exc:
            last_unknown = exc
        if index + 1 >= observations:
            break
        wait = max(0.0, float(poll_seconds))
        if deadline is not None:
            remaining = deadline.remaining_seconds()
            if remaining <= max(0.1, wait):
                break
            wait = min(wait, max(0.0, remaining - 0.1))
        if wait > 0:
            sleep(wait)
    raise PublicationOutcomeUnknown(
        "No unique exact GitHub effect appeared within the reconciliation window.",
        stage=(
            last_unknown.stage
            if last_unknown is not None
            else "publication.reconcile"
        ),
    ) from last_unknown

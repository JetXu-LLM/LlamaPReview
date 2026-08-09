"""Crash-safe coordinator for the one GitHub review transaction.

Candidate integrity belongs to :mod:`publication_candidate`; GitHub surface
observation and the sole write primitive belong to
:mod:`github_publication_surface`.  This module alone advances the durable
prepared/dispatching intent and commits the exact terminal receipt.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .. import persistence
from ..deadline import Deadline
from ..errors import (
    PublicationIdentityUnavailable,
    PublicationIntegrityFailure,
    PublicationOutcomeUnknown,
    PublicationStateConflict,
    classify_failure,
)
from .github_publication_surface import (
    GitHubPublicationEffect,
    _dispatch_exact_review,
    assert_no_existing_bot_review,
    reconcile_dispatching,
)
from .publication_candidate import (
    PUBLICATION_SCHEMA_VERSION,
    build_candidate,
    load_candidate,
    persist_prepared_intent,
    prepared_from_candidate,
    validate_recovery_binding,
)
from .publish import PreparedGitHubReview


POST_WRITE_OBSERVATION_FIELDS = frozenset(
    {
        "publication_post_write_observation",
        "publication_post_write_observation_error",
        "publication_post_write_head_sha",
        "publication_post_write_state",
        "publication_post_write_merged",
        "publication_head_changed_after_dispatch",
    }
)

@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    publication_key: str
    outcome: str
    payload_sha256: str
    review_id: int | str
    commit_id: str
    inline_comment_ids: tuple[int | str, ...]
    publication_generation_phase: str
    publication_generation_attempt: int
    publication_attempt: int
    publication_recovery_attempt: int
    recorded_at: str

    def as_dict(self) -> Dict[str, Any]:
        receipt = {
            "schema_version": PUBLICATION_SCHEMA_VERSION,
            "publication_key": self.publication_key,
            "outcome": self.outcome,
            "payload_sha256": self.payload_sha256,
            "review_id": self.review_id,
            "commit_id": self.commit_id,
            "inline_comment_ids": list(self.inline_comment_ids),
            "publication_generation_phase": (
                self.publication_generation_phase
            ),
            "publication_generation_attempt": (
                self.publication_generation_attempt
            ),
            "recorded_at": self.recorded_at,
            "publication_attempt": self.publication_attempt,
            "publication_recovery_attempt": (
                self.publication_recovery_attempt
            ),
        }
        if self.publication_generation_phase == "review":
            receipt["review_generation_attempt"] = (
                self.publication_generation_attempt
            )
        return receipt

    def artifact_fields(self) -> Dict[str, Any]:
        return {
            "publication_status": "published",
            "github_review_id": self.review_id,
            "github_review_commit_id": self.commit_id,
            "github_inline_comment_ids": list(
                self.inline_comment_ids
            ),
            "publication_receipt": self.as_dict(),
            "publication_attempt": self.publication_attempt,
            "publication_recovery_attempt": (
                self.publication_recovery_attempt
            ),
        }


def begin_recovery(
    *,
    repo: str,
    pr_number: int,
    expected_status: str,
    intent: Mapping[str, Any],
    phase_claim: Mapping[str, Any],
    recovery_runtime_identity: Mapping[str, Any],
    table=None,
) -> Dict[str, Any]:
    next_intent = deepcopy(dict(intent))
    recovery_attempt = int(phase_claim.get("attempt") or 0)
    if recovery_attempt <= 0:
        raise PublicationStateConflict(
            "Publication recovery requires the actual claimed phase attempt.",
            stage="publication.recovery",
        )
    next_intent["publication_recovery_attempt"] = recovery_attempt
    next_intent["owner_request_id"] = str(
        phase_claim.get("owner_id") or ""
    )
    next_intent["publication_recovery_runtime_identity"] = deepcopy(
        dict(recovery_runtime_identity)
    )
    stored = persistence.replace_publication_intent(
        repo,
        pr_number,
        expected_status=expected_status,
        expected_intent=intent,
        next_intent=next_intent,
        phase_claim=phase_claim,
        table=table,
    )
    if not stored:
        raise PublicationStateConflict(
            "Publication recovery lost its owner-bound intent.",
            stage="publication.recovery",
        )
    return next_intent


def mark_dispatching(
    *,
    repo: str,
    pr_number: int,
    expected_status: str,
    intent: Mapping[str, Any],
    phase_claim: Mapping[str, Any],
    table=None,
) -> Dict[str, Any]:
    if str(intent.get("state") or "") != "prepared":
        raise PublicationStateConflict(
            "Only a prepared publication intent may dispatch.",
            stage="publication.dispatch",
        )
    next_intent = deepcopy(dict(intent))
    next_intent.update(
        {
            "state": "dispatching",
            "publication_attempt": 1,
            "dispatched_at": persistence.iso_now(),
            "owner_request_id": str(
                phase_claim.get("owner_id") or ""
            ),
        }
    )
    stored = persistence.replace_publication_intent(
        repo,
        pr_number,
        expected_status=expected_status,
        expected_intent=intent,
        next_intent=next_intent,
        phase_claim=phase_claim,
        table=table,
    )
    if not stored:
        raise PublicationStateConflict(
            "Prepared publication intent lost its dispatch CAS.",
            stage="publication.dispatch",
        )
    return next_intent


def _receipt(
    intent: Mapping[str, Any],
    effect: GitHubPublicationEffect,
) -> PublicationReceipt:
    return PublicationReceipt(
        publication_key=str(intent.get("publication_key") or ""),
        outcome=effect.outcome,
        payload_sha256=str(intent.get("payload_sha256") or ""),
        review_id=effect.review_id,
        commit_id=effect.commit_id,
        inline_comment_ids=effect.inline_comment_ids,
        publication_generation_phase=str(
            intent.get("publication_generation_phase") or ""
        ),
        publication_generation_attempt=int(
            intent.get("publication_generation_attempt") or 0
        ),
        publication_attempt=int(
            intent.get("publication_attempt") or 0
        ),
        publication_recovery_attempt=int(
            intent.get("publication_recovery_attempt") or 0
        ),
        recorded_at=persistence.iso_now(),
    )


def terminal_payload(
    candidate: Mapping[str, Any],
    intent: Mapping[str, Any],
    receipt: PublicationReceipt,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    artifact = deepcopy(candidate.get("review_artifact") or {})
    terminal_attributes = deepcopy(
        candidate.get("terminal_attributes") or {}
    )
    fields = receipt.artifact_fields()
    artifact.update(fields)
    artifact["publication_intent"] = deepcopy(dict(intent))
    artifact["publication_generation_phase"] = str(
        intent.get("publication_generation_phase") or ""
    )
    artifact["publication_generation_attempt"] = int(
        intent.get("publication_generation_attempt") or 0
    )
    if intent.get("publication_generation_phase") == "review":
        artifact["review_generation_attempt"] = int(
            intent.get("publication_generation_attempt") or 0
        )
    terminal_attributes.update(fields)
    terminal_attributes["publication_generation_phase"] = str(
        intent.get("publication_generation_phase") or ""
    )
    terminal_attributes["publication_generation_attempt"] = int(
        intent.get("publication_generation_attempt") or 0
    )
    if intent.get("publication_generation_phase") == "review":
        terminal_attributes["review_generation_attempt"] = int(
            intent.get("publication_generation_attempt") or 0
        )
    return artifact, terminal_attributes


def store_terminal_receipt(
    *,
    candidate: Mapping[str, Any],
    intent: Mapping[str, Any],
    receipt: PublicationReceipt,
    expected_status: str,
    phase_claim: Mapping[str, Any],
    observation: Optional[Mapping[str, Any]] = None,
    table=None,
) -> bool:
    """Atomically persist one exact receipt, or prove it already won."""

    expected = {
        "publication_key": str(
            candidate.get("publication_key") or ""
        ),
        "payload_sha256": str(
            candidate.get("payload_sha256") or ""
        ),
        "commit_id": str(candidate.get("head_sha") or ""),
        "publication_generation_phase": str(
            candidate.get("publication_generation_phase") or ""
        ),
        "publication_generation_attempt": int(
            candidate.get("publication_generation_attempt") or 0
        ),
        "publication_attempt": int(
            intent.get("publication_attempt") or 0
        ),
        "publication_recovery_attempt": int(
            intent.get("publication_recovery_attempt") or 0
        ),
    }
    actual = {
        field: getattr(receipt, field) for field in expected
    }
    candidate_intent_fields = (
        "publication_key",
        "payload_sha256",
        "head_sha",
        "publication_generation_phase",
        "publication_generation_attempt",
    )
    expected_inline_count = len(
        prepared_from_candidate(candidate).comments
    )

    def valid_identity(value: Any) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return value > 0
        return isinstance(value, str) and bool(value.strip())

    if (
        actual != expected
        or any(
            candidate.get(field) != intent.get(field)
            for field in candidate_intent_fields
        )
        or receipt.outcome not in {"created", "adopted"}
        or not valid_identity(receipt.review_id)
        or len(receipt.inline_comment_ids) != expected_inline_count
        or any(
            not valid_identity(value)
            for value in receipt.inline_comment_ids
        )
        or not str(receipt.recorded_at or "").strip()
    ):
        raise PublicationIntegrityFailure(
            "Publication receipt is not bound to the candidate and intent.",
            stage="publication.terminal",
        )

    artifact, terminal_attributes = terminal_payload(
        candidate,
        intent,
        receipt,
    )
    if observation:
        observed = deepcopy(dict(observation))
        unknown_fields = set(observed) - POST_WRITE_OBSERVATION_FIELDS
        if unknown_fields:
            raise PublicationIntegrityFailure(
                "Post-write observation contains unauthorized terminal fields.",
                stage="publication.terminal",
            )
        artifact.update(observed)
        terminal_attributes.update(observed)
    stored = persistence.store_review_result(
        str(candidate.get("repo") or ""),
        int(candidate.get("pr_number") or 0),
        expected_status=expected_status,
        dry_run=False,
        review_comment=str(artifact.get("main_comment") or ""),
        artifact=artifact,
        review_mode=str(artifact.get("review_mode") or ""),
        extra_attrs=terminal_attributes,
        phase_claim=phase_claim,
        head_sha=str(candidate.get("head_sha") or ""),
        run_id=str(candidate.get("run_id") or ""),
        expected_publication_intent=intent,
        table=table,
    )
    if stored:
        return True
    latest = persistence.get_item(
        str(candidate.get("repo") or ""),
        int(candidate.get("pr_number") or 0),
        table=table,
        consistent_read=True,
    ) or {}
    if (
        latest.get("status") == "PROCESSED"
        and latest.get("publication_receipt") == receipt.as_dict()
    ):
        return True
    raise PublicationStateConflict(
        "Publication receipt terminal transition lost its exact intent.",
        stage="publication.terminal",
    )


def execute_dispatching(
    repo: Any,
    pr_number: int,
    *,
    intent: Mapping[str, Any],
    candidate: Mapping[str, Any],
    deadline: Optional[Deadline] = None,
) -> PublicationReceipt:
    """Dispatch once, then prove the exact effect from complete GitHub state."""

    dispatch_error: Optional[BaseException] = None
    dispatched = False
    try:
        _dispatch_exact_review(
            repo,
            pr_number,
            prepared_from_candidate(candidate),
            deadline=deadline,
        )
        dispatched = True
    except PublicationIntegrityFailure:
        raise
    except Exception as error:
        dispatch_error = error
        classified = classify_failure(
            error,
            stage="publication.dispatch",
        )
        if (
            not classified.retryable
            and not isinstance(
                error, PublicationIdentityUnavailable
            )
        ):
            raise
    try:
        effect = reconcile_dispatching(
            repo,
            pr_number,
            intent=intent,
            candidate=candidate,
            deadline=deadline,
        )
        if dispatched:
            effect = GitHubPublicationEffect(
                outcome="created",
                review_id=effect.review_id,
                commit_id=effect.commit_id,
                inline_comment_ids=effect.inline_comment_ids,
            )
        return _receipt(intent, effect)
    except PublicationOutcomeUnknown as outcome:
        if dispatch_error is None:
            raise
        raise outcome from dispatch_error


def publish_prepared_transaction(
    prepared: PreparedGitHubReview,
    *,
    repo_obj: Any,
    repo: str,
    pr_number: int,
    expected_status: str,
    run_id: str,
    phase: str,
    generation_attempt: int,
    runtime_identity: Mapping[str, Any],
    terminal_attributes: Mapping[str, Any],
    pre_publish_check: Callable[[], None],
    phase_claim: Mapping[str, Any],
    post_write_observation: Optional[
        Callable[[Mapping[str, Any]], Mapping[str, Any]]
    ] = None,
    deadline: Optional[Deadline] = None,
    table=None,
) -> bool:
    """Own the sole prepared-to-receipt control path for any pipeline phase."""

    pre_publish_check()
    preflight_completed_at = assert_no_existing_bot_review(
        repo_obj,
        pr_number,
        deadline=deadline,
    )
    candidate = build_candidate(
        prepared,
        repo=repo,
        pr_number=pr_number,
        run_id=run_id,
        phase=phase,
        owner_event_id=str(
            phase_claim.get("stream_event_id") or ""
        ),
        owner_request_id=str(phase_claim.get("owner_id") or ""),
        publication_generation_attempt=generation_attempt,
        preflight_completed_at=preflight_completed_at,
        generation_runtime_identity=runtime_identity,
        terminal_attributes=terminal_attributes,
    )
    intent = persist_prepared_intent(
        candidate,
        expected_status=expected_status,
        phase_claim=phase_claim,
        table=table,
    )
    pre_publish_check()
    assert_no_existing_bot_review(
        repo_obj,
        pr_number,
        deadline=deadline,
    )
    intent = mark_dispatching(
        repo=repo,
        pr_number=pr_number,
        expected_status=expected_status,
        intent=intent,
        phase_claim=phase_claim,
        table=table,
    )
    receipt = execute_dispatching(
        repo_obj,
        pr_number,
        intent=intent,
        candidate=candidate,
        deadline=deadline,
    )
    observation = (
        post_write_observation(candidate)
        if post_write_observation is not None
        else {}
    )
    return store_terminal_receipt(
        candidate=candidate,
        intent=intent,
        receipt=receipt,
        expected_status=expected_status,
        phase_claim=phase_claim,
        observation=observation,
        table=table,
    )


def recover_publication_transaction(
    *,
    current_item: Mapping[str, Any],
    expected_status: str,
    phase_claim: Mapping[str, Any],
    recovery_runtime_identity: Mapping[str, Any],
    repository_for: Callable[[str], Any],
    pre_publish_check_for: Callable[
        [Mapping[str, Any]], Callable[[], None]
    ],
    post_write_observation: Optional[
        Callable[[Mapping[str, Any]], Mapping[str, Any]]
    ] = None,
    prepared_no_write_commit: Optional[
        Callable[[Mapping[str, Any], Mapping[str, Any]], bool]
    ] = None,
    deadline: Optional[Deadline] = None,
    table=None,
) -> bool:
    """Resume one current intent without repeating generation or dispatch."""

    raw_intent = current_item.get("publication_intent")
    if not isinstance(raw_intent, Mapping):
        return False
    intent = deepcopy(dict(raw_intent))
    event_id = str(phase_claim.get("stream_event_id") or "")
    if not event_id or str(intent.get("owner_event_id") or "") != event_id:
        raise PublicationStateConflict(
            "Publication intent belongs to a different stream record.",
            stage="publication.recovery",
        )
    candidate = load_candidate(intent)
    validate_recovery_binding(
        current_item=current_item,
        intent=intent,
        candidate=candidate,
        expected_status=expected_status,
        phase_claim=phase_claim,
    )
    intent = begin_recovery(
        repo=str(candidate.get("repo") or ""),
        pr_number=int(candidate.get("pr_number") or 0),
        expected_status=expected_status,
        intent=intent,
        phase_claim=phase_claim,
        recovery_runtime_identity=recovery_runtime_identity,
        table=table,
    )
    state = str(intent.get("state") or "")
    if state == "prepared":
        if prepared_no_write_commit is not None:
            return prepared_no_write_commit(candidate, intent)
        repo_obj = repository_for(str(candidate.get("repo") or ""))
        pre_publish_check = pre_publish_check_for(candidate)
        pre_publish_check()
        assert_no_existing_bot_review(
            repo_obj,
            int(candidate.get("pr_number") or 0),
            deadline=deadline,
        )
        intent = mark_dispatching(
            repo=str(candidate.get("repo") or ""),
            pr_number=int(candidate.get("pr_number") or 0),
            expected_status=expected_status,
            intent=intent,
            phase_claim=phase_claim,
            table=table,
        )
        receipt = execute_dispatching(
            repo_obj,
            int(candidate.get("pr_number") or 0),
            intent=intent,
            candidate=candidate,
            deadline=deadline,
        )
    elif state == "dispatching":
        repo_obj = repository_for(str(candidate.get("repo") or ""))
        effect = reconcile_dispatching(
            repo_obj,
            int(candidate.get("pr_number") or 0),
            intent=intent,
            candidate=candidate,
            deadline=deadline,
        )
        receipt = _receipt(intent, effect)
    else:
        raise PublicationStateConflict(
            f"Unsupported publication intent state: {state or 'missing'}",
            stage="publication.recovery",
        )
    observation = (
        post_write_observation(candidate)
        if post_write_observation is not None
        else {}
    )
    return store_terminal_receipt(
        candidate=candidate,
        intent=intent,
        receipt=receipt,
        expected_status=expected_status,
        phase_claim=phase_claim,
        observation=observation,
        table=table,
    )

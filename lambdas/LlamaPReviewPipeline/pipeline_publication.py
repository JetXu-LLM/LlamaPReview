"""Phase-neutral adapter for the crash-safe GitHub publication transaction.

Context terminal notices and generated reviews enter the same prepared-review
commit boundary.  The adapter selects live transaction versus dry-run storage,
but never implements a second GitHub dispatch path or another intent state.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import persistence
from .deadline import Deadline, DeadlineExceeded
from .errors import (
    PublicationIntegrityFailure,
    PublicationOutcomeUnknown,
    PublicationStateConflict,
)
from .pipeline_admission import assert_current_head, current_pr_snapshot
from .pr_ingest import fetch_pr_details, has_existing_llamapreview_review
from .review.publication import (
    publish_prepared_transaction,
    recover_publication_transaction,
)
from .review.github_publication_surface import (
    GITHUB_SURFACE_OPERATION_BUDGET_SECONDS,
)
from .review.publication_candidate import prepared_from_candidate
from .review.publish import (
    GITHUB_PUBLICATION_FIELDS,
    PreparedGitHubReview,
    prepare_main_comment_publication,
)


@dataclass(frozen=True, slots=True)
class PublicationContext:
    """Explicit invocation facts shared by Context and Review publication."""

    repo: str
    pr_number: int
    head_sha: str
    expected_status: str
    phase: str
    run_id: str
    generation_attempt: int
    runtime_identity: Mapping[str, Any]
    phase_claim: Mapping[str, Any]
    dry_run: bool

    def __post_init__(self) -> None:
        expected = {"context": "PENDING", "review": "CONTEXT_READY"}
        if self.phase not in expected:
            raise ValueError(f"Unsupported publication phase: {self.phase}")
        if self.expected_status != expected[self.phase]:
            raise ValueError(
                "Publication phase and expected status do not agree"
            )


def terminal_ci_gate_status(ci_snapshot: Mapping[str, Any]) -> str:
    """Preserve exact-head CI state without adding generic public copy."""

    if ci_snapshot.get("blocking_checks"):
        return "ci_failure"
    if ci_snapshot.get("action_required_checks"):
        return "ci_action_required"
    if ci_snapshot.get("pending_checks"):
        return "ci_pending"
    if ci_snapshot.get("incomplete_checks"):
        return "ci_incomplete"
    return ""


def canonical_ai_skip_reason(analyzer_result: Mapping[str, Any]) -> str:
    """Build public skip copy without echoing model-authored factual claims."""

    if str(analyzer_result.get("pr_type") or "").strip().lower() == "docs":
        return "This change was classified as documentation-only."
    return (
        "This change was classified as having no substantive "
        "code-review target."
    )


def make_pre_publish_check(
    context: PublicationContext,
    runtime: Any,
    *,
    table=None,
    check_duplicate: bool = True,
):
    """Return the final exact-state/head check run before a live write."""

    def pre_publish_check() -> None:
        latest = persistence.get_item(
            context.repo,
            context.pr_number,
            table=table,
            consistent_read=True,
        )
        if not latest:
            raise RuntimeError(
                "Pre-publish state is missing; aborting GitHub write"
            )
        if latest.get("status") != context.expected_status:
            raise RuntimeError(
                f"Pre-publish status changed to {latest.get('status')}; "
                "aborting GitHub write"
            )
        latest_pr_content, _ = fetch_pr_details(
            runtime,
            context.repo,
            context.pr_number,
        )
        assert_current_head(
            runtime,
            context.repo,
            context.pr_number,
            context.head_sha,
            pr_content=latest_pr_content,
            stage="pre_publish",
        )
        if check_duplicate and has_existing_llamapreview_review(
            latest_pr_content
        ):
            raise RuntimeError(
                "Pre-publish duplicate guard: already reviewed by "
                "llamapreview[bot]"
            )

    return pre_publish_check


def post_publication_observation(
    *,
    runtime: Any,
    repo: str,
    pr_number: int,
    expected_head_sha: str,
    deadline: Optional[Deadline] = None,
) -> Dict[str, Any]:
    """Observe lifecycle after an irreversible write without erasing receipt."""

    if deadline is not None:
        try:
            deadline.check(
                "publication.post_write_observation",
                minimum_seconds=(
                    GITHUB_SURFACE_OPERATION_BUDGET_SECONDS
                ),
            )
        except DeadlineExceeded:
            return {
                "publication_post_write_observation": (
                    "skipped_deadline"
                ),
            }
    try:
        snapshot = current_pr_snapshot(
            runtime,
            repo,
            pr_number,
            stage="publication.post_write_observation",
        )
    except Exception as exc:
        return {
            "publication_post_write_observation": "unavailable",
            "publication_post_write_observation_error": (
                exc.__class__.__name__
            ),
        }
    observed_head = str(snapshot.get("head_sha") or "")
    return {
        "publication_post_write_observation": "complete",
        "publication_post_write_head_sha": observed_head,
        "publication_post_write_state": str(snapshot.get("state") or ""),
        "publication_post_write_merged": bool(snapshot.get("merged")),
        "publication_head_changed_after_dispatch": (
            observed_head != str(expected_head_sha)
        ),
    }


def recover_pending(
    item: Mapping[str, Any],
    *,
    context: PublicationContext,
    runtime: Any,
    deadline: Optional[Deadline],
    table=None,
) -> bool:
    """Recover the current intent without replaying phase generation."""

    def commit_prepared_without_write(
        candidate: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> bool:
        prepared = prepared_from_candidate(candidate)
        artifact = deepcopy(prepared.artifact)
        artifact.update(
            {
                "publication_status": "suppressed_dry_run",
                "publication_suppressed_reason": (
                    "dry_run_enabled_before_dispatch"
                ),
            }
        )
        terminal_attributes = {
            **deepcopy(
                dict(candidate.get("terminal_attributes") or {})
            ),
            "publication_status": "suppressed_dry_run",
            "publication_suppressed_reason": (
                "dry_run_enabled_before_dispatch"
            ),
            "publication_generation_phase": candidate.get(
                "publication_generation_phase"
            ),
            "publication_generation_attempt": candidate.get(
                "publication_generation_attempt"
            ),
            "publication_recovery_attempt": intent.get(
                "publication_recovery_attempt"
            ),
        }
        stored = persistence.store_review_result(
            str(candidate.get("repo") or ""),
            int(candidate.get("pr_number") or 0),
            expected_status=context.expected_status,
            dry_run=True,
            review_comment=str(artifact.get("main_comment") or ""),
            artifact=artifact,
            review_mode=str(artifact.get("review_mode") or ""),
            extra_attrs=terminal_attributes,
            phase_claim=context.phase_claim,
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
            latest.get("status") == "PROCESSED_DRYRUN"
            and latest.get("publication_suppressed_reason")
            == "dry_run_enabled_before_dispatch"
        ):
            return True
        raise PublicationStateConflict(
            "Dry-run suppression lost the prepared publication intent.",
            stage="publication.dry_run_suppression",
        )

    return recover_publication_transaction(
        current_item=item,
        expected_status=context.expected_status,
        phase_claim=context.phase_claim,
        recovery_runtime_identity=context.runtime_identity,
        repository_for=runtime.get_repository,
        pre_publish_check_for=lambda candidate: make_pre_publish_check(
            PublicationContext(
                repo=str(candidate.get("repo") or ""),
                pr_number=int(candidate.get("pr_number") or 0),
                head_sha=str(candidate.get("head_sha") or ""),
                expected_status=context.expected_status,
                phase=context.phase,
                run_id=str(candidate.get("run_id") or ""),
                generation_attempt=int(
                    candidate.get("publication_generation_attempt") or 0
                ),
                runtime_identity=context.runtime_identity,
                phase_claim=context.phase_claim,
                dry_run=False,
            ),
            runtime,
            table=table,
            check_duplicate=False,
        ),
        post_write_observation=lambda candidate: (
            post_publication_observation(
                runtime=runtime,
                repo=str(candidate.get("repo") or ""),
                pr_number=int(candidate.get("pr_number") or 0),
                expected_head_sha=str(candidate.get("head_sha") or ""),
                deadline=deadline,
            )
        ),
        prepared_no_write_commit=(
            commit_prepared_without_write if context.dry_run else None
        ),
        deadline=deadline,
        table=table,
    )


def commit_prepared(
    prepared: PreparedGitHubReview,
    terminal_attributes: Mapping[str, Any],
    *,
    context: PublicationContext,
    runtime: Any,
    deadline: Optional[Deadline],
    pre_persist_stage: str,
    table=None,
) -> bool:
    """Commit one prepared result through the sole live or dry-run boundary."""

    if not context.dry_run:
        repo_obj = runtime.get_repository(context.repo)
        return publish_prepared_transaction(
            prepared=prepared,
            repo_obj=repo_obj,
            repo=context.repo,
            pr_number=context.pr_number,
            expected_status=context.expected_status,
            run_id=context.run_id,
            phase=context.phase,
            generation_attempt=context.generation_attempt,
            runtime_identity=context.runtime_identity,
            terminal_attributes=terminal_attributes,
            pre_publish_check=make_pre_publish_check(
                context,
                runtime,
                table=table,
                check_duplicate=False,
            ),
            phase_claim=context.phase_claim,
            post_write_observation=lambda candidate: (
                post_publication_observation(
                    runtime=runtime,
                    repo=str(candidate.get("repo") or ""),
                    pr_number=int(candidate.get("pr_number") or 0),
                    expected_head_sha=str(
                        candidate.get("head_sha") or ""
                    ),
                    deadline=deadline,
                )
            ),
            deadline=deadline,
            table=table,
        )

    assert_current_head(
        runtime,
        context.repo,
        context.pr_number,
        context.head_sha,
        stage=pre_persist_stage,
    )
    artifact = prepared.artifact
    return persistence.store_review_result(
        context.repo,
        context.pr_number,
        expected_status=context.expected_status,
        dry_run=True,
        review_comment=str(artifact.get("main_comment") or ""),
        artifact=artifact,
        review_mode=str(artifact.get("review_mode") or ""),
        extra_attrs=dict(terminal_attributes),
        phase_claim=context.phase_claim,
        head_sha=context.head_sha,
        run_id=context.run_id,
        table=table,
    )


def commit_terminal_result(
    *,
    context: PublicationContext,
    runtime: Any,
    review_mode: str,
    body: str,
    deadline: Optional[Deadline] = None,
    ci_snapshot: Optional[Mapping[str, Any]] = None,
    extra_attributes: Optional[Mapping[str, Any]] = None,
    generation_status: str = "complete",
    quality_scoreable: bool = True,
    quality_exclusion_reasons: Optional[list[str]] = None,
    table=None,
) -> bool:
    """Build and commit a deterministic Context terminal notice."""

    if review_mode == "skip":
        generation_status = "complete"
        quality_scoreable = False
        quality_exclusion_reasons = ["skipped_by_policy"]
    assert_current_head(
        runtime,
        context.repo,
        context.pr_number,
        context.head_sha,
        stage="terminal_result.pre_render",
    )
    prepared = prepare_main_comment_publication(
        body,
        head_sha=context.head_sha,
        review_mode=review_mode,
    )
    artifact = prepared.artifact
    artifact["run_id"] = context.run_id
    artifact["pipeline_attempt"] = int(context.generation_attempt)
    artifact.update(
        {
            "review_generation_status": generation_status,
            "review_fallback_used": False,
            "review_failure_kind": None,
            "review_failure_stage": None,
            "quality_scoreable": bool(quality_scoreable),
            "quality_exclusion_reasons": list(
                quality_exclusion_reasons or []
            ),
            "context_runtime_identity": deepcopy(
                dict(context.runtime_identity)
            ),
        }
    )
    if isinstance(ci_snapshot, Mapping):
        artifact["ci_snapshot"] = deepcopy(dict(ci_snapshot))
    supplied = dict(extra_attributes or {})
    if supplied.get("visible_projection_source"):
        artifact["visible_projection_source"] = supplied[
            "visible_projection_source"
        ]
    for key in (
        "route_delta_provenance",
        "deepseek_model_phases",
        "deepseek_discarded_model_phases",
        "deepseek_all_attempt_model_phases",
        "deepseek_usage_total",
        "deepseek_winning_usage_total",
        "deepseek_discarded_usage_total",
        "deepseek_usage_accounting",
    ):
        if key in supplied:
            artifact[key] = deepcopy(supplied[key])
    terminal_attributes = {
        **supplied,
        **{
            key: deepcopy(artifact[key])
            for key in GITHUB_PUBLICATION_FIELDS
            if key in artifact
        },
        "run_id": context.run_id,
        "pipeline_attempt": int(context.generation_attempt),
        "review_generation_status": generation_status,
        "review_fallback_used": False,
        "quality_scoreable": bool(quality_scoreable),
        "quality_exclusion_reasons": list(
            quality_exclusion_reasons or []
        ),
        "context_runtime_identity": deepcopy(dict(context.runtime_identity)),
    }
    return commit_prepared(
        prepared,
        terminal_attributes,
        context=context,
        runtime=runtime,
        deadline=deadline,
        pre_persist_stage="terminal_result.pre_persist",
        table=table,
    )


def failure_attributes(
    *,
    repo: str,
    pr_number: int,
    exc: BaseException,
    base: Mapping[str, Any],
    table=None,
) -> Dict[str, Any]:
    """Project publication state into a typed terminal failure."""

    attrs = dict(base)
    if not isinstance(
        exc,
        (
            PublicationOutcomeUnknown,
            PublicationIntegrityFailure,
            PublicationStateConflict,
        ),
    ):
        return attrs
    latest = persistence.get_item(
        repo,
        pr_number,
        table=table,
        consistent_read=True,
    ) or {}
    intent = latest.get("publication_intent")
    if isinstance(intent, dict):
        attrs.update(
            {
                "publication_generation_phase": intent.get(
                    "publication_generation_phase"
                ),
                "publication_generation_attempt": intent.get(
                    "publication_generation_attempt"
                ),
                "publication_attempt": intent.get("publication_attempt"),
                "publication_recovery_attempt": intent.get(
                    "publication_recovery_attempt"
                ),
            }
        )
    attrs["publication_status"] = (
        "outcome_unknown"
        if isinstance(exc, PublicationOutcomeUnknown)
        else "integrity_failure"
    )
    return attrs


def generation_is_pending(
    *,
    repo: str,
    pr_number: int,
    phase: str,
    generation_attempt: int,
    stream_event_id: str,
    table=None,
) -> bool:
    """Return whether usage still belongs to a recoverable publication."""

    current = persistence.get_item(
        repo,
        pr_number,
        table=table,
        consistent_read=True,
    ) or {}
    intent = current.get("publication_intent")
    return bool(
        isinstance(intent, dict)
        and not current.get("publication_receipt")
        and intent.get("state") in {"prepared", "dispatching"}
        and str(intent.get("publication_generation_phase") or "")
        == str(phase)
        and int(intent.get("publication_generation_attempt") or 0)
        == int(generation_attempt)
        and str(intent.get("owner_event_id") or "")
        == str(stream_event_id or "")
    )

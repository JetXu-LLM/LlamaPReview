"""Two-phase pipeline orchestration."""

from __future__ import annotations

import json
import logging
import time
from copy import deepcopy
from typing import Any, Dict, Mapping, Optional

from . import config, persistence, pipeline_admission, pipeline_publication
from .context_engine.low_route import (
    collect_low_same_file_context as _collect_low_same_file_context,
)
from .context_engine.pfr import collect_context_pfr as collect_context
from .context_engine.repo_structure import (
    RepoInventory,
    fetch_repo_inventory,
)
from .deadline import Deadline, DeadlineExceeded
from .deepseek_client import (
    DeepSeekClient,
)
from .errors import (
    CIRefreshUnavailable,
    HeadSuperseded,
    HeadVerificationUnavailable,
    PRLifecycleSuperseded,
    ReviewGenerationIncomplete,
    classify_failure,
)
from .pr_ingest import (
    GitHubRuntime,
    fetch_pr_details,
    has_existing_llamapreview_review,
)
from .pipeline_accounting import (
    bind_provider_call_accounting,
    canonical_context_model_phases,
    emit_discarded_attempt_usage,
    provider_call_records,
    provider_usage_accounting,
    route_delta_provenance,
    route_model_phases,
)
from .pipeline_ci import (
    model_ci_snapshot_payload,
    refresh_review_ci_context,
    reapply_latest_ci_guard,
)
from .provider_source import (
    build_provider_source_identity,
    prepare_provider_source,
)
from .review.analyzer import analyze_pr_complexity, build_changed_delta_focus
from .review.generate import generate_review
from .review.placement_sources import (
    fetch_pr_files_and_contents as _fetch_pr_files_and_contents,
)
from .review.terminal_messages import skipped_review_notice
from .review.publish import (
    build_diff_maps_from_pr_files,
    prepare_review_publication,
)
from .review import result_artifact
from .review.evidence_contract import (
    build_review_evidence_catalog,
)
from .runtime_identity import capture_runtime_identity

logger = logging.getLogger(__name__)


class _LifecycleStop(RuntimeError):
    """Internal control signal after lifecycle state is durably handled."""


def _verified_disposition(
    runtime: Any,
    repo: str,
    pr_number: int,
    head_sha: str,
    *,
    stage: str,
) -> pipeline_admission.PRLifecycleDisposition:
    disposition = pipeline_admission.current_pr_disposition(
        runtime,
        repo,
        pr_number,
        head_sha,
        stage=stage,
    )
    if disposition.kind is pipeline_admission.PRDispositionKind.UNVERIFIED:
        raise HeadVerificationUnavailable(
            "GitHub did not return a complete PR head/lifecycle snapshot",
            stage=stage,
        )
    _emit_pipeline_metric(
        "lifecycle_checkpoint",
        phase=stage.split(".", 1)[0],
        stage=stage,
        disposition=disposition.kind.value,
    )
    return disposition


def _has_exact_initial_admission(
    item: Dict[str, Any],
    *,
    head_sha: str,
    run_id: str,
) -> bool:
    admission = item.get("initial_admission")
    return bool(
        isinstance(admission, dict)
        and int(admission.get("schema_version") or 0) == 1
        and str(admission.get("disposition") or "") == "open_same_head"
        and str(admission.get("head_sha") or "") == str(head_sha)
        and str(admission.get("run_id") or "") == str(run_id)
        and str(admission.get("admitted_at") or "")
    )


def _publication_context_for_lifecycle(
    context: pipeline_publication.PublicationContext,
    *,
    publication_kind: str,
    required_disposition: str,
) -> pipeline_publication.PublicationContext:
    return pipeline_publication.PublicationContext(
        repo=context.repo,
        pr_number=context.pr_number,
        head_sha=context.head_sha,
        expected_status=context.expected_status,
        phase=context.phase,
        run_id=context.run_id,
        generation_attempt=context.generation_attempt,
        runtime_identity=context.runtime_identity,
        phase_claim=context.phase_claim,
        dry_run=context.dry_run,
        publication_kind=publication_kind,
        required_disposition=required_disposition,
    )


def _lifecycle_accounting(
    *,
    repo: str,
    pr_number: int,
    table: Any,
    client: Any,
    context_attempt: int,
    review_attempt: Optional[int],
) -> Dict[str, Any]:
    return provider_usage_accounting(
        provider_calls=provider_call_records(
            repo=repo,
            pr_number=pr_number,
            table=table,
            client=client,
        ),
        fallback_winning_phases=[],
        context_attempt=int(context_attempt or 0),
        review_attempt=review_attempt,
    )


def _mark_lifecycle_superseded(
    disposition: pipeline_admission.PRLifecycleDisposition,
    *,
    context: pipeline_publication.PublicationContext,
    stage: str,
    accounting: Dict[str, Any],
    table: Any,
) -> None:
    if disposition.kind in {
        pipeline_admission.PRDispositionKind.MERGED_SAME_HEAD,
        pipeline_admission.PRDispositionKind.MERGED_NEW_HEAD,
    }:
        superseded_kind = "pr_merged"
    elif disposition.kind in {
        pipeline_admission.PRDispositionKind.CLOSED_SAME_HEAD,
        pipeline_admission.PRDispositionKind.CLOSED_NEW_HEAD,
    }:
        superseded_kind = "pr_closed"
    else:
        superseded_kind = "head_changed"
    persistence.mark_superseded(
        context.repo,
        context.pr_number,
        context.expected_status,
        expected_head_sha=context.head_sha,
        actual_head_sha=disposition.actual_head_sha,
        stage=stage,
        superseded_kind=superseded_kind,
        current_state=disposition.current_state,
        merged=disposition.merged,
        extra_attrs=accounting,
        phase_claim=context.phase_claim,
        table=table,
    )


def _commit_lifecycle_cancellation(
    disposition: pipeline_admission.PRLifecycleDisposition,
    *,
    context: pipeline_publication.PublicationContext,
    runtime: Any,
    deadline: Deadline,
    accounting: Dict[str, Any],
    table: Any,
    extra_attributes: Optional[Dict[str, Any]] = None,
) -> None:
    lifecycle = "merged" if disposition.merged else "closed"
    required = f"{lifecycle}_same_head"
    cancellation_context = _publication_context_for_lifecycle(
        context,
        publication_kind="lifecycle_cancellation",
        required_disposition=required,
    )
    unavailable_observed = {"value": False}

    def observe_unavailable(fields: Mapping[str, Any]) -> None:
        unavailable_observed["value"] = True
        _observe_lifecycle_publication_unavailable(fields)

    stored = pipeline_publication.commit_lifecycle_cancellation(
        context=cancellation_context,
        runtime=runtime,
        lifecycle=lifecycle,
        deadline=deadline,
        extra_attributes={
            **accounting,
            **(extra_attributes or {}),
        },
        lifecycle_unavailable_observer=observe_unavailable,
        table=table,
    )
    if unavailable_observed["value"]:
        return
    accounting_status = dict(
        accounting.get("deepseek_usage_accounting") or {}
    )
    _emit_pipeline_metric(
        "lifecycle_publication_complete",
        phase=context.phase,
        stage=disposition.stage,
        publication_kind="lifecycle_cancellation",
        lifecycle=lifecycle,
        stored=stored,
        provider_accounting_complete=bool(
            accounting_status.get("complete_numeric_usage")
        ),
        unreported_usage_call_count=int(
            accounting_status.get("unreported_usage_call_count") or 0
        ),
    )


def _mark_lifecycle_publication_unavailable(
    disposition: pipeline_admission.PRLifecycleDisposition,
    *,
    context: pipeline_publication.PublicationContext,
    publication_kind: str,
    accounting: Dict[str, Any],
    table: Any,
    extra_attributes: Optional[Dict[str, Any]] = None,
) -> bool:
    """Persist a structurally locked, ended publication as silent supersession."""

    if not (
        disposition.ended
        and disposition.same_head
        and disposition.locked is True
        and publication_kind
        in {"lifecycle_cancellation", "post_merge_follow_up"}
    ):
        raise ValueError(
            "locked lifecycle publication requires an exact ended head"
        )
    lifecycle = "merged" if disposition.merged else "closed"
    stored = persistence.mark_superseded(
        context.repo,
        context.pr_number,
        context.expected_status,
        expected_head_sha=context.head_sha,
        actual_head_sha=disposition.actual_head_sha,
        stage=disposition.stage,
        superseded_kind="publication_unavailable_locked",
        current_state=disposition.current_state,
        merged=disposition.merged,
        extra_attrs={
            **accounting,
            **(extra_attributes or {}),
            "publication_kind": publication_kind,
            "required_disposition": f"{lifecycle}_same_head",
            "publication_status": "unavailable_locked",
            "publication_unavailable_locked": True,
            "review_lifecycle_outcome": lifecycle,
            "quality_scoreable": False,
            "quality_exclusion_reasons": [
                "publication_unavailable_locked"
            ],
        },
        phase_claim=context.phase_claim,
        table=table,
    )
    _observe_lifecycle_publication_unavailable(
        {
            "phase": context.phase,
            "stage": disposition.stage,
            "publication_kind": publication_kind,
            "lifecycle": lifecycle,
            "reason": "locked",
            "stored": stored,
            **accounting,
        }
    )
    return stored


def _observe_lifecycle_publication_unavailable(
    fields: Mapping[str, Any],
) -> None:
    accounting_status = dict(
        fields.get("deepseek_usage_accounting") or {}
    )
    _emit_pipeline_metric(
        "lifecycle_publication_unavailable",
        phase=fields.get("phase"),
        stage=fields.get("stage"),
        publication_kind=fields.get("publication_kind"),
        lifecycle=fields.get("lifecycle"),
        reason=fields.get("reason"),
        stored=bool(fields.get("stored")),
        provider_accounting_complete=bool(
            accounting_status.get("complete_numeric_usage")
        ),
        unreported_usage_call_count=int(
            accounting_status.get("unreported_usage_call_count") or 0
        ),
    )


def _trace_metadata(repo: str, pr_number: int, head_sha: str, **extra: Any) -> Dict[str, Any]:
    dry_run = bool(extra.pop("dry_run", config.DRY_RUN))
    run_id = str(extra.pop("run_id", "") or f"{repo.replace('/', '_')}_{pr_number}")
    metadata = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "dry_run": dry_run,
        "run_id": run_id,
    }
    metadata.update(extra)
    return metadata


def _emit_pipeline_metric(name: str, **fields: Any) -> None:
    logger.info(
        "Pipeline metric: %s",
        json.dumps({"metric": name, **fields}, ensure_ascii=False, default=str, sort_keys=True),
    )


def _emit_terminal_error_metric(*, phase: str, kind: str) -> None:
    """Emit CloudWatch EMF without repo/PR dimensions or private content."""
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "LlamaPReview/Pipeline",
                            "Dimensions": [[]],
                            "Metrics": [{"Name": "TerminalErrors", "Unit": "Count"}],
                        }
                    ],
                },
                "Phase": str(phase),
                "FailureKind": str(kind),
                "TerminalErrors": 1,
            },
            sort_keys=True,
        )
    )


def _mark_terminal_error(
    repo: str,
    pr_number: int,
    expected_status: str,
    message: str,
    *,
    phase: str,
    kind: str,
    retryable: bool,
    retry_exhausted: bool,
    attempt: int,
    extra_attrs: Optional[Dict[str, Any]] = None,
    phase_claim: Optional[Dict[str, Any]] = None,
    table,
) -> bool:
    """Persist an operational terminal failure and emit exactly one alarm metric."""
    marked = persistence.mark_error(
        repo,
        pr_number,
        expected_status,
        message,
        error_kind=kind,
        error_stage=phase,
        retryable=retryable,
        retry_exhausted=retry_exhausted,
        attempt=attempt,
        extra_attrs=extra_attrs,
        phase_claim=phase_claim,
        table=table,
    )
    if marked:
        _emit_terminal_error_metric(phase=phase, kind=kind)
    return marked


def _close_owned_runtime(runtime: Any, *, owned: bool) -> None:
    if not owned or runtime is None:
        return
    close = getattr(runtime, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.exception("Failed to close GitHub runtime")


def _handle_phase_failure(
    *,
    repo: str,
    pr_number: int,
    expected_status: str,
    phase: str,
    attempt: int,
    exc: BaseException,
    terminal_attrs: Optional[Dict[str, Any]] = None,
    phase_claim: Optional[Dict[str, Any]] = None,
    table,
) -> None:
    classified = classify_failure(exc, stage=phase)
    if getattr(exc, "paid_dispatch_unrecorded", False) is True:
        provider_call_record = getattr(exc, "provider_call_record", None)
        if isinstance(provider_call_record, dict):
            latest = persistence.get_item(
                repo,
                pr_number,
                table=table,
                consistent_read=True,
            )
            accounting = provider_usage_accounting(
                provider_calls=[
                    *persistence.provider_call_records(latest),
                    provider_call_record,
                ],
                fallback_winning_phases=[],
                context_attempt=-1,
                review_attempt=None,
            )
            accounting_status = dict(
                accounting.get("deepseek_usage_accounting") or {}
            )
            accounting_status.update(
                {
                    "durable_call_ledger_complete": False,
                    "terminal_fallback_call_count": 1,
                }
            )
            accounting["deepseek_usage_accounting"] = accounting_status
            terminal_attrs = {
                **(terminal_attrs or {}),
                **accounting,
                "provider_call_ledger_terminal_fallback": {
                    "schema_version": 1,
                    "call_id": str(
                        provider_call_record.get("call_id") or ""
                    ),
                    "operation_id": str(
                        provider_call_record.get("operation_id") or ""
                    ),
                    "transport_attempt_index": int(
                        provider_call_record.get(
                            "transport_attempt_index"
                        )
                        or 0
                    ),
                    "per_call_sink_persisted": False,
                    "terminal_fallback_persisted": True,
                },
            }
    elif getattr(exc, "provider_dispatch_outcome_unknown", False) is True:
        latest = persistence.get_item(
            repo,
            pr_number,
            table=table,
            consistent_read=True,
        )
        durable_calls = persistence.provider_call_records(latest)
        accounting = provider_usage_accounting(
            provider_calls=durable_calls,
            fallback_winning_phases=[],
            context_attempt=-1,
            review_attempt=None,
        )
        accounting_status = dict(
            accounting.get("deepseek_usage_accounting") or {}
        )
        unresolved = [
            record
            for record in durable_calls
            if str(record.get("status") or "") == "dispatching"
        ]
        accounting_status.update(
            {
                "durable_call_ledger_complete": False,
                "unresolved_dispatch_fence_count": len(unresolved),
                "complete_numeric_usage": False,
            }
        )
        accounting["deepseek_usage_accounting"] = accounting_status
        terminal_attrs = {
            **(terminal_attrs or {}),
            **accounting,
            "provider_dispatch_fence_terminal": {
                "schema_version": 1,
                "call_ids": [
                    str(record.get("call_id") or "")
                    for record in unresolved
                ],
                "outcome": "unknown",
                "second_dispatch_withheld": True,
            },
        }
    _emit_pipeline_metric(
        "phase_failure",
        repo=repo,
        pr_number=pr_number,
        phase=phase,
        attempt=attempt,
        kind=classified.kind,
        stage=classified.stage,
        retryable=classified.retryable,
    )
    if isinstance(exc, (HeadSuperseded, PRLifecycleSuperseded)):
        persistence.mark_superseded(
            repo,
            pr_number,
            expected_status,
            expected_head_sha=exc.expected_head_sha,
            actual_head_sha=exc.actual_head_sha,
            stage=classified.stage,
            superseded_kind=getattr(exc, "superseded_kind", "head_changed"),
            current_state=getattr(exc, "current_state", ""),
            merged=getattr(exc, "merged", None),
            extra_attrs=terminal_attrs,
            phase_claim=phase_claim,
            table=table,
        )
        return
    if classified.retryable and attempt < int(config.MAX_ATTEMPTS):
        if phase_claim:
            persistence.release_phase_claim(
                repo,
                pr_number,
                expected_status=expected_status,
                phase_claim=phase_claim,
                table=table,
            )
        raise exc
    _mark_terminal_error(
        repo,
        pr_number,
        expected_status,
        classified.message,
        phase=classified.stage,
        kind=classified.kind,
        retryable=classified.retryable,
        retry_exhausted=bool(classified.retryable and attempt >= int(config.MAX_ATTEMPTS)),
        attempt=attempt,
        extra_attrs=terminal_attrs,
        phase_claim=phase_claim,
        table=table,
    )


def _review_model_for_mode(review_mode: str) -> Dict[str, str]:
    if review_mode == "low":
        return {"model": config.LOW_REVIEW_MODEL, "reasoning_effort": config.LOW_REVIEW_EFFORT}
    if review_mode == "normal":
        return {"model": config.NORMAL_REVIEW_MODEL, "reasoning_effort": config.NORMAL_REVIEW_EFFORT}
    return {"model": config.REVIEW_MODEL, "reasoning_effort": config.REVIEW_EFFORT}


def _context_for_mode(
    *,
    review_mode: str,
    runtime: Any,
    token: str,
    repo: str,
    pr_number: int,
    pr_content: Dict[str, Any],
    pr_details: str,
    head_sha: str,
    default_branch: str,
    trace_metadata: Dict[str, Any],
    route_plan: Dict[str, Any],
    deadline: Deadline,
    deepseek_client=None,
    table=None,
    repo_inventory: Optional[RepoInventory] = None,
    initial_evidence_ledger: Optional[Dict[str, Any]] = None,
    before_first_reconcile=None,
) -> tuple[str, Dict[str, Any]]:
    if review_mode == "low":
        return _collect_low_same_file_context(
            runtime=runtime,
            repo=repo,
            pr_content=pr_content,
            head_sha=head_sha,
            repo_inventory=repo_inventory,
            initial_evidence_ledger=initial_evidence_ledger,
        )

    kwargs: Dict[str, Any] = {}
    if review_mode == "normal":
        kwargs.update(
            {
                "model": config.PFR_NORMAL_MODEL,
                "reasoning_effort": config.PFR_NORMAL_EFFORT,
                "time_budget": config.PFR_NORMAL_TIME_BUDGET_SECONDS,
                "token_budget": config.PFR_NORMAL_TOKEN_BUDGET,
                "max_tool_rounds": config.PFR_NORMAL_MAX_TOOL_ROUNDS,
                "max_search_calls": config.PFR_NORMAL_MAX_SEARCH_CALLS,
                "max_read_calls": config.PFR_NORMAL_MAX_READ_CALLS,
                "max_context_chars": config.PFR_NORMAL_MAX_CONTEXT_CHARS,
            }
        )

    cached_fact_sheet = ""
    cached_fact_sheet = persistence.load_repo_fact_sheet(
        repo,
        head_sha=head_sha,
        table=table,
    )
    if cached_fact_sheet:
        kwargs["repo_fact_sheet"] = cached_fact_sheet

    kwargs["route_plan"] = route_plan
    kwargs["deadline"] = deadline
    kwargs["repo_inventory"] = repo_inventory
    kwargs["initial_evidence_ledger"] = initial_evidence_ledger
    kwargs["before_first_reconcile"] = before_first_reconcile

    context_text, meta = collect_context(
        runtime=runtime,
        github_token=token,
        repo_full_name=repo,
        pr_content=pr_content,
        pr_details=pr_details,
        head_sha=head_sha,
        default_branch=default_branch,
        client=deepseek_client,
        trace_metadata=trace_metadata,
        **kwargs,
    )
    if not cached_fact_sheet and meta.get("repo_fact_sheet"):
        persistence.store_repo_fact_sheet(
            repo,
            str(meta.get("repo_fact_sheet") or ""),
            head_sha=head_sha,
            owner_run_id=str((trace_metadata or {}).get("run_id") or ""),
            table=table,
        )
    meta["review_mode"] = review_mode
    meta["context_strategy"] = meta.get("context_strategy") or "pfr"
    meta["pr_number"] = pr_number
    return context_text, meta


def run_context_phase(
    item: Dict[str, Any],
    *,
    table=None,
    runtime: Optional[Any] = None,
    deepseek_client=None,
    lambda_context=None,
    stream_event_id: str = "",
) -> None:
    repo = pipeline_admission.require_item_field(item, "repo")
    pr_number = int(pipeline_admission.require_item_field(item, "pr_number"))
    if (item.get("status") or "") != "PENDING":
        return
    context_runtime_identity = capture_runtime_identity(
        lambda_context,
        phase="context",
    )
    admission = pipeline_admission.claim_phase_delivery(
        repo,
        pr_number,
        phase="context",
        expected_status="PENDING",
        runtime_identity=context_runtime_identity,
        stream_event_id=stream_event_id,
        stream_head_sha=str(item.get("head_sha") or ""),
        # Legacy queue items may predate a persisted run_id. Every successor
        # writes one, so the strong stale-event run fence is always active on
        # the new same-status transition without rejecting legacy ordinary
        # deliveries that can still be fenced by exact head.
        stream_run_id=str(item.get("run_id") or ""),
        table=table,
    )
    if admission is None:
        return
    phase_claim = admission.phase_claim
    item_for_context = admission.current_item
    dry_run = pipeline_admission.effective_dry_run(item_for_context)
    attempt = admission.attempt
    deadline = Deadline.from_lambda_context(
        lambda_context,
        phase_limit_seconds=config.PIPELINE_CONTEXT_PHASE_MAX_SECONDS,
    )
    phase_started = time.monotonic()
    runtime_owned = False
    active_runtime = runtime
    context_client = deepseek_client
    try:
        if (
            attempt > int(config.MAX_ATTEMPTS)
            and not isinstance(
                item_for_context.get("publication_intent"), dict
            )
        ):
            _mark_terminal_error(
                repo,
                pr_number,
                "PENDING",
                "Context phase retry budget exhausted before execution",
                phase="context.start",
                kind="retry_budget_exhausted",
                retryable=False,
                retry_exhausted=True,
                attempt=attempt,
                extra_attrs={
                    "context_runtime_identity": context_runtime_identity
                },
                phase_claim=phase_claim,
                table=table,
            )
            return
        installation_id = int(pipeline_admission.require_item_field(item_for_context, "installation_id"))
        head_sha = str(pipeline_admission.require_item_field(item_for_context, "head_sha"))
        default_branch = item_for_context.get("default_branch") or item_for_context.get("base_ref") or "main"
        run_id = pipeline_admission.run_id(item_for_context, repo, pr_number)
        token = pipeline_admission.installation_token(installation_id, deadline=deadline)
        if active_runtime is None:
            active_runtime = GitHubRuntime(token)
            runtime_owned = True
        context_publication = pipeline_publication.PublicationContext(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            expected_status="PENDING",
            phase="context",
            run_id=run_id,
            generation_attempt=attempt,
            runtime_identity=context_runtime_identity,
            phase_claim=phase_claim,
            dry_run=dry_run,
        )
        if pipeline_publication.recover_pending(
            item_for_context,
            context=context_publication,
            runtime=active_runtime,
            deadline=deadline,
            lifecycle_unavailable_observer=(
                _observe_lifecycle_publication_unavailable
            ),
            table=table,
        ):
            return
        if attempt > int(config.MAX_ATTEMPTS):
            _mark_terminal_error(
                repo,
                pr_number,
                "PENDING",
                "Context phase retry budget exhausted before execution",
                phase="context.start",
                kind="retry_budget_exhausted",
                retryable=False,
                retry_exhausted=True,
                attempt=attempt,
                extra_attrs={
                    "context_runtime_identity": context_runtime_identity
                },
                phase_claim=phase_claim,
                table=table,
            )
            return

        lifecycle_terminal_attrs: Dict[str, Any] = {}

        def context_lifecycle_checkpoint(
            *,
            stage: str,
            allow_successor: bool,
        ) -> pipeline_admission.PRLifecycleDisposition:
            nonlocal item_for_context
            disposition = _verified_disposition(
                active_runtime,
                repo,
                pr_number,
                head_sha,
                stage=stage,
            )
            latest = persistence.get_item(
                repo,
                pr_number,
                table=table,
                consistent_read=True,
            ) or item_for_context
            has_admission = _has_exact_initial_admission(
                latest,
                head_sha=head_sha,
                run_id=run_id,
            )
            if (
                disposition.kind
                is pipeline_admission.PRDispositionKind.OPEN_SAME_HEAD
            ):
                if not has_admission:
                    recorded = persistence.record_initial_admission(
                        repo,
                        pr_number,
                        expected_status="PENDING",
                        expected_head_sha=head_sha,
                        phase_claim=phase_claim,
                        table=table,
                    )
                    if not recorded:
                        raise RuntimeError(
                            "Initial lifecycle admission lost its exact owner"
                        )
                    item_for_context = persistence.get_item(
                        repo,
                        pr_number,
                        table=table,
                        consistent_read=True,
                    ) or item_for_context
                return disposition
            if (
                disposition.kind
                is pipeline_admission.PRDispositionKind.OPEN_NEW_HEAD
            ):
                requeued = False
                if allow_successor and str(
                    phase_claim.get("stream_event_id") or ""
                ):
                    requeued = persistence.requeue_head_successor(
                        repo,
                        pr_number,
                        expected_status="PENDING",
                        expected_head_sha=head_sha,
                        actual_head_sha=disposition.actual_head_sha,
                        stage=stage,
                        phase_claim=phase_claim,
                        table=table,
                    )
                if not requeued:
                    accounting = {
                        **_lifecycle_accounting(
                            repo=repo,
                            pr_number=pr_number,
                            table=table,
                            client=context_client,
                            context_attempt=attempt,
                            review_attempt=None,
                        ),
                        **lifecycle_terminal_attrs,
                    }
                    _mark_lifecycle_superseded(
                        disposition,
                        context=context_publication,
                        stage=stage,
                        accounting=accounting,
                        table=table,
                    )
                _emit_pipeline_metric(
                    "head_successor",
                    phase="context",
                    stage=stage,
                    requeued=requeued,
                    successor_count=(1 if requeued else 0),
                )
                raise _LifecycleStop()
            if disposition.ended:
                accounting = {
                    **_lifecycle_accounting(
                        repo=repo,
                        pr_number=pr_number,
                        table=table,
                        client=context_client,
                        context_attempt=attempt,
                        review_attempt=None,
                    ),
                    **lifecycle_terminal_attrs,
                }
                if disposition.same_head and has_admission:
                    if disposition.locked is True:
                        _mark_lifecycle_publication_unavailable(
                            disposition,
                            context=context_publication,
                            publication_kind="lifecycle_cancellation",
                            accounting=accounting,
                            table=table,
                            extra_attributes={
                                "context_runtime_identity": (
                                    context_runtime_identity
                                ),
                                "review_generation_status": "cancelled",
                            },
                        )
                    else:
                        _commit_lifecycle_cancellation(
                            disposition,
                            context=context_publication,
                            runtime=active_runtime,
                            deadline=deadline,
                            accounting=accounting,
                            table=table,
                            extra_attributes={
                                "context_runtime_identity": (
                                    context_runtime_identity
                                ),
                            },
                        )
                else:
                    _mark_lifecycle_superseded(
                        disposition,
                        context=context_publication,
                        stage=stage,
                        accounting=accounting,
                        table=table,
                    )
                raise _LifecycleStop()
            raise HeadVerificationUnavailable(
                "Pull request lifecycle could not be verified",
                stage=stage,
            )

        context_lifecycle_checkpoint(
            stage="context.ingest",
            allow_successor=True,
        )
        try:
            prepared_source = prepare_provider_source(
                active_runtime,
                repo,
                pr_number,
                head_sha,
            )
        except (HeadSuperseded, PRLifecycleSuperseded):
            context_lifecycle_checkpoint(
                stage="context.ingest",
                allow_successor=True,
            )
            raise
        pr_content = prepared_source.pr_content
        pr_details = prepared_source.base_pr_details
        ci_snapshot = prepared_source.ci_snapshot
        ingest_meta = (
            pr_content.get("_ingest_meta")
            if isinstance(pr_content.get("_ingest_meta"), dict)
            else {}
        )
        source_pr_details_chars = int(
            ingest_meta.get("source_pr_details_chars") or len(pr_details)
        )
        repo_inventory: Optional[RepoInventory] = None
        provider_source_identity: Optional[Dict[str, Any]] = None
        trace_metadata = _trace_metadata(
            repo,
            pr_number,
            head_sha,
            default_branch=default_branch,
            dry_run=dry_run,
            run_id=run_id,
            pipeline_phase="context",
            pipeline_attempt=attempt,
        )

        if not dry_run and has_existing_llamapreview_review(pr_content):
            persistence.mark_error(
                repo,
                pr_number,
                "PENDING",
                "already reviewed by llamapreview[bot]",
                error_kind="duplicate_review",
                error_stage="context.ingest",
                retryable=False,
                attempt=attempt,
                extra_attrs={
                    "context_runtime_identity": context_runtime_identity
                },
                phase_claim=phase_claim,
                table=table,
            )
            return

        hard_skip_reason = pipeline_admission.hard_input_skip_reason(
            pr_content.get("file_changes")
        )
        if hard_skip_reason:
            context_lifecycle_checkpoint(
                stage="context.pre_terminal",
                allow_successor=False,
            )
            ci_gate_status = pipeline_publication.terminal_ci_gate_status(ci_snapshot)
            body = skipped_review_notice(hard_skip_reason)
            pipeline_publication.commit_terminal_result(
                context=context_publication,
                runtime=active_runtime,
                table=table,
                review_mode="skip",
                body=body,
                deadline=deadline,
                ci_snapshot=ci_snapshot,
                extra_attributes={
                    "skip_reason": hard_skip_reason,
                    "pr_details_chars": len(pr_details),
                    "ci_gate_status": ci_gate_status,
                    "ci_snapshot_source": ci_snapshot.get("source", "none"),
                    "visible_projection_source": "terminal_input_boundary",
                    **(
                        {
                            "provider_source_identity": (
                                provider_source_identity
                            ),
                            "provider_source_sha256": (
                                provider_source_identity["sha256"]
                            ),
                        }
                        if provider_source_identity is not None
                        else {}
                    ),
                },
            )
            return

        # Give every model-facing context stage the same typed exact-head CI
        # truth.  The source PR details retain
        # the historical commit-status field for compatibility, so the bounded
        # snapshot names that field precisely and aggregates typed checks
        # separately.  The head assertion above scopes this snapshot to the
        # queued revision; the review phase refreshes it again before generation.
        pr_details = prepared_source.model_pr_details
        if len(pr_details) > config.LARGE_PR_MAX_CHARS:
            raise ValueError(
                "bounded PR projection exceeded LARGE_PR_MAX_CHARS"
            )

        # Fetch the exact-head tree once before Route.  Route receives only a
        # bounded, content-free projection; PFR reuses the same inventory so
        # this does not add a duplicate tree call for normal/high reviews.
        # Exact path-state events are shared with downstream evidence rather
        # than becoming Route-only hidden facts.
        if repo_inventory is None:
            repo_inventory = fetch_repo_inventory(
                repo,
                token=token,
                sha=head_sha,
                deadline=deadline,
            )
        if provider_source_identity is None:
            provider_source_identity = build_provider_source_identity(
                prepared_source,
                repo_inventory,
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                default_branch=str(default_branch),
            )

        # Route remains the semantic owner. Code only enforces objective input
        # coverage and selectively asks the stronger model to adjudicate a
        # provisional cheap route when the supplied evidence is not closed.
        context_client = context_client or DeepSeekClient(
            model=config.ANALYZER_MODEL,
            reasoning_effort=config.ANALYZER_EFFORT,
        )
        bind_provider_call_accounting(
            context_client,
            repo=repo,
            pr_number=pr_number,
            expected_status="PENDING",
            phase_claim=phase_claim,
            table=table,
        )
        analyzer_result = analyze_pr_complexity(
            pr_details,
            pr_content=pr_content,
            repo_inventory=repo_inventory,
            client=context_client,
            trace_metadata=trace_metadata,
            deadline=deadline,
            expected_route_input_sha256=provider_source_identity[
                "route_input_sha256"
            ],
        )
        review_mode = analyzer_result.get("complexity", "high")
        route_phases = route_model_phases(analyzer_result)
        if review_mode == "skip":
            reason = analyzer_result.get("reason") or "No route reason provided."
            public_reason = pipeline_publication.canonical_ai_skip_reason(analyzer_result)
            ci_gate_status = pipeline_publication.terminal_ci_gate_status(ci_snapshot)
            body = skipped_review_notice(public_reason)
            context_lifecycle_checkpoint(
                stage="context.pre_terminal",
                allow_successor=False,
            )
            accounting = provider_usage_accounting(
                provider_calls=provider_call_records(
                    repo=repo,
                    pr_number=pr_number,
                    table=table,
                    client=context_client,
                ),
                fallback_winning_phases=route_phases,
                context_attempt=attempt,
                review_attempt=None,
            )
            pipeline_publication.commit_terminal_result(
                context=context_publication,
                runtime=active_runtime,
                table=table,
                review_mode="skip",
                body=body,
                deadline=deadline,
                ci_snapshot=ci_snapshot,
                extra_attributes={
                    "skip_reason": reason,
                    "skip_public_reason": public_reason,
                    "analyzer_result": analyzer_result,
                    "provider_source_identity": provider_source_identity,
                    "provider_source_sha256": provider_source_identity[
                        "sha256"
                    ],
                    "pr_details_chars": len(pr_details),
                    "ci_gate_status": ci_gate_status,
                    "ci_snapshot_source": ci_snapshot.get("source", "none"),
                    "visible_projection_source": "terminal_policy",
                    # A terminal skip still consumed a Route provider call and
                    # still decided against an exact frozen delta. Both must be
                    # recoverable without replaying the model.
                    **accounting,
                    "route_delta_provenance": route_delta_provenance(
                        pr_content,
                        head_sha=head_sha,
                        analyzer_result=analyzer_result,
                    ),
                },
            )
            return
        if review_mode not in {"low", "normal", "high"}:
            review_mode = "high"

        context_text, meta = _context_for_mode(
            review_mode=review_mode,
            runtime=active_runtime,
            token=token,
            repo=repo,
            pr_number=pr_number,
            pr_content=pr_content,
            pr_details=pr_details,
            head_sha=head_sha,
            default_branch=default_branch,
            trace_metadata={**trace_metadata, "review_mode": review_mode},
            route_plan=analyzer_result,
            deadline=deadline,
            deepseek_client=context_client,
            table=table,
            repo_inventory=repo_inventory,
            initial_evidence_ledger=analyzer_result.get(
                "_route_preflight_evidence_ledger"
            ),
            before_first_reconcile=(
                lambda: context_lifecycle_checkpoint(
                    stage="context.pre_reconcile",
                    allow_successor=True,
                )
            ),
        )
        meta["analyzer_result"] = analyzer_result
        meta["provider_source_identity"] = provider_source_identity
        meta["route_model_phases"] = route_phases
        meta["route_delta_provenance"] = route_delta_provenance(
            pr_content,
            head_sha=head_sha,
            analyzer_result=analyzer_result,
        )
        fallback_context_phases = canonical_context_model_phases(
            review_mode,
            route_model_phases=route_phases,
            pfr_model_phases=(
                phase
                for phase in meta.get("pfr_model_phases") or []
                if isinstance(phase, dict)
            ),
        )
        meta.update(
            provider_usage_accounting(
                provider_calls=provider_call_records(
                    repo=repo,
                    pr_number=pr_number,
                    table=table,
                    client=context_client,
                ),
                fallback_winning_phases=fallback_context_phases,
                context_attempt=attempt,
                review_attempt=None,
            )
        )
        meta["changed_delta_focus"] = build_changed_delta_focus(pr_content)
        meta["pr_details_chars"] = source_pr_details_chars
        meta["model_pr_details_chars"] = len(pr_details)
        meta["pr_details_compacted"] = bool(
            ingest_meta.get("pr_details_compacted")
        )
        meta["ci_snapshot"] = ci_snapshot
        meta["evidence_catalog"] = build_review_evidence_catalog(
            pr_content,
            ci_snapshot,
            meta.get("evidence_ledger"),
        )
        meta["context_phase_elapsed_seconds"] = round(time.monotonic() - phase_started, 3)
        meta["deadline"] = deadline.snapshot()
        meta["effective_config"] = {
            "review_mode": review_mode,
            "analyzer_model": config.ANALYZER_MODEL,
            "pfr_model": config.PFR_NORMAL_MODEL if review_mode == "normal" else config.PFR_MODEL,
            "max_search_calls": config.PFR_NORMAL_MAX_SEARCH_CALLS if review_mode == "normal" else config.PFR_MAX_SEARCH_CALLS,
            "max_read_calls": config.PFR_NORMAL_MAX_READ_CALLS if review_mode == "normal" else config.PFR_MAX_READ_CALLS,
            "max_reconcile_rounds": config.PFR_MAX_RECONCILE_ROUNDS,
            "phase_limit_seconds": config.PIPELINE_CONTEXT_PHASE_MAX_SECONDS,
            "state_write_reserve_seconds": config.PIPELINE_STATE_WRITE_RESERVE_SECONDS,
        }
        lifecycle_terminal_attrs.update(
            {
                "pfr_reconcile_dispatches": deepcopy(
                    meta.get("pfr_reconcile_dispatches") or []
                ),
                "context_deadline": deepcopy(meta.get("deadline") or {}),
            }
        )
        context_lifecycle_checkpoint(
            stage="context.pre_persist",
            allow_successor=False,
        )

        stored, _attrs = persistence.store_context(
            repo,
            pr_number,
            context_text=context_text,
            pr_details_text=pr_details,
            meta=meta,
            expected_status="PENDING",
            review_mode=review_mode,
            extra_attrs={
                "analyzer_result": analyzer_result,
                "provider_source_identity": provider_source_identity,
                "provider_source_sha256": provider_source_identity[
                    "sha256"
                ],
                "pr_details_chars": len(pr_details),
                "deepseek_usage_total": meta["deepseek_usage_total"],
                "deepseek_winning_usage_total": meta[
                    "deepseek_winning_usage_total"
                ],
                "deepseek_discarded_usage_total": meta[
                    "deepseek_discarded_usage_total"
                ],
                "deepseek_usage_accounting": meta[
                    "deepseek_usage_accounting"
                ],
                "route_delta_provenance": meta[
                    "route_delta_provenance"
                ],
            },
            context_runtime_identity=context_runtime_identity,
            phase_claim=phase_claim,
            head_sha=head_sha,
            run_id=run_id,
            table=table,
        )
        reconcile_dispatches = [
            entry
            for entry in meta.get("pfr_reconcile_dispatches") or []
            if isinstance(entry, dict)
        ]
        reconcile_metric_fields: Dict[str, Any] = {
            "pfr_reconcile_dispatch_count": len(reconcile_dispatches)
        }
        for entry in reconcile_dispatches:
            round_number = int(entry.get("round") or 0)
            if round_number not in {1, 2}:
                continue
            reconcile_metric_fields.update(
                {
                    f"pfr_reconcile_{round_number}_deadline_remaining_seconds": entry.get(
                        "deadline_remaining_seconds"
                    ),
                    f"pfr_reconcile_{round_number}_elapsed_seconds": entry.get(
                        "elapsed_seconds"
                    ),
                }
            )
        _emit_pipeline_metric(
            "context_phase_complete",
            repo=repo,
            pr_number=pr_number,
            attempt=attempt,
            stored=stored,
            review_mode=review_mode,
            **reconcile_metric_fields,
            elapsed_seconds=round(time.monotonic() - phase_started, 3),
        )
    except _LifecycleStop:
        return
    except Exception as exc:
        if isinstance(exc, (HeadSuperseded, PRLifecycleSuperseded)):
            logger.info("Context phase superseded for %s#%s: %s", repo, pr_number, exc)
        else:
            logger.exception("Context phase failed for %s#%s", repo, pr_number)
        _handle_phase_failure(
            repo=repo,
            pr_number=pr_number,
            expected_status="PENDING",
            phase="context",
            attempt=attempt,
            exc=exc,
            terminal_attrs=pipeline_publication.failure_attributes(
                repo=repo,
                pr_number=pr_number,
                exc=exc,
                base={
                    "context_runtime_identity": (
                        context_runtime_identity
                    )
                },
                table=table,
            ),
            phase_claim=phase_claim,
            table=table,
        )
    finally:
        _close_owned_runtime(active_runtime, owned=runtime_owned)


def _persist_terminal_nonpublishable_review(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    run_id: str,
    review_mode: str,
    attempt: int,
    item: Dict[str, Any],
    review_json: Dict[str, Any],
    context_meta: Dict[str, Any],
    runtime: Any,
    table,
    phase_started: float,
    usage_accounting: Dict[str, Any],
    review_runtime_identity: Dict[str, Any],
    phase_claim: Dict[str, Any],
) -> bool:
    """Persist a terminal review failure, or raise its bounded retry."""

    if not result_artifact.is_nonpublishable(review_json):
        return False
    failure = ReviewGenerationIncomplete(
        "The review model did not produce a publishable review.",
        stage=str(review_json.get("review_failure_stage") or "review"),
        kind=str(
            review_json.get("review_failure_kind")
            or "review_generation_incomplete"
        ),
        retryable=bool(review_json.get("review_failure_retryable")),
    )
    if failure.retryable and attempt < int(config.MAX_ATTEMPTS):
        raise failure
    projected = result_artifact.build_nonpublishable_result(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        run_id=run_id,
        review_mode=review_mode,
        item=item,
        review_json=review_json,
        context_meta=context_meta,
        usage_accounting=usage_accounting,
        review_runtime_identity=review_runtime_identity,
        computed_at=persistence.iso_now(),
        elapsed_seconds=time.monotonic() - phase_started,
    )
    pipeline_admission.assert_current_head(
        runtime,
        repo,
        pr_number,
        head_sha,
        stage="review.failure_pre_persist",
    )
    stored = persistence.store_review_failure(
        repo,
        pr_number,
        expected_status="CONTEXT_READY",
        artifact=projected.artifact,
        error_kind=projected.failure_kind,
        error_stage=projected.failure_stage,
        retryable=projected.retryable,
        retry_exhausted=bool(
            projected.retryable
            and attempt >= int(config.MAX_ATTEMPTS)
        ),
        attempt=attempt,
        head_sha=head_sha,
        run_id=run_id,
        extra_attrs=projected.terminal_attributes,
        phase_claim=phase_claim,
        table=table,
    )
    if stored:
        _emit_terminal_error_metric(
            phase=projected.failure_stage,
            kind=projected.failure_kind,
        )
    _emit_pipeline_metric(
        "review_nonpublishable",
        repo=repo,
        pr_number=pr_number,
        attempt=attempt,
        stored=stored,
        review_mode=review_mode,
        failure_kind=projected.failure_kind,
        failure_stage=projected.failure_stage,
        retryable=projected.retryable,
    )
    return True


def run_review_phase(
    item: Dict[str, Any],
    *,
    table=None,
    runtime: Optional[Any] = None,
    deepseek_client=None,
    lambda_context=None,
    stream_event_id: str = "",
) -> None:
    repo = pipeline_admission.require_item_field(item, "repo")
    pr_number = int(pipeline_admission.require_item_field(item, "pr_number"))
    if (item.get("status") or "") != "CONTEXT_READY":
        return
    review_runtime_identity = capture_runtime_identity(
        lambda_context,
        phase="review",
    )
    admission = pipeline_admission.claim_phase_delivery(
        repo,
        pr_number,
        phase="review",
        expected_status="CONTEXT_READY",
        runtime_identity=review_runtime_identity,
        stream_event_id=stream_event_id,
        stream_head_sha=str(item.get("head_sha") or ""),
        stream_run_id=str(item.get("run_id") or ""),
        table=table,
    )
    if admission is None:
        return
    phase_claim = admission.phase_claim
    item_for_review = admission.current_item
    dry_run = pipeline_admission.effective_dry_run(item_for_review)
    context_runtime_identity = deepcopy(
        item_for_review.get("context_runtime_identity") or {}
    )
    attempt = admission.attempt
    deadline = Deadline.from_lambda_context(
        lambda_context,
        phase_limit_seconds=config.PIPELINE_REVIEW_PHASE_MAX_SECONDS,
    )
    phase_started = time.monotonic()
    runtime_owned = False
    active_runtime = runtime
    completed_review_phases: list[Dict[str, Any]] = []
    review_client = deepseek_client
    try:
        if (
            attempt > int(config.MAX_ATTEMPTS)
            and not isinstance(
                item_for_review.get("publication_intent"), dict
            )
        ):
            _mark_terminal_error(
                repo,
                pr_number,
                "CONTEXT_READY",
                "Review phase retry budget exhausted before execution",
                phase="review.start",
                kind="retry_budget_exhausted",
                retryable=False,
                retry_exhausted=True,
                attempt=attempt,
                extra_attrs={
                    "context_runtime_identity": context_runtime_identity,
                    "review_runtime_identity": review_runtime_identity,
                },
                phase_claim=phase_claim,
                table=table,
            )
            return
        installation_id = int(pipeline_admission.require_item_field(item_for_review, "installation_id"))
        head_sha = str(pipeline_admission.require_item_field(item_for_review, "head_sha"))
        run_id = pipeline_admission.run_id(item_for_review, repo, pr_number)
        token = pipeline_admission.installation_token(installation_id, deadline=deadline)
        if active_runtime is None:
            active_runtime = GitHubRuntime(token)
            runtime_owned = True
        review_publication = pipeline_publication.PublicationContext(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            expected_status="CONTEXT_READY",
            phase="review",
            run_id=run_id,
            generation_attempt=attempt,
            runtime_identity=review_runtime_identity,
            phase_claim=phase_claim,
            dry_run=dry_run,
        )
        if pipeline_publication.recover_pending(
            item_for_review,
            context=review_publication,
            runtime=active_runtime,
            deadline=deadline,
            lifecycle_unavailable_observer=(
                _observe_lifecycle_publication_unavailable
            ),
            table=table,
        ):
            return
        context_text, pr_details, context_meta = (
            persistence.load_context_bundle_from_item(item_for_review)
        )
        review_mode = (
            item_for_review.get("review_mode")
            or context_meta.get("review_mode")
            or "high"
        )
        model_profile = _review_model_for_mode(review_mode)

        def review_lifecycle_checkpoint(
            *,
            stage: str,
            final_publishable: bool,
        ) -> pipeline_admission.PRLifecycleDisposition:
            nonlocal item_for_review
            disposition = _verified_disposition(
                active_runtime,
                repo,
                pr_number,
                head_sha,
                stage=stage,
            )
            latest = persistence.get_item(
                repo,
                pr_number,
                table=table,
                consistent_read=True,
            ) or item_for_review
            has_admission = _has_exact_initial_admission(
                latest,
                head_sha=head_sha,
                run_id=run_id,
            )
            if (
                disposition.kind
                is pipeline_admission.PRDispositionKind.OPEN_SAME_HEAD
            ):
                if not has_admission:
                    recorded = persistence.record_initial_admission(
                        repo,
                        pr_number,
                        expected_status="CONTEXT_READY",
                        expected_head_sha=head_sha,
                        phase_claim=phase_claim,
                        table=table,
                    )
                    if not recorded:
                        raise RuntimeError(
                            "Review admission lost its exact phase owner"
                        )
                    item_for_review = persistence.get_item(
                        repo,
                        pr_number,
                        table=table,
                        consistent_read=True,
                    ) or item_for_review
                return disposition
            if (
                disposition.kind
                is pipeline_admission.PRDispositionKind.OPEN_NEW_HEAD
            ):
                accounting = _lifecycle_accounting(
                    repo=repo,
                    pr_number=pr_number,
                    table=table,
                    client=review_client,
                    context_attempt=int(
                        item_for_review.get("context_attempt") or 0
                    ),
                    review_attempt=attempt,
                )
                _mark_lifecycle_superseded(
                    disposition,
                    context=review_publication,
                    stage=stage,
                    accounting=accounting,
                    table=table,
                )
                raise _LifecycleStop()
            if disposition.ended:
                if (
                    disposition.same_head
                    and has_admission
                    and final_publishable
                    and disposition.merged
                ):
                    if disposition.locked is True:
                        accounting = _lifecycle_accounting(
                            repo=repo,
                            pr_number=pr_number,
                            table=table,
                            client=review_client,
                            context_attempt=int(
                                item_for_review.get("context_attempt") or 0
                            ),
                            review_attempt=attempt,
                        )
                        _mark_lifecycle_publication_unavailable(
                            disposition,
                            context=review_publication,
                            publication_kind="post_merge_follow_up",
                            accounting=accounting,
                            table=table,
                            extra_attributes={
                                "context_runtime_identity": (
                                    context_runtime_identity
                                ),
                                "review_runtime_identity": (
                                    review_runtime_identity
                                ),
                                "review_generation_status": "complete",
                            },
                        )
                        raise _LifecycleStop()
                    return disposition
                accounting = _lifecycle_accounting(
                    repo=repo,
                    pr_number=pr_number,
                    table=table,
                    client=review_client,
                    context_attempt=int(
                        item_for_review.get("context_attempt") or 0
                    ),
                    review_attempt=attempt,
                )
                if disposition.same_head and has_admission:
                    if disposition.locked is True:
                        _mark_lifecycle_publication_unavailable(
                            disposition,
                            context=review_publication,
                            publication_kind="lifecycle_cancellation",
                            accounting=accounting,
                            table=table,
                            extra_attributes={
                                "context_runtime_identity": (
                                    context_runtime_identity
                                ),
                                "review_runtime_identity": (
                                    review_runtime_identity
                                ),
                                "review_generation_status": "cancelled",
                            },
                        )
                    else:
                        _commit_lifecycle_cancellation(
                            disposition,
                            context=review_publication,
                            runtime=active_runtime,
                            deadline=deadline,
                            accounting=accounting,
                            table=table,
                            extra_attributes={
                                "context_runtime_identity": (
                                    context_runtime_identity
                                ),
                                "review_runtime_identity": (
                                    review_runtime_identity
                                ),
                            },
                        )
                else:
                    _mark_lifecycle_superseded(
                        disposition,
                        context=review_publication,
                        stage=stage,
                        accounting=accounting,
                        table=table,
                    )
                raise _LifecycleStop()
            raise HeadVerificationUnavailable(
                "Pull request lifecycle could not be verified",
                stage=stage,
            )

        if attempt > int(config.MAX_ATTEMPTS):
            _mark_terminal_error(
                repo,
                pr_number,
                "CONTEXT_READY",
                "Review phase retry budget exhausted before execution",
                phase="review.start",
                kind="retry_budget_exhausted",
                retryable=False,
                retry_exhausted=True,
                attempt=attempt,
                extra_attrs={
                    "context_runtime_identity": context_runtime_identity,
                    "review_runtime_identity": review_runtime_identity,
                },
                phase_claim=phase_claim,
                table=table,
            )
            return
        review_lifecycle_checkpoint(
            stage="review.start",
            final_publishable=False,
        )
        # A live retry may follow a successful GitHub create_review whose
        # terminal DynamoDB write failed. Observe GitHub before spending any
        # more model or placement work; the ordinary pre-publish check remains
        # the last-moment guard for the first attempt and concurrent writers.
        if not dry_run and attempt > 1:
            retry_pr_content, _ = fetch_pr_details(
                active_runtime,
                repo,
                pr_number,
            )
            review_lifecycle_checkpoint(
                stage="review.retry_duplicate_guard",
                final_publishable=False,
            )
            if has_existing_llamapreview_review(retry_pr_content):
                persistence.mark_error(
                    repo,
                    pr_number,
                    "CONTEXT_READY",
                    "already reviewed by llamapreview[bot]",
                    error_kind="duplicate_review",
                    error_stage="review.retry_duplicate_guard",
                    retryable=False,
                    attempt=attempt,
                    extra_attrs={
                        "context_runtime_identity": (
                            context_runtime_identity
                        ),
                        "review_runtime_identity": (
                            review_runtime_identity
                        ),
                    },
                    phase_claim=phase_claim,
                    table=table,
                )
                return
        pr_details, context_meta = refresh_review_ci_context(
            active_runtime,
            repo,
            head_sha,
            pr_details,
            context_meta,
            stage="review.ci_before_generation",
        )
        context_meta["ci_generation_snapshot"] = deepcopy(
            context_meta.get("ci_snapshot")
        )
        context_meta["ci_generation_model_payload"] = (
            model_ci_snapshot_payload(context_meta["ci_snapshot"])
        )
        context_meta["ci_snapshot_changed_after_generation"] = False
        context_meta["ci_changed_evidence_refs"] = []
        generation_context_meta = deepcopy(context_meta)
        trace_metadata = _trace_metadata(
            repo,
            pr_number,
            head_sha,
            review_mode=review_mode,
            dry_run=dry_run,
            run_id=run_id,
            pipeline_phase="review",
            pipeline_attempt=attempt,
        )
        review_client = deepseek_client or DeepSeekClient(
            model=model_profile["model"],
            reasoning_effort=model_profile["reasoning_effort"],
        )
        bind_provider_call_accounting(
            review_client,
            repo=repo,
            pr_number=pr_number,
            expected_status="CONTEXT_READY",
            phase_claim=phase_claim,
            table=table,
        )
        review_json = generate_review(
            pr_details,
            context_text,
            client=review_client,
            trace_metadata=trace_metadata,
            context_meta=context_meta,
            deadline=deadline,
            phase_sink=completed_review_phases,
            before_final=(
                lambda: review_lifecycle_checkpoint(
                    stage="review.before_final",
                    final_publishable=False,
                )
            ),
            **model_profile,
        )
        fallback_context_phases = [
            dict(phase)
            for phase in context_meta.get("deepseek_model_phases") or []
            if isinstance(phase, dict)
        ]
        if not fallback_context_phases:
            fallback_context_phases = canonical_context_model_phases(
                review_mode,
                route_model_phases=(
                    phase
                    for phase in context_meta.get("route_model_phases") or []
                    if isinstance(phase, dict)
                ),
                pfr_model_phases=(
                    phase
                    for phase in context_meta.get("pfr_model_phases") or []
                    if isinstance(phase, dict)
                ),
            )
        fallback_review_phases = [
            dict(phase)
            for phase in review_json.get("review_model_phases") or []
            if isinstance(phase, dict)
        ]
        usage_accounting = provider_usage_accounting(
            provider_calls=provider_call_records(
                repo=repo,
                pr_number=pr_number,
                table=table,
                client=review_client,
            ),
            fallback_winning_phases=[
                *fallback_context_phases,
                *fallback_review_phases,
            ],
            context_attempt=int(item_for_review.get("context_attempt") or 0),
            review_attempt=attempt,
        )
        final_disposition = review_lifecycle_checkpoint(
            stage="review.after_final",
            final_publishable=(
                not result_artifact.is_nonpublishable(review_json)
            ),
        )
        publication_kind = (
            "post_merge_follow_up"
            if final_disposition.kind
            is pipeline_admission.PRDispositionKind.MERGED_SAME_HEAD
            else "ordinary_review"
        )
        if _persist_terminal_nonpublishable_review(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            run_id=run_id,
            review_mode=review_mode,
            attempt=attempt,
            item=item_for_review,
            review_json=review_json,
            context_meta=context_meta,
            runtime=active_runtime,
            table=table,
            phase_started=phase_started,
            usage_accounting=usage_accounting,
            review_runtime_identity=review_runtime_identity,
            phase_claim=phase_claim,
        ):
            return
        if not review_json.get("review_fallback_used"):
            if deadline.remaining_seconds() > 5.0:
                final_disposition = review_lifecycle_checkpoint(
                    stage="review.ci_before_finalize",
                    final_publishable=True,
                )
                publication_kind = (
                    "post_merge_follow_up"
                    if final_disposition.kind
                    is pipeline_admission.PRDispositionKind.MERGED_SAME_HEAD
                    else "ordinary_review"
                )
                pr_details, context_meta = refresh_review_ci_context(
                    active_runtime,
                    repo,
                    head_sha,
                    pr_details,
                    context_meta,
                    stage="review.ci_before_finalize",
                )
                review_json = reapply_latest_ci_guard(
                    review_json,
                    pr_details,
                    context_meta,
                    generation_context_meta=generation_context_meta,
                )
            elif not dry_run:
                raise DeadlineExceeded(
                    "review.ci_before_finalize",
                    remaining_seconds=deadline.remaining_seconds(),
                )
            else:
                review_json["quality_scoreable"] = False
                exclusions = list(review_json.get("quality_exclusion_reasons") or [])
                exclusions.append("ci_finalize_refresh_skipped_deadline")
                review_json["quality_exclusion_reasons"] = list(dict.fromkeys(exclusions))
                warnings = list(review_json.get("review_quality_warnings") or [])
                warnings.append("ci_finalize_refresh_skipped_deadline")
                review_json["review_quality_warnings"] = list(dict.fromkeys(warnings))
                context_meta["ci_finalize_refresh_skipped"] = "deadline"
        if _persist_terminal_nonpublishable_review(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            run_id=run_id,
            review_mode=review_mode,
            attempt=attempt,
            item=item_for_review,
            review_json=review_json,
            context_meta=context_meta,
            runtime=active_runtime,
            table=table,
            phase_started=phase_started,
            usage_accounting=usage_accounting,
            review_runtime_identity=review_runtime_identity,
            phase_claim=phase_claim,
        ):
            return
        inline_target_paths = {
            str(item.get("file_path") or "")
            for item in review_json.get("inline_comments") or []
            if isinstance(item, dict) and str(item.get("file_path") or "").strip()
        }
        if inline_target_paths:
            final_disposition = review_lifecycle_checkpoint(
                stage="review.generated",
                final_publishable=True,
            )
            publication_kind = (
                "post_merge_follow_up"
                if final_disposition.kind
                is pipeline_admission.PRDispositionKind.MERGED_SAME_HEAD
                else "ordinary_review"
            )
        _repo_obj, files, file_contents, placement_fetch = _fetch_pr_files_and_contents(
            active_runtime,
            repo,
            pr_number,
            head_sha,
            target_paths=inline_target_paths,
            deadline=deadline,
        )
        diff_maps = build_diff_maps_from_pr_files(files)
        final_disposition = review_lifecycle_checkpoint(
            stage="review.pre_publish",
            final_publishable=True,
        )
        publication_kind = (
            "post_merge_follow_up"
            if final_disposition.kind
            is pipeline_admission.PRDispositionKind.MERGED_SAME_HEAD
            else "ordinary_review"
        )
        prepared = prepare_review_publication(
            review_json,
            head_sha=head_sha,
            diff_maps=diff_maps,
            file_contents=file_contents,
            publication_kind=publication_kind,
        )
        projected = result_artifact.build_publishable_result(
            prepared,
            review_json=review_json,
            context_meta=context_meta,
            usage_accounting=usage_accounting,
            fallback_review_phases=fallback_review_phases,
            item=item_for_review,
            review_mode=review_mode,
            placement_fetch=placement_fetch,
            context_runtime_identity=context_runtime_identity,
            review_runtime_identity=review_runtime_identity,
            run_id=run_id,
            attempt=attempt,
            elapsed_seconds=time.monotonic() - phase_started,
        )
        prepared = projected.prepared
        generation_fields = projected.generation_fields
        review_publication_for_commit = _publication_context_for_lifecycle(
            review_publication,
            publication_kind=prepared.publication_kind,
            required_disposition=prepared.required_disposition,
        )
        unavailable_observed = {"value": False}

        def observe_unavailable(fields: Mapping[str, Any]) -> None:
            unavailable_observed["value"] = True
            _observe_lifecycle_publication_unavailable(fields)

        stored = pipeline_publication.commit_prepared(
            prepared,
            projected.terminal_attributes,
            context=review_publication_for_commit,
            runtime=active_runtime,
            deadline=deadline,
            pre_persist_stage="review.pre_persist",
            lifecycle_unavailable_observer=observe_unavailable,
            table=table,
        )
        if unavailable_observed["value"]:
            return
        accounting_status = projected.terminal_attributes.get(
            "deepseek_usage_accounting"
        )
        if not isinstance(accounting_status, dict):
            accounting_status = {}
        _emit_pipeline_metric(
            "review_phase_complete",
            repo=repo,
            pr_number=pr_number,
            attempt=attempt,
            stored=stored,
            review_mode=review_mode,
            publication_kind=prepared.publication_kind,
            generation_status=generation_fields.get("review_generation_status"),
            failure_kind=generation_fields.get("review_failure_kind"),
            quality_scoreable=generation_fields.get("quality_scoreable"),
            provider_accounting_complete=bool(
                accounting_status.get("complete_numeric_usage")
            ),
            unreported_usage_call_count=int(
                accounting_status.get("unreported_usage_call_count") or 0
            ),
            elapsed_seconds=round(time.monotonic() - phase_started, 3),
        )
    except _LifecycleStop:
        return
    except Exception as exc:
        if isinstance(exc, (HeadSuperseded, PRLifecycleSuperseded)):
            logger.info("Review phase superseded for %s#%s: %s", repo, pr_number, exc)
        else:
            logger.exception("Review phase failed for %s#%s", repo, pr_number)
        # A retried attempt discards its review, never its cost. Emit the
        # provider usage this attempt already consumed so end-to-end
        # accounting stays complete even though only the published attempt
        # reaches the immutable artifact.
        recorded_attempt_phases = [
            phase
            for phase in provider_call_records(
                repo=repo,
                pr_number=pr_number,
                table=table,
                client=review_client,
            )
            if phase.get("pipeline_phase") == "review"
            and int(phase.get("pipeline_attempt") or 0) == int(attempt)
        ]
        if not pipeline_publication.generation_is_pending(
            repo=repo,
            pr_number=pr_number,
            phase="review",
            generation_attempt=attempt,
            stream_event_id=stream_event_id,
            table=table,
        ):
            emit_discarded_attempt_usage(
                repo=repo,
                pr_number=pr_number,
                attempt=attempt,
                phases=(
                    recorded_attempt_phases or completed_review_phases
                ),
            )
        failure_base = {
            "context_runtime_identity": context_runtime_identity,
            "review_runtime_identity": review_runtime_identity,
        }
        if isinstance(exc, (HeadSuperseded, PRLifecycleSuperseded)):
            all_provider_calls = provider_call_records(
                repo=repo,
                pr_number=pr_number,
                table=table,
                client=review_client,
            )
            if all_provider_calls:
                failure_base.update(
                    provider_usage_accounting(
                        provider_calls=all_provider_calls,
                        fallback_winning_phases=[],
                        context_attempt=int(
                            item_for_review.get("context_attempt") or 0
                        ),
                        review_attempt=None,
                    )
                )
        _handle_phase_failure(
            repo=repo,
            pr_number=pr_number,
            expected_status="CONTEXT_READY",
            phase="review",
            attempt=attempt,
            exc=exc,
            terminal_attrs=pipeline_publication.failure_attributes(
                repo=repo,
                pr_number=pr_number,
                exc=exc,
                base=failure_base,
                table=table,
            ),
            phase_claim=phase_claim,
            table=table,
        )
    finally:
        _close_owned_runtime(active_runtime, owned=runtime_owned)

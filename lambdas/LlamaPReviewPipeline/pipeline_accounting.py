"""Canonical provider-call and usage accounting for the pipeline.

This capability owns one content-free ledger across Route, PFR, Deep, and
Final. It can still read previously persisted phase names so durable recovery
records remain reconcilable, but it has no review-judgment or publication
responsibility.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Iterable, Optional, Sequence

from . import persistence
from .deepseek_client import (
    ProviderDispatchOutcomeUnknown,
    canonical_provider_phase,
)
from .provider_usage import (
    merge_numeric_usage,
    merge_numeric_usage_with_diagnostics,
    validate_complete_token_usage,
)
from .provider_accounting import reconcile_provider_accounting


logger = logging.getLogger(__name__)

__all__ = [
    "bind_provider_call_accounting",
    "canonical_context_model_phases",
    "dedupe_model_phases",
    "emit_discarded_attempt_usage",
    "provider_call_records",
    "provider_usage_accounting",
    "reconcile_provider_accounting",
    "route_delta_provenance",
    "route_model_phases",
    "sort_model_phases",
]

_PHASE_ORDER = {
    "route": 0,
    "route_adjudication": 1,
    "pfr_plan": 2,
    "pfr_reconcile": 3,
    "pfr_reconcile_representation_repair": 4,
    "deep_thinking": 5,
    "deep_judgment": 6,
    "final_output": 7,
    "final_presentation": 8,
    "final_presentation_repair": 9,
}


def route_delta_provenance(
    pr_content: Optional[Dict[str, Any]],
    *,
    head_sha: str,
    analyzer_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Bind Route accounting to its immutable compare and canonical input."""

    paths = sorted(
        {
            str(change.get("file_path") or "").strip()
            for change in (pr_content or {}).get("file_changes") or []
            if isinstance(change, dict)
            and str(change.get("file_path") or "").strip()
        }
    )
    metadata = (
        (pr_content or {}).get("pr_metadata")
        if isinstance((pr_content or {}).get("pr_metadata"), dict)
        else {}
    )
    base_sha = str((metadata or {}).get("base_sha") or "")
    effective_head = str(
        (metadata or {}).get("head_sha") or head_sha or ""
    )
    route_meta = (
        (analyzer_result or {}).get("_route_plan_meta")
        if isinstance(
            (analyzer_result or {}).get("_route_plan_meta"),
            dict,
        )
        else {}
    )
    compare_identity = (
        hashlib.sha256(
            f"{base_sha}\n{effective_head}".encode("utf-8")
        ).hexdigest()
        if base_sha and effective_head
        else ""
    )
    return {
        "base_sha": base_sha,
        "head_sha": effective_head,
        "compare_identity_sha256": compare_identity,
        "route_input_sha256": str(
            (route_meta or {}).get("route_input_sha256") or ""
        ),
        "route_input_schema": str(
            (route_meta or {}).get("route_input_schema") or ""
        ),
        "route_input_truncation": dict(
            (route_meta or {}).get("digest_truncation") or {}
        ),
        "changed_path_count": len(paths),
        "changed_path_set_digest": hashlib.sha256(
            "\n".join(paths).encode("utf-8", "replace")
        ).hexdigest(),
    }


def _emit_pipeline_metric(name: str, **fields: Any) -> None:
    logger.info(
        "Pipeline metric: %s",
        json.dumps(
            {"metric": name, **fields},
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        ),
    )


def route_model_phases(
    analyzer_result: Optional[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """Return the Route provider phases recorded by the analyzer."""

    plan_meta = (analyzer_result or {}).get("_route_plan_meta") or {}
    phases = [
        {
            **dict(phase),
            "phase": canonical_provider_phase(phase.get("phase")),
        }
        for phase in plan_meta.get("model_phases") or []
        if isinstance(phase, dict)
    ]
    return dedupe_model_phases(phases)


def dedupe_model_phases(
    phases: Iterable[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """Deduplicate provider operations without merging distinct attempts."""

    retained: list[Dict[str, Any]] = []
    retained_index: Dict[str, int] = {}
    for raw in phases:
        if not isinstance(raw, dict):
            continue
        phase = {
            **dict(raw),
            "phase": canonical_provider_phase(raw.get("phase")),
        }
        call_id = str(phase.get("call_id") or "").strip()
        identity = (
            f"call:{call_id}"
            if call_id
            else "legacy:"
            + hashlib.sha256(
                json.dumps(
                    {
                        "phase": phase.get("phase"),
                        "model": phase.get("model"),
                        "logical_model": phase.get("logical_model"),
                        "billed_model": phase.get("billed_model"),
                        "attempt": phase.get("attempt"),
                        "pipeline_phase": phase.get("pipeline_phase"),
                        "pipeline_attempt": phase.get(
                            "pipeline_attempt"
                        ),
                        "call_index": phase.get("call_index"),
                        "elapsed_seconds": phase.get(
                            "elapsed_seconds"
                        ),
                        "finish_reason": phase.get("finish_reason"),
                        "usage": phase.get("usage"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
        )
        existing_index = retained_index.get(identity)
        if existing_index is None:
            retained_index[identity] = len(retained)
            retained.append(phase)
            continue
        existing = retained[existing_index]
        if (
            str(existing.get("status") or "") == "dispatching"
            and str(phase.get("status") or "") != "dispatching"
        ):
            # A local terminal result can be the only complete accounting fact
            # after its durable fence finalization fails.  Prefer that terminal
            # record over the same call's durable dispatching skeleton without
            # merging or inventing any usage.
            retained[existing_index] = phase
    return retained


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def sort_model_phases(
    phases: Iterable[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """Return canonical execution order for a content-free call ledger."""

    return sorted(
        (dict(item) for item in phases if isinstance(item, dict)),
        key=lambda item: (
            (
                0
                if item.get("pipeline_phase") == "context"
                else 1
                if item.get("pipeline_phase") == "review"
                else 2
            ),
            _integer(item.get("pipeline_attempt")),
            _PHASE_ORDER.get(str(item.get("phase") or ""), 99),
            _integer(item.get("call_index") or item.get("round")),
            _integer(item.get("transport_attempt_index")),
            _integer(item.get("attempt")),
            str(item.get("call_id") or ""),
        ),
    )


def canonical_context_model_phases(
    review_mode: str,
    *,
    route_model_phases: Iterable[Dict[str, Any]],
    pfr_model_phases: Iterable[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """Select one Route lineage for one successful context attempt."""

    route = dedupe_model_phases(route_model_phases)
    pfr = dedupe_model_phases(pfr_model_phases)
    if review_mode not in {"normal", "high"}:
        return route
    if any(item.get("phase") == "route" for item in pfr):
        return pfr
    return dedupe_model_phases([*route, *pfr])


def bind_provider_call_accounting(
    client: Any,
    *,
    repo: str,
    pr_number: int,
    expected_status: str,
    phase_claim: Optional[Dict[str, Any]] = None,
    table: Any,
) -> None:
    """Persist every completed provider operation at response boundary."""

    final_setter = getattr(client, "set_provider_call_sink", None)
    fence_setter = getattr(
        client,
        "set_provider_dispatch_fence_sink",
        None,
    )
    if not callable(final_setter):
        return
    if not callable(fence_setter):
        raise TypeError(
            "Provider client exposes a terminal ledger sink without the "
            "required pre-dispatch fence sink"
        )

    def persist_fence(record: Dict[str, Any]) -> None:
        try:
            stored = persistence.begin_provider_call_dispatch(
                repo,
                pr_number,
                expected_status=expected_status,
                record=record,
                phase_claim=phase_claim,
                table=table,
            )
        except persistence.ProviderDispatchFenceUnresolved as exc:
            raise ProviderDispatchOutcomeUnknown(
                "A prior provider dispatch has no durable terminal outcome",
                provider_call_record=exc.record,
            ) from exc
        if not stored:
            raise RuntimeError(
                "Provider-call dispatch fence rejected the active phase owner"
            )

    def persist(record: Dict[str, Any]) -> None:
        stored = persistence.record_provider_call(
            repo,
            pr_number,
            expected_status=expected_status,
            record=record,
            phase_claim=phase_claim,
            table=table,
        )
        if not stored:
            raise RuntimeError(
                "Provider-call ledger rejected the active phase owner"
            )

    fence_setter(persist_fence)
    final_setter(persist)


def provider_call_records(
    *,
    repo: str,
    pr_number: int,
    table: Any,
    client: Any = None,
) -> list[Dict[str, Any]]:
    """Merge durable and process-local provider records by stable call id."""

    latest = persistence.get_item(
        repo,
        pr_number,
        table=table,
        consistent_read=True,
    )
    records = persistence.provider_call_records(latest)
    getter = getattr(client, "provider_call_records", None)
    if callable(getter):
        records.extend(
            item for item in getter() if isinstance(item, dict)
        )
    return dedupe_model_phases(records)


def provider_usage_accounting(
    *,
    provider_calls: Iterable[Dict[str, Any]],
    fallback_winning_phases: Iterable[Dict[str, Any]],
    context_attempt: int,
    review_attempt: Optional[int],
) -> Dict[str, Any]:
    """Project winning, discarded, and all-attempt usage from one ledger."""

    all_calls = dedupe_model_phases(provider_calls)
    fallback = dedupe_model_phases(fallback_winning_phases)
    if all_calls:
        unmatched = set(range(len(all_calls)))

        def coordinates(
            call: Dict[str, Any],
        ) -> tuple[str, int, str, str]:
            return (
                str(call.get("pipeline_phase") or ""),
                _integer(call.get("pipeline_attempt")),
                str(call.get("phase") or ""),
                str(call.get("model") or ""),
            )

        supplemental: list[Dict[str, Any]] = []
        for raw in fallback:
            call = dict(raw)
            phase = canonical_provider_phase(call.get("phase"))
            pipeline_phase = str(call.get("pipeline_phase") or "")
            if not pipeline_phase:
                pipeline_phase = (
                    "review"
                    if phase
                    in {
                        "deep_thinking",
                        "deep_judgment",
                        "final_output",
                        "final_presentation",
                        "final_presentation_repair",
                    }
                    else "context"
                )
            pipeline_attempt = _integer(
                call.get("pipeline_attempt")
            )
            if pipeline_attempt <= 0:
                pipeline_attempt = (
                    int(review_attempt or 0)
                    if pipeline_phase == "review"
                    else int(context_attempt or 0)
                )
            call.update(
                {
                    "phase": phase,
                    "pipeline_phase": pipeline_phase,
                    "pipeline_attempt": pipeline_attempt,
                }
            )
            call.setdefault(
                "usage_state",
                "reported" if call.get("usage") else "unreported",
            )
            call_id = str(call.get("call_id") or "").strip()
            if call_id:
                exact = next(
                    (
                        index
                        for index in unmatched
                        if str(
                            all_calls[index].get("call_id") or ""
                        ).strip()
                        == call_id
                    ),
                    None,
                )
                if exact is not None:
                    unmatched.remove(exact)
                else:
                    supplemental.append(call)
                continue
            candidates = [
                index
                for index in unmatched
                if coordinates(all_calls[index]) == coordinates(call)
            ]
            ordinal = _integer(
                call.get("call_index") or call.get("round")
            )
            if ordinal:
                exact_ordinal = [
                    index
                    for index in candidates
                    if _integer(
                        all_calls[index].get("call_index")
                    )
                    == ordinal
                ]
                if exact_ordinal:
                    unmatched.remove(exact_ordinal[0])
                else:
                    supplemental.append(call)
                continue
            if candidates:
                unmatched.remove(
                    min(
                        candidates,
                        key=lambda index: (
                            _integer(
                                all_calls[index].get("call_index")
                            ),
                            index,
                        ),
                    )
                )
            else:
                supplemental.append(call)
        all_calls = sort_model_phases(
            dedupe_model_phases([*all_calls, *supplemental])
        )
        winning: list[Dict[str, Any]] = []
        discarded: list[Dict[str, Any]] = []
        for call in all_calls:
            phase = str(call.get("pipeline_phase") or "")
            attempt = _integer(call.get("pipeline_attempt"))
            is_winning = (
                phase == "context"
                and attempt == int(context_attempt or 0)
            ) or (
                phase == "review"
                and review_attempt is not None
                and attempt == int(review_attempt)
            )
            (winning if is_winning else discarded).append(call)
    else:
        winning = sort_model_phases(fallback)
        discarded = []
        all_calls = list(winning)

    for item in all_calls:
        usage, validation_errors = validate_complete_token_usage(
            item.get("usage")
            if isinstance(item.get("usage"), dict)
            else None
        )
        declared = str(item.get("usage_state") or "").strip().lower()
        item["usage"] = usage
        item["usage_state"] = (
            "reported"
            if not validation_errors
            and declared in {"", "reported"}
            else "unreported"
        )
        if validation_errors:
            item["usage_validation_errors"] = validation_errors
    unreported = [
        item
        for item in all_calls
        if str(item.get("usage_state") or "reported") != "reported"
    ]
    total_usage, total_conflicts = merge_numeric_usage_with_diagnostics(
        *(phase.get("usage") for phase in all_calls)
    )
    winning_usage, winning_conflicts = (
        merge_numeric_usage_with_diagnostics(
            *(phase.get("usage") for phase in winning)
        )
    )
    discarded_usage, discarded_conflicts = (
        merge_numeric_usage_with_diagnostics(
            *(phase.get("usage") for phase in discarded)
        )
    )
    merge_conflicts = sorted(
        set(
            total_conflicts
            + winning_conflicts
            + discarded_conflicts
        )
    )
    return {
        "deepseek_model_phases": winning,
        "deepseek_discarded_model_phases": discarded,
        "deepseek_all_attempt_model_phases": all_calls,
        "deepseek_usage_total": total_usage,
        "deepseek_winning_usage_total": winning_usage,
        "deepseek_discarded_usage_total": discarded_usage,
        "deepseek_usage_accounting": {
            "schema_version": 2,
            "all_call_count": len(all_calls),
            "transport_operation_count": len(
                {
                    str(item.get("operation_id") or item.get("call_id") or "")
                    for item in all_calls
                    if str(
                        item.get("operation_id")
                        or item.get("call_id")
                        or ""
                    )
                }
            ),
            "winning_call_count": len(winning),
            "discarded_call_count": len(discarded),
            "unreported_usage_call_count": len(unreported),
            "usage_merge_conflicts": merge_conflicts,
            "complete_numeric_usage": (
                not unreported and not merge_conflicts
            ),
        },
    }


def emit_discarded_attempt_usage(
    *,
    repo: str,
    pr_number: int,
    attempt: int,
    phases: Sequence[Dict[str, Any]],
) -> None:
    """Report content-free usage consumed by a discarded attempt."""

    completed = [
        phase for phase in phases if isinstance(phase, dict)
    ]
    if not completed:
        return
    usage = merge_numeric_usage(
        *(phase.get("usage") for phase in completed)
    )
    _emit_pipeline_metric(
        "discarded_attempt_usage",
        repo=repo,
        pr_number=pr_number,
        attempt=attempt,
        phase_count=len(completed),
        phases=",".join(
            str(phase.get("phase") or "") for phase in completed
        ),
        total_tokens=int(usage.get("total_tokens") or 0),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(
            usage.get("completion_tokens") or 0
        ),
        unreported_usage_call_count=sum(
            str(phase.get("usage_state") or "").strip().lower()
            != "reported"
            or bool(
                validate_complete_token_usage(
                    phase.get("usage")
                    if isinstance(phase.get("usage"), dict)
                    else None
                )[1]
            )
            for phase in completed
        ),
    )

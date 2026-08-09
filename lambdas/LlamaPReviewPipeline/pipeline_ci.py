"""Exact-head CI evidence refresh and presentation finalization.

This capability owns bounded model-facing CI packing, typed refresh lineage,
and safe recompilation of the fixed presentation after CI changes.  It never
creates an engineering judgment or a synthetic clear result.
"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Dict, Optional, Sequence

from . import persistence
from .context_engine.packing import (
    CURRENT_HEAD_CI_END,
    CURRENT_HEAD_CI_START,
)
from .errors import CIRefreshUnavailable, HeadSuperseded
from .review.evidence_contract import (
    build_ci_snapshot,
    build_review_evidence_catalog,
    order_ci_checks_for_model,
)
from .review.presentation import compile_presentation_v1


__all__ = [
    "MODEL_CI_SNAPSHOT_MAX_CHARS",
    "mark_ci_basis_change_nonpublishable",
    "model_ci_snapshot_payload",
    "reapply_latest_ci_guard",
    "refresh_review_ci_context",
    "with_current_ci_snapshot",
]


_CURRENT_CI_START = CURRENT_HEAD_CI_START
_CURRENT_CI_END = CURRENT_HEAD_CI_END
MODEL_CI_SNAPSHOT_MAX_CHARS = 64_000
_MODEL_CI_DETAIL_RESERVE_CHARS = 6_000


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def model_ci_snapshot_payload(
    ci_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """Pack CI truth and diagnostics into one globally bounded object."""

    source_checks = order_ci_checks_for_model(
        ci_snapshot.get("checks") or []
    )
    has_details = any(
        item.get("annotations")
        or (
            isinstance(item.get("output"), dict)
            and any(
                item["output"].get(key)
                for key in ("title", "summary", "text", "log_tail")
            )
        )
        for item in source_checks
    )
    base_max_chars = MODEL_CI_SNAPSHOT_MAX_CHARS - (
        _MODEL_CI_DETAIL_RESERVE_CHARS if has_details else 0
    )
    actionable_meta = ci_snapshot.get("actionable_detail_retrieval")
    if not isinstance(actionable_meta, dict):
        actionable_meta = {}
    retained_meta = {
        key: actionable_meta.get(key)
        for key in (
            "outcome",
            "attempted_check_count",
            "enriched_check_count",
            "unmatched_actionable_check_count",
            "annotation_count",
            "annotation_available_count",
            "annotation_omitted_count",
            "truncated_check_count",
            "actions_log_attempted_count",
            "actions_log_enriched_count",
            "actions_log_omitted_count",
            "error_count",
        )
        if actionable_meta.get(key) is not None
    }
    retained_meta.update(
        {
            "prompt_details_truncated": True,
            "prompt_detail_units_retained": 9999,
            "prompt_detail_units_omitted": 9999,
        }
    )
    payload: Dict[str, Any] = {
        "head_scope": "current queued head",
        "aggregate_classification": ci_snapshot.get(
            "aggregate_classification"
        ),
        "commit_status_state": ci_snapshot.get("commit_status_state"),
        "retrieval_outcome": ci_snapshot.get("retrieval_outcome"),
        "actionable_detail_retrieval": retained_meta,
        "model_snapshot_packing": {
            "max_chars": MODEL_CI_SNAPSHOT_MAX_CHARS,
            "detail_reserve_chars": (
                _MODEL_CI_DETAIL_RESERVE_CHARS if has_details else 0
            ),
            "total_check_count": len(source_checks),
            "included_check_count": 0,
            "omitted_check_count": len(source_checks),
            "available_detail_units": 0,
            "retained_detail_units": 0,
            "details_truncated": False,
        },
        "checks": [],
    }
    included_sources: list[Dict[str, Any]] = []
    for item in source_checks:
        rendered = {
            "identity": str(item.get("identity") or ""),
            "name": str(item.get("name") or "unknown")[:120],
            "status": str(item.get("status") or "")[:40],
            "conclusion": str(item.get("conclusion") or "")[:40],
            "classification": str(
                item.get("classification") or ""
            )[:40],
        }
        payload["checks"].append(rendered)
        packing = payload["model_snapshot_packing"]
        packing["included_check_count"] = len(payload["checks"])
        packing["omitted_check_count"] = (
            len(source_checks) - len(payload["checks"])
        )
        if len(_compact_json(payload)) > base_max_chars:
            payload["checks"].pop()
            packing["included_check_count"] = len(payload["checks"])
            packing["omitted_check_count"] = (
                len(source_checks) - len(payload["checks"])
            )
            break
        included_sources.append(item)

    detail_units: list[tuple[int, str, Any]] = []
    max_annotations = max(
        (
            len(item.get("annotations") or [])
            for item in included_sources
            if isinstance(item.get("annotations"), list)
        ),
        default=0,
    )
    for annotation_index in range(max_annotations):
        for check_index, item in enumerate(included_sources):
            annotations = (
                item.get("annotations")
                if isinstance(item.get("annotations"), list)
                else []
            )
            if (
                annotation_index < len(annotations)
                and isinstance(annotations[annotation_index], dict)
            ):
                detail_units.append(
                    (
                        check_index,
                        "annotation",
                        annotations[annotation_index],
                    )
                )
    for output_key in ("title", "summary", "text", "log_tail"):
        for check_index, item in enumerate(included_sources):
            output = (
                item.get("output")
                if isinstance(item.get("output"), dict)
                else {}
            )
            if output.get(output_key) not in (None, ""):
                detail_units.append(
                    (
                        check_index,
                        f"output:{output_key}",
                        output[output_key],
                    )
                )

    packing = payload["model_snapshot_packing"]
    packing["available_detail_units"] = len(detail_units)
    for check_index, kind, value in detail_units:
        target = payload["checks"][check_index]
        if kind == "annotation":
            target.setdefault("annotations", []).append(value)
        else:
            output_key = kind.split(":", 1)[1]
            target.setdefault("output", {})[output_key] = value
        packing["retained_detail_units"] += 1
        packing["details_truncated"] = (
            packing["retained_detail_units"] < len(detail_units)
        )
        if len(_compact_json(payload)) > MODEL_CI_SNAPSHOT_MAX_CHARS:
            packing["retained_detail_units"] -= 1
            if kind == "annotation":
                target["annotations"].pop()
                if not target["annotations"]:
                    target.pop("annotations")
            else:
                output_key = kind.split(":", 1)[1]
                target["output"].pop(output_key, None)
                if not target["output"]:
                    target.pop("output")
            packing["details_truncated"] = True
    packing["details_truncated"] = (
        packing["retained_detail_units"] < len(detail_units)
    )
    retained_meta["prompt_details_truncated"] = packing[
        "details_truncated"
    ]
    retained_meta["prompt_detail_units_retained"] = packing[
        "retained_detail_units"
    ]
    retained_meta["prompt_detail_units_omitted"] = max(
        0,
        len(detail_units) - packing["retained_detail_units"],
    )
    return payload


def with_current_ci_snapshot(
    pr_details: str,
    ci_snapshot: Dict[str, Any],
) -> str:
    """Replace any untrusted CI markers with one code-generated snapshot."""

    payload = _compact_json(model_ci_snapshot_payload(ci_snapshot))
    base = str(pr_details or "")
    base = re.sub(
        re.escape(_CURRENT_CI_START)
        + r".*?"
        + re.escape(_CURRENT_CI_END),
        "",
        base,
        flags=re.DOTALL,
    )
    base = (
        base.replace(_CURRENT_CI_START, "")
        .replace(_CURRENT_CI_END, "")
        .rstrip()
    )
    return (
        f"{base}\n\n{_CURRENT_CI_START}\n"
        f"{payload}\n{_CURRENT_CI_END}"
    ).strip()


def refresh_review_ci_context(
    runtime: Any,
    repo: str,
    head_sha: str,
    pr_details: str,
    context_meta: Dict[str, Any],
    *,
    stage: str,
) -> tuple[str, Dict[str, Any]]:
    """Fetch exact-head CI truth and freshly observed actionable details."""

    getter = getattr(runtime, "get_ci_results_for_head", None)
    if not callable(getter):
        raise CIRefreshUnavailable(
            "Runtime does not support exact-head CI refresh",
            stage=stage,
        )
    raw_ci = getter(
        repo,
        head_sha,
        include_actionable_details=True,
    )
    if not isinstance(raw_ci, dict):
        raise CIRefreshUnavailable(
            "Exact-head CI refresh returned an invalid payload",
            stage=stage,
        )
    sampled_head = str(raw_ci.get("head_sha") or head_sha)
    if sampled_head != head_sha:
        raise HeadSuperseded(head_sha, sampled_head, stage=stage)
    ci_snapshot = build_ci_snapshot(raw_ci)
    if ci_snapshot.get("retrieval_outcome") == "error":
        raise CIRefreshUnavailable(
            "Exact-head CI refresh failed for all CI evidence sources",
            stage=stage,
        )
    refreshed_meta = dict(context_meta or {})
    refreshed_meta["ci_actionable_detail_lineage"] = {
        "schema_version": 2,
        "source": "exact_head_refresh",
        "policy": "fresh_output_and_annotations",
        "outcome": "freshly_observed",
        "check_count": len(ci_snapshot.get("checks") or []),
    }
    existing_catalog = [
        item
        for item in refreshed_meta.get("evidence_catalog") or []
        if isinstance(item, dict) and item.get("source_type") != "ci"
    ]
    refreshed_meta["ci_snapshot"] = ci_snapshot
    refreshed_meta["evidence_catalog"] = (
        existing_catalog + build_review_evidence_catalog({}, ci_snapshot)
    )
    refreshed_meta["ci_refreshed_stage"] = stage
    refreshed_meta["ci_refreshed_at"] = persistence.iso_now()
    return (
        with_current_ci_snapshot(pr_details, ci_snapshot),
        refreshed_meta,
    )


def _ci_catalog_by_ref(
    context_meta: Optional[Dict[str, Any]],
) -> Dict[str, str]:
    snapshot = (context_meta or {}).get("ci_snapshot")
    checks = snapshot.get("checks") if isinstance(snapshot, dict) else None
    if isinstance(snapshot, dict) and isinstance(checks, list):
        # Compare exactly what the model was allowed to see, including bounded
        # outputs and annotations. A check name or prose fragment is never
        # interpreted; the canonical packed payload either matches byte for
        # byte or its evidence ref is treated as changed.
        packed_checks = {
            str(item.get("identity") or ""): item
            for item in model_ci_snapshot_payload(snapshot).get("checks") or []
            if isinstance(item, dict)
            and str(item.get("identity") or "")
        }
        exact = {
            f"ci:{identity}": _compact_json(
                {
                    "model_visible": identity in packed_checks,
                    "payload": packed_checks.get(identity)
                    or {
                        "identity": identity,
                        "status": str(item.get("status") or ""),
                        "classification": str(
                            item.get("classification") or ""
                        ),
                        "conclusion": str(item.get("conclusion") or ""),
                    },
                }
            )
            for item in checks
            if isinstance(item, dict)
            and (identity := str(item.get("identity") or ""))
        }
        if exact:
            return exact
    return {
        str(item.get("id")): str(item.get("outcome") or "")
        for item in (context_meta or {}).get("evidence_catalog") or []
        if isinstance(item, dict)
        and str(item.get("source_type") or "") == "ci"
        and str(item.get("id") or "").startswith("ci:")
    }


def _ci_catalog_changes(
    generation_context_meta: Optional[Dict[str, Any]],
    final_context_meta: Optional[Dict[str, Any]],
) -> list[str]:
    before = _ci_catalog_by_ref(generation_context_meta)
    after = _ci_catalog_by_ref(final_context_meta)
    return sorted(
        ref
        for ref in set(before) | set(after)
        if before.get(ref) != after.get(ref)
    )


def _blocking_ci_refs(
    context_meta: Optional[Dict[str, Any]],
) -> set[str]:
    """Return exact check identities classified as failures by CI packing."""

    snapshot = (context_meta or {}).get("ci_snapshot")
    if not isinstance(snapshot, dict):
        return set()
    raw_blocking = snapshot.get("blocking_checks")
    checks = (
        raw_blocking
        if isinstance(raw_blocking, list)
        else snapshot.get("checks")
    )
    return {
        f"ci:{identity}"
        for item in (checks if isinstance(checks, list) else [])
        if isinstance(item, dict)
        and str(item.get("classification") or "") == "failure"
        and (identity := str(item.get("identity") or ""))
    }


def mark_ci_basis_change_nonpublishable(
    review_json: Dict[str, Any],
    *,
    invalidated_item_ids: Sequence[str] = (),
    reason: str = "ci_deciding_evidence_changed_after_generation",
    retryable: bool = False,
    failure_class: str = "CIDecidingEvidenceChanged",
    failure_message: Optional[str] = None,
) -> Dict[str, Any]:
    """Retain a model result privately when current CI removed its basis."""

    guarded = deepcopy(review_json)
    guarded.update(
        {
            "review_publishable": False,
            "review_publication_safe": False,
            "review_generation_status": "incomplete",
            "review_nonpublish_reason": reason,
            "review_failure_retryable": retryable,
            "review_failure_kind": reason,
            "review_failure_stage": "ci_refresh",
            "review_failure_class": failure_class,
            "review_failure_message": failure_message
            or (
                "An exact-head CI refresh removed evidence required by "
                "the model presentation."
            ),
            "quality_scoreable": False,
        }
    )
    guarded["quality_exclusion_reasons"] = list(
        dict.fromkeys(
            [
                *list(
                    guarded.get("quality_exclusion_reasons") or []
                ),
                reason,
            ]
        )
    )
    item_ids = sorted(
        {
            str(item_id)
            for item_id in invalidated_item_ids
            if str(item_id)
        }
    )
    if item_ids:
        guarded["ci_evidence_invalidated_item_ids"] = item_ids
    return guarded


def reapply_latest_ci_guard(
    review_json: Dict[str, Any],
    pr_details: str,
    context_meta: Dict[str, Any],
    *,
    generation_context_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Recompile the fixed presentation against the latest exact-head CI."""

    changed_ci_refs = _ci_catalog_changes(
        generation_context_meta,
        context_meta,
    )
    context_meta["ci_generation_snapshot"] = (
        (generation_context_meta or {}).get("ci_snapshot")
    )
    context_meta["ci_generation_model_payload"] = deepcopy(
        (generation_context_meta or {}).get(
            "ci_generation_model_payload"
        )
    )
    context_meta["ci_snapshot_changed_after_generation"] = bool(
        changed_ci_refs
    )
    context_meta["ci_changed_evidence_refs"] = changed_ci_refs
    if not changed_ci_refs:
        return review_json
    presentation = review_json.get("presentation_v1")
    if not isinstance(presentation, dict):
        return mark_ci_basis_change_nonpublishable(
            review_json,
            reason="ci_refresh_requires_presentation_v1",
        )
    decision = presentation.get("decision")
    verdict = (
        str(decision.get("verdict") or "")
        if isinstance(decision, dict)
        else ""
    )
    newly_blocking_refs = sorted(
        _blocking_ci_refs(context_meta)
        - _blocking_ci_refs(generation_context_meta)
    )
    if newly_blocking_refs and verdict in {"clear", "verification_needed"}:
        # Do not turn a late CI failure into a deterministic blocker.  Hold
        # this stale presentation and let the existing bounded review retry
        # make the causal judgment with the stable exact-head CI snapshot.
        context_meta["ci_new_blocking_evidence_refs"] = newly_blocking_refs
        return mark_ci_basis_change_nonpublishable(
            review_json,
            reason="ci_blocking_evidence_changed_after_generation",
            retryable=True,
            failure_class="CINewBlockingEvidence",
            failure_message=(
                "An exact-head CI failure appeared after Deep judgment; "
                "the existing bounded review retry must judge it."
            ),
        )
    compiled = compile_presentation_v1(
        presentation,
        pr_details=pr_details,
        context_meta=context_meta,
        changed_ci_refs=changed_ci_refs,
    )
    if not compiled.publishable or not isinstance(
        compiled.review,
        dict,
    ):
        return mark_ci_basis_change_nonpublishable(
            review_json,
            reason=(
                "ci_refresh_"
                + str(
                    compiled.failure_kind
                    or "presentation_not_publishable"
                )
            ),
        )
    projected = deepcopy(compiled.review)
    projection_fields = {
        "pr_review_comment",
        "inline_comments",
        "review_quality_warnings",
        "presentation_v1",
        "v3_review",
        "visible_projection_source",
        "visible_verdict",
        "review_generation_status",
        "review_fallback_used",
        "review_publishable",
        "review_publication_safe",
    }
    projected.update(
        {
            key: value
            for key, value in review_json.items()
            if key not in projection_fields
        }
    )
    projected["review_presentation_normalizations"] = list(
        dict.fromkeys(
            [
                *list(
                    review_json.get(
                        "review_presentation_normalizations"
                    )
                    or []
                ),
                *compiled.normalizations,
            ]
        )
    )
    if compiled.safe_partial:
        projected["quality_scoreable"] = False
        projected["quality_exclusion_reasons"] = list(
            dict.fromkeys(
                [
                    *list(
                        review_json.get(
                            "quality_exclusion_reasons"
                        )
                        or []
                    ),
                    "ci_evidence_changed_after_generation",
                ]
            )
        )
    return projected

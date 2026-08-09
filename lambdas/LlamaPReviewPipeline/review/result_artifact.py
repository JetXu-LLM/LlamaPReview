"""Immutable private artifact projections for completed Review attempts.

The orchestrator owns retry and terminal decisions.  This module only projects
an already generated result and its accounting evidence into the exact private
artifact and DynamoDB compatibility fields that those decisions persist.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict

from .. import config
from ..deepseek_client import trace_s3_prefix
from .publish import (
    GITHUB_PUBLICATION_FIELDS,
    PreparedGitHubReview,
    is_publishable_review,
)


REVIEW_GENERATION_FIELDS = (
    "review_generation_status",
    "review_fallback_used",
    "review_publishable",
    "review_publication_safe",
    "review_nonpublish_reason",
    "review_failure_retryable",
    "review_failure_kind",
    "review_failure_stage",
    "review_failure_class",
    "review_failure_message",
    "review_model_finish_reason",
    "review_stage_finish_reasons",
    "review_timeout_phase",
    "review_presentation_version",
    "review_presentation_selected_phase",
    "review_presentation_normalizations",
    "review_presentation_safe_partial",
    "ci_evidence_invalidated_item_ids",
    "review_final_thinking",
    "review_final_reasoning_effort",
    "review_model_phases",
    "deepseek_usage_total",
    "visible_projection_source",
    "quality_scoreable",
    "quality_exclusion_reasons",
)


@dataclass(frozen=True, slots=True)
class NonpublishableReviewArtifact:
    artifact: Dict[str, Any]
    generation_fields: Dict[str, Any]
    terminal_attributes: Dict[str, Any]
    failure_kind: str
    failure_stage: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class PublishableReviewArtifact:
    prepared: PreparedGitHubReview
    generation_fields: Dict[str, Any]
    terminal_attributes: Dict[str, Any]


def generation_fields(review_json: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: deepcopy(review_json.get(key))
        for key in REVIEW_GENERATION_FIELDS
        if key in review_json
    }


def is_nonpublishable(review_json: Mapping[str, Any]) -> bool:
    """Accept only an explicit, safe, complete model-owned presentation."""

    return not is_publishable_review(review_json)


def _winning_review_phases(
    usage_accounting: Mapping[str, Any],
    fallback_review_phases: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    winning = [
        dict(phase)
        for phase in usage_accounting.get("deepseek_model_phases") or []
        if isinstance(phase, Mapping)
    ]
    selected = [
        dict(phase)
        for phase in winning
        if phase.get("pipeline_phase") == "review"
    ]
    return selected or [dict(phase) for phase in fallback_review_phases]


def build_nonpublishable_result(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    run_id: str,
    review_mode: str,
    item: Mapping[str, Any],
    review_json: Mapping[str, Any],
    context_meta: Mapping[str, Any],
    usage_accounting: Mapping[str, Any],
    review_runtime_identity: Mapping[str, Any],
    computed_at: str,
    elapsed_seconds: float,
) -> NonpublishableReviewArtifact:
    fields = generation_fields(review_json)
    fallback_review_phases = [
        dict(phase)
        for phase in review_json.get("review_model_phases") or []
        if isinstance(phase, Mapping)
    ]
    review_model_phases = _winning_review_phases(
        usage_accounting,
        fallback_review_phases,
    )
    for key in (
        "deepseek_usage_total",
        "deepseek_winning_usage_total",
        "deepseek_discarded_usage_total",
        "deepseek_usage_accounting",
    ):
        fields[key] = deepcopy(usage_accounting.get(key) or {})
    trace_pointer = {
        "bucket": str(config.DEEPSEEK_TRACE_S3_BUCKET or ""),
        "prefix": trace_s3_prefix(
            {
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "run_id": run_id,
            }
        ),
        "retention_days": 7,
    }
    context_runtime_identity = deepcopy(
        item.get("context_runtime_identity") or {}
    )
    artifact: Dict[str, Any] = {
        # A typed generation failure is private evidence, not a surrogate
        # public review and never a code-authored clear result.
        "main_comment": "",
        "inline_comments": [],
        "fallback_comments": [],
        "head_sha": head_sha,
        "computed_at": computed_at,
        "review_mode": review_mode,
        "placement_fetch": {
            "requested_target_count": 0,
            "enumerated_file_count": 0,
            "read_success_count": 0,
            "read_error_count": 0,
            "read_errors": [],
            "removed_path_skip_count": 0,
            "removed_paths": [],
            "unresolved_targets": [],
            "skipped_reason": "review_nonpublishable",
        },
        "context_artifact": item.get("context_artifact"),
        "context_runtime_identity": context_runtime_identity,
        "review_runtime_identity": deepcopy(
            dict(review_runtime_identity)
        ),
        "review_quality_warnings": list(
            review_json.get("review_quality_warnings") or []
        ),
        "review_model_phases": review_model_phases,
        **deepcopy(dict(usage_accounting)),
        "deepseek_trace_pointer": trace_pointer,
        "ci_generation_snapshot": context_meta.get(
            "ci_generation_snapshot"
        ),
        "ci_generation_model_payload": context_meta.get(
            "ci_generation_model_payload"
        ),
        "ci_snapshot": context_meta.get("ci_snapshot"),
        "ci_actionable_detail_lineage": dict(
            context_meta.get("ci_actionable_detail_lineage") or {}
        ),
        "ci_evidence_catalog": [
            dict(entry)
            for entry in context_meta.get("evidence_catalog") or []
            if isinstance(entry, Mapping)
            and entry.get("source_type") == "ci"
        ],
        "run_id": run_id,
        **fields,
    }
    if isinstance(review_json.get("presentation_v1"), Mapping):
        artifact["presentation_v1"] = deepcopy(
            dict(review_json["presentation_v1"])
        )
    if isinstance(review_json.get("v3_review"), Mapping):
        artifact["v3_review"] = deepcopy(dict(review_json["v3_review"]))

    failure_kind = str(
        review_json.get("review_failure_kind")
        or "review_generation_incomplete"
    )
    failure_stage = str(review_json.get("review_failure_stage") or "review")
    retryable = bool(review_json.get("review_failure_retryable"))
    terminal_attributes = {
        **{
            key: deepcopy(value)
            for key, value in fields.items()
            if key != "review_failure_message"
        },
        "deepseek_trace_pointer": trace_pointer,
        "review_phase_elapsed_seconds": round(elapsed_seconds, 3),
        "inline_comments_count": 0,
        "fallback_comments_count": 0,
        "context_runtime_identity": context_runtime_identity,
        "review_runtime_identity": deepcopy(
            dict(review_runtime_identity)
        ),
    }
    return NonpublishableReviewArtifact(
        artifact=artifact,
        generation_fields=fields,
        terminal_attributes=terminal_attributes,
        failure_kind=failure_kind,
        failure_stage=failure_stage,
        retryable=retryable,
    )


def build_publishable_result(
    prepared: PreparedGitHubReview,
    *,
    review_json: Mapping[str, Any],
    context_meta: Mapping[str, Any],
    usage_accounting: Mapping[str, Any],
    fallback_review_phases: Sequence[Mapping[str, Any]],
    item: Mapping[str, Any],
    review_mode: str,
    placement_fetch: Mapping[str, Any],
    context_runtime_identity: Mapping[str, Any],
    review_runtime_identity: Mapping[str, Any],
    run_id: str,
    attempt: int,
    elapsed_seconds: float,
) -> PublishableReviewArtifact:
    artifact = deepcopy(prepared.artifact)
    fields = generation_fields(review_json)
    artifact.update(fields)
    artifact["review_model_phases"] = _winning_review_phases(
        usage_accounting,
        fallback_review_phases,
    )
    artifact.update(deepcopy(dict(usage_accounting)))
    artifact["route_delta_provenance"] = deepcopy(
        context_meta.get("route_delta_provenance") or {}
    )
    # The DynamoDB projection must report the same end-to-end totals as the
    # immutable artifact, including Route/PFR calls from the Context attempt.
    for key in (
        "deepseek_usage_total",
        "deepseek_winning_usage_total",
        "deepseek_discarded_usage_total",
        "deepseek_usage_accounting",
    ):
        fields[key] = deepcopy(artifact.get(key) or {})
    if isinstance(review_json.get("presentation_v1"), Mapping):
        artifact["presentation_v1"] = deepcopy(
            dict(review_json["presentation_v1"])
        )
    if isinstance(review_json.get("v3_review"), Mapping):
        artifact["v3_review"] = deepcopy(dict(review_json["v3_review"]))
    artifact["review_quality_warnings"] = list(
        review_json.get("review_quality_warnings") or []
    )
    artifact["review_mode"] = review_mode
    artifact["placement_fetch"] = deepcopy(dict(placement_fetch))
    artifact["context_artifact"] = item.get("context_artifact")
    artifact["context_runtime_identity"] = deepcopy(
        dict(context_runtime_identity)
    )
    artifact["review_runtime_identity"] = deepcopy(
        dict(review_runtime_identity)
    )
    artifact["ci_generation_snapshot"] = context_meta.get(
        "ci_generation_snapshot"
    )
    artifact["ci_generation_model_payload"] = context_meta.get(
        "ci_generation_model_payload"
    )
    artifact["ci_snapshot"] = context_meta.get("ci_snapshot")
    artifact["ci_actionable_detail_lineage"] = dict(
        context_meta.get("ci_actionable_detail_lineage") or {}
    )
    artifact["ci_evidence_catalog"] = [
        dict(entry)
        for entry in context_meta.get("evidence_catalog") or []
        if isinstance(entry, Mapping) and entry.get("source_type") == "ci"
    ]
    artifact["ci_snapshot_changed_after_generation"] = bool(
        context_meta.get("ci_snapshot_changed_after_generation")
    )
    artifact["ci_changed_evidence_refs"] = list(
        context_meta.get("ci_changed_evidence_refs") or []
    )
    artifact["ci_evidence_invalidated_item_ids"] = list(
        review_json.get("ci_evidence_invalidated_item_ids") or []
    )
    artifact["run_id"] = run_id
    artifact["pipeline_attempt"] = int(attempt)
    terminal_attributes = {
        **fields,
        **{
            key: deepcopy(artifact[key])
            for key in GITHUB_PUBLICATION_FIELDS
            if key in artifact
        },
        "run_id": run_id,
        "pipeline_attempt": int(attempt),
        "review_phase_elapsed_seconds": round(elapsed_seconds, 3),
        "inline_comments_count": len(artifact.get("inline_comments") or []),
        "fallback_comments_count": len(
            artifact.get("fallback_comments") or []
        ),
        "context_runtime_identity": deepcopy(
            dict(context_runtime_identity)
        ),
        "review_runtime_identity": deepcopy(
            dict(review_runtime_identity)
        ),
    }
    return PublishableReviewArtifact(
        prepared=PreparedGitHubReview(
            head_sha=prepared.head_sha,
            main_body=prepared.main_body,
            comments=prepared.comments,
            artifact=artifact,
        ),
        generation_fields=fields,
        terminal_attributes=terminal_attributes,
    )

"""Thin coordinator for Deep judgment and Final presentation.

The review path has one semantic authority and one representation authority:

``exact-head evidence -> free-form Deep -> presentation-only Final -> public v3``

This module coordinates those capabilities.  It does not interpret findings,
invent fallback review substance, maintain a semantic ledger, or implement a
second review protocol.
"""

from __future__ import annotations

from copy import deepcopy
import json
import logging
import time
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from .. import config
from ..context_engine.packing import (
    CURRENT_HEAD_CI_END,
    CURRENT_HEAD_CI_START,
)
from ..deadline import Deadline
from ..deepseek_client import DeepSeekClient
from ..provider_usage import merge_numeric_usage
from ..structured_repair import (
    ContractRepairIssue,
    RepairStageContract,
    build_repair_messages,
    select_repair_issues,
)
from .context_projection import (
    acceptance_criteria_for_deep,
    changed_delta_for_deep,
    evidence_catalog_for_deep,
    evidence_gaps_for_deep,
)
from .judgment import (
    REVIEW_CALL_EXCEPTIONS,
    PresentationRepresentationError,
    ReviewGenerationTimeout,
    ReviewModelResponseError,
    ReviewOutputTruncated,
    artifact_failure_message,
    cap_context_for_review,
    failure_kind,
    failure_retryable,
    model_phase_telemetry,
    phase_timeout_seconds,
    run_model_phase,
)
from .presentation import (
    DECISION_VERDICTS,
    PRESENTATION_VERSION,
    PresentationResult,
    compile_presentation_v1,
    mark_final_response_incomplete,
)
from .prompts import (
    REVIEW_SYSTEM_PROMPT,
    render_deep_judgment_prompt,
    render_final_presentation_prompt,
)

logger = logging.getLogger(__name__)

_FINAL_REASONING_EFFORT = (
    "max" if config.MODEL_PROVIDER == "openrouter" else ""
)
_FINAL_THINKING = config.MODEL_PROVIDER == "openrouter"
_JSON_OBJECT_RESPONSE = {"type": "json_object"}
_FINAL_PRESENTATION_PHASE = "final_presentation"
_FINAL_REPAIR_PHASE = "final_presentation_repair"
_FINAL_REPAIRABLE_FAILURES = frozenset({
    "ambiguous_json_objects",
    "json_parse_error",
    "json_root_invalid",
    "presentation_version_invalid",
    "presentation_shape_invalid",
    "presentation_item_cap_exceeded",
    "decision_invalid",
    "deciding_item_loss",
    "out_of_catalog_material_evidence",
})
_FINAL_REPAIR_CONTRACT = RepairStageContract(
    stage="final_presentation",
    contract_instructions=(
        "Deep's original memo alone owns findings, causal dependencies, severity, and merge posture. Correct only Final's JSON representation or evidence-role assignment.",
        "Copy required references from Deep's Causal refs when present; a matching file path alone does not make supporting evidence causal.",
        "A catalog ID may support both a finding and a confidence check. Preserve valid finding substance and the original Deep-owned decision.",
        "Use only exact admitted catalog IDs cited on Deep's Evidence refs lines. Return the complete presentation_v1 object.",
    ),
    forbidden_instructions=(
        "Do not re-review, add a finding, alter the merge judgment, invent a reference, or turn CI status alone into code causality.",
    ),
)
_FINAL_DEEP_HANDOFF = (
    "The next assistant message is the exact visible Deep review memo from "
    "the completed judgment stage. It is Final's sole substantive authority. "
    "Do not re-review the pull request or infer new evidence; present only the "
    "memo under the following fixed Final request."
)

__all__ = [
    "ReviewGenerationTimeout",
    "ReviewModelResponseError",
    "ReviewOutputTruncated",
    "cap_context_for_review",
    "generate_review",
]


class ReviewInputBudgetExceeded(Exception):
    """The complete required Deep input cannot fit the configured boundary."""


def _split_generated_ci(
    pr_details: str,
    context_meta: Optional[Mapping[str, Any]],
) -> tuple[str, Any]:
    """Keep the code-generated CI snapshot exactly once in Deep's prompt."""

    details = str(pr_details or "")
    start = details.rfind(CURRENT_HEAD_CI_START)
    end = details.rfind(CURRENT_HEAD_CI_END)
    if start >= 0 and end > start:
        raw = details[start + len(CURRENT_HEAD_CI_START) : end].strip()
        try:
            snapshot = json.loads(raw)
        except json.JSONDecodeError:
            snapshot = None
        if isinstance(snapshot, dict):
            without_snapshot = (
                details[:start] + details[end + len(CURRENT_HEAD_CI_END) :]
            ).strip()
            return without_snapshot, snapshot
    snapshot = (context_meta or {}).get("ci_snapshot")
    return details, snapshot if isinstance(snapshot, Mapping) else {}


def _bounded_deep_prompt(
    pr_details: str,
    context: str,
    context_meta: Optional[Mapping[str, Any]],
) -> str:
    """Render Deep's complete evidence packet under its configured input cap."""

    intent_details, ci_snapshot = _split_generated_ci(
        pr_details,
        context_meta,
    )
    acceptance_criteria = acceptance_criteria_for_deep(context_meta)
    changed_delta = changed_delta_for_deep(context_meta)
    evidence_catalog = evidence_catalog_for_deep(context_meta)
    evidence_gaps = evidence_gaps_for_deep(context_meta)
    render_values = {
        "acceptance_criteria": acceptance_criteria,
        "changed_delta": changed_delta,
        "ci_snapshot": ci_snapshot,
        "evidence_catalog": evidence_catalog,
        "evidence_gaps": evidence_gaps,
    }
    input_limit = max(0, int(config.REVIEW_INPUT_MAX_CHARS))

    fixed_prompt = render_deep_judgment_prompt(
        intent_details,
        "",
        **render_values,
    )
    context_budget = max(0, input_limit - len(fixed_prompt))
    capped_context = cap_context_for_review(
        "",
        context,
        max_input_chars=context_budget,
    )
    prompt = render_deep_judgment_prompt(
        intent_details,
        capped_context,
        **render_values,
    )

    # The empty-context rendering includes a short fallback sentence.  A
    # second exact pass accounts for that replacement without truncating any
    # other evidence surface or breaking section-aware packing.
    if len(prompt) > input_limit and capped_context:
        excess = len(prompt) - input_limit
        capped_context = cap_context_for_review(
            "",
            capped_context,
            max_input_chars=max(0, len(capped_context) - excess),
        )
        prompt = render_deep_judgment_prompt(
            intent_details,
            capped_context,
            **render_values,
        )
    if len(prompt) > input_limit:
        raise ReviewInputBudgetExceeded(
            "required Deep evidence exceeds REVIEW_INPUT_MAX_CHARS"
        )
    return prompt


def _messages(prompt: str) -> list[Dict[str, str]]:
    return [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


def _final_messages(
    deep_message: Mapping[str, Any],
    context_meta: Optional[Mapping[str, Any]],
) -> list[Dict[str, str]]:
    """Carry Deep's judgment plus exact changed topology into presentation.

    Final never receives PFR, CI diagnostics, or other material from which it
    could become a second reviewer.  The existing bounded changed-delta
    projection is supplied only so Final can compose Deep's visual judgment
    and exact inline placement from Deep-owned substance.
    """

    return [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": _FINAL_DEEP_HANDOFF},
        {
            "role": "assistant",
            "content": str(deep_message.get("content") or ""),
        },
        {
            "role": "user",
            "content": render_final_presentation_prompt(
                changed_delta_for_deep(context_meta)
            ),
        },
    ]


def _final_repair_issues(compiled: PresentationResult):
    lost_locations = _lost_blocking_decision_locations(compiled)
    partial_deciding_loss = compiled.publishable and bool(lost_locations)
    if (
        (
            not partial_deciding_loss
            and compiled.failure_kind not in _FINAL_REPAIRABLE_FAILURES
        )
        or any(issue.severity == "truth" for issue in compiled.issues)
    ):
        return None
    if partial_deciding_loss:
        source_issues = (
            issue for issue in compiled.issues
            if issue.severity == "item"
            and any(
                issue.location == location
                or issue.location.startswith(f"{location}.")
                for location in lost_locations
            )
        )
    else:
        source_issues = compiled.issues
    issues = [
        ContractRepairIssue(
            code=issue.code,
            location=issue.location,
            message=issue.message,
            repair_action=(
                "restore_deep_causal_refs"
                if issue.code in {
                    "high_priority_evidence_gate_failed",
                    "finding_evidence_capability_insufficient",
                    "deciding_item_lost",
                }
                else "repair_json_representation"
            ),
            priority=(
                10 if issue.code != "deciding_item_lost" else 20
            ),
        )
        for issue in source_issues
    ]
    return select_repair_issues(issues) if issues else None


def _lost_blocking_decision_locations(compiled: PresentationResult) -> set[str]:
    return {
        action.rsplit(":", 1)[0]
        for action in compiled.normalizations
        if action.endswith(":blocking_decision_dependency_removed")
    }


def _append_phase(
    phases: list[Dict[str, Any]],
    telemetry: Optional[Mapping[str, Any]],
) -> None:
    if isinstance(telemetry, Mapping):
        phases.append(dict(telemetry))


def _append_failed_phase(
    phases: list[Dict[str, Any]],
    error: BaseException,
    *,
    phase: str,
    model: str,
    reasoning_effort: str,
    thinking: bool,
    elapsed_seconds: float,
) -> None:
    telemetry = getattr(error, "telemetry", None)
    if isinstance(telemetry, Mapping):
        _append_phase(phases, telemetry)
        return
    # Provider-call accounting is authoritative for dispatched failures.  This
    # placeholder records the code-owned wall/deadline boundary when no
    # provider envelope exists and therefore no numeric usage can be copied.
    _append_phase(
        phases,
        model_phase_telemetry(
            phase,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            attempt=1,
            elapsed_seconds=elapsed_seconds,
            finish_reason="",
            usage={},
        ),
    )


def _usage_total(phases: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return merge_numeric_usage(
        *(phase.get("usage") for phase in phases if isinstance(phase, Mapping))
    )


def _phase_copies(
    phases: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    return [
        dict(phase)
        for phase in phases
        if isinstance(phase, Mapping)
    ]


def _finish_reason_from_telemetry(error: BaseException) -> str:
    telemetry = getattr(error, "telemetry", None)
    if not isinstance(telemetry, Mapping):
        return ""
    return str(telemetry.get("finish_reason") or "")


def _selected_presentation_metadata(
    selected_phase: str,
    phases: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Describe the model response that actually owns the visible review.

    The phase name is deliberately content-free.  Finish reason, thinking,
    and effort come from that phase's recorded telemetry instead of whichever
    presentation attempt happened to run last.
    """

    if selected_phase not in {_FINAL_PRESENTATION_PHASE, _FINAL_REPAIR_PHASE}:
        raise ValueError(
            f"unsupported selected presentation phase: {selected_phase}"
        )
    selected = next(
        (
            phase
            for phase in reversed(phases)
            if str(phase.get("phase") or "") == selected_phase
        ),
        None,
    )
    if selected is None:
        raise ValueError(
            f"selected presentation phase is missing telemetry: "
            f"{selected_phase}"
        )
    return {
        "review_presentation_selected_phase": selected_phase,
        "review_model_finish_reason": str(
            selected.get("finish_reason") or ""
        ),
        "review_final_thinking": bool(selected.get("thinking")),
        "review_final_reasoning_effort": str(
            selected.get("reasoning_effort") or ""
        ),
    }


def _nonpublishable_result(
    *,
    error: BaseException,
    phase: str,
    phases: Sequence[Mapping[str, Any]],
    finish_reasons: Mapping[str, str],
    normalizations: Sequence[str] = (),
    failure_kind_override: Optional[str] = None,
) -> Dict[str, Any]:
    kind = str(failure_kind_override or failure_kind(error))
    if isinstance(error, ReviewInputBudgetExceeded):
        kind = "review_input_budget_exceeded"
        message = (
            "The complete required review input exceeded its bounded model "
            "context budget."
        )
        retryable = False
    else:
        message = artifact_failure_message(error)
        retryable = failure_retryable(error)
    logger.warning(
        "Review generation stopped without publication phase=%s kind=%s class=%s",
        phase,
        kind,
        type(error).__name__,
    )
    result: Dict[str, Any] = {
        "review_generation_status": "failed",
        "review_fallback_used": False,
        "review_publishable": False,
        "review_publication_safe": False,
        "review_nonpublish_reason": kind,
        "review_failure_retryable": bool(retryable),
        "review_failure_kind": kind,
        "review_failure_stage": phase,
        "review_failure_class": type(error).__name__,
        "review_failure_message": message,
        "review_model_finish_reason": (
            str(finish_reasons.get(_FINAL_REPAIR_PHASE) or "")
            or str(finish_reasons.get(_FINAL_PRESENTATION_PHASE) or "")
            or str(finish_reasons.get("deep_judgment") or "")
        ),
        "review_stage_finish_reasons": dict(finish_reasons),
        "review_presentation_version": PRESENTATION_VERSION,
        "review_presentation_safe_partial": False,
        "review_presentation_normalizations": list(
            dict.fromkeys(str(item) for item in normalizations if str(item))
        ),
        "review_final_thinking": bool(
            next(
                (
                    item.get("thinking")
                    for item in reversed(phases)
                    if str(item.get("phase") or "") in {
                        _FINAL_PRESENTATION_PHASE,
                        _FINAL_REPAIR_PHASE,
                    }
                ),
                False,
            )
        ),
        "review_final_reasoning_effort": str(
            next(
                (
                    item.get("reasoning_effort")
                    for item in reversed(phases)
                    if str(item.get("phase") or "") in {
                        _FINAL_PRESENTATION_PHASE,
                        _FINAL_REPAIR_PHASE,
                    }
                ),
                "",
            )
            or ""
        ),
        "review_model_phases": _phase_copies(phases),
        "deepseek_usage_total": _usage_total(phases),
        "review_quality_warnings": [],
        "quality_scoreable": False,
        "quality_exclusion_reasons": [f"review_failure:{kind}"],
    }
    if kind == "wall_timeout":
        result["review_timeout_phase"] = phase
    return result


def _publishable_result(
    compiled: PresentationResult,
    *,
    selected_phase: str,
    phases: Sequence[Mapping[str, Any]],
    finish_reasons: Mapping[str, str],
    normalizations: Sequence[str],
) -> Dict[str, Any]:
    if not compiled.publishable or compiled.review is None:
        raise ValueError("publishable presentation result is required")
    review = deepcopy(compiled.review)
    warnings = list(review.get("review_quality_warnings") or [])
    effective_safe_partial = bool(compiled.safe_partial)
    if effective_safe_partial:
        warnings.append("presentation_safe_partial")
    selected_metadata = _selected_presentation_metadata(
        selected_phase,
        phases,
    )
    review.update(
        {
            "review_generation_status": "complete",
            "review_fallback_used": False,
            "review_publishable": True,
            "review_publication_safe": True,
            "review_nonpublish_reason": None,
            "review_failure_retryable": False,
            "review_failure_kind": None,
            "review_failure_stage": None,
            "review_failure_class": None,
            "review_failure_message": None,
            "review_stage_finish_reasons": dict(finish_reasons),
            "review_presentation_version": PRESENTATION_VERSION,
            "review_presentation_safe_partial": effective_safe_partial,
            "review_presentation_normalizations": list(
                dict.fromkeys(
                    str(item) for item in normalizations if str(item)
                )
            ),
            "review_model_phases": _phase_copies(phases),
            "deepseek_usage_total": _usage_total(phases),
            "review_quality_warnings": list(dict.fromkeys(warnings)),
            "quality_scoreable": True,
            "quality_exclusion_reasons": [],
            **selected_metadata,
        }
    )
    return review


def _presentation_failure(
    result: PresentationResult,
) -> PresentationRepresentationError:
    # Keep parser/model text out of durable artifacts.  ``failure_kind`` is a
    # bounded compiler-owned enum and is persisted separately.
    return PresentationRepresentationError(
        "Final presentation did not yield a safe publishable review"
    )


def generate_review(
    pr_details: str,
    context: str,
    *,
    client: Optional[DeepSeekClient] = None,
    trace_metadata: Optional[Dict[str, Any]] = None,
    context_meta: Optional[Dict[str, Any]] = None,
    deadline: Optional[Deadline] = None,
    model: str = config.REVIEW_MODEL,
    reasoning_effort: str = config.REVIEW_EFFORT,
    phase_sink: Optional[list] = None,
    before_final: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Run the sole production Deep/Final judgment path."""

    active_client = client or DeepSeekClient(
        model=model,
        reasoning_effort=reasoning_effort,
    )
    metadata = {
        **dict(trace_metadata or {}),
        "review_protocol": PRESENTATION_VERSION,
    }
    phases: list[Dict[str, Any]] = (
        phase_sink if phase_sink is not None else []
    )
    finish_reasons: Dict[str, str] = {}
    stage_started = time.monotonic()

    try:
        deep_prompt = _bounded_deep_prompt(
            pr_details,
            context,
            context_meta,
        )
    except ReviewInputBudgetExceeded as error:
        return _nonpublishable_result(
            error=error,
            phase="deep_judgment",
            phases=phases,
            finish_reasons=finish_reasons,
        )

    deep_messages = _messages(deep_prompt)
    deep_started = time.monotonic()
    try:
        deep = run_model_phase(
            active_client,
            phase="deep_judgment",
            messages=deep_messages,
            model=model,
            reasoning_effort=reasoning_effort,
            thinking=True,
            max_tokens=config.REVIEW_DEEP_THINKING_MAX_TOKENS or None,
            timeout_seconds=phase_timeout_seconds(
                config.REVIEW_DEEP_THINKING_TIMEOUT_SECONDS,
                stage_started,
                phase="deep_judgment",
                deadline=deadline,
            ),
            deadline=deadline,
            trace_metadata=metadata,
        )
        _append_phase(phases, deep.telemetry)
        finish_reasons["deep_judgment"] = str(
            deep.telemetry.get("finish_reason") or ""
        )
    except REVIEW_CALL_EXCEPTIONS as error:
        _append_failed_phase(
            phases,
            error,
            phase="deep_judgment",
            model=model,
            reasoning_effort=reasoning_effort,
            thinking=True,
            elapsed_seconds=time.monotonic() - deep_started,
        )
        finish_reasons["deep_judgment"] = _finish_reason_from_telemetry(
            error
        )
        return _nonpublishable_result(
            error=error,
            phase="deep_judgment",
            phases=phases,
            finish_reasons=finish_reasons,
        )

    # Lifecycle policy remains with the orchestrator.  Invoke its boundary
    # only after a successful Deep result and before constructing or
    # dispatching any Final request.  Deliberately leave callback failures out
    # of the model-error conversion path so they propagate to terminal flow.
    if before_final is not None:
        before_final()

    final_messages = _final_messages(deep.message, context_meta)
    final_started = time.monotonic()
    final_timeout = 0.0
    final_incomplete_error: Optional[ReviewModelResponseError] = None
    try:
        final_timeout = phase_timeout_seconds(
            config.REVIEW_FINAL_OUTPUT_TIMEOUT_SECONDS,
            stage_started,
            phase="final_presentation",
            deadline=deadline,
        )
        final = run_model_phase(
            active_client,
            phase="final_presentation",
            messages=final_messages,
            model=model,
            reasoning_effort=_FINAL_REASONING_EFFORT,
            thinking=_FINAL_THINKING,
            max_tokens=config.REVIEW_FINAL_OUTPUT_MAX_TOKENS or None,
            timeout_seconds=final_timeout,
            deadline=deadline,
            trace_metadata=metadata,
            response_format=dict(_JSON_OBJECT_RESPONSE),
        )
        _append_phase(phases, final.telemetry)
        finish_reasons["final_presentation"] = str(
            final.telemetry.get("finish_reason") or ""
        )
        failed_final_content = final.content
    except REVIEW_CALL_EXCEPTIONS as error:
        _append_failed_phase(
            phases,
            error,
            phase="final_presentation",
            model=model,
            reasoning_effort=_FINAL_REASONING_EFFORT,
            thinking=_FINAL_THINKING,
            elapsed_seconds=time.monotonic() - final_started,
        )
        finish_reasons["final_presentation"] = (
            _finish_reason_from_telemetry(error)
        )
        visible_content = getattr(error, "visible_content", "")
        if isinstance(error, ReviewModelResponseError) and visible_content:
            final_incomplete_error = error
            failed_final_content = str(visible_content)
        else:
            return _nonpublishable_result(
                error=error,
                phase="final_presentation",
                phases=phases,
                finish_reasons=finish_reasons,
            )

    compiled = compile_presentation_v1(
        failed_final_content,
        pr_details=pr_details,
        context_meta=context_meta,
    )
    if final_incomplete_error is not None:
        compiled = mark_final_response_incomplete(compiled)
    lost_locations = _lost_blocking_decision_locations(compiled)
    if compiled.publishable and not lost_locations:
        return _publishable_result(
            compiled,
            selected_phase=_FINAL_PRESENTATION_PHASE,
            phases=phases,
            finish_reasons=finish_reasons,
            normalizations=compiled.normalizations,
        )
    repair_selection = (
        _final_repair_issues(compiled)
        if final_incomplete_error is None and failed_final_content.strip()
        else None
    )
    minimum_repaired_findings = (
        len(compiled.presentation["findings"]) + len(lost_locations)
        if compiled.publishable and lost_locations
        else 0
    )
    if (
        repair_selection is not None
        and repair_selection.selected
    ):
        initial_decision = (
            compiled.presentation.get("decision")
            if isinstance(compiled.presentation, Mapping)
            else None
        )
        initial_verdict = (
            initial_decision.get("verdict")
            if isinstance(initial_decision, Mapping)
            else None
        )
        if initial_verdict not in DECISION_VERDICTS:
            initial_verdict = None
        repair_messages = build_repair_messages(
            final_messages,
            failed_final_content,
            contract=_FINAL_REPAIR_CONTRACT,
            issues=repair_selection.selected,
        )
        if before_final is not None:
            before_final()
        repair_started = time.monotonic()
        try:
            repaired = run_model_phase(
                active_client,
                phase=_FINAL_REPAIR_PHASE,
                messages=repair_messages,
                model=model,
                reasoning_effort=_FINAL_REASONING_EFFORT,
                thinking=_FINAL_THINKING,
                max_tokens=config.REVIEW_FINAL_OUTPUT_MAX_TOKENS or None,
                timeout_seconds=phase_timeout_seconds(
                    config.REVIEW_FINAL_OUTPUT_TIMEOUT_SECONDS,
                    stage_started,
                    phase=_FINAL_REPAIR_PHASE,
                    deadline=deadline,
                ),
                deadline=deadline,
                trace_metadata=metadata,
                response_format=dict(_JSON_OBJECT_RESPONSE),
            )
            _append_phase(phases, repaired.telemetry)
            finish_reasons[_FINAL_REPAIR_PHASE] = str(
                repaired.telemetry.get("finish_reason") or ""
            )
        except REVIEW_CALL_EXCEPTIONS as error:
            _append_failed_phase(
                phases,
                error,
                phase=_FINAL_REPAIR_PHASE,
                model=model,
                reasoning_effort=_FINAL_REASONING_EFFORT,
                thinking=_FINAL_THINKING,
                elapsed_seconds=time.monotonic() - repair_started,
            )
            finish_reasons[_FINAL_REPAIR_PHASE] = (
                _finish_reason_from_telemetry(error)
            )
            return _nonpublishable_result(
                error=error,
                phase="final_presentation",
                phases=phases,
                finish_reasons=finish_reasons,
                normalizations=compiled.normalizations,
            )
        corrected = compile_presentation_v1(
            repaired.content,
            pr_details=pr_details,
            context_meta=context_meta,
        )
        corrected_decision = (
            corrected.presentation.get("decision")
            if isinstance(corrected.presentation, Mapping)
            else None
        )
        corrected_verdict = (
            corrected_decision.get("verdict")
            if isinstance(corrected_decision, Mapping)
            else None
        )
        if (
            corrected.publishable
            and not _lost_blocking_decision_locations(corrected)
            and len(corrected.presentation["findings"]) >= minimum_repaired_findings
            and (initial_verdict is None or corrected_verdict == initial_verdict)
        ):
            return _publishable_result(
                corrected,
                selected_phase=_FINAL_REPAIR_PHASE,
                phases=phases,
                finish_reasons=finish_reasons,
                normalizations=(
                    *compiled.normalizations,
                    *corrected.normalizations,
                ),
            )
        if not corrected.publishable or _lost_blocking_decision_locations(corrected):
            compiled = corrected
    presentation_error = _presentation_failure(compiled)
    return _nonpublishable_result(
        error=presentation_error,
        phase="final_presentation",
        phases=phases,
        finish_reasons=finish_reasons,
        normalizations=compiled.normalizations,
        failure_kind_override=(
            "deciding_item_loss"
            if _lost_blocking_decision_locations(compiled)
            else compiled.failure_kind
        ),
    )

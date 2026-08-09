"""Shared PFR provider-protocol plumbing and typed failures."""

from __future__ import annotations

import json

from ...json_representation import normalize_json_object_representation
from ...provider_usage import merge_numeric_usage
from ...structured_repair import ContractRepairIssue
from typing import Any, Dict, List, Optional, Tuple

class PFRStructuredOutputError(ValueError):
    """A completed PFR model response violated its structured contract."""

    def __init__(self, kind: str, message: str, issues: Optional[List[ContractRepairIssue]] = None):
        self.kind = kind
        self.issues = list(issues or [])
        super().__init__(message)

class PFRReconcileFailure(ValueError):
    """Carry both the initial contract failure and one repair outcome."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        usages: List[Dict[str, Any]],
        finish_reasons: Dict[str, str],
        repair_telemetry: Optional[Dict[str, Any]] = None,
        model_phases: Optional[List[Dict[str, Any]]] = None,
    ):
        self.kind = kind
        self.usages = list(usages)
        self.finish_reasons = dict(finish_reasons)
        self.repair_telemetry = dict(repair_telemetry or {})
        self.model_phases = [
            dict(item) for item in model_phases or [] if isinstance(item, dict)
        ]
        super().__init__(message)

def _pfr_model_phase(
    phase: str,
    *,
    model: str,
    thinking: bool,
    reasoning_effort: str,
    attempt: int,
    elapsed_seconds: float,
    finish_reason: str,
    usage: Optional[Dict[str, Any]] = None,
    round_index: Optional[int] = None,
) -> Dict[str, Any]:
    record = {
        "phase": phase,
        "model": str(model or ""),
        "thinking": bool(thinking),
        "reasoning_effort": str(reasoning_effort or ""),
        "attempt": max(1, int(attempt)),
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
        "finish_reason": str(finish_reason or ""),
        "usage": merge_numeric_usage(usage),
    }
    if round_index is not None:
        record["round"] = max(1, int(round_index))
    return record

def _parse_json_object(text: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(text[start : end + 1])
        else:
            raise
    if not isinstance(parsed, dict):
        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            return parsed[0]
        raise PFRStructuredOutputError(
            "json_root_type_invalid",
            "PFR JSON root must be an object",
            [
                ContractRepairIssue(
                    code="json_root_type_invalid",
                    location="$",
                    message="Return one JSON object as the root.",
                    repair_action="ineligible",
                    priority=0,
                )
            ],
        )
    return parsed

def _normalize_json_object_for_contract(
    text: str,
) -> Tuple[Dict[str, Any], List[str]]:
    """Return an exact object after representation-only JSON normalization.

    Unlike the historical planner parser, this contract path never salvages the
    substring between arbitrary braces.  An unparseable output cannot anchor a
    safe model repair because item identity and field binding are unknown.
    """

    normalized = normalize_json_object_representation(text)
    if normalized is not None:
        return dict(normalized.value), list(normalized.actions)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise PFRStructuredOutputError(
            "json_root_type_invalid",
            "PFR JSON root must be an object",
            [
                ContractRepairIssue(
                    code="json_root_type_invalid",
                    location="$",
                    message="Return one JSON object as the root.",
                    repair_action="ineligible",
                    priority=0,
                )
            ],
        )
    return parsed, []

def _message_content(response: Dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise PFRStructuredOutputError(
            "model_response_invalid", "missing choices[0].message"
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise PFRStructuredOutputError(
            "model_response_invalid", "PFR response content is empty"
        )
    return content

def _assistant_message(response: Dict[str, Any]) -> Dict[str, Any]:
    try:
        raw = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise PFRStructuredOutputError(
            "model_response_invalid", "missing choices[0].message"
        ) from exc
    if not isinstance(raw, dict):
        raise PFRStructuredOutputError(
            "model_response_invalid", "choices[0].message must be an object"
        )
    message = dict(raw)
    if message.get("role") not in (None, "assistant"):
        raise PFRStructuredOutputError(
            "model_response_invalid", "choices[0].message role must be assistant"
        )
    if message.get("tool_calls"):
        raise PFRStructuredOutputError(
            "model_response_invalid",
            "choices[0].message has tool_calls in a no-tools PFR stage",
        )
    message.setdefault("role", "assistant")
    _message_content(response)
    return message

def _finish_reason(response: Dict[str, Any]) -> str:
    try:
        return str(response["choices"][0].get("finish_reason") or "").strip().lower()
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""

def _require_complete_response(response: Dict[str, Any], *, stage: str) -> str:
    finish_reason = _finish_reason(response)
    if finish_reason == "length":
        raise PFRStructuredOutputError(
            "output_truncated", f"{stage} response finish_reason=length"
        )
    if finish_reason != "stop":
        raise PFRStructuredOutputError(
            "model_response_invalid",
            f"{stage} requires finish_reason=stop, got {finish_reason or 'missing'}",
        )
    _assistant_message(response)
    return finish_reason

def _truncate(text: str, limit: int) -> str:
    return text if len(text or "") <= limit else (text or "")[: limit - 3] + "..."

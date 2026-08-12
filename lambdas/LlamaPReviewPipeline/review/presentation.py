"""Public facade for Final's fixed ``presentation_v1`` transport.

This module owns transport types, conservative JSON parsing, and the stable API
consumed by generation and orchestration. Item validation, local degradation,
evidence admission, and public-v3 assembly live in
:mod:`presentation_projection`. There is no second model repair path.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Any, Dict, Iterable, Literal, Mapping, Optional, Sequence, Tuple

from ..json_representation import normalize_json_object_representation

PRESENTATION_VERSION = "presentation_v1"
PRESENTATION_STATUSES = {"publishable", "failure"}
DECISION_VERDICTS = {"clear", "verification_needed", "blocking"}
PRIORITIES = {"P0", "P1", "P2"}
CONFIDENCES = {"High", "Medium", "Low"}
PLACEMENTS = {"inline", "headline", "collapsed"}
CATEGORIES = {
    "bug",
    "security",
    "breaking-change",
    "test-gap",
    "maintainability",
    "performance",
    "architecture",
    "documentation",
    "question",
    "note",
}
DIAGRAM_PURPOSES = {"risk_path", "pr_flow_map"}
SUGGESTION_TYPES = {"DIRECT_REPLACEMENT", "CONCEPTUAL_ADVICE"}

# Final is asked to compress to eight items. A small transport tolerance keeps
# a useful review available when the serializer misses that presentation target
# by one or two items; the compiler still validates every retained item and
# never silently truncates a deciding finding.
MAX_FINDINGS = 12
MAX_UNKNOWNS = 12
MAX_CONFIDENCE_CHECKS = 6
MAX_DECISION_ACTIONS = 1
MAX_EVIDENCE_REFS = 16
MAX_TEXT = 4_000
MAX_SUMMARY = 2_000
MAX_CODE = 16_000

ROOT_FIELDS = {
    "version",
    "decision",
    "findings",
    "material_unknowns",
    "confidence_checks",
    "diagram",
}
DECISION_FIELDS = {"verdict", "confidence", "summary", "owner_actions"}
FINDING_FIELDS = {
    "headline",
    "priority",
    "category",
    "confidence",
    "file_path",
    "code_snippet",
    "analysis",
    "owner_action",
    "required_evidence_refs",
    "supporting_evidence_refs",
    "representation_requirement",
    "placement",
    "suggestion",
}
UNKNOWN_FIELDS = {"missing_fact", "impact", "owner_action", "evidence_refs"}
CHECK_FIELDS = {"check", "result", "evidence_refs", "ci_relevance"}
DIAGRAM_FIELDS = {"purpose", "caption", "mermaid", "evidence_refs"}
SUGGESTION_FIELDS = {"type", "content"}

CATEGORY_TO_V3 = {
    "bug": "bug",
    "security": "security",
    "breaking-change": "breaking-change",
    "test-gap": "test-gap",
    "question": "question",
    "note": "note",
    # Public v3 intentionally has a smaller display taxonomy. These are fixed
    # transport aliases, never prose classification.
    "maintainability": "note",
    "performance": "note",
    "architecture": "note",
    "documentation": "note",
}
VERDICT_TO_V3 = {
    "clear": "clear",
    "verification_needed": "unverified",
    "blocking": "blocked_findings",
}
PRIVATE_PRESENTATION_TOKENS = (
    "presentation_v1",
    "schema_version",
    "required_evidence_refs",
    "supporting_evidence_refs",
    "failed to parse ai response",
)


@dataclass(frozen=True)
class PresentationIssue:
    """One private diagnostic; it is never rendered or published."""

    code: str
    location: str
    message: str
    severity: Literal["representation", "surface", "item", "truth"]


@dataclass(frozen=True)
class PresentationParseResult:
    value: Optional[Dict[str, Any]]
    normalizations: Tuple[str, ...] = ()
    error_kind: Optional[str] = None

    @property
    def parsed(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class PresentationResult:
    status: Literal["publishable", "failure"]
    review: Optional[Dict[str, Any]]
    presentation: Optional[Dict[str, Any]]
    issues: Tuple[PresentationIssue, ...] = ()
    normalizations: Tuple[str, ...] = ()
    failure_kind: Optional[str] = None
    safe_partial: bool = False

    def __post_init__(self) -> None:
        if self.status not in PRESENTATION_STATUSES:
            raise ValueError(f"invalid presentation status: {self.status}")

    @property
    def publishable(self) -> bool:
        return self.status == "publishable" and self.review is not None

def _unwrap_single_object(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    return None


def _outermost_json_candidates(text: str) -> list[tuple[int, int, Dict[str, Any]]]:
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, Dict[str, Any]]] = []
    for start, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            parsed, length = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        value = _unwrap_single_object(parsed)
        if value is not None:
            candidates.append((start, start + length, value))
    return [
        candidate
        for candidate in candidates
        if not any(
            other[0] <= candidate[0]
            and candidate[1] <= other[1]
            and (other[0], other[1]) != (candidate[0], candidate[1])
            for other in candidates
        )
    ]


def parse_presentation_v1(content: str) -> PresentationParseResult:
    """Parse one object using only parser-proven unique normalization."""

    if not isinstance(content, str) or not content.strip():
        return PresentationParseResult(None, error_kind="empty_response")
    text = content.lstrip("\ufeff").strip()
    normalizations: list[str] = []
    if text != content.strip():
        normalizations.append("unicode_bom_removed")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    value = _unwrap_single_object(parsed)
    if value is not None:
        if isinstance(parsed, list):
            normalizations.append("single_object_array_unwrapped")
        return PresentationParseResult(deepcopy(value), tuple(normalizations))
    if parsed is not None:
        return PresentationParseResult(None, error_kind="json_root_invalid")

    locally_normalized = normalize_json_object_representation(text)
    if locally_normalized is not None:
        normalizations.extend(locally_normalized.actions)
        return PresentationParseResult(
            deepcopy(dict(locally_normalized.value)),
            tuple(dict.fromkeys(normalizations)),
        )

    fence = re.fullmatch(
        r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```",
        text,
        flags=re.IGNORECASE,
    )
    if fence:
        try:
            fenced = json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            fenced = None
        value = _unwrap_single_object(fenced)
        if value is not None:
            normalizations.append("single_json_fence_unwrapped")
            if isinstance(fenced, list):
                normalizations.append("single_object_array_unwrapped")
            return PresentationParseResult(
                deepcopy(value),
                tuple(normalizations),
            )

    candidates = _outermost_json_candidates(text)
    if len(candidates) == 1:
        start, end, value = candidates[0]
        outside = text[:start] + text[end:]
        if not any(char in outside for char in "{}[]"):
            normalizations.append("unique_embedded_json_object_extracted")
            return PresentationParseResult(
                deepcopy(value),
                tuple(normalizations),
            )
    return PresentationParseResult(
        None,
        error_kind=(
            "ambiguous_json_objects" if len(candidates) > 1 else "json_parse_error"
        ),
    )

def issue(
    code: str,
    location: str,
    message: str,
    severity: Literal["representation", "surface", "item", "truth"],
) -> PresentationIssue:
    return PresentationIssue(code, location, message, severity)


def bounded_text(value: Any, *, limit: int = MAX_TEXT) -> Optional[str]:
    if not isinstance(value, str):
        return None
    result = value.strip()
    if not result or len(result) > limit:
        return None
    return result


def contains_private_presentation_vocabulary(value: str) -> bool:
    folded = value.casefold()
    return any(token in folded for token in PRIVATE_PRESENTATION_TOKENS)


def changed_ref(ref: str, changed_ci_refs: set[str]) -> bool:
    return bool(
        ref in changed_ci_refs
        or (ref.startswith("ci:") and ref[3:] in changed_ci_refs)
        or f"ci:{ref}" in changed_ci_refs
    )


def failure_result(
    *,
    presentation: Optional[Dict[str, Any]],
    issues: Sequence[PresentationIssue],
    normalizations: Sequence[str],
    failure_kind: str,
) -> PresentationResult:
    return PresentationResult(
        status="failure",
        review=None,
        presentation=deepcopy(presentation),
        issues=tuple(issues),
        normalizations=tuple(dict.fromkeys(normalizations)),
        failure_kind=failure_kind,
    )


def compile_presentation_v1(
    content_or_object: str | Mapping[str, Any],
    *,
    pr_details: str,
    context_meta: Optional[Dict[str, Any]] = None,
    changed_ci_refs: Iterable[str] = (),
) -> PresentationResult:
    """Compile one Final presentation without inventing a judgment."""

    if isinstance(content_or_object, str):
        parsed = parse_presentation_v1(content_or_object)
        if not parsed.parsed:
            return failure_result(
                presentation=None,
                issues=(
                    issue(
                        parsed.error_kind or "json_parse_error",
                        "$",
                        "Final did not emit one uniquely parseable JSON object",
                        "representation",
                    ),
                ),
                normalizations=parsed.normalizations,
                failure_kind=parsed.error_kind or "json_parse_error",
            )
        source = parsed.value or {}
        parse_normalizations = parsed.normalizations
    elif isinstance(content_or_object, Mapping):
        source = deepcopy(dict(content_or_object))
        parse_normalizations = ()
    else:
        return failure_result(
            presentation=None,
            issues=(
                issue(
                    "json_root_invalid",
                    "$",
                    "Final presentation must be text or an object",
                    "representation",
                ),
            ),
            normalizations=(),
            failure_kind="json_root_invalid",
        )
    from .presentation_projection import compile_presentation_object

    return compile_presentation_object(
        source,
        pr_details=pr_details,
        context_meta=context_meta,
        changed_ci_refs={
            str(ref).strip()
            for ref in changed_ci_refs
            if isinstance(ref, str) and ref.strip()
        },
        parse_normalizations=parse_normalizations,
    )


def mark_final_response_incomplete(
    result: PresentationResult,
    *,
    failure_kind: str = "incomplete_provider_envelope",
) -> PresentationResult:
    """Fail closed when the provider did not attest a complete response."""

    return PresentationResult(
        status="failure",
        review=None,
        presentation=deepcopy(result.presentation),
        issues=(
            *result.issues,
            issue(
                failure_kind,
                "$",
                "provider did not attest a complete Final response",
                "representation",
            ),
        ),
        normalizations=result.normalizations,
        failure_kind=failure_kind,
        safe_partial=result.safe_partial,
    )

"""Shared state and primitives for the public-v3 structural validator.

The validator is intentionally deterministic.  This module owns its transient
state, fixed constants, typed violations, and field-shape helpers; it does not
own review judgment or evidence capability rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional, Sequence

from .evidence_contract import (
    ReviewContractError,
    ReviewContractViolation,
    catalog_entries,
    expected_head_sha,
)


SCHEMA_VERSION = 3
ALLOWED_MODEL_VERDICTS = {"blocked_findings", "unverified", "clear"}
ALLOWED_DIAGRAM_PURPOSES = {"risk_path", "pr_flow_map"}
ALLOWED_FINDING_TYPES = {
    "bug",
    "security",
    "breaking-change",
    "test-gap",
    "question",
    "note",
}
ALLOWED_PRIORITIES = {"P0", "P1", "P2"}
BLOCKING_PRIORITIES = {"P0", "P1"}
ALLOWED_CONFIDENCE = {"High", "Medium", "Low"}
ALLOWED_EVIDENCE_STATUS = {"verified", "unverified", "contradicted"}
ALLOWED_CLAIM_SCOPES = {
    "changed_region",
    "bounded_context",
    "whole_file",
    "repository",
}
ALLOWED_VISIBILITY = {"inline", "headline", "collapsed"}
FINDING_ID_RE = re.compile(r"^F[1-9][0-9]*$")
UNKNOWN_ID_RE = re.compile(r"^U[1-9][0-9]*$")

MAX_DECISION_REASONS = 3
MAX_OWNER_ACTIONS = 2
MAX_HEADLINE_FINDINGS = 2
MAX_INLINE_FINDINGS = 4
MAX_NONBLOCKING_INLINE_FINDINGS = 1
MAX_VISIBLE_SCOPE_ITEMS = 6

_CLEAR_SENTENCE_PREFIX = "No review blocker found"


@dataclass(slots=True)
class V3ValidationState:
    """Transient facts shared by the ordered validation capabilities."""

    raw: Dict[str, Any]
    context_meta: Optional[Dict[str, Any]]
    pr_details: str
    violations: List[ReviewContractViolation] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    catalog: Dict[str, Dict[str, Any]] = field(init=False)
    expected_head: str = field(init=False)
    referenced_head_shas: set[str] = field(default_factory=set)
    decision: Dict[str, Any] = field(default_factory=dict)
    findings: List[Any] = field(default_factory=list)
    finding_ids: set[str] = field(default_factory=set)
    blocking_ids: set[str] = field(default_factory=set)
    unknowns: List[Any] = field(default_factory=list)
    unknown_ids: set[str] = field(default_factory=set)
    merge_unknown_ids: set[str] = field(default_factory=set)
    reasons: Any = None
    owner_action: List[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.catalog = catalog_entries(self.context_meta)
        self.expected_head = expected_head_sha(self.context_meta)


def text(value: Any) -> str:
    return str(value or "").strip()


def has_clear_sentence_prefix(value: Any) -> bool:
    rendered = text(value)
    if not rendered.casefold().startswith(_CLEAR_SENTENCE_PREFIX.casefold()):
        return False
    boundary = rendered[len(_CLEAR_SENTENCE_PREFIX) :]
    return not boundary or boundary[0].isspace() or boundary[0] in ".,:;!—-"


def violation(
    code: str,
    location: str,
    message: str,
) -> ReviewContractViolation:
    return ReviewContractViolation(
        code=code,
        location=location,
        message=message,
    )


def raise_validation(
    violations: Sequence[ReviewContractViolation],
    warnings: Sequence[str],
) -> None:
    raise ReviewContractError(violations, warnings)


def validate_nonempty_string(
    value: Any,
    location: str,
    violations: List[ReviewContractViolation],
) -> None:
    if not isinstance(value, str) or not value.strip():
        violations.append(
            violation(
                "field_type_invalid",
                location,
                f"{location} must be a non-empty string",
            )
        )


def validate_string_refs(
    value: Any,
    location: str,
    violations: List[ReviewContractViolation],
    *,
    allowed: Optional[set[str]] = None,
    require_nonempty: bool = False,
) -> List[str]:
    if not isinstance(value, list):
        violations.append(
            violation(
                "field_type_invalid",
                location,
                f"{location} must be an array",
            )
        )
        return []
    if require_nonempty and not value:
        violations.append(
            violation(
                "cross_field_invariant",
                location,
                f"{location} must reference at least one existing item",
            )
        )
    refs: List[str] = []
    seen: set[str] = set()
    for index, raw_ref in enumerate(value):
        ref_location = f"{location}[{index}]"
        if not isinstance(raw_ref, str) or not raw_ref.strip():
            violations.append(
                violation(
                    "field_type_invalid",
                    ref_location,
                    f"{ref_location} must be a non-empty string",
                )
            )
            continue
        ref = raw_ref.strip()
        if ref in seen:
            violations.append(
                violation(
                    "cross_field_invariant",
                    ref_location,
                    f"{ref_location} duplicates {ref!r}",
                )
            )
            continue
        seen.add(ref)
        refs.append(ref)
        if allowed is not None and ref not in allowed:
            violations.append(
                violation(
                    "evidence_ref_invalid",
                    ref_location,
                    f"{ref_location} references unknown id {ref!r}",
                )
            )
    return refs

"""Cross-field relationship validation for public review schema v3."""

from __future__ import annotations

from typing import Any, Sequence

from .v3_validation_common import (
    ALLOWED_MODEL_VERDICTS,
    MAX_DECISION_REASONS,
    MAX_OWNER_ACTIONS,
    V3ValidationState,
    validate_nonempty_string,
    validate_string_refs,
    violation,
)


def relationship_verdict(
    findings: Sequence[Any],
    unknowns: Sequence[Any],
) -> str:
    if any(
        isinstance(item, dict)
        and item.get("blocking") is True
        and item.get("evidence_status") == "verified"
        for item in findings
    ):
        return "blocked_findings"
    if any(
        isinstance(item, dict) and item.get("affects_merge") is True
        for item in unknowns
    ):
        return "unverified"
    return "clear"


def validate_reasons_and_owner_actions(state: V3ValidationState) -> None:
    """Validate references after finding and unknown identities are known."""

    raw = state.raw
    decision = state.decision
    violations = state.violations
    warnings = state.warnings
    all_item_ids = state.finding_ids | state.unknown_ids

    reasons = decision.get("reasons") if isinstance(decision, dict) else None
    if not isinstance(reasons, list):
        violations.append(
            violation(
                "field_type_invalid",
                "$.decision.reasons",
                "decision.reasons must be an array",
            )
        )
    else:
        if len(reasons) > MAX_DECISION_REASONS:
            violations.append(
                violation(
                    "visibility_cap_exceeded",
                    "$.decision.reasons",
                    "decision reason cap exceeded",
                )
            )
        for index, reason in enumerate(reasons):
            location = f"$.decision.reasons[{index}]"
            if not isinstance(reason, dict):
                violations.append(
                    violation(
                        "field_type_invalid",
                        location,
                        f"{location} must be an object",
                    )
                )
                continue
            warnings.extend(
                f"ignored extra decision.reasons[{index}] field: {key}"
                for key in sorted(set(reason) - {"text", "refs"})
            )
            validate_nonempty_string(
                reason.get("text"),
                f"{location}.text",
                violations,
            )
            validate_string_refs(
                reason.get("refs"),
                f"{location}.refs",
                violations,
                allowed=all_item_ids,
            )
    state.reasons = reasons

    owner_action = raw.get("owner_action")
    if not isinstance(owner_action, list):
        violations.append(
            violation(
                "field_type_invalid",
                "$.owner_action",
                "owner_action must be an array",
            )
        )
        owner_action = []
    elif len(owner_action) > MAX_OWNER_ACTIONS:
        violations.append(
            violation(
                "visibility_cap_exceeded",
                "$.owner_action",
                "owner_action cap exceeded",
            )
        )
    resolvable_ids = state.blocking_ids | state.merge_unknown_ids
    for index, action in enumerate(owner_action):
        location = f"$.owner_action[{index}]"
        if not isinstance(action, dict):
            violations.append(
                violation(
                    "field_type_invalid",
                    location,
                    f"{location} must be an object",
                )
            )
            continue
        warnings.extend(
            f"ignored extra owner_action[{index}] field: {key}"
            for key in sorted(set(action) - {"text", "resolves"})
        )
        validate_nonempty_string(
            action.get("text"),
            f"{location}.text",
            violations,
        )
        validate_string_refs(
            action.get("resolves"),
            f"{location}.resolves",
            violations,
            allowed=resolvable_ids,
            require_nonempty=True,
        )
    state.owner_action = owner_action


def validate_final_relationships(state: V3ValidationState) -> None:
    """Validate verdict, reason, and action relationships last."""

    decision = state.decision
    findings = state.findings
    unknowns = state.unknowns
    reasons = state.reasons
    owner_action = state.owner_action
    violations = state.violations

    verdict = decision.get("verdict") if isinstance(decision, dict) else None
    expected_verdict = relationship_verdict(findings, unknowns)
    if verdict in ALLOWED_MODEL_VERDICTS and verdict != expected_verdict:
        violations.append(
            violation(
                "decision_relation_mismatch",
                "$.decision.verdict",
                "decision.verdict conflicts with structured blocking/unknown truth",
            )
        )
    reason_refs = {
        str(ref)
        for reason in (reasons if isinstance(reasons, list) else [])
        if isinstance(reason, dict)
        for ref in reason.get("refs") or []
        if isinstance(ref, str)
    }
    required_reason_refs = (
        state.blocking_ids
        if expected_verdict == "blocked_findings"
        else state.merge_unknown_ids
    )
    if required_reason_refs and not (reason_refs & required_reason_refs):
        violations.append(
            violation(
                "cross_field_invariant",
                "$.decision.reasons",
                "decision reasons do not reference the item that determines the verdict",
            )
        )
    resolvable_ids = state.blocking_ids | state.merge_unknown_ids
    if owner_action and not resolvable_ids:
        violations.append(
            violation(
                "cross_field_invariant",
                "$.owner_action",
                "owner_action requires a blocking finding or merge-affecting unknown",
            )
        )

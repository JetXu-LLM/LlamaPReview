"""Root and decision-shape validation for public review schema v3."""

from __future__ import annotations

from .schema import ALLOWED_PR_TYPES
from .v3_validation_common import (
    ALLOWED_MODEL_VERDICTS,
    SCHEMA_VERSION,
    V3ValidationState,
    has_clear_sentence_prefix,
    validate_nonempty_string,
    validate_string_refs,
    violation,
)


def validate_root_and_decision(state: V3ValidationState) -> None:
    """Validate root and decision fields in their canonical order."""

    raw = state.raw
    violations = state.violations
    warnings = state.warnings
    required_root = {
        "schema_version",
        "decision",
        "owner_action",
        "findings",
        "material_unknowns",
        "evidence_scope",
        "diagram",
    }
    allowed_root = required_root | {
        "rendering_plan",
        "visible_projection_source",
    }
    for key in sorted(required_root - set(raw)):
        violations.append(
            violation(
                "required_field_missing",
                f"$.{key}",
                f"missing required root field: {key}",
            )
        )
    warnings.extend(
        f"ignored extra root field: {key}"
        for key in sorted(set(raw) - allowed_root)
    )
    if type(raw.get("schema_version")) is not int or raw.get(
        "schema_version"
    ) != SCHEMA_VERSION:
        violations.append(
            violation(
                "field_type_invalid",
                "$.schema_version",
                "schema_version must be integer 3",
            )
        )

    decision = raw.get("decision")
    required_decision = {
        "verdict",
        "public_sentence",
        "confidence",
        "pr_type",
        "risk_domains",
        "reasons",
    }
    if not isinstance(decision, dict):
        violations.append(
            violation(
                "field_type_invalid",
                "$.decision",
                "decision must be an object",
            )
        )
        decision = {}
    else:
        for key in sorted(required_decision - set(decision)):
            violations.append(
                violation(
                    "required_field_missing",
                    f"$.decision.{key}",
                    f"missing required decision field: {key}",
                )
            )
        warnings.extend(
            f"ignored extra decision field: {key}"
            for key in sorted(set(decision) - required_decision)
        )
    state.decision = decision

    if not decision:
        return
    if decision.get("verdict") not in ALLOWED_MODEL_VERDICTS:
        violations.append(
            violation(
                "enum_invalid",
                "$.decision.verdict",
                "decision.verdict has an invalid enum value",
            )
        )
    validate_nonempty_string(
        decision.get("public_sentence"),
        "$.decision.public_sentence",
        violations,
    )
    if (
        decision.get("verdict") == "clear"
        and isinstance(decision.get("public_sentence"), str)
        and not has_clear_sentence_prefix(decision["public_sentence"])
    ):
        violations.append(
            violation(
                "visibility_contract_invalid",
                "$.decision.public_sentence",
                "clear public_sentence must begin 'No review blocker found'",
            )
        )
    if decision.get("confidence") not in {"high", "medium", "low"}:
        violations.append(
            violation(
                "enum_invalid",
                "$.decision.confidence",
                "decision.confidence has an invalid enum value",
            )
        )
    if decision.get("pr_type") not in ALLOWED_PR_TYPES:
        violations.append(
            violation(
                "enum_invalid",
                "$.decision.pr_type",
                "decision.pr_type has an invalid enum value",
            )
        )
    validate_string_refs(
        decision.get("risk_domains"),
        "$.decision.risk_domains",
        violations,
    )

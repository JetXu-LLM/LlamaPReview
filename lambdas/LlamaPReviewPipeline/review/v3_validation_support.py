"""Unknown, visible-scope, and diagram validation for public schema v3."""

from __future__ import annotations

from .evidence_contract import (
    entry_head_sha,
    entry_is_objectively_renderable,
    entry_supports_claim,
    entry_supports_expected_head,
)
from .rendering_safety import format_mermaid
from .v3_validation_common import (
    ALLOWED_DIAGRAM_PURPOSES,
    MAX_VISIBLE_SCOPE_ITEMS,
    UNKNOWN_ID_RE,
    V3ValidationState,
    text,
    validate_nonempty_string,
    validate_string_refs,
    violation,
)


def validate_unknowns(state: V3ValidationState) -> None:
    """Validate material unknowns before dependent decision references."""

    raw = state.raw
    violations = state.violations
    warnings = state.warnings
    catalog = state.catalog
    unknowns = raw.get("material_unknowns")
    unknown_ids: set[str] = set()
    merge_unknown_ids: set[str] = set()
    if not isinstance(unknowns, list):
        violations.append(
            violation(
                "field_type_invalid",
                "$.material_unknowns",
                "material_unknowns must be an array",
            )
        )
        unknowns = []
    required_unknown = {
        "id",
        "claim",
        "how_to_check",
        "affects_merge",
        "evidence_refs",
    }
    for index, unknown in enumerate(unknowns):
        location = f"$.material_unknowns[{index}]"
        if not isinstance(unknown, dict):
            violations.append(
                violation(
                    "field_type_invalid",
                    location,
                    f"{location} must be an object",
                )
            )
            continue
        for key in sorted(required_unknown - set(unknown)):
            violations.append(
                violation(
                    "required_field_missing",
                    f"{location}.{key}",
                    f"{location}.{key} is required",
                )
            )
        warnings.extend(
            f"ignored extra material_unknowns[{index}] field: {key}"
            for key in sorted(set(unknown) - required_unknown)
        )
        unknown_id = text(unknown.get("id"))
        if not UNKNOWN_ID_RE.fullmatch(unknown_id):
            violations.append(
                violation(
                    "id_contract_invalid",
                    f"{location}.id",
                    f"{location}.id must match U[1-9][0-9]*",
                )
            )
        elif unknown_id in unknown_ids:
            violations.append(
                violation(
                    "id_contract_invalid",
                    f"{location}.id",
                    f"duplicate unknown id {unknown_id!r}",
                )
            )
        else:
            unknown_ids.add(unknown_id)
        validate_nonempty_string(
            unknown.get("claim"),
            f"{location}.claim",
            violations,
        )
        validate_nonempty_string(
            unknown.get("how_to_check"),
            f"{location}.how_to_check",
            violations,
        )
        if type(unknown.get("affects_merge")) is not bool:
            violations.append(
                violation(
                    "field_type_invalid",
                    f"{location}.affects_merge",
                    f"{location}.affects_merge must be a boolean",
                )
            )
        elif unknown.get("affects_merge") is True and unknown_id:
            merge_unknown_ids.add(unknown_id)
        unknown_evidence_refs = validate_string_refs(
            unknown.get("evidence_refs"),
            f"{location}.evidence_refs",
            violations,
            allowed=set(catalog),
        )
        # A material unknown may cite an exact cataloged gap/error event. Such
        # an event cannot prove a finding, but it can honestly explain why a
        # fact remains unverified.

    state.unknowns = unknowns
    state.unknown_ids = unknown_ids
    state.merge_unknown_ids = merge_unknown_ids


def validate_scope_and_diagram(state: V3ValidationState) -> None:
    """Validate visible evidence scope, optional diagram, and head cohesion."""

    raw = state.raw
    violations = state.violations
    warnings = state.warnings
    catalog = state.catalog
    expected_head = state.expected_head

    scope_refs = validate_string_refs(
        raw.get("evidence_scope"),
        "$.evidence_scope",
        violations,
        allowed=set(catalog),
    )
    if len(scope_refs) > MAX_VISIBLE_SCOPE_ITEMS:
        violations.append(
            violation(
                "visibility_cap_exceeded",
                "$.evidence_scope",
                "evidence_scope visibility cap exceeded",
            )
        )
    for index, ref in enumerate(scope_refs):
        entry = catalog.get(ref) or {}
        if not entry_is_objectively_renderable(entry):
            violations.append(
                violation(
                    "evidence_scope_provenance_mismatch",
                    f"$.evidence_scope[{index}]",
                    "evidence_scope ref lacks objective supporting path coverage",
                )
            )
        if not entry_supports_expected_head(
            entry,
            expected_head=expected_head,
        ):
            violations.append(
                violation(
                    "evidence_head_lineage_missing",
                    f"$.evidence_scope[{index}]",
                    "evidence_scope repository ref lacks exact queued-head lineage",
                )
            )
        entry_head = entry_head_sha(entry)
        if entry_head:
            state.referenced_head_shas.add(entry_head)
            if expected_head and entry_head != expected_head:
                violations.append(
                    violation(
                        "evidence_head_mismatch",
                        f"$.evidence_scope[{index}]",
                        "evidence_scope ref belongs to a different PR head",
                    )
                )

    diagram = raw.get("diagram")
    if diagram is not None:
        if not isinstance(diagram, dict):
            violations.append(
                violation(
                    "diagram_contract_invalid",
                    "$.diagram",
                    "diagram must be an object or null",
                )
            )
        else:
            allowed_diagram = {
                "purpose",
                "description",
                "mermaid",
                "finding_refs",
                "evidence_refs",
            }
            warnings.extend(
                f"ignored extra diagram field: {key}"
                for key in sorted(set(diagram) - allowed_diagram)
            )
            if diagram.get("purpose") not in ALLOWED_DIAGRAM_PURPOSES:
                violations.append(
                    violation(
                        "diagram_contract_invalid",
                        "$.diagram.purpose",
                        "diagram.purpose has an invalid enum value",
                    )
                )
            validate_nonempty_string(
                diagram.get("description"),
                "$.diagram.description",
                violations,
            )
            mermaid = diagram.get("mermaid")
            if not isinstance(mermaid, str) or not format_mermaid(
                mermaid,
                strict=True,
                treat_unknown_as_error=True,
                github_flavor=True,
                auto_insert_sequence_header=False,
                auto_strip_leading_noise=False,
                github_convert_multiline_notes=True,
            ):
                violations.append(
                    violation(
                        "diagram_contract_invalid",
                        "$.diagram.mermaid",
                        "diagram.mermaid is not GitHub-safe sequenceDiagram syntax",
                    )
                )
            diagram_findings = validate_string_refs(
                diagram.get("finding_refs"),
                "$.diagram.finding_refs",
                violations,
                allowed=state.finding_ids,
            )
            diagram_evidence = validate_string_refs(
                diagram.get("evidence_refs"),
                "$.diagram.evidence_refs",
                violations,
                allowed=set(catalog),
            )
            if not diagram_findings and not diagram_evidence:
                violations.append(
                    violation(
                        "diagram_contract_invalid",
                        "$.diagram",
                        "diagram must reference a finding or catalog evidence",
                    )
                )
            for ref in diagram_evidence:
                entry = catalog.get(ref) or {}
                if not entry_supports_claim(entry):
                    violations.append(
                        violation(
                            "evidence_ref_invalid",
                            "$.diagram.evidence_refs",
                            "diagram evidence includes a non-supporting outcome",
                        )
                    )
                if not entry_supports_expected_head(
                    entry,
                    expected_head=expected_head,
                ):
                    violations.append(
                        violation(
                            "evidence_head_lineage_missing",
                            "$.diagram.evidence_refs",
                            "diagram repository evidence lacks exact queued-head lineage",
                        )
                    )
                entry_head = entry_head_sha(entry)
                if entry_head:
                    state.referenced_head_shas.add(entry_head)
                    if expected_head and entry_head != expected_head:
                        violations.append(
                            violation(
                                "evidence_head_mismatch",
                                "$.diagram.evidence_refs",
                                "diagram evidence belongs to a different PR head",
                            )
                        )

    if "rendering_plan" in raw and not isinstance(
        raw.get("rendering_plan"),
        dict,
    ):
        violations.append(
            violation(
                "field_type_invalid",
                "$.rendering_plan",
                "rendering_plan must be an object",
            )
        )
    if len(state.referenced_head_shas) > 1:
        violations.append(
            violation(
                "evidence_head_mismatch",
                "$.evidence_scope",
                "review references evidence from multiple PR heads",
            )
        )

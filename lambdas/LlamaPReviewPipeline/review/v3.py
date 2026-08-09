"""Public facade for review-v3 validation and preparation.

Review v3 deliberately separates two responsibilities:

* the model owns engineering judgment and human-facing prose; and
* deterministic code owns JSON shape, identities, cross references, exact
  artifact provenance, GitHub-safe rendering, and visibility limits.

The ordered validator delegates stable capabilities to focused modules while
this facade preserves the public API used by projection and tests. Markdown
and publish-JSON composition live in :mod:`review.render`.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from .evidence_contract import ReviewContractError, ReviewContractViolation
from .rendering_safety import format_mermaid
from .schema import (
    ALLOWED_PR_TYPES,
    PR_TYPE_ALIASES,
    normalize_pr_type,
)
from .v3_validation_common import (
    ALLOWED_CLAIM_SCOPES,
    ALLOWED_CONFIDENCE,
    ALLOWED_DIAGRAM_PURPOSES,
    ALLOWED_EVIDENCE_STATUS,
    ALLOWED_FINDING_TYPES,
    ALLOWED_MODEL_VERDICTS,
    ALLOWED_PRIORITIES,
    ALLOWED_VISIBILITY,
    BLOCKING_PRIORITIES,
    FINDING_ID_RE,
    MAX_DECISION_REASONS,
    MAX_HEADLINE_FINDINGS,
    MAX_INLINE_FINDINGS,
    MAX_NONBLOCKING_INLINE_FINDINGS,
    MAX_OWNER_ACTIONS,
    MAX_VISIBLE_SCOPE_ITEMS,
    SCHEMA_VERSION,
    UNKNOWN_ID_RE,
    V3ValidationState as _V3ValidationState,
    raise_validation as _raise_validation,
    violation as _violation,
)
from .v3_validation_findings import (
    finding_evidence_capability,
    validate_findings as _validate_findings,
)
from .v3_validation_relations import (
    relationship_verdict,
    validate_final_relationships as _validate_final_relationships,
    validate_reasons_and_owner_actions as _validate_reasons_and_owner_actions,
)
from .v3_validation_root import (
    validate_root_and_decision as _validate_root_and_decision,
)
from .v3_validation_support import (
    validate_scope_and_diagram as _validate_scope_and_diagram,
    validate_unknowns as _validate_unknowns,
)


def validate_raw_v3_review(
    raw: Any,
    *,
    context_meta: Optional[Dict[str, Any]] = None,
    pr_details: str = "",
) -> List[str]:
    """Validate only mechanical v3 truth, shape, and visibility invariants."""

    if not isinstance(raw, dict):
        _raise_validation(
            [
                _violation(
                    "json_root_type_invalid",
                    "$",
                    "root must be an object",
                )
            ],
            [],
        )

    state = _V3ValidationState(
        raw=raw,
        context_meta=context_meta,
        pr_details=pr_details,
    )
    _validate_root_and_decision(state)
    _validate_findings(state)
    _validate_unknowns(state)
    _validate_reasons_and_owner_actions(state)
    _validate_scope_and_diagram(state)
    _validate_final_relationships(state)
    if state.violations:
        _raise_validation(state.violations, state.warnings)
    return state.warnings


def prepare_raw_v3_for_strict_validation(
    raw: Any,
    context_meta: Optional[Dict[str, Any]] = None,
    *,
    pr_details: str = "",
) -> Tuple[Any, List[str]]:
    """Apply representation-only and analyzer-owned v3 preparation."""

    working = deepcopy(raw)
    warnings: List[str] = []
    if not isinstance(working, dict):
        return working, warnings

    if isinstance(working.get("schema_version"), str) and working[
        "schema_version"
    ].strip() == "3":
        working["schema_version"] = 3
        warnings.append(
            "schema_representation_repair: schema_version numeric_string"
        )

    decision = working.get("decision")
    if isinstance(decision, dict):
        analyzer = (context_meta or {}).get("analyzer_result") or {}
        risk_domains = (
            analyzer.get("risk_domains")
            if isinstance(analyzer, dict)
            else []
        )
        decision["risk_domains"] = list(
            dict.fromkeys(
                str(item).strip().lower()
                for item in (
                    risk_domains if isinstance(risk_domains, list) else []
                )
                if isinstance(item, str) and item.strip()
            )
        )[:8]
        raw_pr_type = decision.get("pr_type")
        normalized = normalize_pr_type(raw_pr_type)
        analyzer_pr_type = normalize_pr_type(
            analyzer.get("pr_type")
            if isinstance(analyzer, dict)
            else None
        )
        if (
            isinstance(raw_pr_type, str)
            and raw_pr_type.strip().lower() in PR_TYPE_ALIASES
            and normalized in ALLOWED_PR_TYPES
            and normalized == analyzer_pr_type
        ):
            decision["pr_type"] = normalized
            warnings.append(
                "schema_alias_repair: decision.pr_type matched analyzer route"
            )

    def normalize_bool(container: Any, key: str, label: str) -> None:
        if not isinstance(container, dict) or not isinstance(
            container.get(key),
            str,
        ):
            return
        value = container[key].strip().lower()
        if value not in {"true", "false"}:
            return
        container[key] = value == "true"
        warnings.append(
            f"schema_representation_repair: {label} string_boolean"
        )

    for index, finding in enumerate(
        working.get("findings")
        if isinstance(working.get("findings"), list)
        else []
    ):
        normalize_bool(
            finding,
            "blocking",
            f"findings[{index}].blocking",
        )
    for index, unknown in enumerate(
        working.get("material_unknowns")
        if isinstance(working.get("material_unknowns"), list)
        else []
    ):
        normalize_bool(
            unknown,
            "affects_merge",
            f"material_unknowns[{index}].affects_merge",
        )

    return working, warnings

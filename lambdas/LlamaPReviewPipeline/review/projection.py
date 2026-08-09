"""Validated public-v3 projection and exact-CI local degradation.

This module turns one fixed, model-owned presentation into the stable public-v3
artifact. It owns deterministic identities/provenance projection, optional
surface contraction, exact-CI dependency invalidation, publication sanitation,
and rendering handoff. It never re-adjudicates the model verdict.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .evidence_contract import (
    catalog_entries as _catalog_entries,
    classify_changed_region_anchor as _classify_changed_region_anchor,
    entry_paths as _entry_paths,
    entry_supports_claim as _entry_supports_claim,
)
from .rendering_safety import format_mermaid
from .render import project_v3_to_publish_json
from .v3 import (
    ALLOWED_MODEL_VERDICTS,
    MAX_DECISION_REASONS,
    MAX_OWNER_ACTIONS,
    SCHEMA_VERSION,
    finding_evidence_capability,
    prepare_raw_v3_for_strict_validation,
    validate_raw_v3_review,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def elect_primary_inline(
    findings: Sequence[Dict[str, Any]],
    *,
    verdict: str,
    pr_details: str,
    context_meta: Optional[Dict[str, Any]],
) -> bool:
    """Promote one eligible verified finding to inline when none is inline.

    Presentation is code-owned once Final has supplied a verified causal
    finding and a mechanically valid post-change anchor. Do not let a
    conservative model presentation choice waste the single highest-value
    actionable inline opportunity, but never manufacture an inline for clean,
    unverified, low-confidence, informational, or deleted-region output.
    The same election runs at compile time and again after a terminal CI
    refresh removes findings, so an invalidated primary inline is re-elected
    from the survivors. Returns True when a promotion happened.
    """

    if verdict != "blocked_findings":
        return False
    items = [item for item in findings if isinstance(item, dict)]
    if any(item.get("visibility") == "inline" for item in items):
        return False
    eligible = [
        (index, finding)
        for index, finding in enumerate(items)
        if (
            finding.get("finding_type")
            in {"bug", "security", "breaking-change"}
            and finding.get("evidence_status") == "verified"
            and finding.get("confidence") in {"High", "Medium"}
            and bool(finding.get("code_snippet"))
            and bool(finding.get("comment"))
            and finding_evidence_capability(
                finding,
                pr_details=pr_details,
                context_meta=context_meta,
            )["snippet_is_post_change"]
        )
    ]
    if not eligible:
        return False
    priority_rank = {"P0": 0, "P1": 1, "P2": 2}
    confidence_rank = {"High": 0, "Medium": 1}
    _, primary = min(
        eligible,
        key=lambda pair: (
            0 if pair[1].get("blocking") else 1,
            priority_rank.get(str(pair[1].get("priority")), 3),
            confidence_rank.get(str(pair[1].get("confidence")), 2),
            pair[0],
        ),
    )
    primary["visibility"] = "inline"
    return True



@dataclass(frozen=True, slots=True)
class V3CIEvidenceInvalidation:
    """Typed result of exact-head CI dependency invalidation."""

    review: Dict[str, Any]
    invalidated_item_ids: Tuple[str, ...] = ()
    dropped_supporting_refs: Tuple[Tuple[str, str], ...] = ()
    status: str = "unchanged"
    reason_code: str = ""

    @property
    def publishable(self) -> bool:
        return self.status != "nonpublishable"


def _finding_evidence_dependencies(
    finding: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    """Return required/supporting refs, conservatively reading old artifacts."""

    split_declared = (
        "required_evidence_refs" in finding
        or "supporting_evidence_refs" in finding
    )
    if split_declared:
        required = [
            str(ref)
            for ref in finding.get("required_evidence_refs") or []
            if isinstance(ref, str)
        ]
        supporting = [
            str(ref)
            for ref in finding.get("supporting_evidence_refs") or []
            if isinstance(ref, str)
        ]
        return required, supporting
    # A stored pre-split v3 artifact cannot prove that a citation was merely
    # decorative. Treat every citation as required so refresh can only
    # contract publication, never synthesize a safer judgment.
    return (
        [
            str(ref)
            for ref in finding.get("evidence_refs") or []
            if isinstance(ref, str)
        ],
        [],
    )


def _refs_intersect(
    items: Any,
    *,
    key: str,
    changed: set[str],
) -> bool:
    return bool(
        isinstance(items, dict)
        and {
            str(ref)
            for ref in items.get(key) or []
            if isinstance(ref, str)
        }
        & changed
    )


def invalidate_v3_changed_ci_evidence(
    raw: Dict[str, Any],
    *,
    changed_ci_refs: Iterable[str],
) -> V3CIEvidenceInvalidation:
    """Apply exact CI churn according to model-declared dependencies.

    Supporting churn removes only the optional citation. Required churn
    invalidates only the dependent finding and its inseparable free-form
    surfaces. The model verdict is never recomputed. If the retained
    presentation can no longer carry that verdict, the typed result is
    nonpublishable and the caller must persist a generation failure rather
    than publish code-authored clear/unverified copy.
    """

    if not isinstance(raw, dict):
        raise TypeError("raw v3 review must be an object")
    changed = {
        str(ref)
        for ref in changed_ci_refs
        if isinstance(ref, str) and ref.startswith("ci:")
    }
    working = deepcopy(raw)
    if not changed:
        return V3CIEvidenceInvalidation(review=working)

    retained_findings: List[Dict[str, Any]] = []
    invalidated_ids: List[str] = []
    dropped_supporting: List[Tuple[str, str]] = []
    invalidated_deciding_ids: set[str] = set()
    model_verdict = _text(
        (working.get("decision") or {}).get("verdict")
        if isinstance(working.get("decision"), dict)
        else ""
    )

    for finding in working.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        finding_id = _text(finding.get("id")) or "unknown"
        required, supporting = _finding_evidence_dependencies(finding)
        changed_required = set(required) & changed
        if changed_required:
            invalidated_ids.append(finding_id)
            if (
                model_verdict == "blocked_findings"
                and finding.get("blocking") is True
                and finding.get("evidence_status") == "verified"
            ):
                invalidated_deciding_ids.add(finding_id)
            continue
        retained_supporting = [
            ref for ref in supporting if ref not in changed
        ]
        for ref in supporting:
            if ref in changed:
                dropped_supporting.append((finding_id, ref))
        if (
            "required_evidence_refs" in finding
            or "supporting_evidence_refs" in finding
        ):
            finding["required_evidence_refs"] = required
            finding["supporting_evidence_refs"] = retained_supporting
            finding["evidence_refs"] = list(
                dict.fromkeys(required + retained_supporting)
            )
        retained_findings.append(finding)
    working["findings"] = retained_findings

    invalidated = set(invalidated_ids)
    decision = working.get("decision")
    if isinstance(decision, dict):
        decision["reasons"] = [
            reason
            for reason in decision.get("reasons") or []
            if isinstance(reason, dict)
            and not _refs_intersect(
                reason,
                key="refs",
                changed=invalidated,
            )
        ]
    working["owner_action"] = [
        action
        for action in working.get("owner_action") or []
        if isinstance(action, dict)
        and not _refs_intersect(
            action,
            key="resolves",
            changed=invalidated,
        )
    ]
    working["evidence_scope"] = [
        ref
        for ref in working.get("evidence_scope") or []
        if not (isinstance(ref, str) and ref in changed)
    ]

    diagram = working.get("diagram")
    if isinstance(diagram, dict) and (
        _refs_intersect(
            diagram,
            key="finding_refs",
            changed=invalidated,
        )
        or _refs_intersect(
            diagram,
            key="evidence_refs",
            changed=changed,
        )
    ):
        working["diagram"] = None

    if not invalidated_ids and not dropped_supporting:
        return V3CIEvidenceInvalidation(review=working)

    status = "locally_degraded"
    reason_code = (
        "supporting_ci_evidence_changed"
        if dropped_supporting and not invalidated_ids
        else "required_ci_evidence_changed"
    )
    if model_verdict == "blocked_findings" and invalidated_deciding_ids:
        retained_deciding_ids = {
            _text(item.get("id"))
            for item in retained_findings
            if item.get("blocking") is True
            and item.get("evidence_status") == "verified"
        }
        reason_refs = {
            str(ref)
            for reason in (
                decision.get("reasons") or []
                if isinstance(decision, dict)
                else []
            )
            if isinstance(reason, dict)
            for ref in reason.get("refs") or []
            if isinstance(ref, str)
        }
        action_refs = {
            str(ref)
            for action in working.get("owner_action") or []
            if isinstance(action, dict)
            for ref in action.get("resolves") or []
            if isinstance(ref, str)
        }
        if not retained_deciding_ids:
            status = "nonpublishable"
            reason_code = "last_deciding_item_required_ci_changed"
        elif not (
            reason_refs & retained_deciding_ids
            and action_refs & retained_deciding_ids
        ):
            status = "nonpublishable"
            reason_code = "retained_decision_surface_not_trustworthy"
        else:
            status = "deciding_item_degraded"
            reason_code = "deciding_item_required_ci_changed"

    return V3CIEvidenceInvalidation(
        review=working,
        invalidated_item_ids=tuple(invalidated_ids),
        dropped_supporting_refs=tuple(dropped_supporting),
        status=status,
        reason_code=reason_code,
    )


def _objective_scope_description(entry: Dict[str, Any]) -> str:
    if not _entry_supports_claim(entry):
        return ""
    paths = _entry_paths(entry)
    if not paths:
        return ""
    safe_paths = [
        path.replace("`", "'").replace("\n", " ").replace("\r", " ")
        for path in paths[:3]
    ]
    rendered_paths = ", ".join(f"`{path}`" for path in safe_paths)
    if len(paths) > 3:
        rendered_paths += f" and {len(paths) - 3} more path(s)"
    coverage = _text(entry.get("coverage_type"))
    if coverage == "changed_region":
        return f"Reviewed changed regions in {rendered_paths}."
    if coverage == "full_file":
        return f"Read the complete PR-head file {rendered_paths}."
    if coverage == "file_slice":
        return f"Read bounded PR-head context from {rendered_paths}."
    if coverage == "search_snippet":
        relocation = _text(entry.get("head_reread_outcome"))
        if relocation == "relocated_at_head":
            return f"Inspected matching PR-head repository snippets in {rendered_paths}."
        if relocation == "partial_head_relocation":
            return f"Inspected matching snippets in {rendered_paths}; only part of the search result was relocated to the PR head."
        if _text(entry.get("source_ref")) == "default_branch_search":
            return f"Inspected matching default-branch snippets in {rendered_paths}; PR-head relocation was unavailable."
        return f"Inspected matching repository snippets in {rendered_paths}."
    if coverage == "directory_inventory":
        return f"Inspected bounded directory inventory under {rendered_paths}."
    if coverage == "exact_path_state":
        observed = _text(entry.get("observed_state") or entry.get("exact_path_state"))
        if observed in {"present", "absent"}:
            return f"Confirmed the exact PR-head path is {observed}: {rendered_paths}."
    return ""


def project_evidence_scope(
    refs: Sequence[Any],
    context_meta: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Project model-selected IDs into catalog-owned objective descriptions."""

    catalog = _catalog_entries(context_meta)
    projected: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw_ref in refs:
        if not isinstance(raw_ref, str) or raw_ref in seen:
            continue
        seen.add(raw_ref)
        entry = catalog.get(raw_ref) or {}
        description = _objective_scope_description(entry)
        if not description:
            continue
        projected.append(
            {
                "evidence_ref": raw_ref,
                "source": _text(entry.get("source_type")),
                "coverage_type": _text(entry.get("coverage_type")),
                "paths": _entry_paths(entry),
                "description": description,
            }
        )
    return projected


def normalize_v3_review(
    raw: Dict[str, Any],
    pr_details: str,
    context_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Project a validated model decision without re-adjudicating it.

    The caller validates the fixed presentation object before reaching this
    boundary.  Normalization may contract optional presentation surfaces, but
    it never changes verdict, severity, blocking status, evidence status, or
    model prose to manufacture a replacement decision.
    """

    from .schema import (
        has_unresolved_replacement_placeholder,
        is_local_direct_replacement,
    )

    warnings = [
        str(item)
        for item in raw.get("review_quality_warnings") or []
        if isinstance(item, str)
    ]
    findings = [
        deepcopy(item)
        for item in raw.get("findings") or []
        if isinstance(item, dict)
    ]
    for finding in findings:
        anchor_class = _classify_changed_region_anchor(
            _text(finding.get("file_path")),
            _text(finding.get("code_snippet")),
            pr_details,
        )
        if anchor_class == "deleted_region":
            if finding.get("visibility") == "inline":
                finding["visibility"] = (
                    "headline"
                    if finding.get("blocking") is True
                    else "collapsed"
                )
                warnings.append(
                    "v3_deleted_region: inline placement removed"
                )
            if finding.get("suggestion_type") == "DIRECT_REPLACEMENT":
                finding["suggestion_type"] = "CONCEPTUAL_ADVICE"
                warnings.append(
                    "v3_deleted_region: direct replacement rendered as "
                    "conceptual advice"
                )
        if finding.get("suggestion_type") != "DIRECT_REPLACEMENT":
            continue
        if has_unresolved_replacement_placeholder(
            finding.get("suggested_code")
        ) or not is_local_direct_replacement(
            finding.get("code_snippet"),
            finding.get("suggested_code"),
        ):
            finding["suggestion_type"] = "CONCEPTUAL_ADVICE"
            warnings.append(
                "v3_structural_projection: unsafe direct replacement "
                "rendered as conceptual advice"
            )

    unknowns = [
        deepcopy(item)
        for item in raw.get("material_unknowns") or []
        if isinstance(item, dict)
    ]
    live_ids = {
        _text(item.get("id"))
        for item in findings + unknowns
        if _text(item.get("id"))
    }
    raw_decision = (
        raw.get("decision") if isinstance(raw.get("decision"), dict) else {}
    )
    visible_verdict = _text(raw_decision.get("verdict"))
    decision = {
        "verdict": visible_verdict,
        "public_sentence": _text(raw_decision.get("public_sentence")),
        "confidence": _text(raw_decision.get("confidence")),
        "pr_type": _text(raw_decision.get("pr_type")),
        "risk_domains": [
            str(item)
            for item in raw_decision.get("risk_domains") or []
            if isinstance(item, str) and item.strip()
        ],
        "reasons": [
            {
                "text": _text(item.get("text")),
                "refs": [
                    str(ref)
                    for ref in item.get("refs") or []
                    if isinstance(ref, str) and ref in live_ids
                ],
            }
            for item in raw_decision.get("reasons") or []
            if isinstance(item, dict) and _text(item.get("text"))
        ][:MAX_DECISION_REASONS],
    }

    resolvable_ids = {
        _text(item.get("id"))
        for item in findings
        if item.get("blocking") is True
    } | {
        _text(item.get("id"))
        for item in unknowns
        if item.get("affects_merge") is True
    }
    owner_action = [
        {
            "text": _text(item.get("text")),
            "resolves": [
                str(ref)
                for ref in item.get("resolves") or []
                if isinstance(ref, str) and ref in resolvable_ids
            ],
        }
        for item in raw.get("owner_action") or []
        if isinstance(item, dict)
        and _text(item.get("text"))
        and any(
            isinstance(ref, str) and ref in resolvable_ids
            for ref in item.get("resolves") or []
        )
    ][:MAX_OWNER_ACTIONS]

    diagram = (
        deepcopy(raw.get("diagram"))
        if isinstance(raw.get("diagram"), dict)
        else None
    )
    if diagram:
        finding_ids = {
            _text(item.get("id")) for item in findings if _text(item.get("id"))
        }
        diagram["finding_refs"] = [
            str(ref)
            for ref in diagram.get("finding_refs") or []
            if isinstance(ref, str) and ref in finding_ids
        ]
        diagram["evidence_refs"] = [
            str(ref)
            for ref in diagram.get("evidence_refs") or []
            if isinstance(ref, str) and ref in _catalog_entries(context_meta)
        ]
        diagram["mermaid"] = format_mermaid(
            _text(diagram.get("mermaid")),
            strict=True,
            treat_unknown_as_error=True,
            github_flavor=True,
            auto_insert_sequence_header=False,
            auto_strip_leading_noise=False,
            github_convert_multiline_notes=True,
        )
        if not diagram["mermaid"] or not (
            diagram["finding_refs"] or diagram["evidence_refs"]
        ):
            diagram = None
            warnings.append(
                "v3_structural_projection: invalid diagram omitted"
            )

    selected_scope_refs = [
        str(ref)
        for ref in raw.get("evidence_scope") or []
        if isinstance(ref, str)
    ]
    requested_projection_source = _text(
        raw.get("visible_projection_source")
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "model_verdict": visible_verdict,
        "visible_verdict": visible_verdict,
        "decision": decision,
        "owner_action": owner_action,
        "findings": findings,
        "material_unknowns": unknowns,
        "evidence_scope": project_evidence_scope(
            selected_scope_refs,
            context_meta,
        ),
        "evidence_scope_refs": selected_scope_refs,
        "diagram": diagram,
        "rendering_plan": (
            raw.get("rendering_plan")
            if isinstance(raw.get("rendering_plan"), dict)
            else {}
        ),
        "visible_projection_source": (
            requested_projection_source
            if requested_projection_source
            else "model"
        ),
        "review_quality_warnings": list(dict.fromkeys(warnings)),
    }



def build_v3_review(
    raw: Dict[str, Any],
    pr_details: str,
    context_meta: Optional[Dict[str, Any]] = None,
    *,
    strict: bool = False,
    extra_private_identities: Iterable[str] = (),
) -> Dict[str, Any]:
    working = deepcopy(raw)
    if strict:
        decision = (
            working.get("decision")
            if isinstance(working.get("decision"), dict)
            else {}
        )
        primary_inline_elected = elect_primary_inline(
            working.get("findings") or [],
            verdict=_text(decision.get("verdict")),
            pr_details=pr_details,
            context_meta=context_meta,
        )
        working, warnings = prepare_raw_v3_for_strict_validation(
            working,
            context_meta,
            pr_details=pr_details,
        )
        if primary_inline_elected:
            warnings.append(
                "v3_structural_projection: primary inline elected"
            )
        warnings.extend(
            validate_raw_v3_review(
                working,
                context_meta=context_meta,
                pr_details=pr_details,
            )
        )
        if warnings:
            working["review_quality_warnings"] = list(dict.fromkeys(warnings))
    normalized = normalize_v3_review(
        working,
        pr_details,
        context_meta,
    )
    from .public_boundary import sanitize_review_for_publication

    normalized, private_identity_count = sanitize_review_for_publication(
        normalized,
        context_meta=context_meta,
        extra_private_identities=extra_private_identities,
    )
    if private_identity_count:
        normalized["review_quality_warnings"] = list(
            dict.fromkeys(
                list(normalized.get("review_quality_warnings") or [])
                + [
                    "public_identity_boundary: exact private identities "
                    f"removed from public prose ({private_identity_count})"
                ]
            )
        )
    return project_v3_to_publish_json(normalized, context_meta)


def get_internal_review(review_json: Any) -> Optional[Dict[str, Any]]:
    """Return the current internal review without duplicating stored payloads."""

    if not isinstance(review_json, dict):
        return None
    current = review_json.get("v3_review")
    if isinstance(current, dict):
        return current
    return None


def v3_review_to_raw(review: Any) -> Optional[Dict[str, Any]]:
    """Rebuild the strict model-owned v3 shape for a mechanical re-projection.

    Exact-head CI refresh owns this use case.  Stored ``evidence_scope`` is an
    objective catalog projection, not model input, so replay must use the
    retained ``evidence_scope_refs`` identities.  Internal CI/renderer fields
    are intentionally excluded.
    """

    if not isinstance(review, dict) or review.get("schema_version") != 3:
        return None
    decision = deepcopy(review.get("decision"))
    if not isinstance(decision, dict):
        return None
    model_verdict = _text(
        review.get("model_verdict") or review.get("visible_verdict")
    )
    if model_verdict in ALLOWED_MODEL_VERDICTS:
        decision["verdict"] = model_verdict
    return {
        "schema_version": 3,
        "decision": decision,
        "owner_action": deepcopy(review.get("owner_action") or []),
        "findings": deepcopy(review.get("findings") or []),
        "material_unknowns": deepcopy(review.get("material_unknowns") or []),
        "evidence_scope": list(review.get("evidence_scope_refs") or []),
        "diagram": deepcopy(review.get("diagram")),
        "rendering_plan": deepcopy(review.get("rendering_plan") or {}),
        "visible_projection_source": _text(
            review.get("visible_projection_source")
        ),
    }

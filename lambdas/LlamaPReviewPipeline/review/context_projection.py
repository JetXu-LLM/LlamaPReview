"""Project exact-head context into the Deep judgment boundary.

PFR keeps question/event identities for retrieval correctness and operations.
Deep receives only review-relevant facts, provenance, coverage, and honest
gaps.  This module is the explicit boundary between those two responsibilities.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Mapping, Optional

from .evidence_contract import catalog_entries, catalog_ref_admission


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def acceptance_criteria_for_deep(
    context_meta: Optional[Mapping[str, Any]],
) -> list[Dict[str, str]]:
    """Return bounded author criteria with no planner identities."""

    raw = (context_meta or {}).get("pfr_author_acceptance_criteria")
    criteria: list[Dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        criterion = (
            _text(item.get("criterion"), 1600)
            if isinstance(item, Mapping)
            else _text(item, 1600)
        )
        if criterion and criterion not in {
            existing["criterion"] for existing in criteria
        }:
            criteria.append({"criterion": criterion})
        if len(criteria) >= 12:
            break
    return criteria


def changed_delta_for_deep(
    context_meta: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return the code-owned complete-available-delta attention projection."""

    meta = context_meta or {}
    raw = (
        meta.get("changed_delta_focus")
        if isinstance(meta.get("changed_delta_focus"), Mapping)
        else {}
    )
    files: list[Dict[str, Any]] = []
    for item in raw.get("files") or []:
        if not isinstance(item, Mapping):
            continue
        coverage = _text(item.get("diff_coverage"), 32).lower()
        if coverage not in {"complete", "partial", "unavailable"}:
            coverage = "unavailable"
        files.append(
            {
                "path": _text(item.get("path"), 500),
                "change_type": _text(item.get("change_type"), 40),
                "additions": _nonnegative_int(item.get("additions")),
                "deletions": _nonnegative_int(item.get("deletions")),
                "patch": str(item.get("patch") or "")[:30_000],
                "diff_coverage": coverage,
                "source_diff_chars": _nonnegative_int(
                    item.get("source_diff_chars")
                ),
                "visible_diff_chars": _nonnegative_int(
                    item.get("visible_diff_chars")
                ),
            }
        )
        if len(files) >= 80:
            break
    packing = (
        raw.get("packing")
        if isinstance(raw.get("packing"), Mapping)
        else {}
    )
    return {
        "schema": "llamapreview.changed_delta_focus.v1",
        "source": "same queued-head PR file changes",
        "files": files,
        "packing": {
            "source_file_count": _nonnegative_int(
                packing.get("source_file_count")
            ),
            "retained_file_count": len(files),
            "file_list_truncated": bool(
                packing.get("file_list_truncated")
            ),
            "per_patch_limit": _nonnegative_int(
                packing.get("per_patch_limit")
            ),
        },
    }


def evidence_catalog_for_deep(
    context_meta: Optional[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    """Return exact admitted evidence records used for factual provenance."""

    meta = dict(context_meta or {})
    admitted: list[Dict[str, Any]] = []
    for identity, item in catalog_entries(meta).items():
        if not catalog_ref_admission(identity, meta).admissible_for_finding:
            continue
        projected = deepcopy(dict(item))
        # Retrieval-planner linkage is operational state, not judgment
        # evidence. Stable evidence identities remain so Final can cite facts.
        for private_key in ("question_id", "question_ids"):
            projected.pop(private_key, None)
        admitted.append(projected)
        if len(admitted) >= 160:
            break
    return admitted


def evidence_gaps_for_deep(
    context_meta: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return factual unresolved gaps and coverage without planner ledgers."""

    meta = context_meta or {}
    raw_unknowns = meta.get("pfr_unresolved_gaps")
    unknowns: list[Dict[str, Any]] = []
    for item in raw_unknowns if isinstance(raw_unknowns, list) else []:
        if not isinstance(item, Mapping):
            continue
        claim = _text(item.get("claim"), 1600)
        how_to_check = _text(item.get("how_to_check"), 1600)
        if not claim:
            continue
        unknowns.append(
            {
                "claim": claim,
                "how_to_check": how_to_check,
            }
        )
        if len(unknowns) >= 16:
            break

    raw_coverage = meta.get("pfr_evidence_coverage")
    coverage = (
        deepcopy(dict(raw_coverage))
        if isinstance(raw_coverage, Mapping)
        else {}
    )
    return {
        "unresolved_gaps": unknowns,
        "coverage": coverage,
    }

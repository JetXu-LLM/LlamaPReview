"""Validate and project fixed Final presentation objects.

The public :mod:`review.presentation` facade owns parsing and repair.  This
module owns the other stable capability: independent IR item/surface
normalization, evidence admission, local degradation, code-owned public
identities, and assembly of the mechanically validated public-v3 input.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re
from typing import Any, Dict, Mapping, Optional, Sequence

from .evidence_contract import (
    catalog_entries,
    catalog_ref_admission,
    changed_postimages,
    changed_preimages,
    ci_payload_taints_text,
    classify_changed_region_anchor,
    entry_is_objectively_renderable,
    entry_paths,
    generation_ci_check,
    normalize_repo_path,
    uniquely_resolve_changed_region_anchor,
)
from .rendering_safety import format_mermaid
from .presentation import (
    CATEGORIES,
    CATEGORY_TO_V3,
    CHECK_FIELDS,
    CONFIDENCES,
    DECISION_FIELDS,
    DECISION_VERDICTS,
    DIAGRAM_FIELDS,
    DIAGRAM_PURPOSES,
    FINDING_FIELDS,
    MAX_CODE,
    MAX_CONFIDENCE_CHECKS,
    MAX_DECISION_ACTIONS,
    MAX_EVIDENCE_REFS,
    MAX_FINDINGS,
    MAX_SUMMARY,
    MAX_TEXT,
    MAX_UNKNOWNS,
    PLACEMENTS,
    PRESENTATION_VERSION,
    PRIORITIES,
    ROOT_FIELDS,
    SUGGESTION_FIELDS,
    SUGGESTION_TYPES,
    UNKNOWN_FIELDS,
    VERDICT_TO_V3,
    PresentationIssue,
    PresentationResult,
    bounded_text,
    changed_ref,
    contains_private_presentation_vocabulary,
    failure_result,
    issue,
)
from .projection import build_v3_review
from .public_boundary import collect_private_identities, contains_private_identity
from .schema import normalize_pr_type, suggestion_presentation


_PUBLIC_PR_TYPES = {"code", "dependency", "docs", "config", "ci", "large", "mixed"}


def _fold(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


_NO_ACTION_OWNER_ACTION = re.compile(
    r"^(?:none(?: required| needed)?|no action(?: required| needed)?|"
    r"no pre-merge action(?: is)? required)\b",
    re.IGNORECASE,
)


def _declares_no_owner_action(value: str) -> bool:
    """Recognize an explicit no-action sentinel in Final's action field."""

    return bool(_NO_ACTION_OWNER_ACTION.match(value.strip()))


def _is_single_identifier_edit(left: str, right: str) -> bool:
    """Return whether two opaque identifiers differ by one typing edit."""

    if left == right or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        differences = [
            index
            for index, (a, b) in enumerate(zip(left, right))
            if a != b
        ]
        if len(differences) == 1:
            return True
        if (
            len(differences) == 2
            and differences[1] == differences[0] + 1
            and left[differences[0]] == right[differences[1]]
            and left[differences[1]] == right[differences[0]]
        ):
            return True
        # Opaque IDs are also occasionally copied with one character shifted
        # a few positions.  Admit only an exact one-character relocation; the
        # caller still requires one unique catalog candidate.
        for source_index, char in enumerate(left):
            without = left[:source_index] + left[source_index + 1 :]
            for target_index in range(len(right)):
                if (
                    without[:target_index]
                    + char
                    + without[target_index:]
                    == right
                ):
                    return True
        return False
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    short_index = 0
    long_index = 0
    skipped = False
    while short_index < len(shorter) and long_index < len(longer):
        if shorter[short_index] == longer[long_index]:
            short_index += 1
            long_index += 1
            continue
        if skipped:
            return False
        skipped = True
        long_index += 1
    return True


def _unique_catalog_ref_typo(
    value: str,
    catalog: Mapping[str, Any],
) -> Optional[str]:
    """Restore one unique one-edit typo in an opaque evidence identity."""

    if not value.startswith("ev_"):
        return None
    candidates = [
        identity
        for identity in catalog
        if identity.startswith("ev_")
        and _is_single_identifier_edit(value, identity)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _finding_fingerprint(item: Mapping[str, Any]) -> tuple[Any, ...]:
    suggestion = item.get("suggestion")
    return (
        _fold(item.get("headline")),
        item.get("priority"),
        item.get("category"),
        item.get("confidence"),
        item.get("file_path"),
        str(item.get("code_snippet") or "").strip(),
        _fold(item.get("analysis")),
        _fold(item.get("owner_action")),
        tuple(item.get("required_evidence_refs") or []),
        tuple(item.get("supporting_evidence_refs") or []),
        (
            suggestion.get("type"),
            str(suggestion.get("content") or "").strip(),
        )
        if isinstance(suggestion, dict)
        else None,
    )


def _same_path_exact_anchor_refs(
    refs: Sequence[str],
    *,
    path: str,
    state: "_CompileState",
) -> list[str]:
    """Select already-admitted refs that mechanically prove this anchor.

    This is intentionally narrower than evidence admission.  It is used only
    to recover Final's required/supporting representation slip for a P2 in a
    blocking review: the exact post-change snippet and an exact-head
    changed-region/full-file observation must name the same repository path.
    """

    retained: list[str] = []
    for ref in refs:
        entry = state.catalog.get(ref) or {}
        if str(entry.get("source_type") or "").strip() == "ci":
            continue
        if str(entry.get("coverage_type") or "").strip() not in {
            "changed_region",
            "full_file",
        }:
            continue
        if path not in entry_paths(entry):
            continue
        retained.append(ref)
    return retained


def _direct_check_clear_summary(
    checks: Sequence[Mapping[str, Any]],
    *,
    state: "_CompileState",
) -> Optional[str]:
    """Return one admitted direct-observation check as clear proof.

    Search snippets and inventories are deliberately excluded: they are useful
    retrieval evidence but cannot safely carry repository-absence prose above
    the fold.  The function selects existing model words; it does not rewrite
    or infer a replacement engineering conclusion.
    """

    for item in checks:
        refs = item.get("evidence_refs") or []
        if not refs:
            continue
        entries = [state.catalog.get(str(ref)) or {} for ref in refs]
        if not all(
            entry
            and str(entry.get("source_type") or "").strip() != "ci"
            and str(entry.get("coverage_type") or "").strip()
            in {"changed_region", "file_slice", "full_file"}
            and str(entry.get("tool") or "").strip() != "search_code"
            for entry in entries
        ):
            continue
        check = str(item.get("check") or "").strip().rstrip(" .!?:;")
        result = str(item.get("result") or "").strip()
        candidate = bounded_text(
            f"No review blocker found. {check}: {result}",
            limit=MAX_SUMMARY,
        )
        if candidate is not None:
            return candidate
    return None


def _unknown_fingerprint(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _fold(item.get("missing_fact")),
        _fold(item.get("impact")),
        _fold(item.get("owner_action")),
        tuple(item.get("evidence_refs") or []),
    )


@dataclass
class _CompileState:
    context_meta: Optional[Dict[str, Any]]
    changed_ci_refs: set[str]
    catalog: Dict[str, Dict[str, Any]] = field(init=False)
    issues: list[PresentationIssue] = field(default_factory=list)
    normalizations: list[str] = field(default_factory=list)
    safe_partial: bool = False
    clear_decision_dependency_uncertain: bool = False
    blocking_decision_dependency_uncertain: bool = False

    def __post_init__(self) -> None:
        self.catalog = {
            str(key): dict(value)
            for key, value in catalog_entries(self.context_meta).items()
            if isinstance(value, dict)
        }

    @property
    def truth_failed(self) -> bool:
        return any(item.severity == "truth" for item in self.issues)

    def normalize(self, action: str, *, partial: bool = False) -> None:
        self.normalizations.append(action)
        self.safe_partial = self.safe_partial or partial

    def add_issue(
        self,
        code: str,
        location: str,
        message: str,
        severity: str,
    ) -> None:
        self.issues.append(issue(code, location, message, severity))  # type: ignore[arg-type]
        self.safe_partial = self.safe_partial or severity in {"surface", "item"}

    def contract_clear_decision_if_needed(self, location: str) -> None:
        """Record that a removed optional item may have supplied clear prose."""

        self.clear_decision_dependency_uncertain = True
        self.normalize(f"{location}:clear_decision_dependency_removed", partial=True)

    def contract_blocking_decision_if_needed(self, location: str) -> None:
        """Record that a removed deciding item may have supplied first-screen prose."""

        self.blocking_decision_dependency_uncertain = True
        self.normalize(
            f"{location}:blocking_decision_dependency_removed",
            partial=True,
        )

    def admit_refs(
        self,
        value: Any,
        *,
        location: str,
        supporting: bool = False,
        optional_surface: bool = False,
    ) -> Optional[list[str]]:
        """Admit exact catalog refs without judging the model's prose."""

        if not isinstance(value, list) or len(value) > MAX_EVIDENCE_REFS:
            self.add_issue(
                "evidence_refs_shape_invalid",
                location,
                "evidence references must be a bounded string array",
                "surface" if optional_surface else "item",
            )
            return None
        retained: list[str] = []
        for index, raw_ref in enumerate(value):
            if not isinstance(raw_ref, str) or not raw_ref.strip():
                self.add_issue(
                    "evidence_ref_type_invalid",
                    f"{location}[{index}]",
                    "evidence reference must be a non-empty string",
                    "surface" if optional_surface else "item",
                )
                return None
            ref = raw_ref.strip()
            admission = catalog_ref_admission(ref, self.context_meta)
            if not admission.known:
                restored_ref = _unique_catalog_ref_typo(ref, self.catalog)
                if restored_ref is not None:
                    ref = restored_ref
                    admission = catalog_ref_admission(
                        ref,
                        self.context_meta,
                    )
                    self.normalize(
                        f"{location}:unique_catalog_ref_restored"
                    )
            if ref in retained:
                self.normalize(f"{location}:duplicate_ref_removed")
                continue
            if not admission.known:
                if supporting:
                    self.normalize(f"{location}:optional_ref_removed", partial=True)
                    continue
                if optional_surface:
                    self.add_issue(
                        "evidence_ref_out_of_catalog",
                        f"{location}[{index}]",
                        "optional surface cited evidence outside the catalog",
                        "surface",
                    )
                    return None
                self.add_issue(
                    "evidence_ref_out_of_catalog",
                    f"{location}[{index}]",
                    "model cited evidence outside the supplied catalog",
                    "truth",
                )
                return None
            invalidated = changed_ref(ref, self.changed_ci_refs)
            if invalidated or not admission.admissible_for_finding:
                if supporting:
                    self.normalize(
                        f"{location}:supporting_ref_removed",
                        partial=True,
                    )
                    continue
                self.add_issue(
                    (
                        "required_ci_ref_changed"
                        if invalidated
                        else "evidence_ref_inadmissible"
                    ),
                    f"{location}[{index}]",
                    "required evidence is no longer admissible",
                    "surface" if optional_surface else "item",
                )
                return None
            retained.append(ref)
        return retained


def _known_paths(
    state: _CompileState,
    pr_details: str,
) -> set[str]:
    paths = {
        path
        for entry in state.catalog.values()
        if entry_is_objectively_renderable(entry)
        for path in entry_paths(entry)
    }
    paths.update(changed_postimages(pr_details))
    paths.update(changed_preimages(pr_details))
    return paths


def _contains_ci_ref(value: Any) -> bool:
    values = value if isinstance(value, list) else [value]
    return any(
        isinstance(item, str) and item.strip().startswith("ci:")
        for item in values
    )


def _supporting_ci_core_taint(
    source: Mapping[str, Any],
    *,
    context_meta: Optional[Dict[str, Any]],
    state: _CompileState,
) -> Optional[tuple[str, str]]:
    findings = source.get("findings")
    checks = source.get("confidence_checks")
    if not isinstance(findings, list) or not isinstance(checks, list):
        return None
    required_ci_refs = {
        ref.strip()
        for finding in findings
        if isinstance(finding, dict)
        for ref in finding.get("required_evidence_refs") or []
        if isinstance(ref, str) and ref.strip().startswith("ci:")
    }
    check_ci_refs = {
        ref.strip()
        for check in checks
        if isinstance(check, dict)
        for ref in check.get("evidence_refs") or []
        if (
            isinstance(ref, str)
            and ref.strip().startswith("ci:")
        )
    }
    if not check_ci_refs:
        return None

    decision = source.get("decision")
    decision_values: list[tuple[str, Any]] = []
    if isinstance(decision, dict):
        decision_values.append(("$.decision.summary", decision.get("summary")))
        decision_values.extend(
            (f"$.decision.owner_actions[{index}]", value)
            for index, value in enumerate(
                decision.get("owner_actions")
                if isinstance(decision.get("owner_actions"), list)
                else []
            )
        )
    for ref in sorted(check_ci_refs - required_ci_refs):
        check = generation_ci_check(ref, context_meta)
        for location, value in decision_values:
            if ci_payload_taints_text(value, check):
                return location, ref

    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            continue
        finding_required_ci_refs = {
            ref.strip()
            for ref in finding.get("required_evidence_refs") or []
            if isinstance(ref, str) and ref.strip().startswith("ci:")
        }
        core_values = [
            (f"$.findings[{index}].{field}", finding.get(field))
            for field in ("headline", "analysis", "owner_action")
        ]
        for ref in sorted(check_ci_refs - finding_required_ci_refs):
            check = generation_ci_check(ref, context_meta)
            for location, value in core_values:
                if ci_payload_taints_text(value, check):
                    return location, ref
    return None


def _changed_ci_core_taint(
    decision: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    *,
    context_meta: Optional[Dict[str, Any]],
    state: _CompileState,
) -> Optional[tuple[str, str]]:
    changed_refs = {
        ref if ref.startswith("ci:") else f"ci:{ref}"
        for ref in state.changed_ci_refs
        if ref
    }
    if not changed_refs:
        return None
    core_values: list[tuple[str, Any]] = [
        ("$.decision.summary", decision.get("summary")),
    ]
    core_values.extend(
        (f"$.decision.owner_actions[{index}]", value)
        for index, value in enumerate(decision.get("owner_actions") or [])
    )
    for index, finding in enumerate(findings):
        core_values.extend(
            (f"$.findings[{index}].{field}", finding.get(field))
            for field in ("headline", "analysis", "owner_action")
        )
    for ref in sorted(changed_refs):
        check = generation_ci_check(ref, context_meta)
        for location, value in core_values:
            if ci_payload_taints_text(value, check):
                return location, ref
    return None


def _normalize_decision(
    source: Mapping[str, Any],
    state: _CompileState,
) -> Optional[Dict[str, Any]]:
    decision = source.get("decision")
    if not isinstance(decision, dict):
        return None
    if set(decision) - DECISION_FIELDS:
        state.normalize("$.decision:extra_fields_ignored")
    verdict = decision.get("verdict")
    confidence = decision.get("confidence")
    summary = bounded_text(decision.get("summary"), limit=MAX_SUMMARY)
    if (
        verdict not in DECISION_VERDICTS
        or confidence not in CONFIDENCES
        or summary is None
    ):
        return None
    if contains_private_presentation_vocabulary(summary):
        state.add_issue(
            "decision_public_text_unsafe",
            "$.decision.summary",
            "decision summary exposes private presentation machinery",
            "truth",
        )
        return None
    if verdict == "clear" and not summary.startswith(
        "No review blocker found"
    ):
        summary = f"No review blocker found. {summary}"
        state.normalize("$.decision.summary:clear_prefix_added")

    raw_actions = decision.get("owner_actions")
    if not isinstance(raw_actions, list):
        state.normalize(
            "$.decision.owner_actions:invalid_surface_removed",
            partial=True,
        )
        raw_actions = []
    actions: list[str] = []
    for index, raw_action in enumerate(raw_actions[:MAX_DECISION_ACTIONS]):
        action = bounded_text(raw_action)
        if action is None or contains_private_presentation_vocabulary(action):
            state.add_issue(
                "owner_action_invalid",
                f"$.decision.owner_actions[{index}]",
                "owner action is not safe presentation text",
                "surface",
            )
            continue
        actions.append(action)
    if len(raw_actions) > MAX_DECISION_ACTIONS:
        state.normalize("$.decision.owner_actions:cap_applied", partial=True)
    return {
        "verdict": verdict,
        "confidence": confidence,
        "summary": summary,
        "owner_actions": actions,
    }


def _normalize_finding(
    raw: Any,
    *,
    index: int,
    verdict: str,
    pr_details: str,
    known_paths: set[str],
    state: _CompileState,
) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
    location = f"$.findings[{index}]"
    if not isinstance(raw, dict):
        state.add_issue(
            "finding_shape_invalid",
            location,
            "finding must be an object",
            "item",
        )
        if verdict == "clear":
            state.contract_clear_decision_if_needed(location)
        return None
    if set(raw) - FINDING_FIELDS:
        state.normalize(f"{location}:extra_fields_ignored")
    headline = bounded_text(raw.get("headline"))
    analysis = bounded_text(raw.get("analysis"))
    owner_action = bounded_text(raw.get("owner_action"))
    priority = raw.get("priority")
    category = raw.get("category")
    confidence = raw.get("confidence")
    requested_placement = raw.get("placement")
    carries_blocking_decision = (
        verdict == "blocking"
        and category not in {"test-gap", "question", "note"}
    )
    representation_requirement = raw.get("representation_requirement")
    if representation_requirement is None:
        representation_requirement = "semantic"
        state.normalize(
            f"{location}.representation_requirement:legacy_semantic_defaulted"
        )
    if representation_requirement not in {
        "semantic",
        "exact_postimage",
        "exact_full_file",
    }:
        state.add_issue(
            "representation_requirement_invalid",
            f"{location}.representation_requirement",
            (
                "representation requirement is invalid"
            ),
            "item",
        )
        if carries_blocking_decision:
            state.contract_blocking_decision_if_needed(location)
        return None
    path_value = raw.get("file_path")
    path = (
        normalize_repo_path(path_value)
        if isinstance(path_value, str) and len(path_value) <= MAX_TEXT
        else None
    )
    snippet = raw.get("code_snippet")
    snippet = (
        snippet
        if isinstance(snippet, str) and len(snippet) <= MAX_CODE
        else None
    )
    texts = (headline or "", analysis or "", owner_action or "")
    valid = bool(
        headline
        and analysis
        and owner_action
        and priority in PRIORITIES
        and category in CATEGORIES
        and confidence in CONFIDENCES
        and path is not None
        and snippet is not None
        and not any(
            contains_private_presentation_vocabulary(value) for value in texts
        )
    )
    if not valid:
        state.add_issue(
            "finding_value_invalid",
            location,
            "finding has an invalid or unsafe required field",
            "item",
        )
        if verdict == "clear":
            state.contract_clear_decision_if_needed(location)
        return None
    if verdict == "clear" and _declares_no_owner_action(owner_action):
        state.normalize(
            f"{location}:action_free_clear_finding_removed",
            partial=True,
        )
        return None
    placement = (
        requested_placement if requested_placement in PLACEMENTS else "collapsed"
    )
    if placement != requested_placement:
        state.normalize(f"{location}.placement:unsupported_collapsed", partial=True)
    requires_anchor = placement == "inline"
    if (path and path not in known_paths) or (requires_anchor and not path):
        state.add_issue(
            "finding_path_unproven",
            f"{location}.file_path",
            "finding path is outside the changed or observed path set",
            "item",
        )
        if verdict == "clear":
            state.contract_clear_decision_if_needed(location)
        return None

    # A nondeciding P2 is optional review detail. An unsupported reference in
    # that item must not discard an otherwise trustworthy review. The first P2
    # in a blocking response remains strict because Final's contract makes it
    # the carrier when no P0/P1 exists.
    optional_p2 = priority == "P2" and not (
        verdict == "blocking" and index == 0
    )
    required = state.admit_refs(
        raw.get("required_evidence_refs"),
        location=f"{location}.required_evidence_refs",
        optional_surface=optional_p2,
    )
    supporting = state.admit_refs(
        raw.get("supporting_evidence_refs"),
        location=f"{location}.supporting_evidence_refs",
        supporting=True,
    )
    if required is None or supporting is None:
        if verdict == "clear":
            state.contract_clear_decision_if_needed(location)
        return None
    retained_supporting = [
        ref for ref in supporting if not ref.startswith("ci:")
    ]
    if len(retained_supporting) != len(supporting):
        supporting = retained_supporting
        state.normalize(
            f"{location}.supporting_evidence_refs:ci_refs_removed",
            partial=True,
        )
    required_set = set(required)
    if any(ref in required_set for ref in supporting):
        supporting = [ref for ref in supporting if ref not in required_set]
        state.normalize(
            f"{location}.supporting_evidence_refs:required_duplicates_removed",
            partial=True,
        )
    anchor = classify_changed_region_anchor(path, snippet, pr_details)
    if anchor == "invalid" and snippet.strip():
        resolved = uniquely_resolve_changed_region_anchor(
            path,
            snippet,
            pr_details,
        )
        if resolved is not None:
            snippet = resolved
            anchor = "post_change"
            state.normalize(
                f"{location}.code_snippet:unique_changed_region_restored",
                partial=True,
            )
        else:
            snippet = ""
            state.normalize(
                f"{location}.code_snippet:invalid_optional_anchor_removed",
                partial=True,
            )
    if priority in {"P0", "P1"} and not required:
        state.add_issue(
            "high_priority_evidence_gate_failed",
            location,
            "P0/P1 requires admitted required evidence",
            "item",
        )
        return None
    normalized_placement = placement
    if placement == "inline" and anchor != "post_change":
        normalized_placement = (
            "headline" if priority in {"P0", "P1"} else "collapsed"
        )
        state.normalize(f"{location}.placement:inline_removed", partial=True)

    normalized_suggestion = None
    suggested_code = None
    suggestion_type = None
    suggestion = raw.get("suggestion")
    if suggestion is not None and anchor != "post_change":
        state.normalize(
            f"{location}.suggestion:unanchored_optional_surface_removed",
            partial=True,
        )
    elif suggestion is not None:
        if not isinstance(suggestion, dict):
            state.add_issue(
                "suggestion_shape_invalid",
                f"{location}.suggestion",
                "suggestion must be null or an object",
                "surface",
            )
        else:
            declared = suggestion.get("type")
            content = bounded_text(suggestion.get("content"), limit=MAX_CODE)
            if (
                declared not in SUGGESTION_TYPES
                or content is None
                or set(suggestion) - SUGGESTION_FIELDS
            ):
                state.add_issue(
                    "suggestion_value_invalid",
                    f"{location}.suggestion",
                    "suggestion has an invalid type or content",
                    "surface",
                )
            else:
                transport = suggestion_presentation(
                    suggestion_type=declared,
                    code_snippet=snippet,
                    suggested_code=content,
                )
                suggestion_type = (
                    "DIRECT_REPLACEMENT"
                    if transport["committable"]
                    else "CONCEPTUAL_ADVICE"
                )
                if declared != suggestion_type:
                    state.normalize(
                        f"{location}.suggestion:direct_replacement_downgraded",
                        partial=True,
                    )
                suggested_code = content
                normalized_suggestion = {
                    "type": suggestion_type,
                    "content": content,
                }

    if (
        verdict == "blocking"
        and priority == "P2"
        and carries_blocking_decision
        and anchor == "post_change"
        and not required
        and supporting
    ):
        promoted = _same_path_exact_anchor_refs(
            supporting,
            path=path,
            state=state,
        )
        if promoted:
            required = promoted
            promoted_set = set(promoted)
            supporting = [
                ref for ref in supporting if ref not in promoted_set
            ]
            state.normalize(
                f"{location}.required_evidence_refs:"
                "exact_anchor_role_restored",
                partial=True,
            )

    refs = list(dict.fromkeys([*required, *supporting]))
    evidence_verified = bool(required)
    if priority == "P2" and not refs:
        state.normalize(
            f"{location}:unsupported_nonblocking_finding_removed",
            partial=True,
        )
        if verdict == "clear":
            state.contract_clear_decision_if_needed(location)
        return None
    if not evidence_verified and normalized_placement == "inline":
        normalized_placement = "collapsed"
        state.normalize(
            f"{location}.placement:unverified_inline_collapsed",
            partial=True,
        )
    if supporting and not evidence_verified:
        state.normalize(
            f"{location}:supporting_only_marked_unverified",
            partial=True,
        )
    normalized = {
        "headline": headline,
        "priority": priority,
        "category": category,
        "confidence": confidence,
        "file_path": path,
        "code_snippet": snippet,
        "analysis": analysis,
        "owner_action": owner_action,
        "required_evidence_refs": required,
        "supporting_evidence_refs": supporting,
        "representation_requirement": representation_requirement,
        "placement": normalized_placement,
        "suggestion": normalized_suggestion,
    }
    projected = {
        "finding_type": CATEGORY_TO_V3[category],
        "priority": priority,
        "confidence": confidence,
        "evidence_status": "verified" if evidence_verified else "unverified",
        "claim_scope": (
            "changed_region"
            if anchor == "post_change"
            else "bounded_context"
        ),
        "blocking": False,
        "visibility": normalized_placement,
        "headline": headline,
        "file_path": path,
        "code_snippet": snippet,
        "comment": f"{analysis}\n\nOwner action: {owner_action}",
        "representation_requirement": representation_requirement,
        "required_evidence_refs": required,
        "supporting_evidence_refs": supporting,
        "evidence_refs": refs,
    }
    # Required refs carry the finding's asserted scope.  A noncritical P2 may
    # survive as an explicitly unverified, collapsed note when those refs are
    # real but too weak for that scope; it must not turn an otherwise safe
    # review into a whole-review failure.  P0/P1 and merge-deciding P2 items
    # retain the strict fail-closed path below.
    from .v3_validation_findings import finding_evidence_capability

    capability = finding_evidence_capability(
        projected,
        pr_details=pr_details,
        context_meta=state.context_meta,
    )
    if priority in {"P0", "P1"} and not capability["critical_supported"]:
        state.add_issue(
            "finding_evidence_capability_insufficient",
            f"{location}.required_evidence_refs",
            "P0/P1 lacks independent changed-code evidence capability",
            "item",
        )
        if carries_blocking_decision:
            state.contract_blocking_decision_if_needed(location)
        return None
    if priority == "P2" and (
        capability["rejected_refs"] or not capability["scope_supported"]
    ):
        if carries_blocking_decision:
            state.add_issue(
                "finding_evidence_capability_insufficient",
                f"{location}.required_evidence_refs",
                "merge-deciding P2 lacks evidence for its asserted scope",
                "item",
            )
            return None
        rejected = set(capability["rejected_refs"])
        degraded_supporting = list(
            dict.fromkeys(
                [
                    *(
                        ref
                        for ref in capability["usable_refs"]
                        if ref not in rejected
                    ),
                    *(ref for ref in supporting if ref not in rejected),
                ]
            )
        )
        if not degraded_supporting:
            state.normalize(
                f"{location}:unsupported_nonblocking_finding_removed",
                partial=True,
            )
            if verdict == "clear":
                state.contract_clear_decision_if_needed(location)
            return None
        normalized["required_evidence_refs"] = []
        normalized["supporting_evidence_refs"] = degraded_supporting
        normalized["placement"] = "collapsed"
        projected["required_evidence_refs"] = []
        projected["supporting_evidence_refs"] = degraded_supporting
        projected["evidence_refs"] = degraded_supporting
        projected["evidence_status"] = "unverified"
        projected["visibility"] = "collapsed"
        state.normalize(
            f"{location}:insufficient_scope_marked_unverified",
            partial=True,
        )
    if suggested_code:
        projected["suggested_code"] = suggested_code
        projected["suggestion_type"] = suggestion_type
    return normalized, projected


def _normalize_unknown(
    raw: Any,
    *,
    index: int,
    deciding: bool,
    clear_verdict: bool,
    state: _CompileState,
) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
    location = f"$.material_unknowns[{index}]"
    if not isinstance(raw, dict):
        state.add_issue(
            "unknown_shape_invalid",
            location,
            "material unknown must be an object",
            "item",
        )
        if clear_verdict:
            state.contract_clear_decision_if_needed(location)
        return None
    if set(raw) - UNKNOWN_FIELDS:
        state.normalize(f"{location}:extra_fields_ignored")
    missing_fact = bounded_text(raw.get("missing_fact"))
    impact = bounded_text(raw.get("impact"))
    owner_action = bounded_text(raw.get("owner_action"))
    if (
        not missing_fact
        or not impact
        or not owner_action
        or any(
            contains_private_presentation_vocabulary(value)
            for value in (missing_fact or "", impact or "", owner_action or "")
        )
    ):
        state.add_issue(
            "unknown_value_invalid",
            location,
            "material unknown has an invalid or unsafe required field",
            "item",
        )
        if clear_verdict:
            state.contract_clear_decision_if_needed(location)
        return None
    refs = state.admit_refs(
        raw.get("evidence_refs"),
        location=f"{location}.evidence_refs",
        optional_surface=not deciding,
    )
    if refs is None:
        if clear_verdict:
            state.contract_clear_decision_if_needed(location)
        return None
    normalized = {
        "missing_fact": missing_fact,
        "impact": impact,
        "owner_action": owner_action,
        "evidence_refs": refs,
    }
    return normalized, {
        "claim": f"{missing_fact} {impact}",
        "how_to_check": owner_action,
        "affects_merge": deciding,
        "evidence_refs": refs,
    }


def _normalize_check(
    raw: Any,
    *,
    index: int,
    state: _CompileState,
) -> Optional[Dict[str, Any]]:
    location = f"$.confidence_checks[{index}]"
    if not isinstance(raw, dict):
        state.add_issue(
            "confidence_check_shape_invalid",
            location,
            "confidence check must be an object",
            "surface",
        )
        return None
    check = bounded_text(raw.get("check"))
    result = bounded_text(raw.get("result"))
    if (
        not check
        or not result
        or contains_private_presentation_vocabulary(check)
        or contains_private_presentation_vocabulary(result)
    ):
        state.add_issue(
            "confidence_check_value_invalid",
            location,
            "confidence check has invalid or unsafe text",
            "surface",
        )
        return None
    refs = state.admit_refs(
        raw.get("evidence_refs"),
        location=f"$.confidence_checks[{index}].evidence_refs",
        optional_surface=True,
    )
    if not refs:
        if refs is not None:
            state.add_issue(
                "confidence_check_unanchored",
                f"{location}.evidence_refs",
                "confidence check requires admitted evidence",
                "surface",
            )
        return None
    ci_refs = [ref for ref in refs if str(ref).startswith("ci:")]
    ci_relevance = raw.get("ci_relevance")
    if ci_refs:
        if ci_relevance not in {"unrelated", "pr_related", "uncertain"}:
            ci_relevance = "uncertain"
            state.normalize(
                f"{location}.ci_relevance:missing_or_invalid_defaulted"
            )
    else:
        ci_relevance = "not_applicable"
    return {
        "check": check,
        "result": result,
        "evidence_refs": refs,
        "ci_relevance": ci_relevance,
    }


def _structured_ci_public_state(
    context_meta: Optional[Dict[str, Any]],
    checks: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Compile objective CI state plus Deep-owned, evidence-bound relevance."""

    snapshot = (context_meta or {}).get("ci_snapshot") or {}
    if not (
        isinstance(snapshot, dict)
        and snapshot.get("schema_version") == 1
    ):
        return {
            "posture": "not_observed",
            "retrieval_outcome": "not_observed",
            "counts": {},
        }
    observed = [
        item
        for item in snapshot.get("checks") or []
        if isinstance(item, dict)
    ]
    counts = {
        classification: sum(
            1
            for item in observed
            if str(item.get("classification") or "") == classification
        )
        for classification in (
            "failure",
            "action_required",
            "pending",
            "incomplete",
            "success",
        )
    }
    retrieval = str(snapshot.get("retrieval_outcome") or "unverified")
    risk_checks = [
        item
        for item in observed
        if str(item.get("classification") or "")
        in {"failure", "action_required", "pending", "incomplete"}
    ]
    risk_refs = {
        f"ci:{item['identity']}"
        for item in risk_checks
        if str(item.get("identity") or "").strip()
    }
    observed_by_ref = {
        f"ci:{item['identity']}": item
        for item in observed
        if str(item.get("identity") or "").strip()
    }
    catalog = {
        str(item.get("id")): item
        for item in (context_meta or {}).get("evidence_catalog") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    unrelated_refs: set[str] = set()
    for item in checks:
        if item.get("ci_relevance") != "unrelated":
            continue
        refs = {
            str(ref)
            for ref in item.get("evidence_refs") or []
            if isinstance(ref, str)
        }
        ci_refs = refs & risk_refs
        has_exact_diagnostic = any(
            bool((observed_by_ref.get(ref) or {}).get("output"))
            or bool((observed_by_ref.get(ref) or {}).get("annotations"))
            for ref in ci_refs
        )
        has_exact_repository_support = any(
            (catalog.get(ref) or {}).get("source_type") in {"diff", "pfr"}
            and (catalog.get(ref) or {}).get("coverage_type")
            in {"changed_region", "file_slice", "full_file"}
            for ref in refs - ci_refs
        )
        if has_exact_diagnostic or has_exact_repository_support:
            unrelated_refs.update(ci_refs)
    only_completed_failures = bool(risk_checks) and all(
        str(item.get("classification") or "") == "failure"
        for item in risk_checks
    )
    complete = retrieval in {"ok", "no_hit"}
    has_ci = bool(snapshot.get("has_ci")) or bool(observed)
    if not has_ci and complete:
        posture = "not_observed"
    elif not risk_checks and complete:
        posture = "resolved"
    elif (
        complete
        and only_completed_failures
        and risk_refs
        and risk_refs <= unrelated_refs
    ):
        posture = "unrelated_supported"
    else:
        posture = "unresolved"
    return {
        "posture": posture,
        "retrieval_outcome": retrieval,
        "counts": counts,
    }


def _normalize_diagram(
    raw: Any,
    *,
    verdict: str,
    has_retained_blocker: bool,
    state: _CompileState,
) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        state.add_issue(
            "diagram_shape_invalid",
            "$.diagram",
            "diagram was not an object and was omitted",
            "surface",
        )
        return None
    if raw.get("purpose") == "risk_path" and not (
        verdict == "blocking" and has_retained_blocker
    ):
        state.normalize(
            "$.diagram:risk_path_not_eligible",
            partial=True,
        )
        return None
    caption = bounded_text(raw.get("caption"))
    mermaid = bounded_text(raw.get("mermaid"), limit=MAX_CODE)
    refs = state.admit_refs(
        raw.get("evidence_refs"),
        location="$.diagram.evidence_refs",
        supporting=True,
        optional_surface=True,
    )
    rendered = (
        format_mermaid(
            mermaid or "",
            strict=True,
            treat_unknown_as_error=True,
            github_flavor=True,
            auto_insert_sequence_header=False,
            auto_strip_leading_noise=False,
            github_convert_multiline_notes=True,
        )
        if mermaid
        else ""
    )
    if (
        raw.get("purpose") not in DIAGRAM_PURPOSES
        or not caption
        or contains_private_presentation_vocabulary(caption)
        or not rendered
        or not refs
        or set(raw) - DIAGRAM_FIELDS
    ):
        state.add_issue(
            "diagram_value_invalid",
            "$.diagram",
            "diagram failed its optional presentation contract",
            "surface",
        )
        state.normalize("$.diagram:invalid_optional_surface_removed", partial=True)
        return None
    return {
        "purpose": raw["purpose"],
        "caption": caption,
        "mermaid": rendered,
        "evidence_refs": refs,
    }


def _scope_refs(
    findings: Sequence[Dict[str, Any]],
    unknowns: Sequence[Dict[str, Any]],
    checks: Sequence[Dict[str, Any]],
    diagram: Optional[Dict[str, Any]],
    context_meta: Optional[Dict[str, Any]],
) -> list[str]:
    catalog = catalog_entries(context_meta)
    refs: list[str] = []
    for item in [*findings, *unknowns, *checks]:
        for key in (
            "required_evidence_refs",
            "supporting_evidence_refs",
            "evidence_refs",
        ):
            for ref in item.get(key) or []:
                if (
                    ref not in refs
                    and entry_is_objectively_renderable(catalog.get(ref) or {})
                ):
                    refs.append(ref)
    for ref in (diagram or {}).get("evidence_refs") or []:
        if (
            ref not in refs
            and entry_is_objectively_renderable(catalog.get(ref) or {})
        ):
            refs.append(ref)
    return refs[:6]


def _nonpublishable(
    state: _CompileState,
    *,
    source: Dict[str, Any],
    kind: str,
    presentation: Optional[Dict[str, Any]] = None,
) -> PresentationResult:
    return failure_result(
        presentation=presentation if presentation is not None else source,
        issues=state.issues,
        normalizations=state.normalizations,
        failure_kind=kind,
    )


def compile_presentation_object(
    source: Dict[str, Any],
    *,
    pr_details: str,
    context_meta: Optional[Dict[str, Any]],
    changed_ci_refs: set[str],
    parse_normalizations: Sequence[str],
) -> PresentationResult:
    """Validate one parsed fixed IR and project a safe public-v3 artifact."""

    state = _CompileState(context_meta, changed_ci_refs)
    state.normalizations.extend(parse_normalizations)
    if source.get("version") != PRESENTATION_VERSION:
        state.add_issue(
            "version_invalid",
            "$.version",
            "presentation version is missing or unsupported",
            "representation",
        )
        return _nonpublishable(
            state,
            source=source,
            kind="presentation_version_invalid",
        )
    if set(source) - ROOT_FIELDS:
        state.add_issue(
            "root_fields_invalid",
            "$",
            "presentation root contains unsupported fields",
            "representation",
        )
        return _nonpublishable(
            state,
            source=source,
            kind="presentation_shape_invalid",
        )
    decision = _normalize_decision(source, state)
    if state.truth_failed:
        return _nonpublishable(
            state,
            source=source,
            kind="unsafe_public_text",
        )
    if decision is None:
        state.add_issue(
            "decision_value_invalid",
            "$.decision",
            "decision lacks a valid verdict or summary",
            "representation",
        )
        return _nonpublishable(
            state,
            source=source,
            kind="decision_invalid",
        )
    verdict = decision["verdict"]

    raw_findings = source.get("findings")
    raw_unknowns = source.get("material_unknowns")
    if not isinstance(raw_findings, list) or not isinstance(raw_unknowns, list):
        state.add_issue(
            "presentation_array_invalid",
            "$",
            "findings and material_unknowns must be arrays",
            "representation",
        )
        return _nonpublishable(
            state,
            source=source,
            kind="presentation_shape_invalid",
        )
    if len(raw_findings) > MAX_FINDINGS or len(raw_unknowns) > MAX_UNKNOWNS:
        state.add_issue(
            "presentation_item_cap_exceeded",
            "$",
            "findings or material_unknowns exceed the bounded transport cap",
            "representation",
        )
        return _nonpublishable(
            state,
            source=source,
            kind="presentation_item_cap_exceeded",
        )
    core_taint = _supporting_ci_core_taint(
        source,
        context_meta=context_meta,
        state=state,
    )
    if core_taint is not None:
        source = deepcopy(source)
        source_decision = source["decision"]
        raw_findings = source["findings"]
        while core_taint is not None:
            location, _ref = core_taint
            if location == "$.decision.summary":
                if verdict == "clear":
                    replacement = "No review blocker found."
                elif verdict == "blocking":
                    carrier = next(
                        (
                            item
                            for item in raw_findings
                            if isinstance(item, dict)
                            and item.get("category")
                            not in {"test-gap", "question", "note"}
                            and item.get("required_evidence_refs")
                            and bounded_text(item.get("headline"))
                        ),
                        None,
                    )
                    if carrier is None:
                        break
                    replacement = (
                        "Changes are needed before merge: "
                        f"{bounded_text(carrier.get('headline'))}"
                    )
                else:
                    unknown = next(
                        (
                            item
                            for item in raw_unknowns
                            if isinstance(item, dict)
                            and bounded_text(item.get("missing_fact"))
                        ),
                        None,
                    )
                    if unknown is None:
                        break
                    replacement = (
                        "Verification is needed before merge: "
                        f"{bounded_text(unknown.get('missing_fact'))}"
                    )
                source_decision["summary"] = replacement
                decision["summary"] = replacement
            elif match := re.fullmatch(
                r"\$\.decision\.owner_actions\[(\d+)\]",
                location,
            ):
                index = int(match.group(1))
                if index >= len(source_decision.get("owner_actions") or []):
                    break
                del source_decision["owner_actions"][index]
                decision["owner_actions"] = list(
                    source_decision["owner_actions"]
                )
            elif match := re.fullmatch(
                r"\$\.findings\[(\d+)\]\.(headline|analysis|owner_action)",
                location,
            ):
                index = int(match.group(1))
                if index >= len(raw_findings):
                    break
                candidate = raw_findings[index]
                carriers = [
                    item
                    for item in raw_findings
                    if isinstance(item, dict)
                    and item.get("category")
                    not in {"test-gap", "question", "note"}
                    and item.get("required_evidence_refs")
                ]
                is_sole_blocking_carrier = bool(
                    verdict == "blocking"
                    and candidate in carriers
                    and len(carriers) == 1
                )
                field = match.group(2)
                if is_sole_blocking_carrier and field == "analysis":
                    replacement = bounded_text(candidate.get("headline"))
                    if replacement is None:
                        break
                    candidate["analysis"] = replacement
                elif is_sole_blocking_carrier:
                    break
                else:
                    del raw_findings[index]
            else:
                break
            state.normalize(
                f"{location}:supporting_ci_surface_contracted",
                partial=True,
            )
            core_taint = _supporting_ci_core_taint(
                source,
                context_meta=context_meta,
                state=state,
            )
    if core_taint is not None:
        location, _ref = core_taint
        state.add_issue(
            "supporting_ci_core_prose_tainted",
            location,
            (
                "core review prose depends on supporting-only generation-time "
                "CI"
            ),
            "truth",
        )
        return _nonpublishable(
            state,
            source=source,
            kind="supporting_ci_core_prose_tainted",
        )

    known_paths = _known_paths(state, pr_details)
    normalized_findings: list[Dict[str, Any]] = []
    v3_findings: list[Dict[str, Any]] = []
    seen_findings: set[tuple[Any, ...]] = set()
    for index, raw in enumerate(raw_findings[:MAX_FINDINGS]):
        item = _normalize_finding(
            raw,
            index=index,
            verdict=verdict,
            pr_details=pr_details,
            known_paths=known_paths,
            state=state,
        )
        if state.truth_failed:
            return _nonpublishable(
                state,
                source=source,
                kind="out_of_catalog_material_evidence",
            )
        if item is None:
            continue
        normalized, projected = item
        fingerprint = _finding_fingerprint(normalized)
        if fingerprint in seen_findings:
            state.normalize(
                f"$.findings[{index}]:exact_duplicate_removed",
                partial=True,
            )
            continue
        seen_findings.add(fingerprint)
        projected["id"] = f"F{len(v3_findings) + 1}"
        normalized_findings.append(normalized)
        v3_findings.append(projected)

    blocking_indexes: set[int] = set()
    if verdict == "blocking":
        blocking_indexes = {
            index
            for index, item in enumerate(normalized_findings)
            if item["priority"] in {"P0", "P1"}
        }
        if not blocking_indexes:
            for index, item in enumerate(normalized_findings):
                if (
                    item["category"] not in {"test-gap", "question", "note"}
                    and item["required_evidence_refs"]
                ):
                    blocking_indexes.add(index)
                    break
    if (
        verdict == "blocking"
        and state.blocking_decision_dependency_uncertain
        and blocking_indexes
    ):
        primary = normalized_findings[min(blocking_indexes)]
        replacement = bounded_text(
            "Changes are needed before merge: " + primary["headline"],
            limit=MAX_SUMMARY,
        )
        if replacement is None:
            state.add_issue(
                "blocking_decision_contraction_failed",
                "$.decision.summary",
                "retained blocker headline exceeds the public summary bound",
                "truth",
            )
            return _nonpublishable(
                state,
                source=source,
                kind="blocking_decision_contraction_failed",
            )
        decision["summary"] = replacement
        decision["owner_actions"] = [primary["owner_action"]]
        state.normalize(
            "$.decision:unsupported_blocker_contracted",
            partial=True,
        )
    for index, (normalized, projected) in enumerate(
        zip(normalized_findings, v3_findings)
    ):
        projected["blocking"] = index in blocking_indexes
        projected["comment"] = (
            normalized["analysis"]
            if projected["blocking"]
            else (
                f"{normalized['analysis']}\n\n"
                f"Owner action: {normalized['owner_action']}"
            )
        )

    normalized_unknowns: list[Dict[str, Any]] = []
    v3_unknowns: list[Dict[str, Any]] = []
    seen_unknowns: set[tuple[Any, ...]] = set()
    for index, raw in enumerate(raw_unknowns[:MAX_UNKNOWNS]):
        item = _normalize_unknown(
            raw,
            index=index,
            deciding=verdict == "verification_needed",
            clear_verdict=verdict == "clear",
            state=state,
        )
        if state.truth_failed:
            return _nonpublishable(
                state,
                source=source,
                kind="out_of_catalog_material_evidence",
            )
        if item is None:
            continue
        normalized, projected = item
        fingerprint = _unknown_fingerprint(normalized)
        if fingerprint in seen_unknowns:
            state.normalize(
                f"$.material_unknowns[{index}]:exact_duplicate_removed",
                partial=True,
            )
            continue
        seen_unknowns.add(fingerprint)
        projected["id"] = f"U{len(v3_unknowns) + 1}"
        normalized_unknowns.append(normalized)
        v3_unknowns.append(projected)

    raw_checks = source.get("confidence_checks")
    if raw_checks is None:
        state.normalize("$.confidence_checks:missing_optional_surface_defaulted")
        raw_checks = []
    if not isinstance(raw_checks, list):
        state.add_issue(
            "confidence_checks_shape_invalid",
            "$.confidence_checks",
            "confidence checks were not an array and were omitted",
            "surface",
        )
        raw_checks = []
    if len(raw_checks) > MAX_CONFIDENCE_CHECKS:
        state.normalize("$.confidence_checks:cap_applied", partial=True)
    checks = [
        item
        for index, raw in enumerate(raw_checks[:MAX_CONFIDENCE_CHECKS])
        if (item := _normalize_check(raw, index=index, state=state)) is not None
    ]
    unique_checks: list[Dict[str, Any]] = []
    seen_checks: set[tuple[Any, ...]] = set()
    for item in checks:
        fingerprint = (
            _fold(item["check"]),
            _fold(item["result"]),
            tuple(item["evidence_refs"]),
        )
        if fingerprint in seen_checks:
            state.normalize(
                "$.confidence_checks:exact_duplicate_removed",
                partial=True,
            )
            continue
        seen_checks.add(fingerprint)
        unique_checks.append(item)
    checks = unique_checks
    if verdict == "clear" and state.clear_decision_dependency_uncertain:
        # Final has no identity graph between a clear summary and optional
        # findings/unknowns. Once such an item is removed for invalid or
        # unsupported evidence, retaining arbitrary clauses from that summary
        # could publish the same unsupported claim through another surface.
        # Contract only the dependent decision surface; retained items and the
        # rest of the review remain available.
        supported_check_summary = _direct_check_clear_summary(
            checks,
            state=state,
        )
        decision["summary"] = (
            supported_check_summary or "No review blocker found."
        )
        decision["owner_actions"] = []
        state.normalize(
            "$.decision:optional_dependency_contracted",
            partial=True,
        )
        if supported_check_summary:
            state.normalize(
                "$.decision:supported_check_first_screen_retained",
                partial=True,
            )
    diagram = _normalize_diagram(
        source.get("diagram"),
        verdict=verdict,
        has_retained_blocker=any(
            item.get("blocking") is True for item in v3_findings
        ),
        state=state,
    )
    changed_core_taint = _changed_ci_core_taint(
        decision,
        normalized_findings,
        context_meta=context_meta,
        state=state,
    )
    if changed_core_taint is not None:
        location, _ref = changed_core_taint
        state.add_issue(
            "changed_ci_core_prose_tainted",
            location,
            "surviving core review prose depends on changed generation-time CI",
            "truth",
        )
        return _nonpublishable(
            state,
            source=source,
            kind="changed_ci_core_prose_tainted",
        )

    headline_count = 0
    inline_count = 0
    nonblocking_inline_count = 0
    for index, item in enumerate(v3_findings):
        visibility = item["visibility"]
        if visibility == "headline":
            if headline_count >= 2:
                item["visibility"] = "collapsed"
                normalized_findings[index]["placement"] = "collapsed"
                state.normalize(
                    f"$.findings[{index}].placement:headline_cap_collapsed",
                    partial=True,
                )
            else:
                headline_count += 1
        if item["visibility"] != "inline":
            continue
        nonblocking = item.get("blocking") is not True
        if inline_count >= 4 or (nonblocking and nonblocking_inline_count >= 1):
            item["visibility"] = "collapsed"
            normalized_findings[index]["placement"] = "collapsed"
            state.normalize(
                f"$.findings[{index}].placement:inline_cap_collapsed",
                partial=True,
            )
            continue
        inline_count += 1
        nonblocking_inline_count += int(nonblocking)

    blocking_ids = [
        item["id"] for item in v3_findings if item.get("blocking") is True
    ]
    critical_ids = [
        item["id"]
        for item in v3_findings
        if item.get("priority") in {"P0", "P1"}
    ]
    unknown_ids = [item["id"] for item in v3_unknowns]
    if verdict != "blocking" and critical_ids:
        state.add_issue(
            "decision_finding_contradiction",
            "$.decision.verdict",
            "a retained P0/P1 finding contradicts the non-blocking decision",
            "truth",
        )
        return _nonpublishable(
            state,
            source=source,
            kind="decision_finding_contradiction",
        )
    if verdict == "blocking" and not blocking_ids:
        state.add_issue(
            "deciding_item_lost",
            "$.findings",
            "blocking decision has no retained supported blocker",
            "representation",
        )
        return _nonpublishable(
            state,
            source=source,
            kind="deciding_item_loss",
        )
    if verdict == "verification_needed" and not unknown_ids:
        state.add_issue(
            "deciding_item_lost",
            "$.material_unknowns",
            "verification decision has no retained material unknown",
            "representation",
        )
        return _nonpublishable(
            state,
            source=source,
            kind="deciding_item_loss",
        )

    deciding_ids = (
        blocking_ids
        if verdict == "blocking"
        else unknown_ids if verdict == "verification_needed" else []
    )
    owner_actions: list[Dict[str, Any]] = []
    if deciding_ids:
        action = next(iter(decision["owner_actions"]), "")
        if not action:
            action = (
                normalized_findings[
                    next(
                        index
                        for index, item in enumerate(v3_findings)
                        if item["id"] in blocking_ids
                    )
                ]["owner_action"]
                if verdict == "blocking"
                else normalized_unknowns[0]["owner_action"]
            )
        owner_actions = [{"text": action, "resolves": deciding_ids}]

    if verdict == "blocking":
        reasons = [
            {"text": item["headline"], "refs": [item["id"]]}
            for item in v3_findings
            if item["id"] in blocking_ids
        ][:2]
    elif verdict == "verification_needed":
        reasons = [
            {"text": item["claim"], "refs": [item["id"]]}
            for item in v3_unknowns[:2]
        ]
    else:
        reasons = [
            {"text": f"{item['check']}: {item['result']}", "refs": []}
            for item in checks[:3]
        ]

    normalized_diagram = diagram
    v3_diagram = (
        {
            "purpose": diagram["purpose"],
            "description": diagram["caption"],
            "mermaid": diagram["mermaid"],
            "finding_refs": [],
            "evidence_refs": diagram["evidence_refs"],
        }
        if diagram
        else None
    )
    normalized_presentation = {
        "version": PRESENTATION_VERSION,
        "decision": decision,
        "findings": normalized_findings,
        "material_unknowns": normalized_unknowns,
        "confidence_checks": checks,
        "diagram": normalized_diagram,
    }

    analyzer = (context_meta or {}).get("analyzer_result") or {}
    pr_type = normalize_pr_type(analyzer.get("pr_type") or "code")
    if pr_type not in _PUBLIC_PR_TYPES:
        pr_type = "code"
    raw_v3 = {
        "schema_version": 3,
        "decision": {
            "verdict": VERDICT_TO_V3[verdict],
            "public_sentence": decision["summary"],
            "confidence": decision["confidence"].casefold(),
            "pr_type": pr_type,
            "risk_domains": [
                item
                for item in analyzer.get("risk_domains") or []
                if isinstance(item, str) and item.strip()
            ],
            "reasons": reasons,
        },
        "owner_action": owner_actions,
        "findings": v3_findings,
        "material_unknowns": v3_unknowns,
        "evidence_scope": _scope_refs(
            normalized_findings,
            normalized_unknowns,
            checks,
            diagram,
            context_meta,
        ),
        "diagram": v3_diagram,
        "rendering_plan": {
            "ci_public_state": _structured_ci_public_state(
                context_meta,
                checks,
            )
        },
    }
    try:
        review = build_v3_review(
            raw_v3,
            pr_details,
            context_meta,
            strict=True,
        )
    except Exception as error:
        state.add_issue(
            "public_projection_failed",
            "$",
            (
                "deterministic public projection failed: "
                f"{type(error).__name__}: {str(error)[:500]}"
            ),
            "truth",
        )
        return _nonpublishable(
            state,
            source=source,
            presentation=normalized_presentation,
            kind="public_projection_failure",
        )

    comment = review.get("pr_review_comment")
    inline = review.get("inline_comments")
    if not isinstance(comment, str) or not comment.strip() or not isinstance(
        inline, list
    ):
        state.add_issue(
            "public_payload_incomplete",
            "$",
            "public projection lacks a complete comment/list payload",
            "truth",
        )
        return _nonpublishable(
            state,
            source=source,
            presentation=normalized_presentation,
            kind="unsafe_public_payload",
        )
    identities = collect_private_identities(
        review.get("v3_review") or {},
        context_meta,
    )
    public_atoms = [comment] + [
        f"{item.get('comment') or ''}\n{item.get('suggested_code') or ''}"
        for item in inline
        if isinstance(item, dict)
    ]
    if any(contains_private_identity(atom, identities) for atom in public_atoms):
        state.add_issue(
            "private_identity_leak",
            "$",
            "public projection retains a private exact identity",
            "truth",
        )
        return _nonpublishable(
            state,
            source=source,
            presentation=normalized_presentation,
            kind="private_identity_leak",
        )
    review.update(
        {
            "presentation_v1": deepcopy(normalized_presentation),
            "review_generation_status": "complete",
            "review_publishable": True,
            "review_publication_safe": True,
            "review_fallback_used": False,
        }
    )
    return PresentationResult(
        status="publishable",
        review=review,
        presentation=normalized_presentation,
        issues=tuple(state.issues),
        normalizations=tuple(dict.fromkeys(state.normalizations)),
        safe_partial=state.safe_partial,
    )

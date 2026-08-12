"""Finding and evidence-capability validation for public review schema v3."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .evidence_contract import (
    catalog_entries,
    changed_postimages,
    changed_preimages,
    ci_annotation_proves_finding_path,
    ci_ref_matches_actionable_diagnostic,
    classify_changed_region_anchor,
    deleted_region_supporting_refs,
    entry_head_sha,
    entry_paths,
    entry_supports_claim,
    entry_supports_expected_head,
    expected_head_sha,
    normalize_repo_path,
    search_entry_supports_exact_head,
)
from .v3_validation_common import (
    ALLOWED_CLAIM_SCOPES,
    ALLOWED_CONFIDENCE,
    ALLOWED_EVIDENCE_STATUS,
    ALLOWED_FINDING_TYPES,
    ALLOWED_PRIORITIES,
    ALLOWED_VISIBILITY,
    BLOCKING_PRIORITIES,
    FINDING_ID_RE,
    MAX_HEADLINE_FINDINGS,
    MAX_INLINE_FINDINGS,
    MAX_NONBLOCKING_INLINE_FINDINGS,
    V3ValidationState,
    text,
    validate_string_refs,
    violation,
)


def _path_provenance(
    pr_details: str,
    context_meta: Optional[Dict[str, Any]],
) -> Tuple[set[str], set[str]]:
    changed = {
        normalize_repo_path(path)
        for path in (
            set(changed_postimages(pr_details))
            | set(changed_preimages(pr_details))
        )
    }
    catalog = catalog_entries(context_meta)
    observed = {
        path
        for entry in catalog.values()
        if entry_supports_claim(entry)
        for path in entry_paths(entry)
    }
    return changed, observed


def _catalog_ref_has_same_path_coverage(
    refs: Iterable[str],
    *,
    file_path: str,
    coverage_type: str,
    catalog: Dict[str, Dict[str, Any]],
) -> bool:
    target = normalize_repo_path(file_path)
    return any(
        ref in catalog
        and entry_supports_claim(catalog[ref])
        and text(catalog[ref].get("coverage_type")) == coverage_type
        and target in entry_paths(catalog[ref])
        for ref in refs
    )


def _catalog_ref_has_exact_representation(
    refs: Iterable[str],
    *,
    file_path: str,
    requirement: str,
    catalog: Dict[str, Dict[str, Any]],
) -> bool:
    """Require typed source provenance for representation-sensitive claims.

    The model owns whether a finding depends on literal representation.  This
    capability only checks the declared requirement against exact-head source
    provenance; it never guesses from filenames, languages, or prose.
    """

    target = normalize_repo_path(file_path)
    for ref in refs:
        entry = catalog.get(ref) or {}
        if not entry_supports_claim(entry) or target not in entry_paths(entry):
            continue
        coverage = text(entry.get("coverage_type"))
        source_type = text(entry.get("source_type"))
        observed_state = text(entry.get("observed_state"))
        if requirement == "exact_postimage":
            if source_type == "diff" and coverage == "changed_region":
                return True
            if (
                source_type == "pfr"
                and coverage in {"file_slice", "full_file"}
                and observed_state == "content_observed"
            ):
                return True
        elif requirement == "exact_full_file" and (
            source_type == "pfr"
            and coverage == "full_file"
            and observed_state == "content_observed"
        ):
            return True
    return False


def finding_evidence_capability(
    finding: Dict[str, Any],
    *,
    pr_details: str,
    context_meta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute mechanical evidence capability without judging finding prose.

    The model still decides whether the causal claim is true. This function
    only answers whether the cited observation types can honestly carry the
    requested scope and P0/P1 status.
    """

    catalog = catalog_entries(context_meta)
    expected_head = expected_head_sha(context_meta)
    dependency_refs = (
        finding.get("required_evidence_refs")
        if (
            "required_evidence_refs" in finding
            or "supporting_evidence_refs" in finding
        )
        else finding.get("evidence_refs")
    )
    refs = [
        str(ref)
        for ref in dependency_refs or []
        if isinstance(ref, str)
    ]
    file_path = normalize_repo_path(finding.get("file_path"))
    snippet = finding.get("code_snippet")
    anchor_class = (
        classify_changed_region_anchor(file_path, snippet, pr_details)
        if isinstance(snippet, str)
        else "invalid"
    )
    snippet_is_post_change = anchor_class == "post_change"
    snippet_is_deleted_region = anchor_class == "deleted_region"
    snippet_is_changed_region = anchor_class != "invalid"
    changed_paths, _observed_paths = _path_provenance(
        pr_details,
        context_meta,
    )
    usable_refs: List[str] = []
    substantive_refs: List[str] = []
    critical_independent_refs: List[str] = []
    ci_dependency_refs: List[str] = []
    deleted_region_refs = deleted_region_supporting_refs(
        refs,
        context_meta,
    )
    rejected_refs: List[str] = []

    required_refs = [
        str(ref)
        for ref in finding.get("required_evidence_refs") or []
        if isinstance(ref, str)
    ]
    non_source_ci_diagnostic_supported = bool(
        not file_path
        and not str(snippet or "").strip()
        and finding.get("visibility") != "inline"
        and required_refs
        and all(
            text((catalog.get(ref) or {}).get("source_type")) == "ci"
            for ref in required_refs
        )
        and any(
            ci_ref_matches_actionable_diagnostic(
                ref,
                (finding.get("headline"), finding.get("comment")),
                context_meta,
            )
            for ref in required_refs
        )
    )

    def carries_exact_code_capability(entry: Mapping[str, Any]) -> bool:
        source_type = text(entry.get("source_type"))
        coverage = text(entry.get("coverage_type"))
        if source_type in {
            "ci",
            "pr_body",
            "author_report",
            "dependabot_body",
        }:
            return False
        paths = {
            normalize_repo_path(path)
            for path in entry_paths(entry)
            if normalize_repo_path(path)
        }
        if file_path and paths and file_path not in paths:
            return False
        if coverage in {"changed_region", "full_file", "file_slice"}:
            return True
        if coverage == "search_snippet":
            return True
        if coverage == "exact_path_state":
            return bool(
                snippet_is_changed_region
                and any(path and path in str(snippet) for path in paths)
            )
        return False

    for ref in refs:
        entry = catalog.get(ref) or {}
        if not entry or not entry_supports_claim(entry):
            rejected_refs.append(ref)
            continue
        if not entry_supports_expected_head(
            entry,
            expected_head=expected_head,
        ):
            rejected_refs.append(ref)
            continue
        entry_head = entry_head_sha(entry)
        if expected_head and entry_head and entry_head != expected_head:
            rejected_refs.append(ref)
            continue
        if not search_entry_supports_exact_head(
            entry,
            expected_head=expected_head,
        ):
            rejected_refs.append(ref)
            continue
        source_type = text(entry.get("source_type"))
        coverage = text(entry.get("coverage_type"))
        if source_type == "ci":
            # Required CI can be a deciding dependency, but never proves code
            # scope, path, snippet, or causality. An independent required
            # changed/head observation must carry that capability.
            usable_refs.append(ref)
            ci_dependency_refs.append(ref)
            continue

        usable_refs.append(ref)
        if coverage in {"changed_region", "full_file", "file_slice"}:
            substantive_refs.append(ref)
            if carries_exact_code_capability(entry):
                critical_independent_refs.append(ref)
        elif coverage == "search_snippet":
            # Exact-head relocation was already checked above.
            substantive_refs.append(ref)
            if carries_exact_code_capability(entry):
                critical_independent_refs.append(ref)
        elif coverage == "exact_path_state":
            # Exact-path evidence proves only the literal path. For a critical
            # causal precondition that literal must be visible in the changed
            # snippet; it remains auxiliary for bounded-context scope.
            if carries_exact_code_capability(entry):
                critical_independent_refs.append(ref)

    # Final's evidence-role classification remains intact: required CI still
    # carries the deciding observed outcome. A separately cited supporting
    # exact-head observation may prove only the code/path capability that CI
    # cannot prove. It does not become a required causal dependency or change
    # the finding's severity.
    if ci_dependency_refs and not critical_independent_refs:
        for ref in finding.get("supporting_evidence_refs") or []:
            if not isinstance(ref, str):
                continue
            entry = catalog.get(ref) or {}
            if (
                entry
                and entry_supports_claim(entry)
                and entry_supports_expected_head(
                    entry,
                    expected_head=expected_head,
                )
                and (
                    not expected_head
                    or not entry_head_sha(entry)
                    or entry_head_sha(entry) == expected_head
                )
                and search_entry_supports_exact_head(
                    entry,
                    expected_head=expected_head,
                )
                and carries_exact_code_capability(entry)
            ):
                critical_independent_refs.append(ref)

    if (
        ci_dependency_refs
        and not critical_independent_refs
        and not non_source_ci_diagnostic_supported
    ):
        rejected_refs.extend(ci_dependency_refs)

    claim_scope = text(finding.get("claim_scope"))
    if snippet_is_deleted_region:
        scope_supported = bool(
            claim_scope == "bounded_context"
            and deleted_region_refs
        )
    elif claim_scope == "repository":
        scope_supported = False
    elif claim_scope == "whole_file":
        scope_supported = _catalog_ref_has_same_path_coverage(
            usable_refs,
            file_path=file_path,
            coverage_type="full_file",
            catalog=catalog,
        )
    elif claim_scope == "bounded_context":
        scope_supported = bool(
            substantive_refs or non_source_ci_diagnostic_supported
        )
    elif claim_scope == "changed_region":
        scope_supported = bool(file_path and file_path in changed_paths)
    else:
        scope_supported = False

    representation_requirement = text(
        finding.get("representation_requirement") or "semantic"
    )
    if representation_requirement == "semantic":
        representation_supported = True
    elif representation_requirement in {
        "exact_postimage",
        "exact_full_file",
    }:
        representation_supported = bool(
            snippet_is_post_change
            and pr_details
            and _catalog_ref_has_exact_representation(
                required_refs,
                file_path=file_path,
                requirement=representation_requirement,
                catalog=catalog,
            )
        )
    else:
        representation_supported = False

    return {
        "usable_refs": usable_refs,
        "rejected_refs": rejected_refs,
        "substantive_refs": substantive_refs,
        "critical_independent_refs": critical_independent_refs,
        "scope_supported": scope_supported,
        "anchor_class": anchor_class,
        "snippet_is_changed_region": snippet_is_changed_region,
        "snippet_is_post_change": snippet_is_post_change,
        "snippet_is_deleted_region": snippet_is_deleted_region,
        "deleted_region_refs": deleted_region_refs,
        "ci_dependency_refs": ci_dependency_refs,
        "non_source_ci_diagnostic_supported": (
            non_source_ci_diagnostic_supported
        ),
        "representation_requirement": representation_requirement,
        "representation_supported": representation_supported,
        "critical_supported": bool(
            scope_supported
            and representation_supported
            and (
                deleted_region_refs
                if snippet_is_deleted_region
                else (
                    critical_independent_refs
                    or non_source_ci_diagnostic_supported
                )
            )
        ),
    }


def validate_findings(state: V3ValidationState) -> None:
    """Validate findings and their exact evidence capabilities."""

    raw = state.raw
    context_meta = state.context_meta
    pr_details = state.pr_details
    violations = state.violations
    warnings = state.warnings
    catalog = state.catalog
    expected_head = state.expected_head

    findings = raw.get("findings")
    finding_ids: set[str] = set()
    blocking_ids: set[str] = set()
    if not isinstance(findings, list):
        violations.append(
            violation(
                "field_type_invalid",
                "$.findings",
                "findings must be an array",
            )
        )
        findings = []

    required_finding = {
        "id",
        "finding_type",
        "priority",
        "confidence",
        "evidence_status",
        "claim_scope",
        "blocking",
        "visibility",
        "headline",
        "file_path",
        "code_snippet",
        "comment",
        "evidence_refs",
    }
    allowed_finding = required_finding | {
        "required_evidence_refs",
        "supporting_evidence_refs",
        "representation_requirement",
        "suggested_code",
        "suggestion_type",
    }
    changed_paths, observed_paths = _path_provenance(
        pr_details,
        context_meta,
    )

    headline_count = 0
    inline_count = 0
    nonblocking_inline_count = 0
    for index, finding in enumerate(findings):
        location = f"$.findings[{index}]"
        if not isinstance(finding, dict):
            violations.append(
                violation(
                    "field_type_invalid",
                    location,
                    f"{location} must be an object",
                )
            )
            continue
        for key in sorted(required_finding - set(finding)):
            violations.append(
                violation(
                    "required_field_missing",
                    f"{location}.{key}",
                    f"{location}.{key} is required",
                )
            )
        warnings.extend(
            f"ignored extra findings[{index}] field: {key}"
            for key in sorted(set(finding) - allowed_finding)
        )
        finding_id = text(finding.get("id"))
        if not FINDING_ID_RE.fullmatch(finding_id):
            violations.append(
                violation(
                    "id_contract_invalid",
                    f"{location}.id",
                    f"{location}.id must match F[1-9][0-9]*",
                )
            )
        elif finding_id in finding_ids:
            violations.append(
                violation(
                    "id_contract_invalid",
                    f"{location}.id",
                    f"duplicate finding id {finding_id!r}",
                )
            )
        else:
            finding_ids.add(finding_id)
        if finding.get("finding_type") not in ALLOWED_FINDING_TYPES:
            violations.append(
                violation(
                    "enum_invalid",
                    f"{location}.finding_type",
                    f"{location}.finding_type has an invalid enum value",
                )
            )
        if finding.get("priority") not in ALLOWED_PRIORITIES:
            violations.append(
                violation(
                    "enum_invalid",
                    f"{location}.priority",
                    f"{location}.priority has an invalid enum value",
                )
            )
        if finding.get("confidence") not in ALLOWED_CONFIDENCE:
            violations.append(
                violation(
                    "enum_invalid",
                    f"{location}.confidence",
                    f"{location}.confidence has an invalid enum value",
                )
            )
        if finding.get("evidence_status") not in ALLOWED_EVIDENCE_STATUS:
            violations.append(
                violation(
                    "enum_invalid",
                    f"{location}.evidence_status",
                    f"{location}.evidence_status has an invalid enum value",
                )
            )
        if finding.get("representation_requirement", "semantic") not in {
            "semantic",
            "exact_postimage",
            "exact_full_file",
        }:
            violations.append(
                violation(
                    "enum_invalid",
                    f"{location}.representation_requirement",
                    f"{location}.representation_requirement has an invalid enum value",
                )
            )
        claim_scope = finding.get("claim_scope")
        if claim_scope not in ALLOWED_CLAIM_SCOPES:
            violations.append(
                violation(
                    "enum_invalid",
                    f"{location}.claim_scope",
                    f"{location}.claim_scope has an invalid enum value",
                )
            )
        if type(finding.get("blocking")) is not bool:
            violations.append(
                violation(
                    "field_type_invalid",
                    f"{location}.blocking",
                    f"{location}.blocking must be a boolean",
                )
            )
        if finding.get("visibility") not in ALLOWED_VISIBILITY:
            violations.append(
                violation(
                    "enum_invalid",
                    f"{location}.visibility",
                    f"{location}.visibility has an invalid enum value",
                )
            )
        for key in ("headline", "comment"):
            if not isinstance(finding.get(key), str) or not finding.get(
                key,
                "",
            ).strip():
                violations.append(
                    violation(
                        "field_type_invalid",
                        f"{location}.{key}",
                        f"{location}.{key} must be a non-empty string",
                    )
                )
        if not isinstance(finding.get("file_path"), str):
            violations.append(
                violation(
                    "field_type_invalid",
                    f"{location}.file_path",
                    f"{location}.file_path must be a string",
                )
            )
        if not isinstance(finding.get("code_snippet"), str):
            violations.append(
                violation(
                    "field_type_invalid",
                    f"{location}.code_snippet",
                    f"{location}.code_snippet must be a string",
                )
            )

        file_path = normalize_repo_path(finding.get("file_path"))
        evidence_refs = validate_string_refs(
            finding.get("evidence_refs"),
            f"{location}.evidence_refs",
            violations,
            allowed=set(catalog),
            require_nonempty=True,
        )
        split_declared = (
            "required_evidence_refs" in finding
            or "supporting_evidence_refs" in finding
        )
        if split_declared:
            required_evidence_refs = validate_string_refs(
                finding.get("required_evidence_refs"),
                f"{location}.required_evidence_refs",
                violations,
                allowed=set(catalog),
            )
            supporting_evidence_refs = validate_string_refs(
                finding.get("supporting_evidence_refs"),
                f"{location}.supporting_evidence_refs",
                violations,
                allowed=set(catalog),
            )
            overlap = set(required_evidence_refs) & set(
                supporting_evidence_refs
            )
            if overlap:
                violations.append(
                    violation(
                        "evidence_dependency_invalid",
                        f"{location}.supporting_evidence_refs",
                        f"{location} declares the same evidence as required and supporting",
                    )
                )
            declared_union = list(
                dict.fromkeys(
                    required_evidence_refs + supporting_evidence_refs
                )
            )
            if declared_union != evidence_refs:
                violations.append(
                    violation(
                        "evidence_dependency_invalid",
                        f"{location}.evidence_refs",
                        f"{location}.evidence_refs must be the ordered union of required_evidence_refs and supporting_evidence_refs",
                    )
                )
        for ref in evidence_refs:
            entry = catalog.get(ref) or {}
            if not entry_supports_claim(entry):
                violations.append(
                    violation(
                        "evidence_ref_invalid",
                        f"{location}.evidence_refs",
                        f"{location}.evidence_refs includes a non-supporting outcome",
                    )
                )
            if not entry_supports_expected_head(
                entry,
                expected_head=expected_head,
            ):
                violations.append(
                    violation(
                        "evidence_head_lineage_missing",
                        f"{location}.evidence_refs",
                        f"{location}.evidence_refs includes repository evidence without exact queued-head lineage",
                    )
                )
            entry_head = entry_head_sha(entry)
            if entry_head:
                state.referenced_head_shas.add(entry_head)
                if expected_head and entry_head != expected_head:
                    violations.append(
                        violation(
                            "evidence_head_mismatch",
                            f"{location}.evidence_refs",
                            f"{location}.evidence_refs includes a different PR head",
                        )
                    )
            if (
                finding.get("evidence_status") == "verified"
                and not search_entry_supports_exact_head(
                    entry,
                    expected_head=expected_head,
                )
            ):
                violations.append(
                    violation(
                        "evidence_branch_lineage_mismatch",
                        f"{location}.evidence_refs",
                        f"{location}.evidence_refs includes a search event whose hits were not all relocated to the same PR head",
                    )
                )
        if (
            file_path
            and file_path not in changed_paths | observed_paths
            and not ci_annotation_proves_finding_path(
                finding,
                pr_details,
                context_meta,
            )
        ):
            violations.append(
                violation(
                    "finding_path_provenance_mismatch",
                    f"{location}.file_path",
                    f"{location}.file_path lacks changed or catalog provenance",
                )
            )
        snippet = finding.get("code_snippet")
        anchor_class = (
            classify_changed_region_anchor(
                str(finding.get("file_path") or ""),
                snippet,
                pr_details,
            )
            if isinstance(snippet, str)
            else "invalid"
        )
        snippet_is_changed_region = anchor_class != "invalid"
        snippet_is_post_change = anchor_class == "post_change"
        snippet_is_deleted_region = anchor_class == "deleted_region"
        capability = finding_evidence_capability(
            finding,
            pr_details=pr_details,
            context_meta=context_meta,
        )
        empty_path_allowed = bool(
            not file_path
            and finding.get("visibility") != "inline"
            and evidence_refs
            and (
                finding.get("priority") == "P2"
                or capability["non_source_ci_diagnostic_supported"]
            )
        )
        if not file_path and not empty_path_allowed:
            violations.append(
                violation(
                    "finding_path_provenance_mismatch",
                    f"{location}.file_path",
                    f"{location}.file_path lacks an admitted non-source evidence capability",
                )
            )
        if (
            isinstance(snippet, str)
            and snippet.strip()
            and not snippet_is_changed_region
        ):
            violations.append(
                violation(
                    "snippet_contract_invalid",
                    f"{location}.code_snippet",
                    f"{location}.code_snippet is not a contiguous changed region in either the post-change or deleted image",
                )
            )
        if finding.get("blocking") is True:
            if finding.get("finding_type") in {
                "test-gap",
                "question",
            }:
                violations.append(
                    violation(
                        "cross_field_invariant",
                        location,
                        f"{location}.blocking is not allowed for this finding type",
                    )
                )
            if finding.get("evidence_status") != "verified":
                violations.append(
                    violation(
                        "blocking_evidence_mismatch",
                        location,
                        f"{location}.blocking requires verified evidence",
                    )
                )
            if (
                isinstance(snippet, str)
                and snippet.strip()
                and not snippet_is_changed_region
            ):
                violations.append(
                    violation(
                        "snippet_contract_invalid",
                        f"{location}.code_snippet",
                        f"{location}.code_snippet is not a contiguous changed region in either the post-change or deleted image",
                    )
                )
            if finding_id:
                blocking_ids.add(finding_id)
        if (
            finding.get("priority") in BLOCKING_PRIORITIES
            and isinstance(snippet, str)
            and snippet.strip()
            and not snippet_is_changed_region
        ):
            violations.append(
                violation(
                    "snippet_contract_invalid",
                    f"{location}.code_snippet",
                    f"{location} P0/P1 requires a changed-region anchor",
                )
            )
        if (
            finding.get("visibility") == "inline"
            and not snippet_is_post_change
        ):
            violations.append(
                violation(
                    "snippet_contract_invalid",
                    f"{location}.code_snippet",
                    f"{location}.visibility=inline requires a post-change snippet",
                )
            )
        if finding.get("visibility") == "inline" and finding.get(
            "evidence_status"
        ) != "verified":
            violations.append(
                violation(
                    "cross_field_invariant",
                    location,
                    f"{location}.visibility=inline requires verified evidence",
                )
            )
        if (
            snippet_is_deleted_region
            and finding.get("suggestion_type") == "DIRECT_REPLACEMENT"
        ):
            violations.append(
                violation(
                    "cross_field_invariant",
                    f"{location}.suggestion_type",
                    f"{location}.deleted-region findings cannot use DIRECT_REPLACEMENT",
                )
            )
        if (
            snippet_is_deleted_region
            and finding.get("priority") in BLOCKING_PRIORITIES
            and finding.get("evidence_status") == "verified"
            and finding.get("claim_scope") != "bounded_context"
        ):
            violations.append(
                violation(
                    "claim_scope_coverage_mismatch",
                    f"{location}.claim_scope",
                    f"{location}.deleted-region P0/P1 requires claim_scope=bounded_context",
                )
            )
        if capability["rejected_refs"]:
            violations.append(
                violation(
                    "evidence_capability_mismatch",
                    f"{location}.evidence_refs",
                    f"{location}.evidence_refs includes evidence that cannot support a finding",
                )
            )
        if (
            finding.get("evidence_status") == "verified"
            or finding.get("blocking") is True
        ) and not capability["scope_supported"]:
            violations.append(
                violation(
                    "claim_scope_coverage_mismatch",
                    f"{location}.evidence_refs",
                    f"{location}.claim_scope exceeds the cited evidence capability",
                )
            )
        if (
            finding.get("priority") in BLOCKING_PRIORITIES
            and finding.get("evidence_status") == "verified"
            and not capability["critical_supported"]
        ):
            violations.append(
                violation(
                    "critical_evidence_capability_mismatch",
                    f"{location}.evidence_refs",
                    f"{location} P0/P1 lacks independent changed-code evidence capability",
                )
            )

        if finding.get("visibility") == "headline":
            headline_count += 1
        if finding.get("visibility") == "inline":
            inline_count += 1
            if finding.get("blocking") is not True:
                nonblocking_inline_count += 1

        suggested = finding.get("suggested_code")
        suggestion_type = finding.get("suggestion_type")
        if suggested is not None and not isinstance(suggested, str):
            violations.append(
                violation(
                    "field_type_invalid",
                    f"{location}.suggested_code",
                    f"{location}.suggested_code must be a string or null",
                )
            )
        if suggestion_type is not None and suggestion_type not in {
            "DIRECT_REPLACEMENT",
            "CONCEPTUAL_ADVICE",
        }:
            violations.append(
                violation(
                    "enum_invalid",
                    f"{location}.suggestion_type",
                    f"{location}.suggestion_type has an invalid enum value",
                )
            )
        if suggested and suggestion_type is None:
            violations.append(
                violation(
                    "required_field_missing",
                    f"{location}.suggestion_type",
                    f"{location}.suggestion_type is required with suggested_code",
                )
            )

    if headline_count > MAX_HEADLINE_FINDINGS:
        violations.append(
            violation(
                "visibility_cap_exceeded",
                "$.findings",
                "headline finding cap exceeded",
            )
        )
    if inline_count > MAX_INLINE_FINDINGS:
        violations.append(
            violation(
                "visibility_cap_exceeded",
                "$.findings",
                "inline finding cap exceeded",
            )
        )
    if nonblocking_inline_count > MAX_NONBLOCKING_INLINE_FINDINGS:
        violations.append(
            violation(
                "visibility_cap_exceeded",
                "$.findings",
                "non-blocking inline finding cap exceeded",
            )
        )

    state.findings = findings
    state.finding_ids = finding_ids
    state.blocking_ids = blocking_ids

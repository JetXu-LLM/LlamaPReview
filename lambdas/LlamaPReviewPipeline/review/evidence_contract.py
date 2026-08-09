"""Mechanical evidence, exact-head, changed-anchor, and contract primitives.

This module is the deterministic boundary between repository observations and
review judgment.  It does not decide whether a change is correct, assign
severity, or synthesize a merge recommendation.  It only owns:

* stable CI observation identities and classifications;
* the catalog of evidence a model may cite;
* exact changed/deleted-region anchor checks;
* exact-head capability checks for cited repository observations; and
* typed contract errors shared by presentation and projection.

All active callers import this capability directly; no historical review
projection is packaged with the production Lambda.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import textwrap
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..context_engine.evidence import event_supports_answer


CI_BLOCKING_VALUES = {
    "failure",
    "failed",
    "error",
    "timed_out",
    "timed out",
    "startup_failure",
}
CI_ACTION_REQUIRED_VALUES = {"action_required", "action required"}
CI_PENDING_VALUES = {
    "queued",
    "requested",
    "waiting",
    "pending",
    "in_progress",
    "in progress",
}
CI_INCOMPLETE_VALUES = {"cancelled", "canceled", "stale"}
CI_SUCCESS_VALUES = {"success", "successful", "neutral", "skipped"}
SUPPORTING_OUTCOMES = {
    "hit",
    "success",
    "failure",
    "action_required",
    "pending",
    "incomplete",
}
OBJECTIVE_COVERAGE_TYPES = {
    "changed_region",
    "search_snippet",
    "file_slice",
    "full_file",
    "directory_inventory",
    "exact_path_state",
}
HEAD_SOURCE_RE = re.compile(
    r"^pr_head(?:_inventory|_tree)?:([0-9a-fA-F]{7,64})$"
)


def _text(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) and value.strip() else default


def _ci_value(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _bounded_ci_detail(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _ci_actionable_details(item: Dict[str, Any]) -> Dict[str, Any]:
    details: Dict[str, Any] = {}
    raw_output = item.get("output") if isinstance(item.get("output"), dict) else {}
    output = {
        key: rendered
        for key, rendered in (
            ("title", _bounded_ci_detail(raw_output.get("title"), 500)),
            ("summary", _bounded_ci_detail(raw_output.get("summary"), 2_000)),
            ("text", _bounded_ci_detail(raw_output.get("text"), 2_000)),
            ("log_tail", _bounded_ci_detail(raw_output.get("log_tail"), 6_000)),
        )
        if rendered
    }
    if output:
        details["output"] = output
    annotations: List[Dict[str, Any]] = []
    for raw in (item.get("annotations") or [])[:12]:
        if not isinstance(raw, dict):
            continue
        annotation = {
            key: value
            for key, value in {
                "path": _bounded_ci_detail(raw.get("path"), 500),
                "start_line": raw.get("start_line"),
                "end_line": raw.get("end_line"),
                "annotation_level": _bounded_ci_detail(
                    raw.get("annotation_level"), 40
                ),
                "title": _bounded_ci_detail(raw.get("title"), 500),
                "message": _bounded_ci_detail(raw.get("message"), 2_000),
            }.items()
            if value not in (None, "")
        }
        if annotation:
            annotations.append(annotation)
    if annotations:
        details["annotations"] = annotations
    return details


def _ci_check_identity(source: str, item: Dict[str, Any], name: str) -> str:
    if source == "status":
        return f"status:{name}"
    stable = item.get("id") or item.get("html_url")
    if not stable:
        app = item.get("app") if isinstance(item.get("app"), dict) else {}
        suite = (
            item.get("check_suite")
            if isinstance(item.get("check_suite"), dict)
            else {}
        )
        stable = "|".join(
            str(value or "")
            for value in (
                item.get("details_url") or item.get("target_url"),
                name,
                item.get("check_suite_id") or suite.get("id"),
                item.get("app_slug") or app.get("slug"),
            )
        )
    return f"{source}:{stable}"


def _ci_check_class(status: str, conclusion: str, *, source: str) -> str:
    value = conclusion or (status if source == "status" else "")
    if value in CI_BLOCKING_VALUES:
        return "failure"
    if value in CI_ACTION_REQUIRED_VALUES:
        return "action_required"
    if value in CI_INCOMPLETE_VALUES:
        return "incomplete"
    if value in CI_SUCCESS_VALUES:
        return "success"
    if source == "check_run" and status == "completed" and not conclusion:
        return "incomplete"
    if status in CI_PENDING_VALUES:
        return "pending"
    if source == "check_run" and status == "completed":
        return "incomplete"
    return "incomplete"


def build_ci_snapshot(ci_cd_results: Any) -> Dict[str, Any]:
    """Build a stable, lossless snapshot from exact GitHub CI facts."""

    raw = ci_cd_results if isinstance(ci_cd_results, dict) else {}
    retrieval_meta = (
        raw.get("_retrieval_meta")
        if isinstance(raw.get("_retrieval_meta"), dict)
        else {}
    )
    aggregate_meta = (
        retrieval_meta.get("ci_aggregate")
        if isinstance(retrieval_meta.get("ci_aggregate"), dict)
        else {}
    )
    retrieval_outcome = str(
        aggregate_meta.get("outcome") or ""
    ).strip().lower()
    commit_status_state = _ci_value(raw.get("state") or raw.get("status"))
    candidates: List[Dict[str, Any]] = []
    for source, items in (
        ("status", raw.get("statuses")),
        ("check_run", raw.get("check_runs")),
    ):
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            name = _text(item.get("context") or item.get("name"), "unknown")
            status = _ci_value(
                item.get("state") if source == "status" else item.get("status")
            )
            conclusion = _ci_value(item.get("conclusion"))
            candidate = {
                "identity": _ci_check_identity(source, item, name),
                "source": source,
                "name": name,
                "status": status,
                "conclusion": conclusion,
                "classification": _ci_check_class(
                    status, conclusion, source=source
                ),
                "details_url": str(
                    item.get("details_url")
                    or item.get("target_url")
                    or item.get("html_url")
                    or ""
                ),
                "updated_at": str(
                    item.get("updated_at")
                    or item.get("completed_at")
                    or item.get("created_at")
                    or ""
                ),
            }
            if source == "check_run":
                candidate.update(_ci_actionable_details(item))
            candidates.append(candidate)

    by_identity: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        existing = by_identity.get(item["identity"])
        if existing is None or item["updated_at"] >= existing["updated_at"]:
            by_identity[item["identity"]] = item
    checks = list(by_identity.values())
    commit_status_classification = (
        _ci_check_class(
            commit_status_state,
            commit_status_state,
            source="status",
        )
        if commit_status_state
        else ""
    )
    if commit_status_state and (
        not checks
        or (
            commit_status_classification != "success"
            and not any(
                item.get("classification") == commit_status_classification
                for item in checks
            )
        )
    ):
        checks.append(
            {
                "identity": "commit_status_aggregate",
                "source": "aggregate",
                "name": "commit status aggregate",
                "status": commit_status_state,
                "conclusion": commit_status_state,
                "classification": commit_status_classification,
                "details_url": "",
                "updated_at": "",
            }
        )
    if retrieval_outcome in {"partial", "error"}:
        checks.append(
            {
                "identity": "ci_retrieval:aggregate",
                "source": "retrieval",
                "name": "current-head CI evidence retrieval",
                "status": retrieval_outcome,
                "conclusion": "",
                "classification": "incomplete",
                "details_url": "",
                "updated_at": "",
            }
        )
    blocking = [
        item for item in checks if item["classification"] == "failure"
    ]
    action_required = [
        item
        for item in checks
        if item["classification"] == "action_required"
    ]
    pending = [
        item for item in checks if item["classification"] == "pending"
    ]
    incomplete = [
        item for item in checks if item["classification"] == "incomplete"
    ]
    classifications = {
        str(item.get("classification") or "") for item in checks
    }
    aggregate_classification = next(
        (
            value
            for value in (
                "failure",
                "action_required",
                "pending",
                "incomplete",
                "success",
            )
            if value in classifications
        ),
        "none",
    )
    return {
        "schema_version": 1,
        "source": "structured_raw",
        "has_ci": bool(raw) and (bool(checks) or bool(commit_status_state)),
        "commit_status_state": commit_status_state,
        "aggregate_classification": aggregate_classification,
        "overall_state": commit_status_state,
        "retrieval_outcome": retrieval_outcome or "untyped",
        "actionable_detail_retrieval": (
            retrieval_meta.get("ci_actionable_details")
            if isinstance(
                retrieval_meta.get("ci_actionable_details"), dict
            )
            else {}
        ),
        "checks": checks,
        "blocking_checks": blocking,
        "action_required_checks": action_required,
        "pending_checks": pending,
        "incomplete_checks": incomplete,
    }


def order_ci_checks_for_model(checks: Any) -> List[Dict[str, Any]]:
    """Bound model CI facts without allowing green rows to hide risk."""

    classification_rank = {
        "failure": 0,
        "action_required": 1,
        "pending": 2,
        "incomplete": 3,
        "success": 4,
    }
    source_rank = {
        "check_run": 0,
        "status": 1,
        "aggregate": 2,
        "retrieval": 3,
    }
    candidates = (
        [item for item in checks if isinstance(item, dict)]
        if isinstance(checks, list)
        else []
    )
    indexed = list(enumerate(candidates))
    indexed.sort(
        key=lambda pair: (
            classification_rank.get(
                str(pair[1].get("classification") or ""), 5
            ),
            0 if pair[1].get("annotations") or pair[1].get("output") else 1,
            source_rank.get(str(pair[1].get("source") or ""), 4),
            pair[0],
        )
    )
    return [item for _, item in indexed]


def build_review_evidence_catalog(
    pr_content: Any,
    ci_snapshot: Optional[Dict[str, Any]] = None,
    evidence_ledger: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Catalog objective provenance without inferring engineering meaning."""

    raw = pr_content if isinstance(pr_content, dict) else {}
    catalog: List[Dict[str, Any]] = []
    metadata = (
        raw.get("pr_metadata")
        if isinstance(raw.get("pr_metadata"), dict)
        else {}
    )
    if _text(metadata.get("description") or metadata.get("body")):
        catalog.append(
            {
                "id": "pr_body",
                "source_type": "pr_body",
                "coverage_type": "non_repository",
            }
        )
    for change in (
        raw.get("file_changes")
        if isinstance(raw.get("file_changes"), list)
        else []
    ):
        if not isinstance(change, dict) or not _text(change.get("file_path")):
            continue
        path = str(change["file_path"])
        entry = {
            "id": f"path:{path}",
            "source_type": "diff",
            "outcome": "hit",
            "paths": [path],
            "coverage_type": "changed_region",
        }
        catalog.append(entry)
    for index, item in enumerate(
        raw.get("interactions")
        if isinstance(raw.get("interactions"), list)
        else []
    ):
        if not isinstance(item, dict):
            continue
        item_id = item.get("id") or item.get("comment_id") or index
        catalog.append(
            {
                "id": f"author_comment:{item_id}",
                "source_type": "author_report",
                "coverage_type": "non_repository",
            }
        )
    snapshot = ci_snapshot or build_ci_snapshot(raw.get("ci_cd_results"))
    for item in snapshot.get("checks") or []:
        if isinstance(item, dict) and _text(item.get("identity")):
            entry = {
                "id": f"ci:{item['identity']}",
                "source_type": "ci",
                "name": str(item.get("name") or "unknown"),
                "outcome": str(item.get("classification") or "unknown"),
                "coverage_type": "non_repository",
            }
            provenance = str(
                item.get("actionable_details_provenance") or ""
            )
            if provenance:
                entry.update(
                    {
                        "actionable_details_provenance": provenance,
                        "actionable_output_present": isinstance(
                            item.get("output"), dict
                        ),
                        "actionable_annotation_count": len(
                            item.get("annotations") or []
                        ),
                    }
                )
            catalog.append(entry)
    ledger = evidence_ledger if isinstance(evidence_ledger, dict) else {}
    for event in ledger.get("evidence_events") or []:
        if not isinstance(event, dict) or not _text(event.get("id")):
            continue
        tool = str(event.get("tool") or "unknown")
        coverage_type = str(event.get("coverage_type") or "")
        if not coverage_type:
            coverage_type = {
                "read_file": "file_slice",
                "search_code": "search_snippet",
                "list_dir": "directory_inventory",
            }.get(tool, "non_repository")
        search_lineage = [
            {
                "path": str(item.get("path") or ""),
                "outcome": str(item.get("outcome") or ""),
                "head_sha": str(item.get("head_sha") or ""),
            }
            for item in event.get("search_hit_lineage") or []
            if isinstance(item, dict) and item.get("path")
        ][:20]
        entry = {
            "id": str(event["id"]),
            "question_id": str(event.get("question_id") or ""),
            "source_type": (
                "route_preflight"
                if tool == "route_exact_path_state"
                else "pfr"
            ),
            "tool": tool,
            "outcome": str(event.get("outcome") or "unknown"),
            "paths": [
                str(path) for path in event.get("paths") or [] if path
            ],
            "source_ref": str(event.get("source_ref") or ""),
            "head_reread_outcome": str(
                event.get("head_reread_outcome") or ""
            ),
            "coverage_type": coverage_type,
            "exact_path_state": str(event.get("exact_path_state") or ""),
            "observed_state": str(event.get("observed_state") or ""),
        }
        if search_lineage:
            entry["search_hit_lineage"] = search_lineage
        catalog.append(entry)
    return catalog


@dataclass(frozen=True, slots=True)
class ReviewContractViolation:
    """One typed deterministic contract violation."""

    code: str
    location: str
    message: str


# Compatibility names remain stable while v2 callers are retired.


def _violation_from_message(message: str) -> ReviewContractViolation:
    text = str(message or "")
    lowered = text.lower()
    patterns = (
        (
            r"missing required root field: ([A-Za-z_][A-Za-z0-9_]*)",
            r"$.\1",
        ),
        (
            r"missing required decision field: ([A-Za-z_][A-Za-z0-9_]*)",
            r"$.decision.\1",
        ),
        (r"(decision\.(?:risk_domains|reasons)\[\d+\])", r"$.\1"),
        (r"(owner_action\[\d+\])", r"$.\1"),
        (
            r"((?:findings|evidence_scope)\[\d+\]\.evidence_refs\[\d+\])",
            r"$.\1",
        ),
        (
            r"(findings\[\d+\]) missing required field: "
            r"([A-Za-z_][A-Za-z0-9_]*)",
            r"$.\1.\2",
        ),
        (
            r"^((?:schema_version|decision|owner_action|findings|"
            r"material_unknowns|evidence_scope|diagram|rendering_plan)"
            r"(?:\[\d+\])?(?:\.[A-Za-z_][A-Za-z0-9_]*)?)",
            r"$.\1",
        ),
    )
    location = "$"
    for pattern, replacement in patterns:
        match = re.search(pattern, text)
        if match:
            location = match.expand(replacement)[:180]
            break
    if text.startswith("duplicate finding id"):
        location = "$.findings[*].id"

    if lowered.startswith("diagram") or "root field: diagram" in lowered:
        code = "diagram_contract_invalid"
        location = "$.diagram"
    elif "claim_scope" in lowered and (
        "requires same-path full_file" in lowered
        or "cannot be verified or blocking" in lowered
    ):
        code = "claim_scope_coverage_mismatch"
    elif "missing required" in lowered or " is required" in lowered:
        code = "required_field_missing"
    elif "code_snippet" in lowered or "changed region" in lowered:
        code = "snippet_contract_invalid"
    elif "blocking requires evidence_status=verified" in lowered:
        code = "blocking_evidence_mismatch"
    elif "decision.verdict" in lowered and (
        "conflicts with" in lowered or "requires" in lowered
    ):
        code = "decision_relation_mismatch"
    elif "evidence_scope" in lowered and (
        "do not match source" in lowered
        or "matching supporting provenance" in lowered
    ):
        code = "evidence_scope_provenance_mismatch"
    elif "evidence" in lowered and (
        "reference" in lowered
        or "provenance" in lowered
        or "supporting" in lowered
    ):
        code = "evidence_ref_invalid"
    elif "decision.confidence must be high, medium, or low" in lowered:
        code = "enum_invalid"
    elif any(
        token in lowered
        for token in (
            "must be an object",
            "must be an array",
            "must be a boolean",
            "must be a string",
            "must be integer",
            "must be a non-empty string",
            "must be a string or null",
        )
    ):
        code = "field_type_invalid"
    elif any(
        token in lowered
        for token in (
            " conflicts with ",
            " requires ",
            "owner_action",
            "supported blocking finding",
            "must be false for",
            "blocking requires",
        )
    ):
        code = "cross_field_invariant"
    elif "invalid enum" in lowered or "must be blocked_findings" in lowered:
        code = "enum_invalid"
    else:
        code = "schema_contract_invalid"
    return ReviewContractViolation(
        code=code,
        location=location,
        message=text,
    )


class ReviewContractError(ValueError):
    """A model response is parseable but violates a fixed projection contract."""

    def __init__(
        self,
        errors: Sequence[str | ReviewContractViolation],
        warnings: Sequence[str] = (),
    ):
        self.violations = [
            item
            if isinstance(item, ReviewContractViolation)
            else _violation_from_message(str(item))
            for item in errors
        ]
        self.errors = [item.message for item in self.violations]
        self.warnings = list(warnings)
        super().__init__("; ".join(self.errors))




def catalog_entries(
    context_meta: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Return exact catalog entries with ledger facts filled monotonically."""

    catalog = (context_meta or {}).get("evidence_catalog") or []
    if isinstance(catalog, dict):
        result = {
            str(key): (
                value
                if isinstance(value, dict)
                else {"id": str(key), "source_type": str(value)}
            )
            for key, value in catalog.items()
        }
    else:
        result = {
            str(item.get("id") or item.get("ref")): item
            for item in catalog
            if isinstance(item, dict) and (item.get("id") or item.get("ref"))
        }
    ledger = (context_meta or {}).get("evidence_ledger") or {}
    if isinstance(ledger, dict):
        for event in ledger.get("evidence_events") or []:
            if not isinstance(event, dict) or not (
                event.get("id") or event.get("ref")
            ):
                continue
            identity = str(event.get("id") or event.get("ref"))
            existing = result.get(identity)
            if existing is None:
                result[identity] = event
                continue
            for key in (
                "question_id",
                "tool",
                "outcome",
                "paths",
                "source_ref",
                "head_reread_outcome",
                "coverage_type",
                "exact_path_state",
                "observed_state",
                "search_hit_lineage",
                "backend_attempted",
                "derived_from_event_id",
            ):
                if key not in existing or existing.get(key) in (
                    "",
                    None,
                    [],
                ):
                    if key in event:
                        existing[key] = event[key]
    return result


def expected_head_sha(context_meta: Optional[Dict[str, Any]]) -> str:
    meta = context_meta or {}
    for key in ("head_sha", "queued_head_sha", "current_head_sha"):
        value = _text(meta.get(key))
        if value:
            return value.casefold()
    return ""


def entry_head_sha(entry: Dict[str, Any]) -> str:
    match = HEAD_SOURCE_RE.match(_text(entry.get("source_ref")))
    return match.group(1).casefold() if match else ""


def normalize_repo_path(value: Any) -> str:
    path = _text(value).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


def entry_paths(entry: Dict[str, Any]) -> List[str]:
    return list(
        dict.fromkeys(
            path
            for path in (
                normalize_repo_path(item)
                for item in entry.get("paths") or []
            )
            if path
        )
    )


def entry_supports_claim(entry: Dict[str, Any]) -> bool:
    outcome = _text(entry.get("outcome"))
    return not outcome or outcome in SUPPORTING_OUTCOMES


def entry_supports_expected_head(
    entry: Dict[str, Any],
    *,
    expected_head: str,
) -> bool:
    source_type = _text(entry.get("source_type"))
    if source_type not in {"pfr", "route_preflight"}:
        return True
    return event_supports_answer(
        entry,
        expected_head_sha=expected_head,
    )


def search_entry_supports_exact_head(
    entry: Dict[str, Any],
    *,
    expected_head: str,
) -> bool:
    if _text(entry.get("source_ref")) != "default_branch_search":
        return True
    if _text(entry.get("head_reread_outcome")) != "relocated_at_head":
        return False
    lineage = entry.get("search_hit_lineage")
    if isinstance(lineage, list) and lineage:
        for item in lineage:
            if not isinstance(item, dict):
                return False
            if _text(item.get("outcome")) != "relocated_at_head":
                return False
            lineage_head = _text(item.get("head_sha")).casefold()
            if expected_head and lineage_head != expected_head:
                return False
        return True
    return not expected_head


def entry_is_objectively_renderable(entry: Dict[str, Any]) -> bool:
    return bool(
        entry_supports_claim(entry)
        and entry_paths(entry)
        and _text(entry.get("coverage_type"))
        in OBJECTIVE_COVERAGE_TYPES
    )


@dataclass(frozen=True, slots=True)
class EvidenceAdmission:
    """Mechanical admission result for one catalog reference."""

    ref: str
    known: bool
    supporting_outcome: bool
    exact_head_lineage: bool
    exact_search_lineage: bool
    entry_head_matches: bool

    @property
    def admissible_for_finding(self) -> bool:
        return bool(
            self.known
            and self.supporting_outcome
            and self.exact_head_lineage
            and self.exact_search_lineage
            and self.entry_head_matches
        )


def catalog_ref_admission(
    ref: Any,
    context_meta: Optional[Dict[str, Any]],
) -> EvidenceAdmission:
    """Check identity, outcome, and exact-head lineage without judging prose."""

    identity = str(ref or "").strip()
    catalog = catalog_entries(context_meta)
    entry = catalog.get(identity)
    if not isinstance(entry, dict):
        return EvidenceAdmission(
            ref=identity,
            known=False,
            supporting_outcome=False,
            exact_head_lineage=False,
            exact_search_lineage=False,
            entry_head_matches=False,
        )
    expected_head = expected_head_sha(context_meta)
    entry_head = entry_head_sha(entry)
    return EvidenceAdmission(
        ref=identity,
        known=True,
        supporting_outcome=entry_supports_claim(entry),
        exact_head_lineage=entry_supports_expected_head(
            entry,
            expected_head=expected_head,
        ),
        exact_search_lineage=search_entry_supports_exact_head(
            entry,
            expected_head=expected_head,
        ),
        entry_head_matches=not (
            expected_head and entry_head and entry_head != expected_head
        ),
    )


def _normalized_code_block(value: str) -> Tuple[List[str], List[int]]:
    raw_lines = (value or "").splitlines()
    first = 0
    while first < len(raw_lines) and not raw_lines[first].strip():
        first += 1
    last = len(raw_lines)
    while last > first and not raw_lines[last - 1].strip():
        last -= 1
    retained_indexes = list(range(first, last))
    if not retained_indexes:
        return [], []
    dedented = textwrap.dedent(
        "\n".join(raw_lines[first:last])
    ).splitlines()
    return [line.rstrip() for line in dedented], retained_indexes


def _normalized_code_lines(value: str) -> List[str]:
    return _normalized_code_block(value)[0]


def _code_window_matches(
    source_lines: Sequence[str],
    normalized_target: Sequence[str],
) -> bool:
    return _normalized_code_lines("\n".join(source_lines)) == list(
        normalized_target
    )


def has_raw_diff_syntax(value: str) -> bool:
    return any(
        line.startswith(("diff --git ", "@@", "+++ ", "--- "))
        for line in (value or "").splitlines()
        if line.strip()
    )


def changed_postimages(
    pr_details: str,
) -> Dict[str, List[Tuple[List[str], set[int]]]]:
    """Extract exact post-change hunk windows from PR-details Markdown."""

    result: Dict[str, List[Tuple[List[str], set[int]]]] = {}
    in_file_changes = False
    current_path = ""
    in_diff = False
    diff_lines: List[str] = []

    def finish_diff() -> None:
        nonlocal diff_lines
        if not current_path or not diff_lines:
            diff_lines = []
            return
        hunk_lines: List[str] = []
        added_indexes: set[int] = set()

        def finish_hunk() -> None:
            nonlocal hunk_lines, added_indexes
            normalized, retained_indexes = _normalized_code_block(
                "\n".join(hunk_lines)
            )
            if normalized:
                compact_added = {
                    normalized_index
                    for normalized_index, raw_index in enumerate(
                        retained_indexes
                    )
                    if raw_index in added_indexes
                }
                result.setdefault(current_path, []).append(
                    (normalized, compact_added)
                )
            hunk_lines = []
            added_indexes = set()

        for raw in diff_lines:
            if raw.startswith("@@"):
                finish_hunk()
                continue
            if raw.startswith(("+++", "---", "\\ No newline")):
                continue
            if raw.startswith("+"):
                added_indexes.add(len(hunk_lines))
                hunk_lines.append(raw[1:])
            elif raw.startswith("-"):
                continue
            elif raw.startswith(" "):
                hunk_lines.append(raw[1:])
            else:
                hunk_lines.append(raw)
        finish_hunk()
        diff_lines = []

    for line in (pr_details or "").splitlines():
        if line == "## File Changes":
            in_file_changes = True
            continue
        if in_file_changes and not in_diff and line.startswith("## "):
            break
        if not in_file_changes:
            continue
        if not in_diff and line.startswith("### "):
            current_path = line[4:].strip()
            continue
        if not in_diff and line == "```diff":
            in_diff = True
            diff_lines = []
            continue
        if in_diff and line == "```":
            in_diff = False
            finish_diff()
            continue
        if in_diff:
            diff_lines.append(line)
    if in_diff:
        finish_diff()
    return result


def changed_preimages(
    pr_details: str,
) -> Dict[str, List[Tuple[List[str], set[int]]]]:
    """Extract exact pre-change hunk windows and deleted-line indexes."""

    result: Dict[str, List[Tuple[List[str], set[int]]]] = {}
    in_file_changes = False
    current_path = ""
    in_diff = False
    diff_lines: List[str] = []

    def finish_diff() -> None:
        nonlocal diff_lines
        if not current_path or not diff_lines:
            diff_lines = []
            return
        hunk_lines: List[str] = []
        deleted_indexes: set[int] = set()

        def finish_hunk() -> None:
            nonlocal hunk_lines, deleted_indexes
            normalized, retained_indexes = _normalized_code_block(
                "\n".join(hunk_lines)
            )
            if normalized:
                compact_deleted = {
                    normalized_index
                    for normalized_index, raw_index in enumerate(
                        retained_indexes
                    )
                    if raw_index in deleted_indexes
                }
                result.setdefault(current_path, []).append(
                    (normalized, compact_deleted)
                )
            hunk_lines = []
            deleted_indexes = set()

        for raw in diff_lines:
            if raw.startswith("@@"):
                finish_hunk()
                continue
            if raw.startswith(("+++", "---", "\\ No newline")):
                continue
            if raw.startswith("-"):
                deleted_indexes.add(len(hunk_lines))
                hunk_lines.append(raw[1:])
            elif raw.startswith("+"):
                continue
            elif raw.startswith(" "):
                hunk_lines.append(raw[1:])
            else:
                hunk_lines.append(raw)
        finish_hunk()
        diff_lines = []

    for line in (pr_details or "").splitlines():
        if line == "## File Changes":
            in_file_changes = True
            continue
        if in_file_changes and not in_diff and line.startswith("## "):
            break
        if not in_file_changes:
            continue
        if not in_diff and line.startswith("### "):
            current_path = line[4:].strip()
            continue
        if not in_diff and line == "```diff":
            in_diff = True
            diff_lines = []
            continue
        if in_diff and line == "```":
            in_diff = False
            finish_diff()
            continue
        if in_diff:
            diff_lines.append(line)
    if in_diff:
        finish_diff()
    return result


def snippet_matches_changed_region(
    file_path: str,
    snippet: str,
    pr_details: str,
) -> bool:
    if not file_path or not snippet or has_raw_diff_syntax(snippet):
        return False
    target = _normalized_code_lines(snippet)
    if not target:
        return False
    for lines, added_indexes in changed_postimages(pr_details).get(
        file_path, []
    ):
        if len(target) > len(lines):
            continue
        for start in range(len(lines) - len(target) + 1):
            if not _code_window_matches(
                lines[start : start + len(target)],
                target,
            ):
                continue
            if any(
                (start + offset) in added_indexes
                for offset in range(len(target))
            ):
                return True
    return False


def snippet_matches_deleted_region(
    file_path: str,
    snippet: str,
    pr_details: str,
) -> bool:
    if not file_path or not snippet or has_raw_diff_syntax(snippet):
        return False
    target = _normalized_code_lines(snippet)
    if not target:
        return False
    for lines, deleted_indexes in changed_preimages(pr_details).get(
        file_path, []
    ):
        if len(target) > len(lines):
            continue
        for start in range(len(lines) - len(target) + 1):
            if not _code_window_matches(
                lines[start : start + len(target)],
                target,
            ):
                continue
            if any(
                (start + offset) in deleted_indexes
                for offset in range(len(target))
            ):
                return True
    return False


def classify_changed_region_anchor(
    file_path: str,
    snippet: str,
    pr_details: str,
) -> str:
    """Classify a verbatim anchor as post-change, deleted, or invalid."""

    if not file_path or not snippet or has_raw_diff_syntax(snippet):
        return "invalid"
    if not _normalized_code_lines(snippet):
        return "invalid"
    if not pr_details:
        return "post_change"
    if snippet_matches_changed_region(file_path, snippet, pr_details):
        return "post_change"
    if snippet_matches_deleted_region(file_path, snippet, pr_details):
        return "deleted_region"
    return "invalid"


def uniquely_resolve_changed_region_anchor(
    file_path: str,
    snippet: str,
    pr_details: str,
) -> Optional[str]:
    """Return one exact post-change representation when it is unambiguous.

    Final occasionally preserves the right finding while damaging only its
    optional inline anchor: it can crop a line, change leading indentation, or
    join two exact fragments with an omission marker.  The changed-region
    catalog is authoritative for representation, so Projection may recover a
    source window only when the supplied fragment identifies exactly one
    changed post-image window.  Ambiguity returns ``None``; this function never
    chooses evidence, a finding, severity, or merge posture.
    """

    path = normalize_repo_path(file_path)
    if not path or not isinstance(snippet, str) or not snippet.strip():
        return None
    if classify_changed_region_anchor(path, snippet, pr_details) == "post_change":
        return snippet

    raw_segments: List[str] = []
    current: List[str] = []
    for line in snippet.splitlines():
        if line.strip() in {"...", "…"}:
            if current:
                raw_segments.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        raw_segments.append("\n".join(current))
    if not raw_segments:
        raw_segments = [snippet]

    def line_matches(observed: str, supplied: str) -> bool:
        left = observed.strip()
        right = supplied.strip()
        return bool(
            left
            and right
            and (
                left == right
                or left in right
                or right in left
            )
        )

    candidates: set[str] = set()
    for segment in raw_segments:
        target = _normalized_code_lines(segment)
        if not target:
            continue
        for lines, added_indexes in changed_postimages(pr_details).get(path, []):
            if len(target) > len(lines):
                continue
            for start in range(len(lines) - len(target) + 1):
                window = lines[start : start + len(target)]
                if not all(
                    line_matches(observed, supplied)
                    for observed, supplied in zip(window, target)
                ):
                    continue
                if not any(
                    start + offset in added_indexes
                    for offset in range(len(window))
                ):
                    continue
                candidate = "\n".join(window)
                if (
                    classify_changed_region_anchor(path, candidate, pr_details)
                    == "post_change"
                ):
                    candidates.add(candidate)
    return next(iter(candidates)) if len(candidates) == 1 else None


_DELETED_REGION_SUPPORT_COVERAGE = {
    "full_file",
    "file_slice",
    "search_snippet",
}


def _entry_is_current_positive_content_observation(
    entry: Dict[str, Any],
    *,
    expected_head: str,
) -> bool:
    if str(entry.get("source_type") or "") != "pfr":
        return False
    if str(entry.get("outcome") or "") not in {"hit", "success"}:
        return False
    if (
        str(entry.get("coverage_type") or "")
        not in _DELETED_REGION_SUPPORT_COVERAGE
    ):
        return False
    if str(entry.get("observed_state") or "") != "content_observed":
        return False
    if not entry_paths(entry):
        return False
    expected = str(expected_head or "").strip().casefold()
    if not expected:
        return False
    source_ref = str(entry.get("source_ref") or "").strip()
    if source_ref == "default_branch_search":
        if (
            str(entry.get("head_reread_outcome") or "")
            != "relocated_at_head"
        ):
            return False
        lineage = entry.get("search_hit_lineage")
        if not isinstance(lineage, list) or not lineage:
            return False
        return all(
            isinstance(item, dict)
            and str(item.get("outcome") or "") == "relocated_at_head"
            and str(item.get("head_sha") or "").strip().casefold()
            == expected
            for item in lineage
        )
    match = HEAD_SOURCE_RE.fullmatch(source_ref)
    return bool(match and match.group(1).casefold() == expected)


def deleted_region_supporting_refs(
    refs: Sequence[str],
    context_meta: Optional[Dict[str, Any]],
) -> List[str]:
    expected_head = expected_head_sha(context_meta)
    catalog = catalog_entries(context_meta)
    return [
        str(ref)
        for ref in refs
        if str(ref) in catalog
        and _entry_is_current_positive_content_observation(
            catalog[str(ref)],
            expected_head=expected_head,
        )
    ]


_CI_DIAGNOSTIC_TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)
_CI_DIAGNOSTIC_SPLIT_RE = re.compile(
    r"[\r\n]+|(?<=[.!?;。！？；])\s+"
)
_CI_LONG_UNIQUE_TOKEN_CHARS = 12
_CI_MAX_DIAGNOSTIC_ATOMS = 64
_CI_MAX_DIAGNOSTIC_TOKENS = 64


def _ci_diagnostic_tokens(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(
        token
        for token in _CI_DIAGNOSTIC_TOKEN_RE.findall(value.casefold())
        if token
    )


def _contains_token_sequence(
    haystack: Sequence[str],
    needle: Sequence[str],
) -> bool:
    width = len(needle)
    return bool(
        width
        and any(
            tuple(haystack[index : index + width]) == tuple(needle)
            for index in range(len(haystack) - width + 1)
        )
    )


def _sequence_spans(
    haystack: Sequence[str],
    needle: Sequence[str],
) -> List[Tuple[int, int]]:
    width = len(needle)
    if not width:
        return []
    return [
        (index, index + width - 1)
        for index in range(len(haystack) - width + 1)
        if tuple(haystack[index : index + width]) == tuple(needle)
    ]


def _specific_ci_phrase(tokens: Sequence[str]) -> bool:
    """Reject generic CI fragments while admitting identifying diagnostics."""

    meaningful = tuple(token for token in tokens if len(token) >= 2)
    return (
        any(
            len(token) >= _CI_LONG_UNIQUE_TOKEN_CHARS
            for token in meaningful
        )
        or len(meaningful) >= 3
    )


def _specific_ci_name(tokens: Sequence[str]) -> bool:
    """Require more identity than a generic name such as ``Test`` or ``CI``."""

    meaningful = tuple(token for token in tokens if len(token) >= 2)
    return (
        any(
            len(token) >= _CI_LONG_UNIQUE_TOKEN_CHARS
            for token in meaningful
        )
        or len(meaningful) >= 2
    )


def _ci_actionable_messages(
    check: Mapping[str, Any],
) -> List[Tuple[str, ...]]:
    values: List[Any] = []
    output = check.get("output")
    if isinstance(output, dict):
        values.extend(
            output.get(key)
            for key in ("title", "summary", "text", "log_tail")
        )
    annotations = check.get("annotations")
    if isinstance(annotations, list):
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            values.extend(
                annotation.get(key)
                for key in ("title", "message", "raw_details")
            )
    phrases: List[Tuple[str, ...]] = []
    for value in values:
        if not isinstance(value, str):
            continue
        # Match only whole bounded diagnostic atoms. Arbitrary n-grams turn
        # common review wording into a false CI dependency.
        for atom in _CI_DIAGNOSTIC_SPLIT_RE.split(value):
            tokens = _ci_diagnostic_tokens(atom)[:_CI_MAX_DIAGNOSTIC_TOKENS]
            if tokens and _specific_ci_phrase(tokens):
                phrases.append(tokens)
            if len(phrases) >= _CI_MAX_DIAGNOSTIC_ATOMS:
                return list(dict.fromkeys(phrases))
    return list(dict.fromkeys(phrases))


def generation_ci_check(
    ref: str,
    context_meta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return the exact check object Final was allowed to cite."""

    meta = context_meta or {}
    payload = meta.get("ci_generation_model_payload")
    if not isinstance(payload, dict):
        payload = meta.get("ci_generation_snapshot")
    if not isinstance(payload, dict):
        payload = meta.get("ci_snapshot")
    identity = str(ref or "")[3:]
    for check in (payload or {}).get("checks") or []:
        if (
            isinstance(check, dict)
            and str(check.get("identity") or "").strip() == identity
        ):
            return check
    entry = catalog_entries(context_meta).get(ref) or {}
    return {
        "identity": identity,
        "name": entry.get("name"),
        "status": entry.get("status"),
        "classification": entry.get("classification"),
        "conclusion": entry.get("conclusion") or entry.get("outcome"),
    }


def ci_payload_taints_text(
    value: Any,
    check: Mapping[str, Any],
) -> bool:
    """Return whether public prose repeats an identifying CI observation."""

    text = _ci_diagnostic_tokens(value)
    if not text:
        return False
    for message in _ci_actionable_messages(check):
        if _contains_token_sequence(text, message):
            return True

    names = [
        tokens
        for field in ("identity", "name")
        if (tokens := _ci_diagnostic_tokens(check.get(field)))
        and _specific_ci_name(tokens)
    ]
    states = [
        tokens
        for field in ("status", "classification", "conclusion")
        if (tokens := _ci_diagnostic_tokens(check.get(field)))
    ]
    for name in names:
        for status in states:
            for name_start, name_end in _sequence_spans(text, name):
                for status_start, status_end in _sequence_spans(text, status):
                    if (
                        0 <= status_start - name_end - 1 <= 1
                        or 0 <= name_start - status_end - 1 <= 1
                    ):
                        return True
    return False


def ci_ref_matches_actionable_diagnostic(
    ref: str,
    values: Sequence[Any],
    context_meta: Optional[Dict[str, Any]],
) -> bool:
    """Prove a cited failure by its exact-head actionable diagnostic.

    This deliberately excludes a check name plus red status. It is the narrow
    non-source capability used for a PR-metadata or policy blocker whose exact
    diagnostic is already present in the model-authored finding.
    """

    lineage = (context_meta or {}).get("ci_actionable_detail_lineage")
    if not (
        isinstance(lineage, dict)
        and lineage.get("source") == "exact_head_refresh"
        and lineage.get("policy") == "fresh_output_and_annotations"
        and lineage.get("outcome") == "freshly_observed"
    ):
        return False
    check = generation_ci_check(ref, context_meta)
    if str(check.get("classification") or "").strip() not in {
        "failure",
        "action_required",
    }:
        return False
    text_tokens = _ci_diagnostic_tokens(
        "\n".join(str(value or "") for value in values)
    )
    text_token_set = set(text_tokens)
    for message in _ci_actionable_messages(check):
        shared = text_token_set & set(message)
        if len(shared) >= 3 or any(
            len(token) >= _CI_LONG_UNIQUE_TOKEN_CHARS for token in shared
        ):
            return True
    return False




def ci_snapshot(
    _pr_details: str,
    context_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    snapshot = (context_meta or {}).get("ci_snapshot")
    if isinstance(snapshot, dict) and snapshot.get("schema_version") == 1:
        return snapshot
    return {}


def ci_annotation_proves_finding_path(
    finding: Dict[str, Any],
    pr_details: str,
    context_meta: Optional[Dict[str, Any]],
) -> bool:
    """Require an exact cited CI identity and exact normalized path."""

    target_path = normalize_repo_path(finding.get("file_path"))
    if not target_path:
        return False
    cited_refs = {
        str(ref)
        for key in (
            "evidence_refs",
            "required_evidence_refs",
            "supporting_evidence_refs",
        )
        for ref in finding.get(key) or []
        if isinstance(ref, str) and ref.startswith("ci:")
    }
    if not cited_refs:
        return False
    for check in ci_snapshot(pr_details, context_meta).get("checks") or []:
        if not isinstance(check, dict):
            continue
        identity = str(check.get("identity") or "").strip()
        if not identity or f"ci:{identity}" not in cited_refs:
            continue
        if any(
            isinstance(annotation, dict)
            and normalize_repo_path(annotation.get("path")) == target_path
            for annotation in check.get("annotations") or []
        ):
            return True
    return False


def visible_ci_check_label(value: Any, max_chars: int = 120) -> str:
    """Render an untrusted GitHub check name as bounded inert Markdown."""

    label = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "unknown"))
    label = re.sub(r"\s+", " ", label).strip() or "unknown"
    label = label.replace("`", "'").replace("@", "@\u200b")
    if len(label) > max_chars:
        label = label[: max(1, max_chars - 3)].rstrip() + "..."
    return f"`{label}`"


def visible_ci_check_labels(items: Any, limit: int = 3) -> str:
    """Group equal display names without collapsing distinct CI identities."""

    grouped: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        raw = re.sub(
            r"\s+",
            " ",
            str(item.get("name") or "unknown"),
        ).strip()
        key = raw.casefold()
        if key not in grouped:
            grouped[key] = {"name": raw or "unknown", "count": 0}
            order.append(key)
        grouped[key]["count"] += 1
    labels: List[str] = []
    for key in order:
        entry = grouped[key]
        label = visible_ci_check_label(entry["name"])
        if entry["count"] > 1:
            label += f" ×{entry['count']}"
        labels.append(label)
    if not labels:
        return "CI checks"
    shown = ", ".join(labels[:limit])
    if len(labels) > limit:
        shown += f", +{len(labels) - limit} more"
    return shown

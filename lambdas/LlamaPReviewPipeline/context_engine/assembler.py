"""Final bounded PFR context assembly."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .. import config
from .packing import ContextSection, pack_sections
from .state import CollectionState


EVIDENCE_EVENT_INDEX_START = "<CODE_GENERATED_EVIDENCE_EVENT_INDEX>"
EVIDENCE_EVENT_INDEX_END = "</CODE_GENERATED_EVIDENCE_EVENT_INDEX>"


def evidence_event_index_envelope(
    state: CollectionState,
) -> str:
    """Render the complete code-generated Reconcile identity control plane."""

    return (
        EVIDENCE_EVENT_INDEX_START
        + "\n"
        + state.evidence_ledger.compact_event_index_text()
        + "\n"
        + EVIDENCE_EVENT_INDEX_END
    )


def assemble_reconcile_context(
    state: CollectionState,
    *,
    max_chars: int,
) -> Tuple[str, str, Dict[str, object]]:
    """Pack diagnostic context after atomically reserving the event index.

    The returned index envelope is never truncated.  A false ``complete`` flag
    instructs orchestration not to issue a Reconcile request.
    """

    ceiling = max(0, int(max_chars))
    index_envelope = evidence_event_index_envelope(state)
    separator_chars = 2
    if len(index_envelope) > ceiling:
        return (
            "",
            index_envelope,
            {
                "event_count": len(state.evidence_ledger.events),
                "complete": False,
            },
        )
    context_budget = max(0, ceiling - len(index_envelope) - separator_chars)
    context = _assemble_context(
        state,
        max_chars=context_budget,
        include_control_ids=True,
        exact_head_content_only=True,
    )
    return (
        context,
        index_envelope,
        {
            "event_count": len(state.evidence_ledger.events),
            "complete": True,
        },
    )


def _format_tool_args(args: Dict[str, Any]) -> str:
    pieces = []
    for key in ("query", "intent", "path", "target_path", "reason"):
        value = args.get(key)
        if value:
            pieces.append(f"{key}={value}")
    if args.get("symbols"):
        pieces.append("symbols=" + ", ".join(str(item) for item in args["symbols"]))
    if args.get("max_depth"):
        pieces.append(f"max_depth={args['max_depth']}")
    return "; ".join(pieces)


def _tool_trace_lines(
    state: CollectionState,
    *,
    include_control_ids: bool = True,
    exact_head_content_only: bool = False,
) -> List[str]:
    lines: List[str] = ["## Tool Trace"]
    events = state.tool_events[: config.MAX_TOOL_TRACE_EVENTS]
    if not events:
        lines.append("- No tool calls were recorded.")
        return lines
    total = 0
    for index, event in enumerate(events, start=1):
        paths = ", ".join(f"`{path}`" for path in event.get("paths", [])[:8])
        invalid = ", ".join(f"`{path}`" for path in event.get("invalid_paths", [])[:5])
        args = _format_tool_args(event.get("args") or {})
        summary = event.get("result_summary") or ""
        parts = [
            f"- {index}. `{event.get('tool', 'unknown')}`",
            f"outcome={event.get('outcome', 'unknown')}",
            f"hits={event.get('hit_count', 0)}",
        ]
        if args:
            parts.append(args)
        if paths:
            parts.append(f"paths: {paths}")
        if invalid:
            parts.append(f"invalid: {invalid}")
        if event.get("error"):
            parts.append(f"error: {event['error']}")
        if event.get("error_kind"):
            parts.append(f"error_kind={event['error_kind']}")
        if include_control_ids and event.get("question_id"):
            parts.append(f"question={event['question_id']}")
        if include_control_ids and event.get("evidence_event_id"):
            parts.append(f"evidence={event['evidence_event_id']}")
        if event.get("head_reread_outcome"):
            parts.append(f"head_reread={event['head_reread_outcome']}")
        coverage_type = str((event.get("metadata") or {}).get("coverage_type") or "")
        if coverage_type:
            parts.append(f"coverage={coverage_type}")
        lineage = (event.get("metadata") or {}).get("search_hit_lineage")
        if isinstance(lineage, list) and lineage:
            relocated = sum(
                1
                for item in lineage
                if isinstance(item, dict)
                and item.get("outcome") == "relocated_at_head"
            )
            parts.append(
                f"search_lineage=head_relocated:{relocated},default_only:{len(lineage) - relocated}"
            )
        default_branch_body_withheld = False
        if (
            exact_head_content_only
            and event.get("source_ref") == "default_branch_search"
        ):
            default_branch_body_withheld = any(
                isinstance(item, dict)
                and item.get("outcome") != "relocated_at_head"
                for item in (
                    (event.get("metadata") or {}).get(
                        "search_hit_lineage"
                    )
                    or []
                )
            )
        if default_branch_body_withheld:
            parts.append(
                "result: default-branch hit content withheld because every "
                "snippet was not relocated at the queued PR head"
            )
        elif summary:
            parts.append(f"result: {summary}")
        line = "; ".join(parts)
        if total + len(line) > config.MAX_TOOL_TRACE_CHARS:
            lines.append("- [tool trace truncated]")
            break
        lines.append(line)
        total += len(line)
    if len(state.tool_events) > len(events):
        lines.append(f"- [only first {len(events)} of {len(state.tool_events)} tool events shown]")
    return lines


def _assemble_context(
    state: CollectionState,
    *,
    reserved_chars: int = 0,
    max_chars: Optional[int] = None,
    include_control_ids: bool,
    exact_head_content_only: bool,
) -> str:
    seen: set[Tuple[str, int, int]] = set()
    snippets: List[Dict] = []
    for snippet in state.collected_snippets:
        if (
            exact_head_content_only
            and snippet.get("exact_head_admitted") is not True
        ):
            continue
        key = (snippet.get("path", ""), int(snippet.get("start") or 0), int(snippet.get("end") or 0))
        if key in seen:
            continue
        seen.add(key)
        kind_score = {"definition": 0, "usage": 1, "import": 2}.get(snippet.get("kind"), 3)
        snippets.append({**snippet, "kind_score": kind_score})
    snippets.sort(key=lambda item: (item["kind_score"], item.get("path", ""), item.get("start", 0)))

    head = state.head_sha[:8]
    search_hit_events = sum(
        1 for event in state.tool_events if event.get("tool") == "search_code" and event.get("outcome") == "hit"
    )
    header_parts = [
        "# PR Review Context",
        f"**Repository:** {state.repo_full_name}   **PR head:** {head}",
        f"**Files collected:** {len(state.collected_files)}   **Search calls:** {state.search_calls}   **Search hit events:** {search_hit_events}   **PFR rounds:** {state.current_iteration}",
    ]
    changed_parts = ["## Changed Files (PR head)"]
    for file_change in state.pr_content.get("file_changes", []):
        changed_parts.append(
            f"- `{file_change.get('file_path', 'unknown')}` ({file_change.get('change_type', '?')}, +{file_change.get('additions', 0)}/-{file_change.get('deletions', 0)})"
        )
    if len(changed_parts) == 1:
        changed_parts.append("- No changed files were supplied.")

    inventory = state.repo_inventory
    inventory_parts = ["## Repository Inventory"]
    if inventory is None:
        inventory_parts.append("- Status: unavailable (legacy/test state); do not infer repository-wide absence.")
    else:
        inventory_parts.extend(
            [
                f"- Status: {inventory.status}; recursive tree truncated: {inventory.tree_truncated}",
                f"- Discoverable safe files: {len(inventory.discoverable_files)}; sensitive paths excluded: {len(inventory.excluded_sensitive)}",
                "- Owner docs: " + (", ".join(inventory.owner_doc_paths) if inventory.owner_doc_paths else "none discovered"),
            ]
        )
    if state.removed_paths:
        inventory_parts.append("- Removed at PR head: " + ", ".join(f"`{path}`" for path in sorted(state.removed_paths)))

    related_parts = ["## Related Context"]
    for snippet in snippets:
        header = f"### [{snippet.get('kind', 'usage')}] `{snippet.get('path', 'unknown')}` (lines {snippet.get('start', '?')}-{snippet.get('end', '?')}) {snippet.get('source', '')}"
        block = f"{header}\n```text\n{snippet.get('code', '')}\n```"
        related_parts.append(block)
    if len(related_parts) == 1:
        related_parts.append("- No related snippets were collected.")

    ledger_text = (
        state.evidence_ledger.compact_text()
        if include_control_ids
        else state.evidence_ledger.compact_review_text()
    )
    ledger_parts = ["## Verification Ledger", ledger_text]
    trace_parts = _tool_trace_lines(
        state,
        include_control_ids=include_control_ids,
        exact_head_content_only=exact_head_content_only,
    )
    summary_parts = [
        "## Collection Summary",
        f"- Tool calls: search_code x{state.search_calls}, read_file x{state.read_calls}, list_dir x{state.list_calls}",
        f"- Tool outcomes: repeated x{state.repeated_tool_calls}, no-hit x{state.no_hit_tool_calls}, search-error x{state.search_error_tool_calls}, quota-exhausted x{state.quota_exhausted_tool_calls}",
    ]
    if state.finish_reason:
        summary_parts.append(f"- Finish reason: {state.finish_reason}")
    if state.finish_summary:
        summary_parts.append(f"- Summary: {state.finish_summary}")
    if state.known_gaps:
        summary_parts.append("- Known gaps: " + "; ".join(state.known_gaps))
    if state.non_existent_files:
        summary_parts.append("- Invalid paths requested: " + ", ".join(sorted(state.non_existent_files)))
    if state.fetch_degradation_reason_counts:
        reasons = ", ".join(
            f"{kind} x{count}"
            for kind, count in sorted(
                state.fetch_degradation_reason_counts.items()
            )
        )
        summary_parts.append(
            "- Retrieval degradation reasons: "
            + reasons
            + ". Only answer-eligible evidence may support a claim."
        )

    ceiling = int(state.max_context_chars)
    if max_chars is not None:
        ceiling = min(ceiling, max(0, int(max_chars)))
    budget = max(0, ceiling - max(0, int(reserved_chars)))
    return pack_sections(
        [
            ContextSection("header", "\n".join(header_parts), priority=0, required=True, min_chars=180),
            ContextSection("changed_files", "\n".join(changed_parts), priority=1, required=True, min_chars=180),
            ContextSection("inventory", "\n".join(inventory_parts), priority=0, required=True, min_chars=220),
            ContextSection("related", "\n\n".join(related_parts), priority=3),
            ContextSection("ledger", "\n".join(ledger_parts), priority=0, required=True, min_chars=260),
            ContextSection("trace", "\n".join(trace_parts), priority=8, required=True, min_chars=300),
            ContextSection("summary", "\n".join(summary_parts), priority=0, required=True, min_chars=260),
        ],
        budget,
    )


def assemble_context(
    state: CollectionState,
    *,
    reserved_chars: int = 0,
    max_chars: Optional[int] = None,
) -> str:
    """Assemble diagnostic context for the private retrieval control loop."""

    return _assemble_context(
        state,
        reserved_chars=reserved_chars,
        max_chars=max_chars,
        include_control_ids=True,
        exact_head_content_only=False,
    )


def assemble_review_context(
    state: CollectionState,
    *,
    reserved_chars: int = 0,
    max_chars: Optional[int] = None,
) -> str:
    """Assemble Deep-facing evidence without private q/e/res identities."""

    return _assemble_context(
        state,
        reserved_chars=reserved_chars,
        max_chars=max_chars,
        include_control_ids=False,
        exact_head_content_only=True,
    )


def context_meta(state: CollectionState) -> Dict[str, object]:
    tool_counts: Dict[str, int] = {}
    no_hit_counts: Dict[str, int] = {}
    outcome_counts: Dict[str, int] = {}
    search_hit_paths = set()
    list_dir_paths = set()
    planned_read_paths = list(state.planned_read_paths)
    search_no_hit_count = 0
    search_quota_exhausted_count = 0
    search_hit_event_count = 0
    backend_attempted_event_count = 0
    derived_evidence_event_count = 0
    for event in state.tool_events:
        name = str(event.get("tool") or "unknown")
        tool_counts[name] = tool_counts.get(name, 0) + 1
        if int(event.get("hit_count") or 0) == 0:
            no_hit_counts[name] = no_hit_counts.get(name, 0) + 1
        outcome = str(event.get("outcome") or "unknown")
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        if metadata.get("backend_attempted", True):
            backend_attempted_event_count += 1
        else:
            derived_evidence_event_count += 1
        if name == "search_code" and outcome == "hit":
            search_hit_event_count += 1
            search_hit_paths.update(str(path) for path in event.get("paths") or [] if path)
        if name == "search_code" and outcome == "no_hit":
            search_no_hit_count += 1
        if name == "search_code" and outcome == "quota_exhausted":
            search_quota_exhausted_count += 1
        if name == "list_dir" and outcome == "hit":
            target_path = str((event.get("args") or {}).get("target_path") or "").strip().strip("/")
            if target_path:
                list_dir_paths.add(target_path)
            for raw_path in event.get("paths") or []:
                raw_text = str(raw_path or "").strip()
                path = raw_text.strip("/")
                if not path:
                    continue
                parts = path.split("/")
                if len(parts) == 1:
                    list_dir_paths.add(path)
                    continue
                for index in range(1, len(parts)):
                    list_dir_paths.add("/".join(parts[:index]))
                if raw_text.endswith("/"):
                    list_dir_paths.add(path)
        if name == "read_file":
            path = (event.get("args") or {}).get("path")
            if path and str(path) not in planned_read_paths:
                planned_read_paths.append(str(path))
    changed_file_paths = {
        str(change.get("file_path"))
        for change in state.pr_content.get("file_changes") or []
        if isinstance(change, dict) and change.get("file_path")
    }
    planned_unread_paths = [
        path for path in planned_read_paths if path not in state.read_success_paths and path not in state.removed_paths
    ]
    evidence_hit_paths = changed_file_paths | state.read_success_paths | search_hit_paths
    return {
        "head_sha": state.head_sha,
        "files": len(state.collected_files),
        "tokens": state.total_tokens,
        "pfr_rounds": state.current_iteration,
        # `search_hits` is retained for artifact compatibility, but now means
        # actual hit events rather than attempted calls.
        "search_hits": search_hit_event_count,
        "search_calls": state.search_calls,
        "search_hit_events": search_hit_event_count,
        "read_calls": state.read_calls,
        "list_calls": state.list_calls,
        "known_gaps": state.known_gaps,
        "finished": state.finished,
        "finish_reason": state.finish_reason,
        "budget_exhausted": state.budget_exhausted_flag,
        "soft_budget_exhausted": state.soft_budget_exhausted,
        "soft_budget_seconds": state.soft_budget_seconds,
        "budget_health_reasons": list(state.budget_health_reasons),
        "snippet_parse_fallbacks": state.snippet_parse_fallbacks,
        "tool_event_count": len(state.tool_events),
        "backend_attempted_tool_event_count": backend_attempted_event_count,
        "derived_evidence_event_count": derived_evidence_event_count,
        "tool_counts": tool_counts,
        "tool_no_hit_counts": no_hit_counts,
        "tool_outcome_counts": outcome_counts,
        "tool_arg_repair_counts": dict(state.tool_arg_repair_counts),
        "pfr_fetch_degradation_reason_counts": dict(
            state.fetch_degradation_reason_counts
        ),
        "changed_file_paths": sorted(changed_file_paths),
        "search_hit_paths": sorted(search_hit_paths),
        "list_dir_paths": sorted(list_dir_paths),
        "evidence_hit_paths": sorted(evidence_hit_paths),
        "planned_read_paths": planned_read_paths,
        "planned_unread_paths": planned_unread_paths,
        "planned_invalid_read_paths": list(state.planned_invalid_read_paths),
        "removed_path_skips": sorted(state.removed_paths & set(planned_read_paths)),
        "budget_skipped_verification_paths": list(state.budget_skipped_verification_paths),
        "budget_skipped_verification_steps": list(state.budget_skipped_verification_steps),
        "read_success_paths": sorted(state.read_success_paths),
        "read_error_paths": sorted(state.read_error_paths),
        "read_outcomes": dict(sorted(state.read_outcomes.items())),
        "read_file_missing_path_errors": state.read_file_missing_path_errors,
        "attempted_file_count": len(state.attempted_files),
        "attempted_search_count": len(state.attempted_search_queries),
        "repeated_tool_calls": state.repeated_tool_calls,
        "no_hit_tool_calls": state.no_hit_tool_calls,
        "search_error_tool_calls": state.search_error_tool_calls,
        "search_error_queries": list(state.search_error_queries),
        "search_no_hit_count": search_no_hit_count,
        "search_quota_exhausted_count": search_quota_exhausted_count,
        "quota_exhausted_tool_calls": state.quota_exhausted_tool_calls,
        "max_context_chars": state.max_context_chars,
        "repo_inventory": state.repo_inventory.to_meta() if state.repo_inventory is not None else {"status": "unavailable"},
        "evidence_ledger": state.evidence_ledger.to_meta(),
    }

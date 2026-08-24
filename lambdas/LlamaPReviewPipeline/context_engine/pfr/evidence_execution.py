"""Evidence execution: planned reads, budgets, fetch health, safety sweep."""

from __future__ import annotations

import json
import posixpath
import re

from ... import config
from ..code_extractor import extract_diff_entities
from ..evidence import event_supports_answer
from ..tool_contract import (
    MAX_READ_FILE_SYMBOLS,
    literal_identifier_tokens,
    normalize_tool_step_envelope,
    validate_tool_invocation,
)
from ..tools import ToolExecutor, normalize_tool_args
from typing import Any, Dict, List, Optional, Tuple
from .plan_contract import _append_once, _ensure_question_id, _known_gap_once, _valid_read_step

def _tool_call(name: str, args: Dict[str, Any], call_id: str) -> Dict[str, Any]:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}

def _high_signal_literal_shape(token: str) -> bool:
    """Prefer identifier shape over language-specific keyword allow/deny lists."""

    return bool(
        "_" in token
        or "$" in token
        or any(char.isupper() for char in token[1:])
        or token.isupper()
        or any(char.isdigit() for char in token)
    )

def _diff_literals_from_change(change: Dict[str, Any]) -> List[str]:
    """Extract bounded literal anchors already present in one changed diff.

    This is intentionally syntax-agnostic.  It does not infer package names or
    invent search terms; it merely retains identifier-like atoms and quoted
    values from changed lines. Bounded shape checks exclude numeric atoms,
    hashes, URLs, and path material without imposing language or domain words.
    """

    literals: List[str] = []

    def add(value: Any, *, quoted: bool) -> None:
        text = str(value or "").strip()
        if (
            len(text) < 3
            or len(text) > 120
            or re.fullmatch(r"[0-9._+~-]+", text)
            or re.fullmatch(r"[0-9a-fA-F]{16,}", text)
            or "://" in text
        ):
            return
        if quoted and "/" not in text and "\\" not in text and text not in literals:
            literals.append(text)
        for token in literal_identifier_tokens(text):
            # Quoted strings are explicit values (for example a dependency
            # name).  Bare identifiers need a language-agnostic high-signal
            # shape; otherwise ubiquitous syntax words such as ``return`` can
            # consume the five-slice cap before the changed contract appears.
            if not quoted and not _high_signal_literal_shape(token):
                continue
            if token not in literals:
                literals.append(token)

    diff = str(change.get("diff") or change.get("patch") or "")
    for raw_line in diff.splitlines():
        if not raw_line.startswith(("+", "-")) or raw_line.startswith(
            ("+++", "---")
        ):
            continue
        line = raw_line[1:]
        for quoted in re.findall(r"['\"]([^'\"\r\n]{3,120})['\"]", line):
            add(quoted, quoted=True)
        for token in literal_identifier_tokens(line):
            add(token, quoted=False)
        if len(literals) >= 24:
            return literals
    return literals

def _diff_literals_for_path(pr_content: Dict[str, Any], path: str) -> List[str]:
    for change in pr_content.get("file_changes") or []:
        if (
            isinstance(change, dict)
            and str(change.get("file_path") or "") == path
        ):
            return _diff_literals_from_change(change)
    return []

def _normalized_changed_reference(
    raw_reference: str,
    *,
    source_path: str,
) -> str:
    value = str(raw_reference or "").strip()
    if not value or "/" not in value or "://" in value or value.startswith("/"):
        return ""
    if value.startswith(("./", "../")):
        value = posixpath.normpath(
            posixpath.join(posixpath.dirname(source_path), value)
        )
    else:
        value = posixpath.normpath(value)
    if value in {"", ".", ".."} or value.startswith("../"):
        return ""
    return value.strip("/")

def _companion_diff_literals_for_target(
    pr_content: Dict[str, Any],
    target_path: str,
) -> List[str]:
    """Rank literals from changed files that explicitly reference target."""

    counts: Dict[str, int] = {}
    first_seen: Dict[str, int] = {}
    ordinal = 0
    for change in pr_content.get("file_changes") or []:
        if not isinstance(change, dict):
            continue
        source_path = str(change.get("file_path") or "")
        if not source_path or source_path == target_path:
            continue
        diff = str(change.get("diff") or change.get("patch") or "")
        quoted_values = re.findall(r"['\"]([^'\"\r\n]{3,240})['\"]", diff)
        if not any(
            _normalized_changed_reference(value, source_path=source_path)
            == target_path
            for value in quoted_values
        ):
            continue
        diff = str(change.get("diff") or change.get("patch") or "")
        for raw_line in diff.splitlines():
            if not raw_line.startswith(("+", "-")) or raw_line.startswith(
                ("+++", "---")
            ):
                continue
            line = raw_line[1:]
            values: List[str] = []
            for quoted in re.findall(r"['\"]([^'\"\r\n]{3,120})['\"]", line):
                if "/" not in quoted and "\\" not in quoted:
                    values.append(quoted)
                values.extend(
                    token
                    for token in literal_identifier_tokens(quoted)
                )
            for regex_literal in re.findall(r"/([^/\r\n]{3,120})/", line):
                values.append(regex_literal)
                values.extend(literal_identifier_tokens(regex_literal))
            for literal in values:
                if (
                    len(literal) < 3
                    or len(literal) > 120
                    or re.fullmatch(r"[0-9._+~-]+", literal)
                    or re.fullmatch(r"[0-9a-fA-F]{16,}", literal)
                    or "://" in literal
                ):
                    continue
                if literal not in first_seen:
                    first_seen[literal] = ordinal
                    ordinal += 1
                counts[literal] = counts.get(literal, 0) + 1
    return sorted(
        counts,
        key=lambda literal: (-counts[literal], first_seen[literal]),
    )[:24]

def _large_read_path_tokens(path: str) -> set[str]:
    tokens: set[str] = set()
    for component in str(path or "").split("/"):
        tokens.update(
            token.casefold()
            for token in literal_identifier_tokens(component)
        )
    return tokens

def _usable_large_read_literal(
    value: str,
    *,
    path_tokens: set[str],
) -> bool:
    text = str(value or "").strip()
    folded = text.casefold()
    return bool(
        3 <= len(text) <= 120
        and folded not in path_tokens
        and not re.fullmatch(r"[0-9._+~-]+", text)
        and not re.fullmatch(r"[0-9a-fA-F]{16,}", text)
        and "://" not in text
        and "/" not in text
        and "\\" not in text
    )

def _address_large_read_steps(
    steps: List[Dict[str, Any]],
    *,
    state,
    entities: Dict[str, set],
    named_hints: List[str],
) -> List[str]:
    """Attach only already-named literals to large content reads."""

    inventory = state.repo_inventory
    if inventory is None:
        return []
    base_deterministic_tokens: set[str] = set()
    for values in entities.values():
        iterable = values if isinstance(values, (set, list, tuple)) else (values,)
        for value in iterable:
            nested = value if isinstance(value, (list, tuple, set)) else (value,)
            for item in nested:
                base_deterministic_tokens.update(
                    literal_identifier_tokens(item)
                )
    for hint in named_hints:
        base_deterministic_tokens.update(literal_identifier_tokens(hint))
    diagnostics: List[str] = []
    for step in steps:
        if step.get("tool") != "read_file":
            continue
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        if str(args.get("mode") or "content") != "content":
            continue
        path = str(args.get("path") or "")
        size = inventory.file_size_bytes(path)
        if not isinstance(size, int) or size <= 50 * 1024:
            continue
        path_diff_literals = _diff_literals_for_path(state.pr_content, path)
        companion_diff_literals = _companion_diff_literals_for_target(
            state.pr_content,
            path,
        )
        step_deterministic_tokens = set(base_deterministic_tokens)
        step_deterministic_tokens.update(path_diff_literals)
        step_deterministic_tokens.update(companion_diff_literals)
        step_deterministic_casefold = {
            token.casefold() for token in step_deterministic_tokens
        }
        diff_literal_casefold = {
            token.casefold()
            for token in [*path_diff_literals, *companion_diff_literals]
        }
        path_tokens = _large_read_path_tokens(path)
        named_text = " ".join(
            str(value or "")
            for value in (
                step.get("question"),
                step.get("why_it_matters"),
                args.get("reason"),
            )
        )
        allowed_named_text = "\n".join(
            [named_text, *sorted(step_deterministic_tokens)]
        ).casefold()
        supplied_symbols = [
            str(symbol).strip()
            for symbol in args.get("symbols") or []
            if isinstance(symbol, str) and str(symbol).strip()
        ]
        if supplied_symbols:
            grounded_symbols = [
                symbol
                for symbol in supplied_symbols
                if symbol.casefold() in allowed_named_text
                and _usable_large_read_literal(
                    symbol,
                    path_tokens=path_tokens,
                )
                and (
                    _high_signal_literal_shape(symbol)
                    or symbol.casefold() in diff_literal_casefold
                )
            ][:MAX_READ_FILE_SYMBOLS]
            if grounded_symbols:
                args["symbols"] = grounded_symbols
                step["args"] = args
                if grounded_symbols != supplied_symbols:
                    state.record_tool_arg_repair(
                        "read_file.large_symbols_dropped_ungrounded"
                    )
                    diagnostics.append(
                        f"large_read_symbols_filtered:{path}:{len(grounded_symbols)}"
                    )
                continue
            args.pop("symbols", None)
            step["args"] = args
            state.record_tool_arg_repair(
                "read_file.large_symbols_dropped_ungrounded"
            )
        candidates: List[str] = []
        candidate_tokens = list(literal_identifier_tokens(named_text))
        candidate_tokens.extend(
            token
            for token in [*path_diff_literals, *companion_diff_literals]
            if _high_signal_literal_shape(token)
            and token not in candidate_tokens
        )
        candidate_tokens.extend(
            token
            for token in [*path_diff_literals, *companion_diff_literals]
            if token not in candidate_tokens
        )
        for token in candidate_tokens:
            if (
                _usable_large_read_literal(
                    token,
                    path_tokens=path_tokens,
                )
                and (
                    _high_signal_literal_shape(token)
                    or token.casefold() in step_deterministic_casefold
                )
            ):
                if token not in candidates:
                    candidates.append(token)
            if len(candidates) >= MAX_READ_FILE_SYMBOLS:
                break
        if candidates:
            args["symbols"] = candidates
            step["args"] = args
            state.record_tool_arg_repair(
                "read_file.large_symbols_from_named_literals"
            )
            diagnostics.append(
                f"large_read_symbols_attached:{path}:{len(candidates)}"
            )
        else:
            diagnostics.append(f"large_read_unaddressable:{path}")
    return diagnostics

def _followup_steps(reconcile: Dict[str, Any], round_index: int, state) -> List[Dict[str, Any]]:
    steps = []
    for idx, item in enumerate(reconcile.get("followups") or []):
        if not isinstance(item, dict):
            continue
        envelope = normalize_tool_step_envelope(item)
        if not envelope.valid:
            continue
        if envelope.action:
            state.record_tool_arg_repair(envelope.action)
        item = envelope.step
        if item.get("tool") not in {"search_code", "read_file", "list_dir"}:
            continue
        tool = item.get("tool")
        args = normalize_tool_args(tool, item.get("args") if isinstance(item.get("args"), dict) else {}, state)
        tool_contract = validate_tool_invocation(tool, args)
        if not tool_contract.valid:
            continue
        args = tool_contract.args
        item["args"] = args
        step = {
            "question": item.get("question", ""),
            "tool": tool,
            "args": args,
            "id": f"followup_{round_index}_{idx}",
        }
        if not _valid_read_step(step, state):
            continue
        steps.append(step)
    return steps

def _record_terminal_unexecuted_followups(
    reconcile: Dict[str, Any],
    round_index: int,
    state,
    *,
    steps: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Persist valid terminal requests without turning them into tool calls."""

    records: List[Dict[str, Any]] = []
    terminal_steps = (
        steps
        if steps is not None
        else _followup_steps(reconcile, round_index, state)
    )
    for step in terminal_steps:
        question_id = _ensure_question_id(step, state)
        state.evidence_ledger.set_question_lifecycle(
            question_id,
            "terminal_unexecuted",
        )
        records.append(
            {
                "question_id": question_id,
                **_verification_step_summary(step),
                "reason": "terminal_round_reached",
            }
        )
    return records

def _terminal_evidence_read(
    executor: ToolExecutor,
    reconcile: Dict[str, Any],
    followups: List[Dict[str, Any]],
) -> tuple[Optional[Dict[str, Any]], str]:
    """Execute at most one final content read after the last Reconcile.

    This bounded rescue gathers direct evidence only. It does not rewrite or
    resolve an earlier question, and Deep sees it as post-Reconcile evidence.
    """

    state = executor.state
    if reconcile.get("complete") is True:
        return None, "reconcile_complete"
    if not any(
        isinstance(item, dict)
        for item in reconcile.get("unresolved_gaps") or []
    ):
        return None, "no_unresolved_gap"

    def executable_content_read(step: Dict[str, Any]) -> bool:
        if step.get("tool") != "read_file":
            return False
        args = step.get("args") or {}
        if str(args.get("mode") or "content") != "content":
            return False
        size = (
            state.repo_inventory.file_size_bytes(str(args.get("path") or ""))
            if state.repo_inventory is not None
            else None
        )
        return bool(
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 50 * 1024
            or args.get("symbols")
        )

    eligible = [step for step in followups if executable_content_read(step)]
    if len(eligible) != 1:
        return None, "no_single_content_read"
    if state.read_calls >= state.max_read_calls:
        return None, "hard_read_cap_reached"
    if state.remaining_time() < 30:
        return None, "context_time_reserve"
    deadline_remaining = getattr(state.deadline, "remaining_seconds", None)
    if callable(deadline_remaining) and float(deadline_remaining()) < 30:
        return None, "pipeline_deadline_reserve"

    selected = eligible[0]
    question_id = _ensure_question_id(selected, state)
    before = len(state.tool_events)
    # This final evidence read may cross the soft budget. The hard read cap and
    # both durable-write reserves were checked above.
    executor.execute(
        _tool_call(
            "read_file",
            selected.get("args") or {},
            selected.get("id") or "terminal_evidence_read",
        ),
        question_id=question_id,
    )
    if len(state.tool_events) <= before:
        return None, "execution_not_recorded"
    outcome = str(state.tool_events[-1].get("outcome") or "unknown")
    return selected, outcome

def _soft_budget_reached(state) -> bool:
    if state.deadline is not None and state.deadline.remaining_seconds() <= 0.5:
        return True
    return bool(state.soft_budget_seconds and state.soft_budget_seconds > 0 and state.elapsed_time() >= state.soft_budget_seconds)

def _mark_soft_budget_exhausted(state) -> None:
    state.soft_budget_exhausted = True
    state.budget_exhausted_flag = True
    state.finish_reason = "budget_exhausted"
    if "soft_budget_exhausted" not in state.budget_health_reasons:
        state.budget_health_reasons.append("soft_budget_exhausted")
    gap = "PFR soft time budget reached; remaining planned lookups were skipped and review must rely on collected evidence."
    if gap not in state.known_gaps:
        state.known_gaps.append(gap)

def _verification_step_summary(step: Dict[str, Any]) -> Dict[str, str]:
    args = step.get("args") or {}
    summary = {
        "tool": str(step.get("tool") or ""),
        "question": str(step.get("question") or "")[:240],
        "reason": str(args.get("reason") or "")[:240],
    }
    if args.get("path"):
        summary["path"] = str(args.get("path"))
    if args.get("query"):
        summary["query"] = str(args.get("query"))
    return {key: value for key, value in summary.items() if value}

def _record_budget_skipped_verification(state, step: Dict[str, Any]) -> None:
    summary = _verification_step_summary(step)
    if not summary:
        return
    if summary not in state.budget_skipped_verification_steps:
        state.budget_skipped_verification_steps.append(summary)
    path = summary.get("path")
    if path:
        _append_once(state.budget_skipped_verification_paths, path)
    if "budget_skipped_verification" not in state.budget_health_reasons:
        state.budget_health_reasons.append("budget_skipped_verification")
    label = f"`{path}`" if path else summary.get("query") or summary.get("tool") or "verification step"
    _known_gap_once(state, f"PFR soft budget skipped verification lookup for {label}; review must not treat that lookup as verified.")
    question_id = _ensure_question_id(step, state)
    state.evidence_ledger.set_question_lifecycle(question_id, "budget_skipped")

def _record_budget_skipped_for_steps(state, steps: List[Dict[str, Any]]) -> None:
    for step in steps:
        _record_budget_skipped_verification(state, step)

def _read_step_can_expand_evidence(state, args: Dict[str, Any]) -> bool:
    """Return whether a bounded read can add scope beyond earlier evidence.

    ``read_success_paths`` proves that some content from a path was observed;
    it does not prove that every requested symbol in that file was observed.
    Reconcile may legitimately broaden an initial symbol slice after learning
    that the deciding implementation is still missing.  Permit the one
    existing soft-budget rescue for that broader request while rejecting an
    exact repeat or a request already covered by a full-file observation.
    """

    path = str(args.get("path") or "").strip().strip("/")
    mode = str(args.get("mode") or "content")
    if not path or mode != "content":
        return path not in state.read_success_paths
    if path not in state.read_success_paths:
        return True

    prior_symbols: set[str] = set()
    backend_full_file_fetched = False
    full_file_observed = False
    for event in state.tool_events:
        if event.get("tool") != "read_file" or event.get("outcome") != "hit":
            continue
        prior_args = event.get("args") if isinstance(event.get("args"), dict) else {}
        prior_path = str(prior_args.get("path") or "").strip().strip("/")
        prior_mode = str(prior_args.get("mode") or "content")
        if prior_path != path or prior_mode != "content":
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        backend_full_file_fetched = bool(
            backend_full_file_fetched
            or metadata.get("backend_full_file_fetched") is True
        )
        full_file_observed = bool(
            full_file_observed
            or metadata.get("coverage_type") == "full_file"
        )
        prior_symbols.update(
            str(symbol).strip()
            for symbol in prior_args.get("symbols") or []
            if isinstance(symbol, str) and symbol.strip()
        )

    if full_file_observed:
        return False
    requested_symbols = {
        str(symbol).strip()
        for symbol in args.get("symbols") or []
        if isinstance(symbol, str) and symbol.strip()
    }
    if requested_symbols:
        return bool(
            isinstance((state.source_text_cache.get(path) or {}).get("content"), str)
            and not requested_symbols.issubset(prior_symbols)
        )
    return bool(backend_full_file_fetched)

def _planned_read_priority(state, args: Dict[str, Any]) -> int:
    """Return the stable Plan priority for a read request."""

    path = str(args.get("path") or "").strip().strip("/")
    mode = str(args.get("mode") or "content").strip() or "content"
    requests = getattr(state, "planned_read_requests", []) or []
    for index, request in enumerate(requests):
        if (
            str(request.get("path") or "").strip().strip("/") == path
            and (str(request.get("mode") or "content").strip() or "content")
            == mode
        ):
            return index
    return len(requests)

def _terminal_read_rescue_index(state, steps: List[Dict[str, Any]]) -> Optional[int]:
    """Choose one eligible rescue by Plan priority, then remaining order."""

    candidates: List[Tuple[int, int]] = []
    for index, step in enumerate(steps):
        if step.get("tool") != "read_file":
            continue
        args = step.get("args") or {}
        path = str(args.get("path") or "").strip()
        exact_path_rescue = (
            str(args.get("mode") or "content") == "exact_path_existence"
            and state.repo_inventory is not None
            and (
                state.repo_inventory.exact_path_state(path) in {"present", "absent"}
                or state.repo_inventory.can_direct_probe(path)
            )
        )
        if (
            path
            and (path in state.accessible_files or exact_path_rescue)
            and _read_step_can_expand_evidence(state, args)
        ):
            candidates.append((_planned_read_priority(state, args), index))
    if not candidates:
        return None
    return min(candidates)[1]

def _prioritize_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def priority(step: Dict[str, Any]) -> int:
        if (
            step.get("tool") == "search_code"
            and step.get("_priority_class") == "diff_removed_symbol_floor"
        ):
            # The one reserved deleted-symbol check must execute, not merely
            # survive plan truncation. Put it first so a soft time budget
            # cannot consistently skip it after several slow reads.
            return 0
        # Preserve the model's semantic question order for every ordinary
        # step. Reordering by tool type would undo the planner's explicit
        # acceptance-criteria and highest-consequence priorities.
        return 1

    return sorted(
        enumerate(steps), key=lambda item: (priority(item[1]), item[0])
    )

def _ordered_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [step for _, step in _prioritize_steps(steps)]

def _cap_planned_steps(
    steps: List[Dict[str, Any]],
    state,
    *,
    max_steps: Optional[int] = None,
) -> List[Dict[str, Any]]:
    max_steps = max(
        1,
        int(
            max_steps
            if max_steps is not None
            else config.PFR_MAX_PLAN_QUESTIONS
        ),
    )
    kept: List[Dict[str, Any]] = []
    counts = {"read_file": 0, "search_code": 0, "list_dir": 0}
    tool_caps = {
        "read_file": max(0, int(state.max_read_calls)),
        "search_code": max(0, int(state.max_search_calls)),
        "list_dir": max_steps,
    }
    for step in steps:
        tool = str(step.get("tool") or "")
        if len(kept) >= max_steps or counts.get(tool, 0) >= tool_caps.get(tool, 0):
            continue
        kept.append(step)
        counts[tool] = counts.get(tool, 0) + 1
    removal_steps = [
        step
        for step in steps
        if step.get("tool") == "search_code"
        and step.get("_priority_class") == "diff_removed_symbol_floor"
    ]
    if (
        removal_steps
        and not any(step in kept for step in removal_steps)
        and tool_caps["search_code"] > 0
    ):
        reserved = removal_steps[0]
        if len(kept) >= max_steps:
            # Replace the lowest-priority tail item while retaining the
            # planner's semantic order for every surviving ordinary step.
            kept.pop()
        kept.append(reserved)
        kept = [step for step in steps if step in kept]
    dropped = [step for step in steps if step not in kept]
    for step in dropped:
        question_id = _ensure_question_id(step, state)
        state.evidence_ledger.set_question_lifecycle(question_id, "dropped_cap")
    return kept

def _execute_steps(
    executor: ToolExecutor,
    steps: List[Dict[str, Any]],
    *,
    terminal_read_rescue: int = 0,
    record_skipped_verification: bool = False,
) -> None:
    for index, step in enumerate(steps):
        if _soft_budget_reached(executor.state):
            _mark_soft_budget_exhausted(executor.state)
            remaining_steps = list(steps[index:])
            rescue_index = (
                _terminal_read_rescue_index(executor.state, remaining_steps)
                if terminal_read_rescue > 0
                else None
            )
            if rescue_index is not None:
                rescue_step = remaining_steps.pop(rescue_index)
                args = rescue_step.get("args") or {}
                question_id = _ensure_question_id(rescue_step, executor.state)
                executor.execute(
                    _tool_call(
                        rescue_step["tool"],
                        args,
                        rescue_step.get("id") or rescue_step["tool"],
                    ),
                    question_id=question_id,
                )
            if record_skipped_verification:
                _record_budget_skipped_for_steps(executor.state, remaining_steps)
            break
        question_id = _ensure_question_id(step, executor.state)
        executor.execute(
            _tool_call(step["tool"], step.get("args") or {}, step.get("id") or step["tool"]),
            question_id=question_id,
        )

def _read_file_error_count(state) -> int:
    return sum(1 for event in state.tool_events if event.get("tool") == "read_file" and event.get("outcome") == "error")

def _planned_read_request_identities(
    state,
    plan: Dict[str, Any],
) -> List[Tuple[str, str]]:
    """Return distinct read requests without collapsing modes by path.

    Current PFR state records typed requests before path validation. Plan and
    executed events provide the remaining request sources.
    """

    identities: List[Tuple[str, str]] = []

    def add(path: Any, mode: Any = "content") -> None:
        normalized_path = str(path or "").strip().strip("/")
        normalized_mode = str(mode or "content").strip() or "content"
        if not normalized_path:
            return
        identity = (normalized_path, normalized_mode)
        if identity not in identities:
            identities.append(identity)

    for request in getattr(state, "planned_read_requests", []) or []:
        if isinstance(request, dict):
            add(request.get("path"), request.get("mode"))
    for item in plan.get("verification_plan") or []:
        if not isinstance(item, dict) or item.get("tool") != "read_file":
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        add(args.get("path"), args.get("mode"))
    for event in state.tool_events:
        if event.get("tool") != "read_file":
            continue
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        add(args.get("path"), args.get("mode"))
    return identities

def _fetch_health(state, plan: Dict[str, Any]) -> Dict[str, Any]:
    pr_type = str(plan.get("pr_type") or "").lower()
    planned_read_requests = _planned_read_request_identities(state, plan)
    planned_read_paths = list(dict.fromkeys(path for path, _mode in planned_read_requests))
    planned_reads = len(planned_read_requests)
    read_errors = max(_read_file_error_count(state), len(state.read_error_paths))
    content_requests = [
        (path, mode)
        for path, mode in planned_read_requests
        if mode != "exact_path_existence"
    ]
    exact_path_requests = [
        (path, mode)
        for path, mode in planned_read_requests
        if mode == "exact_path_existence"
    ]
    content_paths = list(dict.fromkeys(path for path, _mode in content_requests))
    exact_path_paths = list(
        dict.fromkeys(path for path, _mode in exact_path_requests)
    )
    exact_path_outcomes: Dict[str, str] = {}
    content_success_identities: set[Tuple[str, str]] = set()
    backend_attempted_count = 0
    derived_hit_count = 0
    for event in state.tool_events:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        if metadata.get("backend_attempted", True):
            backend_attempted_count += 1
        elif event.get("outcome") == "hit":
            derived_hit_count += 1
        if event.get("tool") != "read_file":
            continue
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        mode = str(args.get("mode") or "content")
        path = str(args.get("path") or "").strip().strip("/")
        if mode != "exact_path_existence":
            if (
                path
                and event.get("outcome") == "hit"
                and metadata.get("coverage_type") in {"full_file", "file_slice"}
                and metadata.get("observed_state") == "content_observed"
            ):
                content_success_identities.add((path, mode))
            continue
        observed = str(metadata.get("observed_state") or "unknown")
        if path:
            exact_path_outcomes[path] = observed
    removed_path_skips = list(
        dict.fromkeys(path for path in content_paths if path in state.removed_paths)
    )
    planned_unread_paths = [
        path for path in content_paths if path not in state.read_success_paths and path not in state.removed_paths
    ]
    actionable_content_requests = [
        identity
        for identity in content_requests
        if identity[0] not in state.removed_paths
    ]
    actionable_planned_reads = len(actionable_content_requests)
    successful_planned_reads = [
        identity
        for identity in actionable_content_requests
        if identity in content_success_identities
    ]
    planned_content_unobserved_paths = [
        path
        for path, _mode in actionable_content_requests
        if path not in state.read_success_paths
        and state.read_outcomes.get(path) == "large_read_unaddressable"
    ]
    exact_success_paths = [
        path for path in exact_path_paths
        if exact_path_outcomes.get(path) in {"present", "absent"}
    ]
    plan_question_ids = {
        str(item.get("question_id") or "")
        for item in plan.get("verification_plan") or []
        if isinstance(item, dict) and item.get("question_id")
    }
    # Include valid follow-ups that were actually scheduled, but exclude
    # questions removed by the cap/redundancy pass and route-preflight facts.
    for question_id, question in state.evidence_ledger.questions.items():
        if question.get("tool") in {
            "search_code",
            "read_file",
            "list_dir",
        } and question.get("lifecycle") in {
            "planned",
            "executed",
            "budget_skipped",
            "terminal_unexecuted",
            "dropped_invalid",
        }:
            plan_question_ids.add(question_id)

    question_health: List[Dict[str, Any]] = []
    supporting_question_count = 0
    completed_question_count = 0

    def terminal_completion_kind(events: List[Dict[str, Any]]) -> str:
        if not events:
            return "unexecuted"
        return str(events[-1].get("outcome") or "failed")

    for question_id in sorted(plan_question_ids):
        question = state.evidence_ledger.questions.get(question_id) or {}
        tool = str(question.get("tool") or "")
        args = question.get("args") if isinstance(question.get("args"), dict) else {}
        lifecycle = str(question.get("lifecycle") or "planned")
        read_mode = str(args.get("mode") or "content")
        read_path = str(args.get("path") or "").strip().strip("/")
        events = [
            state.evidence_ledger.events[event_id]
            for event_id in question.get("event_ids") or []
            if event_id in state.evidence_ledger.events
        ]
        completed = False
        supporting = False
        completion_kind = "unexecuted"
        if lifecycle in {"budget_skipped", "terminal_unexecuted", "dropped_invalid"}:
            completion_kind = lifecycle
        elif tool == "search_code":
            successful = [
                event
                for event in events
                if event.get("outcome") in {"hit", "no_hit"}
            ]
            completed = bool(successful)
            supporting = any(
                event_supports_answer(
                    event,
                    expected_head_sha=state.head_sha,
                )
                for event in successful
            )
            if supporting:
                completion_kind = "hit"
            elif completed:
                completion_kind = "no_hit"
            else:
                completion_kind = terminal_completion_kind(events)
        elif tool == "list_dir":
            successful = [
                event
                for event in events
                if event.get("outcome") in {"hit", "no_hit"}
            ]
            completed = bool(successful)
            supporting = any(
                event_supports_answer(
                    event,
                    expected_head_sha=state.head_sha,
                )
                for event in successful
            )
            if supporting:
                completion_kind = "hit"
            elif completed:
                completion_kind = "no_hit"
            else:
                completion_kind = terminal_completion_kind(events)
        elif tool == "read_file":
            mode = read_mode
            path = read_path
            if mode == "exact_path_existence":
                successful = [
                    event
                    for event in events
                    if event_supports_answer(
                        event,
                        expected_head_sha=state.head_sha,
                    )
                    and event.get("coverage_type") == "exact_path_state"
                    and event.get("observed_state") in {"present", "absent"}
                ]
            else:
                successful = [
                    event
                    for event in events
                    if event_supports_answer(
                        event,
                        expected_head_sha=state.head_sha,
                    )
                    and event.get("coverage_type") in {"full_file", "file_slice"}
                    and event.get("observed_state") == "content_observed"
                ]
                if path in state.removed_paths and any(
                    event.get("outcome") == "removed_path" for event in events
                ):
                    completed = True
                    completion_kind = "removed_at_head"
            if successful:
                completed = True
                supporting = True
                completion_kind = "hit"
            elif not completed:
                completion_kind = terminal_completion_kind(events)
        if completed:
            completed_question_count += 1
        if supporting:
            supporting_question_count += 1
        question_health.append(
            {
                "question_id": question_id,
                "tool": tool,
                "lifecycle": lifecycle,
                "completed": completed,
                "supporting_evidence": supporting,
                "completion_kind": completion_kind,
            }
        )

    requested_count = len(question_health)
    success_count = completed_question_count
    if not requested_count:
        planned_retrieval_status = "not_requested"
    elif success_count == requested_count:
        planned_retrieval_status = "complete"
    elif success_count:
        planned_retrieval_status = "degraded"
    else:
        planned_retrieval_status = "failed"
    status = "healthy" if planned_retrieval_status in {"not_requested", "complete"} else "partial_or_failed_context"
    degradation_reason_counts: Dict[str, int] = {}

    def count_degradation(kind: str, amount: int = 1) -> None:
        normalized_amount = max(0, int(amount))
        if normalized_amount:
            degradation_reason_counts[kind] = (
                degradation_reason_counts.get(kind, 0)
                + normalized_amount
            )

    for item in question_health:
        if item.get("completed"):
            # A literal search/list no_hit is a completed query. It supplies no
            # answer evidence, but it is not itself a retrieval-health failure.
            continue
        lifecycle = str(item.get("lifecycle") or "")
        completion_kind = str(item.get("completion_kind") or "")
        tool = str(item.get("tool") or "")
        if lifecycle == "dropped_invalid":
            count_degradation("invalid_plan")
        elif lifecycle == "budget_skipped":
            count_degradation("budget_skipped")
        elif lifecycle == "terminal_unexecuted":
            count_degradation("terminal_unexecuted")
        elif completion_kind == "large_read_unaddressable":
            count_degradation("large_read_unaddressable")
        elif completion_kind in {"quota_exhausted", "budget_exhausted"}:
            count_degradation("quota_or_budget_exhausted")
        elif completion_kind in {
            "invalid_path",
            "not_found",
            "removed_path",
            "directory",
            "excluded_by_policy",
        }:
            count_degradation("read_unavailable")
        elif tool == "search_code":
            count_degradation("search_error")
        elif tool == "read_file":
            count_degradation("read_failure")
        elif tool == "list_dir":
            count_degradation("list_failure")
        else:
            count_degradation("unexecuted")

    if state.planned_invalid_read_paths:
        degradation_reason_counts["invalid_plan"] = max(
            degradation_reason_counts.get("invalid_plan", 0),
            len(state.planned_invalid_read_paths),
        )
    skipped_count = max(
        len(state.budget_skipped_verification_paths),
        len(state.budget_skipped_verification_steps),
    )
    if skipped_count:
        degradation_reason_counts["budget_skipped"] = max(
            degradation_reason_counts.get("budget_skipped", 0),
            skipped_count,
        )
    if state.search_error_tool_calls:
        degradation_reason_counts["search_error"] = max(
            degradation_reason_counts.get("search_error", 0),
            int(state.search_error_tool_calls),
        )
    if planned_content_unobserved_paths:
        degradation_reason_counts["large_read_unaddressable"] = max(
            degradation_reason_counts.get("large_read_unaddressable", 0),
            len(planned_content_unobserved_paths),
        )

    reasons: List[str] = []
    inventory = state.repo_inventory
    if inventory is None or inventory.status == "error":
        reasons.append("repo_inventory_unavailable")
    elif inventory.status == "partial":
        reasons.append("repo_inventory_partial")
    reasons.extend(sorted(degradation_reason_counts))
    return {
        "status": status,
        "pr_type": pr_type,
        "planned_read_file_count": planned_reads,
        "actionable_planned_read_file_count": actionable_planned_reads,
        "planned_retrieval_status": planned_retrieval_status,
        "planned_question_count": requested_count,
        "completed_question_count": completed_question_count,
        "supporting_question_count": supporting_question_count,
        "question_health": question_health,
        "backend_attempted_tool_event_count": backend_attempted_count,
        "derived_hit_event_count": derived_hit_count,
        "planned_read_paths": planned_read_paths,
        "planned_read_requests": [
            {"path": path, "mode": mode}
            for path, mode in planned_read_requests
        ],
        "planned_content_read_paths": content_paths,
        "planned_exact_path_count": len(exact_path_requests),
        "planned_exact_path_paths": exact_path_paths,
        "exact_path_outcomes": dict(sorted(exact_path_outcomes.items())),
        "planned_unread_paths": planned_unread_paths,
        "planned_content_unobserved_paths": planned_content_unobserved_paths,
        "planned_invalid_read_paths": list(state.planned_invalid_read_paths),
        "removed_path_skips": removed_path_skips,
        "budget_skipped_verification_paths": list(state.budget_skipped_verification_paths),
        "budget_skipped_verification_steps": list(state.budget_skipped_verification_steps),
        "read_file_error_count": read_errors,
        "read_success_count": len(state.read_success_paths),
        "read_error_count": len(state.read_error_paths),
        "read_error_paths": sorted(state.read_error_paths),
        "read_outcomes": dict(sorted(state.read_outcomes.items())),
        "search_error_count": state.search_error_tool_calls,
        "search_error_queries": list(state.search_error_queries),
        "soft_budget_exhausted": state.soft_budget_exhausted,
        "soft_budget_seconds": state.soft_budget_seconds,
        "budget_health_reasons": list(state.budget_health_reasons),
        "degradation_reason_counts": dict(
            sorted(degradation_reason_counts.items())
        ),
        "reasons": reasons,
    }

def _safety_sweep(
    state,
    executor: ToolExecutor,
    planned_steps: List[Dict[str, Any]],
) -> int:
    event_start = len(state.tool_events)
    planned_queries = {
        " ".join(str((step.get("args") or {}).get("query") or "").strip().lower().rstrip("(").split())
        for step in planned_steps
        if step.get("tool") == "search_code"
    }
    entities = extract_diff_entities(state.pr_content)
    symbols = sorted((entities.get("added_symbols") or set()) | (entities.get("removed_symbols") or set()))
    for index, symbol in enumerate(symbols[:4]):
        if _soft_budget_reached(state):
            _mark_soft_budget_exhausted(state)
            break
        key = symbol.lower()
        if key in planned_queries:
            continue
        if state.search_calls >= state.max_search_calls:
            break
        step = {
            "question": f"Find callers/usages of {symbol}.",
            "tool": "search_code",
            "args": {"query": f"{symbol}(", "reason": f"Safety sweep for changed symbol {symbol}."},
            "id": f"sweep_{index}",
        }
        question_id = _ensure_question_id(step, state)
        executor.execute(_tool_call("search_code", step["args"], step["id"]), question_id=question_id)
    return sum(
        1
        for event in state.tool_events[event_start:]
        if event.get("outcome") == "hit"
    )

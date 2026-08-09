"""Plan-stage contract: step/ledger primitives, schema normalization, caps."""

from __future__ import annotations

import re

from ... import config
from ..code_extractor import extract_diff_entities
from ...review.schema import ALLOWED_PR_TYPES, PR_TYPE_ALIASES
from ..search_rag import postprocess_search_candidates
from ..tool_contract import (
    literal_query_is_grounded,
    normalize_tool_step_envelope,
    validate_tool_invocation,
    validate_verification_plan,
)
from ..tools import normalize_tool_args
from typing import Any, Dict, List, Optional, Tuple

def _append_once(items: List[str], value: str) -> None:
    if value and value not in items:
        items.append(value)

def _known_gap_once(state, gap: str) -> None:
    if gap and gap not in state.known_gaps:
        state.known_gaps.append(gap)

def _ensure_question_id(step: Dict[str, Any], state) -> str:
    if not state:
        return ""
    existing_id = str(step.get("question_id") or "").strip()
    if existing_id in state.evidence_ledger.questions:
        return existing_id
    question_id = state.evidence_ledger.register_question(
        question=str(step.get("question") or ""),
        tool=str(step.get("tool") or ""),
        args=step.get("args") or {},
    )
    step["question_id"] = question_id
    return question_id

def _register_steps(steps: List[Dict[str, Any]], state) -> None:
    for step in steps:
        _ensure_question_id(step, state)

def _record_invalid_planned_read(state, path: str, *, question: str = "", step: Optional[Dict[str, Any]] = None) -> None:
    if path:
        _append_once(state.planned_read_paths, path)
        _append_once(state.planned_invalid_read_paths, path)
    if "planned_invalid_read_paths" not in state.budget_health_reasons:
        state.budget_health_reasons.append("planned_invalid_read_paths")
    label = f"`{path}`" if path else "a missing path"
    why = f" ({question})" if question else ""
    _known_gap_once(state, f"PFR skipped planned read_file for {label} because it is not in the PR-head accessible file list{why}.")
    if step is not None:
        question_id = _ensure_question_id(step, state)
        state.evidence_ledger.set_question_lifecycle(question_id, "dropped_invalid")

def _valid_read_step(step: Dict[str, Any], state) -> bool:
    if not state or step.get("tool") != "read_file":
        return True
    args = step.get("args") or {}
    path = str(args.get("path") or "").strip().strip("/")
    if not path:
        state.read_file_missing_path_errors += 1
        _record_invalid_planned_read(state, "", question=str(step.get("question") or ""), step=step)
        return False
    args["path"] = path
    mode = str(args.get("mode") or "content")
    request = {"path": path, "mode": mode}
    if request not in state.planned_read_requests:
        state.planned_read_requests.append(request)
    state.planned_read_modes[path] = mode
    if path in state.removed_paths:
        _append_once(state.planned_read_paths, path)
        return True
    inventory = state.repo_inventory
    exact_state = (
        inventory.exact_path_state(path)
        if inventory is not None and mode == "exact_path_existence"
        else "unknown"
    )
    exact_request_is_addressable = mode == "exact_path_existence" and (
        exact_state in {"present", "absent"}
        or (inventory is not None and inventory.can_direct_probe(path))
    )
    if (
        path not in state.accessible_files
        and not exact_request_is_addressable
        and not (inventory is not None and inventory.can_direct_probe(path))
    ):
        _record_invalid_planned_read(state, path, question=str(step.get("question") or ""), step=step)
        return False
    _append_once(state.planned_read_paths, path)
    return True

def _planned_steps(plan: Dict[str, Any], state=None) -> List[Dict[str, Any]]:
    steps = []
    for index, item in enumerate(plan.get("verification_plan") or []):
        if not isinstance(item, dict):
            continue
        tool = item.get("tool")
        if tool not in {"search_code", "read_file", "list_dir"}:
            if state:
                invalid_step = {
                    "question": item.get("question", ""),
                    "tool": str(tool or "invalid_tool"),
                    "args": item.get("args") if isinstance(item.get("args"), dict) else {},
                }
                question_id = _ensure_question_id(invalid_step, state)
                state.evidence_ledger.set_question_lifecycle(question_id, "dropped_invalid")
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        if item.get("why_it_matters") and "reason" not in args:
            args["reason"] = item["why_it_matters"]
        args = normalize_tool_args(tool, args, state)
        tool_contract = validate_tool_invocation(tool, args)
        if not tool_contract.valid:
            if state:
                invalid_step = {
                    "question": item.get("question", ""),
                    "tool": tool,
                    "args": args,
                }
                question_id = _ensure_question_id(invalid_step, state)
                state.evidence_ledger.set_question_lifecycle(question_id, "dropped_invalid")
            continue
        args = tool_contract.args
        item["args"] = args
        step = {
            "question": item.get("question", ""),
            "tool": tool,
            "args": args,
            "id": f"plan_{index}",
        }
        if not _valid_read_step(step, state):
            continue
        steps.append(step)
    return steps

def _sync_plan_from_steps(plan: Dict[str, Any], steps: List[Dict[str, Any]]) -> None:
    plan["verification_plan"] = [
        {
            "question": step.get("question", ""),
            "why_it_matters": (step.get("args") or {}).get("reason", ""),
            "tool": step.get("tool"),
            "args": step.get("args") or {},
            "question_id": step.get("question_id", ""),
        }
        for step in steps
        if step.get("tool") in {"search_code", "read_file", "list_dir"}
    ]

def _normalize_author_acceptance_criteria(
    plan: Dict[str, Any],
    *,
    max_items: int,
) -> List[str]:
    """Normalize the planner's explicit scan of author acceptance criteria."""

    if "author_acceptance_criteria" not in plan:
        plan["author_acceptance_criteria"] = []
        return ["author_acceptance_criteria:missing"]
    raw_items = plan.get("author_acceptance_criteria")
    if not isinstance(raw_items, list):
        plan["author_acceptance_criteria"] = []
        return ["author_acceptance_criteria:invalid_container"]
    accepted: List[Dict[str, str]] = []
    diagnostics: List[str] = []
    for source_index, item in enumerate(raw_items):
        if len(accepted) >= max(0, int(max_items)):
            diagnostics.append(
                f"author_acceptance_criteria[{source_index}]:dropped_cap"
            )
            continue
        if not isinstance(item, dict):
            diagnostics.append(
                f"author_acceptance_criteria[{source_index}]:item_invalid"
            )
            continue
        criterion = item.get("criterion")
        if not isinstance(criterion, str) or not criterion.strip():
            diagnostics.append(
                f"author_acceptance_criteria[{source_index}]:criterion_missing"
            )
            continue
        accepted.append({"criterion": criterion.strip()})
    plan["author_acceptance_criteria"] = accepted
    return diagnostics

def _normalize_plan_schema(plan: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    raw_pr_type = str(plan.get("pr_type") or "").strip().lower()
    normalized_pr_type = PR_TYPE_ALIASES.get(raw_pr_type, raw_pr_type)
    if normalized_pr_type not in ALLOWED_PR_TYPES:
        normalized_pr_type = "mixed"
    if raw_pr_type != normalized_pr_type:
        warnings.append(f"pr_type:{raw_pr_type or '<empty>'}->{normalized_pr_type}")
    plan["pr_type"] = normalized_pr_type

    complexity = str(plan.get("complexity") or "").strip().lower()
    if complexity not in {"low", "normal", "high"}:
        warnings.append(f"complexity:{complexity or '<empty>'}->normal")
        plan["complexity"] = "normal"
    else:
        plan["complexity"] = complexity

    raw_risk_domains = plan.get("risk_domains")
    if raw_risk_domains is None:
        raw_risk_domains = []
    elif not isinstance(raw_risk_domains, list):
        warnings.append("risk_domains:invalid_container->[]")
        raw_risk_domains = []
    risk_domains = [
        item.strip().lower()
        for item in raw_risk_domains
        if isinstance(item, str) and item.strip()
    ]
    plan["risk_domains"] = list(dict.fromkeys(risk_domains))[:8]
    return warnings

def _apply_shared_plan_tool_contract(
    plan: Dict[str, Any],
    state,
    *,
    max_items: Optional[int] = None,
) -> List[str]:
    """Canonicalize legacy aliases, then apply the one typed PFR plan contract."""

    raw_steps = plan.get("verification_plan")
    canonical_steps: Any = raw_steps
    if isinstance(raw_steps, list):
        canonical_steps = []
        for item in raw_steps:
            if not isinstance(item, dict):
                canonical_steps.append(item)
                continue
            envelope = normalize_tool_step_envelope(item)
            if envelope.action:
                state.record_tool_arg_repair(envelope.action)
            if not envelope.valid:
                canonical_steps.append(dict(item))
                continue
            normalized_item = envelope.step
            tool = str(normalized_item.get("tool") or "")
            args = (
                normalized_item.get("args")
                if isinstance(normalized_item.get("args"), dict)
                else {}
            )
            args = dict(args)
            if not str(args.get("reason") or "").strip():
                args["reason"] = str(
                    normalized_item.get("why_it_matters") or ""
                )
            canonical_steps.append(
                {
                    **normalized_item,
                    "args": normalize_tool_args(tool, args, state),
                }
            )
    if isinstance(canonical_steps, list) and state is not None:
        # Invalid model entries are not executable, but they still belong in
        # the replay ledger. Otherwise a strict validator would erase the
        # distinction between "the model asked for an invalid lookup" and
        # "the model never considered this verification question".
        for item in canonical_steps:
            if not isinstance(item, dict):
                continue
            envelope = normalize_tool_step_envelope(item)
            executable_item = envelope.step if envelope.valid else item
            tool = str(executable_item.get("tool") or "")
            args = (
                executable_item.get("args")
                if isinstance(executable_item.get("args"), dict)
                else {}
            )
            checked = (
                validate_tool_invocation(tool, args)
                if envelope.valid
                else None
            )
            required_text_missing = not (
                isinstance(executable_item.get("question"), str)
                and executable_item.get("question", "").strip()
                and isinstance(executable_item.get("why_it_matters"), str)
                and executable_item.get("why_it_matters", "").strip()
            )
            if (
                envelope.valid
                and checked is not None
                and checked.valid
                and not required_text_missing
            ):
                continue
            invalid_step = {
                "question": executable_item.get("question", ""),
                "tool": tool or "invalid_tool",
                "args": args,
            }
            question_id = _ensure_question_id(invalid_step, state)
            state.evidence_ledger.set_question_lifecycle(question_id, "dropped_invalid")
            if (
                tool == "read_file"
                and checked is not None
                and "path_missing" in checked.reasons
            ):
                state.read_file_missing_path_errors += 1
                _record_invalid_planned_read(
                    state,
                    "",
                    question=str(executable_item.get("question") or ""),
                    step=invalid_step,
                )
    accepted, diagnostics = validate_verification_plan(
        canonical_steps,
        max_items=(
            int(max_items)
            if max_items is not None
            else int(config.PFR_MAX_PLAN_QUESTIONS)
        ),
    )
    if isinstance(canonical_steps, list) and state is not None:
        for diagnostic in diagnostics:
            match = re.fullmatch(
                r"verification_plan\[(\d+)\]:dropped_cap",
                str(diagnostic),
            )
            if match is None:
                continue
            index = int(match.group(1))
            if index >= len(canonical_steps):
                continue
            item = canonical_steps[index]
            if not isinstance(item, dict):
                continue
            question_id = _ensure_question_id(item, state)
            state.evidence_ledger.set_question_lifecycle(
                question_id,
                "dropped_cap",
            )
    plan["verification_plan"] = accepted
    return diagnostics

def _postprocess_planned_search_steps(
    steps: List[Dict[str, Any]],
    *,
    state,
    entities: Dict[str, set],
    named_hints: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    raw_search_args = [
        step.get("args") or {}
        for step in steps
        if step.get("tool") == "search_code"
    ]
    if (
        not raw_search_args
        and not entities.get("removed_symbols")
        and not entities.get("added_params")
    ):
        return steps, []
    model_lifecycles: Dict[int, str] = {}
    candidates, debug = postprocess_search_candidates(
        raw_search_args,
        entities=entities,
        pr_content=state.pr_content,
        max_total=state.max_search_calls,
        model_lifecycles=model_lifecycles,
    )
    # Every candidate must retain an identifier named by
    # its question/reason, a diff entity, or an explicit deterministic hint.
    model_source_text: Dict[int, List[str]] = {}
    model_index = 0
    for step in steps:
        if step.get("tool") != "search_code":
            continue
        args = step.get("args") or {}
        model_source_text[model_index] = [
            str(step.get("question") or ""),
            str(args.get("reason") or ""),
        ]
        model_index += 1
    deterministic_grounding = [
        " ".join(
            str(value)
            for key in sorted(entities)
            for value in sorted(str(item) for item in (entities.get(key) or set()))
        ),
        *(named_hints or []),
    ]
    grounded_candidates = []
    for candidate in candidates:
        named_texts = list(deterministic_grounding)
        if candidate.origin_kind == "model":
            named_texts.extend(model_source_text.get(candidate.origin_index, []))
        if literal_query_is_grounded(candidate.args.get("query"), named_texts):
            grounded_candidates.append(candidate)
            continue
        if candidate.origin_kind == "model":
            model_lifecycles[candidate.origin_index] = "dropped_invalid"
        debug.append(
            f"drop_ungrounded_literal:{candidate.origin_kind}:{candidate.args.get('query', '')}"
        )
    candidates = grounded_candidates
    model_candidates = {
        candidate.origin_index: candidate
        for candidate in candidates
        if candidate.origin_kind == "model"
    }
    non_search_steps: List[Dict[str, Any]] = []
    original_model_steps: Dict[int, Dict[str, Any]] = {}
    model_index = 0
    for step in steps:
        if step.get("tool") != "search_code":
            non_search_steps.append(step)
            continue
        candidate = model_candidates.get(model_index)
        original_model_steps[model_index] = step
        model_index += 1
        if candidate is None:
            question_id = _ensure_question_id(step, state)
            lifecycle = model_lifecycles.get(model_index - 1, "dropped_invalid")
            state.evidence_ledger.set_question_lifecycle(question_id, lifecycle)
    rewritten_search_steps: List[Dict[str, Any]] = []
    for candidate in candidates:
        if candidate.origin_kind == "model":
            copied = dict(original_model_steps[candidate.origin_index])
            copied["args"] = dict(candidate.args)
            copied.pop("_priority_class", None)
            if candidate.code_owned_priority:
                copied["_priority_class"] = candidate.code_owned_priority
            rewritten_search_steps.append(copied)
            continue
        args = dict(candidate.args)
        rewritten = {
                "question": args.get("reason") or "Find repo usages from diff-derived search seed.",
                "tool": "search_code",
                "args": args,
                "id": (
                    f"search_seed_{candidate.origin_kind}_"
                    f"{candidate.origin_index}"
                ),
            }
        if candidate.code_owned_priority:
            rewritten["_priority_class"] = candidate.code_owned_priority
        rewritten_search_steps.append(rewritten)
    return [*non_search_steps, *rewritten_search_steps], debug

def _fallback_plan(state, repo_facts: str) -> Dict[str, Any]:
    steps = []
    for change in (state.pr_content.get("file_changes") or [])[:6]:
        path = change.get("file_path")
        if path:
            steps.append(
                {
                    "question": f"Inspect changed file {path}.",
                    "why_it_matters": "Changed file context is the deterministic floor for review.",
                    "tool": "read_file",
                    "args": {"path": path, "reason": "Inspect changed file context."},
                }
            )
    entities = extract_diff_entities(state.pr_content)
    for symbol in sorted((entities.get("added_symbols") or set()) | (entities.get("removed_symbols") or set()))[:3]:
        steps.append(
            {
                "question": f"Find callers/usages of {symbol}.",
                "why_it_matters": "Caller coverage can reveal cross-file breakage.",
                "tool": "search_code",
                "args": {"query": f"{symbol}(", "reason": f"Find callers/usages of {symbol}."},
            }
        )
    return {
        "complexity": "normal",
        "pr_type": "mixed",
        "risk_domains": [],
        "author_acceptance_criteria": [],
        "verification_plan": steps[: config.PFR_MAX_PLAN_QUESTIONS],
        "repo_facts": repo_facts,
    }

def _plan_question_cap(route: Dict[str, Any]) -> int:
    """Normal gets six questions; high may use eight, never above config."""

    route_cap = 8 if str(route.get("complexity") or "").lower() == "high" else 6
    return max(1, min(route_cap, int(config.PFR_MAX_PLAN_QUESTIONS)))

def _plan_model_selection(route: Dict[str, Any]) -> Tuple[str, str]:
    """Use Pro/high for High or risk-bearing plans."""

    risk_domains = route.get("risk_domains")
    risk_bearing = bool(
        isinstance(risk_domains, list)
        and any(
            isinstance(item, str) and item.strip()
            for item in risk_domains
        )
    )
    if risk_bearing:
        return config.DEEPSEEK_MODEL, "high"
    if str(route.get("complexity") or "").strip().lower() == "high":
        return config.DEEPSEEK_MODEL, "high"
    return config.PFR_NORMAL_MODEL, config.PFR_NORMAL_EFFORT

def _apply_route_identity(plan: Dict[str, Any], route: Dict[str, Any]) -> None:
    """Keep route judgment code-owned across the later planning turn."""

    complexity = str(route.get("complexity") or "normal").strip().lower()
    plan["complexity"] = complexity if complexity in {"normal", "high"} else "normal"
    pr_type = str(route.get("pr_type") or "mixed").strip().lower()
    plan["pr_type"] = pr_type if pr_type in ALLOWED_PR_TYPES else "mixed"
    risk_domains = route.get("risk_domains")
    plan["risk_domains"] = (
        list(risk_domains)[:8] if isinstance(risk_domains, list) else []
    )
    route_meta = route.get("_route_plan_meta")
    if isinstance(route_meta, dict):
        plan["_route_plan_meta"] = dict(route_meta)

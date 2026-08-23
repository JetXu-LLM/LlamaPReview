"""Plan -> Fetch -> Reconcile context workflow orchestration."""

from __future__ import annotations

import json
import logging
import re
import time

from ... import config
from ..initialization import initialize_collection
from ..assembler import (
    assemble_reconcile_context,
    assemble_review_context,
    context_meta,
)
from ..code_extractor import extract_diff_entities, format_diff_entities_block
from ...provider_usage import merge_numeric_usage
from ...deadline import Deadline, DeadlineExceeded
from ...deepseek_client import DeepSeekClient
from ..packing import ContextSection, pack_sections, truncate_preserving_current_ci
from ..repo_structure import RepoInventory
from string import Template
from ..tools import ToolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple
from .common import PFRReconcileFailure, _message_content, _parse_json_object, _pfr_model_phase, _require_complete_response, _truncate
from .prompts import PFR_SYSTEM_PROMPT, PLAN_CONTINUATION_PROMPT, PLAN_PROMPT
from .hints import build_repo_fact_sheet, format_unique_suffix_path_hints, read_owner_docs
from .plan_contract import _apply_route_identity, _apply_shared_plan_tool_contract, _fallback_plan, _normalize_author_acceptance_criteria, _normalize_plan_schema, _plan_model_selection, _plan_question_cap, _planned_steps, _postprocess_planned_search_steps, _register_steps, _sync_plan_from_steps
from .evidence_execution import _address_large_read_steps, _cap_planned_steps, _execute_steps, _fetch_health, _followup_steps, _ordered_steps, _record_terminal_unexecuted_followups, _terminal_evidence_read
from .reconcile_contract import (
    _apply_reconcile_to_ledger,
    _strip_reconcile_extra_fields,
)
from . import evidence_execution, reconcile_contract

logger = logging.getLogger(__name__)


_ROUTE_COMMITMENT_FIELDS = (
    "reviewable_semantic_delta",
    "minimum_evidence_boundary",
    "reason",
    "complexity",
    "pr_type",
    "risk_domains",
)


def _fixed_route_commitment(route: Dict[str, Any]) -> str:
    """Serialize only the already-validated semantic Route contract."""

    commitment = {
        key: route[key]
        for key in _ROUTE_COMMITMENT_FIELDS
        if key in route
    }
    serialized = json.dumps(
        commitment,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # Route values remain model-authored data. Keep them inside the fixed block
    # even if an untrusted PR caused the model to echo delimiter-shaped text.
    return serialized.replace("<", "\\u003c").replace(">", "\\u003e")


def _append_pfr_sections(
    context: str,
    *,
    repo_facts: str,
    owner_docs: str,
    plan: Dict[str, Any],
    reconcile: Dict[str, Any],
    max_chars: int,
    pfr_reserve: int,
) -> str:
    parts = ["## PFR Review Context"]
    parts.append("### Repo Fact Sheet\n" + repo_facts)
    if owner_docs and not owner_docs.startswith("No owner"):
        parts.append(
            "### Repository Review Guidance (untrusted evidence)\n"
            + _truncate(owner_docs, 6000)
        )
    if plan:
        author_criteria = [
            item
            for item in plan.get("author_acceptance_criteria") or []
            if isinstance(item, dict)
            and isinstance(item.get("criterion"), str)
            and item.get("criterion", "").strip()
        ]
        if author_criteria:
            parts.append(
                "### Author Acceptance Criteria\n"
                + "\n".join(
                    "- " + str(item.get("criterion") or "")
                    for item in author_criteria[:8]
                )
            )
    acquisition_notes = [
            "The bounded retrieval plan is not the review scope. Independently "
            "review the raw changed behavior and do not treat an unasked "
            "question as evidence of safety."
    ]
    parts.append(
        "### Evidence Acquisition Coverage\n"
        + "\n".join(f"- {line}" for line in acquisition_notes)
    )
    if reconcile:
        parts.append("### Reconcile Summary\n" + _truncate(reconcile.get("summary") or "", 3000))
        unknowns = [
            item
            for item in reconcile.get("unresolved_gaps") or []
            if isinstance(item, dict) and item.get("user_visible") is not False
        ]
        if unknowns:
            parts.append(
                "### Unresolved Evidence Gaps\n"
                + "\n".join(
                    f"- {item.get('claim')} Check: {item.get('how_to_check')}"
                    for item in unknowns[:6]
                )
            )
    pfr_text = "\n\n".join(parts).strip()
    rendered = pack_sections(
        [
            ContextSection("base_context", context.rstrip(), priority=0, required=True, min_chars=max(0, max_chars - pfr_reserve)),
            ContextSection("pfr_context", pfr_text, priority=0, required=True, min_chars=min(pfr_reserve, len(pfr_text))),
        ],
        max_chars,
    ).rstrip()
    return rendered if len(rendered) >= max_chars else rendered + "\n"

def collect_context_pfr(
    *,
    runtime: Any,
    github_token: str,
    repo_full_name: str,
    pr_content: Dict[str, Any],
    pr_details: str,
    head_sha: str,
    default_branch: str,
    client: Optional[DeepSeekClient] = None,
    trace_metadata: Optional[Dict[str, Any]] = None,
    model: str = config.PFR_MODEL,
    reasoning_effort: str = config.PFR_EFFORT,
    time_budget: int = config.PFR_HIGH_TIME_BUDGET_SECONDS,
    token_budget: int = config.PFR_HIGH_TOKEN_BUDGET,
    max_tool_rounds: int = config.PFR_HIGH_MAX_TOOL_ROUNDS,
    max_search_calls: int = config.PFR_MAX_SEARCH_CALLS,
    max_read_calls: int = config.PFR_MAX_READ_CALLS,
    max_context_chars: int = config.PFR_HIGH_MAX_CONTEXT_CHARS,
    soft_time_budget: Optional[float] = None,
    repo_fact_sheet: str = "",
    route_plan: Optional[Dict[str, Any]] = None,
    deadline: Optional[Deadline] = None,
    repo_inventory: Optional[RepoInventory] = None,
    initial_evidence_ledger: Optional[Dict[str, Any]] = None,
    before_first_reconcile: Optional[Callable[[], Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    state = initialize_collection(
        runtime=runtime,
        github_token=github_token,
        repo_full_name=repo_full_name,
        pr_content=pr_content,
        pr_details=pr_details,
        head_sha=head_sha,
        default_branch=default_branch,
        time_budget=time_budget,
        token_budget=token_budget,
        max_tool_rounds=max_tool_rounds,
        max_search_calls=max_search_calls,
        max_read_calls=max_read_calls,
        max_context_chars=max_context_chars,
        deadline=deadline,
        repo_inventory=repo_inventory,
        initial_evidence_ledger=initial_evidence_ledger,
    )
    client = client or DeepSeekClient(model=model, reasoning_effort=reasoning_effort)
    state.deadline = deadline
    trace_metadata = dict(trace_metadata or {})
    if soft_time_budget is None:
        soft_time_budget = (
            config.PFR_NORMAL_SOFT_TIME_BUDGET_SECONDS
            if time_budget <= config.PFR_NORMAL_TIME_BUDGET_SECONDS
            else config.PFR_HIGH_SOFT_TIME_BUDGET_SECONDS
        )
    state.soft_budget_seconds = float(soft_time_budget or 0)
    if deadline is not None:
        state.soft_budget_seconds = min(state.soft_budget_seconds, max(0.0, deadline.remaining_seconds()))
    repo_facts = repo_fact_sheet or build_repo_fact_sheet(state.accessible_files, state.repo_inventory)
    owner_docs = read_owner_docs(state)
    entities = extract_diff_entities(pr_content)
    plan: Dict[str, Any]
    pfr_plan_status = "model_ok"
    pfr_plan_source = "model"
    pfr_plan_failure_kind = ""
    pfr_plan_schema_warnings: List[str] = []
    pfr_query_postprocess: List[str] = []
    pfr_route_usage: Dict[str, Any] = {}
    pfr_plan_usage: Dict[str, Any] = {}
    pfr_plan_finish_reason = ""
    pfr_plan_elapsed_seconds = 0.0
    route = dict(route_plan or {})
    route_commitment = _fixed_route_commitment(route)
    route_meta = (
        dict(route.get("_route_plan_meta") or {})
        if isinstance(route.get("_route_plan_meta"), dict)
        else {}
    )
    pfr_route_usage = dict(route_meta.get("usage") or {})
    state.add_usage(pfr_route_usage)
    plan_question_cap = _plan_question_cap(route)
    plan_model, plan_effort = _plan_model_selection(route)
    # Inventory has been constructed by initialize_collection above. Import at this
    # continuation boundary because Analyzer itself uses context-engine evidence
    # types; an eager reverse import would make a direct Analyzer import depend
    # on whichever module happened to load first.
    from ...review.analyzer import (
        consume_route_conversation,
        derive_unique_suffix_path_candidates,
    )

    # Consume the process-local Route prefix immediately before the Plan turn.
    route_conversation = consume_route_conversation(client)
    unique_suffix_path_candidates = derive_unique_suffix_path_candidates(
        [
            item
            for item in pr_content.get("file_changes") or []
            if isinstance(item, dict)
        ],
        state.repo_inventory,
    )
    route_plan_lineage: Dict[str, Any] = {
        "contract": "inventory_preflight_route_plan_v3",
        "route_present": route_plan is not None,
        "plan_after_inventory": True,
        "same_conversation_prefix_used": bool(route_conversation),
        "max_questions": plan_question_cap,
        "plan_model_tier": (
            "pro" if plan_model == config.DEEPSEEK_MODEL else "flash"
        ),
        "plan_reasoning_effort": plan_effort,
    }
    pfr_reconcile_usages: List[Dict[str, Any]] = []
    pfr_reconcile_representation_repairs: List[Dict[str, Any]] = []
    pfr_reconcile_failures: List[Dict[str, Any]] = []
    pfr_reconcile_finish_reasons: List[Dict[str, Any]] = []
    pfr_reconcile_representation_normalizations: List[
        Dict[str, Any]
    ] = []
    pfr_reconcile_model_phases: List[Dict[str, Any]] = []
    pfr_reconcile_representation_repair_consumed = False
    pfr_terminal_reconcile_available = False
    pfr_terminal_reconcile_failure_kind = ""
    pfr_direct_evidence_only = False
    pfr_evidence_index_event_count = 0
    pfr_evidence_index_complete = True
    pfr_evidence_binding_failure_count = 0
    try:
        if route_conversation:
            plan_messages = [
                *route_conversation,
                {
                    "role": "user",
                    "content": Template(PLAN_CONTINUATION_PROMPT).substitute(
                        max_questions=plan_question_cap,
                        route_commitment=route_commitment,
                        pr_details=truncate_preserving_current_ci(
                            pr_details, 120000
                        ),
                        entities=format_diff_entities_block(entities),
                        repo_facts=repo_facts,
                        owner_docs=_truncate(owner_docs, 8000),
                        path_hints=format_unique_suffix_path_hints(
                            unique_suffix_path_candidates
                        ),
                    ),
                },
            ]
            pfr_plan_source = "route_conversation_plan"
        else:
            plan_messages = [
                {"role": "system", "content": PFR_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": Template(PLAN_PROMPT).substitute(
                        max_questions=plan_question_cap,
                        route_commitment=route_commitment,
                        pr_details=truncate_preserving_current_ci(
                            pr_details, 120000
                        ),
                        entities=format_diff_entities_block(entities),
                        repo_facts=repo_facts,
                        owner_docs=_truncate(owner_docs, 8000),
                        path_hints=format_unique_suffix_path_hints(
                            unique_suffix_path_candidates
                        ),
                    ),
                },
            ]
            pfr_plan_source = (
                "post_route_standalone_plan"
                if route_plan is not None
                else "standalone_plan"
            )
        plan_started = time.monotonic()
        plan_response = client.chat(
            plan_messages,
            model=plan_model,
            reasoning_effort=plan_effort,
            thinking=True,
            response_format={"type": "json_object"},
            trace_phase="pfr_plan",
            trace_metadata={
                **trace_metadata,
                "route_plan_lineage": {
                    "same_conversation_prefix_used": bool(
                        route_conversation
                    ),
                    "max_questions": plan_question_cap,
                    "plan_model_tier": (
                        "pro"
                        if plan_model == config.DEEPSEEK_MODEL
                        else "flash"
                    ),
                },
            },
            deadline=deadline,
        )
        pfr_plan_elapsed_seconds = round(
            time.monotonic() - plan_started, 3
        )
        pfr_plan_finish_reason = _require_complete_response(
            plan_response, stage="pfr_plan"
        )
        plan_payload = _parse_json_object(_message_content(plan_response))
        plan = {
            "verification_plan": plan_payload.get("verification_plan")
        }
        if "author_acceptance_criteria" in plan_payload:
            plan["author_acceptance_criteria"] = plan_payload.get(
                "author_acceptance_criteria"
            )
        _apply_route_identity(plan, route)
        pfr_plan_usage = dict(plan_response.get("usage") or {})
        state.add_usage(pfr_plan_usage)
    except DeadlineExceeded:
        raise
    except Exception as exc:
        if getattr(exc, "provider_call_control_failure", False) is True:
            raise
        pfr_plan_failure_kind = reconcile_contract._pfr_failure_kind(exc)
        logger.warning(
            "PFR plan failed; using deterministic fallback plan (kind=%s)",
            pfr_plan_failure_kind,
        )
        pfr_plan_status = "fallback_used"
        pfr_plan_source = "deterministic_fallback"
        plan = _fallback_plan(state, repo_facts)
        _apply_route_identity(plan, route)
        state.known_gaps.append("PFR plan model failed; deterministic fallback plan used.")

    pfr_plan_schema_warnings = _normalize_plan_schema(plan)
    author_acceptance_diagnostics = _normalize_author_acceptance_criteria(
        plan,
        max_items=8,
    )
    pfr_plan_schema_warnings.extend(author_acceptance_diagnostics)
    pfr_plan_schema_warnings.extend(
        f"tool_contract:{warning}"
        for warning in _apply_shared_plan_tool_contract(
            plan,
            state,
            max_items=plan_question_cap,
        )
    )
    executor = ToolExecutor(state, github_token=github_token)
    planned_steps = _ordered_steps(_planned_steps(plan, state))
    pfr_query_postprocess.extend(
        _address_large_read_steps(
            planned_steps,
            state=state,
            entities=entities,
            named_hints=[],
        )
    )
    planned_steps, planned_search_debug = _postprocess_planned_search_steps(
        planned_steps,
        state=state,
        entities=entities,
        named_hints=[],
    )
    pfr_query_postprocess.extend(planned_search_debug)
    if not planned_steps:
        floor = _fallback_plan(state, repo_facts)
        planned_steps = _ordered_steps(_planned_steps(floor, state))
        pfr_query_postprocess.extend(
            _address_large_read_steps(
                planned_steps,
                state=state,
                entities=entities,
                named_hints=[],
            )
        )
        planned_steps, floor_debug = _postprocess_planned_search_steps(
            planned_steps,
            state=state,
            entities=entities,
            named_hints=[],
        )
        pfr_query_postprocess.extend(floor_debug)
        pfr_plan_status = "model_with_deterministic_floor"
    _register_steps(planned_steps, state)
    planned_steps = _cap_planned_steps(
        _ordered_steps(planned_steps),
        state,
        max_steps=plan_question_cap,
    )
    _sync_plan_from_steps(plan, planned_steps)
    _execute_steps(executor, planned_steps, record_skipped_verification=True)

    # Lifecycle policy remains outside retrieval.  This capability boundary is
    # deliberately after the initial exact-head tools and before Reconcile has
    # assembled or dispatched its first model request.  Callback failures must
    # escape so the orchestrator can persist the appropriate terminal state.
    if before_first_reconcile is not None:
        before_first_reconcile()

    reconcile: Dict[str, Any] = {}
    pfr_reconcile_dispatches: List[Dict[str, Any]] = []
    effective_reconcile_rounds = max(
        1,
        min(2, int(config.PFR_MAX_RECONCILE_ROUNDS)),
    )
    terminal_reconcile_round = 1
    terminal_reconcile_trigger = "round_cap_or_failure"
    terminal_tool_event_count = len(state.tool_events)
    terminal_unexecuted_followups: List[Dict[str, Any]] = []
    pfr_sweep_hit_count = 0
    pfr_terminal_evidence_read_count = 0
    pfr_terminal_evidence_outcome = "not_eligible"
    pfr_terminal_reconcile_covers_all_evidence = True
    round_two_trigger = "round_cap_or_failure"
    for round_index in range(effective_reconcile_rounds):
        # Reconcile has a smaller prompt budget than the final context. Reserve
        # the complete code-generated identity index first; diagnostic context
        # remains priority-packed and may be truncated independently.
        (
            context_snapshot,
            evidence_index_envelope,
            evidence_index_meta,
        ) = assemble_reconcile_context(
            state,
            max_chars=180000,
        )
        pfr_evidence_index_event_count = int(
            evidence_index_meta.get("event_count") or 0
        )
        index_complete = evidence_index_meta.get("complete") is True
        pfr_evidence_index_complete = (
            pfr_evidence_index_complete and index_complete
        )
        if not index_complete:
            state.known_gaps.append(
                "PFR evidence identity control plane exceeded the Reconcile "
                "request budget; only direct exact-head evidence remains usable."
            )
            reconcile = {
                "summary": (
                    "Terminal reconciliation was unavailable; only directly "
                    "observed diff, exact-head tool evidence, and exact CI "
                    "diagnostics remain usable."
                ),
                "answered": [],
                "unresolved_gaps": [],
                "followups": [],
                "complete": False,
            }
            pfr_reconcile_failures.append(
                {
                    "round": round_index + 1,
                    "kind": "evidence_index_incomplete",
                    "repair_attempted": False,
                }
            )
            pfr_terminal_reconcile_available = False
            pfr_terminal_reconcile_failure_kind = (
                "evidence_index_incomplete"
            )
            pfr_direct_evidence_only = True
            terminal_reconcile_round = round_index + 1
            terminal_reconcile_trigger = "round_cap_or_failure"
            terminal_tool_event_count = len(state.tool_events)
            break
        try:
            dispatch_started = time.monotonic()
            dispatch_telemetry: Dict[str, Any] = {
                "round": round_index + 1,
                "deadline_remaining_seconds": (
                    round(deadline.remaining_seconds(), 3)
                    if deadline is not None
                    else None
                ),
                "deadline_elapsed_seconds": (
                    round(deadline.elapsed_seconds(), 3)
                    if deadline is not None
                    else None
                ),
            }
            pfr_reconcile_dispatches.append(dispatch_telemetry)
            try:
                reconcile = reconcile_contract._reconcile(
                    client=client,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    pr_details=pr_details,
                    plan=plan,
                    context_text=context_snapshot,
                    evidence_index_envelope=evidence_index_envelope,
                    trace_metadata=trace_metadata,
                    round_index=round_index + 1,
                    allow_representation_repair=(
                        not pfr_reconcile_representation_repair_consumed
                    ),
                    deadline=deadline,
                )
            finally:
                dispatch_telemetry["elapsed_seconds"] = round(
                    time.monotonic() - dispatch_started,
                    3,
                )
            reconcile_usages = reconcile.pop("_model_usages", [])
            reconcile_phases = reconcile.pop("_model_phases", [])
            if isinstance(reconcile_phases, list):
                pfr_reconcile_model_phases.extend(
                    dict(item)
                    for item in reconcile_phases
                    if isinstance(item, dict)
                )
            for reconcile_usage in (
                reconcile_usages if isinstance(reconcile_usages, list) else []
            ):
                if isinstance(reconcile_usage, dict):
                    pfr_reconcile_usages.append(dict(reconcile_usage))
                    state.add_usage(reconcile_usage)
            representation_repair_record = reconcile.pop(
                "_representation_repair", {}
            )
            if isinstance(representation_repair_record, dict):
                representation_repair_record = {
                    "round": round_index + 1,
                    **representation_repair_record,
                }
                pfr_reconcile_representation_repairs.append(
                    representation_repair_record
                )
                if representation_repair_record.get("attempted"):
                    pfr_reconcile_representation_repair_consumed = True
            finish_record = reconcile.pop("_stage_finish_reasons", {})
            if isinstance(finish_record, dict):
                pfr_reconcile_finish_reasons.append(
                    {"round": round_index + 1, **finish_record}
                )
            normalization_record = reconcile.pop(
                "_representation_normalizations", []
            )
            if isinstance(normalization_record, list) and normalization_record:
                for action in normalization_record:
                    match = re.fullmatch(
                        r"followups\.(search_code|read_file|list_dir)"
                        r"\.args_from_top_level:(\d+)",
                        str(action),
                    )
                    if match:
                        for _ in range(int(match.group(2))):
                            state.record_tool_arg_repair(
                                f"{match.group(1)}.args_from_top_level"
                            )
                pfr_reconcile_representation_normalizations.append(
                    {
                        "round": round_index + 1,
                        "repairs": [str(item) for item in normalization_record],
                    }
                )
            reconcile, sanitize_normalizations = (
                _strip_reconcile_extra_fields(reconcile)
            )
            if sanitize_normalizations:
                if (
                    pfr_reconcile_representation_normalizations
                    and pfr_reconcile_representation_normalizations[-1].get(
                        "round"
                    )
                    == round_index + 1
                ):
                    existing_repairs = (
                        pfr_reconcile_representation_normalizations[-1][
                            "repairs"
                        ]
                    )
                    pfr_reconcile_representation_normalizations[-1][
                        "repairs"
                    ] = list(
                        dict.fromkeys(
                            [*existing_repairs, *sanitize_normalizations]
                        )
                    )
                else:
                    pfr_reconcile_representation_normalizations.append(
                        {
                            "round": round_index + 1,
                            "repairs": sanitize_normalizations,
                        }
                    )
            reconcile = _apply_reconcile_to_ledger(
                reconcile,
                state,
                round_index=round_index + 1,
            )
            pfr_evidence_binding_failure_count += int(
                reconcile.pop("_evidence_binding_failure_count", 0) or 0
            )
            pfr_terminal_reconcile_available = True
        except DeadlineExceeded:
            raise
        except PFRReconcileFailure as exc:
            pfr_reconcile_model_phases.extend(exc.model_phases)
            for reconcile_usage in exc.usages:
                if isinstance(reconcile_usage, dict):
                    pfr_reconcile_usages.append(dict(reconcile_usage))
                    state.add_usage(reconcile_usage)
            if exc.repair_telemetry:
                record = {"round": round_index + 1, **exc.repair_telemetry}
                pfr_reconcile_representation_repairs.append(record)
                if record.get("attempted"):
                    pfr_reconcile_representation_repair_consumed = True
            if exc.finish_reasons:
                pfr_reconcile_finish_reasons.append(
                    {"round": round_index + 1, **exc.finish_reasons}
                )
            pfr_reconcile_failures.append(
                {
                    "round": round_index + 1,
                    "kind": exc.kind,
                    "repair_attempted": bool(
                        (exc.repair_telemetry or {}).get("attempted")
                    ),
                }
            )
            logger.warning(
                "PFR reconcile contract failed (kind=%s)",
                exc.kind,
            )
            state.known_gaps.append(
                "PFR reconcile contract failed; review should treat context as unverified."
            )
            reconcile = {
                "summary": (
                    "Terminal reconciliation was unavailable; only directly "
                    "observed diff, exact-head tool evidence, and exact CI "
                    "diagnostics remain usable."
                ),
                "answered": [],
                "unresolved_gaps": [],
                "followups": [],
                "complete": False,
            }
            pfr_terminal_reconcile_available = False
            pfr_terminal_reconcile_failure_kind = exc.kind
            pfr_direct_evidence_only = True
            terminal_reconcile_round = round_index + 1
            terminal_reconcile_trigger = "round_cap_or_failure"
            terminal_tool_event_count = len(state.tool_events)
            break
        except Exception as exc:
            if getattr(exc, "provider_call_control_failure", False) is True:
                raise
            failure_kind = reconcile_contract._pfr_failure_kind(exc)
            logger.warning(
                "PFR reconcile failed (kind=%s)",
                failure_kind,
            )
            state.known_gaps.append("PFR reconcile failed; review should treat context as unverified.")
            reconcile = {
                "summary": (
                    "Terminal reconciliation was unavailable; only directly "
                    "observed diff, exact-head tool evidence, and exact CI "
                    "diagnostics remain usable."
                ),
                "answered": [],
                "unresolved_gaps": [],
                "followups": [],
                "complete": False,
            }
            pfr_terminal_reconcile_available = False
            pfr_terminal_reconcile_failure_kind = failure_kind
            pfr_direct_evidence_only = True
            terminal_reconcile_round = round_index + 1
            terminal_reconcile_trigger = "round_cap_or_failure"
            terminal_tool_event_count = len(state.tool_events)
            break

        followups = _followup_steps(reconcile, round_index, state)
        pfr_query_postprocess.extend(
            _address_large_read_steps(
                followups,
                state=state,
                entities=entities,
                named_hints=[],
            )
        )

        # The configured single-round mode has no later judgment pass. Never
        # collect evidence that no reconciler can consume.
        if round_index + 1 >= effective_reconcile_rounds:
            terminal_reconcile_round = round_index + 1
            terminal_reconcile_trigger = (
                round_two_trigger
                if effective_reconcile_rounds > 1
                else "round_cap_or_failure"
            )
            terminal_step: Optional[Dict[str, Any]] = None
            if effective_reconcile_rounds > 1:
                terminal_step, pfr_terminal_evidence_outcome = (
                    _terminal_evidence_read(
                        executor,
                        reconcile,
                        followups,
                    )
                )
            else:
                pfr_terminal_evidence_outcome = "single_round_mode"
            if terminal_step is not None:
                pfr_terminal_evidence_read_count = 1
                if pfr_terminal_evidence_outcome == "hit":
                    pfr_terminal_reconcile_covers_all_evidence = False
                    pfr_direct_evidence_only = True
                    terminal_reconcile_trigger = "terminal_evidence_read"
                    reconcile["summary"] = (
                        f"{str(reconcile.get('summary') or '').strip()} "
                        "One bounded exact-head read was collected after this "
                        "reconciliation. Downstream judgment must evaluate that "
                        "direct evidence; this reconciliation does not cover it."
                    ).strip()
                else:
                    terminal_reconcile_trigger = "terminal_evidence_attempted"
            remaining_followups = [
                step for step in followups if step is not terminal_step
            ]
            terminal_unexecuted_followups.extend(
                _record_terminal_unexecuted_followups(
                    reconcile,
                    round_index,
                    state,
                    steps=remaining_followups,
                )
            )
            # The terminal boundary is sealed only after the optional evidence
            # read and after every other request is marked unexecuted.
            terminal_tool_event_count = len(state.tool_events)
            break

        # Only round one can reach this branch. One bounded read may cross the
        # soft retrieval budget; round two then reconciles all acquired facts.
        if not reconcile.get("complete") and followups:
            ordered_followups = _ordered_steps(followups)[:3]
            _execute_steps(
                executor,
                ordered_followups,
                terminal_read_rescue=1,
                record_skipped_verification=True,
            )
            if evidence_execution._soft_budget_reached(state):
                evidence_execution._mark_soft_budget_exhausted(state)
                pfr_terminal_reconcile_covers_all_evidence = False
                pfr_direct_evidence_only = True
                terminal_reconcile_round = round_index + 1
                terminal_reconcile_trigger = "round_cap_or_failure"
                terminal_tool_event_count = len(state.tool_events)
                reconcile["summary"] = (
                    f"{str(reconcile.get('summary') or '').strip()} "
                    "Follow-up exact-head evidence was collected after this "
                    "reconciliation. Downstream judgment must evaluate that "
                    "direct evidence; this reconciliation does not cover it."
                ).strip()
                break
            if not state.soft_budget_exhausted:
                pfr_sweep_hit_count += evidence_execution._safety_sweep(
                    state,
                    executor,
                    [*planned_steps, *ordered_followups],
                )
            round_two_trigger = "followup_attempted"
            continue

        sweep_hits = 0
        if not state.soft_budget_exhausted:
            sweep_hits = evidence_execution._safety_sweep(state, executor, planned_steps)
            pfr_sweep_hit_count += sweep_hits
        if sweep_hits:
            round_two_trigger = "sweep_hit"
            continue

        terminal_reconcile_round = 1
        terminal_reconcile_trigger = (
            "initial_complete_no_new_hit"
            if reconcile.get("complete")
            else "round_cap_or_failure"
        )
        terminal_tool_event_count = len(state.tool_events)
        if followups:
            terminal_unexecuted_followups.extend(
                _record_terminal_unexecuted_followups(
                    reconcile,
                    round_index,
                    state,
                )
            )
        break

    pfr_post_terminal_tool_call_count = max(
        0,
        len(state.tool_events) - terminal_tool_event_count,
    )

    state.finished = True
    state.finish_reason = "budget_exhausted" if state.soft_budget_exhausted else ("plan_complete" if reconcile.get("complete") else "cap_reached")
    state.finish_summary = reconcile.get("summary") or "PFR collected planned evidence and assembled context."
    for unknown in reconcile.get("unresolved_gaps") or []:
        if isinstance(unknown, dict) and unknown.get("claim"):
            state.known_gaps.append(str(unknown["claim"]))
    state.evidence_ledger.ensure_terminal_resolutions()
    fetch_health = _fetch_health(state, plan)
    state.fetch_degradation_reason_counts = dict(
        fetch_health.get("degradation_reason_counts") or {}
    )
    if fetch_health["status"] == "partial_or_failed_context":
        state.finish_reason = "partial_or_failed_context"
        state.known_gaps.append(
            "PFR retrieval was incomplete; only answer-eligible evidence may "
            "support review claims."
        )

    pfr_reserve = min(
        state.max_context_chars,
        min(12000, max(2500, state.max_context_chars // 4)),
    )
    context = _append_pfr_sections(
        assemble_review_context(state, reserved_chars=pfr_reserve),
        repo_facts=repo_facts,
        owner_docs=owner_docs,
        plan=plan,
        reconcile=reconcile,
        max_chars=state.max_context_chars,
        pfr_reserve=pfr_reserve,
    )
    pfr_model_phases: List[Dict[str, Any]] = []
    if route_meta:
        pfr_model_phases.append(_pfr_model_phase(
            "route",
            model=config.ANALYZER_MODEL,
            thinking=True,
            reasoning_effort=config.ANALYZER_EFFORT,
            attempt=1,
            elapsed_seconds=float(route_meta.get("initial_elapsed_seconds") or 0),
            finish_reason=str(route_meta.get("initial_finish_reason") or ""),
            usage=route_meta.get("initial_usage") or {},
        ))
    if route_meta.get("adaptive_adjudication_triggered"):
        pfr_model_phases.append(
            _pfr_model_phase(
                "route_adjudication",
                model=config.DEEPSEEK_MODEL,
                thinking=True,
                reasoning_effort=config.DEEPSEEK_EFFORT,
                attempt=2,
                elapsed_seconds=float(
                    route_meta.get("adjudication_elapsed_seconds") or 0
                ),
                finish_reason=str(
                    route_meta.get("adjudication_finish_reason") or ""
                ),
                usage=route_meta.get("adjudication_usage") or {},
            )
        )
    if pfr_plan_source in {"route_conversation_plan", "post_route_standalone_plan", "standalone_plan"}:
        pfr_model_phases.append(
            _pfr_model_phase(
                "pfr_plan",
                model=plan_model,
                thinking=True,
                reasoning_effort=plan_effort,
                attempt=1,
                elapsed_seconds=pfr_plan_elapsed_seconds,
                finish_reason=pfr_plan_finish_reason,
                usage=pfr_plan_usage,
            )
        )
    pfr_model_phases.extend(pfr_reconcile_model_phases)
    author_acceptance_projection = [
        {"criterion": str(item.get("criterion") or "").strip()}
        for item in plan.get("author_acceptance_criteria") or []
        if isinstance(item, dict)
        and str(item.get("criterion") or "").strip()
    ]
    unresolved_gap_projection = [
        {
            "claim": str(item.get("claim") or "").strip(),
            "how_to_check": str(item.get("how_to_check") or "").strip(),
        }
        for item in reconcile.get("unresolved_gaps") or []
        if isinstance(item, dict)
        and item.get("user_visible") is not False
        and str(item.get("claim") or "").strip()
    ]
    fetch_health_projection = {
        key: fetch_health.get(key)
        for key in (
            "status",
            "planned_retrieval_status",
            "planned_question_count",
            "completed_question_count",
            "supporting_question_count",
            "planned_unread_paths",
            "planned_content_unobserved_paths",
            "planned_invalid_read_paths",
            "budget_skipped_verification_paths",
            "degradation_reason_counts",
            "reasons",
        )
    }
    pfr_evidence_coverage = {
        "plan_status": pfr_plan_status,
        "plan_source": pfr_plan_source,
        "reconcile_complete": reconcile.get("complete") is True,
        "terminal_reconcile_available": pfr_terminal_reconcile_available,
        "direct_evidence_only": pfr_direct_evidence_only,
        "evidence_index_complete": pfr_evidence_index_complete,
        "fetch_health": fetch_health_projection,
    }
    meta = context_meta(state)
    meta.update(
        {
            "context_strategy": "pfr",
            "pfr_plan": plan,
            "pfr_plan_status": pfr_plan_status,
            "pfr_plan_source": pfr_plan_source,
            "pfr_plan_failure_kind": pfr_plan_failure_kind,
            "pfr_plan_schema_warnings": pfr_plan_schema_warnings,
            "pfr_route_usage": pfr_route_usage,
            "pfr_model_phases": pfr_model_phases,
            "pfr_usage_total": merge_numeric_usage(
                *(item.get("usage") for item in pfr_model_phases)
            ),
            "pfr_plan_usage": pfr_plan_usage,
            "pfr_plan_finish_reason": pfr_plan_finish_reason,
            "pfr_plan_elapsed_seconds": pfr_plan_elapsed_seconds,
            "route_plan_lineage": route_plan_lineage,
            "unique_suffix_path_hint_count": len(
                unique_suffix_path_candidates
            ),
            "pfr_reconcile_usages": pfr_reconcile_usages,
            "pfr_reconcile_dispatches": pfr_reconcile_dispatches,
            "pfr_reconcile_representation_repairs": (
                pfr_reconcile_representation_repairs
            ),
            "pfr_reconcile_failures": pfr_reconcile_failures,
            "pfr_reconcile_finish_reasons": pfr_reconcile_finish_reasons,
            "pfr_reconcile_model_phases": pfr_reconcile_model_phases,
            "pfr_reconcile_representation_normalizations": (
                pfr_reconcile_representation_normalizations
            ),
            "pfr_author_acceptance_criteria": author_acceptance_projection,
            "pfr_unresolved_gaps": unresolved_gap_projection,
            "pfr_evidence_coverage": pfr_evidence_coverage,
            "pfr_terminal_reconcile_round": terminal_reconcile_round,
            "pfr_terminal_reconcile_trigger": terminal_reconcile_trigger,
            "pfr_post_terminal_tool_call_count": pfr_post_terminal_tool_call_count,
            "pfr_sweep_hit_count": pfr_sweep_hit_count,
            "pfr_terminal_evidence_read_count": (
                pfr_terminal_evidence_read_count
            ),
            "pfr_terminal_evidence_outcome": pfr_terminal_evidence_outcome,
            "pfr_terminal_reconcile_covers_all_evidence": (
                pfr_terminal_reconcile_covers_all_evidence
            ),
            "pfr_terminal_reconcile_available": pfr_terminal_reconcile_available,
            "pfr_terminal_reconcile_failure_kind": pfr_terminal_reconcile_failure_kind,
            "pfr_direct_evidence_only": pfr_direct_evidence_only,
            "pfr_evidence_index_event_count": pfr_evidence_index_event_count,
            "pfr_evidence_index_complete": pfr_evidence_index_complete,
            "pfr_evidence_binding_failure_count": (
                pfr_evidence_binding_failure_count
            ),
            "pfr_fetch_degradation_reason_counts": dict(
                state.fetch_degradation_reason_counts
            ),
            "terminal_unexecuted_followups": terminal_unexecuted_followups,
            "plan_fallback_used": pfr_plan_status == "fallback_used",
            "deterministic_plan_floor_used": pfr_plan_status
            in {
                "reused_with_deterministic_floor",
                "model_with_deterministic_floor",
            },
            "pfr_reconcile": reconcile,
            "pfr_query_postprocess": pfr_query_postprocess,
            "fetch_health": fetch_health,
            "repo_fact_sheet": repo_facts,
            "owner_docs_present": bool(owner_docs and not owner_docs.startswith("No owner")),
            "route_plan_meta": plan.get("_route_plan_meta") if isinstance(plan.get("_route_plan_meta"), dict) else {},
        }
    )
    return context, meta

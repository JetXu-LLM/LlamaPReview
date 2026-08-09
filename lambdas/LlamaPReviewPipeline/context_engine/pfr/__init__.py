"""Plan -> Fetch -> Reconcile context workflow (package facade).

The workflow is split along stable capability seams: Plan contract, evidence
execution, Reconcile contract, and orchestration.
"""

from __future__ import annotations

from .common import (
    PFRReconcileFailure,
    PFRStructuredOutputError,
    _assistant_message,
    _finish_reason,
    _message_content,
    _normalize_json_object_for_contract,
    _parse_json_object,
    _pfr_model_phase,
    _require_complete_response,
    _truncate,
)
from .prompts import (
    PFR_RECONCILE_NEUTRAL_SUMMARY,
    PFR_RECONCILE_REPRESENTATION_REPAIR_CONTRACT,
    PFR_SYSTEM_PROMPT,
    PLAN_CONTINUATION_PROMPT,
    PLAN_METHOD_PROMPT,
    PLAN_PROMPT,
    RECONCILE_PROMPT,
    RECONCILE_SYSTEM_PROMPT,
)
from .hints import (
    build_repo_fact_sheet,
    format_unique_suffix_path_hints,
    read_owner_docs,
)
from .plan_contract import (
    _append_once,
    _apply_route_identity,
    _apply_shared_plan_tool_contract,
    _ensure_question_id,
    _fallback_plan,
    _known_gap_once,
    _normalize_author_acceptance_criteria,
    _normalize_plan_schema,
    _plan_model_selection,
    _plan_question_cap,
    _planned_steps,
    _postprocess_planned_search_steps,
    _record_invalid_planned_read,
    _register_steps,
    _sync_plan_from_steps,
    _valid_read_step,
)
from .evidence_execution import (
    _address_large_read_steps,
    _cap_planned_steps,
    _companion_diff_literals_for_target,
    _diff_literals_for_path,
    _diff_literals_from_change,
    _execute_steps,
    _fetch_health,
    _followup_steps,
    _high_signal_literal_shape,
    _large_read_path_tokens,
    _mark_soft_budget_exhausted,
    _normalized_changed_reference,
    _ordered_steps,
    _planned_read_request_identities,
    _prioritize_steps,
    _read_file_error_count,
    _record_budget_skipped_for_steps,
    _record_budget_skipped_verification,
    _record_terminal_unexecuted_followups,
    _safety_sweep,
    _soft_budget_reached,
    _terminal_evidence_read,
    _tool_call,
    _usable_large_read_literal,
    _verification_step_summary,
)
from .reconcile_contract import (
    _apply_reconcile_to_ledger,
    _explicit_refs_support_question,
    _json_value_exact,
    _normalize_reconcile_contract,
    _pfr_failure_kind,
    _question_text_punctuation_equivalent,
    _reconcile,
    _reconcile_contract_issues,
    _reconcile_question_id,
    _reconcile_truth_contract_changed,
    _strip_reconcile_extra_fields,
    _validate_reconcile_repair_delta,
)
from .orchestration import (
    _append_pfr_sections,
    collect_context_pfr,
)
from ..assembler import assemble_reconcile_context, assemble_review_context

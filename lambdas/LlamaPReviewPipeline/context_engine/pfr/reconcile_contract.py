"""Reconcile-stage contract: sanitization, repair-delta validation, ledger."""

from __future__ import annotations

import json
import re
import time

from ... import config
from ...structured_repair import (
    ContractRepairIssue,
    RepairIssueSelection,
    build_repair_messages,
    build_repair_telemetry,
    select_repair_issues,
)
from ...deadline import Deadline, DeadlineExceeded
from ...deepseek_client import DeepSeekClient, DeepSeekHTTPError, DeepSeekResponseError, DeepSeekTimeoutError, DeepSeekTransportError
from ..evidence import event_supports_answer
from ..packing import truncate_preserving_current_ci
from ..tool_contract import normalize_tool_step_envelope, validate_tool_invocation
from typing import Any, Dict, List, Optional, Tuple
from .common import PFRReconcileFailure, PFRStructuredOutputError, _assistant_message, _finish_reason, _message_content, _normalize_json_object_for_contract, _pfr_model_phase, _require_complete_response
from .prompts import PFR_RECONCILE_NEUTRAL_SUMMARY, PFR_RECONCILE_REPRESENTATION_REPAIR_CONTRACT, RECONCILE_SYSTEM_PROMPT

_PFR_DELETABLE_ITEM_ISSUE_CODES = frozenset(
    {
        "required_field_missing",
        "field_type_invalid",
        "enum_invalid",
        "tool_contract_invalid",
    }
)

def _strip_reconcile_extra_fields(
    reconcile: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Remove schema-external fields without changing contract substance.

    This representation-only projection is safe to apply symmetrically to the
    initial and repaired objects before delta validation.  It deliberately
    preserves malformed roots/items so the strict validator, rather than this
    helper, decides whether content can be repaired.
    """

    if not isinstance(reconcile, dict):
        return {}, []
    allowed_root = {
        "summary",
        "answered",
        "unresolved_gaps",
        "followups",
        "complete",
    }
    sanitized = {
        key: reconcile[key] for key in allowed_root if key in reconcile
    }
    normalizations: List[str] = []
    dropped_root_count = len(set(reconcile) - allowed_root)
    if dropped_root_count:
        normalizations.append(
            f"extra_root_fields_removed:{dropped_root_count}"
        )
    dropped_unknown_field_count = 0
    allowed_unknown = {
        "question_id",
        "claim",
        "how_to_check",
        "evidence_refs",
    }
    raw_unknowns = sanitized.get("unresolved_gaps")
    if isinstance(raw_unknowns, list):
        unknowns = []
        for item in raw_unknowns:
            if not isinstance(item, dict):
                unknowns.append(item)
                continue
            dropped_unknown_field_count += len(set(item) - allowed_unknown)
            unknowns.append(
                {key: item[key] for key in allowed_unknown if key in item}
            )
        sanitized["unresolved_gaps"] = unknowns
    dropped_answered_field_count = 0
    allowed_answered = {
        "question_id",
        "question",
        "evidence_refs",
        "evidence",
    }
    raw_answered = sanitized.get("answered")
    if isinstance(raw_answered, list):
        answered = []
        for item in raw_answered:
            if not isinstance(item, dict):
                answered.append(item)
                continue
            dropped_answered_field_count += len(set(item) - allowed_answered)
            answered.append(
                {key: item[key] for key in allowed_answered if key in item}
            )
        sanitized["answered"] = answered
    dropped_followup_field_count = 0
    allowed_followup = {
        "question",
        "tool",
        "args",
    }
    raw_followups = sanitized.get("followups")
    if isinstance(raw_followups, list):
        followups = []
        for item in raw_followups:
            if not isinstance(item, dict):
                followups.append(item)
                continue
            dropped_followup_field_count += len(set(item) - allowed_followup)
            followups.append(
                {key: item[key] for key in allowed_followup if key in item}
            )
        sanitized["followups"] = followups
    for label, count in (
        ("answered", dropped_answered_field_count),
        ("unresolved_gap", dropped_unknown_field_count),
        ("followup", dropped_followup_field_count),
    ):
        if count:
            normalizations.append(f"extra_{label}_fields_removed:{count}")
    return sanitized, normalizations

def _normalize_reconcile_contract(reconcile: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Apply only unambiguous representation repairs before model repair."""
    normalized = dict(reconcile)
    repairs: List[str] = []
    truth_contracted = False
    summary = normalized.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        normalized["summary"] = PFR_RECONCILE_NEUTRAL_SUMMARY
        repairs.append("summary:invalid_neutral_sentinel")
    complete = normalized.get("complete")
    if isinstance(complete, str) and complete.strip().lower() in {"true", "false"}:
        normalized["complete"] = complete.strip().lower() == "true"
        repairs.append("complete:string_boolean")
    elif type(complete) is not bool:
        normalized["complete"] = False
        repairs.append("complete:invalid_conservative_false")
        truth_contracted = True

    raw_unknowns = normalized.get("unresolved_gaps")
    unknowns = []
    for item in raw_unknowns if isinstance(raw_unknowns, list) else []:
        if not isinstance(item, dict):
            unknowns.append(item)
            continue
        copied = dict(item)
        raw_refs = copied.get("evidence_refs")
        if isinstance(raw_refs, str):
            raw_refs = [raw_refs]
            repairs.append("unresolved_gaps.evidence_refs:string_array")
        elif not isinstance(raw_refs, list):
            if "evidence_refs" in copied:
                repairs.append(
                    "unresolved_gaps.evidence_refs:invalid_container_cleared"
                )
            raw_refs = []
        event_refs = [
            ref
            for ref in raw_refs
            if isinstance(ref, str) and ref.startswith("ev_")
        ]
        if len(event_refs) != len(raw_refs):
            repairs.append(
                "unresolved_gaps.evidence_refs:ordinary_non_event_refs_removed"
            )
        copied["evidence_refs"] = event_refs
        unknowns.append(copied)
    if isinstance(raw_unknowns, list):
        normalized["unresolved_gaps"] = unknowns

    raw_answered = normalized.get("answered")
    answered = []
    for item in raw_answered if isinstance(raw_answered, list) else []:
        if not isinstance(item, dict):
            answered.append(item)
            continue
        copied = dict(item)
        for key in ("question_id", "question"):
            if key in copied and not isinstance(copied.get(key), str):
                copied.pop(key, None)
                repairs.append(f"answered.{key}:invalid_removed")
                truth_contracted = True
        raw_refs = copied.get("evidence_refs")
        if isinstance(raw_refs, str):
            copied["evidence_refs"] = [raw_refs]
            repairs.append("answered.evidence_refs:string_array")
        elif isinstance(raw_refs, list):
            if any(not isinstance(ref, str) for ref in raw_refs):
                copied["evidence_refs"] = []
                repairs.append("answered.evidence_refs:invalid_items_cleared")
                truth_contracted = True
            else:
                copied["evidence_refs"] = list(raw_refs)
        elif "evidence_refs" in copied:
            copied["evidence_refs"] = []
            repairs.append("answered.evidence_refs:invalid_container_removed")
            truth_contracted = True
        answered.append(copied)
    if isinstance(raw_answered, list):
        normalized["answered"] = answered

    raw_followups = normalized.get("followups")
    followups = []
    promoted_followup_args: Dict[str, int] = {}
    for item in raw_followups if isinstance(raw_followups, list) else []:
        if not isinstance(item, dict):
            followups.append(item)
            continue
        envelope = normalize_tool_step_envelope(item)
        copied = dict(envelope.step if envelope.valid else item)
        if envelope.action:
            promoted_followup_args[envelope.action] = (
                promoted_followup_args.get(envelope.action, 0) + 1
            )
        elif not envelope.valid:
            # Preserve a strict failure without retaining unknown or conflicting
            # top-level values. The malformed follow-up may be deleted by the
            # existing bounded repair contract, but can never execute.
            copied["args"] = None
            repairs.extend(
                f"followups.tool_envelope_invalid:{reason}"
                for reason in envelope.reasons
            )
        args = copied.get("args")
        question = copied.get("question")
        if (
            isinstance(args, dict)
            and not str(args.get("reason") or "").strip()
            and isinstance(question, str)
            and question.strip()
        ):
            copied["args"] = {**args, "reason": question.strip()}
            repairs.append("followups.args.reason:from_question")
        followups.append(copied)
    if isinstance(raw_followups, list):
        normalized["followups"] = followups
    repairs.extend(
        f"followups.{action}:{count}"
        for action, count in sorted(promoted_followup_args.items())
    )

    if truth_contracted:
        if normalized.get("summary") != PFR_RECONCILE_NEUTRAL_SUMMARY:
            normalized["summary"] = PFR_RECONCILE_NEUTRAL_SUMMARY
            repairs.append("summary:truth_contraction_neutral")
        if normalized.get("complete") is not False:
            normalized["complete"] = False
            repairs.append("complete:truth_contraction_false")
    return normalized, list(dict.fromkeys(repairs))

def _reconcile_truth_contract_changed(
    original: Dict[str, Any],
    repaired: Dict[str, Any],
) -> bool:
    if not _json_value_exact(
        original.get("complete"), repaired.get("complete")
    ):
        return True
    return any(
        not _json_value_exact(original.get(key), repaired.get(key))
        for key in ("answered", "unresolved_gaps", "followups")
    )

def _reconcile_contract_issues(
    reconcile: Dict[str, Any],
) -> List[ContractRepairIssue]:
    issues: List[ContractRepairIssue] = []

    def add(code: str, location: str, message: str) -> None:
        repair_action, priority = _pfr_representation_repair_policy(
            code, location
        )
        issues.append(
            ContractRepairIssue(
                code=code,
                location=location,
                message=message,
                repair_action=repair_action,
                priority=priority,
            )
        )

    required = (
        "summary",
        "answered",
        "unresolved_gaps",
        "followups",
        "complete",
    )
    for key in required:
        if key not in reconcile:
            add(
                "required_field_missing",
                f"$.{key}",
                "Restore this required reconcile field using only the preceding output.",
            )

    if not isinstance(reconcile.get("summary"), str) or not reconcile.get(
        "summary", ""
    ).strip():
        add(
            "field_type_invalid",
            "$.summary",
            "summary must be a non-empty JSON string.",
        )
    for key in ("answered", "unresolved_gaps", "followups"):
        if not isinstance(reconcile.get(key), list):
            add(
                "field_type_invalid",
                f"$.{key}",
                f"{key} must be a JSON array.",
            )
    if type(reconcile.get("complete")) is not bool:
        add(
            "field_type_invalid",
            "$.complete",
            "complete must be a JSON boolean.",
        )

    if isinstance(reconcile.get("answered"), list):
        for index, item in enumerate(reconcile["answered"]):
            if not isinstance(item, dict):
                add(
                    "field_type_invalid",
                    f"$.answered[{index}]",
                    "Every answered item must be a JSON object.",
                )
                continue
            if not isinstance(item.get("evidence"), str) or not item.get(
                "evidence", ""
            ).strip():
                add(
                    "required_field_missing",
                    f"$.answered[{index}].evidence",
                    "Delete any answered item that lacks its original non-empty evidence summary; do not invent evidence text.",
                )

    if isinstance(reconcile.get("unresolved_gaps"), list):
        for index, item in enumerate(reconcile["unresolved_gaps"]):
            if not isinstance(item, dict):
                add(
                    "field_type_invalid",
                    f"$.unresolved_gaps[{index}]",
                    "Every unresolved gap must be a JSON object.",
                )
                continue
            for key in ("claim", "how_to_check"):
                if not isinstance(item.get(key), str) or not item.get(key, "").strip():
                    add(
                        "required_field_missing",
                        f"$.unresolved_gaps[{index}].{key}",
                        f"Delete any unresolved gap that lacks its original non-empty {key} string; do not invent text.",
                    )
            if "provenance_kind" in item:
                add(
                    "enum_invalid",
                    f"$.unresolved_gaps[{index}].provenance_kind",
                    "PFR unresolved gaps must not contain provenance_kind.",
                )
            if "evidence_refs" in item:
                refs = item.get("evidence_refs")
                if (
                    not isinstance(refs, list)
                    or any(
                        not isinstance(ref, str)
                        or not ref.startswith("ev_")
                        for ref in refs
                    )
                ):
                    add(
                        "field_type_invalid",
                        f"$.unresolved_gaps[{index}].evidence_refs",
                        "PFR unknown evidence_refs must be an array of ev_* event IDs.",
                    )

    if isinstance(reconcile.get("followups"), list):
        for index, item in enumerate(reconcile["followups"]):
            if not isinstance(item, dict):
                add(
                    "field_type_invalid",
                    f"$.followups[{index}]",
                    "Every followup must be a JSON object.",
                )
                continue
            if not isinstance(item.get("question"), str) or not item.get(
                "question", ""
            ).strip():
                add(
                    "required_field_missing",
                    f"$.followups[{index}].question",
                    "Delete any followup that lacks its original non-empty question; do not invent a question.",
                )
            if item.get("tool") not in {"search_code", "read_file", "list_dir"}:
                add(
                    "enum_invalid",
                    f"$.followups[{index}].tool",
                    "Delete a followup whose original tool is invalid; do not substitute a different tool.",
                )
            if not isinstance(item.get("args"), dict):
                add(
                    "field_type_invalid",
                    f"$.followups[{index}].args",
                    "Delete a followup whose original args are not one JSON object; do not invent or rewrite tool arguments.",
                )
            else:
                checked = validate_tool_invocation(item.get("tool"), item.get("args"))
                reason_missing = not str(
                    item.get("args", {}).get("reason") or ""
                ).strip()
                if not checked.valid or reason_missing:
                    add(
                        "tool_contract_invalid",
                        f"$.followups[{index}].args",
                        "Delete a followup whose literal tool request is malformed; do not invent or broaden a replacement request.",
                    )
    if reconcile.get("complete") is True and reconcile.get("followups"):
        add(
            "cross_field_invariant",
            "$.complete",
            "complete=true cannot retain followups.",
        )
    return issues


def _drop_invalid_followup_tool_requests(
    reconcile: Dict[str, Any],
    issues: List[ContractRepairIssue],
) -> Tuple[Dict[str, Any], List[str]]:
    """Delete only validator-identified, non-executable follow-up requests.

    Tool shape is code-owned and the issue location identifies the exact array
    item. A model continuation adds no semantic authority here and can
    accidentally delete a valid sibling. Preserve every unselected item,
    neutralize the stale summary and keep the lifecycle explicitly incomplete.
    """

    followups = reconcile.get("followups")
    if not isinstance(followups, list):
        return reconcile, []
    invalid_indexes = {
        int(match.group(1))
        for issue in issues
        if issue.code == "tool_contract_invalid"
        and (
            match := re.fullmatch(
                r"\$\.followups\[(\d+)\]\.args",
                issue.location,
            )
        )
        and int(match.group(1)) < len(followups)
    }
    if not invalid_indexes:
        return reconcile, []

    projected = dict(reconcile)
    projected["followups"] = [
        item
        for index, item in enumerate(followups)
        if index not in invalid_indexes
    ]
    projected["summary"] = PFR_RECONCILE_NEUTRAL_SUMMARY
    projected["complete"] = False
    return projected, [
        "followups.tool_contract_invalid_dropped:"
        + ",".join(str(index) for index in sorted(invalid_indexes)),
        "summary:truth_contraction_neutral",
        "complete:truth_contraction_false",
    ]


def _pfr_representation_repair_policy(
    code: str, location: str
) -> Tuple[str, int]:
    if code == "json_syntax_invalid":
        return "repair_json_syntax", 10
    if code == "json_root_type_invalid":
        return "ineligible", 0
    if code == "cross_field_invariant":
        return "set_complete_false", 8
    if location.startswith("$.answered["):
        if location.endswith(".evidence_refs"):
            return "fill_empty_array_or_delete_item", 15
        # Missing/non-object answer substance cannot be reconstructed by a
        # format-only continuation. Deleting it would erase a claimed verified
        # question without a terminal unknown.
        return "ineligible", 0
    if location.startswith("$.unresolved_gaps["):
        # Gap text or shape is factual unresolved state, not disposable
        # serializer structure. Every malformed gap must fail closed.
        return "ineligible", 0
    if location.startswith("$.followups["):
        return "delete_item", 10
    if code == "required_field_missing":
        if location == "$.summary":
            return "preserve_code_owned_neutral_summary", 15
        if location == "$.unresolved_gaps":
            return "ineligible", 0
        if location in {"$.answered", "$.followups"}:
            return "set_empty_array_and_complete_false", 12
        return "fill_conservative_structure", 20
    if code == "field_type_invalid":
        if location == "$.unresolved_gaps":
            return "ineligible", 0
        if location in {"$.answered", "$.followups"}:
            return "set_empty_array_and_complete_false", 12
        return "fill_conservative_structure", 25
    if code == "enum_invalid":
        return "delete_item", 15
    return "repair_contract_conservatively", 50

def _json_value_exact(left: Any, right: Any) -> bool:
    """Compare parsed JSON without Python's bool/int numeric coercion."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_value_exact(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_value_exact(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right

def _validate_reconcile_repair_delta(
    original: Optional[Dict[str, Any]],
    repaired: Dict[str, Any],
    selection: RepairIssueSelection,
) -> None:
    """Allow only the concrete, code-owned mutations selected for this repair.

    The model receives canonical ``[*]`` locations for a compact prompt, while
    ``selected_occurrences`` retains the validator's exact array indexes for the
    deterministic permission boundary.  Everything outside those occurrences is
    immutable, including item order and otherwise-valid siblings.
    """

    def reject(message: str) -> None:
        raise PFRStructuredOutputError("repair_semantic_expansion", message)

    if original is None or not isinstance(repaired, dict):
        reject("action-scoped repair requires parseable object baselines")
    if selection.omitted_count or selection.ineligible_count or not selection.selected:
        reject("action-scoped repair requires a complete eligible issue selection")

    collections = ("answered", "unresolved_gaps", "followups")
    root_empty: set[str] = set()
    deletable: Dict[str, set[int]] = {key: set() for key in collections}
    replacements: Dict[str, Dict[int, List[Any]]] = {
        key: {} for key in collections
    }
    allow_complete_false = False
    item_location = re.compile(
        r"^\$\.(answered|unresolved_gaps|followups)\[(\d+)\](?:\.([A-Za-z_][A-Za-z0-9_]*))?$"
    )

    for issue, occurrences in zip(
        selection.selected, selection.selected_occurrences
    ):
        for occurrence in occurrences:
            canonical_occurrence = re.sub(r"\[\d+\]", "[*]", occurrence)
            if canonical_occurrence != issue.location:
                reject("repair issue occurrence does not match its canonical location")

            action = issue.repair_action
            if action == "set_empty_array_and_complete_false":
                if issue.code not in {"required_field_missing", "field_type_invalid"}:
                    reject("structural repair action has an unsupported issue code")
                if occurrence not in {"$.answered", "$.followups"}:
                    reject(
                        "root structural repair is allowed only for answered/followups"
                    )
                key = occurrence[2:]
                if isinstance(original.get(key), list):
                    reject("root structural repair cannot replace an existing collection")
                root_empty.add(key)
                continue

            if action == "set_complete_false":
                if issue.code != "cross_field_invariant" or occurrence != "$.complete":
                    reject("complete demotion requires its exact cross-field issue")
                allow_complete_false = True
                continue

            match = item_location.fullmatch(occurrence)
            if match is None:
                reject("repair action has no supported concrete PFR occurrence")
            key, index_text, field = match.groups()
            index = int(index_text)
            before_items = original.get(key)
            if not isinstance(before_items, list) or index >= len(before_items):
                reject("repair issue points outside its original PFR collection")

            if action == "delete_item":
                if issue.code not in _PFR_DELETABLE_ITEM_ISSUE_CODES:
                    reject("item deletion has an unsupported issue code")
                deletable[key].add(index)
                continue

            if action == "fill_empty_array_or_delete_item":
                if key != "answered" or field != "evidence_refs":
                    reject("empty-array repair is outside the answered evidence scope")
                before_item = before_items[index]
                if not isinstance(before_item, dict):
                    reject("empty-array repair requires an original object item")
                replacement = dict(before_item)
                replacement["evidence_refs"] = []
                replacements[key].setdefault(index, []).append(replacement)
                deletable[key].add(index)
                continue

            reject("repair action is not supported by the PFR mutation policy")

    def field_is_exact(key: str) -> bool:
        return (key in original) == (key in repaired) and _json_value_exact(
            original.get(key), repaired.get(key)
        )

    truth_contract_changed = _reconcile_truth_contract_changed(
        original,
        repaired,
    )
    if truth_contract_changed:
        if repaired.get("summary") != PFR_RECONCILE_NEUTRAL_SUMMARY:
            reject(
                "truth-contract repair must use the code-owned neutral summary"
            )
        if repaired.get("complete") is not False:
            reject("truth-contract repair must set complete=false")
    elif not field_is_exact("summary"):
        reject("repair changed an unselected reconcile summary")
    if root_empty:
        if repaired.get("complete") is not False:
            reject(
                "root collection repair must set complete=false because omitted content is unknown"
            )
    elif allow_complete_false:
        if original.get("complete") is not True or repaired.get("complete") is not False:
            reject("cross-field repair may only demote complete from true to false")
    elif not field_is_exact("complete"):
        reject("repair changed complete without its selected cross-field issue")

    for key in collections:
        after_items = repaired.get(key)
        if key in root_empty:
            if key not in repaired or after_items != []:
                reject(f"root structural repair may only set {key} to an empty array")
            continue

        before_items = original.get(key)
        if not isinstance(before_items, list) or not isinstance(after_items, list):
            reject(f"repair changed {key} without a root structural permission")

        # Dynamic programming avoids a duplicate-item ambiguity: a targeted bad
        # item and an identical valid sibling can still be distinguished by their
        # original indexes without allowing reorder, rebinding, or new items.
        reachable_after_indexes = {0}
        for index, before_item in enumerate(before_items):
            next_indexes: set[int] = set()
            for after_index in reachable_after_indexes:
                if (
                    after_index < len(after_items)
                    and _json_value_exact(after_items[after_index], before_item)
                ):
                    next_indexes.add(after_index + 1)
                for replacement in replacements[key].get(index, []):
                    if (
                        after_index < len(after_items)
                        and _json_value_exact(after_items[after_index], replacement)
                    ):
                        next_indexes.add(after_index + 1)
                if index in deletable[key]:
                    next_indexes.add(after_index)
            reachable_after_indexes = next_indexes
        if len(after_items) not in reachable_after_indexes:
            reject(
                f"repair changed, reordered, rebound, or deleted unselected {key} items"
            )

    handled = {"summary", "complete", *collections}
    for key in set(original) | set(repaired):
        if key not in handled and not field_is_exact(key):
            reject("repair changed an unselected top-level field")

def _explicit_refs_support_question(
    raw_refs: Any,
    *,
    question_id: str,
    state,
) -> bool:
    if isinstance(raw_refs, str):
        refs = [raw_refs]
    elif isinstance(raw_refs, list):
        refs = raw_refs
    else:
        return False
    if not refs or any(not isinstance(ref, str) for ref in refs):
        return False
    for ref in refs:
        event = state.evidence_ledger.events.get(ref) or {}
        if (
            event.get("question_id") != question_id
            or not event_supports_answer(
                event,
                expected_head_sha=state.head_sha,
            )
        ):
            return False
    return True

def _question_text_punctuation_equivalent(left: str, right: str) -> bool:
    def key(value: str) -> str:
        normalized = " ".join(str(value or "").strip().lower().split())
        return re.sub(r"[\s?!.,:;]+$", "", normalized)

    return bool(key(left) and key(left) == key(right))

def _reconcile_question_id(item: Dict[str, Any], state) -> str:
    explicit = str(item.get("question_id") or "").strip()
    explicit_question_text = str(item.get("question") or "")
    question_text = explicit_question_text or str(item.get("claim") or "")
    if explicit in state.evidence_ledger.questions:
        # Reconcile answered items normally repeat the ledger question.  An
        # explicit ID is authoritative only when that text is omitted or still
        # names the same normalized question.  Otherwise a misplaced q_* could
        # borrow the wrong question's unique hit when evidence_refs are omitted.
        # Unresolved gaps use a distinct claim rather than a repeated
        # ``question`` field, so their valid explicit ID remains acceptable.
        if (
            not explicit_question_text.strip()
            or explicit
            in state.evidence_ledger.question_ids_for_text(
                explicit_question_text
            )
            or (
                _explicit_refs_support_question(
                    item.get("evidence_refs"),
                    question_id=explicit,
                    state=state,
                )
                and _question_text_punctuation_equivalent(
                    explicit_question_text,
                    str(
                        (
                            state.evidence_ledger.questions.get(explicit)
                            or {}
                        ).get("text")
                        or ""
                    ),
                )
            )
        ):
            return explicit
    raw_refs = item.get("evidence_refs")
    if isinstance(raw_refs, str):
        raw_refs = [raw_refs]
    referenced_question_ids = set()
    for ref in raw_refs if isinstance(raw_refs, list) else []:
        if not isinstance(ref, str):
            continue
        event = state.evidence_ledger.events.get(ref) or {}
        question_id = str(event.get("question_id") or "")
        if (
            event.get("outcome") == "hit"
            and question_id in state.evidence_ledger.questions
        ):
            referenced_question_ids.add(question_id)
    if len(referenced_question_ids) == 1:
        referenced_id = next(iter(referenced_question_ids))
        text_matches = state.evidence_ledger.question_ids_for_text(question_text)
        if not question_text.strip() or referenced_id in text_matches:
            return referenced_id
    return state.evidence_ledger.question_id_for_text(question_text)


def _apply_reconcile_to_ledger(
    reconcile: Dict[str, Any],
    state,
    *,
    round_index: int = 0,
) -> Dict[str, Any]:
    """Bind model reconciliation to deterministic evidence truth."""
    updated = dict(reconcile or {})
    answered: List[Dict[str, Any]] = []
    unknowns = [dict(item) for item in updated.get("unresolved_gaps") or [] if isinstance(item, dict)]
    downgraded_ids = set()
    evidence_binding_degraded = False
    evidence_binding_failure_count = 0

    for raw in updated.get("answered") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        question_id = _reconcile_question_id(item, state)
        if not question_id:
            evidence_binding_degraded = True
            evidence_binding_failure_count += 1
            explicit = str(item.get("question_id") or "").strip()
            if explicit not in state.evidence_ledger.questions:
                explicit = ""
            if explicit:
                downgraded_ids.add(explicit)
            continue
        if "evidence_refs" not in item:
            raw_refs = None
        else:
            raw_refs = item.get("evidence_refs")
            if isinstance(raw_refs, str):
                raw_refs = [raw_refs]
            elif not isinstance(raw_refs, list):
                raw_refs = []
        resolution = state.evidence_ledger.resolve(
            question_id=question_id,
            status="answered",
            evidence_refs=raw_refs,
            conclusion=str(item.get("evidence") or ""),
        )
        if resolution["status"] != "answered":
            evidence_binding_degraded = True
            evidence_binding_failure_count += 1
            downgraded_ids.add(question_id)
            continue
        item["question_id"] = question_id
        item["evidence_refs"] = resolution["evidence_refs"]
        answered.append(item)

    # Exact duplicate answers are harmless serializer noise.  Distinct answers
    # for the same code-owned question are not: selecting first/last would let
    # array order decide evidence truth.  Collapse that question to one visible
    # unknown and let a later review stage reason from the underlying events.
    grouped_answers: Dict[str, List[Dict[str, Any]]] = {}
    for item in answered:
        grouped_answers.setdefault(str(item.get("question_id") or ""), []).append(
            item
        )
    answered = []
    for question_id, items in grouped_answers.items():
        unique: List[Dict[str, Any]] = []
        for item in items:
            if not any(_json_value_exact(item, prior) for prior in unique):
                unique.append(item)
        if len(unique) == 1:
            answered.append(unique[0])
            continue
        evidence_binding_degraded = True
        evidence_binding_failure_count += 1
        downgraded_ids.add(question_id)
        state.evidence_ledger.resolve(
            question_id=question_id,
            status="unknown",
        )

    resolved_unknowns: List[Dict[str, Any]] = []
    used_unknown_question_ids: set[str] = set()
    for unknown_index, raw in enumerate(unknowns):
        item = dict(raw)
        question_id = _reconcile_question_id(item, state)
        if not question_id or question_id in used_unknown_question_ids:
            # Reconcile may preserve a factual verification gap that was not
            # a fetch-plan question. Register it deterministically instead of
            # accepting a model-invented ID such as ``manual_validation``.
            question_id = state.evidence_ledger.register_unresolved_gap(
                source_slot=(
                    f"{state.repo_full_name}:{state.head_sha}:"
                    f"pfr_reconcile:{round_index}:{unknown_index}"
                ),
                question=str(item.get("claim") or "Manual verification."),
                how_to_check=str(item.get("how_to_check") or ""),
            )
        used_unknown_question_ids.add(question_id)
        resolution = state.evidence_ledger.resolve(
            question_id=question_id,
            status="unknown",
            conclusion=str(item.get("claim") or ""),
            how_to_check=str(item.get("how_to_check") or ""),
            provenance_kind="",
            provenance_refs=[],
        )
        item["question_id"] = question_id
        item["resolution_id"] = resolution["id"]
        item.pop("provenance_kind", None)
        item.pop("user_visible", None)
        item.pop("evidence_refs", None)
        downgraded_ids.add(question_id)
        resolved_unknowns.append(item)

    if downgraded_ids:
        answered = [item for item in answered if item.get("question_id") not in downgraded_ids]
    updated["answered"] = answered
    updated["unresolved_gaps"] = resolved_unknowns
    updated["_evidence_binding_failure_count"] = (
        evidence_binding_failure_count
    )
    if evidence_binding_degraded:
        updated["summary"] = PFR_RECONCILE_NEUTRAL_SUMMARY
        updated["complete"] = False
    return updated

def _pfr_failure_kind(error: Exception) -> str:
    if isinstance(error, PFRStructuredOutputError):
        return error.kind
    if isinstance(error, json.JSONDecodeError):
        return "json_syntax_invalid"
    if isinstance(error, DeadlineExceeded):
        return "wall_timeout"
    if isinstance(error, DeepSeekHTTPError):
        return "model_http_error"
    if isinstance(error, DeepSeekTimeoutError):
        return "model_transport_timeout"
    if isinstance(error, DeepSeekTransportError):
        return "model_transport_error"
    if isinstance(error, DeepSeekResponseError):
        return "model_response_error"
    return "unclassified_contract_failure"


def _reconcile(
    *,
    client: DeepSeekClient,
    model: str,
    reasoning_effort: str,
    pr_details: str,
    plan: Dict[str, Any],
    context_text: str,
    trace_metadata: Dict[str, Any],
    round_index: int,
    allow_representation_repair: bool,
    deadline: Optional[Deadline] = None,
    evidence_index_envelope: str = (
        "<CODE_GENERATED_EVIDENCE_EVENT_INDEX>"
        "\n{\"events\":[],\"expected_head_sha\":\"\",\"questions\":[]}\n"
        "</CODE_GENERATED_EVIDENCE_EVENT_INDEX>"
    ),
) -> Dict[str, Any]:
    messages = [
        {
            "role": "system",
            "content": RECONCILE_SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": (
                "<UNTRUSTED_PR_DETAILS>\n"
                + truncate_preserving_current_ci(pr_details, 120000)
                + "\n</UNTRUSTED_PR_DETAILS>\n\n<VERIFICATION_PLAN>\n"
                + json.dumps(plan, ensure_ascii=False)
                + "\n</VERIFICATION_PLAN>\n\n"
                + evidence_index_envelope
                + "\n\n<UNTRUSTED_FETCHED_CONTEXT>\n"
                + context_text
                + "\n</UNTRUSTED_FETCHED_CONTEXT>"
            ),
        },
    ]
    initial_usage: Dict[str, Any] = {}
    initial_finish_reason = ""
    initial_content = ""
    original_for_delta: Optional[Dict[str, Any]] = None
    initial_issues: List[ContractRepairIssue] = []
    initial_selection: Optional[RepairIssueSelection] = None
    normalizations: List[str] = []
    model_phases: List[Dict[str, Any]] = []
    initial_started = time.monotonic()
    try:
        response = client.chat(
            messages,
            model=model,
            reasoning_effort=reasoning_effort,
            thinking=True,
            response_format={"type": "json_object"},
            trace_phase="pfr_reconcile",
            trace_metadata={**trace_metadata, "round": round_index},
            deadline=deadline,
        )
        initial_usage = dict(response.get("usage") or {})
        initial_finish_reason = _finish_reason(response)
        model_phases.append(
            _pfr_model_phase(
                "pfr_reconcile",
                model=model,
                thinking=True,
                reasoning_effort=reasoning_effort,
                attempt=1,
                elapsed_seconds=time.monotonic() - initial_started,
                finish_reason=initial_finish_reason,
                usage=initial_usage,
                round_index=round_index,
            )
        )
        _require_complete_response(response, stage="pfr_reconcile")
        initial_content = _message_content(response)
        parsed, json_normalizations = _normalize_json_object_for_contract(
            initial_content
        )
        normalizations.extend(json_normalizations)
        parsed, representation_normalizations = _normalize_reconcile_contract(parsed)
        normalizations.extend(representation_normalizations)
        parsed, extra_field_normalizations = _strip_reconcile_extra_fields(
            parsed
        )
        normalizations.extend(extra_field_normalizations)
        normalizations = list(dict.fromkeys(normalizations))
        # Compare a repair with the already-approved deterministic
        # representation normalization, not the raw stringly object. This
        # prevents both missing->material upgrades and false rejection of exact
        # "true"/"false" or scalar-ref canonicalization.
        original_for_delta = parsed
        initial_issues = _reconcile_contract_issues(parsed)
        if initial_issues:
            parsed, tool_request_normalizations = (
                _drop_invalid_followup_tool_requests(
                    parsed,
                    initial_issues,
                )
            )
            if tool_request_normalizations:
                normalizations.extend(tool_request_normalizations)
                normalizations = list(dict.fromkeys(normalizations))
                original_for_delta = parsed
                initial_issues = _reconcile_contract_issues(parsed)
        if initial_issues:
            raise PFRStructuredOutputError(
                "schema_validation_error",
                "PFR reconcile output failed its structured contract",
                initial_issues,
            )
    except DeadlineExceeded:
        raise
    except (
        DeepSeekHTTPError,
        DeepSeekResponseError,
        DeepSeekTimeoutError,
        DeepSeekTransportError,
    ) as exc:
        if not model_phases:
            model_phases.append(
                _pfr_model_phase(
                    "pfr_reconcile",
                    model=model,
                    thinking=True,
                    reasoning_effort=reasoning_effort,
                    attempt=1,
                    elapsed_seconds=time.monotonic() - initial_started,
                    finish_reason=initial_finish_reason,
                    usage=initial_usage,
                    round_index=round_index,
                )
            )
        raise PFRReconcileFailure(
            str(exc),
            kind=_pfr_failure_kind(exc),
            usages=[initial_usage] if initial_usage else [],
            finish_reasons=(
                {"pfr_reconcile": initial_finish_reason}
                if initial_finish_reason
                else {}
            ),
            model_phases=model_phases,
        ) from exc
    except json.JSONDecodeError as exc:
        initial_issues = [
            ContractRepairIssue(
                code="json_syntax_invalid",
                location="$",
                message="Return one syntactically valid JSON object.",
                repair_action="repair_json_syntax",
                priority=10,
            )
        ]
        initial_error: Exception = exc
    except PFRStructuredOutputError as exc:
        initial_error = exc
        initial_issues = list(exc.issues or initial_issues)
    else:
        telemetry = build_repair_telemetry(
            stage="pfr_reconcile",
            attempted=False,
            recovered=False,
            trigger_kind=None,
            finish_reason=initial_finish_reason or None,
            initial_usage=initial_usage,
        ).as_dict()
        telemetry["delta_guard_mode"] = "not_applicable"
        parsed["_model_usages"] = [initial_usage]
        parsed["_model_phases"] = model_phases
        parsed["_representation_repair"] = telemetry
        parsed["_representation_normalizations"] = normalizations
        parsed["_stage_finish_reasons"] = {
            "pfr_reconcile": initial_finish_reason
        }
        return parsed

    initial_selection = select_repair_issues(initial_issues)
    initial_issues = list(initial_selection.selected)
    trigger_kind = _pfr_failure_kind(initial_error)
    skip_reason = None
    if original_for_delta is None:
        skip_reason = "representation_baseline_missing"
    elif initial_selection.ineligible_count:
        skip_reason = "contract_not_repairable"
    elif initial_selection.omitted_count:
        skip_reason = "contract_issue_overflow"
    elif not initial_issues:
        skip_reason = "contract_not_repairable"
    if (
        not allow_representation_repair
        or trigger_kind
        not in {
            "json_syntax_invalid",
            "json_root_type_invalid",
            "schema_validation_error",
        }
        or skip_reason is not None
    ):
        telemetry = build_repair_telemetry(
            stage="pfr_reconcile",
            attempted=False,
            recovered=False,
            trigger_kind=trigger_kind,
            issues=initial_issues,
            finish_reason=initial_finish_reason or None,
            initial_usage=initial_usage,
            selection=initial_selection,
        ).as_dict()
        telemetry["delta_guard_mode"] = "not_applicable"
        telemetry["skipped_reason"] = (
            skip_reason
            or (
                "repair_disabled_or_ineligible_failure"
                if not allow_representation_repair
                else "failure_kind_not_repairable"
            )
        )
        raise PFRReconcileFailure(
            str(initial_error),
            kind=trigger_kind,
            usages=[initial_usage],
            finish_reasons={"pfr_reconcile": initial_finish_reason},
            repair_telemetry=telemetry,
            model_phases=model_phases,
        ) from initial_error

    repair_timeout = float(config.PFR_RECONCILE_REPAIR_TIMEOUT_SECONDS)
    if deadline is not None:
        try:
            repair_timeout = deadline.timeout_for(
                config.PFR_RECONCILE_REPAIR_TIMEOUT_SECONDS,
                stage="pfr.reconcile_representation_repair",
                minimum_seconds=1.0,
            )
        except DeadlineExceeded as deadline_exc:
            telemetry = build_repair_telemetry(
                stage="pfr_reconcile",
                attempted=False,
                recovered=False,
                trigger_kind=trigger_kind,
                issues=initial_issues,
                initial_usage=initial_usage,
                selection=initial_selection,
            ).as_dict()
            telemetry["delta_guard_mode"] = "not_applicable"
            telemetry["skipped_reason"] = "insufficient_deadline"
            raise PFRReconcileFailure(
                str(deadline_exc),
                kind="wall_timeout",
                usages=[initial_usage],
                finish_reasons={"pfr_reconcile": initial_finish_reason},
                repair_telemetry=telemetry,
                model_phases=model_phases,
            ) from deadline_exc

    repair_usage: Dict[str, Any] = {}
    repair_finish_reason = ""
    repair_call_attempted = False
    repair_started: Optional[float] = None
    try:
        try:
            repair_messages = build_repair_messages(
                messages,
                _assistant_message(response),
                contract=PFR_RECONCILE_REPRESENTATION_REPAIR_CONTRACT,
                issues=initial_issues,
            )
        except (TypeError, ValueError) as protocol_error:
            raise PFRStructuredOutputError(
                "repair_protocol_error",
                "Could not construct a valid PFR reconcile repair continuation",
            ) from protocol_error
        repair_call_attempted = True
        repair_started = time.monotonic()
        repair_response = client.chat(
            repair_messages,
            model=model,
            reasoning_effort=reasoning_effort,
            thinking=False,
            max_tokens=config.PFR_RECONCILE_REPAIR_MAX_TOKENS or None,
            timeout_seconds=repair_timeout,
            response_format={"type": "json_object"},
            trace_phase="pfr_reconcile_representation_repair",
            trace_metadata={
                **trace_metadata,
                "round": round_index,
                "repair_trigger_kind": trigger_kind,
                "repair_issue_codes": [issue.code for issue in initial_issues],
            },
            deadline=deadline,
        )
        repair_usage = dict(repair_response.get("usage") or {})
        repair_finish_reason = _finish_reason(repair_response)
        model_phases.append(
            _pfr_model_phase(
                "pfr_reconcile_representation_repair",
                model=model,
                thinking=False,
                reasoning_effort=reasoning_effort,
                attempt=2,
                elapsed_seconds=time.monotonic() - repair_started,
                finish_reason=repair_finish_reason,
                usage=repair_usage,
                round_index=round_index,
            )
        )
        _require_complete_response(
            repair_response, stage="pfr_reconcile_representation_repair"
        )
        repair_content = _message_content(repair_response)
        repaired, repair_json_normalizations = _normalize_json_object_for_contract(
            repair_content
        )
        repair_normalizations: List[str] = [
            f"repair_{action}" for action in repair_json_normalizations
        ]
        repaired, representation_normalizations = _normalize_reconcile_contract(
            repaired
        )
        repair_normalizations.extend(representation_normalizations)
        repaired, extra_field_normalizations = _strip_reconcile_extra_fields(
            repaired
        )
        repair_normalizations.extend(extra_field_normalizations)
        if _reconcile_truth_contract_changed(original_for_delta, repaired):
            repaired["summary"] = PFR_RECONCILE_NEUTRAL_SUMMARY
            repaired["complete"] = False
            repair_normalizations.append("summary:truth_contraction_neutral")
            repair_normalizations.append("complete:truth_contraction_false")
        _validate_reconcile_repair_delta(
            original_for_delta,
            repaired,
            initial_selection,
        )
        repair_issues = _reconcile_contract_issues(repaired)
        if repair_issues:
            raise PFRStructuredOutputError(
                "schema_validation_error",
                "Repaired PFR reconcile output still failed its structured contract",
                repair_issues,
            )
    except (
        DeadlineExceeded,
        DeepSeekHTTPError,
        DeepSeekResponseError,
        DeepSeekTimeoutError,
        DeepSeekTransportError,
        json.JSONDecodeError,
        PFRStructuredOutputError,
    ) as repair_error:
        if repair_call_attempted and not any(
            item.get("phase") == "pfr_reconcile_representation_repair"
            and item.get("round") == round_index
            for item in model_phases
        ):
            model_phases.append(
                _pfr_model_phase(
                    "pfr_reconcile_representation_repair",
                    model=model,
                    thinking=False,
                    reasoning_effort=reasoning_effort,
                    attempt=2,
                    elapsed_seconds=(
                        time.monotonic() - repair_started
                        if repair_started is not None
                        else 0.0
                    ),
                    finish_reason=repair_finish_reason,
                    usage=repair_usage,
                    round_index=round_index,
                )
            )
        telemetry = build_repair_telemetry(
            stage="pfr_reconcile",
            attempted=repair_call_attempted,
            recovered=False,
            trigger_kind=trigger_kind,
            issues=initial_issues,
            finish_reason=repair_finish_reason or None,
            initial_usage=initial_usage,
            repair_usage=repair_usage,
            selection=initial_selection,
        ).as_dict()
        telemetry["delta_guard_mode"] = (
            "action_scoped_monotonic"
            if repair_call_attempted and original_for_delta is not None
            else "not_applicable"
        )
        telemetry["repair_failure_kind"] = _pfr_failure_kind(repair_error)
        if not repair_call_attempted:
            telemetry["skipped_reason"] = "invalid_continuation_protocol"
        raise PFRReconcileFailure(
            str(repair_error),
            kind=_pfr_failure_kind(repair_error),
            usages=[initial_usage, repair_usage],
            finish_reasons={
                "pfr_reconcile": initial_finish_reason,
                "pfr_reconcile_representation_repair": repair_finish_reason,
            },
            repair_telemetry=telemetry,
            model_phases=model_phases,
        ) from repair_error

    telemetry = build_repair_telemetry(
        stage="pfr_reconcile",
        attempted=True,
        recovered=True,
        trigger_kind=trigger_kind,
        issues=initial_issues,
        finish_reason=repair_finish_reason,
        initial_usage=initial_usage,
        repair_usage=repair_usage,
        selection=initial_selection,
    ).as_dict()
    telemetry["delta_guard_mode"] = "action_scoped_monotonic"
    repaired["_model_usages"] = [initial_usage, repair_usage]
    repaired["_model_phases"] = model_phases
    repaired["_representation_repair"] = telemetry
    repaired["_representation_normalizations"] = list(
        dict.fromkeys(normalizations + repair_normalizations)
    )
    repaired["_stage_finish_reasons"] = {
        "pfr_reconcile": initial_finish_reason,
        "pfr_reconcile_representation_repair": repair_finish_reason,
    }
    return repaired

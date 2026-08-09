"""Shared typed contract for bounded PFR retrieval requests.

This module is deliberately small: the inventory-aware continuation Plan,
standalone Plan, and Reconcile follow-ups all use the same vocabulary and
validator so a prompt cannot silently describe a tool shape that deterministic
Fetch will reinterpret differently.  The preceding Route turn is semantic only
and intentionally has no tool contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from .repo_structure import normalize_repo_path

RETRIEVAL_TOOLS = frozenset({"search_code", "read_file", "list_dir"})
MAX_READ_FILE_SYMBOLS = 5
SEARCH_INTENTS = frozenset(
    {
        "external_usage",
        "peer_dependency",
        "internal_definition",
        "interface_implementations",
        "removal_cleanup",
        "parameter_adoption",
        "adoption_migration",
    }
)

# GitHub code search accepts a richer language than PFR deliberately exposes.
# PFR queries are literal code fragments only; accepting qualifiers or regexp
# notation here would make model wording and deterministic Fetch disagree.
_SEARCH_QUALIFIER_RE = re.compile(
    r"(?:^|\s)(?:repo|org|user|path|filename|extension|language):", re.IGNORECASE
)
_BOOLEAN_OPERATOR_RE = re.compile(r"(?:^|\s)(?:OR|AND|NOT)(?:\s|$)", re.IGNORECASE)
_LITERAL_IDENTIFIER_RE = re.compile(r"[$A-Za-z_][$A-Za-z0-9_]*")
_GENERIC_ONLY_QUERIES = {
    "array",
    "common",
    "config",
    "data",
    "dict",
    "list",
    "manager",
    "model",
    "object",
    "result",
    "string",
    "util",
    "value",
}
_TOOL_ARG_KEYS = {
    "search_code": frozenset({"query", "reason", "intent"}),
    "read_file": frozenset({"path", "reason", "symbols", "mode"}),
    "list_dir": frozenset({"target_path", "max_depth", "reason"}),
}
_TOOL_STEP_ENVELOPE_KEYS = frozenset(
    {
        "question",
        "why_it_matters",
        "tool",
        "args",
        "id",
        "question_id",
    }
)


@dataclass(frozen=True)
class ToolContractValidation:
    """A normalized retrieval request or a content-free rejection reason."""

    tool: str
    args: Dict[str, Any]
    valid: bool
    reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolStepEnvelopeNormalization:
    """One representation-only normalization of a retrieval step envelope."""

    step: Dict[str, Any]
    valid: bool
    action: str = ""
    reasons: Tuple[str, ...] = ()


def normalize_tool_step_envelope(
    item: Any,
) -> ToolStepEnvelopeNormalization:
    """Move unambiguous flat tool arguments into ``args``.

    The operation is deliberately narrower than tool validation.  It never
    invents a value, changes a value, or chooses between two representations.
    Unknown fields, cross-tool fields, and a simultaneous non-empty ``args``
    object fail closed and remain available to the strict validator.
    """

    if not isinstance(item, Mapping):
        return ToolStepEnvelopeNormalization(
            {},
            False,
            reasons=("tool_step_invalid_container",),
        )
    step = dict(item)
    tool = str(step.get("tool") or "").strip()
    if tool not in RETRIEVAL_TOOLS:
        return ToolStepEnvelopeNormalization(
            step,
            False,
            reasons=("tool_not_allowed",),
        )

    extra_keys = set(step) - _TOOL_STEP_ENVELOPE_KEYS
    if not extra_keys:
        raw_args = step.get("args")
        if raw_args is not None and not isinstance(raw_args, Mapping):
            return ToolStepEnvelopeNormalization(
                step,
                False,
                reasons=("tool_args_invalid_container",),
            )
        return ToolStepEnvelopeNormalization(step, True)

    if not extra_keys.issubset(_TOOL_ARG_KEYS[tool]):
        return ToolStepEnvelopeNormalization(
            step,
            False,
            reasons=("mixed_or_unknown_top_level_tool_args",),
        )

    raw_args = step.get("args")
    if raw_args is not None and not isinstance(raw_args, Mapping):
        return ToolStepEnvelopeNormalization(
            step,
            False,
            reasons=("tool_args_invalid_container",),
        )
    if isinstance(raw_args, Mapping) and raw_args:
        return ToolStepEnvelopeNormalization(
            step,
            False,
            reasons=("conflicting_tool_arg_envelopes",),
        )

    promoted = {key: step[key] for key in step if key in extra_keys}
    for key in extra_keys:
        step.pop(key, None)
    step["args"] = promoted
    return ToolStepEnvelopeNormalization(
        step,
        True,
        action=f"{tool}.args_from_top_level",
    )


def normalize_literal_search_query(value: Any) -> str:
    """Normalize one PFR literal query without widening its meaning."""

    if not isinstance(value, str):
        return ""
    query = value.strip()
    if len(query) >= 2 and query[0] == query[-1] and query[0] in {"'", '"', "`"}:
        query = query[1:-1].strip()
    # Slash-wrapped text conventionally signals regex. Do not convert it to a
    # literal because that would conceal an invalid planner request.
    if len(query) >= 2 and query.startswith("/") and query.endswith("/"):
        return ""
    return " ".join(query.split())


def literal_query_reason(value: Any) -> str:
    """Return a stable rejection kind for a non-literal search query."""

    if isinstance(value, str) and ("\n" in value or "\r" in value):
        return "search_query_multiline_forbidden"
    query = normalize_literal_search_query(value)
    if not query:
        return "search_query_missing_or_regex"
    if len(query) > 240:
        return "search_query_too_long"
    if _SEARCH_QUALIFIER_RE.search(query):
        return "search_query_qualifier_forbidden"
    if _BOOLEAN_OPERATOR_RE.search(query):
        return "search_query_boolean_operator_forbidden"
    # Preserve punctuation-only/version literals such as ``?.`` and
    # ``15.5.19``. Reject only unmistakable regexp operators attached to an
    # identifier; treating every dot or question mark as regex previously
    # discarded useful literal searches.
    if re.search(r"(?:[A-Za-z0-9_$)\]])(?:\.\*|\.\+)", query):
        return "search_query_missing_or_regex"
    identifiers = _LITERAL_IDENTIFIER_RE.findall(query)
    if identifiers and all(
        identifier.casefold() in _GENERIC_ONLY_QUERIES
        for identifier in identifiers
    ):
        return "search_query_generic_only"
    return ""


def literal_identifier_tokens(value: Any) -> Tuple[str, ...]:
    """Return stable non-generic query atoms for deterministic grounding."""

    query = normalize_literal_search_query(value)
    return tuple(
        dict.fromkeys(
            token
            for token in _LITERAL_IDENTIFIER_RE.findall(query)
            if (
                len(token) >= 2
                and token.casefold() not in _GENERIC_ONLY_QUERIES
            )
        )
    )


def validate_tool_invocation(tool: Any, args: Any) -> ToolContractValidation:
    """Validate one plan/follow-up request without inventing missing intent."""

    normalized_tool = str(tool or "").strip()
    raw_args = dict(args) if isinstance(args, Mapping) else {}
    if normalized_tool not in RETRIEVAL_TOOLS:
        return ToolContractValidation(
            normalized_tool,
            {},
            False,
            ("tool_not_allowed",),
        )

    reasons: List[str] = []
    if set(raw_args) - _TOOL_ARG_KEYS[normalized_tool]:
        reasons.append("mixed_or_unknown_tool_args")
    if normalized_tool == "search_code":
        query = normalize_literal_search_query(raw_args.get("query"))
        query_reason = literal_query_reason(raw_args.get("query"))
        if query_reason:
            reasons.append(query_reason)
        reason = raw_args.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reasons.append("search_reason_missing")
        intent = str(raw_args.get("intent") or "").strip().lower()
        if intent and intent not in SEARCH_INTENTS:
            reasons.append("search_intent_invalid")
        normalized = {"query": query, "reason": str(reason or "").strip()}
        if intent:
            normalized["intent"] = intent
        return ToolContractValidation(normalized_tool, normalized, not reasons, tuple(reasons))

    if normalized_tool == "read_file":
        path = raw_args.get("path")
        if not isinstance(path, str) or not path.strip():
            reasons.append("path_missing")
        normalized = {"path": str(path or "").strip().replace("\\", "/")}
        mode = str(raw_args.get("mode") or "content").strip().lower()
        if mode not in {"content", "exact_path_existence"}:
            reasons.append("read_file_mode_invalid")
        normalized["mode"] = mode
        symbols = raw_args.get("symbols")
        if symbols is not None and not isinstance(symbols, list):
            reasons.append("read_file_symbols_invalid")
        elif isinstance(symbols, list):
            if len(symbols) > MAX_READ_FILE_SYMBOLS:
                reasons.append("read_file_symbols_exceed_cap")
            normalized_symbols = [
                str(symbol).strip()
                for symbol in symbols[:MAX_READ_FILE_SYMBOLS]
                if isinstance(symbol, str) and symbol.strip()
            ]
            if len(normalized_symbols) != len(symbols[:MAX_READ_FILE_SYMBOLS]):
                reasons.append("read_file_symbols_invalid")
            if normalized_symbols:
                normalized["symbols"] = normalized_symbols
        if mode == "exact_path_existence" and normalized.get("symbols"):
            reasons.append("exact_path_existence_symbols_forbidden")
        reason = raw_args.get("reason")
        if reason is not None and not isinstance(reason, str):
            reasons.append("read_file_reason_invalid")
        elif isinstance(reason, str) and reason.strip():
            normalized["reason"] = reason.strip()
        return ToolContractValidation(normalized_tool, normalized, not reasons, tuple(reasons))

    # list_dir stays compatible with direct/legacy tool callers.  The higher
    # level plan and Reconcile contracts require a reason and fill the plan
    # reason from why_it_matters before reaching this low-level validator.
    target_path = raw_args.get("target_path", "")
    if target_path is not None and not isinstance(target_path, str):
        reasons.append("target_path_invalid")
    raw_depth = raw_args.get("max_depth", 2)
    try:
        depth = int(raw_depth)
    except (TypeError, ValueError):
        depth = 0
    if depth < 1 or depth > 3:
        reasons.append("list_dir_max_depth_invalid")
    raw_target = str(target_path or "").strip().replace("\\", "/")
    normalized_target = normalize_repo_path(raw_target) if raw_target else ""
    if raw_target and (
        not normalized_target
        or normalized_target != raw_target.strip("/")
    ):
        reasons.append("list_dir_target_path_invalid")
    normalized = {
        "target_path": normalized_target,
        "max_depth": depth,
    }
    reason = raw_args.get("reason")
    if isinstance(reason, str) and reason.strip():
        normalized["reason"] = reason.strip()
    elif reason is not None and not isinstance(reason, str):
        reasons.append("list_dir_reason_invalid")
    return ToolContractValidation(normalized_tool, normalized, not reasons, tuple(reasons))


def validate_verification_plan(
    items: Any,
    *,
    max_items: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Keep only typed, bounded plan entries and return stable diagnostics."""

    accepted: List[Dict[str, Any]] = []
    diagnostics: List[str] = []
    if not isinstance(items, list):
        return accepted, ["verification_plan_invalid_container"]
    for index, item in enumerate(items):
        if len(accepted) >= max_items:
            diagnostics.append(f"verification_plan[{index}]:dropped_cap")
            continue
        if not isinstance(item, Mapping):
            diagnostics.append(f"verification_plan[{index}]:item_invalid")
            continue
        envelope = normalize_tool_step_envelope(item)
        if not envelope.valid:
            diagnostics.append(
                f"verification_plan[{index}]:" + ",".join(envelope.reasons)
            )
            continue
        item = envelope.step
        question = item.get("question")
        why = item.get("why_it_matters")
        if not isinstance(question, str) or not question.strip():
            diagnostics.append(f"verification_plan[{index}]:question_missing")
            continue
        if not isinstance(why, str) or not why.strip():
            diagnostics.append(f"verification_plan[{index}]:why_it_matters_missing")
            continue
        raw_args = dict(item.get("args")) if isinstance(item.get("args"), Mapping) else {}
        if not str(raw_args.get("reason") or "").strip():
            raw_args["reason"] = why.strip()
        checked = validate_tool_invocation(item.get("tool"), raw_args)
        if not checked.valid:
            diagnostics.append(
                f"verification_plan[{index}]:" + ",".join(checked.reasons)
            )
            continue
        args = dict(checked.args)
        normalized_step = {
            "question": question.strip(),
            "why_it_matters": why.strip(),
            "tool": checked.tool,
            "args": args,
        }
        accepted.append(normalized_step)
    return accepted, diagnostics


def shared_tool_contract_prompt() -> str:
    """Prompt fragment shared by continuation Plan, standalone Plan, and Reconcile."""

    contract = """Bounded retrieval tool contract (apply exactly):
- search_code: {\"query\": \"literal code fragment\", \"reason\": \"merge-relevance\", \"intent\": \"optional enum\"}. Allowed intent values: __SEARCH_INTENTS__. Queries are literal substring tokens (space = AND), never regex, Boolean OR/AND/NOT, generic-only terms, wildcard/metachar syntax, or GitHub qualifiers such as path:, filename:, language:, repo:, org:, or user:.
- read_file content mode: {\"path\": \"repo/relative/file\", \"mode\": \"content\", \"reason\": \"why content is needed\", \"symbols\": [\"optional literal anchors\"]}. Omit mode for this default. It requests PR-head file CONTENT. The optional symbols array accepts at most __MAX_READ_FILE_SYMBOLS__ literal anchors for every file; for a file over 50 KiB, every anchor must already be named by the question, reason, diff, or deterministic hints.
- read_file exact-path mode: {\"path\": \"one/exact/repo/path\", \"mode\": \"exact_path_existence\", \"reason\": \"why this exact path state matters\"}. It establishes only whether that one literal PR-head path is present or absent. It cannot establish file contents, a symbol, a directory, or repository-wide absence, and it never accepts symbols.
- list_dir: {\"target_path\": \"optional/subdir\", \"max_depth\": 1|2|3, \"reason\": \"why scoped discovery matters\"}. Directory inventory does not establish file contents.
- Use only one of search_code, read_file, or list_dir. Do not mix argument shapes. Every request must be tied to a named verification question and merge-relevance reason."""
    return (
        contract.replace("__SEARCH_INTENTS__", ", ".join(sorted(SEARCH_INTENTS)))
        .replace("__MAX_READ_FILE_SYMBOLS__", str(MAX_READ_FILE_SYMBOLS))
    )


def literal_query_is_grounded(query: Any, named_texts: Iterable[Any]) -> bool:
    """Require deterministic query enrichment to retain a named literal anchor."""

    tokens = literal_identifier_tokens(query)
    if not tokens:
        return False
    haystack = "\n".join(str(text or "").casefold() for text in named_texts)
    return any(token.casefold() in haystack for token in tokens)

"""Literal-grounded search helpers for deterministic PFR retrieval."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .code_extractor import CodeContextExtractor


INTENTS_ALLOW_MODIFIED = {
    "peer_dependency",
    "internal_definition",
    "parameter_adoption",
    "adoption_migration",
}
INTENTS_PREFER_EXTERNAL = {
    "external_usage",
    "interface_implementations",
    "removal_cleanup",
}
CALLABLE_QUERY_RE = re.compile(r"^\$?[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SearchQueryCandidate:
    """A bounded literal query with replay-stable source identity."""

    args: Dict[str, str]
    origin_kind: str
    origin_index: int
    code_owned_priority: str = ""


def normalize_github_search_query(raw_query: str) -> str:
    if not isinstance(raw_query, str):
        return ""
    query = raw_query.strip()
    if len(query) >= 2 and (
        (
            query[0] == query[-1]
            and query[0] in {"'", '"', "`"}
        )
        or (query.startswith("/") and query.endswith("/"))
    ):
        query = query[1:-1].strip()
    return query


def canonical_search_key(query: str) -> str:
    value = normalize_github_search_query(query)
    return " ".join(value.strip().lower().split())


def reduce_multiword_query_with_intent(
    query: str,
    intent: str = "",
) -> str:
    """Remove exact duplicate atoms without guessing language semantics."""

    if intent != "parameter_adoption":
        return query
    unique: List[str] = []
    seen: Set[str] = set()
    for part in query.split():
        key = part.casefold()
        if key not in seen:
            unique.append(part)
            seen.add(key)
    return " ".join(unique)


def _search_arg(
    query: str,
    *,
    reason: str,
    intent: str,
) -> Dict[str, str]:
    result = {"query": query, "reason": reason}
    if intent:
        result["intent"] = intent
    return result


def _balanced_parentheses(query: str) -> bool:
    depth = 0
    for char in query:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _sanitize_literal_query(query: str) -> Tuple[str, str]:
    """Apply syntax-only normalization without creating query variants."""

    safe = normalize_github_search_query(query)
    if not safe:
        return "", "empty"
    if safe.endswith("(") and CALLABLE_QUERY_RE.match(safe[:-1]):
        return safe, ""
    if not _balanced_parentheses(safe):
        return "", "unbalanced_parentheses"
    simple_call = re.match(
        r"^(\$?[A-Za-z_][A-Za-z0-9_]*)\(\)$",
        safe,
    )
    if simple_call:
        return f"{simple_call.group(1)}(", "empty_call_suffix"
    if re.search(r"[{}|\\]", safe):
        return "", "unsupported_metachar"
    return safe, ""


def postprocess_search_candidates(
    raw_args: List[Dict[str, Any]],
    *,
    entities: Dict[str, Any],
    pr_content: Dict[str, Any],
    max_total: int,
    model_lifecycles: Optional[Dict[int, str]] = None,
) -> Tuple[List[SearchQueryCandidate], List[str]]:
    """Cap, deduplicate, and ground exact literals without language rules."""

    del pr_content
    debug: List[str] = []
    final: List[SearchQueryCandidate] = []
    seen: Set[str] = set()

    def add(
        query: str,
        *,
        reason: str,
        intent: str,
        debug_key: str,
        origin_kind: str,
        origin_index: int,
        code_owned_priority: str = "",
    ) -> bool:
        def mark_model(lifecycle: str) -> None:
            if origin_kind == "model" and model_lifecycles is not None:
                model_lifecycles[origin_index] = lifecycle

        safe, normalization = _sanitize_literal_query(query)
        if not safe:
            mark_model("dropped_invalid")
            debug.append(f"drop_{normalization}:{query!r}")
            return False
        if normalization:
            debug.append(
                f"normalize_{normalization}:{query}->{safe}"
            )
        safe = reduce_multiword_query_with_intent(safe, intent)
        if intent == "parameter_adoption" and len(safe.split()) != 2:
            mark_model("dropped_invalid")
            debug.append(f"drop_degenerate_parameter_adoption:{safe}")
            return False
        key = canonical_search_key(safe)
        if not key or key in seen:
            mark_model("dropped_redundant")
            debug.append(f"dedup:{safe}")
            return False
        seen.add(key)
        if len(final) >= max(0, int(max_total)):
            mark_model("dropped_cap")
            debug.append(f"drop_cap:{origin_kind}:{origin_index}")
            return False
        final.append(
            SearchQueryCandidate(
                args=_search_arg(
                    safe,
                    reason=reason,
                    intent=intent,
                ),
                origin_kind=origin_kind,
                origin_index=origin_index,
                code_owned_priority=code_owned_priority,
            )
        )
        mark_model("kept")
        debug.append(f"{debug_key}:{safe}")
        return True

    removed_symbols = sorted(entities.get("removed_symbols") or set())

    def model_query_covers_symbol(
        item: Dict[str, Any],
        symbol: str,
    ) -> bool:
        safe, _ = _sanitize_literal_query(
            str(item.get("query") or "")
        )
        if not safe:
            return False
        safe = reduce_multiword_query_with_intent(
            safe,
            str(item.get("intent") or "").strip().lower(),
        )
        return canonical_search_key(safe).rstrip(
            "("
        ) == canonical_search_key(symbol).rstrip("(")

    reserved_model_index: Optional[int] = None
    if removed_symbols:
        for model_index, item in enumerate(raw_args):
            if not model_query_covers_symbol(
                item,
                removed_symbols[0],
            ):
                continue
            if add(
                str(item.get("query") or ""),
                reason=str(
                    item.get("reason") or "Find repository usage."
                ),
                intent="removal_cleanup",
                debug_key="add_model_removed_coverage",
                origin_kind="model",
                origin_index=model_index,
                code_owned_priority="diff_removed_symbol_floor",
            ):
                reserved_model_index = model_index
            break
        if reserved_model_index is None:
            symbol = removed_symbols[0]
            add(
                symbol,
                reason=f"Find references to removed symbol {symbol}.",
                intent="removal_cleanup",
                debug_key="add_removed_reserved",
                origin_kind="removed_symbol",
                origin_index=0,
                code_owned_priority="diff_removed_symbol_floor",
            )

    for model_index, item in enumerate(raw_args):
        if model_index == reserved_model_index:
            continue
        add(
            str(item.get("query") or ""),
            reason=str(
                item.get("reason") or "Find repository usage."
            ),
            intent=str(item.get("intent") or "").strip().lower(),
            debug_key="add_model",
            origin_kind="model",
            origin_index=model_index,
        )

    for symbol_index, symbol in enumerate(
        removed_symbols[1:],
        start=1,
    ):
        add(
            symbol,
            reason=f"Find references to removed symbol {symbol}.",
            intent="removal_cleanup",
            debug_key="add_removed",
            origin_kind="removed_symbol",
            origin_index=symbol_index,
        )

    parameter_adoptions = sorted(
        {
            (str(owner), str(parameter))
            for owner, parameter in (
                entities.get("parameter_adoptions") or set()
            )
            if owner
            and parameter
            and str(owner).casefold()
            != str(parameter).casefold()
        },
        key=lambda item: (
            len(item[0]) + len(item[1]),
            item[0],
            item[1],
        ),
    )
    if parameter_adoptions:
        owner, parameter = parameter_adoptions[0]
        add(
            f"{owner} {parameter}",
            reason=(
                f"Check adoption of {parameter} in calls to {owner}."
            ),
            intent="parameter_adoption",
            debug_key="add_parameter_adoption",
            origin_kind="parameter_adoption",
            origin_index=0,
        )

    return final, debug


def postprocess_search_args(
    raw_args: List[Dict[str, Any]],
    *,
    entities: Dict[str, Any],
    pr_content: Dict[str, Any],
    max_total: int,
) -> Tuple[List[Dict[str, str]], List[str]]:
    candidates, debug = postprocess_search_candidates(
        raw_args,
        entities=entities,
        pr_content=pr_content,
        max_total=max_total,
    )
    return [dict(candidate.args) for candidate in candidates], debug


def should_prefer_external_hits(intent: str) -> bool:
    return intent in INTENTS_PREFER_EXTERNAL


def should_allow_modified_hits(intent: str) -> bool:
    return intent in INTENTS_ALLOW_MODIFIED


def snippets_from_search_results(
    results: List[Dict[str, Any]],
    *,
    symbol: str,
    source_label: str,
    max_snippets: int = 5,
) -> List[Dict[str, Any]]:
    extractor = CodeContextExtractor()
    snippets: List[Dict[str, Any]] = []
    for result in results[:max_snippets]:
        content = result.get("content") or ""
        path = (
            result.get("path")
            or result.get("file_path")
            or "unknown"
        )
        lines = content.splitlines()
        candidate_lines = [
            index
            for index, line in enumerate(lines)
            if not symbol or symbol in line
        ]
        line_index = (
            extractor.pick_representative_line(
                candidate_lines,
                lines,
                symbol,
            )
            if candidate_lines
            else 0
        )
        block, start, end = extractor.extract_enclosing_block(
            content,
            line_index,
            symbol,
        )
        if not block:
            block, start, end = extractor.build_line_window(
                content,
                line_index,
                window=4,
            )
        snippets.append(
            {
                "path": path,
                "code": block,
                "start": start,
                "end": end,
                "kind": extractor.classify_snippet_kind(
                    symbol,
                    block,
                    path,
                ),
                "source": source_label,
                "api_index": result.get("index", len(snippets)),
            }
        )
    return snippets

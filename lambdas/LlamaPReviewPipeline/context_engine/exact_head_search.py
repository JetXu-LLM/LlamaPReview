"""Literal repository search with deterministic PR-head admission.

GitHub code search discovers candidates on the repository default branch.
This capability owns the complete boundary from that discovery result to
model-visible evidence: sensitive-path filtering, intent-based candidate
selection, bounded literal relocation at the immutable PR head, and explicit
default-only lineage when relocation cannot be established.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import config
from .code_extractor import CodeContextExtractor
from .repo_structure import is_sensitive_repo_path
from .search_rag import (
    canonical_search_key,
    should_prefer_external_hits,
    snippets_from_search_results,
)
from .state import CollectionState
from .tool_contract import (
    literal_identifier_tokens,
    normalize_literal_search_query,
    validate_tool_invocation,
)
from .tool_result import ToolResult


RepositoryTextReader = Callable[
    [str],
    tuple[str | None, Dict[str, Any], ToolResult | None],
]


class ExactHeadSearch:
    """Search once on the default branch and admit only exact-head evidence."""

    def __init__(
        self,
        state: CollectionState,
        *,
        read_repository_text: RepositoryTextReader,
        extractor: Optional[CodeContextExtractor] = None,
    ) -> None:
        self.state = state
        self._read_repository_text = read_repository_text
        self.extractor = extractor or CodeContextExtractor()

    def _filter_results_for_intent(
        self,
        results: List[Dict[str, Any]],
        intent: str,
    ) -> tuple[List[Dict[str, Any]], List[str]]:
        notes: List[str] = []
        if not results or not should_prefer_external_hits(intent):
            return results, notes
        changed_paths = {
            str(change.get("file_path"))
            for change in self.state.pr_content.get("file_changes") or []
            if isinstance(change, dict) and change.get("file_path")
        }
        if not changed_paths:
            return results, notes
        external = [
            result
            for result in results
            if str(
                result.get("path") or result.get("file_path") or ""
            )
            not in changed_paths
        ]
        excluded = len(results) - len(external)
        if external:
            if excluded:
                notes.append(
                    f"intent={intent}: excluded {excluded} changed-file "
                    "search hits to prioritize unchanged ripple context."
                )
            return external, notes
        if excluded:
            notes.append(
                f"intent={intent}: relaxed modified-file filter because only "
                "changed-file hits were found."
            )
        return results, notes

    def _relocate_at_head(
        self,
        snippet: Dict[str, Any],
        head_content: str,
        query: str,
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Relocate a default-branch hit by literal identity, never line number."""

        path = str(snippet.get("path") or "")
        default_start = max(1, int(snippet.get("start") or 1))
        default_end = max(
            default_start,
            int(snippet.get("end") or default_start),
        )
        tokens = list(literal_identifier_tokens(query))
        if not tokens and query.strip():
            # Exact punctuation/version literals have no identifier token but
            # still support deterministic same-string relocation.
            tokens = [query.strip()]
        lineage: Dict[str, Any] = {
            "path": path,
            "default_start": default_start,
            "default_end": default_end,
            "head_sha": self.state.head_sha,
            "query_token_count": len(tokens),
        }
        if not tokens:
            lineage["outcome"] = "query_has_no_relocatable_literal"
            return None, lineage

        lines = head_content.splitlines()
        folded_lines = [line.casefold() for line in lines]
        positions: Dict[str, List[int]] = {}
        for token in tokens:
            matches = [
                index
                for index, line in enumerate(folded_lines)
                if token.casefold() in line
            ]
            if not matches:
                lineage["outcome"] = "literal_missing_at_head"
                return None, lineage
            positions[token] = matches

        default_index = default_start - 1
        anchor = max(tokens, key=len)
        anchor_index = min(
            positions[anchor],
            key=lambda index: abs(index - default_index),
        )
        selected = {
            token: min(
                matches,
                key=lambda index: abs(index - anchor_index),
            )
            for token, matches in positions.items()
        }
        first_match = min(selected.values())
        last_match = max(selected.values())
        # File-wide AND matches are useful discovery but do not establish one
        # bounded exact-head observation when the atoms are far apart.
        if last_match - first_match > 120:
            lineage["outcome"] = "literals_disjoint_at_head"
            return None, lineage

        if len(tokens) == 1:
            block, start, end = self.extractor.extract_enclosing_block(
                head_content,
                anchor_index,
                anchor,
            )
            if not block:
                block, start, end = self.extractor.build_line_window(
                    head_content,
                    anchor_index,
                )
            match_kind = "single_literal"
        else:
            start_index = max(0, first_match - 4)
            end_index = min(len(lines), last_match + 5)
            block = "\n".join(lines[start_index:end_index])
            start, end = start_index + 1, end_index
            match_kind = "bounded_all_query_literals"

        retained = block[: config.MAX_FILE_SIZE]
        retained_folded = retained.casefold()
        if not all(
            token.casefold() in retained_folded for token in tokens
        ):
            lineage["outcome"] = "head_match_exceeds_bounded_snippet"
            return None, lineage
        lineage.update(
            {
                "outcome": "relocated_at_head",
                "match_kind": match_kind,
                "head_start": start,
                "head_end": end,
            }
        )
        return (
            {
                **snippet,
                "code": retained,
                "start": start,
                "end": end,
                "source": (
                    "[found via default branch search; literal relocated at "
                    f"PR head {self.state.head_sha[:8]}]"
                ),
                "search_lineage": dict(lineage),
                "exact_head_admitted": True,
            },
            lineage,
        )

    def _search_once(
        self,
        query: str,
        *,
        intent: str,
        phase: str,
    ) -> tuple[
        List[str],
        List[Dict[str, Any]],
        List[str],
        str,
        List[Dict[str, Any]],
        str,
    ]:
        query_key = canonical_search_key(query)
        if query_key in self.state.attempted_search_queries:
            return (
                [],
                [],
                [],
                f"`{query}` already searched; call finish_context if this "
                "repeats or only low-signal evidence remains.",
                [],
                "repeat",
            )
        if self.state.search_calls >= self.state.max_search_calls:
            return (
                [],
                [],
                [],
                "search_code quota exhausted; call finish_context if enough "
                "context is available.",
                [],
                "quota_exhausted",
            )
        self.state.attempted_search_queries.add(query_key)
        self.state.search_calls += 1
        try:
            if hasattr(self.state.runtime, "search_code_with_status"):
                payload = self.state.runtime.search_code_with_status(
                    query,
                    self.state.repo_full_name,
                ) or {}
                if isinstance(payload, dict):
                    typed_outcome = str(
                        payload.get("outcome") or ""
                    ).strip().lower()
                    if payload.get("error") or typed_outcome in {
                        "error",
                        "search_error",
                    }:
                        error_text = str(
                            payload.get("error")
                            or "unknown GitHub search error"
                        )
                        status = payload.get("status")
                        status_text = f"HTTP {status}: " if status else ""
                        try:
                            status_code = (
                                int(status)
                                if status is not None
                                else None
                            )
                        except (TypeError, ValueError):
                            status_code = None
                        terminal_kind = (
                            "literal_backend_unsupported"
                            if status_code in {400, 422}
                            else "github_search_error"
                        )
                        return (
                            [],
                            [],
                            [],
                            "search_code error: "
                            f"{status_text}{error_text}",
                            [],
                            terminal_kind,
                        )
                    results = (
                        []
                        if typed_outcome == "no_hit"
                        else (
                            payload.get("results")
                            or payload.get("items")
                            or []
                        )
                    )
                else:
                    results = payload or []
            else:
                results = self.state.runtime.search_code(
                    query,
                    self.state.repo_full_name,
                ) or []
        except Exception as exc:
            return (
                [],
                [],
                [],
                f"search_code error: {exc}",
                [],
                "github_search_error",
            )

        safe_results: List[Dict[str, Any]] = []
        sensitive_excluded = 0
        for result in results:
            path = (
                str(
                    result.get("path")
                    or result.get("file_path")
                    or ""
                )
                if isinstance(result, dict)
                else ""
            )
            if path and is_sensitive_repo_path(path):
                sensitive_excluded += 1
                if self.state.repo_inventory is not None:
                    self.state.repo_inventory.excluded_sensitive.add(path)
                continue
            if isinstance(result, dict):
                safe_results.append(result)
        filtered, policy_notes = self._filter_results_for_intent(
            safe_results,
            intent,
        )
        if sensitive_excluded:
            policy_notes.insert(
                0,
                f"excluded {sensitive_excluded} sensitive-path search result(s)",
            )
        default_snippets = snippets_from_search_results(
            filtered,
            symbol=(
                query.rstrip("(").split()[-1]
                if query.split()
                else query
            ),
            source_label="[source: default branch]",
        )
        observed: List[str] = []
        lineages: List[Dict[str, Any]] = []
        for snippet in default_snippets:
            path = snippet["path"]
            head_content = None
            head_read_outcome = "path_not_available_at_head_inventory"
            inventory = self.state.repo_inventory
            can_probe = (
                inventory is not None
                and inventory.can_direct_probe(path)
            )
            if path in self.state.accessible_files or can_probe:
                (
                    head_content,
                    _head_metadata,
                    head_failure,
                ) = self._read_repository_text(path)
                head_read_outcome = (
                    "success"
                    if isinstance(head_content, str)
                    else str(
                        (
                            head_failure.outcome
                            if head_failure is not None
                            else "error"
                        )
                        or "error"
                    )
                )
                if can_probe:
                    inventory.record_direct_probe(
                        path,
                        readable=isinstance(head_content, str),
                    )
                    if isinstance(head_content, str):
                        self.state.accessible_files.add(path)
                elif inventory is not None:
                    inventory.record_read(
                        path,
                        readable=isinstance(head_content, str),
                    )
            if isinstance(head_content, str):
                relocated, lineage = self._relocate_at_head(
                    snippet,
                    head_content,
                    query,
                )
                lineages.append(lineage)
                if relocated is not None:
                    merged = relocated
                else:
                    merged = {
                        **snippet,
                        "source": (
                            "[source: default branch; PR-head literal "
                            f"relocation failed: {lineage.get('outcome')}]"
                        ),
                        "search_lineage": dict(lineage),
                        "exact_head_admitted": False,
                    }
            else:
                lineage = {
                    "path": path,
                    "default_start": int(
                        snippet.get("start") or 1
                    ),
                    "default_end": int(
                        snippet.get("end")
                        or snippet.get("start")
                        or 1
                    ),
                    "head_sha": self.state.head_sha,
                    "outcome": "head_unreadable_or_absent",
                    "head_read_outcome": head_read_outcome,
                    "query_token_count": len(
                        literal_identifier_tokens(query)
                    ),
                }
                lineages.append(lineage)
                merged = {
                    **snippet,
                    "source": (
                        "[source: default branch, may differ on PR branch]"
                    ),
                    "search_lineage": dict(lineage),
                    "exact_head_admitted": False,
                }
            self.state.collected_snippets.append(merged)
            observed.append(
                f"{merged['path']}:{merged['start']}-{merged['end']} "
                f"{merged['source']}\n{merged['code'][:1200]}"
            )
        notes = [f"{phase} query `{query}`"]
        notes.extend(policy_notes)
        return observed, default_snippets, notes, "", lineages, ""

    def search_code(self, args: Dict[str, Any]) -> ToolResult:
        checked = validate_tool_invocation("search_code", args)
        if not checked.valid:
            return ToolResult(
                "search_code rejected non-literal or malformed request.",
                "search_error",
                error_kind=",".join(checked.reasons),
                metadata={
                    "retrieval_outcome": "invalid_request",
                    "observed_state": "not_applicable",
                },
            )
        raw_query = checked.args.get("query", "")
        query = normalize_literal_search_query(raw_query)
        intent = str(
            checked.args.get("intent") or ""
        ).strip().lower()
        if not query:
            return ToolResult(
                "search_code error: empty query",
                "search_error",
                error_kind="invalid_query",
            )

        (
            observed,
            _snippets,
            notes,
            terminal,
            search_hit_lineage,
            terminal_kind,
        ) = self._search_once(
            query,
            intent=intent,
            phase="primary",
        )
        if terminal:
            if terminal_kind == "quota_exhausted":
                return ToolResult(terminal, "quota_exhausted")
            if terminal_kind == "repeat":
                return ToolResult(terminal, "repeat")
            return ToolResult(
                terminal,
                "search_error",
                error_kind=(
                    terminal_kind or "github_search_error"
                ),
                metadata={
                    "retrieval_outcome": (
                        "backend_unsupported"
                        if terminal_kind
                        == "literal_backend_unsupported"
                        else "search_error"
                    ),
                    "observed_state": "content_unobserved",
                },
            )

        has_observation = bool(observed)
        sections: List[str] = []
        if notes:
            sections.append("[search policy] " + " | ".join(notes))
        sections.extend(observed)
        text = (
            "\n\n".join(sections)
            if has_observation
            else f"No default-branch search hits for `{query}`."
        )
        relocated_count = sum(
            item.get("outcome") == "relocated_at_head"
            for item in search_hit_lineage
        )
        default_only_count = (
            len(search_hit_lineage) - relocated_count
        )
        if not search_hit_lineage:
            head_reread_outcome = "not_applicable"
        elif relocated_count == len(search_hit_lineage):
            head_reread_outcome = "relocated_at_head"
        elif relocated_count:
            head_reread_outcome = "partial_head_relocation"
        else:
            head_reread_outcome = "default_branch_only"
        return ToolResult(
            text,
            "hit" if has_observation else "no_hit",
            source_ref="default_branch_search",
            head_reread_outcome=head_reread_outcome,
            metadata={
                "coverage_type": (
                    "search_snippet" if has_observation else ""
                ),
                "retrieval_outcome": (
                    "hit" if has_observation else "no_hit"
                ),
                "observed_state": (
                    "content_observed"
                    if has_observation
                    else "content_unobserved"
                ),
                "head_relocated_hit_count": relocated_count,
                "default_only_hit_count": default_only_count,
                "search_hit_lineage": search_hit_lineage,
            },
        )

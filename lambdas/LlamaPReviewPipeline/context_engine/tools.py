"""DeepSeek function tool schemas and execution."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from .. import config
from ..repository_paths import bounded_read_opt_in
from .code_extractor import CodeContextExtractor
from .exact_head_search import ExactHeadSearch
from .repo_structure import get_repo_structure_for_llm, is_sensitive_repo_path, normalize_repo_path
from .tool_contract import (
    MAX_READ_FILE_SYMBOLS,
    RETRIEVAL_TOOLS,
    validate_tool_invocation,
)
from .search_rag import canonical_search_key
from .state import CollectionState
from .tool_result import ToolResult

logger = logging.getLogger(__name__)

SNIPPET_PARSE_FALLBACK_PREFIX = "Syntax error in the provided code"
FULL_FILE_EVIDENCE_MAX_BYTES = 50 * 1024
EPHEMERAL_SOURCE_CACHE_MAX_BYTES = 2 * 1024 * 1024

TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search the repository's DEFAULT branch for code matching a literal substring query (tokens separated by spaces = AND). Use to find call sites / implementations / usages of a changed symbol elsewhere in the codebase (ripple effects). Results come from the default branch, NOT the PR branch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Literal substring, e.g. 'processData(' or 'implements IFoo'. No regex."},
                    "reason": {"type": "string", "description": "Why this search matters for the review."},
                    "intent": {
                        "type": "string",
                        "description": "Optional retrieval intent: external_usage, peer_dependency, internal_definition, interface_implementations, removal_cleanup, parameter_adoption, or adoption_migration.",
                    },
                },
                "required": ["query", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read bounded file content at the PR head, or check one exact PR-head path without making any content, directory, symbol, or repository-wide absence claim.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative path, must exist in the accessible file list."},
                    "mode": {"type": "string", "enum": ["content", "exact_path_existence"], "description": "Defaults to content. exact_path_existence checks only this literal path state."},
                    "reason": {"type": "string", "description": "Why this file's content can change the review decision."},
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": MAX_READ_FILE_SYMBOLS,
                        "description": (
                            "Optional literal anchors of interest to slice the "
                            f"file around; at most {MAX_READ_FILE_SYMBOLS}."
                        ),
                    },
                },
                "required": ["path", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the repository directory tree (PR head version) under an optional target path, to discover relevant files before reading them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_path": {"type": "string", "description": "Optional subdir, e.g. 'src/api'. Empty = repo root."},
                    "max_depth": {"type": "integer", "description": "Default 3."},
                    "reason": {"type": "string", "description": "Optional scoped-discovery reason."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_context",
            "description": "Signal that enough context has been collected. Provide a short summary of what was gathered and any known gaps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "known_gaps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary"],
            },
        },
    },
]


def parse_tool_arguments(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    return json.loads(arguments)


def _truncate(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _safe_args(args: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for key in (
        "query",
        "reason",
        "intent",
        "path",
        "mode",
        "target_path",
        "max_depth",
    ):
        if key in args:
            safe[key] = _truncate(args.get(key), 500)
    if isinstance(args.get("symbols"), list):
        safe["symbols"] = [
            _truncate(symbol, 120)
            for symbol in args["symbols"][:MAX_READ_FILE_SYMBOLS]
        ]
    return safe


def _safe_result_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Keep retrieval diagnostics content-free and schema-stable."""

    allowed = {
        "coverage_type",
        "exact_path_state",
        "observed_state",
        "retrieval_outcome",
        "source_size_bytes",
        "bytes_read",
        "max_bytes",
        "policy_class",
        "status_code",
        "error_type",
        "head_relocated_hit_count",
        "default_only_hit_count",
        "backend_attempted",
        "backend_full_file_fetched",
        "derived_from_event_id",
    }
    safe = {
        key: metadata.get(key)
        for key in allowed
        if metadata.get(key) is not None
    }
    raw_lineage = metadata.get("search_hit_lineage")
    if isinstance(raw_lineage, list):
        lineage_fields = {
            "path",
            "default_start",
            "default_end",
            "head_start",
            "head_end",
            "head_sha",
            "head_read_outcome",
            "outcome",
            "match_kind",
            "query_token_count",
        }
        safe["search_hit_lineage"] = [
            {
                key: item.get(key)
                for key in lineage_fields
                if item.get(key) is not None
            }
            for item in raw_lineage[:20]
            if isinstance(item, dict)
        ]
    return safe


def normalize_tool_args(name: str, args: Dict[str, Any], state: CollectionState | None = None) -> Dict[str, Any]:
    """Canonicalize model-planned tool args at the single tool boundary."""
    normalized = dict(args or {})
    if name == "read_file":
        if not str(normalized.get("path") or "").strip():
            for alias in ("target_path", "file_path", "filename", "file"):
                value = normalized.get(alias)
                if isinstance(value, str) and value.strip():
                    normalized["path"] = value
                    if state:
                        state.record_tool_arg_repair(f"read_file.path_from_{alias}")
                    break
        for alias in ("target_path", "file_path", "filename", "file"):
            normalized.pop(alias, None)
        if "max_depth" in normalized:
            normalized.pop("max_depth", None)
            if state:
                state.record_tool_arg_repair("read_file.dropped_max_depth")
    elif name == "list_dir":
        if not str(normalized.get("target_path") or "").strip() and str(normalized.get("path") or "").strip():
            normalized["target_path"] = normalized.get("path")
            normalized.pop("path", None)
            if state:
                state.record_tool_arg_repair("list_dir.target_path_from_path")
    return normalized


def _paths_from_tree(tree_text: str) -> List[str]:
    paths: List[str] = []
    stack: List[str] = []
    for line in tree_text.splitlines():
        stripped = line.rstrip()
        match = re.match(r"^(?P<prefix>(?:\|   |    )*)(?:`-- |\|-- )(?P<name>.+)$", stripped)
        if match:
            depth = len(match.group("prefix")) // 4
            name = match.group("name").strip()
            is_dir = name.endswith("/")
            cleaned = re.sub(r"\s+\(\d+B\)$", "", name).rstrip("/")
            if cleaned:
                stack = stack[:depth]
                path = "/".join([*stack, cleaned])
                paths.append(f"{path}/" if is_dir else path)
                if is_dir:
                    stack = [*stack, cleaned]
        else:
            cleaned = re.sub(r"^[\s|`+-]*", "", stripped.strip()).strip()
            cleaned = re.sub(r"\s+\(\d+B\)$", "", cleaned)
            if cleaned and cleaned not in {".", "./"}:
                if cleaned.startswith("[") and cleaned.endswith("]"):
                    continue
                paths.append(cleaned)
        if len(paths) >= 12:
            break
    return paths


def _canonical_search_key(query: str) -> str:
    return canonical_search_key(query)


def _result_outcome(name: str, result: str, hit_count: int, error: str) -> str:
    lowered = (result or "").lower()
    status_lines = [line.strip().lower() for line in (result or "").splitlines() if line.strip()]
    if name == "search_code" and (
        error
        or any(line.startswith("search_code error:") or line.startswith("github search error") for line in status_lines[:5])
    ):
        return "search_error"
    if "quota exhausted" in lowered:
        return "quota_exhausted"
    if "already collected" in lowered or "already searched" in lowered:
        return "repeat"
    if "no default-branch search hits" in lowered or "path guard" in lowered or "not readable" in lowered:
        return "no_hit"
    if name == "finish_context":
        return "finished"
    if error or any(line.startswith(f"{name} error:") or line.startswith("tool ") for line in status_lines[:5]):
        return "error"
    return "hit" if hit_count > 0 else "no_hit"


class _SnippetParseFallbackFilter(logging.Filter):
    def __init__(self, state: CollectionState):
        super().__init__()
        self.state = state

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if message.startswith(SNIPPET_PARSE_FALLBACK_PREFIX):
            self.state.snippet_parse_fallbacks += 1
            return False
        return True


class ToolExecutor:
    def __init__(self, state: CollectionState, *, github_token: str = ""):
        self.state = state
        self.github_token = github_token
        self.extractor = CodeContextExtractor()
        self._exact_head_search = ExactHeadSearch(
            state,
            read_repository_text=self._read_repository_text,
            extractor=self.extractor,
        )

    def execute(self, tool_call: Dict[str, Any], *, question_id: str = "") -> str:
        function = tool_call.get("function", {})
        name = function.get("name")
        args: Dict[str, Any] = {}
        before_snippets = len(self.state.collected_snippets)
        before_files = set(self.state.collected_files)
        before_invalid = set(self.state.non_existent_files)
        started = time.time()
        error = ""
        result = ""
        typed_result: ToolResult | None = None
        snippet_logger = logging.getLogger("llama_github.utils")
        snippet_filter = _SnippetParseFallbackFilter(self.state)
        snippet_logger.addFilter(snippet_filter)
        try:
            args = parse_tool_arguments(function.get("arguments", "{}"))
            args = normalize_tool_args(name or "", args, self.state)
            if name in RETRIEVAL_TOOLS:
                checked = validate_tool_invocation(name, args)
                args = checked.args
                if not checked.valid:
                    typed_result = ToolResult(
                        f"{name} rejected malformed retrieval request.",
                        "search_error" if name == "search_code" else "error",
                        error_kind=",".join(checked.reasons),
                        metadata={
                            "retrieval_outcome": "invalid_request",
                            "observed_state": "not_applicable",
                        },
                    )
            if typed_result is None:
                typed_result = self._derive_same_head_result(name or "", args)
            if typed_result is not None:
                pass
            elif name == "search_code":
                typed_result = self.search_code(args)
            elif name == "read_file":
                typed_result = self.read_file(args)
            elif name == "list_dir":
                typed_result = self.list_dir(args)
            elif name == "finish_context":
                typed_result = self.finish_context(args)
            else:
                result = f"Unknown tool: {name}"
                error = result
                typed_result = ToolResult(result, "error", error_kind="unknown_tool")
            result = typed_result.text
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            self.state.failed_tools.append(name or "unknown")
            error = str(exc)
            result = f"Tool {name} failed: {exc}"
            typed_result = ToolResult(result, "error", error_kind=type(exc).__name__)
        finally:
            snippet_logger.removeFilter(snippet_filter)
            self._record_event(
                name=name or "unknown",
                args=args,
                before_snippets=before_snippets,
                before_files=before_files,
                before_invalid=before_invalid,
                result=result,
                error=error,
                elapsed_seconds=time.time() - started,
                typed_result=typed_result,
                question_id=question_id,
            )
        return result

    @staticmethod
    def _symbol_key(args: Dict[str, Any]) -> tuple[str, ...]:
        return tuple(
            str(item).strip()
            for item in args.get("symbols") or []
            if isinstance(item, str) and item.strip()
        )

    def _prior_hit(self, name: str, predicate) -> Dict[str, Any] | None:
        for event in reversed(self.state.tool_events):
            if (
                event.get("tool") == name
                and event.get("outcome") == "hit"
                and event.get("evidence_event_id")
                and predicate(event)
            ):
                return event
        return None

    def _derived_result(
        self,
        prior: Dict[str, Any],
        *,
        text: str,
        outcome: str = "hit",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ToolResult:
        prior_metadata = (
            prior.get("metadata") if isinstance(prior.get("metadata"), dict) else {}
        )
        return ToolResult(
            text,
            outcome,
            source_ref=str(prior.get("source_ref") or ""),
            head_reread_outcome=str(prior.get("head_reread_outcome") or ""),
            metadata={
                **prior_metadata,
                **(metadata or {}),
                "backend_attempted": False,
                "derived_from_event_id": str(prior["evidence_event_id"]),
            },
        )

    def _derive_same_head_result(
        self,
        name: str,
        args: Dict[str, Any],
    ) -> ToolResult | None:
        """Derive only positive, compatible facts already observed at this head."""

        if name == "search_code":
            query = _canonical_search_key(str(args.get("query") or ""))
            intent = str(args.get("intent") or "").strip().lower()

            def same_search(event: Dict[str, Any]) -> bool:
                prior_args = event.get("args") or {}
                if _canonical_search_key(str(prior_args.get("query") or "")) != query:
                    return False
                if str(prior_args.get("intent") or "").strip().lower() != intent:
                    return False
                lineage = (event.get("metadata") or {}).get("search_hit_lineage")
                return bool(lineage) and all(
                    isinstance(item, dict)
                    and item.get("outcome") == "relocated_at_head"
                    and item.get("head_sha") == self.state.head_sha
                    for item in lineage
                )

            prior = self._prior_hit(name, same_search)
            if prior is not None:
                return self._derived_result(
                    prior,
                    text="Compatible exact-head search evidence reused without a backend call.",
                    metadata={"derived_paths": list(prior.get("paths") or [])},
                )
            return None

        if name == "list_dir":
            target = str(args.get("target_path") or "").strip().strip("/")
            depth = int(args.get("max_depth") or 3)
            prior = self._prior_hit(
                name,
                lambda event: str((event.get("args") or {}).get("target_path") or "")
                .strip()
                .strip("/")
                == target
                and int((event.get("args") or {}).get("max_depth") or 3) == depth,
            )
            if prior is not None:
                return self._derived_result(
                    prior,
                    text="Compatible exact-head directory inventory reused without a backend call.",
                    metadata={"derived_paths": list(prior.get("paths") or [])},
                )
            return None

        if name != "read_file":
            return None
        path = normalize_repo_path(args.get("path") or "")
        mode = str(args.get("mode") or "content")
        symbols = self._symbol_key(args)
        if not path:
            return None
        if mode == "exact_path_existence":
            prior = self._prior_hit(
                name,
                lambda event: normalize_repo_path((event.get("args") or {}).get("path") or "")
                == path
                and str((event.get("args") or {}).get("mode") or "content")
                == "exact_path_existence",
            )
            if prior is not None:
                return self._derived_result(
                    prior,
                    text=f"Exact PR-head path state for `{path}` reused without a backend call.",
                )
            return None

        prior = self._prior_hit(
            name,
            lambda event: normalize_repo_path((event.get("args") or {}).get("path") or "")
            == path
            and str((event.get("args") or {}).get("mode") or "content") != "exact_path_existence",
        )
        if prior is None:
            return None
        prior_metadata = prior.get("metadata") or {}
        cached = self.state.source_text_cache.get(path) or {}
        content = cached.get("content")
        if symbols and isinstance(content, str):
            rendered: List[str] = []
            for symbol in symbols:
                lines = content.splitlines()
                matches = [
                    index for index, line in enumerate(lines) if symbol in line
                ]
                if not matches:
                    continue
                line_index = self.extractor.pick_representative_line(
                    matches, lines, symbol
                )
                block, start, end = self.extractor.extract_enclosing_block(
                    content, line_index, symbol
                )
                if not block:
                    block, start, end = self.extractor.build_line_window(
                        content, line_index
                    )
                self.state.collected_snippets.append(
                    {
                        "path": path,
                        "code": block,
                        "start": start,
                        "end": end,
                        "kind": self.extractor.classify_snippet_kind(
                            symbol, block, path
                        ),
                        "source": (
                            f"[source: PR head {self.state.head_sha[:8]}]"
                        ),
                        "exact_head_admitted": True,
                    }
                )
                rendered.append(
                    f"{path}:{start}-{end} "
                    f"[source: PR head {self.state.head_sha[:8]}]\n"
                    f"{block[:2000]}"
                )
            if not rendered:
                return self._derived_result(
                    prior,
                    text=(
                        "No requested literal was found in cached bounded "
                        f"text for `{path}`."
                    ),
                    outcome="no_hit",
                    metadata={
                        "coverage_type": "",
                        "observed_state": "content_unobserved",
                        "retrieval_outcome": "symbol_no_hit",
                    },
                )
            return self._derived_result(
                prior,
                text="\n\n".join(rendered),
                metadata={
                    "coverage_type": "file_slice",
                    "observed_state": "content_observed",
                    "retrieval_outcome": "hit",
                },
            )

        if (
            not symbols
            and isinstance(content, str)
            and prior_metadata.get("backend_full_file_fetched") is True
        ):
            self.state.collected_snippets.append(
                {
                    "path": path,
                    "code": content,
                    "start": 1,
                    "end": max(1, len(content.splitlines())),
                    "kind": "file",
                    "source": f"[source: PR head {self.state.head_sha[:8]}]",
                    "exact_head_admitted": True,
                }
            )
            return self._derived_result(
                prior,
                text=(
                    f"{path}:1-{max(1, len(content.splitlines()))} "
                    f"[source: PR head {self.state.head_sha[:8]}]\n"
                    f"{content[:2000]}"
                ),
                metadata={
                    "coverage_type": "full_file",
                    "observed_state": "content_observed",
                    "retrieval_outcome": "hit",
                },
            )

        # A prior event without the bounded source cache is not enough to
        # manufacture a new model-visible observation. Fall through to the
        # backend so the requested bytes are materialized for this question.
        return None

    def _record_event(
        self,
        *,
        name: str,
        args: Dict[str, Any],
        before_snippets: int,
        before_files: set,
        before_invalid: set,
        result: str,
        error: str,
        elapsed_seconds: float,
        typed_result: ToolResult | None,
        question_id: str,
    ) -> None:
        new_snippets = self.state.collected_snippets[before_snippets:]
        new_files = sorted(set(self.state.collected_files) - before_files)
        invalid_paths = sorted(self.state.non_existent_files - before_invalid)
        paths = []
        for snippet in new_snippets:
            path = snippet.get("path")
            if path and path not in paths:
                paths.append(path)
        for path in new_files + invalid_paths:
            if path and path not in paths:
                paths.append(path)
        for path in (typed_result.metadata if typed_result else {}).get(
            "derived_paths", []
        ):
            if path and path not in paths:
                paths.append(path)
        if name == "read_file":
            requested_path = str(args.get("path") or "").strip()
            if requested_path and requested_path not in paths:
                paths.append(requested_path)
        if name == "list_dir" and result and not result.startswith("list_dir error"):
            target_path = str(args.get("target_path") or "").strip().strip("/")
            for path in _paths_from_tree(result):
                if target_path and path != target_path and not path.startswith(f"{target_path}/"):
                    path = f"{target_path}/{path}"
                if path not in paths:
                    paths.append(path)
        hit_count = len(new_snippets) + len(new_files)
        if name == "list_dir" and result and not result.startswith("list_dir error"):
            hit_count = max(hit_count, len(paths))
        outcome = typed_result.outcome if typed_result is not None else _result_outcome(name, result, hit_count, error)
        if outcome == "hit" and hit_count == 0 and typed_result is not None:
            hit_count = 1
        if outcome == "repeat":
            self.state.repeated_tool_calls += 1
        elif outcome == "no_hit":
            self.state.no_hit_tool_calls += 1
        elif outcome == "search_error":
            self.state.search_error_tool_calls += 1
            query = str(args.get("query") or "").strip()
            if query:
                self.state.search_error_queries.append({"query": query, "error": _truncate(error or result, 300)})
        elif outcome == "quota_exhausted":
            self.state.quota_exhausted_tool_calls += 1
        safe_metadata = _safe_result_metadata(
            typed_result.metadata if typed_result else {}
        )
        safe_metadata.setdefault("backend_attempted", True)
        observation_prefix = "; ".join(
            f"{key}={safe_metadata[key]}"
            for key in ("exact_path_state", "observed_state")
            if safe_metadata.get(key)
        )
        result_summary = _truncate(result.replace("\n", " "), 900)
        if observation_prefix:
            result_summary = _truncate(
                f"{observation_prefix}; {result_summary}", 900
            )
        event = {
                "tool": name,
                "args": _safe_args(args),
                "reason": _truncate(args.get("reason"), 400),
                "outcome": outcome,
                "hit_count": hit_count,
                "paths": paths[:20],
                "invalid_paths": invalid_paths,
                "error": _truncate(error, 500),
                "error_kind": _truncate(typed_result.error_kind if typed_result else "", 120),
                "source_ref": _truncate(typed_result.source_ref if typed_result else "", 240),
                "head_reread_outcome": _truncate(typed_result.head_reread_outcome if typed_result else "", 120),
                "question_id": question_id,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "result_summary": result_summary,
                "metadata": safe_metadata,
            }
        if question_id:
            evidence_event_id = self.state.evidence_ledger.record_event(
                question_id=question_id,
                tool=name,
                args=_safe_args(args),
                outcome=outcome,
                paths=paths[:20],
                source_ref=event["source_ref"],
                head_reread_outcome=event["head_reread_outcome"],
                error_kind=event["error_kind"],
                coverage_type=str(event["metadata"].get("coverage_type") or ""),
                exact_path_state=str(event["metadata"].get("exact_path_state") or ""),
                observed_state=str(event["metadata"].get("observed_state") or ""),
                search_hit_lineage=(
                    event["metadata"].get("search_hit_lineage") or []
                ),
                backend_attempted=bool(
                    event["metadata"].get("backend_attempted", True)
                ),
                derived_from_event_id=str(
                    event["metadata"].get("derived_from_event_id") or ""
                ),
            )
            event["evidence_event_id"] = evidence_event_id
        self.state.record_tool_event(event)

    def search_code(self, args: Dict[str, Any]) -> ToolResult:
        """Delegate literal discovery and PR-head admission as one capability."""

        return self._exact_head_search.search_code(args)

    def _read_repository_text(
        self,
        path: str,
    ) -> tuple[str | None, Dict[str, Any], ToolResult | None]:
        """Read one PR-head text file through the typed bounded contract."""

        reader = getattr(self.state.runtime, "read_text_file_bounded", None)
        if callable(reader):
            opt_in = bounded_read_opt_in(path)
            try:
                payload = reader(
                    self.state.repo_full_name,
                    path,
                    sha=self.state.head_sha,
                    opt_in=opt_in,
                )
            except Exception as exc:
                return None, {}, ToolResult(
                    f"read_file error: `{path}` could not be read at PR head {self.state.head_sha[:8]}",
                    "error",
                    error_kind=type(exc).__name__,
                    source_ref=f"pr_head:{self.state.head_sha}",
                    head_reread_outcome="error",
                )
            if not isinstance(payload, dict):
                return None, {}, ToolResult(
                    f"read_file error: `{path}` returned invalid bounded-read metadata.",
                    "error",
                    error_kind="invalid_bounded_read_result",
                    source_ref=f"pr_head:{self.state.head_sha}",
                    head_reread_outcome="error",
                )
            outcome = str(payload.get("outcome") or "error")
            metadata = {
                "retrieval_outcome": outcome,
                **{
                    key: payload.get(key)
                    for key in (
                        "source_size_bytes",
                        "bytes_read",
                        "max_bytes",
                        "policy_class",
                        "status_code",
                        "error_type",
                    )
                    if payload.get(key) is not None
                },
            }
            content = payload.get("content")
            if outcome == "success" and isinstance(content, str):
                return content, metadata, None
            return None, metadata, ToolResult(
                f"read_file {outcome}: `{path}` produced no usable PR-head text.",
                outcome,
                error_kind=str(payload.get("error_type") or outcome),
                source_ref=f"pr_head:{self.state.head_sha}",
                head_reread_outcome=outcome,
                metadata=metadata,
            )

        # Compatibility for old unit adapters only. Production pins the typed
        # bounded SDK and never takes this branch.
        try:
            content = self.state.runtime.get_file_content(
                self.state.repo_full_name,
                path,
                sha=self.state.head_sha,
            )
        except Exception as exc:
            return None, {}, ToolResult(
                f"read_file error: `{path}` could not be read at PR head {self.state.head_sha[:8]}",
                "error",
                error_kind=type(exc).__name__,
                source_ref=f"pr_head:{self.state.head_sha}",
                head_reread_outcome="error",
            )
        if not isinstance(content, str):
            return None, {}, ToolResult(
                f"read_file error: `{path}` not readable at PR head {self.state.head_sha[:8]}",
                "error",
                error_kind="head_read_failed",
                source_ref=f"pr_head:{self.state.head_sha}",
                head_reread_outcome="error",
            )
        return content, {
            "retrieval_outcome": "adapter_success",
            "source_size_bytes": len(content.encode("utf-8")),
            "bytes_read": len(content.encode("utf-8")),
        }, None

    def read_file(self, args: Dict[str, Any]) -> ToolResult:
        checked = validate_tool_invocation("read_file", args)
        if not checked.valid:
            self.state.read_file_missing_path_errors += int("path_missing" in checked.reasons)
            return ToolResult(
                "read_file rejected malformed content request.",
                "error",
                error_kind=",".join(checked.reasons),
                metadata={"exact_path_state": "unknown", "observed_state": "content_unobserved"},
            )
        args = checked.args
        path = normalize_repo_path(args.get("path") or "")
        mode = str(args.get("mode") or "content")
        inventory = self.state.repo_inventory
        exact_path_state = (
            inventory.exact_path_state(path)
            if inventory is not None and hasattr(inventory, "exact_path_state")
            else "unknown"
        )
        if not path:
            self.state.read_file_missing_path_errors += 1
            return ToolResult("read_file error: missing path", "error", error_kind="missing_path")
        if path in self.state.removed_paths:
            if mode == "exact_path_existence":
                return ToolResult(
                    f"Exact PR-head path `{path}` is absent.",
                    "hit",
                    source_ref=f"pr_head:{self.state.head_sha}",
                    head_reread_outcome="removed_at_head",
                    metadata={
                        "coverage_type": "exact_path_state",
                        "retrieval_outcome": "hit",
                        "exact_path_state": "absent",
                        "observed_state": "absent",
                    },
                )
            return ToolResult(
                f"read_file skipped: `{path}` was removed by this PR and has no PR-head content.",
                "removed_path",
                source_ref=f"pr_head:{self.state.head_sha}",
                head_reread_outcome="removed_at_head",
                metadata={"exact_path_state": "absent", "observed_state": "content_unobserved"},
            )
        if is_sensitive_repo_path(path):
            if self.state.repo_inventory is not None:
                self.state.repo_inventory.excluded_sensitive.add(path)
            return ToolResult(
                f"read_file excluded sensitive path: `{path}`.",
                "excluded_sensitive",
                error_kind="sensitive_path_policy",
                metadata={"exact_path_state": "unknown", "observed_state": "content_unobserved"},
            )
        if mode == "exact_path_existence":
            if exact_path_state == "directory":
                return ToolResult(
                    f"Exact PR-head path `{path}` is a directory, not a file.",
                    "no_hit",
                    error_kind="exact_path_not_file",
                    source_ref=f"pr_head_inventory:{self.state.head_sha}",
                    head_reread_outcome="inventory_path_is_directory",
                    metadata={
                        "retrieval_outcome": "not_applicable",
                        "exact_path_state": "unknown",
                        "observed_state": "unknown",
                    },
                )
            if exact_path_state in {"present", "absent"}:
                return ToolResult(
                    f"Exact PR-head path `{path}` is {exact_path_state}.",
                    "hit",
                    source_ref=f"pr_head_inventory:{self.state.head_sha}",
                    head_reread_outcome="inventory_exact_path_state",
                    metadata={
                        "coverage_type": "exact_path_state",
                        "retrieval_outcome": "hit",
                        "exact_path_state": exact_path_state,
                        "observed_state": exact_path_state,
                    },
                )
            if inventory is None or not inventory.can_direct_probe(path):
                return ToolResult(
                    f"Exact PR-head path state for `{path}` is unknown because the inventory is incomplete.",
                    "no_hit",
                    error_kind="exact_path_state_unknown",
                    source_ref=f"pr_head_inventory:{self.state.head_sha}",
                    head_reread_outcome="not_probed",
                    metadata={
                        "retrieval_outcome": "unknown",
                        "exact_path_state": "unknown",
                        "observed_state": "unknown",
                    },
                )
            if self.state.read_calls >= self.state.max_read_calls:
                return ToolResult(
                    "read_file quota exhausted; exact path state remains unknown.",
                    "quota_exhausted",
                    error_kind="exact_path_probe_quota_exhausted",
                    metadata={"exact_path_state": "unknown", "observed_state": "unknown"},
                )
            self.state.read_calls += 1
            self.state.attempted_files.add(path)
            probed_content, _read_meta, read_failure = self._read_repository_text(path)
            if read_failure is not None:
                absent = (
                    read_failure.outcome == "not_found"
                    or read_failure.head_reread_outcome == "not_found"
                )
                present_without_text = read_failure.outcome in {
                    "oversize",
                    "binary_or_non_utf8",
                    "directory",
                }
                inventory.record_direct_probe(path, readable=False)
                if not absent and not present_without_text:
                    return ToolResult(
                        f"Exact PR-head path state for `{path}` could not be established.",
                        "error",
                        error_kind=read_failure.error_kind or "exact_path_probe_error",
                        source_ref=f"pr_head:{self.state.head_sha}",
                        head_reread_outcome=read_failure.head_reread_outcome or "error",
                        metadata={"exact_path_state": "unknown", "observed_state": "unknown"},
                    )
                exact_path_state = "absent" if absent else "present"
            else:
                inventory.record_direct_probe(path, readable=probed_content is not None)
                exact_path_state = "present" if probed_content is not None else "absent"
            return ToolResult(
                f"Exact PR-head path `{path}` is {exact_path_state}.",
                "hit",
                source_ref=f"pr_head:{self.state.head_sha}",
                head_reread_outcome=f"exact_path_{exact_path_state}",
                metadata={
                    "coverage_type": "exact_path_state",
                    "retrieval_outcome": "hit",
                    "exact_path_state": exact_path_state,
                    "observed_state": exact_path_state,
                },
            )
        if path not in self.state.accessible_files:
            if inventory is None or not inventory.can_direct_probe(path):
                self.state.non_existent_files.add(path)
                # Preserve the historical failed-read metric for callers that
                # construct CollectionState without the production inventory.
                self.state.read_error_paths.add(path)
                return ToolResult(
                    f"read_file path guard: `{path}` is not discoverable in the PR-head inventory.",
                    "invalid_path",
                    error_kind="path_not_discoverable",
                    metadata={"exact_path_state": exact_path_state, "observed_state": "content_unobserved"},
                )
            if self.state.read_calls >= self.state.max_read_calls:
                return ToolResult("read_file quota exhausted; call finish_context if enough context is available.", "quota_exhausted")
            self.state.read_calls += 1
            self.state.attempted_files.add(path)
            probed_content, read_meta, read_failure = self._read_repository_text(path)
            if read_failure is not None:
                inventory.record_direct_probe(path, readable=False)
                self.state.read_error_paths.add(path)
                self.state.read_outcomes[path] = read_failure.outcome
                read_failure.metadata = {
                    **read_failure.metadata,
                    "exact_path_state": "unknown",
                    "observed_state": "content_unobserved",
                }
                return read_failure
            inventory.record_direct_probe(path, readable=probed_content is not None)
            if probed_content is None:
                return ToolResult(
                    f"read_file direct probe found no PR-head file at `{path}`.",
                    "not_found",
                    error_kind="direct_probe_not_found",
                    head_reread_outcome="not_found",
                    metadata={"exact_path_state": "absent", "observed_state": "content_unobserved"},
                )
            self.state.accessible_files.add(path)
            content = probed_content
            content_meta = read_meta
        else:
            content = None
            content_meta = {}
        self.state.attempted_files.add(path)
        symbols = [
            str(symbol)
            for symbol in (args.get("symbols") or [])[:MAX_READ_FILE_SYMBOLS]
            if isinstance(symbol, str) and symbol.strip()
        ]
        preflight_size = (
            inventory.file_size_bytes(path)
            if inventory is not None and hasattr(inventory, "file_size_bytes")
            else None
        )
        if (
            content is None
            and isinstance(preflight_size, int)
            and preflight_size > FULL_FILE_EVIDENCE_MAX_BYTES
            and not symbols
        ):
            self.state.read_outcomes[path] = "large_read_unaddressable"
            return ToolResult(
                f"read_file large_read_unaddressable: `{path}` is {preflight_size} bytes and no literal symbol window was requested.",
                "no_hit",
                error_kind="large_read_unaddressable",
                source_ref=f"pr_head_inventory:{self.state.head_sha}",
                head_reread_outcome="preflight_not_downloaded",
                metadata={
                    "source_size_bytes": preflight_size,
                    "retrieval_outcome": "large_read_unaddressable",
                    "exact_path_state": "present",
                    "observed_state": "content_unobserved",
                },
            )
        if path in self.state.collected_files:
            return ToolResult(
                f"`{path}` already collected; choose a different caller/contract/test/config file or call finish_context.",
                "repeat",
            )
        if content is None:
            if self.state.read_calls >= self.state.max_read_calls:
                return ToolResult("read_file quota exhausted; call finish_context if enough context is available.", "quota_exhausted")
            self.state.read_calls += 1
            content, content_meta, read_failure = self._read_repository_text(path)
            if read_failure is not None:
                self.state.read_error_paths.add(path)
                self.state.read_outcomes[path] = read_failure.outcome
                if self.state.repo_inventory is not None:
                    self.state.repo_inventory.record_read(path, readable=False)
                read_failure.metadata = {
                    **read_failure.metadata,
                    "exact_path_state": "present",
                    "observed_state": "content_unobserved",
                }
                return read_failure
        if content is None:
            self.state.read_error_paths.add(path)
            if self.state.repo_inventory is not None:
                self.state.repo_inventory.record_read(path, readable=False)
            return ToolResult(
                f"read_file error: `{path}` not readable at PR head {self.state.head_sha[:8]}",
                "error",
                error_kind="head_read_failed",
                source_ref=f"pr_head:{self.state.head_sha}",
                head_reread_outcome="error",
                metadata={
                    "exact_path_state": exact_path_state,
                    "observed_state": "content_unobserved",
                },
            )
        content_bytes = len(content.encode("utf-8"))
        if content_bytes <= EPHEMERAL_SOURCE_CACHE_MAX_BYTES:
            self.state.source_text_cache[path] = {
                "content": content,
                "source_size_bytes": content_meta.get("source_size_bytes"),
                "bytes_read": content_meta.get("bytes_read"),
            }
        source_size = content_meta.get("source_size_bytes")
        if not isinstance(source_size, int) or isinstance(source_size, bool):
            source_size = len(content.encode("utf-8"))
        bytes_read = content_meta.get("bytes_read")
        if not isinstance(bytes_read, int) or isinstance(bytes_read, bool):
            bytes_read = len(content.encode("utf-8"))
        backend_full_file_fetched = (
            source_size <= FULL_FILE_EVIDENCE_MAX_BYTES
            and bytes_read >= source_size
            and len(content.encode("utf-8")) >= source_size
            and len(content) <= config.MAX_FILE_SIZE
        )
        model_observed_full_file = backend_full_file_fetched and not symbols
        blocks = []
        for symbol in symbols:
            lines = content.splitlines()
            candidates = [idx for idx, line in enumerate(lines) if symbol and symbol in line]
            if not candidates:
                continue
            line_index = self.extractor.pick_representative_line(candidates, lines, symbol)
            block, start, end = self.extractor.extract_enclosing_block(content, line_index, symbol)
            if not block:
                block, start, end = self.extractor.build_line_window(content, line_index)
            blocks.append((symbol, block, start, end))
        if not blocks and model_observed_full_file:
            blocks = [
                (
                    "",
                    content[: config.MAX_FILE_SIZE],
                    1,
                    max(1, len(content.splitlines())),
                )
            ]
        if not blocks:
            outcome = "symbols_not_provided" if not symbols else "symbol_no_hit"
            self.state.read_error_paths.add(path)
            self.state.read_outcomes[path] = outcome
            if self.state.repo_inventory is not None:
                self.state.repo_inventory.record_read(path, readable=True)
            return ToolResult(
                f"read_file {outcome}: `{path}` is larger than the full-file evidence cap and no bounded symbol window was collected.",
                "no_hit",
                error_kind=outcome,
                source_ref=f"pr_head:{self.state.head_sha}",
                head_reread_outcome="hit_without_usable_slice",
                metadata={
                    **content_meta,
                    "retrieval_outcome": outcome,
                    "exact_path_state": "present",
                    "observed_state": "content_unobserved",
                },
            )
        rendered = []
        for symbol, block, start, end in blocks:
            snippet = {
                "path": path,
                "code": block,
                "start": start,
                "end": end,
                "kind": self.extractor.classify_snippet_kind(symbol, block, path),
                "source": f"[source: PR head {self.state.head_sha[:8]}]",
                "exact_head_admitted": True,
            }
            self.state.collected_snippets.append(snippet)
            rendered.append(f"{path}:{start}-{end} {snippet['source']}\n{block[:2000]}")
        self.state.collected_files[path] = (
            content
            if backend_full_file_fetched
            else "[bounded symbol slices retained]"
        )
        self.state.read_success_paths.add(path)
        self.state.read_outcomes[path] = "success"
        if self.state.repo_inventory is not None:
            self.state.repo_inventory.record_read(path, readable=True)
        return ToolResult(
            "\n\n".join(rendered),
            "hit",
            source_ref=f"pr_head:{self.state.head_sha}",
            head_reread_outcome="hit",
            metadata={
                **content_meta,
                "coverage_type": (
                    "full_file" if model_observed_full_file else "file_slice"
                ),
                "backend_full_file_fetched": backend_full_file_fetched,
                "exact_path_state": "present",
                "observed_state": "content_observed",
            },
        )

    def list_dir(self, args: Dict[str, Any]) -> ToolResult:
        self.state.list_calls += 1
        inventory = self.state.repo_inventory
        if inventory is None:
            # Compatibility only for unit adapters and legacy callers that
            # bypass initialize_collection. Production always reuses RepoInventory
            # and therefore performs no additional tree request here.
            result = get_repo_structure_for_llm(
                self.state.repo_full_name,
                token=self.github_token,
                sha=self.state.head_sha,
                target_path=args.get("target_path") or None,
                max_depth=int(args.get("max_depth") or 3),
                include_file_list=False,
            )
        else:
            result = inventory.render_tree(
                target_path=args.get("target_path") or None,
                max_depth=int(args.get("max_depth") or 3),
                include_file_list=False,
            )
        if "error" in result:
            return ToolResult(f"list_dir error: {result['error']}", "error", error_kind="inventory_error")
        tree = result.get("tree", "")
        outcome = "no_hit" if tree.startswith("[Empty or no items") else "hit"
        return ToolResult(
            tree,
            outcome,
            source_ref=f"pr_head_tree:{self.state.head_sha}",
            metadata={
                "coverage_type": "directory_inventory" if outcome == "hit" else "",
                "retrieval_outcome": outcome,
            },
        )

    def finish_context(self, args: Dict[str, Any]) -> ToolResult:
        self.state.finished = True
        self.state.finish_reason = "explicit_finish"
        self.state.finish_summary = args.get("summary", "")
        self.state.known_gaps = [gap for gap in args.get("known_gaps", []) if isinstance(gap, str)]
        return ToolResult(
            "Context collection finished.",
            "finished",
            metadata={"coverage_type": "non_repository"},
        )

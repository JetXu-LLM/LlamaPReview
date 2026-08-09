"""Shared bounded state for exact-head PFR evidence collection."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .. import config
from .evidence import EvidenceLedger
from .repo_structure import RepoInventory


@dataclass
class QualityMetrics:
    completeness: float = 0.0
    relevance: float = 0.0
    sufficiency: float = 0.0
    efficiency: float = 0.0
    overall: float = 0.0
    confidence: float = 0.0


@dataclass
class CollectionState:
    pr_details: str
    pr_content: Dict[str, Any]
    repo_full_name: str
    head_sha: str
    default_branch: str
    runtime: Any
    deadline: Optional[Any] = None
    root_tree: str = ""
    repo_inventory: Optional[RepoInventory] = None
    accessible_files: Set[str] = field(default_factory=set)
    removed_paths: Set[str] = field(default_factory=set)
    evidence_ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    collected_snippets: List[Dict[str, Any]] = field(default_factory=list)
    collected_files: Dict[str, str] = field(default_factory=dict)
    # Ephemeral bounded source used only to derive same-head evidence for a new
    # question without a second backend call. Never serialize or log it.
    source_text_cache: Dict[str, Dict[str, Any]] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    non_existent_files: Set[str] = field(default_factory=set)
    failed_tools: List[str] = field(default_factory=list)
    tool_events: List[Dict[str, Any]] = field(default_factory=list)
    attempted_files: Set[str] = field(default_factory=set)
    attempted_search_queries: Set[str] = field(default_factory=set)
    tool_arg_repair_counts: Dict[str, int] = field(default_factory=dict)
    read_success_paths: Set[str] = field(default_factory=set)
    read_error_paths: Set[str] = field(default_factory=set)
    read_outcomes: Dict[str, str] = field(default_factory=dict)
    planned_read_paths: List[str] = field(default_factory=list)
    planned_read_modes: Dict[str, str] = field(default_factory=dict)
    # One path can legitimately be checked both for existence and for content.
    # Keep those as separate request identities; the path-only fields above are
    # retained for artifact compatibility and must not be used to collapse the
    # two evidence obligations in fetch-health accounting.
    planned_read_requests: List[Dict[str, str]] = field(default_factory=list)
    planned_invalid_read_paths: List[str] = field(default_factory=list)
    budget_skipped_verification_paths: List[str] = field(default_factory=list)
    budget_skipped_verification_steps: List[Dict[str, str]] = field(default_factory=list)
    read_file_missing_path_errors: int = 0
    snippet_parse_fallbacks: int = 0
    repeated_tool_calls: int = 0
    no_hit_tool_calls: int = 0
    search_error_tool_calls: int = 0
    search_error_queries: List[Dict[str, str]] = field(default_factory=list)
    quota_exhausted_tool_calls: int = 0
    finish_summary: str = ""
    finish_reason: str = ""
    known_gaps: List[str] = field(default_factory=list)
    budget_exhausted_flag: bool = False
    soft_budget_exhausted: bool = False
    soft_budget_seconds: float = 0.0
    budget_health_reasons: List[str] = field(default_factory=list)
    fetch_degradation_reason_counts: Dict[str, int] = field(default_factory=dict)
    current_iteration: int = 0
    finished: bool = False
    total_tokens: int = 0
    search_calls: int = 0
    read_calls: int = 0
    list_calls: int = 0
    monotonic_started: float = field(default_factory=time.monotonic, repr=False)
    time_budget: int = config.PFR_HIGH_TIME_BUDGET_SECONDS
    token_budget: int = config.PFR_HIGH_TOKEN_BUDGET
    max_tool_rounds: int = config.PFR_HIGH_MAX_TOOL_ROUNDS
    max_search_calls: int = config.PFR_MAX_SEARCH_CALLS
    max_read_calls: int = config.PFR_MAX_READ_CALLS
    max_context_chars: int = config.PFR_HIGH_MAX_CONTEXT_CHARS

    def __post_init__(self) -> None:
        self.evidence_ledger.expected_head_sha = str(self.head_sha or "")

    def elapsed_time(self) -> float:
        return max(0.0, time.monotonic() - self.monotonic_started)

    def remaining_time(self) -> float:
        return self.time_budget - self.elapsed_time()

    def remaining_tokens(self) -> int:
        return self.token_budget - self.total_tokens

    def budget_exhausted(self) -> bool:
        deadline_exhausted = False
        if self.deadline is not None:
            remaining = getattr(self.deadline, "remaining_seconds", None)
            if callable(remaining):
                deadline_exhausted = float(remaining()) < 30
        return (
            deadline_exhausted
            or self.remaining_time() < 30
            or self.remaining_tokens() < 5000
            or self.current_iteration >= self.max_tool_rounds
        )

    def add_usage(self, usage: Optional[Dict[str, Any]]) -> None:
        if usage:
            self.total_tokens += int(usage.get("total_tokens") or 0)

    def record_tool_event(self, event: Dict[str, Any]) -> None:
        self.tool_events.append(event)

    def record_tool_arg_repair(self, key: str) -> None:
        self.tool_arg_repair_counts[key] = self.tool_arg_repair_counts.get(key, 0) + 1

"""Replay-stable question, evidence-event, and resolution ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional


SUPPORTING_OUTCOMES = {"hit"}
_EXACT_HEAD_SOURCE_RE = re.compile(
    r"^pr_head(?:_inventory|_tree)?:(?P<head>[^:\s]+)$",
    re.IGNORECASE,
)
QUESTION_LIFECYCLES = {
    "planned",
    "executed",
    "derived_gap",
    "dropped_invalid",
    "dropped_redundant",
    "dropped_cap",
    "budget_skipped",
    "terminal_unexecuted",
}
RESOLUTION_STATUSES = {"answered", "unknown", "contradicted"}
COVERAGE_TYPES = {
    "changed_region",
    "search_snippet",
    "file_slice",
    "full_file",
    "directory_inventory",
    "exact_path_state",
    "non_repository",
}
EXACT_PATH_STATES = {"present", "absent", "unknown"}
OBSERVED_STATES = {
    "content_observed",
    "content_unobserved",
    "present",
    "absent",
    "unknown",
    "not_applicable",
}


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(_jsonable(item) for item in value)
    return value


def stable_id(prefix: str, payload: Any) -> str:
    canonical = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def event_supports_answer(
    event: Dict[str, Any],
    *,
    expected_head_sha: str,
) -> bool:
    """Return whether one event can answer its exact-head repository question.

    ``outcome=hit`` only reports that a backend returned something.  Answer
    eligibility additionally requires the observation to belong to the queued
    PR head.  Default-branch search is deliberately stricter: every retained
    hit must have been relocated to that same head.
    """

    if not isinstance(event, dict) or event.get("outcome") not in SUPPORTING_OUTCOMES:
        return False
    expected_head = _normalized_text(expected_head_sha).casefold()
    if not expected_head:
        # A standalone ledger without a declared head retains its historical
        # local-contract behavior. Production collection state always supplies
        # the queued head; once supplied, missing lineage fails closed below.
        return _normalized_text(event.get("source_ref")) != "default_branch_search"

    source_ref = _normalized_text(event.get("source_ref"))
    if source_ref == "default_branch_search":
        if _normalized_text(event.get("head_reread_outcome")) != "relocated_at_head":
            return False
        lineage = event.get("search_hit_lineage")
        if not isinstance(lineage, list) or not lineage:
            return False
        return all(
            isinstance(item, dict)
            and _normalized_text(item.get("outcome")) == "relocated_at_head"
            and _normalized_text(item.get("head_sha")).casefold() == expected_head
            for item in lineage
        )

    source_match = _EXACT_HEAD_SOURCE_RE.fullmatch(source_ref)
    return bool(
        source_match
        and source_match.group("head").casefold() == expected_head
    )


@dataclass
class EvidenceLedger:
    expected_head_sha: str = ""
    questions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    events: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    resolutions: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def ingest_meta(self, raw: Any) -> None:
        """Merge an internally generated ledger snapshot without rewriting IDs.

        Route preflight can establish exact-head path-state evidence before PFR
        creates its collection state.  Reusing those facts must preserve their
        stable IDs so Route, Reconcile, Deep, and Final all refer to the same
        observation.  Conflicting duplicates are a programming error; malformed
        external data is ignored rather than allowed to corrupt the ledger.
        """

        if not isinstance(raw, dict):
            return

        def merge(
            destination: Dict[str, Dict[str, Any]],
            values: Any,
            *,
            identity_key: str,
        ) -> None:
            if not isinstance(values, list):
                return
            for value in values:
                if not isinstance(value, dict):
                    continue
                identity = _normalized_text(value.get(identity_key))
                if not identity:
                    continue
                candidate = deepcopy(value)
                existing = destination.get(identity)
                if existing is not None and existing != candidate:
                    raise ValueError(f"conflicting evidence ledger identity: {identity}")
                destination.setdefault(identity, candidate)

        merge(self.questions, raw.get("questions"), identity_key="id")
        merge(self.events, raw.get("evidence_events"), identity_key="id")
        for value in raw.get("resolutions") or []:
            if not isinstance(value, dict):
                continue
            question_id = _normalized_text(value.get("question_id"))
            if not question_id:
                continue
            candidate = deepcopy(value)
            existing = self.resolutions.get(question_id)
            if existing is not None and existing != candidate:
                raise ValueError(
                    f"conflicting evidence ledger resolution: {question_id}"
                )
            self.resolutions.setdefault(question_id, candidate)

    def register_question(
        self,
        *,
        question: str,
        tool: str,
        args: Dict[str, Any],
        lifecycle: str = "planned",
    ) -> str:
        text = _normalized_text(question) or f"Verify with {tool}."
        safe_args = _jsonable(args or {})
        question_id = stable_id(
            "q",
            {"question": text.lower(), "tool": tool, "args": safe_args},
        )
        existing = self.questions.get(question_id)
        if existing is None:
            self.questions[question_id] = {
                "id": question_id,
                "text": text,
                "tool": tool,
                "args": safe_args,
                "lifecycle": lifecycle if lifecycle in QUESTION_LIFECYCLES else "planned",
                "event_ids": [],
            }
        elif lifecycle in QUESTION_LIFECYCLES:
            existing["lifecycle"] = lifecycle
        return question_id

    def register_unresolved_gap(
        self,
        *,
        source_slot: str,
        question: str,
        how_to_check: str,
    ) -> str:
        """Register a non-tool evidence gap under a code-owned identity.

        Display prose is deliberately excluded from the ID so a later stage can
        improve the wording without losing provenance. ``source_slot`` comes
        from orchestration (repo/head/stage/round/occurrence), never the model.
        """

        slot = _normalized_text(source_slot)
        if not slot:
            raise ValueError("source_slot must not be empty")
        text = _normalized_text(question) or "Manual verification."
        question_id = stable_id("q", {"derived_gap_source": slot})
        self.questions.setdefault(
            question_id,
            {
                "id": question_id,
                "text": text,
                "tool": "manual_verification",
                "args": {"how_to_check": _normalized_text(how_to_check)},
                "lifecycle": "derived_gap",
                "event_ids": [],
            },
        )
        return question_id

    def set_question_lifecycle(self, question_id: str, lifecycle: str) -> None:
        if question_id in self.questions and lifecycle in QUESTION_LIFECYCLES:
            self.questions[question_id]["lifecycle"] = lifecycle

    def question_ids_for_text(self, text: str) -> List[str]:
        normalized = _normalized_text(text).lower()
        if not normalized:
            return []
        return [
            question_id
            for question_id, question in self.questions.items()
            if _normalized_text(question.get("text")).lower() == normalized
        ]

    def question_id_for_text(self, text: str) -> str:
        matches = self.question_ids_for_text(text)
        executed = [
            question_id
            for question_id in matches
            if (self.questions.get(question_id) or {}).get("lifecycle")
            == "executed"
        ]
        if len(executed) == 1:
            return executed[0]
        if executed:
            return ""
        return matches[0] if len(matches) == 1 else ""

    def record_event(
        self,
        *,
        question_id: str,
        tool: str,
        args: Dict[str, Any],
        outcome: str,
        paths: Iterable[str] = (),
        source_ref: str = "",
        head_reread_outcome: str = "",
        error_kind: str = "",
        coverage_type: str = "",
        exact_path_state: str = "",
        observed_state: str = "",
        search_hit_lineage: Optional[Iterable[Dict[str, Any]]] = None,
        backend_attempted: bool = True,
        derived_from_event_id: str = "",
    ) -> str:
        question = self.questions.get(question_id)
        prior_count = len(question.get("event_ids") or []) if question else 0
        event_id = stable_id(
            "ev",
            {
                "question_id": question_id,
                "tool": tool,
                "args": _jsonable(args or {}),
                "attempt": prior_count,
            },
        )
        event = {
            "id": event_id,
            "question_id": question_id,
            "tool": tool,
            "args": _jsonable(args or {}),
            "outcome": str(outcome or "unknown"),
            "paths": list(dict.fromkeys(str(path) for path in paths if path)),
            "source_ref": _normalized_text(source_ref),
            "head_reread_outcome": _normalized_text(head_reread_outcome),
            "error_kind": _normalized_text(error_kind),
            "coverage_type": (
                coverage_type if coverage_type in COVERAGE_TYPES else ""
            ),
            "exact_path_state": (
                exact_path_state if exact_path_state in EXACT_PATH_STATES else ""
            ),
            "observed_state": (
                observed_state if observed_state in OBSERVED_STATES else ""
            ),
            "search_hit_lineage": [
                _jsonable(item)
                for item in search_hit_lineage or []
                if isinstance(item, dict)
            ][:20],
            "backend_attempted": bool(backend_attempted),
            "derived_from_event_id": (
                _normalized_text(derived_from_event_id)
                if not backend_attempted
                else ""
            ),
        }
        self.events[event_id] = event
        if question is not None:
            if event_id not in question["event_ids"]:
                question["event_ids"].append(event_id)
            question["lifecycle"] = "executed"
        return event_id

    def supporting_event_ids(self, question_id: str) -> List[str]:
        question = self.questions.get(question_id) or {}
        return [
            event_id
            for event_id in question.get("event_ids") or []
            if event_supports_answer(
                self.events.get(event_id) or {},
                expected_head_sha=self.expected_head_sha,
            )
        ]

    def resolve(
        self,
        *,
        question_id: str,
        status: str,
        evidence_refs: Optional[Iterable[str]] = None,
        conclusion: str = "",
        how_to_check: str = "",
        provenance_kind: str = "",
        provenance_refs: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        refs_were_provided = evidence_refs is not None
        requested_refs = list(dict.fromkeys(str(ref) for ref in evidence_refs or []))
        valid_refs = [
            ref
            for ref in requested_refs
            if ref in self.events
            and self.events[ref].get("question_id") == question_id
            and event_supports_answer(
                self.events[ref],
                expected_head_sha=self.expected_head_sha,
            )
        ]
        explicit_refs_invalid = refs_were_provided and (
            not requested_refs or len(valid_refs) != len(requested_refs)
        )
        normalized_status = status if status in RESOLUTION_STATUSES else "unknown"
        # A no-hit/error-only question cannot become answered merely because the model says so.
        # Code may fill omitted refs only when one same-question hit is
        # unambiguous. Explicit but mismatched refs must fail closed instead of
        # being silently replaced with a different observation.
        if normalized_status == "answered" and not refs_were_provided:
            supporting_refs = self.supporting_event_ids(question_id)
            if len(supporting_refs) == 1:
                valid_refs = supporting_refs
        answer_binding_failed = normalized_status == "answered" and (
            not valid_refs or explicit_refs_invalid
        )
        if answer_binding_failed:
            normalized_status = "unknown"
            valid_refs = []
            # An answer without same-question, answer-eligible evidence cannot
            # carry its model conclusion into review context. Preserve only
            # the deterministic fact that this retrieval question is unresolved.
            conclusion = ""
            how_to_check = ""
            provenance_kind = ""
            provenance_refs = []
        resolution_id = stable_id("res", {"question_id": question_id})
        resolution = {
            "id": resolution_id,
            "question_id": question_id,
            "status": normalized_status,
            "evidence_refs": valid_refs,
            "conclusion": _normalized_text(conclusion),
            "how_to_check": _normalized_text(how_to_check),
            "provenance_kind": _normalized_text(provenance_kind),
            "provenance_refs": list(
                dict.fromkeys(str(ref) for ref in provenance_refs or [] if ref)
            ),
        }
        self.resolutions[question_id] = resolution
        return resolution

    def ensure_terminal_resolutions(self) -> None:
        for question_id, question in self.questions.items():
            if question_id in self.resolutions:
                continue
            self.resolve(
                question_id=question_id,
                status="unknown",
                conclusion="Verification did not produce supporting evidence.",
                how_to_check="Run the recorded lookup or inspect the referenced repository path.",
            )

    def to_meta(self) -> Dict[str, Any]:
        self.ensure_terminal_resolutions()
        return {
            "questions": list(self.questions.values()),
            "evidence_events": list(self.events.values()),
            "resolutions": list(self.resolutions.values()),
        }

    def event_index(self) -> Dict[str, Any]:
        """Return the complete content-free control plane for Reconcile.

        This index preserves only identities and code-owned observation
        eligibility.  It intentionally excludes question text, arguments,
        paths, queries, snippets, and model-authored conclusions; those remain
        in the separately bounded diagnostic context.
        """

        questions = []
        for question_id, question in self.questions.items():
            questions.append(
                {
                    "question_id": question_id,
                    "tool": str(question.get("tool") or ""),
                    "event_ids": [
                        event_id
                        for event_id in question.get("event_ids") or []
                        if event_id in self.events
                    ],
                }
            )
        events = []
        for event_id, event in self.events.items():
            events.append(
                {
                    "event_id": event_id,
                    "question_id": str(event.get("question_id") or ""),
                    "tool": str(event.get("tool") or ""),
                    "outcome": str(event.get("outcome") or "unknown"),
                    "coverage": str(event.get("coverage_type") or ""),
                    "answer_eligible": event_supports_answer(
                        event,
                        expected_head_sha=self.expected_head_sha,
                    ),
                }
            )
        return {
            "expected_head_sha": str(self.expected_head_sha or ""),
            "questions": questions,
            "events": events,
        }

    def compact_event_index_text(self) -> str:
        """Serialize the Reconcile control plane without lossy truncation."""

        return json.dumps(
            self.event_index(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def compact_text(self, *, finalize: bool = False) -> str:
        if finalize:
            self.ensure_terminal_resolutions()
        lines: List[str] = []
        for question_id, question in self.questions.items():
            resolution = self.resolutions.get(question_id) or {}
            refs = ", ".join(resolution.get("evidence_refs") or []) or "none"
            lines.append(
                f"- `{question_id}` [{question.get('lifecycle')}] {question.get('text')} "
                f"=> {resolution.get('status', 'pending')} (evidence: {refs})"
            )
        return "\n".join(lines) if lines else "- No verification questions were registered."

    def compact_review_text(self, *, finalize: bool = False) -> str:
        """Render review-facing facts and gaps without control-plane identities."""

        if finalize:
            self.ensure_terminal_resolutions()
        lines: List[str] = []
        for question_id, question in self.questions.items():
            resolution = self.resolutions.get(question_id) or {}
            status = str(resolution.get("status") or "pending")
            conclusion = _normalized_text(resolution.get("conclusion"))
            how_to_check = _normalized_text(resolution.get("how_to_check"))
            line = (
                f"- [{status}] {question.get('text')} "
                f"(retrieval: {question.get('lifecycle')})"
            )
            if conclusion:
                line += f"; reconcile conclusion: {conclusion}"
            if status == "unknown" and how_to_check:
                line += f"; remaining check: {how_to_check}"
            lines.append(line)
        return (
            "\n".join(lines)
            if lines
            else "- No verification questions were registered."
        )

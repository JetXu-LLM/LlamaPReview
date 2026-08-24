"""Deterministic public-v3 Markdown and publish-JSON rendering.

This module owns presentation mechanics only. It consumes a validated,
model-owned decision and may contract unsafe optional surfaces; it never makes
or repairs engineering judgment.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .evidence_contract import (
    catalog_entries as _catalog_entries,
    entry_paths as _entry_paths,
    entry_supports_claim as _entry_supports_claim,
)
from .rendering_safety import format_mermaid
from .schema import clean_suggested_content, suggestion_presentation


SCHEMA_VERSION = 3
BLOCKING_PRIORITIES = {"P0", "P1"}
MAX_OWNER_ACTIONS = 2
MAX_HEADLINE_FINDINGS = 2
MAX_INLINE_FINDINGS = 4
MAX_NONBLOCKING_INLINE_FINDINGS = 1
MAX_VISIBLE_SCOPE_ITEMS = 6
MAX_VISIBLE_DETAILS_FINDINGS = 8
MAX_VISIBLE_UNKNOWN_ITEMS = 8


def _text(value: Any) -> str:
    return str(value or "").strip()


_CLEAR_SENTENCE_PREFIX = "No review blocker found"


def _has_clear_sentence_prefix(value: Any) -> bool:
    text = _text(value)
    if not text.casefold().startswith(_CLEAR_SENTENCE_PREFIX.casefold()):
        return False
    boundary = text[len(_CLEAR_SENTENCE_PREFIX) :]
    return not boundary or boundary[0].isspace() or boundary[0] in ".,:;!—-"


def _derive_public_sentence(
    verdict: str,
    findings: Sequence[Dict[str, Any]],
    unknowns: Sequence[Dict[str, Any]],
) -> str:
    if verdict == "blocked_findings":
        finding = next((item for item in findings if item.get("blocking") is True), None)
        headline = _text((finding or {}).get("headline"))
        return (
            f"Don't merge yet — {headline.rstrip('.')} .".replace(" .", ".")
            if headline
            else "One supported finding needs attention before merging."
        )
    if verdict == "unverified":
        unknown = next(
            (item for item in unknowns if item.get("affects_merge") is True), None
        )
        claim = _text((unknown or {}).get("claim"))
        return (
            f"Merge readiness still needs verification: {claim.rstrip('.')} .".replace(" .", ".")
            if claim
            else "One material check still needs verification before merging."
        )
    return "No review blocker found in the reviewed changes."

def _decision_heading(verdict: str) -> str:
    if verdict == "blocked_findings":
        return "Blocking issues found"
    if verdict == "unverified":
        return "Verification needed"
    return "No blocking issues found"


def _dedupe_decision_sentence(sentence: Any, *, visible_verdict: str) -> str:
    """Avoid repeating the clear-only JSON contract in the public heading.

    The v3 contract deliberately requires a clear public sentence to start with
    ``No review blocker found`` so the object remains truthful outside this
    renderer.  The Markdown heading states the same conclusion in more natural
    user-facing copy.  Remove only that exact contract prefix; this remains
    presentation normalization, not a prose classifier or a change to
    model-owned engineering judgment.
    """

    value = _text(sentence)
    if visible_verdict != "clear":
        return value
    label = _CLEAR_SENTENCE_PREFIX
    if not value.casefold().startswith(label.casefold()):
        return value
    boundary = value[len(label) :]
    if boundary and not (boundary[0].isspace() or boundary[0] in ".,:;!—-"):
        return value
    remainder = boundary.lstrip(" .,:;!—-")
    if remainder.casefold().rstrip(".") in {
        "in the reviewed changes",
        "in the retained evidence",
    }:
        return ""
    if remainder and remainder[0].islower():
        remainder = remainder[0].upper() + remainder[1:]
    return remainder


def _priority_rank(priority: Any) -> int:
    return {"P0": 0, "P1": 1, "P2": 2}.get(_text(priority), 3)


def _short_path(path: Any, max_len: int = 44) -> str:
    value = _text(path)
    if len(value) <= max_len:
        return value
    parts = value.split("/")
    if len(parts) <= 2:
        return value[-max_len:]
    return f"{parts[0]}/.../{parts[-1]}"


def _table_cell(value: Any) -> str:
    return _text(value).replace("|", "\\|").replace("\n", "<br/>")


def _code_path(value: Any) -> str:
    return _short_path(value).replace("`", "'") or "review context"


def _dynamic_code_fence(value: Any) -> str:
    """Fence model-owned code without letting embedded backticks escape."""

    code = str(value or "")
    longest_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", code)),
        default=0,
    )
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}\n{code.rstrip(chr(10))}\n{fence}"


def _verification_boundary(finding: Dict[str, Any]) -> str:
    status = {
        "verified": "confirmed",
        "unverified": "needs verification",
        "contradicted": "contradicted",
    }.get(_text(finding.get("evidence_status")), "not stated")
    scope = {
        "changed_region": "changed region",
        "bounded_context": "bounded reviewed context",
        "whole_file": "complete reviewed file",
        "repository": "repository",
    }.get(_text(finding.get("claim_scope")), "retained review context")
    return f"Verification boundary: {status}; scope: {scope}."


def _normalized_fold_text(value: Any) -> str:
    """Normalize first-screen copy for the duplicate-suppression guard (G1)."""

    text = " ".join(_text(value).split()).casefold()
    return text.strip().rstrip(".!?").strip()


_TEMPLATE_ARTIFACT_MARKERS = (
    "whether whether",
    "whether how",
    "whether not all",
)


def _template_artifact_lint(text: str) -> bool:
    """Detect mechanical composition artifacts in first-screen copy (G4).

    Bounded, observed patterns only: template double-joins, nested
    could-not-verify composition, and a repeated >=10-word substring. On a
    hit the caller falls back to the code-derived sentence for that state;
    clean copy is never modified.
    """

    if not text:
        return False
    if ".;" in text:
        return True
    lowered = " ".join(text.split()).casefold()
    for marker in _TEMPLATE_ARTIFACT_MARKERS:
        if marker in lowered:
            return True
    if lowered.count("could not verify whether") >= 2:
        return True
    words = re.findall(r"[a-z0-9']+", lowered)
    seen: set[tuple[str, ...]] = set()
    for index in range(len(words) - 9):
        window = tuple(words[index : index + 10])
        if window in seen:
            return True
        seen.add(window)
    return False


_MECHANICAL_PASS_PHRASES = (
    "all checks passed",
    "ci checks pass",
    "ci pipeline passes",
    "every completed gate passed",
    "passed all test jobs",
    "the build is green",
    "the mutation check and all test matrices pass",
)


def _is_mechanical_pass_fragment(value: str) -> bool:
    """Recognize only Final's observed mechanical pass fragment shapes."""

    normalized = _text(value).rstrip(".!?").casefold()
    if normalized in _MECHANICAL_PASS_PHRASES:
        return True
    head, separator, result = _text(value).rpartition(":")
    return bool(
        separator
        and head.strip()
        and result.strip().rstrip(".!?").casefold()
        in {"pass", "passes", "passed"}
    )


def _without_mechanical_pass_claim(value: str) -> str:
    """Remove a contradictory CI-pass clause while retaining code rationale."""

    cleaned = _text(value)
    for phrase in _MECHANICAL_PASS_PHRASES:
        cleaned = re.sub(
            rf"(?:\s*[,;—-]?\s*(?:and|while|with)\s+)?{re.escape(phrase)}",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
    cleaned = re.sub(r"\s+([,;.!?])", r"\1", cleaned)
    cleaned = re.sub(r"(?:,|;|—|-|\band\b)\s*$", "", cleaned).strip()
    return cleaned


def _ci_public_state(review: Dict[str, Any]) -> Dict[str, Any]:
    plan = review.get("rendering_plan") or {}
    state = plan.get("ci_public_state") if isinstance(plan, dict) else None
    return state if isinstance(state, dict) else {}


def _ci_fact_sentence(state: Dict[str, Any]) -> str:
    counts = state.get("counts") or {}
    parts: list[str] = []
    labels = (
        ("failure", "failed"),
        ("action_required", "action required"),
        ("pending", "pending"),
        ("incomplete", "incomplete"),
    )
    for key, label in labels:
        count = int(counts.get(key) or 0)
        if count:
            parts.append(f"{count} {label}")
    retrieval = _text(state.get("retrieval_outcome"))
    if retrieval in {"partial", "error", "unverified"}:
        parts.append(f"retrieval {retrieval}")
    observed = ", ".join(parts) or "unresolved evidence"
    if state.get("posture") == "not_observed":
        if retrieval == "no_hit":
            return (
                "Exact-head CI reported no statuses or check runs; no "
                "CI-dependent merge-safety claim is made."
            )
        return (
            "Exact-head CI evidence was not observed; no CI-dependent "
            "merge-safety claim is made."
        )
    if state.get("posture") == "unrelated_supported":
        return (
            f"Exact-head CI reports {observed}; retained exact evidence "
            "attributes those failures outside this change."
        )
    return (
        f"Exact-head CI remains unresolved ({observed}); no CI-dependent "
        "merge-safety claim is made."
    )


def _first_sentence(text: str) -> str:
    match = re.search(r"[.!?](?:\s|$)", text)
    if match:
        return text[: match.end()].strip()
    return text.strip()


def _bounded_first_screen_explanation(text: str, max_words: int = 40) -> str:
    """Keep one root-cause sentence within the visible word budget."""

    words = text.split()
    if len(words) <= max_words:
        return text
    sentence = _first_sentence(text)
    sentence_words = sentence.split()
    if len(sentence_words) <= max_words:
        return sentence
    # A half-sentence is worse than a conservative complete fallback. Callers
    # already own verdict-specific fallbacks when no bounded sentence exists.
    return ""


def _highest_value_nonblocking_finding(
    review: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    candidates = [
        (index, finding)
        for index, finding in enumerate(review.get("findings") or [])
        if isinstance(finding, dict)
        and finding.get("blocking") is not True
        and _text(finding.get("headline"))
    ]
    if not candidates:
        return None
    visibility_rank = {"headline": 0, "inline": 1, "collapsed": 2}
    return min(
        candidates,
        key=lambda item: (
            _priority_rank(item[1].get("priority")),
            visibility_rank.get(_text(item[1].get("visibility")), 3),
            item[0],
        ),
    )[1]


def _clear_first_screen_lines(
    review: Dict[str, Any],
    model_sentence: str,
    *,
    suppress_mechanical_pass_claims: bool = False,
) -> Tuple[list[str], str]:
    """Compose the clear fold: causal proof, then retained-finding trace.

    Whenever retained findings exist the arithmetic count line is mandatory
    (G2) - a green heading may never appear with zero visible trace of
    confirmed findings, and the count is computed, never hardcoded. The
    proof line prefers a substantive validated model sentence, then the
    evidence-scope projection, then the conservative boundary.
    """

    findings = [
        finding
        for finding in review.get("findings") or []
        if isinstance(finding, dict)
    ]
    lines: list[str] = []
    source = ""
    model_proof = _first_sentence(model_sentence)
    if suppress_mechanical_pass_claims:
        model_proof = _without_mechanical_pass_claim(model_proof)
    if _is_mechanical_pass_fragment(model_proof):
        model_proof = ""
    model_proof = _bounded_first_screen_explanation(model_proof)
    if model_proof and not model_proof.endswith((".", "!", "?", "…")):
        model_proof += "."
    model_ok = bool(
        model_proof
        and len(model_proof) <= 360
        and not _template_artifact_lint(model_proof)
    )
    if model_ok:
        lines.append(model_proof)
        source = "model"

    if findings:
        top = _highest_value_nonblocking_finding(review)
        count = len(findings)
        plural = "" if count == 1 else "s"
        headline = _text((top or {}).get("headline")).rstrip(".")
        lines.append(
            f"{count} non-blocking finding{plural} retained — highest: "
            f"{headline}."
        )
        source = source or "finding"
    if lines:
        return lines, source

    scope_item = next(
        (
            item
            for item in review.get("evidence_scope") or []
            if isinstance(item, dict) and _text(item.get("description"))
        ),
        None,
    )
    if scope_item is not None:
        return [_text(scope_item.get("description"))], "evidence_scope"

    return (
        [
            "Reviewed only the available changed regions; no broader "
            "coverage is claimed."
        ],
        "conservative_fallback",
    )


def _format_details(
    review: Dict[str, Any],
    *,
    shown_action_texts: Tuple[str, ...] = (),
    include_inline_findings: bool = False,
) -> str:
    lines = ["<details>", "<summary>Review details and evidence</summary>", ""]
    findings = review.get("findings") or []
    if findings:
        visible_findings = findings[:MAX_VISIBLE_DETAILS_FINDINGS]
        lines.extend(["| Priority | File | Finding | Evidence |", "|---|---|---|---|"])
        for finding in visible_findings:
            evidence = {
                "verified": "confirmed",
                "unverified": "needs verification",
                "contradicted": "contradicted",
            }.get(_text(finding.get("evidence_status")), "")
            lines.append(
                f"| {_table_cell(finding.get('priority'))} | `{_code_path(finding.get('file_path'))}` | "
                f"{_table_cell(finding.get('headline'))} | {_table_cell(evidence)} |"
            )
        if len(findings) > MAX_VISIBLE_DETAILS_FINDINGS:
            lines.append(
                f"\n_{len(findings) - MAX_VISIBLE_DETAILS_FINDINGS} additional low-priority notes omitted from the visible table._"
            )
        lines.append("")
        detailed_findings = [
            finding
            for finding in visible_findings
            if isinstance(finding, dict)
            and (
                include_inline_findings
                or finding.get("visibility") != "inline"
            )
        ]
        if detailed_findings:
            lines.append("### Finding details")
            for finding in detailed_findings:
                priority = _text(finding.get("priority"))
                headline = _text(finding.get("headline")).replace("\n", " ")
                lines.extend(
                    [
                        f"#### {priority} · {headline}",
                        "",
                        f"`{_code_path(finding.get('file_path'))}`",
                        "",
                        _text(finding.get("comment")),
                        "",
                        _verification_boundary(finding),
                    ]
                )
                suggested_code = clean_suggested_content(
                    finding.get("suggested_code")
                )
                if suggested_code:
                    suggestion_label = suggestion_presentation(
                        suggestion_type=finding.get("suggestion_type"),
                        code_snippet=finding.get("code_snippet"),
                        suggested_code=suggested_code,
                    )["label"]
                    lines.extend(
                        [
                            "",
                            f"**{suggestion_label}:**",
                            "",
                            _dynamic_code_fence(suggested_code),
                        ]
                    )
                lines.append("")
    unknowns = review.get("material_unknowns") or []
    if unknowns:
        visible_unknowns = unknowns[:MAX_VISIBLE_UNKNOWN_ITEMS]
        shown_actions = {
            _normalized_fold_text(text) for text in shown_action_texts
        }
        lines.append("### Material unknowns")
        for unknown in visible_unknowns:
            lines.append(f"- {_text(unknown.get('claim'))}")
            check = _text(unknown.get("how_to_check"))
            # The visible Owner action already carries this exact check; a
            # verbatim repeat in details is duplication, not audit value.
            if check and _normalized_fold_text(check) not in shown_actions:
                lines.append(f"  - Check: {check}")
        if len(unknowns) > MAX_VISIBLE_UNKNOWN_ITEMS:
            lines.append(
                f"- {len(unknowns) - MAX_VISIBLE_UNKNOWN_ITEMS} additional "
                "merge checks remain in the private review artifact."
            )
        lines.append("")
    scope = review.get("evidence_scope") or []
    if scope:
        lines.append("### LlamaPReview checks")
        seen_descriptions: set[str] = set()
        for item in scope:
            description = _text(item.get("description"))
            if not description or description in seen_descriptions:
                continue
            seen_descriptions.add(description)
            lines.append(f"- {description}")
            if len(seen_descriptions) >= MAX_VISIBLE_SCOPE_ITEMS:
                break
        lines.append("")
    lines.append("</details>")
    return "\n".join(lines)


def _format_visible_diagram(diagram: Dict[str, Any]) -> str:
    """Render an already validated, purpose-gated diagram outside details."""

    title = "Risk path" if diagram.get("purpose") == "risk_path" else "Change flow"
    lines = [f"#### {title}"]
    if _text(diagram.get("description")):
        lines.extend(["", f"*{_text(diagram.get('description'))}*"])
    lines.extend(["", _text(diagram.get("mermaid"))])
    return "\n".join(lines)


_BLOCKED_SENTENCE_PREFIXES = ("Don't merge yet — ", "Don't merge yet - ")


def _blocked_sentence_remainder(sentence: str) -> str:
    for prefix in _BLOCKED_SENTENCE_PREFIXES:
        if sentence.casefold().startswith(prefix.casefold()):
            return sentence[len(prefix) :]
    return sentence


def render_v3_markdown(review: Dict[str, Any]) -> str:
    """Render familiar Markdown without exposing internal F/U/evidence IDs.

    The first screen is a code-owned contract surface: one heading, one proof
    unit, at most one action surface, zero duplicated strings, zero template
    artifacts, zero raw machine identities. Everything below the fold is the
    audit surface.
    """

    decision = review.get("decision") or {}
    visible_verdict = _text(review.get("visible_verdict")) or "unverified"
    ci_state = _ci_public_state(review)
    ci_unresolved = ci_state.get("posture") in {
        "unresolved",
        "unrelated_supported",
    } or (
        ci_state.get("posture") == "not_observed"
        and ci_state.get("retrieval_outcome") != "not_observed"
    )
    heading = (
        "Conditional code-review clear"
        if visible_verdict == "clear" and ci_unresolved
        else _decision_heading(visible_verdict)
    )
    unknowns = [
        item
        for item in review.get("material_unknowns") or []
        if isinstance(item, dict)
    ]
    merge_unknowns = [
        item
        for item in unknowns
        if item.get("affects_merge") is True and _text(item.get("claim"))
    ]
    public_sentence = _dedupe_decision_sentence(
        decision.get("public_sentence"),
        visible_verdict=visible_verdict,
    )
    fold_lines: list[str] = []
    clear_projection_source = ""
    if visible_verdict == "clear" and ci_unresolved:
        clear_lines, clear_projection_source = _clear_first_screen_lines(
            review,
            public_sentence,
            suppress_mechanical_pass_claims=True,
        )
        # Keep the model-owned code rationale first. Exact-head CI is a
        # separate second paragraph and never replaces or upgrades that
        # judgment; any retained finding count follows both.
        fold_lines = [
            clear_lines[0],
            _ci_fact_sentence(ci_state),
            *clear_lines[1:],
        ]
    elif visible_verdict == "clear":
        fold_lines, clear_projection_source = _clear_first_screen_lines(
            review,
            public_sentence,
        )
    elif visible_verdict == "unverified":
        # One compressed explanation of what is unknown and why it gates
        # merge. The heading already says "Verification needed", so the fold
        # carries the claim itself; a >40-word claim keeps its first sentence
        # here and its full reasoning in details.
        claim = _text((merge_unknowns[0] if merge_unknowns else {}).get("claim"))
        if not claim:
            claim = public_sentence
        claim = _bounded_first_screen_explanation(claim)
        if claim and claim[0].islower():
            claim = claim[0].upper() + claim[1:]
        if claim and not claim.endswith((".", "!", "?")):
            claim += "."
        if not claim or _template_artifact_lint(claim):
            claim = "One material check still needs verification before merging."
        fold_lines = [claim]
    else:
        sentence = public_sentence
        for legacy_prefix in ("Don't merge yet - ",):
            if sentence.startswith(legacy_prefix):
                sentence = "Don't merge yet — " + sentence[len(legacy_prefix) :]
        if not sentence or _template_artifact_lint(sentence):
            sentence = _derive_public_sentence(
                visible_verdict,
                review.get("findings") or [],
                unknowns,
            )
        fold_lines = [sentence]

    if ci_unresolved and visible_verdict != "clear":
        fold_lines.append(_ci_fact_sentence(ci_state))

    lines = [f"### LlamaPReview — {heading}"]
    for fold_line in fold_lines:
        lines.extend(["", fold_line])

    fold_norms = {_normalized_fold_text(line) for line in fold_lines}
    if visible_verdict == "blocked_findings":
        fold_norms.add(
            _normalized_fold_text(_blocked_sentence_remainder(fold_lines[0]))
        )
    bullets: list[str] = []
    if visible_verdict == "blocked_findings":
        # Only finding-backed reasons may render as blocking bullets; an
        # unknown is labeled separately below so it can never read as a
        # confirmed blocker (the Games#62 shape). A bullet that duplicates
        # the visible sentence is suppressed (G1).
        finding_reasons = [
            item
            for item in decision.get("reasons") or []
            if isinstance(item, dict)
            and _text(item.get("text"))
            and all(
                str(ref).startswith("F")
                for ref in item.get("refs") or []
                if isinstance(ref, str)
            )
        ]
        for item in finding_reasons:
            text = _text(item.get("text"))
            if _normalized_fold_text(text) in fold_norms:
                continue
            bullets.append(text)
            fold_norms.add(_normalized_fold_text(text))
            if len(bullets) >= MAX_HEADLINE_FINDINGS - 1:
                break
    if bullets:
        lines.append("")
        lines.extend(f"- {bullet}" for bullet in bullets)

    actions = [
        item
        for item in review.get("owner_action") or []
        if isinstance(item, dict) and _text(item.get("text"))
    ]
    if visible_verdict == "unverified" and not actions:
        primary_unknown = next(
            (
                item
                for item in merge_unknowns
                if _text(item.get("how_to_check"))
            ),
            None,
        )
        if primary_unknown is not None:
            actions = [{"text": _text(primary_unknown.get("how_to_check"))}]
    action_cap = 1 if visible_verdict == "unverified" else MAX_OWNER_ACTIONS
    action_segments: list[str] = []
    for item in actions[:action_cap]:
        text = _text(item.get("text"))
        if _normalized_fold_text(text) in fold_norms:
            continue
        action_segments.append(text)
    shown_action_texts = tuple(action_segments)
    if action_segments:
        # Strip trailing periods from all but the final segment so a
        # multi-action join can never produce the ".;" machine artifact.
        joined = "; ".join(
            [segment.rstrip(".") for segment in action_segments[:-1]]
            + [action_segments[-1]]
        )
        lines.append("")
        lines.append(f"Owner action: {joined}")

    if visible_verdict == "blocked_findings" and merge_unknowns:
        claim = _bounded_first_screen_explanation(
            _text(merge_unknowns[0].get("claim"))
        )
        if claim[:1].isupper() and claim[1:2].islower():
            claim = claim[0].lower() + claim[1:]
        if claim and not claim.endswith((".", "!", "?")):
            claim += "."
        lines.append("")
        lines.append(f"Also needs verification: {claim}")
    elif visible_verdict == "unverified" and len(merge_unknowns) > 1:
        further = len(merge_unknowns) - 1
        plural = "" if further == 1 else "s"
        lines.append("")
        lines.append(f"{further} further check{plural} in details.")

    # G4 is a composed-surface contract, not merely a per-field check. A
    # repeated ten-word span can be harmless inside each source string yet
    # become mechanical duplication once sentence, bullet, action, and unknown
    # copy share the fold. Detect that final composition and contract to one
    # truthful state-specific proof unit, then re-admit the action only when the
    # combined surface stays clean.
    if _template_artifact_lint("\n".join(lines)):
        lines = [f"### LlamaPReview — {heading}"]
        if visible_verdict == "clear":
            retained_count = len(
                [
                    item
                    for item in review.get("findings") or []
                    if isinstance(item, dict)
                ]
            )
            if retained_count:
                plural = "" if retained_count == 1 else "s"
                safe_proof = (
                    f"{retained_count} non-blocking finding{plural} retained; "
                    "see details."
                )
            else:
                safe_proof = (
                    "Reviewed only the available changed regions; no broader "
                    "coverage is claimed."
                )
        elif visible_verdict == "blocked_findings":
            primary_blocker = next(
                (
                    item
                    for item in review.get("findings") or []
                    if isinstance(item, dict)
                    and item.get("blocking") is True
                    and _text(item.get("headline"))
                ),
                None,
            )
            blocker_text = _bounded_first_screen_explanation(
                _text((primary_blocker or {}).get("headline"))
            )
            safe_proof = blocker_text or "A supported finding blocks merge."
            if safe_proof and not safe_proof.endswith((".", "!", "?")):
                safe_proof += "."
        else:
            safe_proof = _bounded_first_screen_explanation(
                _text((merge_unknowns[0] if merge_unknowns else {}).get("claim"))
            )
            if not safe_proof or _template_artifact_lint(safe_proof):
                safe_proof = (
                    "One material check still needs verification before merging."
                )
            elif not safe_proof.endswith((".", "!", "?")):
                safe_proof += "."
        lines.extend(["", safe_proof])
        if ci_unresolved and _ci_fact_sentence(ci_state) != safe_proof:
            lines.extend(["", _ci_fact_sentence(ci_state)])
        if action_segments:
            candidate_action = f"Owner action: {action_segments[0]}"
            candidate_lines = [*lines, "", candidate_action]
            if not _template_artifact_lint("\n".join(candidate_lines)):
                lines = candidate_lines
                shown_action_texts = (action_segments[0],)
            else:
                shown_action_texts = ()

    diagram = review.get("diagram")
    if isinstance(diagram, dict):
        lines.append("")
        lines.append(_format_visible_diagram(diagram))

    scope = [
        item
        for item in review.get("evidence_scope") or []
        if isinstance(item, dict) and _text(item.get("description"))
    ]
    scope_fully_shown = bool(scope) and all(
        _normalized_fold_text(item.get("description")) in fold_norms
        for item in scope
    )
    needs_details = bool(
        review.get("findings")
        or unknowns
        or (scope and not scope_fully_shown)
    )
    if needs_details:
        lines.append("")
        lines.append(
            _format_details(review, shown_action_texts=shown_action_texts)
        )
    return "\n".join(lines).strip()


POST_MERGE_PREAMBLE = (
    "### LlamaPReview — Post-merge follow-up\n\n"
    "This review started before the pull request was merged and completed "
    "afterward. It covers the exact merged PR head below; treat the findings "
    "as follow-up work, not a merge gate."
)


def _follow_up_location_lines(
    placements: Sequence[Dict[str, Any]],
) -> list[str]:
    """Render exact placed locations without recreating inline payloads."""

    lines: list[str] = []
    ordered = sorted(
        placements,
        key=lambda item: int(item.get("follow_up_order") or 0),
    )
    for placement in ordered:
        path = _text(placement.get("path")).replace("`", "'")
        line = placement.get("line")
        if not path or not isinstance(line, int) or line <= 0:
            continue
        actions = placement.get("follow_up_actions") or []
        for action in actions:
            text = " ".join(_text(action).split())
            if text:
                lines.append(f"- `{path}:{line}` — {text}")
    return lines


def _post_merge_ci_sentence(state: Dict[str, Any]) -> str:
    counts = state.get("counts") or {}
    parts: list[str] = []
    for key, label in (
        ("failure", "failed"),
        ("action_required", "action required"),
        ("pending", "pending"),
        ("incomplete", "incomplete"),
    ):
        count = int(counts.get(key) or 0)
        if count:
            parts.append(f"{count} {label}")
    retrieval = _text(state.get("retrieval_outcome"))
    if retrieval in {"partial", "error", "unverified"}:
        parts.append(f"retrieval {retrieval}")
    observed = ", ".join(parts) or "unresolved evidence"
    posture = state.get("posture")
    if posture == "not_observed":
        if retrieval == "no_hit":
            return "Exact-head CI reported no statuses or check runs."
        return "Exact-head CI evidence was not observed."
    if posture == "unrelated_supported":
        return (
            f"Exact-head CI reports {observed}; retained exact evidence "
            "attributes those failures outside this change."
        )
    if posture != "resolved":
        return (
            f"Exact-head CI remains unresolved ({observed}); follow-up work "
            "should account for that uncertainty."
        )
    success_count = int((state.get("counts") or {}).get("success") or 0)
    if success_count:
        plural = "" if success_count == 1 else "s"
        return (
            f"Exact-head CI reports {success_count} successful check{plural} "
            "and no unresolved check state."
        )
    return "Exact-head CI reports no unresolved check state."


def render_post_merge_follow_up(
    review: Dict[str, Any],
    placements: Sequence[Dict[str, Any]],
) -> str:
    """Project a completed exact-head review as non-gating follow-up.

    The open-PR decision heading, sentence and merge posture are deliberately
    not consumed. Findings, uncertainty, owner actions, structured CI and an
    already validated diagram remain sourced from the structured review.
    """

    lines = [POST_MERGE_PREAMBLE]
    ci_state = _ci_public_state(review)
    if ci_state:
        lines.extend(["", _post_merge_ci_sentence(ci_state)])

    actions = [
        _text(item.get("text"))
        for item in review.get("owner_action") or []
        if isinstance(item, dict) and _text(item.get("text"))
    ]
    if actions:
        lines.extend(["", "### Follow-up actions"])
        lines.extend(f"- {action}" for action in actions[:MAX_OWNER_ACTIONS])

    diagram = review.get("diagram")
    if isinstance(diagram, dict):
        lines.extend(["", _format_visible_diagram(diagram)])

    location_lines = _follow_up_location_lines(placements)
    if location_lines:
        lines.extend(["", "### Follow-up locations", *location_lines])

    unknowns = [
        item
        for item in review.get("material_unknowns") or []
        if isinstance(item, dict)
    ]
    scope = [
        item
        for item in review.get("evidence_scope") or []
        if isinstance(item, dict) and _text(item.get("description"))
    ]
    if review.get("findings") or unknowns or scope:
        lines.extend(
            [
                "",
                _format_details(
                    review,
                    shown_action_texts=tuple(actions[:MAX_OWNER_ACTIONS]),
                    include_inline_findings=True,
                ),
            ]
        )
    return "\n".join(lines).strip()



def _compact_scope_phrase(
    ref: str,
    entry: Dict[str, Any],
    ci_names: Dict[str, str],
) -> str:
    """Project one evidence identity into a public-safe provenance phrase.

    Same catalog-owned facts as ``_objective_scope_description``, compressed
    to a noun phrase for inline-comment trailers. Never exposes the internal
    identifier itself.
    """

    if ref.startswith("ci:"):
        name = ci_names.get(ref[3:])
        return f"{name} check diagnostic" if name else ""
    if not _entry_supports_claim(entry):
        return ""
    paths = _entry_paths(entry)
    if not paths:
        return ""
    safe_paths = [
        path.replace("`", "'").replace("\n", " ").replace("\r", " ")
        for path in paths[:2]
    ]
    rendered = ", ".join(f"`{path}`" for path in safe_paths)
    if len(paths) > 2:
        rendered += f" and {len(paths) - 2} more"
    coverage = _text(entry.get("coverage_type"))
    if coverage == "changed_region":
        return f"changed region in {rendered}"
    if coverage == "full_file":
        return f"PR-head read of {rendered}"
    if coverage == "file_slice":
        return f"bounded PR-head context from {rendered}"
    if coverage == "search_snippet":
        return f"matching repository snippets in {rendered}"
    if coverage == "directory_inventory":
        return f"directory inventory under {rendered}"
    if coverage == "exact_path_state":
        observed = _text(entry.get("observed_state") or entry.get("exact_path_state"))
        if observed in {"present", "absent"}:
            return f"exact PR-head path {observed}: {rendered}"
    return ""


def project_v3_to_publish_json(
    review: Dict[str, Any],
    context_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    inline_findings = [
        item
        for item in review.get("findings") or []
        if isinstance(item, dict) and item.get("visibility") == "inline"
    ][:MAX_INLINE_FINDINGS]
    from .public_boundary import ci_display_names

    catalog = _catalog_entries(context_meta)
    ci_names = ci_display_names(context_meta)
    inline_comments: List[Dict[str, Any]] = []
    for finding in inline_findings:
        if not _text(finding.get("file_path")) or not _text(
            finding.get("code_snippet")
        ):
            continue
        refs = [
            str(ref)
            for ref in finding.get("evidence_refs") or []
            if isinstance(ref, str)
        ]
        phrases = list(
            dict.fromkeys(
                phrase
                for phrase in (
                    _compact_scope_phrase(ref, catalog.get(ref) or {}, ci_names)
                    for ref in refs
                )
                if phrase
            )
        )
        item = {
            "file_path": finding["file_path"],
            "code_snippet": finding["code_snippet"],
            "comment": finding["comment"],
            "priority": finding["priority"],
            "confidence": finding["confidence"],
            # Exact identities stay in v3_review; the published inline surface
            # carries only catalog-owned humanized provenance.
            "evidence_note": "; ".join(phrases[:4]),
        }
        if finding.get("suggested_code"):
            item["suggested_code"] = finding["suggested_code"]
            item["suggestion_type"] = finding.get("suggestion_type") or "CONCEPTUAL_ADVICE"
        inline_comments.append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "v3_review": review,
        "pr_review_comment": render_v3_markdown(review),
        "inline_comments": inline_comments,
        "review_quality_warnings": review.get("review_quality_warnings", []),
        "visible_projection_source": review.get("visible_projection_source"),
        "visible_verdict": review.get("visible_verdict"),
    }

"""Deterministic, section-aware context packing."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, List


TRUNCATION_MARKER = "\n[section truncated]"
CURRENT_HEAD_CI_START = "<CURRENT_HEAD_CI_SNAPSHOT>"
CURRENT_HEAD_CI_END = "</CURRENT_HEAD_CI_SNAPSHOT>"


@dataclass(frozen=True)
class ContextSection:
    name: str
    text: str
    priority: int = 50
    required: bool = False
    min_chars: int = 0


def _truncate_section(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(TRUNCATION_MARKER):
        return text[:limit]
    return text[: limit - len(TRUNCATION_MARKER)].rstrip() + TRUNCATION_MARKER


def truncate_preserving_current_ci(value: str, max_chars: int) -> str:
    """Truncate untrusted PR text while retaining the generated CI truth block.

    The Pipeline appends the bounded current-head snapshot at the end of PR
    details. Prefix truncation must not silently remove the very structured
    evidence that planner/reconciler prompts are instructed to use.
    """
    text = str(value or "")
    limit = max(0, int(max_chars))
    if len(text) <= limit:
        return text
    matches = list(
        re.finditer(
            re.escape(CURRENT_HEAD_CI_START)
            + r".*?"
            + re.escape(CURRENT_HEAD_CI_END),
            text,
            flags=re.DOTALL,
        )
    )
    if not matches:
        return _truncate_section(text, limit)
    block = matches[-1].group(0)
    if len(block) >= limit:
        return _truncate_section(block, limit)
    prefix = re.sub(
        re.escape(CURRENT_HEAD_CI_START)
        + r".*?"
        + re.escape(CURRENT_HEAD_CI_END),
        "",
        text,
        flags=re.DOTALL,
    ).replace(CURRENT_HEAD_CI_START, "").replace(CURRENT_HEAD_CI_END, "").rstrip()
    separator = "\n\n"
    prefix_budget = max(0, limit - len(block) - len(separator))
    rendered_prefix = _truncate_section(prefix, prefix_budget)
    if not rendered_prefix:
        return block[:limit]
    return f"{rendered_prefix}{separator}{block}"[:limit]


def pack_sections(sections: Iterable[ContextSection], max_chars: int) -> str:
    """Pack sections without letting snippets evict truth/health sections.

    Required sections receive their minimum reservation first. Remaining space
    is assigned by priority, while emitted order remains the caller's order so
    the model sees a stable document shape.
    """
    candidates: List[ContextSection] = [section for section in sections if section.text]
    if max_chars <= 0 or not candidates:
        return ""
    separator_chars = max(0, len(candidates) - 1) * 2
    content_budget = max(0, max_chars - separator_chars)
    allocations = [0] * len(candidates)
    required_indexes = [index for index, section in enumerate(candidates) if section.required]

    requested = [
        min(len(candidates[index].text), max(32, int(candidates[index].min_chars or 0)))
        for index in required_indexes
    ]
    requested_total = sum(requested)
    if requested_total <= content_budget:
        for index, amount in zip(required_indexes, requested):
            allocations[index] = amount
    elif required_indexes:
        # Extremely small caps still expose each required section deterministically.
        base = content_budget // len(required_indexes)
        remainder = content_budget % len(required_indexes)
        for position, index in enumerate(required_indexes):
            allocations[index] = min(len(candidates[index].text), base + (1 if position < remainder else 0))

    remaining = content_budget - sum(allocations)
    for index in sorted(range(len(candidates)), key=lambda item: (candidates[item].priority, item)):
        if remaining <= 0:
            break
        available = len(candidates[index].text) - allocations[index]
        if available <= 0:
            continue
        added = min(available, remaining)
        allocations[index] += added
        remaining -= added

    rendered = [
        _truncate_section(section.text, allocations[index])
        for index, section in enumerate(candidates)
        if allocations[index] > 0
    ]
    return "\n\n".join(rendered)[:max_chars]

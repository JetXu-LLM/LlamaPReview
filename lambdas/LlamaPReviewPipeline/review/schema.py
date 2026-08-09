"""Shared categorical contracts for planner and final review output."""

from __future__ import annotations

from difflib import SequenceMatcher
import re
import textwrap


ALLOWED_PR_TYPES = {"code", "dependency", "docs", "config", "ci", "large", "mixed"}

# One public presentation contract for a model-proposed change, shared by the
# details renderer, the inline publisher, and the unanchored fallback. Only a
# committable block may claim a language: a ``suggestion`` fence is applied by
# GitHub verbatim, while every other block may legitimately carry prose advice.
DIRECT_SUGGESTION_LABEL = "Suggested direct replacement"
CONCEPTUAL_SUGGESTION_LABEL = (
    "Conceptual guidance (not a committable GitHub suggestion)"
)

# Models commonly use these narrower labels even when the schema intentionally
# keeps all implementation work under ``code``. They are safe categorical
# aliases, not review-judgment heuristics.
PR_TYPE_ALIASES = {
    "api": "code",
    "backend": "code",
    "bug": "code",
    "bugfix": "code",
    "feature": "code",
    "fix": "code",
    "frontend": "code",
    "refactor": "code",
    "test": "code",
    "tests": "code",
    "ui": "code",
}


_UNRESOLVED_ANGLE_PLACEHOLDER_RE = re.compile(
    r"<\s*(?:"
    r"(?:full[-_ ]*)?commit[-_ ]*sha|"
    r"(?:your|replace|insert)[-_ ][A-Za-z0-9_-]+|"
    r"placeholder|todo|fixme|\.\.\."
    r")\s*>",
    re.IGNORECASE,
)
_UNRESOLVED_TOKEN_PLACEHOLDER_RE = re.compile(
    r"\b(?:TODO|FIXME|REPLACE_ME|INSERT_HERE|PLACEHOLDER|YOUR_[A-Z0-9_]+)\b"
)


def normalize_pr_type(value: object) -> str:
    raw = str(value or "").strip().lower()
    return PR_TYPE_ALIASES.get(raw, raw)


def has_unresolved_replacement_placeholder(value: object) -> bool:
    """Detect explicit stand-ins that make a one-click replacement unsafe.

    The contract deliberately avoids broad markers such as every ``...`` or
    every angle-bracket token: those can be valid Python, C++, HTML, or JSX.
    It only recognizes explicit replacement language and commit-SHA stand-ins.
    """

    if not isinstance(value, str) or not value.strip():
        return False
    return bool(
        _UNRESOLVED_ANGLE_PLACEHOLDER_RE.search(value)
        or _UNRESOLVED_TOKEN_PLACEHOLDER_RE.search(value)
    )


def is_local_direct_replacement(original: object, replacement: object) -> bool:
    """Conservatively verify that a one-click edit belongs at its anchor.

    GitHub applies a ``suggestion`` block by replacing the exact anchored lines.
    A model can diagnose a cross-file cause correctly but return the other
    file's fix as a direct replacement. That remains useful conceptual advice,
    but it is unsafe one-click code. A direct replacement must therefore cover
    the same number of lines and retain both the local text structure and enough
    of the anchored code vocabulary. False negatives only remove the one-click
    wrapper; the finding and proposed code remain visible.
    """

    if not isinstance(original, str) or not isinstance(replacement, str):
        return False
    if has_unresolved_replacement_placeholder(replacement):
        return False

    def normalized(value: str) -> str:
        lines = value.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if (
            len(lines) >= 2
            and re.fullmatch(r"```[A-Za-z0-9_+.-]*", lines[0].strip())
            and lines[-1].strip() == "```"
        ):
            lines = lines[1:-1]
        return textwrap.dedent("\n".join(line.rstrip() for line in lines)).strip()

    before = normalized(original)
    after = normalized(replacement)
    if not before or not after:
        return False
    if len(before.splitlines()) != len(after.splitlines()):
        return False

    def code_tokens(value: str) -> set[str]:
        return {
            token.lower()
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_-]*|\d+", value)
        }

    before_tokens = code_tokens(before)
    after_tokens = code_tokens(after)
    if not before_tokens or not after_tokens:
        return False
    token_union = before_tokens | after_tokens
    token_overlap = len(before_tokens & after_tokens) / len(token_union)
    return (
        SequenceMatcher(None, before, after, autojunk=False).ratio() >= 0.5
        and token_overlap >= 0.5
    )


def clean_suggested_content(value: object) -> str:
    """Remove only an outer Markdown fence and wrapper blank lines.

    All public suggestion surfaces use this same representation boundary.
    A fence-only model value therefore becomes empty everywhere, while the
    indentation and bytes of actual replacement content remain intact.
    """

    if not isinstance(value, str):
        return ""
    lines = value.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if (
        len(lines) >= 2
        and re.fullmatch(r"```[A-Za-z0-9_+.-]*", lines[0].strip())
        and lines[-1].strip() == "```"
    ):
        lines = lines[1:-1]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines)


def suggestion_presentation(
    *,
    suggestion_type: object,
    code_snippet: object = None,
    suggested_code: object = None,
) -> dict:
    """Return how one model-proposed change may be presented publicly.

    ``committable`` is the existing double gate: the model must declare a
    direct replacement and the deterministic anchor check must agree. Only that
    case earns the ``suggestion`` fence GitHub applies with one click, and only
    that case may be labelled as a direct replacement.

    Every other block is labelled non-committable guidance and keeps a neutral
    fence. Tagging it with the anchor file's language would assert that
    model-owned advice is source code in that language, which is exactly how a
    maintainer-facing sentence ends up rendered as a JSON program.
    """

    declared = str(suggestion_type or "").strip().upper()
    committable = bool(
        declared == "DIRECT_REPLACEMENT"
        and isinstance(suggested_code, str)
        and suggested_code.strip()
        and not has_unresolved_replacement_placeholder(suggested_code)
        and is_local_direct_replacement(code_snippet, suggested_code)
    )
    if committable:
        return {
            "committable": True,
            "label": DIRECT_SUGGESTION_LABEL,
            "fence_language": "suggestion",
        }
    return {
        "committable": False,
        "label": CONCEPTUAL_SUGGESTION_LABEL,
        "fence_language": "",
    }

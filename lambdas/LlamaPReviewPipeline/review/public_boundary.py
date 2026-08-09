"""Exact-identity sanitation for model-owned public prose.

The review compiler and renderer know the complete private identity set.  That
lets them remove accidental citations without a fuzzy ``ev_*``/keyword scrubber
that could damage repository identifiers or model-provided code.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Dict, Iterable, Mapping, NamedTuple, Optional

from .evidence_contract import visible_ci_check_label


_PUBLIC_PROSE_FIELDS = {
    "public_sentence",
    "text",
    "headline",
    "comment",
    "claim",
    "how_to_check",
    "description",
}
_IDENTITY_CONTAINER_KEYS = {
    "id",
    "refs",
    "resolves",
    "evidence_refs",
    "required_evidence_refs",
    "supporting_evidence_refs",
    "evidence_scope",
    "finding_refs",
}


def _nonempty_strings(values: Iterable[Any]) -> set[str]:
    return {
        str(value).strip()
        for value in values
        if isinstance(value, str) and str(value).strip()
    }


def collect_private_identities(
    review: Optional[Mapping[str, Any]],
    context_meta: Optional[Mapping[str, Any]] = None,
) -> set[str]:
    """Return exact private identities known to this compiled review."""

    identities: set[str] = set()

    def visit(value: Any, *, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                name = str(child_key)
                if name in _IDENTITY_CONTAINER_KEYS:
                    if isinstance(child, str):
                        identities.update(_nonempty_strings([child]))
                    elif isinstance(child, (list, tuple, set)):
                        identities.update(_nonempty_strings(child))
                if name in {"question_id", "evidence_ref"}:
                    identities.update(_nonempty_strings([child]))
                visit(child, key=name)
            return
        if isinstance(value, (list, tuple, set)):
            for child in value:
                visit(child, key=key)

    visit(review or {})
    meta = context_meta or {}
    for key in (
        "evidence_catalog",
        "ci_evidence_catalog",
        "evidence_ledger",
    ):
        visit(meta.get(key) or [])
    return identities


def ci_display_names(
    context_meta: Optional[Mapping[str, Any]],
) -> Dict[str, str]:
    """Map exact CI check identities to their sanitized display names."""

    snapshot = (context_meta or {}).get("ci_snapshot") or {}
    checks = snapshot.get("checks") if isinstance(snapshot, dict) else []
    names: Dict[str, str] = {}
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict):
            continue
        identity = str(check.get("identity") or "").strip()
        name = str(check.get("name") or "").strip()
        if identity and name:
            names[identity] = visible_ci_check_label(name)
    return names


_CI_RUN_ID_RE = re.compile(r"([0-9]{8,})\s*$")
_URL_START_RE = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|mailto:|www\.)",
    re.IGNORECASE,
)
_REFERENCE_DEFINITION_RE = re.compile(
    r"^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*",
    re.MULTILINE,
)
_EMAIL_AUTOLINK_RE = re.compile(
    r"<([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)>"
)
_URL_TRAILING_PROSE = ".,;:!?"


class _PublicUrlAtom(NamedTuple):
    """One URL plus any Markdown/autolink wrapper that owns it."""

    start: int
    end: int
    url_start: int
    url_end: int
    kind: str
    label_start: Optional[int] = None
    label_end: Optional[int] = None


def _identity_replacements(
    identities: Iterable[str],
    context_meta: Optional[Mapping[str, Any]],
) -> Dict[str, str]:
    """Choose the public-safe replacement per exact private identity (G5).

    A CI identity cited in prose becomes the check's human display name so
    the product's signature cite-don't-duplicate pattern survives without
    machine vocabulary; every other private identity stays a neutral
    "the cited evidence". Long numeric run-id tails are registered as
    aliases so a bare "(89663657479)" also leaves public prose.
    """

    names = ci_display_names(context_meta)
    replacements: Dict[str, str] = {}
    for identity in _nonempty_strings(identities):
        if not identity.startswith("ci:"):
            continue
        name = names.get(identity[3:])
        replacement = f"the {name} check" if name else "the cited check"
        replacements[identity] = replacement
        numeric = _CI_RUN_ID_RE.search(identity)
        if numeric:
            replacements.setdefault(numeric.group(1), "the cited check")
    return replacements


def _identity_pattern(identities: Iterable[str]) -> Optional[re.Pattern[str]]:
    ordered = sorted(
        _nonempty_strings(identities),
        key=lambda item: (-len(item), item),
    )
    if not ordered:
        return None
    alternatives = "|".join(re.escape(item) for item in ordered)
    return re.compile(
        rf"(?<![A-Za-z0-9_])(?:{alternatives})(?![A-Za-z0-9_])"
    )


def contains_private_identity(text: Any, identities: Iterable[str]) -> bool:
    if not isinstance(text, str) or not text:
        return False
    pattern = _identity_pattern(identities)
    return bool(pattern and pattern.search(text))


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return bool(backslashes % 2)


def _scan_url_end(
    text: str,
    start: int,
    *,
    limit: Optional[int] = None,
    trim_trailing_prose: bool = True,
) -> int:
    """Return the end of a URL while retaining balanced URL punctuation."""

    stop = len(text) if limit is None else min(len(text), limit)
    cursor = start
    depths = {"(": 0, "[": 0, "{": 0}
    closing = {")": "(", "]": "[", "}": "{"}
    while cursor < stop:
        char = text[cursor]
        if char == "\\" and cursor + 1 < stop:
            # CommonMark permits backslash-escaped punctuation inside link
            # destinations. Keep both bytes inside the URL atom; otherwise an
            # escaped ``\)`` can terminate the scan early and leave a private
            # identity in the remaining clickable destination to be rewritten
            # piecemeal.
            cursor += 2
            continue
        if char.isspace() or ord(char) < 0x20 or char in '<>`"\'|':
            break
        if char in depths:
            depths[char] += 1
        elif char in closing:
            opener = closing[char]
            if depths[opener] <= 0:
                break
            depths[opener] -= 1
        cursor += 1

    # Sentence punctuation is not part of a bare URL. Query/fragment delimiters
    # inside the address are retained; only a trailing prose mark is detached.
    if trim_trailing_prose:
        while cursor > start and text[cursor - 1] in _URL_TRAILING_PROSE:
            cursor -= 1
    return cursor


def _matching_bracket(text: str, start: int) -> Optional[int]:
    depth = 1
    cursor = start + 1
    while cursor < len(text):
        char = text[cursor]
        if char == "\\" and cursor + 1 < len(text):
            cursor += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return None


def _matching_link_paren(text: str, start: int) -> Optional[int]:
    depth = 1
    quote = ""
    cursor = start + 1
    while cursor < len(text):
        char = text[cursor]
        if char == "\\" and cursor + 1 < len(text):
            cursor += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return None


def _markdown_url_atoms(text: str) -> list[_PublicUrlAtom]:
    """Recognize inline Markdown links without treating their label as a URL."""

    atoms: list[_PublicUrlAtom] = []
    cursor = 0
    while cursor < len(text):
        label_open = text.find("[", cursor)
        if label_open < 0:
            break
        if _is_escaped(text, label_open):
            cursor = label_open + 1
            continue
        label_close = _matching_bracket(text, label_open)
        if label_close is None:
            break
        open_paren = label_close + 1
        while open_paren < len(text) and text[open_paren] in " \t":
            open_paren += 1
        if open_paren >= len(text) or text[open_paren] != "(":
            cursor = label_close + 1
            continue
        close_paren = _matching_link_paren(text, open_paren)
        if close_paren is None:
            cursor = open_paren + 1
            continue

        destination = open_paren + 1
        while destination < close_paren and text[destination] in " \t":
            destination += 1
        angled = destination < close_paren and text[destination] == "<"
        url_start = destination + 1 if angled else destination
        if angled:
            angle_end = text.find(">", url_start, close_paren)
            if angle_end < 0:
                cursor = close_paren + 1
                continue
            url_end = _scan_url_end(
                text,
                url_start,
                limit=angle_end,
                trim_trailing_prose=False,
            )
            if url_end != angle_end:
                cursor = close_paren + 1
                continue
        else:
            url_end = _scan_url_end(
                text,
                url_start,
                limit=close_paren,
                trim_trailing_prose=False,
            )
        # Markdown destinations may be absolute URLs, mail addresses,
        # fragments, or repository-relative paths. They all remain atomic:
        # rewriting an identity inside any of them would leave a misleading
        # clickable target.
        if url_end <= url_start:
            cursor = close_paren + 1
            continue

        atom_start = (
            label_open - 1
            if label_open > 0
            and text[label_open - 1] == "!"
            and not _is_escaped(text, label_open - 1)
            else label_open
        )
        atoms.append(
            _PublicUrlAtom(
                start=atom_start,
                end=close_paren + 1,
                url_start=url_start,
                url_end=url_end,
                kind="markdown",
                label_start=label_open + 1,
                label_end=label_close,
            )
        )
        cursor = close_paren + 1
    return atoms


def _reference_definition_atoms(text: str) -> list[_PublicUrlAtom]:
    """Recognize CommonMark-style link definitions as whole atomic lines."""

    atoms: list[_PublicUrlAtom] = []
    for match in _REFERENCE_DEFINITION_RE.finditer(text):
        line_end = text.find("\n", match.end())
        if line_end < 0:
            line_end = len(text)
        destination = match.end()
        if destination >= line_end:
            continue
        angled = text[destination] == "<"
        url_start = destination + 1 if angled else destination
        if angled:
            angle_end = text.find(">", url_start, line_end)
            if angle_end < 0:
                continue
            url_end = _scan_url_end(
                text,
                url_start,
                limit=angle_end,
                trim_trailing_prose=False,
            )
            if url_end != angle_end:
                continue
        else:
            url_end = _scan_url_end(
                text,
                url_start,
                limit=line_end,
                trim_trailing_prose=False,
            )
        if url_end <= url_start:
            continue
        atoms.append(
            _PublicUrlAtom(
                start=match.start(),
                end=line_end,
                url_start=url_start,
                url_end=url_end,
                kind="reference_definition",
                label_start=match.start(1),
                label_end=match.end(1),
            )
        )
    return atoms


def _email_autolink_atoms(text: str) -> list[_PublicUrlAtom]:
    """Recognize RFC-style Markdown email autolinks without a scheme."""

    return [
        _PublicUrlAtom(
            start=match.start(),
            end=match.end(),
            url_start=match.start(1),
            url_end=match.end(1),
            kind="autolink",
        )
        for match in _EMAIL_AUTOLINK_RE.finditer(text)
    ]


def _public_url_atoms(text: str) -> list[_PublicUrlAtom]:
    """Return non-overlapping Markdown, autolink, and bare URL atoms."""

    structured = [
        *_markdown_url_atoms(text),
        *_reference_definition_atoms(text),
        *_email_autolink_atoms(text),
    ]
    structured.sort(key=lambda atom: (atom.start, atom.end))
    atoms: list[_PublicUrlAtom] = []
    occupied: list[tuple[int, int]] = []
    for atom in structured:
        if any(
            start < atom.end and atom.start < end
            for start, end in occupied
        ):
            continue
        atoms.append(atom)
        occupied.append((atom.start, atom.end))
    cursor = 0
    while cursor < len(text):
        match = _URL_START_RE.search(text, cursor)
        if match is None:
            break
        containing = next(
            (
                (start, end)
                for start, end in occupied
                if start <= match.start() < end
            ),
            None,
        )
        if containing is not None:
            cursor = containing[1]
            continue
        url_start = match.start()
        angle_end = (
            text.find(">", url_start)
            if url_start > 0 and text[url_start - 1] == "<"
            else -1
        )
        if angle_end >= 0:
            url_end = _scan_url_end(
                text,
                url_start,
                limit=angle_end,
                trim_trailing_prose=False,
            )
        else:
            url_end = _scan_url_end(text, url_start)
        if url_end <= match.end():
            cursor = match.end()
            continue
        if (
            url_start > 0
            and text[url_start - 1] == "<"
            and url_end < len(text)
            and text[url_end] == ">"
        ):
            atom = _PublicUrlAtom(
                start=url_start - 1,
                end=url_end + 1,
                url_start=url_start,
                url_end=url_end,
                kind="autolink",
            )
        else:
            atom = _PublicUrlAtom(
                start=url_start,
                end=url_end,
                url_start=url_start,
                url_end=url_end,
                kind="bare",
            )
        atoms.append(atom)
        occupied.append((atom.start, atom.end))
        cursor = atom.end
    atoms.sort(key=lambda atom: (atom.start, atom.end))
    return atoms


def sanitize_public_prose(
    text: Any,
    identities: Iterable[str],
    *,
    replacements: Optional[Mapping[str, str]] = None,
) -> tuple[str, int]:
    """Remove exact private citations from model-authored public prose."""

    if not isinstance(text, str) or not text:
        return str(text or ""), 0
    exact_identities = _nonempty_strings(identities)
    identity_set = set(exact_identities)
    replacement_map = dict(replacements or {})
    identity_set |= set(replacement_map)
    pattern = _identity_pattern(identity_set)
    exact_pattern = _identity_pattern(exact_identities)
    if pattern is None:
        return text, 0

    total = 0

    def sanitize_segment(segment: str) -> tuple[str, int]:
        count = 0

        def strip_citation_group(match: re.Match[str]) -> str:
            nonlocal count
            raw = match.group(0)
            inner = raw[1:-1]
            tokens = [
                token.strip().strip("`")
                for token in re.split(r"[,;|\s]+", inner)
                if token.strip().strip("`")
            ]
            if tokens and all(token in identity_set for token in tokens):
                count += len(tokens)
                public_ci_labels = list(
                    dict.fromkeys(
                        replacement_map[token]
                        for token in tokens
                        if token in replacement_map
                    )
                )
                if public_ci_labels:
                    return ", ".join(public_ci_labels)
                return ""
            return raw

        sanitized = re.sub(
            r"\([^()\n]*\)|\[[^\[\]\n]*\]",
            strip_citation_group,
            segment,
        )

        def replace_identity(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            return replacement_map.get(match.group(0), "the cited evidence")

        sanitized = pattern.sub(replace_identity, sanitized)
        sanitized = re.sub(
            r"`\s*the cited evidence\s*`",
            "the cited evidence",
            sanitized,
        )
        sanitized = re.sub(
            r"`(the )(`[^`]+`)( check)`",
            r"\1\2\3",
            sanitized,
        )
        sanitized = re.sub(
            r"`(the cited (?:evidence|check))`",
            r"\1",
            sanitized,
        )
        sanitized = re.sub(r"[ \t]{2,}", " ", sanitized)
        sanitized = re.sub(r"\s+([,.;:!?])", r"\1", sanitized)
        return sanitized, count

    def replacement_for(matches: list[re.Match[str]]) -> str:
        labels = list(
            dict.fromkeys(
                replacement_map.get(match.group(0), "the cited evidence")
                for match in matches
            )
        )
        return labels[0] if len(labels) == 1 else "the cited evidence"

    def sanitize_link_label(label: str) -> tuple[str, int]:
        # A label can itself look like a URL. Reuse the same URL-aware boundary
        # so preserving a safe destination never corrupts a displayed public
        # address in the label.
        return sanitize_public_prose(
            label,
            exact_identities,
            replacements=replacement_map,
        )

    # Fenced blocks inside a comment/description are still model-authored
    # public prose. Sanitize them too. Repository-proven ``code_snippet`` is a
    # separate field and intentionally never enters this function.
    #
    # URLs are handled before ordinary identity replacement. Safe URLs are
    # copied byte-for-byte so a numeric CI alias cannot corrupt a public link.
    # If the URL itself embeds an exact private identity, its whole clickable
    # atom is removed: Markdown keeps only a plain sanitized label, autolinks
    # lose ``<>``, and bare URLs become neutral prose.
    pieces: list[str] = []
    cursor = 0
    for atom in _public_url_atoms(text):
        if atom.start < cursor:
            continue
        segment, count = sanitize_segment(text[cursor : atom.start])
        pieces.append(segment)
        total += count

        url = text[atom.url_start : atom.url_end]
        identity_surface = (
            text[atom.start : atom.end]
            if atom.kind == "reference_definition"
            else url
        )
        exact_matches = (
            list(exact_pattern.finditer(identity_surface))
            if exact_pattern is not None
            else []
        )
        if exact_matches:
            total += len(exact_matches)
            fallback_label = replacement_for(exact_matches)
            if atom.kind == "reference_definition":
                # A link definition has no independently visible label. Drop
                # the whole line so no partially rewritten clickable target or
                # private reference key survives.
                pieces.append("")
            elif (
                atom.kind == "markdown"
                and atom.label_start is not None
                and atom.label_end is not None
            ):
                label, label_count = sanitize_link_label(
                    text[atom.label_start : atom.label_end]
                )
                total += label_count
                pieces.append(label.strip() or fallback_label)
            else:
                pieces.append(fallback_label)
            cursor = atom.end
            continue

        if (
            atom.kind == "markdown"
            and atom.label_start is not None
            and atom.label_end is not None
        ):
            # The destination is protected, but the human-facing label and an
            # optional title remain ordinary model prose and are sanitized.
            pieces.append(text[atom.start : atom.label_start])
            label, label_count = sanitize_link_label(
                text[atom.label_start : atom.label_end]
            )
            pieces.append(label)
            total += label_count
            pieces.append(text[atom.label_end : atom.url_start])
            pieces.append(url)
            suffix, suffix_count = sanitize_segment(
                text[atom.url_end : atom.end]
            )
            pieces.append(suffix)
            total += suffix_count
        else:
            pieces.append(text[atom.start : atom.end])
        cursor = atom.end

    segment, count = sanitize_segment(text[cursor:])
    pieces.append(segment)
    total += count
    sanitized = "".join(pieces)
    return sanitized.strip(), total


def sanitize_review_for_publication(
    review: Mapping[str, Any],
    *,
    context_meta: Optional[Mapping[str, Any]] = None,
    extra_private_identities: Iterable[str] = (),
) -> tuple[Dict[str, Any], int]:
    """Return a public-safe copy while preserving only repository-owned code."""

    working = deepcopy(dict(review))
    identities = collect_private_identities(working, context_meta)
    identities.update(_nonempty_strings(extra_private_identities))
    replacements = _identity_replacements(identities, context_meta)
    total = 0

    def visit(value: Any, *, key: str = "") -> Any:
        nonlocal total
        if isinstance(value, dict):
            sanitized = {
                child_key: visit(child, key=str(child_key))
                for child_key, child in value.items()
            }
            suggested = sanitized.get("suggested_code")
            if isinstance(suggested, str) and contains_private_identity(
                suggested, identities
            ):
                # Suggested code is model-authored and is not repository
                # provenance. Omit the optional suggestion atomically rather
                # than rewriting executable text.
                sanitized.pop("suggested_code", None)
                sanitized.pop("suggestion_type", None)
                total += 1
            return sanitized
        if isinstance(value, list):
            return [visit(child, key=key) for child in value]
        if isinstance(value, str) and key in _PUBLIC_PROSE_FIELDS:
            sanitized, count = sanitize_public_prose(
                value,
                identities,
                replacements=replacements,
            )
            total += count
            return sanitized
        return value

    diagram = working.get("diagram")
    if isinstance(diagram, dict) and contains_private_identity(
        diagram.get("mermaid"), identities
    ):
        working["diagram"] = None
        total += 1
    working = visit(working)
    return working, total

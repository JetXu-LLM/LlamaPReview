"""Parser-proven JSON object representation normalization.

This module repairs only unambiguous delimiters and wrappers.  It never changes
keys, scalar values, array membership, or object membership, and it has no
model or JSON Patch dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping, Optional


MAX_DELIMITER_EDITS = 5


@dataclass(frozen=True, slots=True)
class LocalJSONNormalization:
    """One object recovered using representation-only deterministic edits."""

    value: Mapping[str, Any]
    normalized_text: str
    actions: tuple[str, ...]


def _strip_exact_markdown_fence(raw_text: str) -> tuple[str, bool]:
    lines = raw_text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if (
        len(lines) >= 2
        and re.fullmatch(r"```(?:json)?", lines[0].strip(), re.IGNORECASE)
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]), True
    return raw_text, False


def _previous_nonspace(text: str, position: int) -> tuple[int, str]:
    index = min(position - 1, len(text) - 1)
    while index >= 0 and text[index].isspace():
        index -= 1
    return index, text[index] if index >= 0 else ""


def _next_nonspace(text: str, position: int) -> tuple[int, str]:
    index = max(0, position)
    while index < len(text) and text[index].isspace():
        index += 1
    return index, text[index] if index < len(text) else ""


def _is_escaped(text: str, position: int) -> bool:
    backslashes = 0
    index = position - 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _mismatched_closer_replacement(
    text: str,
    error: json.JSONDecodeError,
) -> Optional[str]:
    """Return the stack-required closer for one parser-point mismatch."""

    if not 0 <= error.pos < len(text) or text[error.pos] not in "}]":
        return None
    stack: list[str] = []
    in_string = False
    for index, char in enumerate(text[: error.pos]):
        if char == '"' and not _is_escaped(text, index):
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "[{":
            stack.append(char)
            continue
        if char in "]}":
            if not stack:
                return None
            expected = "]" if stack[-1] == "[" else "}"
            if char != expected:
                return None
            stack.pop()
    if in_string or not stack:
        return None
    expected = "]" if stack[-1] == "[" else "}"
    return expected if text[error.pos] != expected else None


def _unescaped_inner_quote_index(
    text: str,
    error: json.JSONDecodeError,
) -> Optional[int]:
    """Locate an internal quote exposed by the JSON parser.

    Final occasionally emits natural-language quotation marks without JSON
    escaping them.  The parser then stops on the first bare word after that
    quote.  Escaping only the immediately preceding quote preserves the model
    text; a later full-object parse still has to prove the repair coherent.
    """

    if error.msg != "Expecting ',' delimiter":
        return None
    quote_index, previous = _previous_nonspace(text, error.pos)
    if previous != '"' or _is_escaped(text, quote_index):
        return None
    if not 0 <= error.pos < len(text):
        return None
    return quote_index if re.match(r"[^\W\d_]", text[error.pos]) else None


def _unescaped_empty_quote_pair(
    text: str,
    error: json.JSONDecodeError,
) -> Optional[tuple[int, int]]:
    """Locate one bare ``""`` pair inside the active JSON string.

    Natural-language code fragments such as ``unitName=""`` occasionally
    reach Final's otherwise complete JSON without escaping either quote.  The
    parser points at the second quote after treating the first as the end of
    the surrounding scalar.  Escape the pair only at that exact parser point,
    while the first quote is provably inside a JSON string; the caller still
    requires the complete object to parse after the edit.
    """

    second = error.pos
    first = second - 1
    if (
        error.msg != "Expecting ',' delimiter"
        or first < 0
        or second >= len(text)
        or text[first : second + 1] != '""'
        or _is_escaped(text, first)
        or _is_escaped(text, second)
    ):
        return None
    in_string = False
    for index, char in enumerate(text[:first]):
        if char == '"' and not _is_escaped(text, index):
            in_string = not in_string
    if not in_string:
        return None
    return first, second


_TRAILING_COMMA_ERROR_MESSAGES = {
    "Expecting property name enclosed in double quotes",
    "Expecting value",
    "Illegal trailing comma before end of array",
    "Illegal trailing comma before end of object",
}


def _trailing_comma_index(
    text: str,
    error: json.JSONDecodeError,
) -> Optional[int]:
    if error.msg not in _TRAILING_COMMA_ERROR_MESSAGES or not text:
        return None
    position = min(max(error.pos, 0), len(text) - 1)
    if text[position] == ",":
        comma_index = position
    else:
        comma_index, previous = _previous_nonspace(text, position)
        if previous != ",":
            return None
    _, following = _next_nonspace(text, comma_index + 1)
    return comma_index if following in {"}", "]"} else None


def normalize_json_object_representation(
    raw_text: str,
    *,
    max_delimiter_edits: int = MAX_DELIMITER_EDITS,
) -> Optional[LocalJSONNormalization]:
    """Recover one object using only parser-proven wrapper/delimiter edits."""

    if not isinstance(raw_text, str):
        raise TypeError("raw_text must be a string")
    if (
        type(max_delimiter_edits) is not int
        or not 0 <= max_delimiter_edits <= 20
    ):
        raise ValueError(
            "max_delimiter_edits must be an integer from 0 through 20"
        )
    working, stripped_fence = _strip_exact_markdown_fence(raw_text)
    actions: list[str] = (
        ["json_outer_fence_removed"] if stripped_fence else []
    )
    delimiter_edits = 0

    while True:
        try:
            parsed = json.loads(working)
            break
        except json.JSONDecodeError as error:
            if error.msg.startswith("Invalid control character"):
                try:
                    parsed_with_controls = json.loads(working, strict=False)
                except json.JSONDecodeError:
                    return None
                if not isinstance(parsed_with_controls, (dict, list)):
                    return None
                parsed = parsed_with_controls
                working = json.dumps(
                    parsed,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                actions.append("json_literal_control_character_escaped")
                break
            if delimiter_edits >= max_delimiter_edits:
                return None
            empty_quote_pair = _unescaped_empty_quote_pair(working, error)
            if empty_quote_pair is not None:
                first, second = empty_quote_pair
                working = (
                    working[:first]
                    + '\\"\\"'
                    + working[second + 1 :]
                )
                actions.append("json_unescaped_empty_quote_pair_escaped")
                delimiter_edits += 1
                continue
            inner_quote_index = _unescaped_inner_quote_index(
                working,
                error,
            )
            if inner_quote_index is not None:
                working = (
                    working[:inner_quote_index]
                    + "\\"
                    + working[inner_quote_index:]
                )
                actions.append("json_unescaped_inner_quote_escaped")
                delimiter_edits += 1
                continue
            required_closer = _mismatched_closer_replacement(
                working,
                error,
            )
            if required_closer is not None:
                working = (
                    working[: error.pos]
                    + required_closer
                    + working[error.pos + 1 :]
                )
                actions.append("json_mismatched_closing_delimiter_replaced")
                delimiter_edits += 1
                continue
            if (
                error.msg == "Invalid \\escape"
                and error.pos + 1 < len(working)
                and working[error.pos] == "\\"
                and working[error.pos + 1] not in '"\\/bfnrtu'
            ):
                # JSON has no interpretation for this escape. Encoding the
                # existing backslash as a literal backslash preserves the
                # exact model string instead of losing the complete object.
                working = (
                    working[: error.pos]
                    + "\\"
                    + working[error.pos :]
                )
                actions.append("json_invalid_escape_escaped")
                delimiter_edits += 1
                continue
            if error.msg == "Extra data":
                try:
                    prefix, prefix_end = json.JSONDecoder().raw_decode(
                        working
                    )
                except json.JSONDecodeError:
                    return None
                suffix = working[prefix_end:]
                if isinstance(prefix, dict) and suffix.strip() == "}":
                    working = (
                        working[:prefix_end]
                        + suffix.replace("}", "", 1)
                    )
                    actions.append(
                        "json_extra_closing_delimiter_removed"
                    )
                    delimiter_edits += 1
                    continue
                return None
            _, previous = _previous_nonspace(working, error.pos)
            next_index, following = _next_nonspace(
                working,
                error.pos,
            )
            comma_index = _trailing_comma_index(working, error)
            if comma_index is not None:
                working = (
                    working[:comma_index] + working[comma_index + 1 :]
                )
                actions.append("json_trailing_comma_removed")
                delimiter_edits += 1
                continue
            if error.msg == "Expecting ',' delimiter" and (
                previous in {'"', "}", "]"}
                or previous.isdigit()
                or previous in {"e", "l"}
            ) and following in {
                '"',
                "{",
                "[",
                "-",
                "t",
                "f",
                "n",
                *"0123456789",
            }:
                working = (
                    working[:next_index] + "," + working[next_index:]
                )
                actions.append("json_missing_comma_inserted")
                delimiter_edits += 1
                continue
            return None

    if (
        isinstance(parsed, list)
        and len(parsed) == 1
        and isinstance(parsed[0], dict)
    ):
        parsed = parsed[0]
        actions.append("json_single_object_array_unwrapped")
    if not isinstance(parsed, dict):
        return None
    return LocalJSONNormalization(
        value=parsed,
        normalized_text=working,
        actions=tuple(actions),
    )

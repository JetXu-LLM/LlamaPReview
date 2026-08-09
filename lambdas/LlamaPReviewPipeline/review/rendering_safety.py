"""Strict Mermaid validation and GitHub-safe rendering."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set, Tuple

__all__ = ["format_mermaid", "validate_sequence_mermaid_offline"]

ALLOWED_ARROWS_SUPERSET = {
    "->", "-->", "->>", "-->>", "-x", "--x",
    "-)", "--)", ")-", ")--", "-o", "--o", "o-", "o--"
}

RE_COMMENT_FULL = re.compile(r'^\s*%%(?!\{).*')
RE_DIRECTIVE = re.compile(r'^\s*%%\{.*?\}%%\s*$')
RE_SEQUENCE_START = re.compile(r'^\s*sequenceDiagram\s*$', re.IGNORECASE)

RE_PARTICIPANT = re.compile(
    r'^\s*(participant|actor)\s+(".*?"|[A-Za-z0-9_]+)'
    r'(?:\s+as\s+(".*?"|.+))?\s*$',
    re.IGNORECASE
)

RE_AUTONUM = re.compile(r'^\s*autonumber(\s+(off|resume))?\s*$', re.IGNORECASE)
RE_TITLE = re.compile(r'^\s*title\s+.+$', re.IGNORECASE)
RE_ACTIVATE = re.compile(r'^\s*activate\s+(".*?"|[A-Za-z0-9_]+)\s*$', re.IGNORECASE)
RE_DEACTIVATE = re.compile(r'^\s*deactivate\s+(".*?"|[A-Za-z0-9_]+)\s*$', re.IGNORECASE)
RE_CREATE = re.compile(r'^\s*create\s+(".*?"|[A-Za-z0-9_]+)\s*$', re.IGNORECASE)
RE_DESTROY = re.compile(r'^\s*destroy\s+(".*?"|[A-Za-z0-9_]+)\s*$', re.IGNORECASE)

RE_BLOCK_START = re.compile(r'^\s*(alt|opt|loop|par|critical|break|rect|box)\b.*$', re.IGNORECASE)
RE_BLOCK_END = re.compile(r'^\s*end\s*$', re.IGNORECASE)
RE_SINGLE_UNCLOSED_BLOCK = re.compile(
    r"^Unclosed block '(?:alt|opt|loop|par|critical|break|rect|box)' "
    r"\(line \d+\)\.$",
    re.IGNORECASE,
)
RE_ELSE = re.compile(r'^\s*else\b.*$', re.IGNORECASE)
RE_AND = re.compile(r'^\s*and\b.*$', re.IGNORECASE)

RE_NOTE_MULTI_START = re.compile(r'^\s*note\s+(over|left|right|of)\b(?!.*:).*$', re.IGNORECASE)
RE_NOTE_MULTI_END = re.compile(r'^\s*end\s+note\s*$', re.IGNORECASE)
RE_NOTE_SINGLE = re.compile(r'^\s*note\s+(over|left|right|of)\b.*?:.*$', re.IGNORECASE)

RE_MESSAGE = re.compile(
    # r'^\s*(".*?"|[A-Za-z0-9_]+)\s*([\-o>x\)]+)\s*(".*?"|[A-Za-z0-9_]+)\s*(?::\s*(.*))?$'
    r'^\s*(".*?"|[A-Za-z0-9_]+)\s*([\-o>x\)]+)\s*(".*?"|[A-Za-z0-9_]+)\s*(?::\s*)?(.*)$'
)

# Mermaid treats semicolons as statement separators, even inside sequence
# diagram labels. Preserve existing entity codes while encoding literal text
# characters so GitHub never receives an otherwise-valid diagram that its
# Mermaid parser splits into two statements.
RE_MERMAID_ENTITY = re.compile(
    r"(?:#(?:35|59)|#[A-Za-z][A-Za-z0-9]+|&(?:#[0-9]+|[A-Za-z][A-Za-z0-9]+));"
)


def _escape_github_mermaid_text(value: str) -> str:
    def escape_literal_characters(segment: str) -> str:
        return re.sub(
            r"[#;]",
            lambda match: "#35;" if match.group(0) == "#" else "#59;",
            segment,
        )

    parts: List[str] = []
    start = 0
    for match in RE_MERMAID_ENTITY.finditer(value):
        parts.append(escape_literal_characters(value[start : match.start()]))
        parts.append(match.group(0))
        start = match.end()
    parts.append(escape_literal_characters(value[start:]))
    return "".join(parts)

def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s

def _inside_block(stack: List[Dict[str, Any]], types: Set[str]) -> bool:
    for b in reversed(stack):
        if b["type"] in types:
            return True
    return False

def _nearest_block(stack: List[Dict[str, Any]], t: str):
    for b in reversed(stack):
        if b["type"] == t:
            return b
    return None

def _mark_branch_content(stack: List[Dict[str, Any]]):
    if stack:
        top = stack[-1]
        if top.get("type") == "par":
            branches = top.setdefault("branches", [False])
            if not branches:
                branches.append(False)
            branches[-1] = True

def _mark_content(stack: List[Dict[str, Any]]):
    if stack:
        stack[-1]["has_content"] = True
    _mark_branch_content(stack)

def validate_sequence_mermaid_offline(
    diagram: str,
    *,
    strict: bool = True,
    require_explicit_participants: bool = False,
    treat_unknown_as_error: bool = False,
    github_flavor: bool = True,
    max_lines: int = 5000,
    max_bytes: int = 300_000
) -> Dict[str, Any]:
    res = {
        "is_valid": False,
        "errors": [],
        "warnings": [],
        "details": {
            "participants": [],
            "implicit_participants": [],
            "created": [],
            "destroyed": [],
            "open_blocks": [],
            "unknown_lines": [],
            "stats": {}
        }
    }

    if not isinstance(diagram, str):
        res["errors"].append("Input is not a string.")
        return res

    byte_len = len(diagram.encode('utf-8', errors='ignore'))
    if byte_len > max_bytes:
        res["errors"].append(f"Diagram size exceeds {max_bytes} bytes.")
        return res

    lines = diagram.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if len(lines) > max_lines:
        res["errors"].append(f"Diagram line count exceeds limit {max_lines}.")
        return res

    participants: Dict[str, int] = {}
    implicit: Set[str] = set()
    created: Set[str] = set()
    destroyed: Set[str] = set()

    in_sequence = False
    block_stack: List[Dict[str, Any]] = []
    in_multiline_note = False
    multiline_note_start = -1
    activation_stack: List[str] = []
    unknown_lines: List[Tuple[int, str]] = []
    first_meaningful_encountered = False

    for idx, raw in enumerate(lines, 1):
        line = raw.rstrip()
        stripped = line.strip()
        if stripped == '':
            continue
        if RE_COMMENT_FULL.match(stripped) or RE_DIRECTIVE.match(stripped):
            continue

        if not first_meaningful_encountered:
            first_meaningful_encountered = True
            if RE_SEQUENCE_START.match(stripped):
                in_sequence = True
                continue
            else:
                if github_flavor:
                    res["errors"].append(
                        "First non-comment line must be 'sequenceDiagram' for GitHub rendering."
                    )
                else:
                    res["warnings"].append(
                        f"Line {idx}: content before 'sequenceDiagram' tolerated (lenient mode)."
                    )
        if not in_sequence and not github_flavor:
            if RE_SEQUENCE_START.search(stripped):
                in_sequence = True
                continue
            else:
                res["warnings"].append(
                    f"Line {idx}: extra content before sequenceDiagram (lenient)."
                )
                continue

        if github_flavor and not in_sequence:
            continue

        if in_multiline_note:
            if RE_NOTE_MULTI_END.match(stripped):
                in_multiline_note = False
            continue

        if stripped.startswith("```"):
            res["errors"].append(f"Line {idx}: unexpected code fence ``` outside note.")
            continue

        if RE_NOTE_SINGLE.match(stripped):
            _mark_content(block_stack)
            continue
        if RE_NOTE_MULTI_START.match(stripped):
            in_multiline_note = True
            multiline_note_start = idx
            _mark_content(block_stack)
            continue
        if RE_NOTE_MULTI_END.match(stripped):
            res["errors"].append(f"Line {idx}: 'end note' without matching multi-line note start.")
            continue

        m_part = RE_PARTICIPANT.match(stripped)
        if m_part:
            left_raw = m_part.group(2)
            alias_raw = m_part.group(3)
            if alias_raw:
                left_quoted = left_raw.startswith('"') and left_raw.endswith('"')
                alias_quoted = alias_raw.startswith('"') and alias_raw.endswith('"')
                left_clean = _strip_quotes(left_raw)
                alias_clean = _strip_quotes(alias_raw)
                if left_quoted and not alias_quoted:
                    pid = alias_clean
                else:
                    pid = left_clean
            else:
                pid = _strip_quotes(left_raw)
            participants[pid] = participants.get(pid, 0) + 1
            continue

        if RE_AUTONUM.match(stripped):
            continue
        if RE_TITLE.match(stripped):
            continue

        if RE_BLOCK_START.match(stripped):
            block_type = stripped.split()[0].lower()
            _mark_branch_content(block_stack)
            block_stack.append({
                "type": block_type,
                "line": idx,
                "has_content": False,
                "else_count": 0,
                "and_count": 0,
                "branches": [False] if block_type == "par" else None
            })
            continue

        if RE_BLOCK_END.match(stripped):
            if not block_stack:
                res["errors"].append(f"Line {idx}: stray 'end' without matching block start.")
            else:
                b = block_stack.pop()
                if strict and not b["has_content"]:
                    res["warnings"].append(f"Block '{b['type']}' at line {b['line']} is empty.")
                if b["type"] == "par":
                    branches = b.get("branches") or [False]
                    if b["and_count"] == 0:
                        res["warnings"].append(f"par block at line {b['line']} has no 'and'.")
                    else:
                        if branches and branches[-1] is False:
                            res["errors"].append(f"Line {idx}: empty par branch after 'and'.")
                _mark_branch_content(block_stack)
            continue

        if RE_ELSE.match(stripped):
            if not _inside_block(block_stack, {"alt"}):
                res["errors"].append(f"Line {idx}: 'else' without alt block.")
            else:
                alt_block = _nearest_block(block_stack, "alt")
                alt_block["else_count"] += 1
                _mark_content(block_stack)
            continue

        if RE_AND.match(stripped):
            # This line starts with 'and'. Now we must distinguish:
            # Is it a keyword like "and Orphan"?
            # Or is it a sentence like "And the process concludes."?

            # HEURISTIC: A keyword line is unlikely to have many words.
            # We'll assume a line with 4 or more words is natural language prose.
            words = stripped.split()
            if len(words) >= 4:
                # This is likely a sentence. Let it fall through to be handled
                # by the generic 'unknown syntax' check at the end of the loop.
                pass
            else:
                # This is likely a keyword. Apply the original validation logic.
                if not _inside_block(block_stack, {"par"}):
                    res["errors"].append(f"Line {idx}: 'and' without par block.")
                else:
                    # This is the logic for a valid 'and' inside a 'par' block.
                    while block_stack and block_stack[-1]["type"] != "par":
                        unclosed = block_stack.pop()
                        res["errors"].append(
                            f"Unclosed block '{unclosed['type']}' (line {unclosed['line']}) before 'and'."
                        )

                    if not block_stack or block_stack[-1]["type"] != "par":
                        res["errors"].append(f"Line {idx}: 'and' without par block.")
                    else:
                        par_block = block_stack[-1]
                        branches = par_block.setdefault("branches", [False])
                        if branches and branches[-1] is False:
                            res["errors"].append(f"Line {idx}: empty par branch before 'and'.")
                        par_block["and_count"] += 1
                        branches.append(False)

                # Crucially, we 'continue' only after processing it as a keyword.
                continue

        if (m := RE_ACTIVATE.match(stripped)):
            actor = _strip_quotes(m.group(1))
            activation_stack.append(actor)
            if actor not in participants:
                if require_explicit_participants:
                    res["errors"].append(f"Line {idx}: activate unknown participant '{actor}'.")
                else:
                    implicit.add(actor)
            _mark_branch_content(block_stack)
            continue

        if (m := RE_DEACTIVATE.match(stripped)):
            actor = _strip_quotes(m.group(1))
            if actor not in activation_stack:
                if strict:
                    res["warnings"].append(f"Line {idx}: deactivate '{actor}' not active.")
            else:
                for i in range(len(activation_stack)-1, -1, -1):
                    if activation_stack[i] == actor:
                        activation_stack.pop(i)
                        break
            _mark_branch_content(block_stack)
            continue

        if (m := RE_CREATE.match(stripped)):
            actor = _strip_quotes(m.group(1))
            if actor in created and strict:
                res["warnings"].append(f"Line {idx}: duplicate create '{actor}'.")
            created.add(actor)
            _mark_branch_content(block_stack)
            continue

        if (m := RE_DESTROY.match(stripped)):
            actor = _strip_quotes(m.group(1))
            if actor in destroyed and strict:
                res["warnings"].append(f"Line {idx}: duplicate destroy '{actor}'.")
            destroyed.add(actor)
            _mark_branch_content(block_stack)
            continue

        m_msg = RE_MESSAGE.match(stripped)
        if m_msg:
            src = _strip_quotes(m_msg.group(1))
            arrow = m_msg.group(2)
            dst = _strip_quotes(m_msg.group(3))
            _mark_content(block_stack)
            if arrow not in ALLOWED_ARROWS_SUPERSET and strict:
                res["warnings"].append(f"Line {idx}: arrow '{arrow}' not in superset (tolerated).")
            for actor in (src, dst):
                if actor not in participants:
                    if require_explicit_participants:
                        res["errors"].append(f"Line {idx}: participant '{actor}' not declared.")
                    else:
                        implicit.add(actor)
            continue

        unknown_lines.append((idx, stripped))
        if treat_unknown_as_error:
            res["errors"].append(f"Line {idx}: unknown syntax '{stripped[:60]}'")
        else:
            res["warnings"].append(f"Line {idx}: unknown line tolerated '{stripped[:60]}'")

    if github_flavor and not in_sequence:
        if not any("sequenceDiagram" in e for e in res["errors"]):
            res["errors"].append("Missing 'sequenceDiagram' at diagram start (GitHub flavor).")
    else:
        if not in_sequence:
            res["errors"].append("Missing 'sequenceDiagram' keyword.")

    if in_multiline_note:
        res["errors"].append(f"Multi-line note starting at line {multiline_note_start} not closed.")

    if block_stack:
        for b in reversed(block_stack):
            res["errors"].append(f"Unclosed block '{b['type']}' (line {b['line']}).")
            if b["type"] == "par" and b.get("and_count", 0) == 0:
                res["warnings"].append(f"par block at line {b['line']} has no 'and'.")
            if strict and not b.get("has_content"):
                res["warnings"].append(f"Block '{b['type']}' at line {b['line']} is empty.")

    if strict and activation_stack:
        res["warnings"].append(f"{len(activation_stack)} activation(s) not deactivated: {activation_stack}")

    if strict:
        for d in sorted(destroyed):
            if d not in created:
                res["warnings"].append(f"destroy '{d}' with no prior create.")

    res["details"]["participants"] = sorted(participants.keys())
    res["details"]["implicit_participants"] = sorted(implicit)
    res["details"]["created"] = sorted(created)
    res["details"]["destroyed"] = sorted(destroyed)
    res["details"]["unknown_lines"] = unknown_lines
    res["details"]["stats"] = {
        "line_count": len(lines),
        "byte_length": byte_len
    }
    res["is_valid"] = len(res["errors"]) == 0
    return res

OTHER_DIAGRAM_KEYWORDS = {
    "graph", "flowchart", "classDiagram", "stateDiagram", "erDiagram",
    "journey", "gantt", "pie", "mindmap", "timeline", "gitGraph",
    "quadrantChart", "xychart-beta"
}

_ARROW_PATTERN = re.compile(r'^\s*([A-Za-z0-9_"]+)\s*[-o>x\)]+[A-Za-z0-9_"]+')
_PARTICIPANT_PATTERN = re.compile(r'^\s*(participant|actor)\s+', re.IGNORECASE)

def _looks_like_sequence_without_header(lines):
    score = 0
    for ln in lines[:50]:
        s = ln.strip()
        if not s or s.startswith("%%"):
            continue
        first_word = s.split()[0]
        if first_word in OTHER_DIAGRAM_KEYWORDS:
            return False
        if _PARTICIPANT_PATTERN.match(s):
            score += 2
        if '->' in s or '-->' in s:
            if _ARROW_PATTERN.match(s):
                score += 2
        if score >= 2:
            return True
    return False

def _convert_multiline_notes(lines: List[str]) -> List[str]:
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if RE_NOTE_MULTI_START.match(line):
            start = i
            i += 1
            collected = []
            while i < n and not RE_NOTE_MULTI_END.match(lines[i]):
                collected.append(lines[i])
                i += 1
            if i >= n:  # unclosed, just push original
                out.append(line)
                out.extend(collected)
                break
            # i at end note
            i += 1
            content = []
            for c in collected:
                t = c.strip()
                if t == "":
                    t = "&nbsp;"
                content.append(t)
            joined = "<br/>".join(content) if content else ""
            header = re.sub(r'\s+', ' ', line.strip())
            out.append(f"{header}: {joined}" if joined else f"{header}:")
        else:
            out.append(line)
            i += 1
    return out


def _repair_opt_with_else(lines: List[str]) -> List[str]:
    """Use Mermaid's two-branch keyword when an ``opt`` contains ``else``."""

    repaired = list(lines)
    block_stack: List[Tuple[str, int]] = []
    for index, line in enumerate(repaired):
        stripped = line.strip()
        match = RE_BLOCK_START.match(stripped)
        if match:
            block_stack.append((match.group(1).lower(), index))
            continue
        if RE_BLOCK_END.match(stripped):
            if block_stack:
                block_stack.pop()
            continue
        if RE_ELSE.match(stripped) and block_stack[-1:]:
            block_type, start_index = block_stack[-1]
            if block_type == "opt":
                repaired[start_index] = re.sub(
                    r"^(\s*)opt\b",
                    r"\1alt",
                    repaired[start_index],
                    count=1,
                    flags=re.IGNORECASE,
                )
                block_stack[-1] = ("alt", start_index)
    return repaired


def _repair_bare_impact_note(lines: List[str]) -> List[str]:
    """Attach a standalone impact label to the diagram's visible span.

    Final occasionally emits the intended impact note without Mermaid's
    ``note over`` prefix.  With explicit participants, its representation is
    unambiguous: span the first and last declared participant and preserve the
    model-authored text verbatim.
    """

    participants: List[str] = []
    for line in lines:
        match = RE_PARTICIPANT.match(line.strip())
        if match:
            participant = match.group(2).strip()
            if participant not in participants:
                participants.append(participant)
    if not participants:
        return list(lines)

    span = participants[0]
    if participants[-1] != participants[0]:
        span = f"{participants[0]},{participants[-1]}"
    repaired: List[str] = []
    for line in lines:
        match = re.match(
            r"^(\s*)(Impact\s*(?:—|-)\s*\S.*)$",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            repaired.append(
                f"{match.group(1)}note over {span}: {match.group(2)}"
            )
        else:
            repaired.append(line)
    return repaired

def format_mermaid(
    diagram: str,
    *,
    strict: bool = True,
    require_explicit_participants: bool = False,
    treat_unknown_as_error: bool = True,
    github_flavor: bool = True,
    auto_insert_sequence_header: bool = False,
    auto_strip_leading_noise: bool = False,
    github_convert_multiline_notes: bool = True,
    max_leading_noise_lines: int = 3,
    max_lines: int = 5000,
    max_bytes: int = 300_000
) -> str:
    try:
        if not isinstance(diagram, str):
            return ""
        raw = diagram.strip()
        if not raw:
            return ""

        lines = raw.splitlines()

        if lines:
            first = lines[0].lstrip()
            if first.startswith("```"):
                tag = first[3:].strip().lower()
                if tag == "" or tag.startswith("mermaid"):
                    lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        if github_flavor and auto_strip_leading_noise:
            stripped_count = 0
            new_lines = list(lines)
            while stripped_count < max_leading_noise_lines and new_lines:
                t = new_lines[0].strip()
                if t == "" or t.startswith("%%") or t.startswith("%%{"):
                    break
                if t.lower().startswith("sequencediagram"):
                    break
                fw = t.split()[0]
                if fw in OTHER_DIAGRAM_KEYWORDS:
                    break
                new_lines.pop(0)
                stripped_count += 1
            if stripped_count > 0:
                lines = new_lines

        def first_meaningful(ls):
            for l in ls:
                tt = l.strip()
                if not tt or tt.startswith("%%"):
                    continue
                return tt
            return None

        fml = first_meaningful(lines)
        has_header = fml and fml.lower().startswith("sequencediagram")
        if not has_header and auto_insert_sequence_header:
            joined = "\n".join(lines)
            if "sequenceDiagram" not in joined and _looks_like_sequence_without_header(lines):
                lines = ["sequenceDiagram"] + ([""] if lines and lines[0].strip() else []) + lines

        if github_flavor and github_convert_multiline_notes:
            lines = _convert_multiline_notes(lines)

        # ``else`` has no meaning inside Mermaid's one-branch ``opt`` block.
        # The model's intended two branches are unambiguous, so normalize only
        # that syntax token and preserve every participant, message, and label.
        lines = _repair_opt_with_else(lines)

        # A standalone ``Impact — ...`` line is clearly note content, not a
        # message or a new claim.  Restore only the missing Mermaid prefix.
        lines = _repair_bare_impact_note(lines)

        lines = [
            line
            if RE_COMMENT_FULL.match(line.strip()) or RE_DIRECTIVE.match(line.strip())
            else _escape_github_mermaid_text(line)
            for line in lines
        ]

        core = "\n".join(lines).strip()
        if not core:
            return ""
        if len(core.encode("utf-8", errors="ignore")) > max_bytes:
            return ""
        if core.count("\n") + 1 > max_lines:
            return ""
        if "sequenceDiagram" not in core:
            return ""

        result = validate_sequence_mermaid_offline(
            core,
            strict=strict,
            require_explicit_participants=require_explicit_participants,
            treat_unknown_as_error=treat_unknown_as_error,
            github_flavor=github_flavor,
            max_lines=max_lines,
            max_bytes=max_bytes
        )
        if not (isinstance(result, dict) and result.get("is_valid")):
            errors = result.get("errors") if isinstance(result, dict) else None
            if not (
                isinstance(errors, list)
                and len(errors) == 1
                and isinstance(errors[0], str)
                and RE_SINGLE_UNCLOSED_BLOCK.fullmatch(errors[0])
            ):
                return ""
            # One unclosed EOF block has exactly one safe representation:
            # append one terminator, then require the complete strict contract
            # to pass. No participant, message, branch, or claim is changed.
            repaired_core = core.rstrip() + "\nend"
            repaired = validate_sequence_mermaid_offline(
                repaired_core,
                strict=strict,
                require_explicit_participants=require_explicit_participants,
                treat_unknown_as_error=treat_unknown_as_error,
                github_flavor=github_flavor,
                max_lines=max_lines,
                max_bytes=max_bytes,
            )
            if not (
                isinstance(repaired, dict) and repaired.get("is_valid")
            ):
                return ""
            core = repaired_core
        return "```mermaid\n" + core.rstrip() + "\n```"
    except Exception:
        return ""

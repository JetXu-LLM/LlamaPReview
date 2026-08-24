"""Language-agnostic code block extraction and classification."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class CodeContextExtractor:
    def __init__(self):
        local_patterns = [
            re.compile(r"^\s*def\s+\w+"),
            re.compile(r"^\s*async\s+def\s+\w+"),
            re.compile(r"^\s*class\s+\w+"),
            re.compile(r"^\s*function\s+\w+"),
            re.compile(r"^\s*const\s+\w+\s*="),
            re.compile(
                r"^\s*export\s+(?:default\s+)?"
                r"(?:(?:async\s+)?function|const|class)\s+\w+"
            ),
            re.compile(r"^\s*interface\s+\w+"),
            re.compile(r"^\s*type\s+\w+\s*="),
            re.compile(r"^\s*enum\s+\w+"),
            re.compile(
                r"^\s*(?:public|private|protected|internal)\s+"
                r"(?:static\s+)?(?:async\s+)?[\w<>\[\]]+\s+\w+\s*\("
            ),
            re.compile(r"^\s*func\s+(?:\(\w+\s+\*?\w+\)\s+)?\w+"),
            re.compile(r"^\s*type\s+\w+\s+(?:struct|interface)"),
            re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+\w+"),
            re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+\w+"),
            re.compile(r"^\s*impl\s+"),
            re.compile(
                r"^\s*(?:public|private|internal|fileprivate)?\s*fun\s+\w+"
            ),
        ]
        try:
            from llama_github.utils import DiffGenerator

            sdk_patterns = list(DiffGenerator._FUNC_CONTEXT_PATTERNS)
        except Exception:
            sdk_patterns = []
        # The SDK patterns are useful diff-context hints, but they intentionally
        # do not cover every declaration form. Keep runtime-owned declaration
        # patterns active even when the SDK is installed so a literal symbol
        # request cannot be attributed to an earlier definition.
        self._patterns = [*sdk_patterns, *local_patterns]

    def _is_definition_start_line(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            return False
        return any(pattern.search(line) for pattern in self._patterns)

    def extract_enclosing_block(
        self,
        content: str,
        line_index: int,
        symbol: str = "",
        max_block_lines: int = 200,
    ) -> Tuple[Optional[str], int, int]:
        lines = content.splitlines()
        if line_index < 0 or line_index >= len(lines):
            return None, -1, -1
        def_idx = -1
        for idx in range(line_index, -1, -1):
            if self._is_definition_start_line(lines[idx]):
                def_idx = idx
                break
        if def_idx == -1:
            return None, -1, -1
        for idx in range(def_idx - 1, -1, -1):
            stripped = lines[idx].strip()
            if not stripped:
                break
            if stripped.startswith("@") or (stripped.startswith("/*") and "*/" in stripped):
                def_idx = idx
            else:
                break
        brace_mode = "{" in lines[def_idx] or any("{" in lines[idx] for idx in range(def_idx, min(len(lines), def_idx + 5)))
        if brace_mode:
            depth = 0
            opened = False
            end = def_idx
            for idx in range(def_idx, len(lines)):
                if not lines[idx].strip().startswith(("//", "#")):
                    for char in lines[idx]:
                        if char == "{":
                            depth += 1
                            opened = True
                        elif char == "}":
                            depth = max(0, depth - 1)
                end = idx
                if opened and depth == 0:
                    break
                if end - def_idx >= max_block_lines:
                    break
            return "\n".join(lines[def_idx : end + 1]), def_idx + 1, end + 1
        content_idx = def_idx
        while content_idx < len(lines) and lines[content_idx].strip().startswith("@"):
            content_idx += 1
        base_indent = len(lines[content_idx]) - len(lines[content_idx].lstrip()) if content_idx < len(lines) else 0
        signature_end = content_idx
        for idx in range(content_idx, len(lines)):
            signature_end = idx
            if lines[idx].strip().endswith(":"):
                break
        end = signature_end
        for idx in range(signature_end + 1, len(lines)):
            if not lines[idx].strip():
                end = idx
                continue
            indent = len(lines[idx]) - len(lines[idx].lstrip())
            if indent <= base_indent:
                break
            end = idx
            if end - def_idx >= max_block_lines:
                break
        return "\n".join(lines[def_idx : end + 1]), def_idx + 1, end + 1

    def build_line_window(self, content: str, line_index: int, window: int = 3) -> Tuple[str, int, int]:
        lines = content.splitlines()
        if not lines:
            return "", 0, 0
        start = max(0, line_index - window)
        end = min(len(lines) - 1, line_index + window)
        return "\n".join(lines[start : end + 1]), start + 1, end + 1

    def pick_representative_line(self, candidate_lines: List[int], lines: List[str], symbol: str) -> int:
        for idx in candidate_lines:
            if "(" in lines[idx] and (not symbol or symbol in lines[idx]):
                return idx
        return candidate_lines[0]

    def classify_snippet_kind(self, symbol: str, code: str, path: str) -> str:
        lowered_lines = [line.strip().lower() for line in code.splitlines() if line.strip()]
        if not lowered_lines:
            return "usage"
        sym_re = re.compile(rf"\b{re.escape(symbol)}\b") if symbol else None
        def_prefixes = ("class ", "interface ", "struct ", "enum ", "trait ", "object ", "record ", "module ", "def ", "fn ", "function ", "type ")
        for line in lowered_lines[:8]:
            if (sym_re is None or sym_re.search(line)) and (line.endswith("{") or line.endswith(":") or any(line.startswith(p) for p in def_prefixes)):
                return "definition"
        import_like = sum(1 for line in lowered_lines if re.match(r"^(from\s+\S+\s+import\s+|import\s+\S+|using\s+\S+|export\s+|pub\s+use\s+)", line))
        if import_like >= max(2, int(0.6 * len(lowered_lines))):
            return "import"
        return "usage"

RESERVED_KEYWORDS = {
    "def",
    "class",
    "for",
    "if",
    "else",
    "elif",
    "import",
    "from",
    "as",
    "return",
    "const",
    "let",
    "var",
    "function",
    "interface",
    "type",
    "enum",
    "export",
    "public",
    "private",
    "protected",
    "static",
    "void",
    "new",
    "func",
    "package",
    "struct",
    "fn",
    "pub",
    "use",
    "mod",
    "extends",
    "implements",
}


IDENTIFIER_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
COMMENT_LINE_PREFIXES = ("#", "//", "/*", "*", "<!--", "--")
STRUCTURAL_SYMBOL_PATTERN = re.compile(
    r"^\s*(?:(?:export|public|private|protected|internal|abstract|sealed|static)\s+)*"
    r"(?:class|interface|struct|enum|type)\s+([A-Za-z_][A-Za-z0-9_]*)"
)


@dataclass(frozen=True)
class CallableSignaturePattern:
    """One structural signature recognizer and its parameter declaration form."""

    regex: re.Pattern[str]
    parameter_name_position: str = "last"


SIGNATURE_PATTERNS = (
    CallableSignaturePattern(
        re.compile(r"^\s*(?:async\s+)?def\s+(?P<owner>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<params>[^)]*)")
    ),
    CallableSignaturePattern(re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+"
        r"(?P<owner>[A-Za-z_$][A-Za-z0-9_$]*)\s*\((?P<params>[^)]*)"
    )),
    CallableSignaturePattern(re.compile(
        r"^\s*[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*\s*=\s*"
        r"(?:async\s+)?function\s+(?P<owner>[A-Za-z_$][A-Za-z0-9_$]*)\s*\((?P<params>[^)]*)"
    )),
    CallableSignaturePattern(
        re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(?P<owner>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<params>[^)]*)")
    ),
    CallableSignaturePattern(
        re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(?P<owner>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<params>[^)]*)"),
        parameter_name_position="first",
    ),
    CallableSignaturePattern(
        re.compile(r"^\s*(?:(?:public|private|protected|internal|open|override|suspend|inline)\s+)*fun\s+(?P<owner>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<params>[^)]*)")
    ),
    CallableSignaturePattern(re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<owner>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
        r"(?:async\s*)?\((?P<params>[^)]*)\)\s*=>"
    )),
    CallableSignaturePattern(re.compile(
        r"^\s*(?:(?:public|private|protected|internal|static|final|virtual|override|abstract|"
        r"synchronized|async|extern|sealed|partial|unsafe)\s+)*"
        r"(?:[A-Za-z_][A-Za-z0-9_.<>,?\[\]]*\s+)+"
        r"(?P<owner>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<params>[^)]*)\)\s*"
        r"(?:\{|=>|throws\b|where\b|$)"
    )),
)

PARAMETER_MODIFIERS = {
    "const",
    "final",
    "in",
    "inout",
    "mut",
    "out",
    "params",
    "readonly",
    "ref",
    "this",
    "var",
}
IMPLICIT_PARAMETER_NAMES = {"cls", "self", "this"}
def _split_signature_parameters(raw: str) -> List[str]:
    """Split a signature without treating nested generic/callback commas as separators."""
    parts: List[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0, "<": 0}
    closing = {")": "(", "]": "[", "}": "{", ">": "<"}
    quote = ""
    escaped = False
    for index, char in enumerate(raw):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in depths:
            depths[char] += 1
        elif char in closing:
            opener = closing[char]
            depths[opener] = max(0, depths[opener] - 1)
        elif char == "," and not any(depths.values()):
            parts.append(raw[start:index])
            start = index + 1
    parts.append(raw[start:])
    return [part.strip() for part in parts if part.strip()]


def _parameter_name(segment: str, *, position: str) -> str:
    value = segment.strip()
    if not value or value.startswith(("{", "[")):
        return ""
    value = value.split("=", 1)[0].strip().lstrip("*")
    if not value:
        return ""
    if ":" in value:
        value = value.split(":", 1)[0]
    identifiers = [item for item in IDENTIFIER_RE.findall(value) if item.lower() not in PARAMETER_MODIFIERS]
    if not identifiers:
        return ""
    name = identifiers[0] if position == "first" else identifiers[-1]
    if name.lower() in IMPLICIT_PARAMETER_NAMES or name.lower() in RESERVED_KEYWORDS:
        return ""
    return name


def _callable_signature(line: str) -> Tuple[str, Set[str]]:
    stripped = str(line or "").strip()
    if not stripped or stripped.startswith(COMMENT_LINE_PREFIXES):
        return "", set()
    if re.match(r"^(?:await|new|return|throw|yield)\b", stripped):
        return "", set()
    for signature_pattern in SIGNATURE_PATTERNS:
        match = signature_pattern.regex.search(line)
        if not match:
            continue
        owner = match.group("owner")
        if owner.lower() in RESERVED_KEYWORDS:
            continue
        params = {
            name
            for segment in _split_signature_parameters(match.group("params") or "")
            if (
                name := _parameter_name(
                    segment,
                    position=signature_pattern.parameter_name_position,
                )
            )
        }
        return owner, params
    return "", set()


def extract_diff_entities(pr_content: dict) -> dict:
    added_symbols: Set[str] = set()
    removed_symbols: Set[str] = set()
    added_signatures: Dict[Tuple[str, str], Set[str]] = {}
    removed_signatures: Dict[Tuple[str, str], Set[str]] = {}
    for fc in pr_content.get("file_changes", []):
        diff = fc.get("diff") or ""
        file_path = str(fc.get("file_path") or "")
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                changed_line = line[1:]
                for match in STRUCTURAL_SYMBOL_PATTERN.finditer(changed_line):
                    symbol = match.group(1)
                    if symbol.lower() not in RESERVED_KEYWORDS:
                        added_symbols.add(symbol)
                owner, params = _callable_signature(changed_line)
                if owner:
                    added_symbols.add(owner)
                    added_signatures.setdefault((file_path, owner), set()).update(params)
            elif line.startswith("-") and not line.startswith("---"):
                changed_line = line[1:]
                for match in STRUCTURAL_SYMBOL_PATTERN.finditer(changed_line):
                    symbol = match.group(1)
                    if symbol.lower() not in RESERVED_KEYWORDS:
                        removed_symbols.add(symbol)
                owner, params = _callable_signature(changed_line)
                if owner:
                    removed_symbols.add(owner)
                    removed_signatures.setdefault((file_path, owner), set()).update(params)
    parameter_adoptions = {
        (owner, param)
        for (path, owner), added_params in added_signatures.items()
        if (path, owner) in removed_signatures
        for param in added_params - removed_signatures[(path, owner)]
        if len(owner) > 2
        and len(param) > 2
        and owner.lower() != param.lower()
    }
    return {
        "added_symbols": {
            symbol
            for symbol in added_symbols
            if len(symbol) > 2
            and symbol.casefold() not in RESERVED_KEYWORDS
        },
        "removed_symbols": {
            symbol
            for symbol in removed_symbols
            if len(symbol) > 2
            and symbol.casefold() not in RESERVED_KEYWORDS
        },
        "added_params": {param for _owner, param in parameter_adoptions},
        "parameter_adoptions": parameter_adoptions,
    }


def format_diff_entities_block(entities: dict) -> str:
    parts = []
    if entities.get("added_symbols"):
        parts.append("Added symbols: " + ", ".join(sorted(entities["added_symbols"])))
    if entities.get("removed_symbols"):
        parts.append("Removed symbols: " + ", ".join(sorted(entities["removed_symbols"])))
    if entities.get("added_params"):
        parts.append("New parameter/property keys: " + ", ".join(sorted(entities["added_params"])))
    if entities.get("parameter_adoptions"):
        parts.append(
            "Signature-bound parameter adoption: "
            + ", ".join(f"{owner}({param})" for owner, param in sorted(entities["parameter_adoptions"]))
        )
    return "\n".join(parts) if parts else "None detected."

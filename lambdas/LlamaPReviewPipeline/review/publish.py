"""Pure GitHub review-request preparation with inline placement."""

from __future__ import annotations

import json
import hashlib
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from .language_fences import language_fence_for_path
from .schema import clean_suggested_content, suggestion_presentation

logger = logging.getLogger(__name__)

ENABLE_MULTILINE_LAYER1 = True
MULTILINE_MIN_LINES = 2
MULTILINE_MAX_LINES = 10
STRICT_MULTILINE_REQUIRE_ALL_LINES_IN_DIFF = True

PUBLIC_FOOTER_MARKER = "LlamaPReview reviewed this pull request"

# The footer is the only place the project speaks for itself in someone else's
# repository, so it stays one line and offers one door rather than a menu. The
# opening sentence never varies: it carries the marker other publication code
# matches on, and it states the one fact a reader needs to trust the comment.
_PUBLIC_FOOTER_LEAD = (
    "*LlamaPReview reviewed this pull request at its exact head commit."
)

# Which door is offered is chosen from the reviewed head, so a retry, a
# recovery, and a rebuild of the same review all produce the same footer, while
# different pull requests surface different entry points over time.
PUBLIC_FOOTER_INVITATIONS = (
    "[Read the code that reviewed it](https://github.com/JetXu-LLM/LlamaPReview).*",
    "[Tell it where it was wrong](https://github.com/JetXu-LLM/LlamaPReview/discussions).*",
    "[See what it will and will not say](https://github.com/JetXu-LLM/LlamaPReview/blob/main/docs/REVIEW_OUTPUT.md).*",
    "[Run it on your own account](https://github.com/JetXu-LLM/LlamaPReview/blob/main/docs/HOSTING.md).*",
    "[Report a review bug](https://github.com/JetXu-LLM/LlamaPReview/issues).*",
)

PUBLIC_FOOTER_VARIANTS = tuple(
    "\n\n---\n" + _PUBLIC_FOOTER_LEAD + " " + invitation
    for invitation in PUBLIC_FOOTER_INVITATIONS
)

# Callers without a reviewed head, and every test that asserts on a literal
# block, get the first variant.
PUBLIC_FOOTER = PUBLIC_FOOTER_VARIANTS[0]


def public_footer(seed: str = "") -> str:
    """Return the one code-owned footer block for this reviewed head."""

    if not seed:
        return PUBLIC_FOOTER_VARIANTS[0]
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    index = int.from_bytes(digest, "big") % len(PUBLIC_FOOTER_VARIANTS)
    return PUBLIC_FOOTER_VARIANTS[index]


def strip_public_footer(body: str) -> str:
    """Remove any code-owned footer block a presented body already carries."""

    for variant in PUBLIC_FOOTER_VARIANTS:
        body = body.replace(variant, "")
    return body


GITHUB_REVIEW_COMMENT_FIELDS = (
    "path",
    "body",
    "line",
    "side",
    "start_line",
    "start_side",
)
GITHUB_PUBLICATION_FIELDS = (
    "publication_status",
    "github_review_id",
    "github_review_commit_id",
    "github_inline_comment_ids",
)
PUBLICATION_KINDS = frozenset(
    {
        "ordinary_review",
        "lifecycle_cancellation",
        "post_merge_follow_up",
    }
)
PUBLICATION_DISPOSITIONS = frozenset(
    {
        "open_same_head",
        "merged_same_head",
        "closed_same_head",
    }
)
PUBLICATION_KIND_DISPOSITIONS = {
    "ordinary_review": frozenset({"open_same_head"}),
    "lifecycle_cancellation": frozenset(
        {"merged_same_head", "closed_same_head"}
    ),
    "post_merge_follow_up": frozenset({"merged_same_head"}),
}


@dataclass(frozen=True, slots=True)
class PreparedGitHubReview:
    """Exact immutable request and private artifact prepared before a write."""

    head_sha: str
    main_body: str
    comments: tuple[Dict[str, Any], ...]
    artifact: Dict[str, Any]
    publication_kind: str = "ordinary_review"
    required_disposition: str = "open_same_head"

    def __post_init__(self) -> None:
        allowed = PUBLICATION_KIND_DISPOSITIONS.get(self.publication_kind)
        if allowed is None:
            raise ValueError(
                f"unsupported publication kind: {self.publication_kind}"
            )
        if self.required_disposition not in allowed:
            raise ValueError(
                "publication kind and required lifecycle disposition do not "
                "agree"
            )

    def request_payload(self) -> Dict[str, Any]:
        return {
            "head_sha": self.head_sha,
            "body": self.main_body,
            "event": "COMMENT",
            "comments": [dict(comment) for comment in self.comments],
        }

    @property
    def main_body_sha256(self) -> str:
        return hashlib.sha256(self.main_body.encode("utf-8")).hexdigest()

    @property
    def payload_sha256(self) -> str:
        encoded = json.dumps(
            self.request_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def is_publishable_review(value: Any) -> bool:
    """Return whether a generated review may cross the publish boundary."""

    return bool(
        isinstance(value, dict)
        and value.get("review_generation_status") == "complete"
        and value.get("review_publishable") is True
        and value.get("review_publication_safe") is True
        and value.get("review_fallback_used") is False
        and isinstance(value.get("pr_review_comment"), str)
        and bool(value["pr_review_comment"].strip())
        and isinstance(value.get("inline_comments"), list)
    )


def _preprocess_snippet(snippet: str) -> str:
    if not isinstance(snippet, str) or not snippet.strip():
        return ""
    cleaned = re.sub(r"^\s*```[a-zA-Z]*\n?|\n\s*```\s*$", "", snippet)
    lines = cleaned.split("\n")
    processed = [re.sub(r"^[+\- ] ?", "", line) for line in lines]
    final = "\n".join(processed).strip()
    return final if final else snippet.strip()


def build_diff_index_and_maps(patch_text: str):
    if not patch_text:
        return [], {}, {}
    diff_index = []
    old_pos_map: Dict[int, int] = {}
    new_pos_map: Dict[int, int] = {}
    position = 0
    old_line = None
    new_line = None
    hunk_header_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for raw in patch_text.splitlines():
        match = hunk_header_re.match(raw)
        if match:
            old_line = int(match.group(1))
            new_line = int(match.group(2))
            continue
        if old_line is None or new_line is None:
            continue
        if raw.startswith("+"):
            position += 1
            content = raw[1:]
            diff_index.append({"position": position, "type": "add", "old_line": None, "new_line": new_line, "content": content})
            new_pos_map[new_line] = position
            new_line += 1
        elif raw.startswith("-"):
            position += 1
            content = raw[1:]
            diff_index.append({"position": position, "type": "del", "old_line": old_line, "new_line": None, "content": content})
            old_pos_map[old_line] = position
            old_line += 1
        else:
            position += 1
            content = raw[1:] if raw.startswith(" ") else raw
            diff_index.append({"position": position, "type": "context", "old_line": old_line, "new_line": new_line, "content": content})
            old_pos_map[old_line] = position
            new_pos_map[new_line] = position
            old_line += 1
            new_line += 1
    return diff_index, old_pos_map, new_pos_map


def parse_diff(patch_text: str) -> List[Dict[str, Any]]:
    if not patch_text:
        return []
    hunks = []
    current = None
    header_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*")
    for line in patch_text.splitlines():
        match = header_re.match(line)
        if match:
            if current:
                hunks.append(current)
            current = {"new_start": int(match.group(3)), "lines": []}
        elif current is not None and (line.startswith("+") or line.startswith(" ")):
            current["lines"].append(line)
    if current:
        hunks.append(current)
    return hunks


def _normalize_for_matching(text: str) -> str:
    if not isinstance(text, str):
        return ""
    for char in "\"'`;":
        text = text.replace(char, "")
    text = text.replace(",", "")
    return re.sub(r"\s+", "", text)


def find_code_snippet_line(file_content: str, snippet: str, threshold: float = 0.75) -> Optional[int]:
    if not isinstance(file_content, str) or not isinstance(snippet, str):
        raise TypeError("file_content and snippet must be strings")
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0.0 and 1.0")
    if not file_content.strip() or not snippet.strip():
        return None

    def calculate_ngram_similarity(text1: str, text2: str, n: int = 3) -> float:
        if not text1 or not text2:
            return 0.0
        if len(text1) < n or len(text2) < n:
            return 1.0 if text1 == text2 else 0.0
        ngrams1: Set[str] = {text1[i : i + n] for i in range(len(text1) - n + 1)}
        ngrams2: Set[str] = {text2[i : i + n] for i in range(len(text2) - n + 1)}
        union = len(ngrams1 | ngrams2)
        return len(ngrams1 & ngrams2) / union if union > 0 else 0.0

    def find_best_match_in_lines(lines: list, target_snippet: str) -> Optional[int]:
        best_similarity = 0.0
        best_line_num = None
        normalized_snippet = _normalize_for_matching(target_snippet)
        for i, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            similarity = calculate_ngram_similarity(_normalize_for_matching(line), normalized_snippet)
            if similarity > best_similarity:
                best_similarity = similarity
                best_line_num = i
        return best_line_num if best_similarity >= threshold else None

    def find_multiline_match(lines: list, target_lines: list) -> Optional[int]:
        if not target_lines:
            return None
        target_line_count = len(target_lines)
        best_similarity = 0.0
        best_start_line = None
        combined_target = "".join(_normalize_for_matching(line) for line in target_lines)
        for start_idx in range(len(lines) - target_line_count + 1):
            candidate_lines = lines[start_idx : start_idx + target_line_count]
            if any(not candidate.strip() and target.strip() for candidate, target in zip(candidate_lines, target_lines)):
                continue
            combined_candidate = "".join(_normalize_for_matching(line) for line in candidate_lines)
            similarity = calculate_ngram_similarity(combined_candidate, combined_target)
            if similarity > best_similarity:
                best_similarity = similarity
                best_start_line = start_idx + 1
        return best_start_line if best_similarity >= threshold else None

    file_lines = file_content.splitlines()
    snippet_lines = snippet.strip().splitlines()
    if not file_lines or not snippet_lines:
        return None
    if len(snippet_lines) == 1:
        return find_best_match_in_lines(file_lines, snippet_lines[0])
    return find_multiline_match(file_lines, snippet_lines)


def find_line_in_diff(
    hunk: Dict[str, Any],
    snippet: str,
    threshold: float = 0.75,
    new_pos_map: Optional[Dict[int, int]] = None,
    hunk_index: int = -1,
) -> Optional[Dict[str, Any]]:
    snippet_lines = snippet.strip().splitlines()
    if not any(line.strip() for line in snippet_lines):
        return None
    clean_hunk_lines = [line[1:] for line in hunk["lines"]]
    match_start_index = find_code_snippet_line("\n".join(clean_hunk_lines), snippet, threshold)
    if match_start_index is None:
        return None
    match_start_index -= 1
    match_end_index = match_start_index + len(snippet_lines)
    if match_end_index > len(hunk["lines"]):
        return None

    new_line_map: List[Optional[int]] = []
    current_new_line = hunk["new_start"] - 1
    for raw_line in hunk["lines"]:
        if raw_line.startswith("+") or raw_line.startswith(" "):
            current_new_line += 1
            new_line_map.append(current_new_line)
        else:
            new_line_map.append(None)
    window_new_lines = [
        new_line_map[idx]
        for idx in range(match_start_index, match_end_index)
        if idx < len(new_line_map) and new_line_map[idx] is not None
    ]
    if not window_new_lines:
        return None

    added_line_new_line = None
    for idx in range(match_start_index, match_end_index):
        raw_line = hunk["lines"][idx]
        if raw_line.startswith("+"):
            added_line_new_line = new_line_map[idx]
            break
    target_new_line = added_line_new_line if added_line_new_line is not None else window_new_lines[0]

    if added_line_new_line is None:
        normalized_snippet_lines = {_normalize_for_matching(s) for s in snippet_lines if s.strip()}
        for idx in range(match_start_index, match_end_index):
            raw_line = hunk["lines"][idx]
            if raw_line.startswith("+") and _normalize_for_matching(raw_line[1:]) in normalized_snippet_lines:
                target_new_line = new_line_map[idx]
                break

    return {
        "new_line": target_new_line,
        "side": "RIGHT",
        "position": new_pos_map.get(target_new_line) if new_pos_map else None,
        "range_start_new_line": window_new_lines[0],
        "range_end_new_line": window_new_lines[-1],
        "range_all_new_lines": window_new_lines,
    }


def verify_new_line_in_diff(file_path: str, new_line: int, file_position_maps: Dict[str, Dict[str, Dict[int, int]]]) -> bool:
    new_map = file_position_maps.get(file_path, {}).get("new", {})
    if new_line in new_map:
        return True
    logger.warning("[VerifyLine] %s: new_line %s NOT present in diff new_pos_map. Skipping this match.", file_path, new_line)
    return False


def _is_safe_multiline(
    file_path: str,
    range_start: int,
    range_end: int,
    all_lines: List[int],
    file_position_maps: Dict[str, Dict[str, Dict[int, int]]],
    snippet_line_count: int,
) -> bool:
    if not ENABLE_MULTILINE_LAYER1:
        return False
    if snippet_line_count < MULTILINE_MIN_LINES or snippet_line_count > MULTILINE_MAX_LINES:
        return False
    if range_start >= range_end:
        return False
    expected_len = range_end - range_start + 1
    if expected_len != len(all_lines):
        return False
    for i in range(1, len(all_lines)):
        if all_lines[i] != all_lines[i - 1] + 1:
            return False
    if STRICT_MULTILINE_REQUIRE_ALL_LINES_IN_DIFF:
        new_map = file_position_maps.get(file_path, {}).get("new", {})
        for line in all_lines:
            if line not in new_map:
                return False
    return True


def _fenced_code(value: Any, language: str = "") -> str:
    """Fence public code without allowing embedded backticks to escape."""

    code = str(value or "").rstrip("\n")
    longest_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", code)),
        default=0,
    )
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}{language}\n{code}\n{fence}"


def _format_inline_comment(comment_data: Dict[str, Any]) -> str:
    priority = comment_data.get("priority", "P2")
    confidence = comment_data.get("confidence", "Medium")
    comment_text = comment_data.get("comment", "").strip()
    formatted_comment = f"**{priority}** | Confidence: {confidence}\n\n{comment_text}"
    suggested_code_raw = comment_data.get("suggested_code") or ""
    if isinstance(suggested_code_raw, str) and suggested_code_raw.strip():
        clean_code = clean_suggested_content(suggested_code_raw)
        if clean_code:
            presentation = suggestion_presentation(
                suggestion_type=comment_data.get("suggestion_type"),
                code_snippet=comment_data.get("code_snippet"),
                suggested_code=clean_code,
            )
            if presentation["committable"]:
                formatted_comment += (
                    "\n\n" + _fenced_code(clean_code, "suggestion")
                )
            else:
                formatted_comment += (
                    f"\n\n**{presentation['label']}:**\n"
                    + _fenced_code(clean_code, presentation["fence_language"])
                )

    evidence_note = comment_data.get("evidence_note")
    if isinstance(evidence_note, str) and evidence_note.strip():
        formatted_comment += "\n\nEvidence: " + evidence_note.strip() + "."
    return formatted_comment


def find_sparse_match_in_file(file_content: str, snippet: str, max_window_factor: int = 3) -> Optional[int]:
    snippet_lines = [line for line in snippet.strip().splitlines() if line.strip()]
    if len(snippet_lines) < 3:
        return None

    def _find_line_in_content(lines_to_search: List[str], line_to_find: str, start_offset: int = 0) -> Optional[int]:
        normalized_line_to_find = _normalize_for_matching(line_to_find)
        for i, content_line in enumerate(lines_to_search):
            if _normalize_for_matching(content_line) == normalized_line_to_find:
                return start_offset + i + 1
        return None

    file_lines = file_content.splitlines()
    start_anchor_line_num = _find_line_in_content(file_lines, snippet_lines[0])
    if start_anchor_line_num is None:
        return None
    end_anchor_line_num = _find_line_in_content(file_lines[start_anchor_line_num:], snippet_lines[-1], start_offset=start_anchor_line_num)
    if end_anchor_line_num is None:
        return None
    if not (start_anchor_line_num < end_anchor_line_num):
        return None
    window_size = end_anchor_line_num - start_anchor_line_num
    if window_size > len(snippet_lines) * max_window_factor:
        return None

    search_window_lines = file_lines[start_anchor_line_num : end_anchor_line_num - 1]
    last_found_index = -1
    for snippet_line in snippet_lines[1:-1]:
        normalized_snippet_line = _normalize_for_matching(snippet_line)
        found_in_window = False
        for i, window_line in enumerate(search_window_lines[last_found_index + 1 :]):
            if _normalize_for_matching(window_line) == normalized_snippet_line:
                last_found_index += i + 1
                found_in_window = True
                break
        if not found_in_window:
            return None
    return start_anchor_line_num


def find_anchor_based_match(file_content: str, snippet: str, window_size: int = 10, context_threshold: float = 0.75) -> Optional[int]:
    snippet_lines = [line for line in snippet.strip().splitlines() if line.strip()]
    if len(snippet_lines) < 3:
        return None
    best_anchor_index = -1
    max_score = -1
    definition_keywords = re.compile(r"\b(class|def|function|public|private|protected|internal)\b")
    for i, line in enumerate(snippet_lines):
        score = len(line)
        if definition_keywords.search(line):
            score *= 1.5
        if score > max_score:
            max_score = score
            best_anchor_index = i
    if best_anchor_index == -1:
        return None

    anchor_line_num = find_code_snippet_line(file_content, snippet_lines[best_anchor_index], threshold=0.95)
    if anchor_line_num is None:
        return None
    context_lines = snippet_lines[:best_anchor_index] + snippet_lines[best_anchor_index + 1 :]
    if not context_lines:
        return anchor_line_num
    file_lines = file_content.splitlines()
    window_start = max(0, anchor_line_num - 1 - window_size)
    window_end = min(len(file_lines), anchor_line_num + window_size)
    search_window_content = "\n".join(file_lines[window_start:window_end])
    matched_context_count = 0
    for context_line in context_lines:
        if find_code_snippet_line(search_window_content, context_line, threshold=0.85):
            matched_context_count += 1
    return anchor_line_num if matched_context_count / len(context_lines) >= context_threshold else None


def _anchor_contextual_placement(
    file_path: str,
    real_line_num: int,
    comment_data: Dict[str, Any],
    file_position_maps: Dict[str, Dict[str, Dict[int, int]]],
    layer_tag: str,
) -> Optional[Dict[str, Any]]:
    new_pos_map = file_position_maps.get(file_path, {}).get("new", {})
    if not new_pos_map:
        return None
    if real_line_num in new_pos_map:
        candidate_line = real_line_num
        anchor_reason = "real_line_exact"
    else:
        try:
            nearest_changed = min(new_pos_map.keys(), key=lambda line: abs(line - real_line_num))
        except ValueError:
            return None
        candidate_line = nearest_changed
        anchor_reason = f"nearest_changed({nearest_changed})"
    if candidate_line not in new_pos_map:
        return None
    contextual_header = ""
    if real_line_num != candidate_line:
        contextual_header = (
            "**[Contextual Comment]**\n"
            f"_This comment refers to code near real line {real_line_num}. "
            f"Anchored to {anchor_reason} line {candidate_line}._\n\n---\n\n"
        )
    return {
        "path": file_path,
        "line": candidate_line,
        "side": "RIGHT",
        "body": contextual_header + _format_inline_comment(comment_data),
        "layer": layer_tag,
    }


def _comment_sort_key(comment: Dict[str, Any]) -> tuple[int, int]:
    body = comment.get("body", "")
    priority_map = {"P0": 0, "P1": 1, "P2": 2}
    confidence_map = {"High": 0, "Medium": 1, "Low": 2}
    priority_score = 3
    confidence_score = 3
    if body.startswith("**P0**"):
        priority_score = priority_map["P0"]
    elif body.startswith("**P1**"):
        priority_score = priority_map["P1"]
    elif body.startswith("**P2**"):
        priority_score = priority_map["P2"]
    if "Confidence: High" in body:
        confidence_score = confidence_map["High"]
    elif "Confidence: Medium" in body:
        confidence_score = confidence_map["Medium"]
    elif "Confidence: Low" in body:
        confidence_score = confidence_map["Low"]
    return priority_score, confidence_score


def _dedupe_and_sort_comments(review_comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged_map: Dict[Any, Dict[str, Any]] = {}
    order_keys: List[Any] = []
    for comment in review_comments:
        path = comment.get("path")
        line = comment.get("line")
        side = comment.get("side") or "RIGHT"
        start_line = comment.get("start_line")
        start_side = comment.get("start_side")
        if path is None or line is None:
            key = id(comment)
            order_keys.append(key)
            merged_map[key] = comment
            continue
        key = (path, line, side, start_line, start_side) if start_line is not None and start_side is not None else (path, line, side)
        body_text = str(comment.get("body") or "").strip()
        if key not in merged_map:
            stored = {"path": path, "line": line, "side": side, "body": body_text, "layer": comment.get("layer")}
            if comment.get("follow_up_actions"):
                stored["follow_up_actions"] = list(
                    comment["follow_up_actions"]
                )
                stored["follow_up_order"] = int(
                    comment.get("follow_up_order") or 0
                )
            if start_line is not None and start_side is not None:
                stored["start_line"] = start_line
                stored["start_side"] = start_side
            merged_map[key] = stored
            order_keys.append(key)
        elif body_text:
            separator = "\n\n---\n\n"
            existing = merged_map[key].get("body") or ""
            if existing.split(separator)[-1].strip() != body_text:
                merged_map[key]["body"] = existing.rstrip() + separator + body_text
            actions = comment.get("follow_up_actions") or []
            if actions:
                merged_map[key].setdefault("follow_up_actions", []).extend(
                    action
                    for action in actions
                    if action
                    and action
                    not in merged_map[key]["follow_up_actions"]
                )
                merged_map[key]["follow_up_order"] = min(
                    int(merged_map[key].get("follow_up_order") or 0),
                    int(comment.get("follow_up_order") or 0),
                )
    comments = [merged_map[key] for key in order_keys]
    comments.sort(key=_comment_sort_key)
    return comments


def resolve_inline_placements(
    final_json: Dict[str, Any],
    diff_maps: Dict[str, Dict[str, Any]],
    file_contents: Optional[Dict[str, str]] = None,
    *,
    include_follow_up_actions: bool = False,
) -> Dict[str, Any]:
    file_contents = file_contents or {}
    file_position_maps = {path: {"old": maps.get("old", {}), "new": maps.get("new", {})} for path, maps in diff_maps.items()}
    placements: List[Dict[str, Any]] = []
    fallback_comments: List[Dict[str, Any]] = []
    for follow_up_order, comment_data in enumerate(
        final_json.get("inline_comments", []) or []
    ):
        path = comment_data.get("file_path")
        original_snippet = comment_data.get("code_snippet")
        snippet = _preprocess_snippet(original_snippet or "")
        if not path or not snippet or path not in diff_maps:
            fallback_comments.append(comment_data)
            continue
        hunk_maps = diff_maps[path]
        placed = False

        for hunk_index, hunk in enumerate(hunk_maps.get("hunks", [])):
            line_info = find_line_in_diff(hunk, snippet, threshold=0.75, new_pos_map=hunk_maps.get("new", {}), hunk_index=hunk_index)
            if not line_info:
                continue
            found_line = line_info["new_line"]
            if not verify_new_line_in_diff(path, found_line, file_position_maps):
                continue
            body = _format_inline_comment(comment_data)
            placement = {"path": path, "line": found_line, "side": line_info["side"], "body": body, "layer": 1}
            if include_follow_up_actions:
                placement["follow_up_actions"] = [
                    str(comment_data.get("comment") or "").strip()
                ]
                placement["follow_up_order"] = follow_up_order
            snippet_line_count = len([line for line in snippet.strip().splitlines() if line.strip()])
            range_start = line_info.get("range_start_new_line")
            range_end = line_info.get("range_end_new_line")
            all_new_lines = line_info.get("range_all_new_lines") or []
            if range_start is not None and range_end is not None and _is_safe_multiline(
                path,
                range_start,
                range_end,
                all_new_lines,
                file_position_maps,
                snippet_line_count,
            ):
                placement.update({"start_line": range_start, "start_side": line_info["side"], "line": range_end})
            placements.append(placement)
            placed = True
            break
        if placed:
            continue

        content = file_contents.get(path, "")
        real_line = find_code_snippet_line(content, snippet) if content else None
        if real_line:
            placement = _anchor_contextual_placement(path, real_line, comment_data, file_position_maps, "Layer 2")
            if placement:
                if include_follow_up_actions:
                    placement["follow_up_actions"] = [
                        str(comment_data.get("comment") or "").strip()
                    ]
                    placement["follow_up_order"] = follow_up_order
                placements.append(placement)
                continue

        real_line = find_sparse_match_in_file(content, snippet) if content else None
        if real_line:
            placement = _anchor_contextual_placement(path, real_line, comment_data, file_position_maps, "Layer 2.5")
            if placement:
                if include_follow_up_actions:
                    placement["follow_up_actions"] = [
                        str(comment_data.get("comment") or "").strip()
                    ]
                    placement["follow_up_order"] = follow_up_order
                placements.append(placement)
                continue

        real_line = find_anchor_based_match(content, snippet) if content else None
        if real_line:
            placement = _anchor_contextual_placement(path, real_line, comment_data, file_position_maps, "Layer 2.7")
            if placement:
                if include_follow_up_actions:
                    placement["follow_up_actions"] = [
                        str(comment_data.get("comment") or "").strip()
                    ]
                    placement["follow_up_order"] = follow_up_order
                placements.append(placement)
                continue

        fallback_comments.append(comment_data)
    return {"inline_comments": _dedupe_and_sort_comments(placements), "fallback_comments": fallback_comments}


def build_diff_maps_from_pr_files(pr_files: List[Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for file_obj in pr_files:
        filename = getattr(file_obj, "filename", None) or file_obj.get("filename")
        patch = getattr(file_obj, "patch", None) if not isinstance(file_obj, dict) else file_obj.get("patch", "")
        _index, old_map, new_map = build_diff_index_and_maps(patch or "")
        result[filename] = {"hunks": parse_diff(patch or ""), "old": old_map, "new": new_map}
    return result


def _format_fallback_comment(comment_data: Dict[str, Any]) -> str:
    path = comment_data.get("file_path", "Unknown")
    lang = language_fence_for_path(path)
    lang_tag = lang if lang != "unknown" else ""
    original_snippet = (comment_data.get("code_snippet") or "[Snippet not available]").strip()
    ai_body = comment_data.get("comment", "No description provided.")
    suggested_code = comment_data.get("suggested_code")
    parts = [f"### File: `{path}`\n\n{ai_body}\n"]
    if suggested_code:
        clean_code = clean_suggested_content(suggested_code)
        if clean_code:
            presentation = suggestion_presentation(
                suggestion_type=comment_data.get("suggestion_type"),
                code_snippet=comment_data.get("code_snippet"),
                suggested_code=clean_code,
            )
            # An unanchored block is never one-click applicable, so it keeps
            # the neutral fence even when its content is a validated local
            # replacement. The shared label still distinguishes that precise
            # replacement from conceptual guidance.
            parts.append(
                f"\n**{presentation['label']}:**\n"
                + _fenced_code(clean_code, "")
            )
    parts.append(
        "\n**Related Code:**\n"
        + _fenced_code(original_snippet, lang_tag)
        + "\n\n---"
    )
    return "\n".join(parts)


def build_main_comment(
    final_json: Dict[str, Any],
    fallback_comments: Optional[List[Dict[str, Any]]] = None,
    *,
    invitation_seed: str = "",
) -> str:
    raw_body = final_json.get("pr_review_comment")
    final_body = raw_body.strip() if isinstance(raw_body, str) else ""
    if not final_body:
        if (
            final_json.get("review_publishable") is True
            or final_json.get("review_generation_status") == "complete"
        ):
            raise ValueError(
                "a complete review requires a non-empty model-derived comment"
            )
        # Failure artifacts may call this renderer for a uniform storage shape.
        # Keep their body empty: code must not manufacture a clear judgment or
        # attach the code-owned footer when no trustworthy decision exists.
        return ""
    if not is_publishable_review(final_json):
        return final_body
    if fallback_comments:
        fallback_body = [
            "<details>",
            "<summary>Unanchored Suggestions (Manual Review Recommended)</summary>",
            "",
            "_The following suggestions could not be precisely anchored to a specific line in the diff. Please review them in the context of the full file._",
            "",
            "---",
        ]
        for comment_data in fallback_comments:
            fallback_body.append(_format_fallback_comment(comment_data))
        fallback_body.append("</details>")
        final_body += "\n\n" + "\n".join(fallback_body)

    # Recovery reuses the immutable prepared request, but this helper remains
    # deterministic if an already prepared body is presented again. Match the
    # complete code-owned block, not a marker phrase that model prose could
    # coincidentally contain. Every block this code can emit is stripped, so a
    # body prepared under a different reviewed head still ends with exactly one.
    return strip_public_footer(final_body).rstrip() + public_footer(invitation_seed)


def prepare_main_comment_publication(
    main_body: str,
    *,
    head_sha: str,
    review_mode: str,
    publication_kind: str = "ordinary_review",
    required_disposition: str = "open_same_head",
) -> PreparedGitHubReview:
    artifact = {
        "main_comment": main_body,
        "inline_comments": [],
        "fallback_comments": [],
        "head_sha": head_sha,
        "computed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "review_quality_warnings": [],
        "review_mode": review_mode,
        "publication_kind": publication_kind,
        "required_disposition": required_disposition,
        "publication_status": "not_published",
    }
    return prepare_github_review_request(
        main_body,
        (),
        head_sha=head_sha,
        artifact=artifact,
        publication_kind=publication_kind,
        required_disposition=required_disposition,
    )


def prepare_github_review_request(
    main_body: str,
    placements: Iterable[Mapping[str, Any]],
    *,
    head_sha: str,
    artifact: Optional[Dict[str, Any]] = None,
    publication_kind: str = "ordinary_review",
    required_disposition: str = "open_same_head",
) -> PreparedGitHubReview:
    """Compile one immutable GitHub request from internal placements."""

    github_comments = tuple(
        {
            field: placement[field]
            for field in GITHUB_REVIEW_COMMENT_FIELDS
            if field in placement
        }
        for placement in placements
    )
    return PreparedGitHubReview(
        head_sha=str(head_sha or "").strip(),
        main_body=str(main_body),
        comments=github_comments,
        artifact=artifact or {},
        publication_kind=publication_kind,
        required_disposition=required_disposition,
    )


def prepare_review_publication(
    final_json: Dict[str, Any] | str,
    *,
    head_sha: str,
    diff_maps: Dict[str, Dict[str, Any]],
    file_contents: Optional[Dict[str, str]] = None,
    publication_kind: str = "ordinary_review",
) -> PreparedGitHubReview:
    if isinstance(final_json, str):
        final_json = json.loads(final_json)
    if not is_publishable_review(final_json):
        raise ValueError(
            "publication requires an explicitly complete, publishable, safe review"
        )
    placement_result = resolve_inline_placements(
        final_json,
        diff_maps,
        file_contents=file_contents,
        include_follow_up_actions=(
            publication_kind == "post_merge_follow_up"
        ),
    )
    if publication_kind == "post_merge_follow_up":
        from .render import render_post_merge_follow_up

        v3_review = final_json.get("v3_review")
        if not isinstance(v3_review, dict):
            raise ValueError(
                "post-merge publication requires the structured v3 review"
            )
        projected = dict(final_json)
        projected["pr_review_comment"] = render_post_merge_follow_up(
            v3_review,
            placement_result["inline_comments"],
        )
        main_body = build_main_comment(
            projected,
            placement_result["fallback_comments"],
            invitation_seed=head_sha,
        )
        published_placements: List[Dict[str, Any]] = []
        required_disposition = "merged_same_head"
    else:
        if publication_kind != "ordinary_review":
            raise ValueError(
                "generated review supports ordinary or post-merge publication"
            )
        main_body = build_main_comment(
            final_json,
            placement_result["fallback_comments"],
            invitation_seed=head_sha,
        )
        published_placements = placement_result["inline_comments"]
        required_disposition = "open_same_head"
    artifact = {
        "main_comment": main_body,
        "inline_comments": published_placements,
        "fallback_comments": placement_result["fallback_comments"],
        "head_sha": head_sha,
        "computed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "review_quality_warnings": final_json.get("review_quality_warnings", []),
        "publication_kind": publication_kind,
        "required_disposition": required_disposition,
        "publication_status": "not_published",
    }
    return prepare_github_review_request(
        main_body,
        published_placements,
        head_sha=head_sha,
        artifact=artifact,
        publication_kind=publication_kind,
        required_disposition=required_disposition,
    )

"""Bounded, text-only OpenAI Responses dialect for the existing fenced client.

No SDK, implicit retries, tools, remote conversation state, or paid fallback.
The wire contract is deliberately smaller than the provider's full API.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .provider_model_routing import validate_direct_openai_selection

MAX_INPUT_BYTES = 4_000_000
MAX_OUTPUT_TOKENS = 128_000
# Reserve the provider's entire context input limit, not an undocumented
# chars/token heuristic. Actual valid usage releases the conservative margin.
MAX_INPUT_TOKENS = 1_050_000


def build_request(
    messages,
    *,
    model,
    effort,
    max_tokens,
    response_format=None,
    tools=None,
    tool_choice=None,
    api_variant="stable",
) -> dict[str, Any]:
    validate_direct_openai_selection(model, effort)
    if tools or tool_choice is not None or api_variant != "stable":
        raise ValueError("Direct OpenAI permits only stateless text requests without tools")
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_OUTPUT_TOKENS:
        raise ValueError("Direct OpenAI requires a bounded max_tokens")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 128:
        raise ValueError("Direct OpenAI requires bounded text messages")
    inputs = []
    size = 0
    for message in messages:
        if not isinstance(message, dict) or set(message) - {"role", "content", "reasoning_content"}:
            raise ValueError("Unsupported direct OpenAI message fields")
        role, content = message.get("role"), message.get("content")
        if role not in {"system", "developer", "user", "assistant"} or not isinstance(content, str):
            raise ValueError("Direct OpenAI messages must be plain text")
        size += len(content.encode("utf-8"))
        inputs.append({"role": "developer" if role == "system" else role, "content": content})
    if size > MAX_INPUT_BYTES:
        raise ValueError("Direct OpenAI text input exceeds the byte cap")
    result = {
        "model": model,
        "input": inputs,
        "reasoning": {"effort": effort},
        "max_output_tokens": max_tokens,
        "store": False,
        "stream": False,
        "background": False,
        "service_tier": "default",
        "truncation": "disabled",
        "prompt_cache_options": {"mode": "explicit"},
    }
    if response_format is not None:
        if response_format not in ({"type": "json_object"}, {"type": "text"}):
            raise ValueError("Unsupported direct OpenAI text format")
        result["text"] = {"format": deepcopy(response_format)}
    return result


def normalize_response(result: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt only a complete assistant text envelope; preserve usage on failures.

    No concatenation of tool outputs, reasoning, refusal, or incomplete text can
    masquerade as a completed review. Existing stage validators fail closed on
    every finish_reason other than stop.
    """
    content = []
    incomplete = result.get("incomplete_details")
    invalid = (
        bool(result.get("error")) or result.get("status") != "completed" or incomplete is not None
    )
    refused = False
    output = result.get("output")
    if not isinstance(output, list) or not output:
        invalid = True
        output = []
    for item in output:
        if not isinstance(item, Mapping):
            invalid = True
            continue
        if item.get("type") == "reasoning":
            continue  # invisible reasoning is billed in output_tokens, not text
        if (
            item.get("type") != "message"
            or item.get("role") != "assistant"
            or item.get("status") != "completed"
        ):
            invalid = True
            continue
        blocks = item.get("content")
        if not isinstance(blocks, list) or not blocks:
            invalid = True
            continue
        for block in blocks:
            if not isinstance(block, Mapping):
                invalid = True
            elif block.get("type") == "refusal":
                refused = True
            elif block.get("type") == "output_text" and isinstance(block.get("text"), str):
                content.append(block["text"])
            else:
                invalid = True
    text = "".join(content)
    finish = "stop"
    if refused:
        finish = "content_filter"
    elif (
        result.get("status") == "incomplete"
        and isinstance(incomplete, Mapping)
        and incomplete.get("reason") == "max_output_tokens"
    ):
        finish = "length"
    elif invalid or not text.strip():
        finish = "error"
    usage = deepcopy(result.get("usage")) if isinstance(result.get("usage"), Mapping) else {}
    for target, source in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
    ):
        if source in usage:
            usage[target] = usage[source]
    return {
        "id": str(result.get("id") or ""),
        "model": str(result.get("model") or ""),
        "service_tier": str(result.get("service_tier") or ""),
        "usage": usage,
        "choices": [
            {
                "finish_reason": finish,
                "message": {"role": "assistant", "content": text if finish == "stop" else ""},
            }
        ],
        "_direct_openai_status": str(result.get("status") or ""),
    }

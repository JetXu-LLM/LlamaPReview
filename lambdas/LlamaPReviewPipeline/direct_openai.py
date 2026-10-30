"""Bounded, text-only OpenAI Responses dialect for the existing fenced client.

No SDK, implicit retries, tools, remote conversation state, or paid fallback.
The wire contract is deliberately smaller than the provider's full API.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
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


CACHE_OFF = "explicit_no_breakpoints"
CACHE_RECONCILE = "same_run_30m"


@dataclass(frozen=True)
class ReconcilePrefix:
    """Code-builder boundary, separate from canonical/repair string messages."""

    prefix: str
    suffix: str


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def valid_cache_scope(scope):
    if not isinstance(scope, Mapping) or set(scope) != {
        "installation_id",
        "account_id",
        "repository_id",
        "repo",
        "pr_number",
        "head_sha",
        "run_id",
    }:
        return False
    return (
        all(
            type(scope[k]) is int and scope[k] > 0
            for k in ("installation_id", "account_id", "repository_id", "pr_number")
        )
        and isinstance(scope["repo"], str)
        and bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", scope["repo"]))
        and isinstance(scope["head_sha"], str)
        and bool(re.fullmatch(r"[0-9a-f]{40}", scope["head_sha"]))
        and isinstance(scope["run_id"], str)
        and 0 < len(scope["run_id"]) <= 200
    )


def _cache_key(profile, payload, scope, policy, prefix):
    return _digest(
        {
            "scope": dict(scope),
            "organization_id": policy["organization_id"],
            "project_id": policy["project_id"],
            "profile_sha256": profile.sha256,
            "model": payload["model"],
            "effort": payload["reasoning"]["effort"],
            "text": payload.get("text"),
            "prefix_sha256": _digest([payload["input"][0], prefix]),
        }
    )


def apply_reconcile_cache(payload, descriptor, *, profile, phase, round_index, scope, policy):
    """Convert only the trusted initial Reconcile boundary; never mutate messages."""
    if descriptor is None:
        return payload
    if (
        type(descriptor) is not ReconcilePrefix
        or phase != "pfr_reconcile"
        or type(round_index) is not int
        or round_index not in (1, 2)
    ):
        raise ValueError("Cache descriptor is outside initial Reconcile rounds")
    inputs = payload.get("input")
    if (
        not isinstance(inputs, list)
        or len(inputs) != 2
        or inputs[0].get("role") != "developer"
        or inputs[1].get("role") != "user"
        or not isinstance(inputs[0].get("content"), str)
        or not isinstance(descriptor.prefix, str)
        or not descriptor.prefix
        or not isinstance(descriptor.suffix, str)
        or not descriptor.suffix
        or descriptor.prefix + descriptor.suffix != inputs[1].get("content")
    ):
        raise ValueError("Cache descriptor differs from canonical Reconcile text")
    if profile.reconcile_cache == "off" or not valid_cache_scope(scope):
        return payload
    result = deepcopy(payload)
    result["input"][1]["content"] = [
        {
            "type": "input_text",
            "text": descriptor.prefix,
            "prompt_cache_breakpoint": {"mode": "explicit"},
        },
        {"type": "input_text", "text": descriptor.suffix},
    ]
    result["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
    result["prompt_cache_key"] = _cache_key(profile, result, scope, policy, descriptor.prefix)
    return result


def request_cache_receipt(payload, *, profile, phase, round_index, tier, scope, policy):
    """Independently derive the mode from approved profile AND actual wire.

    Called both by transport and persistence. Only hashes leave this function;
    the wire/scope argument is ephemeral and must never enter the durable item.
    """
    inputs = payload.get("input")
    if (
        payload.get("prompt_cache_options") == {"mode": "explicit"}
        and "prompt_cache_key" not in payload
        and isinstance(inputs, list)
        and all(
            isinstance(m, dict) and set(m) == {"role", "content"} and isinstance(m["content"], str)
            for m in inputs
        )
    ):
        return None
    if (
        profile.reconcile_cache != CACHE_RECONCILE
        or phase != "pfr_reconcile"
        or type(round_index) is not int
        or round_index not in (1, 2)
        or not valid_cache_scope(scope)
        or payload.get("prompt_cache_options") != {"mode": "explicit", "ttl": "30m"}
        or not isinstance(inputs, list)
        or len(inputs) != 2
        or set(inputs[0]) != {"role", "content"}
        or inputs[0]["role"] != "developer"
        or not isinstance(inputs[0]["content"], str)
        or set(inputs[1]) != {"role", "content"}
        or inputs[1]["role"] != "user"
    ):
        raise ValueError("Unapproved direct OpenAI cache wire")
    selected = profile.select("pfr_reconcile", tier)
    if (
        payload.get("model") != selected.model
        or payload.get("reasoning") != {"effort": selected.reasoning_effort}
        or policy["profile_sha256"] != profile.sha256
    ):
        raise ValueError("Cache wire differs from approved profile")
    blocks = inputs[1]["content"]
    if (
        not isinstance(blocks, list)
        or len(blocks) != 2
        or not all(isinstance(b, dict) for b in blocks)
        or set(blocks[0]) != {"type", "text", "prompt_cache_breakpoint"}
        or set(blocks[1]) != {"type", "text"}
        or blocks[0]["prompt_cache_breakpoint"] != {"mode": "explicit"}
        or any(
            b["type"] != "input_text" or not isinstance(b["text"], str) or not b["text"]
            for b in blocks
        )
    ):
        raise ValueError("Only one initial Reconcile text breakpoint is supported")
    flattened = [inputs[0], {"role": "user", "content": "".join(b["text"] for b in blocks)}]
    canonical = build_request(
        flattened,
        model=selected.model,
        effort=selected.reasoning_effort,
        max_tokens=payload.get("max_output_tokens"),
        response_format=(payload.get("text") or {}).get("format"),
    )
    expected = {
        **canonical,
        "input": inputs,
        "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
        "prompt_cache_key": payload.get("prompt_cache_key"),
    }
    if payload != expected:
        raise ValueError("Cache wire contains unsupported request fields")
    key = _cache_key(profile, payload, scope, policy, blocks[0]["text"])
    if payload.get("prompt_cache_key") != key:
        raise ValueError("Reconcile cache scope differs from actual wire")
    return {
        "wire_sha256": _digest(payload),
        "key_sha256": key,
        "prefix_sha256": _digest([inputs[0], blocks[0]["text"]]),
        "round": round_index,
    }


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

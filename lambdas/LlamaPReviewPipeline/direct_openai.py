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
CACHE_PUBLIC_STATIC = "public_static_30m_v1"
CACHE_WRITE_MODES = frozenset({CACHE_RECONCILE, CACHE_PUBLIC_STATIC})


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


@dataclass(frozen=True)
class PublicStaticPrefix:
    """Deep's template-owned boundary; neither field modifies canonical messages."""

    prefix: str
    suffix: str


def _public_static_template(phase):
    # Versioned allowlist of already public application constants. A template
    # edit is not implicitly qualified for sharing by this strategy.
    if phase in {"route", "route_adjudication", "pfr_plan"}:
        from .review.analyzer import PR_ANALYZER_SYSTEM_PROMPT
        developer = PR_ANALYZER_SYSTEM_PROMPT
        if hashlib.sha256(developer.encode()).hexdigest() != "bc414821edb519837157c07e37a1154618f7a9de58d9c61d6a3e2bd9ae1b03c4":
            return None
        roles = ["developer", "user", "assistant", "user"] if phase == "pfr_plan" else ["developer", "user"]
        return "router_developer_v1", developer, "", roles, "pfr_plan" if phase == "pfr_plan" else "route"
    if phase in {"deep_judgment", "deep_thinking"}:
        from .review.prompts import REVIEW_SYSTEM_PROMPT, deep_judgment_public_prefix
        developer, prefix = REVIEW_SYSTEM_PROMPT, deep_judgment_public_prefix()
        if (hashlib.sha256(developer.encode()).hexdigest() != "dce259a76570692b44847103ebae7421f28e2a79e951bdea0d4bf8ed9035e3c6"
                or hashlib.sha256(prefix.encode()).hexdigest() != "a41794b9e8dee4f365ddd4d9c9caf95a8dd237b4652a7134083d7ac46af4a4d5"):
            return None
        return "deep_user_v1", developer, prefix, ["developer", "user"], "deep"
    return None


def _public_static_key(payload, policy, template):
    kind, developer, prefix, _roles, _stage = template
    before_marker = [{"role": "developer", "content": developer}]
    if prefix:
        before_marker.append({"role": "user", "content": prefix})
    prefix_hash = _digest(before_marker)
    key = _digest({
        "strategy": CACHE_PUBLIC_STATIC, "prefix_kind": kind,
        "prefix_sha256": prefix_hash, "organization_id": policy["organization_id"],
        "project_id": policy["project_id"], "model": payload["model"],
        "effort": payload["reasoning"]["effort"], "text": payload.get("text"),
    })
    return key, prefix_hash


def apply_public_static_cache(payload, descriptor, *, profile, phase, policy):
    """Share only exact public constants; no tenant scope or rendered-tag search."""
    if profile.static_prefix_cache == "off":
        return payload
    template = _public_static_template(phase)
    if template is None:
        return payload
    kind, developer, prefix, roles, _stage = template
    inputs = payload.get("input")
    if (not isinstance(inputs, list) or len(inputs) != len(roles)
            or any(not isinstance(m, dict) or set(m) != {"role", "content"}
                   or m["role"] != role or not isinstance(m["content"], str)
                   for m, role in zip(inputs, roles))
            or inputs[0]["content"] != developer):
        return payload
    result = deepcopy(payload)
    if kind == "deep_user_v1":
        if descriptor is None:
            return payload
        if (type(descriptor) is not PublicStaticPrefix or descriptor.prefix != prefix
                or not isinstance(descriptor.suffix, str) or not descriptor.suffix
                or descriptor.prefix + descriptor.suffix != inputs[1]["content"]):
            raise ValueError("Public Deep boundary differs from its exact template/text")
        result["input"][1]["content"] = [
            {"type": "input_text", "text": prefix, "prompt_cache_breakpoint": {"mode": "explicit"}},
            {"type": "input_text", "text": descriptor.suffix},
        ]
    else:
        if descriptor is not None:
            raise ValueError("Public Deep descriptor supplied outside Deep")
        result["input"][0]["content"] = [
            {"type": "input_text", "text": developer, "prompt_cache_breakpoint": {"mode": "explicit"}}
        ]
    result["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
    result["prompt_cache_key"] = _public_static_key(result, policy, template)[0]
    return result


def _public_static_receipt(payload, *, profile, phase, tier, policy):
    template = _public_static_template(phase)
    if profile.static_prefix_cache != CACHE_PUBLIC_STATIC or template is None:
        raise ValueError("Public static cache is not approved for this phase/template")
    kind, developer, prefix, roles, stage = template
    selected = profile.select(stage, tier)
    inputs = payload.get("input")
    if (not isinstance(inputs, list) or len(inputs) != len(roles)
            or any(not isinstance(m, dict) or set(m) != {"role", "content"}
                   or m["role"] != role for m, role in zip(inputs, roles))
            or payload.get("model") != selected.model
            or payload.get("reasoning") != {"effort": selected.reasoning_effort}
            or policy["profile_sha256"] != profile.sha256):
        raise ValueError("Public cache wire differs from approved profile/shape")
    index = 1 if prefix else 0
    blocks = inputs[index]["content"]
    if (not isinstance(blocks, list) or len(blocks) != (2 if prefix else 1)
            or not all(isinstance(b, dict) for b in blocks)
            or set(blocks[0]) != {"type", "text", "prompt_cache_breakpoint"}
            or blocks[0].get("type") != "input_text"
            or blocks[0].get("text") != (prefix or developer)
            or blocks[0].get("prompt_cache_breakpoint") != {"mode": "explicit"}
            or (prefix and (set(blocks[1]) != {"type", "text"}
                            or blocks[1].get("type") != "input_text"
                            or not isinstance(blocks[1].get("text"), str) or not blocks[1]["text"]))):
        raise ValueError("Only the exact public-template breakpoint is supported")
    flattened = deepcopy(inputs)
    flattened[index]["content"] = "".join(b["text"] for b in blocks)
    if flattened[0]["content"] != developer:
        raise ValueError("Public cache developer differs from its exact public constant")
    canonical = build_request(flattened, model=selected.model, effort=selected.reasoning_effort,
                              max_tokens=payload.get("max_output_tokens"),
                              response_format=(payload.get("text") or {}).get("format"))
    key, prefix_hash = _public_static_key(payload, policy, template)
    expected = {**canonical, "input": inputs, "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
                "prompt_cache_key": key}
    if payload != expected:
        raise ValueError("Public cache wire contains unsupported fields or key")
    return {"mode": CACHE_PUBLIC_STATIC, "stage": stage, "prefix_kind": kind,
            "wire_sha256": _digest(payload), "key_sha256": key, "prefix_sha256": prefix_hash}


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
    if phase != "pfr_reconcile":
        return _public_static_receipt(payload, profile=profile, phase=phase, tier=tier, policy=policy)
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

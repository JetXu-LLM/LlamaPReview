"""Grant-window reservation arithmetic; persistence owns the atomic effect.

Amounts are integer nano-USD. There is no wall-calendar reset, balance poll,
spend-limit assumption, fallback, counter TTL, or per-month call-ID map.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import os
import re
import time
from typing import Any, Mapping

from .direct_openai import MAX_INPUT_TOKENS

PRICING_VERSION = "openai-standard-text-2026-10-29"
# Nano-USD per token, standard non-regional, no tools. The long-context rates
# are reserved for EVERY call. Explicit-only caching has no breakpoints.
PRICES = {
    "gpt-6-luna": {"input": 100, "cached": 10, "write_30m": 125, "write_12h": 250, "output": 500},
    "gpt-6.1-terra": {
        "input": 1000,
        "cached": 50,
        "write_30m": 1250,
        "write_12h": 2500,
        "output": 5000,
    },
}
MAX_ALLOCATION_NANOUSD = 150_000_000_000
POLICY_FIELDS = frozenset(
    {
        "cycle_id",
        "valid_from",
        "expires_at",
        "allocation_nanousd",
        "verified_available_nanousd",
        "organization_id",
        "project_id",
        "profile_sha256",
        "pricing_version",
        "pricing_verified_until",
        "authorization",
    }
)


class BudgetUnavailable(ValueError):
    """Missing, exhausted, expired or unverified authority: zero new dispatch."""

    provider_dispatch_not_started = True
    provider_call_control_failure = True


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _int(value: Any, name: str, *, zero=True) -> int:
    if type(value) is not int or value < (0 if zero else 1):
        raise BudgetUnavailable(f"{name} must be an exact nonnegative integer")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BudgetUnavailable("Duplicate budget policy key")
        result[key] = value
    return result


def load_policy(raw=None, *, now=None, request_seconds=0, profile_sha256=None) -> dict[str, Any]:
    if raw is None:
        raw = os.environ.get("DIRECT_OPENAI_BUDGET", "")
    try:
        policy = json.loads(raw, object_pairs_hook=_unique) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError) as exc:
        raise BudgetUnavailable("Explicit verified DIRECT_OPENAI_BUDGET is required") from exc
    if set(policy) != POLICY_FIELDS:
        raise BudgetUnavailable("Budget policy fields are missing or unsupported")
    if policy["authorization"] != "approved" or policy["pricing_version"] != PRICING_VERSION:
        raise BudgetUnavailable("Budget authorization or pricing snapshot is unqualified")
    for key in (
        "valid_from",
        "expires_at",
        "allocation_nanousd",
        "verified_available_nanousd",
        "pricing_verified_until",
    ):
        _int(policy[key], key, zero=False)
    for key in ("cycle_id", "organization_id", "project_id"):
        if not isinstance(policy[key], str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,96}", policy[key]
        ):
            raise BudgetUnavailable(f"Invalid explicit {key}")
    if not isinstance(policy["profile_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", policy["profile_sha256"]
    ):
        raise BudgetUnavailable("An exact qualified profile digest is required")
    if profile_sha256 is not None and policy["profile_sha256"] != profile_sha256:
        raise BudgetUnavailable("Selected profile has not been qualified for this allocation")
    if (
        not 0
        < policy["allocation_nanousd"]
        <= min(MAX_ALLOCATION_NANOUSD, policy["verified_available_nanousd"])
    ):
        raise BudgetUnavailable("Allocation exceeds verified funds or the $150 application ceiling")
    now = time.time() if now is None else now
    # Stop purchases sufficiently before expiry to include this entire HTTP
    # attempt plus durable settlement margin. In-flight late settlement is NOT
    # gated by current config/time and always uses its original cycle/price.
    if not policy["valid_from"] <= now or now + request_seconds + 30 >= min(
        policy["expires_at"], policy["pricing_verified_until"]
    ):
        raise BudgetUnavailable("Grant or verified pricing window does not cover this attempt")
    return policy


def reservation(policy: Mapping[str, Any], *, model: str, max_output_tokens: int) -> dict[str, Any]:
    if (
        model not in PRICES
        or type(max_output_tokens) is not int
        or not 1 <= max_output_tokens <= 128_000
    ):
        raise BudgetUnavailable("No finite model/output price bound")
    price = PRICES[model]
    # Both approved models share the documented context cap. Reserving that
    # full input limit (even for tiny requests) is conservative without a paid
    # token-count call or an unproven tokenizer-overhead assumption.
    amount = MAX_INPUT_TOKENS * price["input"] * 2 + max_output_tokens * price["output"] * 3 // 2
    key = "_OPENAI_GRANT_" + digest(
        {key: policy[key] for key in ("cycle_id", "organization_id", "project_id")}
    )
    return {
        "schema_version": 1,
        "state": "reserved",
        "counter_repo": key,
        "policy_sha256": digest(dict(policy)),
        "cycle_id": policy["cycle_id"],
        "allocation_nanousd": policy["allocation_nanousd"],
        "expires_at": policy["expires_at"],
        "pricing_version": policy["pricing_version"],
        "price": dict(price),
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": max_output_tokens,
        "reserved_nanousd": amount,
        "cache_mode": "explicit_no_breakpoints",
    }


def actual_cost(record: Mapping[str, Any], original: Mapping[str, Any]) -> int | None:
    """Price only complete validated usage with the ORIGINAL fence's tariff.

    Reasoning is a subset of output, never added twice. Unexpected cache writes,
    service tier, model, or token bounds retain the full reservation for manual
    reconciliation instead of manufacturing a refund.
    """
    if record.get("usage_state") != "reported" or record.get("service_tier") != "default":
        return None
    if record.get("response_model") != record.get("billed_model"):
        return None
    usage = record.get("usage")
    if not isinstance(usage, Mapping):
        return None
    inp, out, total = (usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens"))
    details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    if not isinstance(details, Mapping) or not isinstance(output_details, Mapping):
        return None
    cache, write, reasoning = (
        details.get("cached_tokens"),
        details.get("cache_write_tokens"),
        output_details.get("reasoning_tokens"),
    )

    def integral(value):
        if type(value) is int and value >= 0:
            return value
        if (
            isinstance(value, Decimal)
            and value.is_finite()
            and value >= 0
            and value == value.to_integral_value()
        ):
            return int(value)
        return None

    numbers = tuple(integral(x) for x in (inp, out, total, cache, write, reasoning))
    if any(x is None for x in numbers):
        return None
    inp, out, total, cache, write, reasoning = numbers
    if (
        total != inp + out
        or inp > original["max_input_tokens"]
        or out > original["max_output_tokens"]
        or reasoning > out
        or cache + write > inp
    ):
        return None
    if usage.get("prompt_tokens") != inp or usage.get("completion_tokens") != out:
        return None
    # This adapter explicitly disables implicit caching and supplies no
    # breakpoint. Nonzero cache use proves a contract mismatch, not a discount.
    if cache or write:
        return None
    write_details = details.get("cache_write_tokens_details")
    if write_details is not None and (
        not isinstance(write_details, Mapping)
        or any(integral(value) != 0 for value in write_details.values())
    ):
        return None
    price = original["price"]
    numerator = inp * price["input"] * (4 if inp > 272_000 else 2) + out * price["output"] * (
        3 if inp > 272_000 else 2
    )
    cost = (numerator + 1) // 2
    return int(cost) if cost <= original["reserved_nanousd"] else None

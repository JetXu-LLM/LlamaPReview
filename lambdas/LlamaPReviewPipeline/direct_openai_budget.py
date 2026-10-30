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

from .direct_openai import MAX_INPUT_TOKENS, CACHE_OFF, CACHE_WRITE_MODES

PRICING_VERSION = "openai-standard-text-2026-10-29"
# Nano-USD per token, standard non-regional, no tools. The long-context rates
# are reserved for EVERY call; opt-in Reconcile reserves a cold 30m write.
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
    provider_budget_unavailable = True


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


def reservation(
    policy: Mapping[str, Any], *, model: str, max_output_tokens: int, cache_mode: str = CACHE_OFF
) -> dict[str, Any]:
    if (
        cache_mode not in ({CACHE_OFF} | CACHE_WRITE_MODES)
        or model not in PRICES
        or type(max_output_tokens) is not int
        or not 1 <= max_output_tokens <= 128_000
    ):
        raise BudgetUnavailable("No finite model/output price bound")
    price = PRICES[model]
    # Both approved models share the documented context cap. Reserving that
    # full input limit (even for tiny requests) is conservative without a paid
    # token-count call or an unproven tokenizer-overhead assumption.
    input_price = price["write_30m"] if cache_mode in CACHE_WRITE_MODES else price["input"]
    amount = MAX_INPUT_TOKENS * input_price * 2 + max_output_tokens * price["output"] * 3 // 2
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
        "cache_mode": cache_mode,
        **({"cache_ttl": "30m"} if cache_mode in CACHE_WRITE_MODES else {}),
    }


def _integral(value):
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


def cache_usage_parts(usage, cache_mode):
    """Validate RAW partitions before the generic numeric tree can drop bad leaves.

    Reused on durable Decimal records. No coercion, inferred TTL or missing-zero
    default may turn contradictory provider accounting into a refund.
    """
    if not isinstance(usage, Mapping) or cache_mode not in ({CACHE_OFF} | CACHE_WRITE_MODES):
        return None
    details, output_details = usage.get("input_tokens_details"), usage.get("output_tokens_details")
    if not isinstance(details, Mapping) or not isinstance(output_details, Mapping):
        return None
    values = tuple(
        _integral(x)
        for x in (
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("total_tokens"),
            details.get("cached_tokens"),
            details.get("cache_write_tokens"),
            output_details.get("reasoning_tokens"),
        )
    )
    if any(x is None for x in values):
        return None
    inp, out, total, cache, write, reasoning = values
    if total != inp + out or reasoning > out or cache + write > inp:
        return None
    ttl = details.get("cache_write_tokens_details")
    if cache_mode == CACHE_OFF:
        if (
            cache
            or write
            or (
                ttl is not None
                and (not isinstance(ttl, Mapping) or any(_integral(v) != 0 for v in ttl.values()))
            )
        ):
            return None
        return inp, out, cache, 0
    if not isinstance(ttl, Mapping) or set(ttl) != {"ttl_30m_tokens", "ttl_12h_tokens"}:
        return None
    short, long = _integral(ttl["ttl_30m_tokens"]), _integral(ttl["ttl_12h_tokens"])
    if short is None or long != 0 or short != write:
        return None
    return inp, out, cache, short


def actual_cost(record: Mapping[str, Any], original: Mapping[str, Any]) -> int | None:
    """Settle valid partitions using only the original durable fence's tariff."""
    if record.get("usage_state") != "reported" or record.get("service_tier") != "default":
        return None
    if record.get("response_model") != record.get("billed_model"):
        return None
    mode = original.get("cache_mode")
    if mode in CACHE_WRITE_MODES and original.get("cache_ttl") != "30m":
        return None
    usage = record.get("usage")
    parts = cache_usage_parts(usage, mode)
    if parts is None:
        return None
    inp, out, cache, write = parts
    if (
        inp > original["max_input_tokens"]
        or out > original["max_output_tokens"]
        or _integral(usage.get("prompt_tokens")) != inp
        or _integral(usage.get("completion_tokens")) != out
    ):
        return None
    price = original["price"]
    input_cost = (
        (inp - cache - write) * price["input"]
        + cache * price["cached"]
        + write * price["write_30m"]
    )
    # Full input, including ALL read/write partitions, selects the long price.
    numerator = input_cost * (4 if inp > 272_000 else 2) + out * price["output"] * (
        3 if inp > 272_000 else 2
    )
    cost = (numerator + 1) // 2
    return int(cost) if cost <= original["reserved_nanousd"] else None

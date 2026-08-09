"""Dependency-free normalization for provider usage accounting.

Usage telemetry is shared by Route, PFR, Deep, Final, persistence, and the
pipeline coordinator.  It is not review-contract repair and must not pull
schema, JSON Patch, or model-protocol code into those capability boundaries.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import math
from numbers import Integral, Real
import re
from typing import Any, Mapping, Optional


_USAGE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,95}$")


def _numeric_usage_tree(value: Any) -> Optional[Any]:
    """Retain only finite, non-negative numeric leaves."""

    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        if not value.is_finite() or value < 0:
            return None
        if value == value.to_integral_value():
            return int(value)
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, Real):
        numeric = float(value) if isinstance(value, float) else int(value)
        if numeric < 0 or not math.isfinite(float(numeric)):
            return None
        return numeric
    if not isinstance(value, Mapping):
        return None

    retained: dict[str, Any] = {}
    for key, child in value.items():
        if not isinstance(key, str) or not _USAGE_KEY_RE.fullmatch(key):
            continue
        numeric_child = _numeric_usage_tree(child)
        if numeric_child is not None and numeric_child != {}:
            retained[key] = numeric_child
    return retained


def _merge_numeric_tree(
    target: dict[str, Any],
    incoming: Mapping[str, Any],
    *,
    path: str = "",
    conflicts: Optional[list[str]] = None,
) -> None:
    for key, value in incoming.items():
        if key not in target:
            target[key] = deepcopy(value)
            continue
        current = target[key]
        if isinstance(current, dict) and isinstance(value, Mapping):
            _merge_numeric_tree(
                current,
                value,
                path=f"{path}.{key}" if path else key,
                conflicts=conflicts,
            )
        elif (
            isinstance(current, Real)
            and not isinstance(current, bool)
            and isinstance(value, Real)
            and not isinstance(value, bool)
        ):
            target[key] = current + value
        elif conflicts is not None:
            conflicts.append(f"{path}.{key}" if path else key)


def merge_numeric_usage_with_diagnostics(
    *usage_records: Optional[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Sum numeric leaves and report every incompatible telemetry shape."""

    merged: dict[str, Any] = {}
    conflicts: list[str] = []
    for usage in usage_records:
        numeric = _numeric_usage_tree(usage)
        if isinstance(numeric, Mapping):
            _merge_numeric_tree(
                merged,
                numeric,
                conflicts=conflicts,
            )
    return merged, sorted(set(conflicts))


def merge_numeric_usage(
    *usage_records: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Sum valid numeric usage leaves and discard all other content."""

    merged, _conflicts = merge_numeric_usage_with_diagnostics(
        *usage_records
    )
    return merged


def validate_complete_token_usage(
    usage: Optional[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Require exact integral prompt, completion, and total token classes."""

    normalized = _numeric_usage_tree(usage)
    retained = dict(normalized) if isinstance(normalized, Mapping) else {}
    errors: list[str] = []
    values: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = retained.get(key)
        if not isinstance(value, Integral) or isinstance(value, bool):
            errors.append(f"{key}_missing_or_non_integral")
            continue
        values[key] = int(value)
    if len(values) == 3 and values["total_tokens"] != (
        values["prompt_tokens"] + values["completion_tokens"]
    ):
        errors.append("total_tokens_invariant_mismatch")
    return retained, errors

"""Canonical provider-ledger reconciliation for runtime and operations.

This module owns ledger partition, usage, dispatch identity, retries, and
logical/billed model truth. Pricing and release policy remain external.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from numbers import Real
from typing import Any, Iterable, Mapping, Optional

CALL_ID_RE = re.compile(r"^[0-9a-f]{64}$")
PARTITIONS = ("all", "winning", "discarded")
LEDGER_FIELDS = {
    "all": "deepseek_all_attempt_model_phases",
    "winning": "deepseek_model_phases",
    "discarded": "deepseek_discarded_model_phases",
}
USAGE_FIELDS = {
    "all": "deepseek_usage_total",
    "winning": "deepseek_winning_usage_total",
    "discarded": "deepseek_discarded_usage_total",
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _protocol_integer(value: Any) -> Optional[int]:
    """Return an exact protocol integer from Python or DynamoDB values."""

    if type(value) is int:
        return value
    if (
        isinstance(value, Decimal)
        and value.is_finite()
        and value == value.to_integral_value()
    ):
        return int(value)
    return None


def numeric_usage_tree(value: Any) -> Any:
    """Retain only finite non-negative numeric usage leaves."""

    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        if not value.is_finite() or value < 0:
            return None
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Real):
        numeric = float(value) if isinstance(value, float) else int(value)
        if numeric < 0 or not math.isfinite(float(numeric)):
            return None
        return numeric
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key, child in value.items():
        if not isinstance(key, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+",
            key,
        ):
            continue
        normalized = numeric_usage_tree(child)
        if normalized not in (None, {}):
            result[key] = normalized
    return result


def merge_numeric_tree(
    target: dict[str, Any],
    incoming: Mapping[str, Any],
) -> list[str]:
    """Merge usage and report shape conflicts instead of silently repricing."""

    conflicts: list[str] = []
    for key, value in incoming.items():
        if key not in target:
            target[key] = deepcopy(value)
        elif isinstance(target[key], dict) and isinstance(value, Mapping):
            conflicts.extend(
                f"{key}.{path}"
                for path in merge_numeric_tree(target[key], value)
            )
        elif (
            isinstance(target[key], Real)
            and not isinstance(target[key], bool)
            and isinstance(value, Real)
            and not isinstance(value, bool)
        ):
            target[key] += value
        else:
            conflicts.append(str(key))
    return conflicts


def merged_usage(
    records: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    merged: dict[str, Any] = {}
    conflicts: list[str] = []
    for index, record in enumerate(records):
        usage = numeric_usage_tree(record.get("usage"))
        if not isinstance(usage, Mapping):
            continue
        conflicts.extend(
            f"record[{index}].{path}"
            for path in merge_numeric_tree(merged, usage)
        )
    return merged, conflicts


def _ledger(
    artifact: Mapping[str, Any],
    *,
    partition: str,
    errors: list[str],
    allow_empty_all: bool,
) -> list[Mapping[str, Any]]:
    raw = artifact.get(LEDGER_FIELDS[partition])
    if not isinstance(raw, list) or (
        partition == "all" and not raw and not allow_empty_all
    ):
        errors.append(f"{partition}_call_ledger_missing")
        raw = []
    if any(not isinstance(item, Mapping) for item in raw):
        errors.append(f"{partition}_call_ledger_invalid")
    return [item for item in raw if isinstance(item, Mapping)]


def _validate_dispatch(
    record: Mapping[str, Any],
    *,
    errors: list[str],
    require_schema_v2: bool,
) -> None:
    raw_schema_version = record.get("schema_version")
    normalized_schema_version = _protocol_integer(raw_schema_version)
    schema_version = (
        1
        if raw_schema_version is None
        else normalized_schema_version
        if normalized_schema_version is not None
        else 0
    )
    if require_schema_v2 and schema_version != 2:
        errors.append("provider_call_schema_v2_required")
        return
    model = str(record.get("model") or "")
    logical_model = str(record.get("logical_model") or "")
    billed_model = str(record.get("billed_model") or "")
    if not model or not logical_model or model != logical_model:
        errors.append("provider_logical_model_invalid")
    if not billed_model:
        errors.append("provider_billed_model_invalid")
    operation_identity = {
        "run_id": str(record.get("run_id") or ""),
        "head_sha": str(record.get("head_sha") or ""),
        "pipeline_phase": str(record.get("pipeline_phase") or ""),
        "pipeline_attempt": _protocol_integer(
            record.get("pipeline_attempt")
        ),
        "phase": str(record.get("phase") or ""),
        "call_index": _protocol_integer(record.get("call_index")),
    }
    if schema_version < 2:
        if str(record.get("call_id") or "") != sha256_value(
            operation_identity
        ):
            errors.append("provider_dispatch_identity_mismatch")
        return
    expected_operation_id = sha256_value(operation_identity)
    operation_id = str(record.get("operation_id") or "")
    if operation_id != expected_operation_id:
        errors.append("provider_operation_identity_mismatch")
    raw_attempt_index = record.get("transport_attempt_index")
    raw_dispatch_count = record.get("transport_dispatch_count")
    raw_attempt_count = record.get("transport_attempt_count")
    attempt_index = _protocol_integer(raw_attempt_index) or 0
    dispatch_count = _protocol_integer(raw_dispatch_count) or 0
    attempt_count = _protocol_integer(raw_attempt_count) or 0
    if str(record.get("call_id") or "") != sha256_value(
        {
            "operation_id": operation_id,
            "transport_attempt_index": attempt_index,
        }
    ):
        errors.append("provider_dispatch_identity_mismatch")
    if (
        attempt_index < 1
        or dispatch_count != 1
        or (require_schema_v2 and attempt_count != 1)
    ):
        errors.append("provider_transport_dispatch_invalid")


def billed_model_matches_transport_contract(
    record: Mapping[str, Any],
    expected_transport_model_override: Optional[str],
) -> bool:
    """Check the billed model against the frozen transport policy.

    ``None`` is reserved for offline historical readers that do not assert a
    current transport policy.  An exact empty string restores normal dispatch,
    so the billed model must then equal the logical model.
    """

    if expected_transport_model_override is None:
        return True
    if (
        not isinstance(expected_transport_model_override, str)
        or expected_transport_model_override
        != expected_transport_model_override.strip()
    ):
        return False
    expected = (
        expected_transport_model_override
        if expected_transport_model_override
        else str(record.get("logical_model") or "")
    )
    return bool(expected) and str(record.get("billed_model") or "") == expected


def reconcile_provider_accounting(
    artifact: Mapping[str, Any],
    *,
    allow_zero_calls: bool = False,
    require_schema_v2: bool = True,
    expected_transport_model_override: Optional[str] = None,
) -> dict[str, Any]:
    """Reconcile one artifact without pricing or release policy."""

    errors: list[str] = []
    if expected_transport_model_override is not None and (
        not isinstance(expected_transport_model_override, str)
        or expected_transport_model_override
        != expected_transport_model_override.strip()
    ):
        errors.append("provider_transport_model_contract_invalid")
    ledgers = {
        partition: _ledger(
            artifact,
            partition=partition,
            errors=errors,
            allow_empty_all=allow_zero_calls,
        )
        for partition in PARTITIONS
    }
    ids: dict[str, list[str]] = {}
    for partition, records in ledgers.items():
        ids[partition] = [
            str(record.get("call_id") or "").strip().lower()
            for record in records
        ]
        if any(not CALL_ID_RE.fullmatch(value) for value in ids[partition]):
            errors.append(f"{partition}_call_id_invalid")
        if len(ids[partition]) != len(set(ids[partition])):
            errors.append(f"{partition}_call_id_duplicate")
    if set(ids["winning"]) & set(ids["discarded"]):
        errors.append("winning_discarded_overlap")
    if set(ids["winning"]) | set(ids["discarded"]) != set(ids["all"]):
        errors.append("winning_discarded_not_exact_partition")

    # A partition is evidence, not a second mutable description of a call.
    # Matching call IDs alone would allow a winning or discarded row to alter
    # model, usage, status, or coordinates while still passing the set check.
    # Bind both partitions to the exact canonical rows recorded in ``all``.
    all_by_call_id: dict[str, Mapping[str, Any]] = {}
    for call_id, record in zip(ids["all"], ledgers["all"]):
        if CALL_ID_RE.fullmatch(call_id) and call_id not in all_by_call_id:
            all_by_call_id[call_id] = record
    for partition in ("winning", "discarded"):
        for call_id, record in zip(ids[partition], ledgers[partition]):
            canonical_record = all_by_call_id.get(call_id)
            if canonical_record is not None and record != canonical_record:
                errors.append(f"{partition}_call_record_mismatch")

    for record in ledgers["all"]:
        _validate_dispatch(
            record,
            errors=errors,
            require_schema_v2=require_schema_v2,
        )
        if str(record.get("status") or "") != "completed":
            errors.append("provider_call_not_completed")
        if str(record.get("usage_state") or "") != "reported":
            errors.append("provider_usage_unknown")
        if not isinstance(numeric_usage_tree(record.get("usage")), Mapping):
            errors.append("provider_usage_invalid")
        if not billed_model_matches_transport_contract(
            record,
            expected_transport_model_override,
        ):
            errors.append(
                "provider_billed_model_transport_contract_mismatch"
            )

    # A later transport attempt cannot exist without every preceding durable
    # fence for the same logical operation.  Validating each call_id in
    # isolation would allow an index-2 success to hide an unreported first
    # dispatch.  Bind the all-ledger to the exact contiguous 1..N sequence.
    if require_schema_v2:
        attempt_indexes_by_operation: dict[str, list[int]] = {}
        for record in ledgers["all"]:
            operation_id = str(record.get("operation_id") or "")
            raw_attempt_index = record.get("transport_attempt_index")
            attempt_index = _protocol_integer(raw_attempt_index) or 0
            if operation_id:
                attempt_indexes_by_operation.setdefault(
                    operation_id, []
                ).append(attempt_index)
        if any(
            sorted(indexes) != list(range(1, len(indexes) + 1))
            for indexes in attempt_indexes_by_operation.values()
        ):
            errors.append("provider_transport_attempt_sequence_invalid")

    usage: dict[str, dict[str, Any]] = {}
    merge_conflicts: dict[str, list[str]] = {}
    for partition, records in ledgers.items():
        usage[partition], merge_conflicts[partition] = merged_usage(records)
        if artifact.get(USAGE_FIELDS[partition]) != usage[partition]:
            errors.append(f"{USAGE_FIELDS[partition]}_mismatch")
        if merge_conflicts[partition]:
            errors.append(f"{partition}_usage_merge_conflicts")

    accounting = artifact.get("deepseek_usage_accounting")
    if not isinstance(accounting, Mapping):
        errors.append("usage_accounting_missing")
        accounting = {}
    if (
        ledgers["all"]
        and require_schema_v2
        and accounting.get("schema_version") != 2
    ):
        errors.append("usage_accounting_schema_v2_required")
    expected_counts = {
        "all_call_count": len(ledgers["all"]),
        "winning_call_count": len(ledgers["winning"]),
        "discarded_call_count": len(ledgers["discarded"]),
        "unreported_usage_call_count": 0,
        "complete_numeric_usage": True,
    }
    if ledgers["all"] and require_schema_v2:
        expected_counts["transport_operation_count"] = len(
            {
                str(record.get("operation_id") or "")
                for record in ledgers["all"]
            }
        )
        if accounting.get("usage_merge_conflicts") != []:
            errors.append("usage_accounting_merge_conflicts")
    for field, expected in expected_counts.items():
        if accounting.get(field) != expected:
            errors.append(f"usage_accounting_{field}_mismatch")

    phase_counts = {
        partition: dict(
            sorted(
                {
                    phase: sum(
                        str(record.get("phase") or "") == phase
                        for record in records
                    )
                    for phase in {
                        str(record.get("phase") or "")
                        for record in records
                        if str(record.get("phase") or "")
                    }
                }.items()
            )
        )
        for partition, records in ledgers.items()
    }
    model_counts = {
        partition: dict(
            sorted(
                {
                    model: sum(
                        str(record.get("model") or "") == model
                        for record in records
                    )
                    for model in {
                        str(record.get("model") or "")
                        for record in records
                        if str(record.get("model") or "")
                    }
                }.items()
            )
        )
        for partition, records in ledgers.items()
    }
    billed_model_counts = {
        partition: dict(
            sorted(
                Counter(
                    str(record.get("billed_model") or "")
                    for record in records
                ).items()
            )
        )
        for partition, records in ledgers.items()
    }
    return {
        "valid": not errors,
        "errors": list(dict.fromkeys(errors)),
        "all_call_count": len(ledgers["all"]),
        "winning_call_count": len(ledgers["winning"]),
        "discarded_call_count": len(ledgers["discarded"]),
        "transport_operation_count": len(
            {
                str(record.get("operation_id") or "")
                for record in ledgers["all"]
                if str(record.get("operation_id") or "")
            }
        ),
        "usage_total": usage["all"],
        "winning_usage_total": usage["winning"],
        "discarded_usage_total": usage["discarded"],
        "phase_counts": phase_counts,
        "model_counts": model_counts,
        "billed_model_counts": billed_model_counts,
        "transport_model_contract": {
            "enforced": expected_transport_model_override is not None,
            "expected_override": expected_transport_model_override,
            "all_calls_match": not any(
                error
                == "provider_billed_model_transport_contract_mismatch"
                for error in errors
            ),
        },
        "call_ids": ids,
        "_records": ledgers,
    }


def public_accounting_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove in-memory ledger rows before persisting a receipt."""

    return {
        str(key): deepcopy(child)
        for key, child in value.items()
        if key != "_records"
    }

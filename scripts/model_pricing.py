"""Frozen exact-model pricing for local no-write validation evidence.

This module is operational tooling, not Lambda runtime policy.  Callers must
load an explicit local JSON table for every cost-validation run.  The table
identity covers its currency, authoritative source metadata, exact model keys,
and every rate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from numbers import Real
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlparse


MODEL_PRICING_SCHEMA = "llamapreview.model-pricing/v2"
RATE_FIELDS = (
    "cache_hit_input_per_million_source_currency",
    "cache_miss_input_per_million_source_currency",
    "output_per_million_source_currency",
)
TOKEN_CLASS_FIELDS = (
    "cache_hit_input_tokens",
    "cache_miss_input_tokens",
    "output_tokens",
)
COMPONENT_FIELDS = (
    "cache_hit_input_usd",
    "cache_miss_input_usd",
    "output_usd",
)
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "source_currency",
        "reporting_currency",
        "authoritative_source",
        "fx_conversion",
        "models",
        "pricing_identity_sha256",
    }
)
_SOURCE_FIELDS = frozenset({"url", "as_of"})
_FX_FIELDS = frozenset(
    {
        "source_currency",
        "reporting_currency",
        "reporting_currency_per_source_currency",
        "euro_reference_rates",
        "derivation_formula",
        "authoritative_source",
    }
)
_EURO_REFERENCE_FIELDS = frozenset({"usd_per_eur", "cny_per_eur"})
_FX_DERIVATION_FORMULA = "usd_per_cny=usd_per_eur/cny_per_eur"
_MILLION = Decimal(1_000_000)


class PricingValidationError(ValueError):
    """A pricing table or cost receipt cannot support exact accounting."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _identity(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _decimal(
    value: Any,
    *,
    field: str,
    allow_zero: bool = True,
) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PricingValidationError(f"{field} must be a non-negative decimal")
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PricingValidationError(
            f"{field} must be a non-negative decimal"
        ) from exc
    if not numeric.is_finite() or numeric < 0 or (not allow_zero and numeric == 0):
        qualifier = "positive" if not allow_zero else "non-negative"
        raise PricingValidationError(f"{field} must be a {qualifier} decimal")
    return numeric


def _decimal_string(value: Any, *, field: str) -> str:
    numeric = _decimal(value, field=field)
    return _format_decimal(numeric)


def _format_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _decimal_sum(values: Iterable[Decimal]) -> Decimal:
    """Add finite evidence decimals without ambient-context rounding drift."""

    with localcontext() as context:
        context.prec = 80
        return sum(values, Decimal(0))


def _decimal_product(*values: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 80
        result = Decimal(1)
        for value in values:
            result *= value
        return result


def normalize_pricing_table(value: Any) -> dict[str, Any]:
    """Validate and canonicalize one closed, exact-model pricing table."""

    if not isinstance(value, Mapping):
        raise PricingValidationError("Pricing table must be a JSON object")
    unknown_root = sorted(set(value) - _ROOT_FIELDS)
    if unknown_root:
        raise PricingValidationError(
            "Pricing table contains unsupported fields: "
            + ", ".join(str(item) for item in unknown_root)
        )
    if value.get("schema_version") != MODEL_PRICING_SCHEMA:
        raise PricingValidationError(
            f"schema_version must equal {MODEL_PRICING_SCHEMA}"
        )
    if value.get("source_currency") != "CNY":
        raise PricingValidationError("source_currency must equal CNY")
    if value.get("reporting_currency") != "USD":
        raise PricingValidationError("reporting_currency must equal USD")

    def normalize_source(source: Any, *, field: str) -> dict[str, str]:
        if not isinstance(source, Mapping):
            raise PricingValidationError(f"{field} must be an object")
        unknown_source = sorted(set(source) - _SOURCE_FIELDS)
        if unknown_source:
            raise PricingValidationError(
                f"{field} contains unsupported fields: "
                + ", ".join(str(item) for item in unknown_source)
            )
        if set(source) != _SOURCE_FIELDS:
            raise PricingValidationError(
                f"{field} must contain exactly url and as_of"
            )
        source_url = str(source.get("url") or "").strip()
        parsed_url = urlparse(source_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.username
            or parsed_url.password
        ):
            raise PricingValidationError(
                f"{field}.url must be an absolute HTTPS URL"
            )
        as_of = str(source.get("as_of") or "").strip()
        try:
            if date.fromisoformat(as_of).isoformat() != as_of:
                raise ValueError
        except ValueError as exc:
            raise PricingValidationError(
                f"{field}.as_of must be an ISO YYYY-MM-DD date"
            ) from exc
        return {"url": source_url, "as_of": as_of}

    source = normalize_source(
        value.get("authoritative_source"),
        field="authoritative_source",
    )
    fx = value.get("fx_conversion")
    if not isinstance(fx, Mapping):
        raise PricingValidationError("fx_conversion must be an object")
    if set(fx) != _FX_FIELDS:
        missing = sorted(_FX_FIELDS - set(fx))
        extra = sorted(set(fx) - _FX_FIELDS)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unsupported " + ", ".join(str(item) for item in extra))
        raise PricingValidationError(
            "fx_conversion must contain the closed frozen conversion evidence"
            + (": " + "; ".join(detail) if detail else "")
        )
    if fx.get("source_currency") != "CNY" or fx.get("reporting_currency") != "USD":
        raise PricingValidationError(
            "fx_conversion currencies must match CNY source and USD reporting"
        )
    reference_rates = fx.get("euro_reference_rates")
    if not isinstance(reference_rates, Mapping) or set(reference_rates) != _EURO_REFERENCE_FIELDS:
        raise PricingValidationError(
            "fx_conversion.euro_reference_rates must contain exactly "
            "usd_per_eur and cny_per_eur"
        )
    usd_per_eur = _decimal(
        reference_rates.get("usd_per_eur"),
        field="fx_conversion.euro_reference_rates.usd_per_eur",
        allow_zero=False,
    )
    cny_per_eur = _decimal(
        reference_rates.get("cny_per_eur"),
        field="fx_conversion.euro_reference_rates.cny_per_eur",
        allow_zero=False,
    )
    if fx.get("derivation_formula") != _FX_DERIVATION_FORMULA:
        raise PricingValidationError(
            f"fx_conversion.derivation_formula must equal {_FX_DERIVATION_FORMULA}"
        )
    fx_rate = _decimal_string(
        fx.get("reporting_currency_per_source_currency"),
        field="fx_conversion.reporting_currency_per_source_currency",
    )
    if Decimal(fx_rate) <= 0:
        raise PricingValidationError(
            "fx_conversion.reporting_currency_per_source_currency must be positive"
        )
    derived_fx_rate = _format_decimal(usd_per_eur / cny_per_eur)
    if fx_rate != derived_fx_rate:
        raise PricingValidationError(
            "fx_conversion.reporting_currency_per_source_currency does not "
            "match the frozen euro reference-rate derivation"
        )
    fx_source = normalize_source(
        fx.get("authoritative_source"),
        field="fx_conversion.authoritative_source",
    )

    raw_models = value.get("models")
    if not isinstance(raw_models, Mapping) or not raw_models:
        raise PricingValidationError(
            "models must be a non-empty object keyed by exact model"
        )
    models: dict[str, dict[str, str]] = {}
    for raw_model, raw_rates in raw_models.items():
        if not isinstance(raw_model, str) or not _MODEL_RE.fullmatch(raw_model):
            raise PricingValidationError(
                f"Invalid exact model key: {raw_model!r}"
            )
        if not isinstance(raw_rates, Mapping):
            raise PricingValidationError(
                f"models.{raw_model} must be an object"
            )
        if set(raw_rates) != set(RATE_FIELDS):
            missing = sorted(set(RATE_FIELDS) - set(raw_rates))
            extra = sorted(set(raw_rates) - set(RATE_FIELDS))
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("unsupported " + ", ".join(str(item) for item in extra))
            raise PricingValidationError(
                f"models.{raw_model} must contain exactly the three rates"
                + (": " + "; ".join(details) if details else "")
            )
        models[raw_model] = {
            field: _decimal_string(
                raw_rates[field],
                field=f"models.{raw_model}.{field}",
            )
            for field in RATE_FIELDS
        }

    normalized: dict[str, Any] = {
        "schema_version": MODEL_PRICING_SCHEMA,
        "source_currency": "CNY",
        "reporting_currency": "USD",
        "authoritative_source": source,
        "fx_conversion": {
            "source_currency": "CNY",
            "reporting_currency": "USD",
            "reporting_currency_per_source_currency": fx_rate,
            "euro_reference_rates": {
                "usd_per_eur": _format_decimal(usd_per_eur),
                "cny_per_eur": _format_decimal(cny_per_eur),
            },
            "derivation_formula": _FX_DERIVATION_FORMULA,
            "authoritative_source": fx_source,
        },
        "models": dict(sorted(models.items())),
    }
    pricing_identity = _identity(normalized)
    supplied_identity = value.get("pricing_identity_sha256")
    if supplied_identity is not None and supplied_identity != pricing_identity:
        raise PricingValidationError(
            "pricing_identity_sha256 does not match the full normalized table"
        )
    normalized["pricing_identity_sha256"] = pricing_identity
    return normalized


def load_pricing_file(path: Path | str) -> dict[str, Any]:
    """Load an explicit absolute JSON file without any network lookup."""

    target = Path(path).expanduser()
    if not target.is_absolute():
        raise PricingValidationError("--pricing-file must be an absolute path")
    if target.is_symlink() or not target.is_file():
        raise PricingValidationError(
            "--pricing-file must name an existing regular JSON file"
        )
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PricingValidationError(
            f"--pricing-file is not readable valid JSON: {exc}"
        ) from exc
    return normalize_pricing_table(value)


def _token_count(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise PricingValidationError(f"{field} must be a non-negative integer")
    numeric = float(value)
    if (
        not math.isfinite(numeric)
        or numeric < 0
        or numeric != math.floor(numeric)
    ):
        raise PricingValidationError(f"{field} must be a non-negative integer")
    return int(numeric)


def _coalesced_token(
    usage: Mapping[str, Any],
    *,
    field: str,
    paths: Sequence[tuple[str, ...]],
) -> int:
    observed: list[tuple[str, int]] = []
    for path in paths:
        parent: Any = usage
        for part in path[:-1]:
            if not isinstance(parent, Mapping) or part not in parent:
                parent = None
                break
            parent = parent[part]
        if not isinstance(parent, Mapping) or path[-1] not in parent:
            continue
        label = ".".join(path)
        observed.append(
            (
                label,
                _token_count(parent[path[-1]], field=f"usage.{label}"),
            )
        )
    if not observed:
        raise PricingValidationError(f"usage.{field} is missing")
    distinct = {numeric for _label, numeric in observed}
    if len(distinct) != 1:
        raise PricingValidationError(f"usage.{field} has conflicting values")
    return observed[0][1]


def _usage_token_classes(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise PricingValidationError("usage must be an object")
    prompt = _coalesced_token(
        value,
        field="prompt_tokens",
        paths=(("prompt_tokens",), ("input_tokens",)),
    )
    output = _coalesced_token(
        value,
        field="completion_tokens",
        paths=(("completion_tokens",), ("output_tokens",)),
    )
    total = _coalesced_token(
        value,
        field="total_tokens",
        paths=(("total_tokens",),),
    )
    cache_hit = _coalesced_token(
        value,
        field="prompt_cache_hit_tokens",
        paths=(
            ("prompt_cache_hit_tokens",),
            ("prompt_cache_details", "hit_tokens"),
        ),
    )
    cache_miss = _coalesced_token(
        value,
        field="prompt_cache_miss_tokens",
        paths=(
            ("prompt_cache_miss_tokens",),
            ("prompt_cache_details", "miss_tokens"),
        ),
    )
    if cache_hit + cache_miss != prompt:
        raise PricingValidationError(
            "usage prompt total does not equal cache-hit plus cache-miss input"
        )
    if prompt + output != total:
        raise PricingValidationError(
            "usage total_tokens does not equal prompt plus output"
        )
    return {
        "cache_hit_input_tokens": cache_hit,
        "cache_miss_input_tokens": cache_miss,
        "output_tokens": output,
    }


def _empty_numeric_subtotal() -> dict[str, Any]:
    return {
        "call_count": 0,
        "token_classes": {field: 0 for field in TOKEN_CLASS_FIELDS},
        "components": {field: Decimal(0) for field in COMPONENT_FIELDS},
        "total_usd": Decimal(0),
    }


def _add_call_to_subtotal(
    subtotal: dict[str, Any],
    *,
    tokens: Mapping[str, int],
    components: Mapping[str, Decimal],
    total: Decimal,
) -> None:
    subtotal["call_count"] += 1
    for field in TOKEN_CLASS_FIELDS:
        subtotal["token_classes"][field] += int(tokens[field])
    for field in COMPONENT_FIELDS:
        subtotal["components"][field] = _decimal_sum(
            (subtotal["components"][field], components[field])
        )
    subtotal["total_usd"] = _decimal_sum((subtotal["total_usd"], total))


def _render_subtotal(value: Mapping[str, Any]) -> dict[str, Any]:
    component_total = _decimal_sum(
        Decimal(value["components"][field]) for field in COMPONENT_FIELDS
    )
    return {
        "call_count": int(value["call_count"]),
        "token_classes": {
            field: int(value["token_classes"][field])
            for field in TOKEN_CLASS_FIELDS
        },
        "components": {
            field: _format_decimal(Decimal(value["components"][field]))
            for field in COMPONENT_FIELDS
        },
        # Derive the rendered total from the exact rendered component source
        # rather than a separately accumulated Decimal path.  Repeating FX
        # quotients can otherwise differ by one context-precision ULP.
        "total_usd": _format_decimal(component_total),
    }


def _incomplete_cost(
    *,
    pricing: Mapping[str, Any],
    errors: Iterable[str],
    calls: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    return {
        "complete": False,
        "errors": list(dict.fromkeys(str(error) for error in errors)),
        "currency": pricing.get("reporting_currency"),
        "pricing_identity_sha256": pricing.get("pricing_identity_sha256"),
        "call_count": len(calls or []),
        "priced_call_count": len(
            [item for item in calls or [] if item.get("total_usd") is not None]
        ),
        "calls": calls or [],
        "token_classes": None,
        "components": None,
        "per_model": {},
        "per_phase": {},
        "total_usd": None,
    }


def price_call_records(
    records: Iterable[Mapping[str, Any]],
    pricing: Mapping[str, Any],
    *,
    expected_usage_total: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Price each all-call ledger row from its own exact model and usage."""

    try:
        table = normalize_pricing_table(pricing)
    except PricingValidationError as exc:
        return _incomplete_cost(pricing={}, errors=[f"pricing_invalid: {exc}"])

    raw_records = list(records)
    errors: list[str] = []
    calls: list[dict[str, Any]] = []
    overall = _empty_numeric_subtotal()
    per_model: dict[str, dict[str, Any]] = {}
    per_phase: dict[str, dict[str, Any]] = {}

    for index, record in enumerate(raw_records):
        prefix = f"call[{index}]"
        receipt: dict[str, Any] = {
            "call_id": (
                str(record.get("call_id") or "")
                if isinstance(record, Mapping)
                else ""
            ),
            "model": None,
            "logical_model": None,
            "billed_model": None,
            "phase": None,
            "token_classes": None,
            "components": None,
            "total_usd": None,
        }
        calls.append(receipt)
        if not isinstance(record, Mapping):
            errors.append(f"{prefix}.record_invalid")
            continue
        model = record.get("model")
        if (
            not isinstance(model, str)
            or not model
            or model != model.strip()
            or not _MODEL_RE.fullmatch(model)
        ):
            errors.append(f"{prefix}.model_missing")
            continue
        receipt["model"] = model
        receipt["logical_model"] = model
        schema_version = record.get("schema_version", 1)
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version < 1
        ):
            errors.append(f"{prefix}.schema_version_invalid")
            continue
        if schema_version >= 2:
            if record.get("logical_model") != model:
                errors.append(f"{prefix}.logical_model_invalid")
                continue
            billed_model = record.get("billed_model")
        else:
            billed_model = model
        if (
            not isinstance(billed_model, str)
            or not billed_model
            or billed_model != billed_model.strip()
            or not _MODEL_RE.fullmatch(billed_model)
        ):
            errors.append(f"{prefix}.billed_model_missing")
            continue
        receipt["billed_model"] = billed_model
        rates = table["models"].get(billed_model)
        if not isinstance(rates, Mapping):
            errors.append(f"{prefix}.billed_model_unknown:{billed_model}")
            continue
        phase = record.get("phase")
        if not isinstance(phase, str) or not phase.strip():
            errors.append(f"{prefix}.phase_missing")
            continue
        phase = phase.strip()
        receipt["phase"] = phase
        if schema_version >= 2:
            transport_attempt_index = record.get("transport_attempt_index")
            transport_dispatch_count = record.get("transport_dispatch_count")
            if (
                isinstance(transport_attempt_index, bool)
                or not isinstance(transport_attempt_index, int)
                or transport_attempt_index < 1
                or isinstance(transport_dispatch_count, bool)
                or transport_dispatch_count != 1
            ):
                errors.append(f"{prefix}.transport_dispatch_identity_invalid")
                continue
        transport_attempt_count = record.get("transport_attempt_count")
        if (
            isinstance(transport_attempt_count, bool)
            or not isinstance(transport_attempt_count, int)
            or transport_attempt_count < 1
        ):
            errors.append(f"{prefix}.transport_attempt_count_invalid")
            continue
        if transport_attempt_count > 1:
            errors.append(f"{prefix}.retry_dispatch_usage_unreported")
            continue
        if record.get("usage_state") != "reported":
            errors.append(f"{prefix}.usage_unreported")
            continue
        if record.get("usage_validation_errors") not in (None, []):
            errors.append(f"{prefix}.usage_validation_errors_reported")
            continue
        if record.get("status") != "completed":
            errors.append(f"{prefix}.call_not_completed")
            continue
        try:
            tokens = _usage_token_classes(record.get("usage"))
        except PricingValidationError as exc:
            errors.append(f"{prefix}.{exc}")
            continue
        decimal_rates = {
            field: _decimal(
                rates.get(field),
                field=f"models.{billed_model}.{field}",
            )
            for field in RATE_FIELDS
        }
        fx_rate = _decimal(
            table["fx_conversion"][
                "reporting_currency_per_source_currency"
            ],
            field="fx_conversion.reporting_currency_per_source_currency",
            allow_zero=False,
        )
        usd_rates = {
            "cache_hit_input_per_million_usd": (
                _decimal_product(
                    decimal_rates["cache_hit_input_per_million_source_currency"],
                    fx_rate,
                )
            ),
            "cache_miss_input_per_million_usd": (
                _decimal_product(
                    decimal_rates["cache_miss_input_per_million_source_currency"],
                    fx_rate,
                )
            ),
            "output_per_million_usd": (
                _decimal_product(
                    decimal_rates["output_per_million_source_currency"],
                    fx_rate,
                )
            ),
        }
        components = {
            "cache_hit_input_usd": (
                _decimal_product(
                    Decimal(tokens["cache_hit_input_tokens"]),
                    usd_rates["cache_hit_input_per_million_usd"],
                    Decimal("0.000001"),
                )
            ),
            "cache_miss_input_usd": (
                _decimal_product(
                    Decimal(tokens["cache_miss_input_tokens"]),
                    usd_rates["cache_miss_input_per_million_usd"],
                    Decimal("0.000001"),
                )
            ),
            "output_usd": (
                _decimal_product(
                    Decimal(tokens["output_tokens"]),
                    usd_rates["output_per_million_usd"],
                    Decimal("0.000001"),
                )
            ),
        }
        total = _decimal_sum(components.values())
        receipt.update(
            {
                "token_classes": dict(tokens),
                "source_currency": table["source_currency"],
                "rates_per_million_source_currency": dict(rates),
                "rates_per_million_usd": {
                    field: _format_decimal(rate)
                    for field, rate in usd_rates.items()
                },
                "components": {
                    field: _format_decimal(components[field])
                    for field in COMPONENT_FIELDS
                },
                "total_usd": _format_decimal(total),
            }
        )
        _add_call_to_subtotal(
            overall,
            tokens=tokens,
            components=components,
            total=total,
        )
        _add_call_to_subtotal(
            per_model.setdefault(billed_model, _empty_numeric_subtotal()),
            tokens=tokens,
            components=components,
            total=total,
        )
        _add_call_to_subtotal(
            per_phase.setdefault(phase, _empty_numeric_subtotal()),
            tokens=tokens,
            components=components,
            total=total,
        )

    if expected_usage_total is not None:
        try:
            expected_tokens = _usage_token_classes(expected_usage_total)
        except PricingValidationError as exc:
            errors.append(f"aggregate_usage.{exc}")
        else:
            if expected_tokens != overall["token_classes"]:
                errors.append("aggregate_usage.token_class_total_mismatch")
    if errors:
        return _incomplete_cost(pricing=table, errors=errors, calls=calls)

    rendered = _render_subtotal(overall)
    return {
        "complete": True,
        "errors": [],
        "currency": table["reporting_currency"],
        "pricing_identity_sha256": table["pricing_identity_sha256"],
        "call_count": rendered["call_count"],
        "priced_call_count": rendered["call_count"],
        "calls": calls,
        "token_classes": rendered["token_classes"],
        "components": rendered["components"],
        "per_model": {
            key: _render_subtotal(value)
            for key, value in sorted(per_model.items())
        },
        "per_phase": {
            key: _render_subtotal(value)
            for key, value in sorted(per_phase.items())
        },
        "total_usd": rendered["total_usd"],
    }


def zero_cost_receipt(pricing: Mapping[str, Any]) -> dict[str, Any]:
    """Represent an explicitly allowed policy skip with no provider dispatch."""

    table = normalize_pricing_table(pricing)
    rendered = _render_subtotal(_empty_numeric_subtotal())
    return {
        "complete": True,
        "errors": [],
        "currency": table["reporting_currency"],
        "pricing_identity_sha256": table["pricing_identity_sha256"],
        "call_count": 0,
        "priced_call_count": 0,
        "calls": [],
        "token_classes": rendered["token_classes"],
        "components": rendered["components"],
        "per_model": {},
        "per_phase": {},
        "total_usd": rendered["total_usd"],
    }


def _receipt_subtotal(
    value: Any,
    *,
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PricingValidationError(f"{field} must be an object")
    call_count = value.get("call_count")
    if (
        isinstance(call_count, bool)
        or not isinstance(call_count, int)
        or call_count < 0
    ):
        raise PricingValidationError(
            f"{field}.call_count must be a non-negative integer"
        )
    tokens = value.get("token_classes")
    if not isinstance(tokens, Mapping):
        raise PricingValidationError(f"{field}.token_classes must be an object")
    token_classes = {
        key: _token_count(tokens.get(key), field=f"{field}.token_classes.{key}")
        for key in TOKEN_CLASS_FIELDS
    }
    components = value.get("components")
    if not isinstance(components, Mapping):
        raise PricingValidationError(f"{field}.components must be an object")
    decimal_components = {
        key: _decimal(
            components.get(key),
            field=f"{field}.components.{key}",
        )
        for key in COMPONENT_FIELDS
    }
    total = _decimal(value.get("total_usd"), field=f"{field}.total_usd")
    if _decimal_sum(decimal_components.values()) != total:
        raise PricingValidationError(f"{field}.component_total_mismatch")
    return {
        "call_count": call_count,
        "token_classes": token_classes,
        "components": decimal_components,
        "total_usd": total,
    }


def sum_cost_receipts(
    receipts: Iterable[Mapping[str, Any]],
    pricing: Mapping[str, Any],
    *,
    cell_ids: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Sum already-priced cell receipts without repricing merged usage."""

    try:
        table = normalize_pricing_table(pricing)
    except PricingValidationError as exc:
        return _incomplete_cost(pricing={}, errors=[f"pricing_invalid: {exc}"])
    values = list(receipts)
    identities = list(cell_ids) if cell_ids is not None else [
        str(index) for index in range(len(values))
    ]
    if len(identities) != len(values):
        return _incomplete_cost(
            pricing=table,
            errors=["cell_identity_count_mismatch"],
        )

    errors: list[str] = []
    overall = _empty_numeric_subtotal()
    per_model: dict[str, dict[str, Any]] = {}
    per_phase: dict[str, dict[str, Any]] = {}
    per_cell: list[dict[str, Any]] = []
    for index, (cell_id, receipt) in enumerate(zip(identities, values)):
        prefix = f"cell[{index}]"
        if not isinstance(receipt, Mapping) or receipt.get("complete") is not True:
            errors.append(f"{prefix}.cost_incomplete")
            continue
        if receipt.get("currency") != table["reporting_currency"]:
            errors.append(f"{prefix}.currency_mismatch")
        if (
            receipt.get("pricing_identity_sha256")
            != table["pricing_identity_sha256"]
        ):
            errors.append(f"{prefix}.pricing_identity_mismatch")
        try:
            cell = _receipt_subtotal(receipt, field=prefix)
        except PricingValidationError as exc:
            errors.append(str(exc))
            continue
        model_view = receipt.get("per_model")
        phase_view = receipt.get("per_phase")
        if not isinstance(model_view, Mapping):
            errors.append(f"{prefix}.per_model must be an object")
            continue
        if not isinstance(phase_view, Mapping):
            errors.append(f"{prefix}.per_phase must be an object")
            continue

        def accumulate_view(
            raw_view: Mapping[str, Any],
            *,
            label: str,
            target: dict[str, dict[str, Any]],
        ) -> dict[str, Any]:
            view_total = _empty_numeric_subtotal()
            for key, raw_subtotal in raw_view.items():
                if not isinstance(key, str) or not key.strip():
                    raise PricingValidationError(
                        f"{prefix}.{label} has an invalid key"
                    )
                subtotal = _receipt_subtotal(
                    raw_subtotal,
                    field=f"{prefix}.{label}.{key}",
                )
                view_total["call_count"] += subtotal["call_count"]
                for token_field in TOKEN_CLASS_FIELDS:
                    view_total["token_classes"][token_field] += subtotal[
                        "token_classes"
                    ][token_field]
                for component_field in COMPONENT_FIELDS:
                    view_total["components"][component_field] = _decimal_sum(
                        (
                            view_total["components"][component_field],
                            subtotal["components"][component_field],
                        )
                    )
                view_total["total_usd"] = _decimal_sum(
                    (view_total["total_usd"], subtotal["total_usd"])
                )

                destination = target.setdefault(key, _empty_numeric_subtotal())
                destination["call_count"] += subtotal["call_count"]
                for token_field in TOKEN_CLASS_FIELDS:
                    destination["token_classes"][token_field] += subtotal[
                        "token_classes"
                    ][token_field]
                for component_field in COMPONENT_FIELDS:
                    destination["components"][component_field] = _decimal_sum(
                        (
                            destination["components"][component_field],
                            subtotal["components"][component_field],
                        )
                    )
                destination["total_usd"] = _decimal_sum(
                    (destination["total_usd"], subtotal["total_usd"])
                )
            view_total["total_usd"] = _decimal_sum(
                Decimal(view_total["components"][field])
                for field in COMPONENT_FIELDS
            )
            return view_total

        try:
            model_total = accumulate_view(
                model_view,
                label="per_model",
                target=per_model,
            )
            phase_total = accumulate_view(
                phase_view,
                label="per_phase",
                target=per_phase,
            )
        except PricingValidationError as exc:
            errors.append(str(exc))
            continue
        for label, view_total in (
            ("per_model", model_total),
            ("per_phase", phase_total),
        ):
            if (
                view_total["call_count"] != cell["call_count"]
                or view_total["token_classes"] != cell["token_classes"]
                or view_total["components"] != cell["components"]
                or view_total["total_usd"] != cell["total_usd"]
            ):
                errors.append(f"{prefix}.{label}_subtotal_mismatch")

        overall["call_count"] += cell["call_count"]
        for token_field in TOKEN_CLASS_FIELDS:
            overall["token_classes"][token_field] += cell["token_classes"][
                token_field
            ]
        for component_field in COMPONENT_FIELDS:
            overall["components"][component_field] = _decimal_sum(
                (
                    overall["components"][component_field],
                    cell["components"][component_field],
                )
            )
        overall["total_usd"] = _decimal_sum(
            (overall["total_usd"], cell["total_usd"])
        )
        per_cell.append(
            {
                "cell_id": str(cell_id),
                "call_count": cell["call_count"],
                "total_usd": _format_decimal(cell["total_usd"]),
            }
        )

    if errors:
        return _incomplete_cost(pricing=table, errors=errors)
    rendered = _render_subtotal(overall)
    return {
        "complete": True,
        "errors": [],
        "currency": table["reporting_currency"],
        "pricing_identity_sha256": table["pricing_identity_sha256"],
        "cell_count": len(values),
        "call_count": rendered["call_count"],
        "priced_call_count": rendered["call_count"],
        "token_classes": rendered["token_classes"],
        "components": rendered["components"],
        "per_model": {
            key: _render_subtotal(value)
            for key, value in sorted(per_model.items())
        },
        "per_phase": {
            key: _render_subtotal(value)
            for key, value in sorted(per_phase.items())
        },
        "per_cell": per_cell,
        "total_usd": rendered["total_usd"],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Validate a private table and print its full normalized identity."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pricing-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        pricing = load_pricing_file(args.pricing_file)
    except PricingValidationError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            pricing,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

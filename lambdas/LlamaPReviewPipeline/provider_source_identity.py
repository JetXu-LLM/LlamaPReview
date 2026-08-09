"""Dependency-free canonical primitives for provider-source receipts."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import re
from typing import Any, Dict, Mapping


PROVIDER_SOURCE_SCHEMA = "llamapreview.provider-source/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROVIDER_SOURCE_COMPONENTS = frozenset(
    {
        "identity",
        "pr_content",
        "model_pr_details",
        "ci_snapshot",
        "repo_inventory",
        "route_digest",
    }
)


class ProviderSourceReceiptError(ValueError):
    """A content-free provider-source receipt is malformed or inconsistent."""


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, set):
        return sorted(value)
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    raise TypeError(
        f"Provider source contains non-canonical {type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def provider_source_receipt_sha256(
    component_sha256: Mapping[str, Any],
) -> str:
    return sha256_value(
        {
            "schema": PROVIDER_SOURCE_SCHEMA,
            "component_sha256": dict(component_sha256),
        }
    )


def validate_provider_source_receipt(
    receipt: Any,
    *,
    expected_sha256: str = "",
) -> Dict[str, Any]:
    """Validate the complete self-authenticating content-free receipt."""

    if not isinstance(receipt, Mapping):
        raise ProviderSourceReceiptError(
            "Provider source receipt must be an object"
        )
    components = receipt.get("component_sha256")
    if (
        receipt.get("schema") != PROVIDER_SOURCE_SCHEMA
        or not isinstance(components, Mapping)
        or set(components) != PROVIDER_SOURCE_COMPONENTS
    ):
        raise ProviderSourceReceiptError(
            "Provider source receipt schema/components are incomplete"
        )
    normalized_components = {
        str(name): str(value or "")
        for name, value in components.items()
    }
    if any(
        not SHA256_RE.fullmatch(value)
        for value in normalized_components.values()
    ):
        raise ProviderSourceReceiptError(
            "Provider source component digest is invalid"
        )
    route_input = str(receipt.get("route_input_sha256") or "")
    digest = str(receipt.get("sha256") or "")
    if (
        route_input != normalized_components["route_digest"]
        or digest
        != provider_source_receipt_sha256(normalized_components)
        or (expected_sha256 and digest != str(expected_sha256))
    ):
        raise ProviderSourceReceiptError(
            "Provider source receipt identity is inconsistent"
        )
    return {
        "schema": PROVIDER_SOURCE_SCHEMA,
        "sha256": digest,
        "component_sha256": normalized_components,
        "route_input_sha256": route_input,
    }

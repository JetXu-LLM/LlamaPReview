"""Resolve the selected provider's logical model to the model sent over HTTP.

The override is owned at the provider transport boundary. The pipeline keeps
selecting and recording its logical model tier while this module preserves the
exact billed transport identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping


KNOWN_DEEPSEEK_MODELS = frozenset(
    {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    }
)
OPENROUTER_LUNA_MODEL = "openai/gpt-6-luna"
DIRECT_OPENAI_EFFORTS = {
    "gpt-6-luna": frozenset({"medium", "max"}),
    "gpt-6.1-terra": frozenset({"low", "medium", "high", "xhigh", "max"}),
}


@dataclass(frozen=True)
class StageModelSelection:
    model: str
    reasoning_effort: str
    profile_version: str
    profile_sha256: str
    stage: str
    tier: str

    def metadata(self) -> dict[str, str]:
        return {
            "profile_version": self.profile_version,
            "profile_sha256": self.profile_sha256,
            "stage": self.stage,
            "tier": self.tier,
        }


@dataclass(frozen=True)
class DirectOpenAIProfile:
    """An immutable stage table, not a routing classifier or escalation chain."""

    version: str
    sha256: str
    entries: tuple[tuple[str, str, str, str], ...]
    reconcile_cache: str = "off"

    def select(self, stage: str, tier: str = "") -> StageModelSelection:
        for entry_stage, entry_tier, model, effort in self.entries:
            if (entry_stage, entry_tier) == (stage, tier):
                return StageModelSelection(
                    model, effort, self.version, self.sha256, stage, tier
                )
        raise ValueError("Direct OpenAI profile has no such stage/tier")


def validate_direct_openai_selection(model: object, effort: object) -> None:
    if not isinstance(model, str) or model not in DIRECT_OPENAI_EFFORTS:
        raise ValueError("Direct OpenAI requires an approved exact model ID")
    if not isinstance(effort, str) or effort not in DIRECT_OPENAI_EFFORTS[model]:
        raise ValueError("Direct OpenAI reasoning effort is not approved for this model")


def parse_direct_openai_profile(raw: str) -> DirectOpenAIProfile:
    """Require every existing payable stage explicitly; skip buys no calls.

    JSON pairs are [exact model ID, effort] to keep Lambda configuration small.
    No defaults, wildcard models, aliases, fallback, or future model upgrade.
    Qualification and a valid grant allocation are separate activation gates.
    """

    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 4096:
        raise ValueError("DIRECT_OPENAI_PROFILE must be explicit bounded JSON")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Direct OpenAI profile contains duplicate keys")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique_object)
    if (not isinstance(value, dict)
            or not {"version", "route", "tiers"} <= set(value)
            or set(value) - {"version", "route", "tiers", "reconcile_cache"}):
        raise ValueError("Direct OpenAI profile fields are missing or unsupported")
    cache = value.get("reconcile_cache", "off")
    if cache not in ("off", "same_run_30m"):
        raise ValueError("Direct OpenAI Reconcile cache mode is unsupported")
    version = value["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
        raise ValueError("Direct OpenAI profile version is invalid")
    entries: list[tuple[str, str, str, str]] = []

    def add(stage: str, tier: str, pair: Any) -> None:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("Direct OpenAI profile cells require [model, effort]")
        model, effort = pair
        validate_direct_openai_selection(model, effort)
        entries.append((stage, tier, model, effort))

    add("route", "", value["route"])
    tiers = value["tiers"]
    if not isinstance(tiers, Mapping) or set(tiers) != {"low", "normal", "high"}:
        raise ValueError("Direct OpenAI profile requires exactly low/normal/high tiers")
    for tier in ("low", "normal", "high"):
        stages = ("deep", "final") if tier == "low" else (
            "pfr_plan", "pfr_reconcile", "deep", "final"
        )
        cells = tiers[tier]
        if not isinstance(cells, Mapping) or set(cells) != set(stages):
            raise ValueError("Direct OpenAI profile stage table is incomplete or unexpected")
        for stage in stages:
            add(stage, tier, cells[stage])
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return DirectOpenAIProfile(
        version, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), tuple(entries), cache
    )


@dataclass(frozen=True)
class ProviderModelSelection:
    logical_model: str
    billed_model: str

    @property
    def override_applied(self) -> bool:
        return self.logical_model != self.billed_model


def _known_model(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty exact model name")
    if value not in KNOWN_DEEPSEEK_MODELS:
        raise ValueError(f"{field} is not an approved exact model: {value!r}")
    return value


def resolve_provider_model(
    logical_model: object,
    transport_model_override: object,
    *,
    provider: str = "deepseek",
) -> ProviderModelSelection:
    """Return the logical and billed model, failing closed on ambiguity.

    An exact empty string deliberately disables the transport override and
    restores normal logical-model dispatch.  Whitespace-only values are
    rejected because they are usually an operational configuration mistake.
    """

    if provider == "direct_openai":
        if logical_model not in DIRECT_OPENAI_EFFORTS or transport_model_override != "":
            raise ValueError("Direct OpenAI requires an approved exact model without override")
        return ProviderModelSelection(logical_model=logical_model, billed_model=logical_model)
    if provider == "openrouter":
        if logical_model != OPENROUTER_LUNA_MODEL or transport_model_override != "":
            raise ValueError("OpenRouter profile requires exact Luna model without override")
        return ProviderModelSelection(
            logical_model=OPENROUTER_LUNA_MODEL,
            billed_model=OPENROUTER_LUNA_MODEL,
        )
    if provider != "deepseek":
        raise ValueError(f"Unsupported model provider: {provider!r}")
    logical = _known_model(logical_model, field="logical_model")
    if transport_model_override == "":
        billed = logical
    else:
        billed = _known_model(
            transport_model_override,
            field="transport_model_override",
        )
    return ProviderModelSelection(logical_model=logical, billed_model=billed)

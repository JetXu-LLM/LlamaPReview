"""Resolve logical DeepSeek models to the model sent over HTTP.

The override is owned at the provider transport boundary. The pipeline keeps
selecting and recording its logical model tier while this module preserves the
exact billed transport identity.
"""

from __future__ import annotations

from dataclasses import dataclass


KNOWN_DEEPSEEK_MODELS = frozenset(
    {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    }
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
) -> ProviderModelSelection:
    """Return the logical and billed model, failing closed on ambiguity.

    An exact empty string deliberately disables the transport override and
    restores normal logical-model dispatch.  Whitespace-only values are
    rejected because they are usually an operational configuration mistake.
    """

    logical = _known_model(logical_model, field="logical_model")
    if transport_model_override == "":
        billed = logical
    else:
        billed = _known_model(
            transport_model_override,
            field="transport_model_override",
        )
    return ProviderModelSelection(logical_model=logical, billed_model=billed)

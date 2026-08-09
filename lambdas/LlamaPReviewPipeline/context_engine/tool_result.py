"""Typed result shared by context retrieval capabilities and their executor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class ToolResult:
    """One content result plus content-free retrieval diagnostics."""

    text: str
    outcome: str
    error_kind: str = ""
    source_ref: str = ""
    head_reread_outcome: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

"""Private, content-free identity for one Lambda phase invocation."""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional


__all__ = ["capture_runtime_identity"]


def _bounded_scalar(value: Any, *, max_chars: int) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    return str(value).strip()[:max_chars]


def capture_runtime_identity(
    lambda_context: Any = None,
    *,
    phase: str,
    environ: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Return only immutable AWS invocation identifiers safe to persist."""

    source = os.environ if environ is None else environ
    try:
        request_id = getattr(lambda_context, "aws_request_id", "")
    except Exception:
        request_id = ""
    return {
        "schema_version": 1,
        "phase": _bounded_scalar(phase, max_chars=16),
        "function_version": _bounded_scalar(
            source.get("AWS_LAMBDA_FUNCTION_VERSION", ""),
            max_chars=64,
        ),
        "log_stream_name": _bounded_scalar(
            source.get("AWS_LAMBDA_LOG_STREAM_NAME", ""),
            max_chars=512,
        ),
        "aws_request_id": _bounded_scalar(
            request_id,
            max_chars=128,
        ),
    }

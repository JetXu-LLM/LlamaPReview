"""DeepSeek V4 client for thinking-mode review and tool loops."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
import uuid
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional

from . import config
from .deadline import Deadline, DeadlineExceeded
from .provider_model_routing import resolve_provider_model
from .provider_usage import validate_complete_token_usage

logger = logging.getLogger(__name__)

try:
    import requests  # type: ignore
except ModuleNotFoundError:  # local offline mock scripts may not need HTTP
    requests = None  # type: ignore


_REDACTED_KEYS = {"authorization", "api_key", "apikey", "token", "access_token", "private_key"}
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b"),
    re.compile(r"\bgh[psuor]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|private[_-]?key)\s*[:=]\s*['\"]?[^'\"\s]{12,}"),
)

PROVIDER_CALL_RECORD_KEY = "_llamapreview_provider_call"
_CANONICAL_PHASE_NAMES = {
    "pr_analyzer": "route",
    "pr_analyzer_adjudication": "route_adjudication",
}


def canonical_provider_phase(value: Any) -> str:
    """Return the stable phase vocabulary used by durable model-call ledgers."""

    phase = str(value or "unknown").strip() or "unknown"
    return _CANONICAL_PHASE_NAMES.get(phase, phase)


class DeepSeekError(RuntimeError):
    """Base class for typed DeepSeek transport/response failures."""


class DeepSeekHTTPError(DeepSeekError):
    def __init__(self, message: str, *, status_code: Optional[int] = None):
        self.status_code = status_code
        super().__init__(message)


class DeepSeekResponseError(DeepSeekError):
    """The service response could not be decoded as a response object."""


class DeepSeekTransportError(DeepSeekError):
    """The HTTP transport failed without a model HTTP response."""


class DeepSeekTimeoutError(DeepSeekTransportError):
    """The model HTTP request timed out after bounded retries."""


class ProviderCallLedgerError(DeepSeekError):
    """A paid provider dispatch could not enter the durable call ledger."""

    paid_dispatch_unrecorded = True
    provider_call_control_failure = True

    def __init__(self, message: str, *, provider_call_record: Dict[str, Any]):
        self.provider_call_record = deepcopy(provider_call_record)
        # Review's generic failed-phase collector understands telemetry;
        # Context can consume provider_call_record explicitly at its terminal
        # persistence boundary.
        self.telemetry = deepcopy(provider_call_record)
        super().__init__(message)


class ProviderCallFenceError(DeepSeekError):
    """The durable pre-dispatch fence could not be proven before HTTP."""

    provider_dispatch_not_started = True
    provider_call_control_failure = True

    def __init__(self, message: str, *, provider_call_record: Dict[str, Any]):
        self.provider_call_record = deepcopy(provider_call_record)
        self.telemetry = deepcopy(provider_call_record)
        super().__init__(message)


class ProviderDispatchOutcomeUnknown(DeepSeekError):
    """A prior fenced dispatch has no durable terminal transport outcome."""

    provider_dispatch_outcome_unknown = True
    provider_call_control_failure = True

    def __init__(self, message: str, *, provider_call_record: Dict[str, Any]):
        self.provider_call_record = deepcopy(provider_call_record)
        self.telemetry = deepcopy(provider_call_record)
        super().__init__(message)


def _trace_mode() -> str:
    mode = os.environ.get("DEEPSEEK_TRACE_MODE", config.DEEPSEEK_TRACE_MODE).strip().lower()
    return mode if mode in {"full", "summary", "off"} else "summary"


def _trace_chunk_chars() -> int:
    raw = os.environ.get("DEEPSEEK_TRACE_CHUNK_CHARS")
    if raw and raw.isdigit():
        return max(1000, int(raw))
    return max(1000, int(config.DEEPSEEK_TRACE_CHUNK_CHARS))


def _trace_dir() -> str:
    return os.environ.get("DEEPSEEK_TRACE_DIR", config.DEEPSEEK_TRACE_DIR).strip()


def _trace_bucket() -> str:
    return os.environ.get("DEEPSEEK_TRACE_S3_BUCKET", config.DEEPSEEK_TRACE_S3_BUCKET).strip()


def _get_s3_client():
    import boto3

    return boto3.client("s3")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if str(key).lower() in _REDACTED_KEYS else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern in _SECRET_VALUE_PATTERNS:
            redacted = pattern.sub("[REDACTED_SECRET]", redacted)
        return redacted
    return value


def _message_summary(message: Dict[str, Any]) -> Dict[str, Any]:
    tool_calls = message.get("tool_calls") or []
    return {
        "content_chars": len(message.get("content") or ""),
        "reasoning_content_chars": len(message.get("reasoning_content") or ""),
        "tool_call_count": len(tool_calls),
        "tool_call_names": [
            ((call.get("function") or {}).get("name") or "unknown")
            for call in tool_calls
            if isinstance(call, dict)
        ],
    }


def _write_local_trace(event: Dict[str, Any]) -> None:
    directory = _trace_dir()
    if not directory:
        return
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    run_id = str((event.get("metadata") or {}).get("run_id") or "deepseek_trace")
    run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
    target = path / f"{run_id}.jsonl.gz"
    with gzip.open(target, "at", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def _summary_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Return the CloudWatch-safe projection, never prompts or model output."""
    return {
        "trace_id": event.get("trace_id"),
        "mode": event.get("mode"),
        "phase": event.get("phase"),
        "metadata": event.get("metadata") or {},
        "summary": event.get("summary") or {},
    }


def _emit_cloudwatch_trace(event: Dict[str, Any]) -> None:
    logger.info(
        "DeepSeek trace summary: %s",
        json.dumps(_summary_event(event), ensure_ascii=False, default=str),
    )


def trace_s3_prefix(metadata: Dict[str, Any]) -> str:
    """Return the content-free S3 prefix shared by one Pipeline run."""

    components = [
        str(config.RUN_ARTIFACT_PREFIX or "pipeline").strip("/"),
        "deepseek-traces",
        str(metadata.get("repo") or "unknown-repo"),
        f"pr-{metadata.get('pr_number') or 'unknown'}",
        str(metadata.get("head_sha") or "unknown-head"),
        str(metadata.get("run_id") or "unknown-run"),
    ]
    return "/".join(
        re.sub(r"[^A-Za-z0-9._=/-]+", "_", component).strip("/") or "unknown"
        for component in components
    )


def _trace_s3_key(event: Dict[str, Any]) -> str:
    metadata = event.get("metadata") or {}
    suffix = "/".join(
        (
            re.sub(
                r"[^A-Za-z0-9._=/-]+",
                "_",
                str(event.get("phase") or "unknown"),
            ).strip("/")
            or "unknown",
            f"{event.get('trace_id') or uuid.uuid4().hex}.json.gz",
        )
    )
    return f"{trace_s3_prefix(metadata)}/{suffix}"


def _write_s3_trace(event: Dict[str, Any]) -> None:
    bucket = _trace_bucket()
    if not bucket:
        return
    payload = gzip.compress(
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"),
        mtime=0,
    )
    digest = hashlib.sha256(payload).hexdigest()
    _get_s3_client().put_object(
        Bucket=bucket,
        Key=_trace_s3_key(event),
        Body=payload,
        ContentType="application/json",
        ContentEncoding="gzip",
        ServerSideEncryption="AES256",
        Metadata={"sha256": digest, "artifact-kind": "deepseek-trace"},
    )


def _build_trace_event(
    *,
    mode: str,
    payload: Dict[str, Any],
    result: Dict[str, Any],
    elapsed_seconds: float,
    trace_phase: Optional[str],
    trace_metadata: Optional[Dict[str, Any]],
    attempt_count: int,
    last_attempt_elapsed_seconds: Optional[float] = None,
    provider_call_record: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    message = (result.get("choices") or [{}])[0].get("message", {})
    logical_model = payload.get("_llamapreview_logical_model")
    billed_model = payload.get("_llamapreview_billed_model")
    summary = {
        # ``model`` remains the compatibility alias for the logical routing
        # choice; billed_model is the exact provider-dispatched identity.
        "model": logical_model,
        "logical_model": logical_model,
        "billed_model": billed_model,
        "call_id": str((provider_call_record or {}).get("call_id") or ""),
        "operation_id": str(
            (provider_call_record or {}).get("operation_id") or ""
        ),
        "pipeline_phase": str(
            (provider_call_record or {}).get("pipeline_phase") or ""
        ),
        "pipeline_attempt": int(
            (provider_call_record or {}).get("pipeline_attempt") or 0
        ),
        "phase": str((provider_call_record or {}).get("phase") or ""),
        "call_index": int(
            (provider_call_record or {}).get("call_index") or 0
        ),
        "transport_attempt_index": int(
            (provider_call_record or {}).get("transport_attempt_index") or 0
        ),
        "api_variant": payload.get("_llamapreview_api_variant", "stable"),
        "reasoning_effort": payload.get("reasoning_effort"),
        "thinking": (payload.get("thinking") or {}).get("type") == "enabled",
        "thinking_type": (payload.get("thinking") or {}).get("type"),
        "response_format": payload.get("response_format"),
        "strict_tool_count": sum(
            1
            for tool in payload.get("tools") or []
            if isinstance(tool, dict)
            and isinstance(tool.get("function"), dict)
            and tool["function"].get("strict") is True
        ),
        "tool_choice_kind": (
            "named"
            if isinstance(payload.get("tool_choice"), dict)
            else payload.get("tool_choice")
        ),
        "usage": result.get("usage") or {},
        "elapsed_seconds": round(elapsed_seconds, 3),
        "last_attempt_elapsed_seconds": round(
            elapsed_seconds
            if last_attempt_elapsed_seconds is None
            else max(0.0, float(last_attempt_elapsed_seconds)),
            3,
        ),
        "attempt_count": int(attempt_count),
        **_message_summary(message if isinstance(message, dict) else {}),
    }
    event: Dict[str, Any] = {
        "trace_id": uuid.uuid4().hex,
        "mode": mode,
        "phase": summary["phase"] or trace_phase or "unknown",
        "metadata": _redact(trace_metadata or {}),
        "summary": summary,
    }
    if mode == "full":
        event["request"] = _redact(
            {
                key: value
                for key, value in payload.items()
                if not key.startswith("_llamapreview_")
            }
        )
        event["response"] = _redact(result)
    return event


def _emit_failure_summary(
    *,
    trace_phase: Optional[str],
    trace_metadata: Optional[Dict[str, Any]],
    attempt_count: int,
    status_code: Optional[int],
    error: BaseException,
    elapsed_seconds: float,
) -> None:
    logger.warning(
        "DeepSeek failure summary: %s",
        json.dumps(
            {
                "phase": trace_phase or "unknown",
                "metadata": _redact(trace_metadata or {}),
                "attempt_count": int(attempt_count),
                "status_code": status_code,
                "error_class": error.__class__.__name__,
                "elapsed_seconds": round(max(0.0, elapsed_seconds), 3),
            },
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        ),
    )


class DeepSeekClient:
    API_BASE = "https://api.deepseek.com"
    API_BETA_BASE = "https://api.deepseek.com/beta"
    CHAT_PATH = "/chat/completions"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: str = config.DEEPSEEK_MODEL,
        reasoning_effort: str = config.DEEPSEEK_EFFORT,
        timeout: int = config.DEEPSEEK_TIMEOUT_SECONDS,
        transport_model_override: str = config.DEEPSEEK_TRANSPORT_MODEL_OVERRIDE,
    ):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise ValueError("DEEPSEEK_API_KEY is not set.")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.transport_model_override = transport_model_override
        self._provider_dispatch_fence_sink: Optional[
            Callable[[Dict[str, Any]], None]
        ] = None
        self._provider_call_sink: Optional[Callable[[Dict[str, Any]], None]] = None
        self._provider_call_ordinals: Dict[tuple[str, int, str], int] = {}
        self._provider_call_records: List[Dict[str, Any]] = []

    def set_provider_call_sink(
        self,
        sink: Optional[Callable[[Dict[str, Any]], None]],
    ) -> Optional[Callable[[Dict[str, Any]], None]]:
        """Install a content-free completed-operation sink and return the old one."""

        previous = self._provider_call_sink
        self._provider_call_sink = sink
        return previous

    def set_provider_dispatch_fence_sink(
        self,
        sink: Optional[Callable[[Dict[str, Any]], None]],
    ) -> Optional[Callable[[Dict[str, Any]], None]]:
        """Install the durable authorization boundary for each HTTP attempt."""

        previous = self._provider_dispatch_fence_sink
        self._provider_dispatch_fence_sink = sink
        return previous

    def provider_call_records(self) -> List[Dict[str, Any]]:
        """Return this client's content-free provider operation ledger."""

        return deepcopy(self._provider_call_records)

    def _begin_provider_operation(
        self,
        *,
        trace_phase: Optional[str],
        trace_metadata: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Allocate one stable logical operation before any HTTP dispatch."""

        metadata = trace_metadata if isinstance(trace_metadata, dict) else {}
        pipeline_phase = str(metadata.get("pipeline_phase") or "").strip()
        try:
            pipeline_attempt = max(
                0,
                int(metadata.get("pipeline_attempt") or 0),
            )
        except (TypeError, ValueError):
            pipeline_attempt = 0
        phase = canonical_provider_phase(trace_phase)
        ordinal_key = (pipeline_phase, pipeline_attempt, phase)
        call_index = self._provider_call_ordinals.get(ordinal_key, 0) + 1
        self._provider_call_ordinals[ordinal_key] = call_index
        identity = {
            "run_id": str(metadata.get("run_id") or ""),
            "head_sha": str(metadata.get("head_sha") or ""),
            "pipeline_phase": pipeline_phase,
            "pipeline_attempt": pipeline_attempt,
            "phase": phase,
            "call_index": call_index,
        }
        return {
            **identity,
            "operation_id": hashlib.sha256(
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }

    def _record_provider_call(
        self,
        *,
        payload: Dict[str, Any],
        result: Optional[Dict[str, Any]],
        operation: Dict[str, Any],
        transport_attempt_index: int,
        status: str,
        elapsed_seconds: float,
        last_attempt_elapsed_seconds: Optional[float] = None,
        http_status: Optional[int] = None,
        error: Optional[BaseException] = None,
        persist_sink: bool = True,
    ) -> Dict[str, Any]:
        """Record exactly one HTTP dispatch without prompt or response data."""

        pipeline_phase = str(operation.get("pipeline_phase") or "")
        pipeline_attempt = int(operation.get("pipeline_attempt") or 0)
        phase = canonical_provider_phase(operation.get("phase"))
        call_index = int(operation.get("call_index") or 0)
        operation_id = str(operation.get("operation_id") or "")
        identity = {
            "operation_id": operation_id,
            "transport_attempt_index": max(
                1,
                int(transport_attempt_index),
            ),
        }
        call_id = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        finish_reason = ""
        if isinstance(result, dict):
            try:
                finish_reason = str(
                    (result.get("choices") or [{}])[0].get("finish_reason") or ""
                ).strip().lower()
            except (AttributeError, IndexError, TypeError):
                finish_reason = ""
        raw_usage = result.get("usage") if isinstance(result, dict) else None
        numeric_usage, usage_errors = validate_complete_token_usage(
            raw_usage if isinstance(raw_usage, Mapping) else None
        )
        usage_reported = not usage_errors
        record: Dict[str, Any] = {
            "schema_version": 2,
            "call_id": call_id,
            "operation_id": operation_id,
            "run_id": str(operation.get("run_id") or ""),
            "head_sha": str(operation.get("head_sha") or ""),
            "phase": phase,
            "pipeline_phase": pipeline_phase,
            "pipeline_attempt": pipeline_attempt,
            "call_index": call_index,
            "transport_attempt_index": max(
                1,
                int(transport_attempt_index),
            ),
            "transport_dispatch_count": 1,
            "model": str(payload.get("_llamapreview_logical_model") or ""),
            "logical_model": str(
                payload.get("_llamapreview_logical_model") or ""
            ),
            "billed_model": str(
                payload.get("_llamapreview_billed_model") or ""
            ),
            "thinking": (payload.get("thinking") or {}).get("type") == "enabled",
            "reasoning_effort": str(payload.get("reasoning_effort") or ""),
            "status": str(status or "unknown"),
            "finish_reason": finish_reason,
            "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
            "last_attempt_elapsed_seconds": round(
                max(
                    0.0,
                    float(
                        elapsed_seconds
                        if last_attempt_elapsed_seconds is None
                        else last_attempt_elapsed_seconds
                    ),
                ),
                3,
            ),
            "transport_attempt_count": 1,
            "usage_state": "reported" if usage_reported else "unreported",
            "usage": numeric_usage,
        }
        if usage_errors:
            record["usage_validation_errors"] = usage_errors
        if http_status is not None:
            record["http_status"] = int(http_status)
        if error is not None:
            record["error_class"] = error.__class__.__name__
        self._provider_call_records.append(deepcopy(record))
        if persist_sink:
            self._persist_provider_call_record(record)
        return record

    def _build_provider_dispatch_fence(
        self,
        *,
        payload: Dict[str, Any],
        operation: Dict[str, Any],
        transport_attempt_index: int,
    ) -> Dict[str, Any]:
        """Build the content-free durable fact required before one HTTP attempt."""

        pipeline_phase = str(operation.get("pipeline_phase") or "")
        pipeline_attempt = int(operation.get("pipeline_attempt") or 0)
        phase = canonical_provider_phase(operation.get("phase"))
        call_index = int(operation.get("call_index") or 0)
        operation_id = str(operation.get("operation_id") or "")
        attempt_index = max(1, int(transport_attempt_index))
        call_id = hashlib.sha256(
            json.dumps(
                {
                    "operation_id": operation_id,
                    "transport_attempt_index": attempt_index,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": 2,
            "call_id": call_id,
            "operation_id": operation_id,
            "run_id": str(operation.get("run_id") or ""),
            "head_sha": str(operation.get("head_sha") or ""),
            "phase": phase,
            "pipeline_phase": pipeline_phase,
            "pipeline_attempt": pipeline_attempt,
            "call_index": call_index,
            "transport_attempt_index": attempt_index,
            # The fence reserves exactly one possible transport dispatch.  If
            # the process dies before HTTP, the ledger conservatively retains
            # that one cost slot as unknown rather than undercounting it; only a
            # terminal replacement can make its usage reported.
            "transport_dispatch_count": 1,
            "transport_attempt_count": 1,
            "model": str(payload.get("_llamapreview_logical_model") or ""),
            "logical_model": str(
                payload.get("_llamapreview_logical_model") or ""
            ),
            "billed_model": str(
                payload.get("_llamapreview_billed_model") or ""
            ),
            "thinking": (payload.get("thinking") or {}).get("type")
            == "enabled",
            "reasoning_effort": str(payload.get("reasoning_effort") or ""),
            "status": "dispatching",
            "finish_reason": "",
            "elapsed_seconds": 0,
            "last_attempt_elapsed_seconds": 0,
            "usage_state": "unreported",
            "usage": {},
        }

    def _persist_provider_dispatch_fence(
        self,
        record: Dict[str, Any],
    ) -> None:
        """Prove durable dispatch authorization before crossing into HTTP."""

        sink = self._provider_dispatch_fence_sink
        if not callable(sink):
            return
        try:
            sink(deepcopy(record))
        except ProviderDispatchOutcomeUnknown:
            raise
        except Exception as exc:
            raise ProviderCallFenceError(
                "Provider-call dispatch fence persistence failed",
                provider_call_record=record,
            ) from exc

    def _persist_provider_call_record(self, record: Dict[str, Any]) -> None:
        """Send an already-built local record to the durable sink exactly once."""

        sink = self._provider_call_sink
        if callable(sink):
            try:
                sink(deepcopy(record))
            except Exception as exc:
                raise ProviderCallLedgerError(
                    "Provider-call ledger persistence failed",
                    provider_call_record=record,
                ) from exc

    def build_payload(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        response_format: Optional[Dict[str, str]] = None,
        reasoning_effort: Optional[str] = None,
        thinking: bool = True,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        api_variant: str = "stable",
    ) -> Dict[str, Any]:
        if api_variant not in {"stable", "beta"}:
            raise ValueError("DeepSeek api_variant must be stable or beta")
        payload: Dict[str, Any] = {
            "model": self.model if model is None else model,
            "messages": messages,
            # Internal-only routing marker. _post removes it before transport;
            # trace summaries retain only this stable enum.
            "_llamapreview_api_variant": api_variant,
        }
        if thinking:
            payload["reasoning_effort"] = reasoning_effort or self.reasoning_effort
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if response_format:
            payload["response_format"] = response_format
        if max_tokens:
            payload["max_tokens"] = max_tokens
        return payload

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        response_format: Optional[Dict[str, str]] = None,
        reasoning_effort: Optional[str] = None,
        thinking: bool = True,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        api_variant: str = "stable",
        max_retries: int = 3,
        timeout_seconds: Optional[float] = None,
        deadline: Optional[Deadline] = None,
        trace_phase: Optional[str] = None,
        trace_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = self.build_payload(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
            thinking=thinking,
            max_tokens=max_tokens,
            model=model,
            api_variant=api_variant,
        )
        return self._post(
            payload,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
            trace_phase=trace_phase,
            trace_metadata=trace_metadata,
        )

    def _post(
        self,
        payload: Dict[str, Any],
        *,
        max_retries: int,
        timeout_seconds: Optional[float] = None,
        deadline: Optional[Deadline] = None,
        trace_phase: Optional[str] = None,
        trace_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        global requests
        if requests is None:
            import requests as imported_requests  # type: ignore

            requests = imported_requests

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        api_variant = str(payload.get("_llamapreview_api_variant") or "stable")
        api_base = self.API_BETA_BASE if api_variant == "beta" else self.API_BASE
        model_selection = resolve_provider_model(
            payload.get("model"),
            self.transport_model_override,
        )
        transport_payload = {
            key: value
            for key, value in payload.items()
            if not key.startswith("_llamapreview_")
        }
        transport_payload["model"] = model_selection.billed_model
        evidence_payload = {
            **transport_payload,
            "_llamapreview_api_variant": api_variant,
            "_llamapreview_logical_model": model_selection.logical_model,
            "_llamapreview_billed_model": model_selection.billed_model,
        }
        url = f"{api_base}{self.CHAT_PATH}"
        last_error: Optional[Exception] = None
        last_status: Optional[int] = None
        configured_timeout = timeout_seconds if timeout_seconds and timeout_seconds > 0 else self.timeout
        operation_started_at = time.monotonic()
        operation_deadline_at = operation_started_at + float(configured_timeout)
        operation = self._begin_provider_operation(
            trace_phase=trace_phase,
            trace_metadata=trace_metadata,
        )
        transport_attempt_count = 0
        for attempt in range(max_retries):
            dispatch_index = 0
            dispatch_recorded = False
            dispatch_http_status: Optional[int] = None
            try:
                operation_remaining = operation_deadline_at - time.monotonic()
                if operation_remaining <= 0.1:
                    raise DeadlineExceeded(
                        f"deepseek.{trace_phase or 'unknown'}.operation",
                        remaining_seconds=operation_remaining,
                    )
                request_timeout = (
                    deadline.timeout_for(
                        operation_remaining,
                        stage=f"deepseek.{trace_phase or 'unknown'}.attempt_{attempt + 1}",
                    )
                    if deadline is not None
                    else operation_remaining
                )
                transport_attempt_count += 1
                dispatch_index = transport_attempt_count
                dispatch_fence = self._build_provider_dispatch_fence(
                    payload=evidence_payload,
                    operation=operation,
                    transport_attempt_index=dispatch_index,
                )
                # This is the irreversible-effect boundary.  A production
                # client bound to DynamoDB may cross into requests.post only
                # after the exact attempt identity is durable.  Any ambiguous
                # prior fence is terminal and therefore cannot buy a second
                # dispatch on stream redelivery.
                self._persist_provider_dispatch_fence(dispatch_fence)
                started = time.monotonic()
                response = requests.post(
                    url,
                    headers=headers,
                    json=transport_payload,
                    timeout=request_timeout,
                )
                elapsed = time.monotonic() - started
                dispatch_http_status = int(response.status_code)
                if response.status_code == 200:
                    last_status = 200
                    try:
                        result = response.json()
                    except Exception as exc:
                        raise DeepSeekResponseError("DeepSeek returned a non-JSON success response") from exc
                    if not isinstance(result, dict):
                        raise DeepSeekResponseError("DeepSeek success response must be a JSON object")
                    self._log_success(result)
                    operation_elapsed = time.monotonic() - operation_started_at
                    call_record = self._record_provider_call(
                        payload=evidence_payload,
                        result=result,
                        operation=operation,
                        transport_attempt_index=dispatch_index,
                        status="completed",
                        elapsed_seconds=operation_elapsed,
                        last_attempt_elapsed_seconds=elapsed,
                        http_status=200,
                        persist_sink=False,
                    )
                    self._trace_success(
                        evidence_payload,
                        result,
                        elapsed_seconds=operation_elapsed,
                        last_attempt_elapsed_seconds=elapsed,
                        trace_phase=trace_phase,
                        trace_metadata=trace_metadata,
                        attempt_count=attempt + 1,
                        provider_call_record=call_record,
                    )
                    self._persist_provider_call_record(call_record)
                    dispatch_recorded = True
                    result[PROVIDER_CALL_RECORD_KEY] = call_record
                    return result
                if response.status_code == 429 or response.status_code >= 500:
                    last_status = int(response.status_code)
                    last_error = DeepSeekHTTPError(
                        f"DeepSeek retryable HTTP status {response.status_code}",
                        status_code=last_status,
                    )
                    final_dispatch = attempt == max_retries - 1
                    self._record_provider_call(
                        payload=evidence_payload,
                        result=None,
                        operation=operation,
                        transport_attempt_index=dispatch_index,
                        status=(
                            "http_error"
                            if final_dispatch
                            else "http_retry"
                        ),
                        elapsed_seconds=(
                            time.monotonic() - operation_started_at
                        ),
                        last_attempt_elapsed_seconds=elapsed,
                        http_status=dispatch_http_status,
                        error=last_error,
                    )
                    dispatch_recorded = True
                    retry_after = response.headers.get("Retry-After")
                    wait = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                    logger.warning("DeepSeek retryable status %s; waiting %ss", response.status_code, wait)
                    if attempt < max_retries - 1:
                        self._bounded_backoff(
                            wait,
                            deadline=deadline,
                            operation_deadline_at=operation_deadline_at,
                            stage=trace_phase,
                        )
                    continue
                raise DeepSeekHTTPError(
                    f"DeepSeek API client error status {response.status_code}",
                    status_code=int(response.status_code),
                )
            except (
                ProviderCallFenceError,
                ProviderCallLedgerError,
                ProviderDispatchOutcomeUnknown,
            ):
                raise
            except DeepSeekResponseError as exc:
                if dispatch_index and not dispatch_recorded:
                    self._record_provider_call(
                        payload=evidence_payload,
                        result=None,
                        operation=operation,
                        transport_attempt_index=dispatch_index,
                        status="http_error",
                        elapsed_seconds=(
                            time.monotonic() - operation_started_at
                        ),
                        http_status=200,
                        error=exc,
                    )
                _emit_failure_summary(
                    trace_phase=trace_phase,
                    trace_metadata=trace_metadata,
                    attempt_count=attempt + 1,
                    status_code=last_status,
                    error=exc,
                    elapsed_seconds=time.monotonic() - operation_started_at,
                )
                raise
            except DeadlineExceeded as exc:
                if dispatch_index and not dispatch_recorded:
                    self._record_provider_call(
                        payload=evidence_payload,
                        result=None,
                        operation=operation,
                        transport_attempt_index=dispatch_index,
                        status="transport_error",
                        elapsed_seconds=time.monotonic() - operation_started_at,
                        http_status=dispatch_http_status,
                        error=exc,
                    )
                _emit_failure_summary(
                    trace_phase=trace_phase,
                    trace_metadata=trace_metadata,
                    attempt_count=attempt + 1,
                    status_code=last_status,
                    error=exc,
                    elapsed_seconds=time.monotonic() - operation_started_at,
                )
                raise
            except DeepSeekHTTPError as exc:
                if dispatch_index and not dispatch_recorded:
                    self._record_provider_call(
                        payload=evidence_payload,
                        result=None,
                        operation=operation,
                        transport_attempt_index=dispatch_index,
                        status="http_error",
                        elapsed_seconds=time.monotonic() - operation_started_at,
                        http_status=exc.status_code,
                        error=exc,
                    )
                _emit_failure_summary(
                    trace_phase=trace_phase,
                    trace_metadata=trace_metadata,
                    attempt_count=attempt + 1,
                    status_code=exc.status_code,
                    error=exc,
                    elapsed_seconds=time.monotonic() - operation_started_at,
                )
                raise
            except (requests.Timeout, requests.RequestException) as exc:
                last_error = exc
                if dispatch_index and not dispatch_recorded:
                    self._record_provider_call(
                        payload=evidence_payload,
                        result=None,
                        operation=operation,
                        transport_attempt_index=dispatch_index,
                        status="transport_error",
                        elapsed_seconds=(
                            time.monotonic() - operation_started_at
                        ),
                        http_status=dispatch_http_status,
                        error=exc,
                    )
                    dispatch_recorded = True
                if attempt == max_retries - 1:
                    break
                self._bounded_backoff(
                    2**attempt,
                    deadline=deadline,
                    operation_deadline_at=operation_deadline_at,
                    stage=trace_phase,
                )
            except Exception as exc:
                # A caller-owned wall timer (for example Review's SIGALRM)
                # can interrupt requests after transport dispatch but before
                # requests raises its own typed timeout. Preserve that paid
                # operation in the all-call ledger without importing the
                # caller's exception type or converting its control flow.
                if dispatch_index and not dispatch_recorded:
                    self._record_provider_call(
                        payload=evidence_payload,
                        result=None,
                        operation=operation,
                        transport_attempt_index=dispatch_index,
                        status="transport_error",
                        elapsed_seconds=time.monotonic()
                        - operation_started_at,
                        http_status=dispatch_http_status,
                        error=exc,
                    )
                raise
        if isinstance(last_error, requests.Timeout):
            final_error: DeepSeekError = DeepSeekTimeoutError(
                f"DeepSeek request timed out after {max_retries} attempt(s)"
            )
        elif isinstance(last_error, requests.RequestException):
            final_error = DeepSeekTransportError(
                f"DeepSeek transport failed after {max_retries} attempt(s): "
                f"{last_error.__class__.__name__}"
            )
        else:
            final_error = DeepSeekHTTPError(
                "DeepSeek request failed after retry exhaustion",
                status_code=last_status,
            )
        _emit_failure_summary(
            trace_phase=trace_phase,
            trace_metadata=trace_metadata,
            attempt_count=max(1, transport_attempt_count),
            status_code=last_status,
            error=final_error,
            elapsed_seconds=time.monotonic() - operation_started_at,
        )
        raise final_error

    @staticmethod
    def _bounded_backoff(
        wait_seconds: float,
        *,
        deadline: Optional[Deadline],
        operation_deadline_at: float,
        stage: Optional[str],
    ) -> None:
        wait = max(0.0, float(wait_seconds))
        operation_remaining = float(operation_deadline_at) - time.monotonic()
        if operation_remaining <= 0.1:
            raise DeadlineExceeded(
                f"deepseek.{stage or 'unknown'}.backoff",
                remaining_seconds=operation_remaining,
            )
        wait = min(wait, max(0.0, operation_remaining - 0.1))
        if deadline is not None:
            remaining = deadline.check(f"deepseek.{stage or 'unknown'}.backoff", minimum_seconds=0.1)
            wait = min(wait, max(0.0, remaining - 0.1))
        if wait > 0:
            time.sleep(wait)

    def _log_success(self, result: Dict[str, Any]) -> None:
        usage = result.get("usage", {})
        logger.info("DeepSeek API success: total_tokens=%s", usage.get("total_tokens", 0))

    def _trace_success(
        self,
        payload: Dict[str, Any],
        result: Dict[str, Any],
        *,
        elapsed_seconds: float,
        last_attempt_elapsed_seconds: Optional[float] = None,
        trace_phase: Optional[str],
        trace_metadata: Optional[Dict[str, Any]],
        attempt_count: int,
        provider_call_record: Mapping[str, Any],
    ) -> None:
        mode = _trace_mode()
        if mode == "off":
            return
        event = _build_trace_event(
            mode=mode,
            payload=payload,
            result=result,
            elapsed_seconds=elapsed_seconds,
            trace_phase=trace_phase,
            trace_metadata=trace_metadata,
            attempt_count=attempt_count,
            last_attempt_elapsed_seconds=last_attempt_elapsed_seconds,
            provider_call_record=provider_call_record,
        )
        _emit_cloudwatch_trace(event)
        try:
            _write_local_trace(event)
        except Exception:
            # Local tracing is optional diagnostic evidence.  Never replay a
            # paid provider call because a workstation/private trace path is
            # unavailable; CloudWatch already has the content-free summary and
            # the durable provider ledger still executes next.
            logger.exception(
                "DeepSeek local trace persistence failed: phase=%s trace_id=%s",
                event.get("phase"),
                event.get("trace_id"),
            )
        if mode == "full":
            try:
                _write_s3_trace(event)
            except Exception:
                # The model result remains usable and CloudWatch already has a
                # summary. Avoid replaying an expensive model call solely
                # because optional full-trace persistence failed.
                logger.exception(
                    "DeepSeek full trace persistence failed: phase=%s trace_id=%s",
                    event.get("phase"),
                    event.get("trace_id"),
                )

"""Model-call boundary for the production review judgment path.

Deep owns engineering judgment.  This module owns only the mechanics required
to obtain that judgment safely: context packing, bounded calls, response
envelope validation, and content-free phase telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import signal
import threading
import time
from typing import Any, Dict, Mapping, Optional, Sequence

from .. import config
from ..context_engine.packing import ContextSection, pack_sections
from ..deadline import Deadline, DeadlineExceeded
from ..deepseek_client import (
    DeepSeekClient,
    DeepSeekHTTPError,
    DeepSeekResponseError,
    DeepSeekTimeoutError,
    DeepSeekTransportError,
    ProviderCallFenceError,
    ProviderCallLedgerError,
    ProviderDispatchOutcomeUnknown,
)
from ..provider_usage import merge_numeric_usage


class ReviewGenerationTimeout(Exception):
    """A review model phase exceeded its code-owned wall-clock budget."""


class ReviewModelResponseError(Exception):
    """The provider envelope cannot yield one complete visible message."""

    def __init__(self, message: str):
        super().__init__(message)
        # Keep typed, content-minimized failure context for accounting and
        # diagnostics. Callers must never persist the raw provider body.
        self.response: Dict[str, Any] = {}
        self.visible_message: Optional[Dict[str, str]] = None
        self.visible_content = ""
        self.telemetry: Optional[Dict[str, Any]] = None


class ReviewOutputTruncated(ReviewModelResponseError):
    """The provider explicitly stopped because the output limit was reached."""


class PresentationRepresentationError(ReviewModelResponseError):
    """Final returned content that cannot satisfy the fixed presentation IR."""


@dataclass(frozen=True)
class ModelPhaseResult:
    """One completed visible model response plus content-free telemetry."""

    response: Dict[str, Any]
    message: Dict[str, str]
    content: str
    telemetry: Dict[str, Any]


_REQUIRED_CONTEXT_SECTIONS = {
    "# PR Review Context": (0, 1000),
    "## Changed Files (PR head)": (1, 4000),
    "## Repository Inventory": (0, 2500),
    "## Verification Ledger": (0, 6000),
    "## Tool Trace": (1, 8000),
    "## Collection Summary": (0, 5000),
    "## PFR Review Context": (0, 12000),
}


def _context_sections(context: str) -> list[ContextSection]:
    """Split assembled context without letting snippets evict truth controls."""

    lines = (context or "").splitlines()
    if not lines:
        return []
    groups: list[tuple[str, list[str]]] = []
    heading = "context"
    current: list[str] = []
    for line in lines:
        if line.startswith("## ") or (line.startswith("# ") and not current):
            if current:
                groups.append((heading, current))
            heading = line.strip()
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append((heading, current))

    sections: list[ContextSection] = []
    for index, (name, body) in enumerate(groups):
        text = "\n".join(body).strip()
        priority, minimum = _REQUIRED_CONTEXT_SECTIONS.get(name, (50, 0))
        sections.append(
            ContextSection(
                name=f"review_context_{index}",
                text=text,
                priority=priority,
                required=name in _REQUIRED_CONTEXT_SECTIONS,
                min_chars=min(len(text), minimum),
            )
        )
    return sections


def cap_context_for_review(
    pr_details: str,
    context: str,
    *,
    max_input_chars: int = config.REVIEW_INPUT_MAX_CHARS,
) -> str:
    """Pack context under the Deep input budget while preserving truth controls."""

    budget = max(0, int(max_input_chars) - len(pr_details or ""))
    if len(context or "") <= budget:
        return context or ""
    sections = _context_sections(context or "")
    return pack_sections(sections, budget) if sections else ""


def message_content(response: Mapping[str, Any]) -> str:
    """Return the provider's visible assistant content."""

    try:
        content = response["choices"][0]["message"].get("content")  # type: ignore[index]
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ReviewModelResponseError("missing choices[0].message") from exc
    if not isinstance(content, str) or not content.strip():
        raise ReviewModelResponseError("model response content is empty")
    return content


def assistant_message(response: Mapping[str, Any]) -> Dict[str, str]:
    """Project one safe visible assistant message for a continuation."""

    try:
        raw = response["choices"][0]["message"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as exc:
        raise ReviewModelResponseError("missing choices[0].message") from exc
    if not isinstance(raw, Mapping):
        raise ReviewModelResponseError("choices[0].message must be an object")
    if raw.get("role") not in (None, "assistant"):
        raise ReviewModelResponseError(
            "choices[0].message role must be assistant"
        )
    if raw.get("tool_calls"):
        raise ReviewModelResponseError(
            "choices[0].message has tool calls in a no-tools review stage"
        )
    return {"role": "assistant", "content": message_content(response)}


def finish_reason(response: Mapping[str, Any]) -> str:
    try:
        value = response["choices"][0].get("finish_reason")  # type: ignore[index]
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    return str(value or "").strip().lower()


def require_complete_response(response: Mapping[str, Any]) -> str:
    """Fail unless the provider returned one complete no-tools message."""

    reason = finish_reason(response)
    if not reason:
        raise ReviewModelResponseError("model response is missing finish_reason")
    if reason == "length":
        raise ReviewOutputTruncated("model finish_reason=length")
    if reason != "stop":
        raise ReviewModelResponseError(
            f"unexpected model finish_reason={reason}"
        )
    assistant_message(response)
    return reason


def phase_timeout_seconds(
    phase_timeout: int,
    stage_started: float,
    *,
    phase: str,
    deadline: Optional[Deadline],
) -> float:
    """Resolve the tightest phase, review-stage, and invocation deadline."""

    candidates: list[float] = []
    if phase_timeout and phase_timeout > 0:
        candidates.append(float(phase_timeout))
    stage_timeout = int(config.REVIEW_STAGE_TIMEOUT_SECONDS or 0)
    if stage_timeout > 0:
        remaining = float(stage_timeout) - (time.monotonic() - stage_started)
        if remaining <= 0:
            raise ReviewGenerationTimeout(
                "review stage exceeded its wall-clock budget"
            )
        candidates.append(remaining)
    if deadline is not None:
        candidates.append(
            deadline.check(
                f"review.{phase}.start",
                minimum_seconds=0.1,
            )
        )
    return min(candidates) if candidates else 0.0


def _chat_with_wall_timeout(
    client: DeepSeekClient,
    phase: str,
    timeout_seconds: float,
    messages: Sequence[Mapping[str, Any]],
    **kwargs: Any,
) -> Dict[str, Any]:
    if timeout_seconds > 0:
        kwargs.setdefault("timeout_seconds", timeout_seconds)
    if (
        timeout_seconds <= 0
        or threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "setitimer")
    ):
        return client.chat(list(messages), **kwargs)

    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)

    def raise_timeout(_signum: int, _frame: Any) -> None:
        raise ReviewGenerationTimeout(
            f"{phase} exceeded {timeout_seconds}s"
        )

    try:
        signal.signal(signal.SIGALRM, raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        return client.chat(list(messages), **kwargs)
    finally:
        signal.setitimer(signal.ITIMER_REAL, old_timer[0], old_timer[1])
        signal.signal(signal.SIGALRM, old_handler)


def run_model_phase(
    client: DeepSeekClient,
    *,
    phase: str,
    messages: Sequence[Mapping[str, Any]],
    model: str,
    reasoning_effort: str,
    thinking: bool,
    max_tokens: Optional[int],
    timeout_seconds: float,
    deadline: Optional[Deadline],
    trace_metadata: Optional[Mapping[str, Any]],
    response_format: Optional[Dict[str, str]] = None,
    attempt: int = 1,
) -> ModelPhaseResult:
    """Run and validate one model phase without interpreting its substance."""

    started = time.monotonic()
    response: Dict[str, Any] = {}
    reason = ""
    try:
        call_options: Dict[str, Any] = {
            "model": model,
            "reasoning_effort": reasoning_effort,
            "thinking": thinking,
            "max_tokens": max_tokens,
            "deadline": deadline,
            "trace_phase": phase,
            "trace_metadata": {
                **dict(trace_metadata or {}),
                "call_index": phase,
            },
        }
        if response_format is not None:
            call_options["response_format"] = response_format
        response = _chat_with_wall_timeout(
            client,
            phase,
            timeout_seconds,
            messages,
            **call_options,
        )
        reason = finish_reason(response)
        require_complete_response(response)
        message = assistant_message(response)
        content = message["content"]
    except (ReviewOutputTruncated, ReviewModelResponseError) as error:
        elapsed = time.monotonic() - started
        telemetry = model_phase_telemetry(
            phase,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            attempt=attempt,
            elapsed_seconds=elapsed,
            finish_reason=reason or finish_reason(response),
            usage=response.get("usage") or {},
        )
        error.response = dict(response)
        try:
            partial_content = message_content(response)
        except ReviewModelResponseError:
            partial_content = ""
        if partial_content:
            error.visible_content = partial_content
            error.visible_message = {
                "role": "assistant",
                "content": partial_content,
            }
        error.telemetry = telemetry
        raise

    telemetry = model_phase_telemetry(
        phase,
        model=model,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        attempt=attempt,
        elapsed_seconds=time.monotonic() - started,
        finish_reason=reason,
        usage=response.get("usage") or {},
    )
    return ModelPhaseResult(
        response=response,
        message=message,
        content=content,
        telemetry=telemetry,
    )


def model_phase_telemetry(
    phase: str,
    *,
    model: str,
    thinking: bool,
    reasoning_effort: str,
    attempt: int,
    elapsed_seconds: float,
    finish_reason: str,
    usage: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return content-free, numeric-only phase telemetry."""

    return {
        "phase": phase,
        "model": str(model or ""),
        "thinking": bool(thinking),
        "reasoning_effort": str(reasoning_effort or ""),
        "attempt": max(1, int(attempt)),
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
        "finish_reason": str(finish_reason or ""),
        "usage": merge_numeric_usage(usage),
    }


def failure_kind(error: BaseException) -> str:
    """Map a model/service failure to a stable content-free artifact kind."""

    if isinstance(error, (ReviewGenerationTimeout, DeadlineExceeded)):
        return "wall_timeout"
    if isinstance(error, ReviewOutputTruncated):
        return "output_truncated"
    if isinstance(error, json.JSONDecodeError):
        return "json_parse_error"
    if isinstance(error, PresentationRepresentationError):
        return "presentation_validation_error"
    if isinstance(error, DeepSeekHTTPError):
        return "model_http_error"
    if isinstance(error, DeepSeekTimeoutError):
        return "model_transport_timeout"
    if isinstance(error, DeepSeekTransportError):
        return "model_transport_error"
    if isinstance(error, ProviderCallLedgerError):
        return "provider_call_ledger_error"
    if isinstance(error, ProviderCallFenceError):
        return "provider_dispatch_fence_unavailable"
    if isinstance(error, ProviderDispatchOutcomeUnknown):
        return "provider_dispatch_outcome_unknown"
    if isinstance(
        error,
        (DeepSeekResponseError, ReviewModelResponseError),
    ):
        return "model_response_error"
    return "review_internal_error"


def failure_retryable(error: BaseException) -> bool:
    """Retry only provider/service completion failures, never review substance."""

    if isinstance(error, PresentationRepresentationError):
        return False
    if isinstance(
        error,
        (
            DeadlineExceeded,
            ReviewGenerationTimeout,
            ReviewOutputTruncated,
            DeepSeekTimeoutError,
            DeepSeekTransportError,
            DeepSeekResponseError,
            ReviewModelResponseError,
        ),
    ):
        return True
    if isinstance(error, DeepSeekHTTPError):
        return error.status_code in {408, 425, 429, 500, 502, 503, 504}
    if isinstance(error, ProviderCallFenceError):
        return True
    if isinstance(
        error,
        (ProviderCallLedgerError, ProviderDispatchOutcomeUnknown),
    ):
        return False
    return False


def artifact_failure_message(error: BaseException) -> str:
    """Return a stable failure summary without echoing untrusted model text."""

    messages = {
        "wall_timeout": (
            "The review stage exceeded its bounded wall-clock budget."
        ),
        "output_truncated": (
            "The model response ended before the requested output completed."
        ),
        "json_parse_error": (
            "The Final response was not a complete JSON object."
        ),
        "presentation_validation_error": (
            "The Final response could not yield a safe presentation."
        ),
        "model_http_error": "The model provider returned an HTTP error.",
        "model_transport_timeout": (
            "The model provider transport timed out."
        ),
        "model_transport_error": (
            "The model provider transport failed."
        ),
        "model_response_error": (
            "The model response envelope was incomplete or invalid."
        ),
        "provider_call_ledger_error": (
            "The paid model dispatch could not be recorded durably."
        ),
        "provider_dispatch_fence_unavailable": (
            "The model dispatch was withheld because its durable fence could not be proven."
        ),
        "provider_dispatch_outcome_unknown": (
            "A prior model dispatch may have started but has no durable terminal outcome."
        ),
        "review_internal_error": (
            "The review pipeline could not complete its internal projection."
        ),
    }
    return messages[failure_kind(error)]


REVIEW_CALL_EXCEPTIONS = (
    DeadlineExceeded,
    ReviewGenerationTimeout,
    ReviewOutputTruncated,
    ReviewModelResponseError,
    DeepSeekHTTPError,
    DeepSeekResponseError,
    DeepSeekTimeoutError,
    DeepSeekTransportError,
)

"""Typed pipeline failures used by retry and terminal-state policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from .deadline import DeadlineExceeded


@dataclass(frozen=True)
class FailureClassification:
    kind: str
    retryable: bool
    stage: str
    message: str


class PipelineFailure(RuntimeError):
    """Base class for failures with an explicit operational contract."""

    kind = "pipeline_error"
    retryable = False

    def __init__(self, message: str, *, stage: str):
        self.stage = stage
        super().__init__(message)


class RetryablePipelineFailure(PipelineFailure):
    retryable = True


class TerminalPipelineFailure(PipelineFailure):
    retryable = False


class ReviewGenerationIncomplete(PipelineFailure):
    """A model review did not reach a publishable, validated artifact."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        kind: str,
        retryable: bool,
    ):
        self.kind = str(kind or "review_generation_incomplete")
        self.retryable = bool(retryable)
        super().__init__(message, stage=stage)


class ProviderSourceIdentityMismatch(TerminalPipelineFailure):
    """The actual model source differs from an explicitly pinned snapshot."""

    kind = "provider_source_identity_mismatch"


class HeadSuperseded(PipelineFailure):
    """The queued head SHA is no longer the PR's current head."""

    kind = "head_superseded"
    retryable = False

    def __init__(self, expected_head_sha: str, actual_head_sha: str, *, stage: str):
        self.expected_head_sha = expected_head_sha
        self.actual_head_sha = actual_head_sha
        super().__init__(
            f"PR head changed from {expected_head_sha} to {actual_head_sha}",
            stage=stage,
        )


class PRLifecycleSuperseded(PipelineFailure):
    """The queued revision still exists, but the pull request is no longer open."""

    kind = "pr_lifecycle_superseded"
    retryable = False

    def __init__(
        self,
        expected_head_sha: str,
        actual_head_sha: str,
        *,
        current_state: str,
        merged: bool,
        stage: str,
    ):
        self.expected_head_sha = expected_head_sha
        self.actual_head_sha = actual_head_sha
        self.current_state = current_state
        self.merged = bool(merged)
        self.superseded_kind = "pr_merged" if self.merged else "pr_closed"
        lifecycle = "merged" if self.merged else current_state or "not_open"
        super().__init__(
            f"PR lifecycle changed to {lifecycle} at head {actual_head_sha}",
            stage=stage,
        )


class HeadVerificationUnavailable(RetryablePipelineFailure):
    """The current PR head could not be read, so no result may be committed."""

    kind = "head_verification_unavailable"


class PublicationIdentityUnavailable(RetryablePipelineFailure):
    """GitHub may have accepted a write but did not identify the review."""

    kind = "publication_identity_unavailable"


class PhaseClaimUnavailable(RetryablePipelineFailure):
    """Another invocation owns the active stream-record phase lease."""

    kind = "phase_claim_unavailable"


class PublicationPreflightUnavailable(RetryablePipelineFailure):
    """The complete pre-write GitHub duplicate proof could not be read."""

    kind = "publication_preflight_unavailable"


class PublicationOutcomeUnknown(TerminalPipelineFailure):
    """A dispatch may have occurred but no exact GitHub effect is observable."""

    kind = "publication_outcome_unknown"


class PublicationIntegrityFailure(TerminalPipelineFailure):
    """Observed GitHub publication state conflicts with the durable intent."""

    kind = "publication_integrity_failure"


class PublicationStateConflict(TerminalPipelineFailure):
    """The publication intent or terminal receipt lost its owner-bound CAS."""

    kind = "publication_state_conflict"


class CIRefreshUnavailable(RetryablePipelineFailure):
    """Current-head CI truth could not be refreshed completely enough to use."""

    kind = "ci_refresh_unavailable"


class GitHubAuthConfigurationError(TerminalPipelineFailure):
    """The GitHub App credentials cannot produce a signed application JWT."""

    kind = "github_auth_configuration_error"


_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_TERMINAL_HTTP_STATUSES = {400, 401, 403, 404, 405, 409, 410, 422}
_RETRYABLE_SERVICE_CODES = {
    "InternalServerError",
    "LimitExceededException",
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
    "ThrottledException",
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "TransactionInProgressException",
}


_HTTP_STATUS_FIELDS = ("status", "status_code", "code")
_HTTP_STATUS_MAPPING_FIELDS = ("status", "status_code", "statusCode", "HTTPStatusCode", "code")
_HTTP_STATUS_CHILD_FIELDS = (
    "response",
    "__cause__",
    "__context__",
    "reason",
    "status",
    "status_code",
    "code",
)
_MAX_HTTP_STATUS_EXTRACTION_DEPTH = 8


def _status_from_value(value: Any) -> Optional[int]:
    try:
        if value is not None:
            return int(value)
    except (OverflowError, TypeError, ValueError):
        pass
    return None


def _safe_attribute(value: Any, name: str) -> Any:
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _status_from_typed_object(value: Any) -> Optional[int]:
    """Read only explicit HTTP status fields; never inspect exception text."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return _status_from_value(value.strip())
    if isinstance(value, Mapping):
        for field in _HTTP_STATUS_MAPPING_FIELDS:
            status = _status_from_value(value.get(field))
            if status is not None:
                return status
        metadata = value.get("ResponseMetadata")
        if isinstance(metadata, Mapping):
            return _status_from_value(metadata.get("HTTPStatusCode"))

    for field in _HTTP_STATUS_FIELDS:
        status = _status_from_value(_safe_attribute(value, field))
        if status is not None:
            return status
    return None


def _http_status(exc: BaseException) -> Optional[int]:
    """Find a typed HTTP status through bounded exception/response wrappers."""
    pending: list[tuple[Any, int]] = [(exc, 0)]
    seen: set[int] = set()

    for candidate, depth in pending:
        candidate_id = id(candidate)
        if candidate_id in seen:
            continue
        seen.add(candidate_id)

        status = _status_from_typed_object(candidate)
        if status is not None:
            return status
        if depth >= _MAX_HTTP_STATUS_EXTRACTION_DEPTH:
            continue

        for field in _HTTP_STATUS_CHILD_FIELDS:
            child = _safe_attribute(candidate, field)
            if child is not None:
                pending.append((child, depth + 1))
        if isinstance(candidate, Mapping):
            for field in ("ResponseMetadata", "response", "reason", "status", "status_code", "code"):
                child = candidate.get(field)
                if child is not None:
                    pending.append((child, depth + 1))
    return None


def _service_error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Code") or "")
    return str(getattr(exc, "code", "") or "")


def _rate_limit_headers(exc: BaseException) -> dict[str, Any]:
    for candidate in (
        getattr(exc, "headers", None),
        getattr(exc, "response_headers", None),
        getattr(getattr(exc, "response", None), "headers", None),
    ):
        if isinstance(candidate, dict):
            return {str(key).lower(): value for key, value in candidate.items()}
    return {}


def classify_failure(exc: BaseException, *, stage: str) -> FailureClassification:
    """Classify by typed signals and status codes, never error-message keywords."""
    explicit_stage = str(getattr(exc, "stage", "") or stage)
    # These explicit typed signals are owned by the provider transport boundary
    # without making the general failure module import the HTTP client.
    if getattr(exc, "provider_dispatch_outcome_unknown", False) is True:
        return FailureClassification(
            "provider_dispatch_outcome_unknown",
            False,
            explicit_stage,
            str(exc),
        )
    if getattr(exc, "provider_dispatch_not_started", False) is True:
        return FailureClassification(
            "provider_dispatch_fence_unavailable",
            True,
            explicit_stage,
            str(exc),
        )
    # This explicit typed signal is owned by ProviderCallLedgerError without
    # making the general failure module import the HTTP client.  A paid
    # dispatch whose durable ledger write failed must never trigger an
    # automatic second call in Route, PFR, Deep, or Final.
    if getattr(exc, "paid_dispatch_unrecorded", False) is True:
        return FailureClassification(
            "provider_call_ledger_error",
            False,
            explicit_stage,
            str(exc),
        )
    if isinstance(exc, HeadSuperseded):
        return FailureClassification(exc.kind, False, explicit_stage, str(exc))
    if isinstance(exc, DeadlineExceeded):
        return FailureClassification("wall_timeout", True, explicit_stage, str(exc))
    if isinstance(exc, PipelineFailure):
        return FailureClassification(
            str(getattr(exc, "kind", "pipeline_error")),
            bool(getattr(exc, "retryable", False)),
            explicit_stage,
            str(exc),
        )

    service_code = _service_error_code(exc)
    if service_code in _RETRYABLE_SERVICE_CODES:
        return FailureClassification("service_throttled", True, explicit_stage, str(exc))
    status = _http_status(exc)
    headers = _rate_limit_headers(exc)
    if status == 403 and (
        str(headers.get("x-ratelimit-remaining") or "") == "0"
        or bool(headers.get("retry-after"))
    ):
        return FailureClassification("rate_limited", True, explicit_stage, str(exc))
    if status in _RETRYABLE_HTTP_STATUSES:
        return FailureClassification("http_transient", True, explicit_stage, str(exc))
    if status in _TERMINAL_HTTP_STATUSES:
        return FailureClassification("http_terminal", False, explicit_stage, str(exc))
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return FailureClassification("transport_error", True, explicit_stage, str(exc))
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return FailureClassification("invalid_pipeline_input", False, explicit_stage, str(exc))
    # Unknown runtime failures get bounded retries. This avoids permanently
    # dropping a transient SDK/AWS failure while the per-phase attempt cap
    # prevents unbounded cost.
    return FailureClassification("unclassified_runtime_error", True, explicit_stage, str(exc))

"""DynamoDB persistence, idempotency, and compressed context codecs."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from . import config

logger = logging.getLogger(__name__)


# Cache ABI owned by the repo-fact-sheet builder code.  Bump this value whenever
# build_repo_fact_sheet changes the meaning or shape of the persisted facts.
REPO_FACT_SHEET_SCHEMA_VERSION = "repo-fact-sheet-v1"
PROVIDER_CALL_ATTR_PREFIX = "_deepseek_call_"
_PROVIDER_CALL_ID_RE = re.compile(r"^[0-9a-f]{64}$")
PHASE_CLAIM_LEASE_SECONDS = 16 * 60
PUBLICATION_CANDIDATE_SCHEMA_VERSION = 2
SUPPORTED_PUBLICATION_CANDIDATE_SCHEMA_VERSIONS = frozenset({1, 2})
_PROVIDER_PHASE_ORDER = {
    "route": 0,
    "route_adjudication": 1,
    "pfr_plan": 2,
    "pfr_reconcile": 3,
    "pfr_reconcile_representation_repair": 4,
    "deep_thinking": 5,
    "deep_judgment": 6,
    "final_output": 7,
    "final_presentation": 8,
    "final_presentation_repair": 9,
}


class ConditionalWriteLost(Exception):
    """Raised when another worker already advanced the status."""


class DynamoItemTooLarge(ValueError):
    """Raised before a state update can exceed DynamoDB's item limit."""


class ArtifactIntegrityError(ValueError):
    """Raised when a persisted artifact is incomplete, unsafe, or corrupted."""


class ProviderDispatchFenceUnresolved(ArtifactIntegrityError):
    """A fenced provider attempt has no durable terminal transport outcome."""

    def __init__(self, record: Mapping[str, Any]):
        self.record = dict(record)
        super().__init__(
            "A prior provider dispatch fence remains unresolved"
        )


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ttl_epoch(days: int = config.TTL_DAYS) -> int:
    return int(time.time()) + days * 24 * 3600


def _boto3():
    import boto3

    return boto3


def get_table(table_name: Optional[str] = None):
    return _boto3().resource("dynamodb").Table(table_name or config.DYNAMODB_PIPELINE_TABLE)


def get_s3_client():
    return _boto3().client("s3")


def gzip_b64(text: str) -> str:
    payload = (text or "").encode("utf-8")
    return base64.b64encode(gzip.compress(payload, mtime=0)).decode("ascii")


def gunzip_b64(blob: str) -> str:
    if not blob:
        return ""
    return gzip.decompress(base64.b64decode(blob.encode("ascii"))).decode("utf-8")


def gzip_json_b64(value: Any) -> str:
    return gzip_b64(_canonical_json(value).decode("utf-8"))


def gunzip_json_b64(blob: str) -> Any:
    text = gunzip_b64(blob)
    return json.loads(text) if text else None


def _client_error_code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _is_conditional_failure(exc: Exception) -> bool:
    return _client_error_code(exc) == "ConditionalCheckFailedException" or exc.__class__.__name__ == "ConditionalWriteLost"


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def _gzip_json_bytes(value: Any) -> bytes:
    return gzip.compress(_canonical_json(value), mtime=0)


def _dynamodb_safe(value: Any) -> Any:
    """Convert JSON-like values into the numeric types accepted by boto3."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {str(key): _dynamodb_safe(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_dynamodb_safe(child) for child in value]
    if isinstance(value, tuple):
        return [_dynamodb_safe(child) for child in value]
    return value


def _safe_key_component(value: Any, *, fallback: str) -> str:
    rendered = re.sub(r"[^A-Za-z0-9._=-]+", "_", str(value or "").strip())
    return rendered[:180] or fallback


def _artifact_key(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    run_id: str,
    kind: str,
) -> str:
    prefix = str(config.RUN_ARTIFACT_PREFIX or "pipeline").strip("/")
    owner, _, name = str(repo).partition("/")
    return "/".join(
        part
        for part in (
            prefix,
            _safe_key_component(owner, fallback="unknown-owner"),
            _safe_key_component(name, fallback="unknown-repo"),
            f"pr-{int(pr_number)}",
            _safe_key_component(head_sha, fallback="unknown-head"),
            _safe_key_component(run_id, fallback="unknown-run"),
            f"{_safe_key_component(kind, fallback='artifact')}.json.gz",
        )
        if part
    )


def _put_json_artifact(
    value: Dict[str, Any],
    *,
    bucket: str,
    key: str,
    kind: str,
    s3_client=None,
) -> Dict[str, Any]:
    if not bucket:
        raise ArtifactIntegrityError(f"No artifact bucket configured for {kind}")
    payload = _gzip_json_bytes(value)
    digest = hashlib.sha256(payload).hexdigest()
    if key.endswith(".json.gz"):
        key = f"{key[:-8]}.{digest[:16]}.json.gz"
    else:
        key = f"{key}.{digest[:16]}"
    s3_client = s3_client or get_s3_client()
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType="application/json",
        ContentEncoding="gzip",
        ServerSideEncryption="AES256",
        Metadata={
            "schema-version": str(config.RUN_ARTIFACT_SCHEMA_VERSION),
            "artifact-kind": str(kind),
            "sha256": digest,
        },
    )
    return {
        "bucket": bucket,
        "key": key,
        "codec": "json-gzip",
        "schema_version": str(config.RUN_ARTIFACT_SCHEMA_VERSION),
        "sha256": digest,
        "size_bytes": len(payload),
        "kind": kind,
    }


def _read_json_artifact(pointer: Dict[str, Any], *, s3_client=None) -> Dict[str, Any]:
    bucket = str(pointer.get("bucket") or "")
    key = str(pointer.get("key") or "")
    if not bucket or not key:
        raise ArtifactIntegrityError("Artifact pointer is missing bucket or key")
    s3_client = s3_client or get_s3_client()
    response = s3_client.get_object(Bucket=bucket, Key=key)
    payload = response["Body"].read()
    if not isinstance(payload, bytes):
        payload = bytes(payload)
    expected_size = pointer.get("size_bytes")
    if expected_size is not None and len(payload) != int(expected_size):
        raise ArtifactIntegrityError(
            f"Artifact byte count mismatch for {key}: expected {expected_size}, got {len(payload)}"
        )
    expected_sha = str(pointer.get("sha256") or "")
    actual_sha = hashlib.sha256(payload).hexdigest()
    if expected_sha and actual_sha != expected_sha:
        raise ArtifactIntegrityError(f"Artifact checksum mismatch for {key}")
    try:
        decoded = json.loads(gzip.decompress(payload).decode("utf-8"))
    except Exception as exc:
        raise ArtifactIntegrityError(f"Artifact decode failed for {key}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ArtifactIntegrityError(f"Artifact root must be an object: {key}")
    expected_schema = str(pointer.get("schema_version") or "")
    actual_schema = str(decoded.get("schema_version") or "")
    if expected_schema and actual_schema != expected_schema:
        raise ArtifactIntegrityError(
            f"Artifact schema mismatch for {key}: expected {expected_schema}, got {actual_schema}"
        )
    return decoded


def store_publication_candidate(
    candidate: Dict[str, Any],
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    run_id: str,
    publication_generation_phase: str,
    publication_generation_attempt: int,
    s3_client=None,
) -> Dict[str, Any]:
    """Write the immutable private publication candidate before any GitHub write."""

    if not isinstance(candidate, dict):
        raise ArtifactIntegrityError("Publication candidate must be an object")
    _assert_artifact_safe(candidate)
    required = {
        "publication_schema_version": PUBLICATION_CANDIDATE_SCHEMA_VERSION,
        "kind": "publication_candidate",
        "repo": str(repo),
        "pr_number": int(pr_number),
        "head_sha": str(head_sha),
        "run_id": str(run_id),
        "phase": str(publication_generation_phase),
        "publication_generation_phase": str(
            publication_generation_phase
        ),
        "publication_generation_attempt": int(
            publication_generation_attempt
        ),
    }
    for field, expected in required.items():
        if candidate.get(field) != expected:
            raise ArtifactIntegrityError(
                f"Publication candidate identity mismatch for {field}"
            )
    for field in (
        "owner_event_id",
        "publication_key",
        "payload_sha256",
        "main_body_sha256",
        "comments_sha256",
    ):
        if not str(candidate.get(field) or ""):
            raise ArtifactIntegrityError(
                f"Publication candidate identity is missing {field}"
            )
    bucket = str(config.PUBLICATION_ARTIFACT_BUCKET or "")
    if not bucket:
        raise ArtifactIntegrityError(
            "Live publication requires PUBLICATION_ARTIFACT_BUCKET"
        )
    wrapper = {
        "schema_version": str(config.RUN_ARTIFACT_SCHEMA_VERSION),
        **candidate,
    }
    return _put_json_artifact(
        wrapper,
        bucket=bucket,
        key=_artifact_key(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            run_id=run_id,
            kind=(
                "publication-candidate-"
                f"{int(publication_generation_attempt)}"
            ),
        ),
        kind="publication_candidate",
        s3_client=s3_client,
    )


def load_publication_candidate(
    intent: Mapping[str, Any],
    *,
    s3_client=None,
) -> Dict[str, Any]:
    """Read and verify the exact candidate bound by a publication intent."""

    pointer = intent.get("candidate_artifact")
    if not isinstance(pointer, dict):
        raise ArtifactIntegrityError(
            "Publication intent candidate artifact pointer is missing"
        )
    pointer_sha = str(pointer.get("sha256") or "")
    if (
        not pointer_sha
        or pointer_sha
        != str(intent.get("candidate_artifact_sha256") or "")
    ):
        raise ArtifactIntegrityError(
            "Publication candidate pointer digest does not match intent"
        )
    candidate = _read_json_artifact(pointer, s3_client=s3_client)
    try:
        candidate_schema = int(candidate.get("publication_schema_version") or 0)
        intent_schema = int(intent.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ArtifactIntegrityError(
            "Publication candidate schema version is invalid"
        ) from exc
    if (
        candidate_schema not in SUPPORTED_PUBLICATION_CANDIDATE_SCHEMA_VERSIONS
        or candidate_schema != intent_schema
    ):
        raise ArtifactIntegrityError(
            "Publication candidate schema does not match its intent"
        )
    expected = {
        "kind": "publication_candidate",
        "publication_schema_version": candidate_schema,
        "repo": str(intent.get("repo") or ""),
        "pr_number": int(intent.get("pr_number") or 0),
        "head_sha": str(intent.get("head_sha") or ""),
        "run_id": str(intent.get("run_id") or ""),
        "phase": str(intent.get("phase") or ""),
        "owner_event_id": str(intent.get("owner_event_id") or ""),
        "publication_generation_phase": str(
            intent.get("publication_generation_phase") or ""
        ),
        "publication_generation_attempt": int(
            intent.get("publication_generation_attempt") or 0
        ),
        "publication_key": str(intent.get("publication_key") or ""),
        "payload_sha256": str(intent.get("payload_sha256") or ""),
        "main_body_sha256": str(
            intent.get("main_body_sha256") or ""
        ),
        "comments_sha256": str(
            intent.get("comments_sha256") or ""
        ),
    }
    for field, value in expected.items():
        if candidate.get(field) != value:
            raise ArtifactIntegrityError(
                f"Publication candidate does not match intent field {field}"
            )
    if not isinstance(candidate.get("review_artifact"), dict):
        raise ArtifactIntegrityError(
            "Publication candidate review artifact is missing"
        )
    if not isinstance(candidate.get("terminal_attributes"), dict):
        raise ArtifactIntegrityError(
            "Publication candidate terminal attributes are missing"
        )
    return candidate


def _validate_bundle_identity(bundle: Dict[str, Any], item: Dict[str, Any]) -> None:
    for field in ("repo", "pr_number", "head_sha", "run_id"):
        expected = item.get(field)
        actual = bundle.get(field)
        if expected not in (None, "") and str(actual) != str(expected):
            raise ArtifactIntegrityError(
                f"Artifact identity mismatch for {field}: expected {expected!r}, got {actual!r}"
            )


def get_item(
    repo: str,
    pr_number: int,
    table=None,
    *,
    consistent_read: bool = False,
) -> Optional[Dict[str, Any]]:
    table = table or get_table()
    kwargs: Dict[str, Any] = {
        "Key": {"repo": repo, "pr_number": int(pr_number)}
    }
    if consistent_read:
        kwargs["ConsistentRead"] = True
    response = table.get_item(**kwargs)
    return response.get("Item")


def load_repo_fact_sheet(repo: str, *, head_sha: str, table=None) -> str:
    item = get_item(repo, 0, table=table)
    if not item or item.get("status") != "REPO_FACT_SHEET":
        return ""
    if not head_sha or str(item.get("fact_sheet_head_sha") or "") != str(head_sha):
        return ""
    if (
        str(item.get("fact_sheet_schema_version") or "")
        != REPO_FACT_SHEET_SCHEMA_VERSION
    ):
        return ""
    return str(item.get("fact_sheet") or "")


def store_repo_fact_sheet(
    repo: str,
    fact_sheet: str,
    *,
    head_sha: str,
    owner_run_id: str = "",
    table=None,
) -> None:
    if not fact_sheet or not head_sha:
        return
    table = table or get_table()
    table.update_item(
        Key={"repo": repo, "pr_number": 0},
        UpdateExpression=(
            "SET #s = :status, fact_sheet = :fact_sheet, "
            "fact_sheet_head_sha = :head, "
            "fact_sheet_schema_version = :schema_version, "
            "fact_sheet_run_id = :run_id, updated_at = :now, "
            "ttl_epoch = :ttl"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": "REPO_FACT_SHEET",
            ":fact_sheet": fact_sheet[:12000],
            ":head": str(head_sha),
            ":schema_version": REPO_FACT_SHEET_SCHEMA_VERSION,
            ":run_id": str(owner_run_id or ""),
            ":now": iso_now(),
            ":ttl": ttl_epoch(config.TTL_DAYS),
        },
    )


def increment_phase_attempt(
    repo: str,
    pr_number: int,
    phase: str,
    *,
    runtime_identity: Optional[Dict[str, Any]] = None,
    table=None,
) -> int:
    if phase not in {"context", "review"}:
        raise ValueError(f"Unsupported pipeline phase: {phase}")
    table = table or get_table()
    phase_field = f"{phase}_attempt"
    names = {"#phase_attempt": phase_field}
    values: Dict[str, Any] = {":one": 1, ":now": iso_now()}
    set_parts = ["updated_at = :now"]
    if runtime_identity is not None:
        names["#runtime_identity"] = f"{phase}_runtime_identity"
        values[":runtime_identity"] = _dynamodb_safe(runtime_identity)
        set_parts.append("#runtime_identity = :runtime_identity")
    response = table.update_item(
        Key={"repo": repo, "pr_number": int(pr_number)},
        UpdateExpression=(
            "ADD #phase_attempt :one, attempt :one SET "
            + ", ".join(set_parts)
        ),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="UPDATED_NEW",
    )
    attributes = response.get("Attributes") or {}
    if phase_field in attributes:
        return int(attributes[phase_field])
    item = get_item(repo, pr_number, table=table) or {}
    return int(item.get(phase_field) or 0)


def claim_phase_attempt(
    repo: str,
    pr_number: int,
    phase: str,
    *,
    expected_status: str,
    runtime_identity: Dict[str, Any],
    owner_id: str = "",
    stream_event_id: str = "",
    expected_head_sha: str = "",
    expected_run_id: str = "",
    now_epoch: Optional[int] = None,
    lease_seconds: int = PHASE_CLAIM_LEASE_SECONDS,
    table=None,
) -> Optional[Dict[str, Any]]:
    """Claim a phase or safely resume the same stream event after a crash."""

    if phase not in {"context", "review"}:
        raise ValueError(f"Unsupported pipeline phase: {phase}")
    table = table or get_table()
    now = int(time.time() if now_epoch is None else now_epoch)
    lease = max(PHASE_CLAIM_LEASE_SECONDS, int(lease_seconds))
    owner = str(
        owner_id
        or runtime_identity.get("aws_request_id")
        or uuid.uuid4().hex
    )[:128]
    event_id = str(stream_event_id or "")[:256]
    claim = {
        "schema_version": 1,
        "phase": phase,
        "owner_id": owner,
        "stream_event_id": event_id,
        "claimed_at_epoch": now,
        "expires_at_epoch": now + lease,
        "runtime_identity": _dynamodb_safe(runtime_identity),
    }
    phase_field = f"{phase}_attempt"
    claim_field = f"{phase}_claim"
    identity_field = f"{phase}_runtime_identity"
    try:
        same_event_clause = (
            " OR #claim.#stream_event_id = :stream_event_id"
            if event_id
            else ""
        )
        names = {
            "#s": "status",
            "#phase_attempt": phase_field,
            "#claim": claim_field,
            "#runtime_identity": identity_field,
            "#expires_at_epoch": "expires_at_epoch",
        }
        values = {
            ":expected": str(expected_status),
            ":one": 1,
            ":claim": claim,
            ":runtime_identity": _dynamodb_safe(runtime_identity),
            ":updated_at": iso_now(),
            ":now_epoch": now,
        }
        if event_id:
            names["#stream_event_id"] = "stream_event_id"
            values[":stream_event_id"] = event_id
        identity_clause = ""
        if expected_head_sha:
            names["#head_sha"] = "head_sha"
            values[":expected_head_sha"] = str(expected_head_sha)
            identity_clause += " AND #head_sha = :expected_head_sha"
        if expected_run_id:
            names["#run_id"] = "run_id"
            values[":expected_run_id"] = str(expected_run_id)
            identity_clause += " AND #run_id = :expected_run_id"
        response = table.update_item(
            Key={"repo": repo, "pr_number": int(pr_number)},
            UpdateExpression=(
                "ADD #phase_attempt :one, attempt :one "
                "SET #claim = :claim, #runtime_identity = :runtime_identity, "
                "updated_at = :updated_at"
            ),
            ConditionExpression=(
                "#s = :expected AND "
                "(attribute_not_exists(#claim) "
                "OR #claim.#expires_at_epoch < :now_epoch"
                f"{same_event_clause}){identity_clause}"
            ),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="UPDATED_NEW",
        )
    except Exception as exc:
        if _is_conditional_failure(exc):
            return None
        raise
    attempt = int(
        (response.get("Attributes") or {}).get(phase_field) or 0
    )
    if attempt <= 0:
        current = get_item(repo, pr_number, table=table) or {}
        attempt = int(current.get(phase_field) or 0)
    return {
        "phase": phase,
        "owner_id": owner,
        "stream_event_id": event_id,
        "attempt": attempt,
        "expires_at_epoch": claim["expires_at_epoch"],
    }


def claim_current_phase_delivery(
    repo: str,
    pr_number: int,
    phase: str,
    *,
    expected_status: str,
    runtime_identity: Dict[str, Any],
    stream_event_id: str,
    expected_head_sha: str = "",
    expected_run_id: str = "",
    table=None,
) -> Dict[str, Any]:
    """Reread, claim and verify one current stream-delivered phase item."""

    current = get_item(
        repo,
        pr_number,
        table=table,
        consistent_read=True,
    )
    identity_matches = bool(current) and bool(
        not expected_head_sha
        or str(current.get("head_sha") or "") == str(expected_head_sha)
    ) and bool(
        not expected_run_id
        or str(current.get("run_id") or "") == str(expected_run_id)
    )
    if (
        not current
        or str(current.get("status") or "") != str(expected_status)
        or not identity_matches
    ):
        return {
            "eligible": False,
            "current_item": current,
            "phase_claim": None,
            "claim_valid": False,
        }
    phase_claim = claim_phase_attempt(
        repo,
        pr_number,
        phase,
        expected_status=expected_status,
        runtime_identity=runtime_identity,
        stream_event_id=stream_event_id,
        expected_head_sha=expected_head_sha,
        expected_run_id=expected_run_id,
        table=table,
    )
    if phase_claim is None:
        return {
            "eligible": True,
            "current_item": current,
            "phase_claim": None,
            "claim_valid": False,
        }
    claimed_item = get_item(
        repo,
        pr_number,
        table=table,
        consistent_read=True,
    ) or {}
    active_claim = claimed_item.get(f"{phase}_claim")
    claim_valid = bool(
        str(claimed_item.get("status") or "") == str(expected_status)
        and isinstance(active_claim, Mapping)
        and str(active_claim.get("owner_id") or "")
        == str(phase_claim.get("owner_id") or "")
        and str(active_claim.get("stream_event_id") or "")
        == str(stream_event_id or "")
        and int(claimed_item.get(f"{phase}_attempt") or 0)
        == int(phase_claim.get("attempt") or 0)
        and (
            not expected_head_sha
            or str(claimed_item.get("head_sha") or "")
            == str(expected_head_sha)
        )
        and (
            not expected_run_id
            or str(claimed_item.get("run_id") or "")
            == str(expected_run_id)
        )
    )
    return {
        "eligible": True,
        "current_item": claimed_item,
        "phase_claim": phase_claim,
        "claim_valid": claim_valid,
    }


def release_phase_claim(
    repo: str,
    pr_number: int,
    *,
    expected_status: str,
    phase_claim: Mapping[str, Any],
    table=None,
) -> bool:
    """Release only the caller's still-active lease before a bounded retry."""

    phase = str(phase_claim.get("phase") or "")
    owner = str(phase_claim.get("owner_id") or "")
    if phase not in {"context", "review"} or not owner:
        return False
    table = table or get_table()
    claim_field = f"{phase}_claim"
    try:
        table.update_item(
            Key={"repo": repo, "pr_number": int(pr_number)},
            UpdateExpression="SET updated_at = :now REMOVE #claim",
            ConditionExpression=(
                "#s = :expected AND #claim.#owner_id = :owner_id"
            ),
            ExpressionAttributeNames={
                "#s": "status",
                "#claim": claim_field,
                "#owner_id": "owner_id",
            },
            ExpressionAttributeValues={
                ":expected": str(expected_status),
                ":owner_id": owner,
                ":now": iso_now(),
            },
        )
        return True
    except Exception as exc:
        if _is_conditional_failure(exc):
            return False
        raise


def store_publication_intent(
    repo: str,
    pr_number: int,
    *,
    expected_status: str,
    head_sha: str,
    intent: Mapping[str, Any],
    phase_claim: Mapping[str, Any],
    table=None,
) -> bool:
    """Persist one immutable prepared intent under the active phase owner."""

    table = table or get_table()
    phase = str(phase_claim.get("phase") or "")
    owner = str(phase_claim.get("owner_id") or "")
    if phase not in {"context", "review"} or not owner:
        raise ValueError("publication intent requires an active phase owner")
    safe_intent = _dynamodb_safe(dict(intent))
    if safe_intent.get("state") != "prepared":
        raise ValueError("new publication intent must be prepared")
    try:
        table.update_item(
            Key={"repo": repo, "pr_number": int(pr_number)},
            UpdateExpression=(
                "SET #intent = :intent, updated_at = :now"
            ),
            ConditionExpression=(
                "#s = :expected AND #head = :head AND "
                "#claim.#owner_id = :owner_id AND "
                "attribute_not_exists(#intent) AND "
                "attribute_not_exists(#receipt)"
            ),
            ExpressionAttributeNames={
                "#s": "status",
                "#head": "head_sha",
                "#claim": f"{phase}_claim",
                "#owner_id": "owner_id",
                "#intent": "publication_intent",
                "#receipt": "publication_receipt",
            },
            ExpressionAttributeValues={
                ":expected": str(expected_status),
                ":head": str(head_sha),
                ":owner_id": owner,
                ":intent": safe_intent,
                ":now": iso_now(),
            },
        )
        return True
    except Exception as exc:
        if _is_conditional_failure(exc):
            return False
        raise


def replace_publication_intent(
    repo: str,
    pr_number: int,
    *,
    expected_status: str,
    expected_intent: Mapping[str, Any],
    next_intent: Mapping[str, Any],
    phase_claim: Mapping[str, Any],
    table=None,
) -> bool:
    """CAS one publication-intent state while retaining phase ownership."""

    table = table or get_table()
    phase = str(phase_claim.get("phase") or "")
    owner = str(phase_claim.get("owner_id") or "")
    if phase not in {"context", "review"} or not owner:
        raise ValueError("publication intent CAS requires a phase owner")
    safe_expected = _dynamodb_safe(dict(expected_intent))
    safe_next = _dynamodb_safe(dict(next_intent))
    try:
        table.update_item(
            Key={"repo": repo, "pr_number": int(pr_number)},
            UpdateExpression=(
                "SET #intent = :next_intent, updated_at = :now"
            ),
            ConditionExpression=(
                "#s = :expected_status AND "
                "#claim.#owner_id = :owner_id AND "
                "#intent = :expected_intent AND "
                "attribute_not_exists(#receipt)"
            ),
            ExpressionAttributeNames={
                "#s": "status",
                "#claim": f"{phase}_claim",
                "#owner_id": "owner_id",
                "#intent": "publication_intent",
                "#receipt": "publication_receipt",
            },
            ExpressionAttributeValues={
                ":expected_status": str(expected_status),
                ":owner_id": owner,
                ":expected_intent": safe_expected,
                ":next_intent": safe_next,
                ":now": iso_now(),
            },
        )
        return True
    except Exception as exc:
        if _is_conditional_failure(exc):
            return False
        raise


def increment_attempt(repo: str, pr_number: int, *, table=None) -> None:
    """Compatibility wrapper for older callers; new code uses phase attempts."""
    table = table or get_table()
    table.update_item(
        Key={"repo": repo, "pr_number": int(pr_number)},
        UpdateExpression="ADD attempt :one SET updated_at = :now",
        ExpressionAttributeValues={":one": 1, ":now": iso_now()},
    )


def _provider_call_attr(call_id: str) -> str:
    normalized = str(call_id or "").strip().lower()
    if not _PROVIDER_CALL_ID_RE.fullmatch(normalized):
        raise ValueError("provider call_id must be a lowercase SHA-256 hex digest")
    return f"{PROVIDER_CALL_ATTR_PREFIX}{normalized}"


_PROVIDER_CALL_IDENTITY_FIELDS = (
    "schema_version",
    "call_id",
    "operation_id",
    "run_id",
    "head_sha",
    "phase",
    "pipeline_phase",
    "pipeline_attempt",
    "call_index",
    "transport_attempt_index",
    "model",
    "logical_model",
    "billed_model",
    "thinking",
    "reasoning_effort",
)


def _same_provider_call_identity(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    return all(
        left.get(field) == right.get(field)
        for field in _PROVIDER_CALL_IDENTITY_FIELDS
    )


def _unresolved_provider_dispatch(
    item: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not isinstance(item, Mapping):
        return None
    for key, value in item.items():
        if not str(key).startswith(PROVIDER_CALL_ATTR_PREFIX):
            continue
        if not isinstance(value, Mapping):
            continue
        if str(value.get("status") or "") == "dispatching":
            return dict(value)
    return None


def _provider_phase_claim_binding(
    current: Mapping[str, Any],
    record: Mapping[str, Any],
    phase_claim: Mapping[str, Any],
) -> Optional[tuple[str, str, str, int, str, str]]:
    """Return exact live claim coordinates when they authorize this call."""

    phase = str(phase_claim.get("phase") or "")
    owner = str(phase_claim.get("owner_id") or "")
    event_id = str(phase_claim.get("stream_event_id") or "")
    try:
        attempt = int(phase_claim.get("attempt") or 0)
        active_attempt = int(current.get(f"{phase}_attempt") or 0)
        record_attempt = int(record.get("pipeline_attempt") or 0)
    except (TypeError, ValueError):
        return None
    active_claim = current.get(f"{phase}_claim")
    if (
        phase not in {"context", "review"}
        or not owner
        or not event_id
        or attempt <= 0
        or not isinstance(active_claim, Mapping)
        or str(active_claim.get("owner_id") or "") != owner
        or str(active_claim.get("stream_event_id") or "") != event_id
        or active_attempt != attempt
        or str(record.get("pipeline_phase") or "") != phase
        or record_attempt != attempt
        or str(record.get("run_id") or "")
        != str(current.get("run_id") or "")
        or str(record.get("head_sha") or "")
        != str(current.get("head_sha") or "")
    ):
        return None
    return (
        phase,
        owner,
        event_id,
        attempt,
        str(record.get("run_id") or ""),
        str(record.get("head_sha") or ""),
    )


def begin_provider_call_dispatch(
    repo: str,
    pr_number: int,
    *,
    expected_status: str,
    record: Mapping[str, Any],
    phase_claim: Optional[Mapping[str, Any]] = None,
    table=None,
) -> bool:
    """Persist one owner-bound attempt fence before provider HTTP begins.

    No second fence may be created while any prior provider attempt on the item
    remains ``dispatching``.  That deliberate fail-closed boundary prevents an
    at-least-once stream retry from buying another call when the prior transport
    outcome cannot be proven.
    """

    table = table or get_table()
    call_id = str(record.get("call_id") or "").strip().lower()
    attribute = _provider_call_attr(call_id)
    if int(record.get("schema_version") or 0) != 2:
        raise ValueError("provider dispatch fence must use schema version 2")
    if str(record.get("status") or "") != "dispatching":
        raise ValueError("provider dispatch fence status must be dispatching")
    if str(record.get("usage_state") or "") != "unreported":
        raise ValueError("provider dispatch fence usage must be unreported")
    if record.get("usage") not in ({}, None):
        raise ValueError("provider dispatch fence cannot report usage")
    try:
        transport_attempt_index = int(
            record.get("transport_attempt_index") or 0
        )
        transport_dispatch_count = int(
            record.get("transport_dispatch_count") or 0
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("provider dispatch fence transport identity is invalid") from exc
    expected_call_id = hashlib.sha256(
        _canonical_json(
            {
                "operation_id": str(record.get("operation_id") or ""),
                "transport_attempt_index": transport_attempt_index,
            }
        )
    ).hexdigest()
    if (
        transport_attempt_index < 1
        or transport_dispatch_count != 1
        or expected_call_id != call_id
    ):
        raise ValueError("provider dispatch fence call identity is invalid")
    safe_record = _dynamodb_safe(dict(record))
    current = get_item(
        repo,
        pr_number,
        table=table,
        consistent_read=True,
    )
    if not current or str(current.get("status") or "") != str(expected_status):
        return False
    claim_binding = None
    if phase_claim:
        claim_binding = _provider_phase_claim_binding(
            current,
            record,
            phase_claim,
        )
        if claim_binding is None:
            return False
    unresolved = _unresolved_provider_dispatch(current)
    if unresolved is not None:
        raise ProviderDispatchFenceUnresolved(unresolved)
    candidate = {
        **current,
        attribute: safe_record,
        "updated_at": iso_now(),
    }
    estimated = estimate_dynamodb_wire_bytes(candidate)
    if estimated > int(config.MAX_DYNAMODB_WIRE_BYTES):
        raise DynamoItemTooLarge(
            f"DynamoDB provider-call fence candidate is {estimated} bytes; "
            f"safe limit is {config.MAX_DYNAMODB_WIRE_BYTES}"
        )
    try:
        names = {"#s": "status", "#call": attribute}
        values = {
            ":expected": str(expected_status),
            ":record": safe_record,
            ":now": iso_now(),
        }
        condition = "#s = :expected AND attribute_not_exists(#call)"
        if claim_binding is not None:
            phase, owner, event_id, attempt, run_id, head_sha = (
                claim_binding
            )
            names.update(
                {
                    "#claim": f"{phase}_claim",
                    "#owner_id": "owner_id",
                    "#stream_event_id": "stream_event_id",
                    "#phase_attempt": f"{phase}_attempt",
                    "#run_id": "run_id",
                    "#head_sha": "head_sha",
                }
            )
            values.update(
                {
                    ":owner_id": owner,
                    ":stream_event_id": event_id,
                    ":phase_attempt": attempt,
                    ":run_id": run_id,
                    ":head_sha": head_sha,
                }
            )
            condition += (
                " AND #claim.#owner_id = :owner_id"
                " AND #claim.#stream_event_id = :stream_event_id"
                " AND #phase_attempt = :phase_attempt"
                " AND #run_id = :run_id"
                " AND #head_sha = :head_sha"
            )
        table.update_item(
            Key={"repo": repo, "pr_number": int(pr_number)},
            UpdateExpression="SET #call = :record, updated_at = :now",
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except Exception as exc:
        if _is_conditional_failure(exc):
            latest = get_item(
                repo,
                pr_number,
                table=table,
                consistent_read=True,
            ) or {}
            unresolved = _unresolved_provider_dispatch(latest)
            if unresolved is not None:
                raise ProviderDispatchFenceUnresolved(unresolved) from exc
            return False
        raise


def provider_call_records(item: Optional[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    """Return one stable, content-free record per provider operation."""

    if not isinstance(item, Mapping):
        return []
    by_id: Dict[str, Dict[str, Any]] = {}
    for key, value in item.items():
        if not str(key).startswith(PROVIDER_CALL_ATTR_PREFIX):
            continue
        if not isinstance(value, Mapping):
            continue
        call_id = str(value.get("call_id") or "").strip().lower()
        if not _PROVIDER_CALL_ID_RE.fullmatch(call_id):
            continue
        by_id.setdefault(call_id, dict(value))
    # Recovery fallback for records written before provider calls moved to one
    # conditional top-level attribute per dispatch fence.
    for value in item.get("deepseek_all_attempt_model_phases") or []:
        if not isinstance(value, Mapping):
            continue
        call_id = str(value.get("call_id") or "").strip().lower()
        if _PROVIDER_CALL_ID_RE.fullmatch(call_id):
            by_id.setdefault(call_id, dict(value))

    def sort_key(record: Dict[str, Any]) -> tuple[Any, ...]:
        pipeline_phase = str(record.get("pipeline_phase") or "")
        return (
            0 if pipeline_phase == "context" else 1 if pipeline_phase == "review" else 2,
            int(record.get("pipeline_attempt") or 0),
            _PROVIDER_PHASE_ORDER.get(str(record.get("phase") or ""), 99),
            int(record.get("call_index") or 0),
            int(record.get("transport_attempt_index") or 0),
            str(record.get("call_id") or ""),
        )

    return sorted(by_id.values(), key=sort_key)


def record_provider_call(
    repo: str,
    pr_number: int,
    *,
    expected_status: str,
    record: Mapping[str, Any],
    phase_claim: Optional[Mapping[str, Any]] = None,
    table=None,
) -> bool:
    """Finalize one provider operation exactly once without advancing state.

    A separate top-level attribute per stable call id avoids a read/modify/write
    list race between at-least-once Lambda invocations. A terminal record may
    replace only its exact ``dispatching`` fence; it can never attach to another
    operation or owner. Duplicate delivery of the same terminal record is a
    successful no-op.
    """

    table = table or get_table()
    call_id = str(record.get("call_id") or "").strip().lower()
    attribute = _provider_call_attr(call_id)
    safe_record = _dynamodb_safe(dict(record))
    current = get_item(
        repo,
        pr_number,
        table=table,
        consistent_read=True,
    )
    if not current or str(current.get("status") or "") != str(expected_status):
        return False
    claim_binding = None
    if phase_claim:
        claim_binding = _provider_phase_claim_binding(
            current,
            record,
            phase_claim,
        )
        if claim_binding is None:
            return False
    existing = current.get(attribute)
    if isinstance(existing, Mapping):
        if (
            str(existing.get("call_id") or "").strip().lower()
            == call_id
            and str(existing.get("status") or "") != "dispatching"
            and dict(existing) == safe_record
        ):
            return True
        if (
            str(existing.get("status") or "") != "dispatching"
            or not _same_provider_call_identity(existing, safe_record)
        ):
            raise ArtifactIntegrityError(
                f"Provider call terminal record conflicts for {call_id}"
            )
        if str(safe_record.get("status") or "") not in {
            "completed",
            "http_retry",
            "http_error",
            "transport_error",
        }:
            raise ArtifactIntegrityError(
                f"Provider call terminal status is invalid for {call_id}"
            )
        dispatching_record = _dynamodb_safe(dict(existing))
    else:
        # Compatibility for callers that have not installed the pre-dispatch
        # sink (offline tools and historical fixtures). Production binds both
        # sinks and therefore always takes the exact-fence CAS branch.
        dispatching_record = None
    if isinstance(existing, Mapping) and str(
        existing.get("call_id") or ""
    ).strip().lower() != call_id:
        raise ArtifactIntegrityError(
            f"Provider call attribute collision for {call_id}"
        )
    candidate = {
        **current,
        attribute: safe_record,
        "updated_at": iso_now(),
    }
    estimated = estimate_dynamodb_wire_bytes(candidate)
    if estimated > int(config.MAX_DYNAMODB_WIRE_BYTES):
        raise DynamoItemTooLarge(
            f"DynamoDB provider-call ledger candidate is {estimated} bytes; "
            f"safe limit is {config.MAX_DYNAMODB_WIRE_BYTES}"
        )
    try:
        names = {
            "#s": "status",
            "#call": attribute,
        }
        values = {
            ":expected": str(expected_status),
            ":record": safe_record,
            ":now": iso_now(),
        }
        if dispatching_record is None:
            condition = "#s = :expected AND attribute_not_exists(#call)"
        else:
            values[":dispatching_record"] = dispatching_record
            condition = "#s = :expected AND #call = :dispatching_record"
        if claim_binding is not None:
            phase, owner, event_id, attempt, run_id, head_sha = (
                claim_binding
            )
            names.update(
                {
                    "#claim": f"{phase}_claim",
                    "#owner_id": "owner_id",
                    "#stream_event_id": "stream_event_id",
                    "#phase_attempt": f"{phase}_attempt",
                    "#run_id": "run_id",
                    "#head_sha": "head_sha",
                }
            )
            values.update(
                {
                    ":owner_id": owner,
                    ":stream_event_id": event_id,
                    ":phase_attempt": attempt,
                    ":run_id": run_id,
                    ":head_sha": head_sha,
                }
            )
            condition += (
                " AND #claim.#owner_id = :owner_id"
                " AND #claim.#stream_event_id = :stream_event_id"
                " AND #phase_attempt = :phase_attempt"
                " AND #run_id = :run_id"
                " AND #head_sha = :head_sha"
            )
        table.update_item(
            Key={"repo": repo, "pr_number": int(pr_number)},
            UpdateExpression="SET #call = :record, updated_at = :now",
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except Exception as exc:
        if _is_conditional_failure(exc):
            latest = get_item(
                repo,
                pr_number,
                table=table,
                consistent_read=True,
            ) or {}
            repeated = latest.get(attribute)
            if (
                isinstance(repeated, Mapping)
                and str(repeated.get("call_id") or "").strip().lower()
                == call_id
                and str(repeated.get("status") or "") != "dispatching"
                and dict(repeated) == safe_record
            ):
                return True
            logger.info(
                "Provider-call ledger write lost after state advance: %s#%s call=%s",
                repo,
                pr_number,
                call_id,
            )
            return False
        raise


def estimate_dynamodb_wire_bytes(item: Dict[str, Any]) -> int:
    """Return a conservative boto3 wire-size estimate for a complete item."""
    safe_item = _dynamodb_safe(item)
    try:
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()
        wire = {str(key): serializer.serialize(value) for key, value in safe_item.items()}
        return len(_canonical_json(wire))
    except (ImportError, AttributeError):
        # Unit-test fakes do not install boto3.dynamodb. JSON plus a per-field
        # type/name allowance remains conservative for the JSON-like state used
        # by this pipeline.
        return len(_canonical_json(safe_item)) + sum(len(str(key)) + 8 for key in safe_item)


def _ensure_update_fits(
    repo: str,
    pr_number: int,
    *,
    next_status: str,
    attributes: Dict[str, Any],
    remove_attributes: tuple[str, ...] = (),
    table,
) -> None:
    current = get_item(repo, pr_number, table=table) or {
        "repo": repo,
        "pr_number": int(pr_number),
    }
    candidate = {
        **current,
        **attributes,
        "status": next_status,
        "updated_at": iso_now(),
    }
    for attribute in remove_attributes:
        candidate.pop(attribute, None)
    estimated = estimate_dynamodb_wire_bytes(candidate)
    if estimated > int(config.MAX_DYNAMODB_WIRE_BYTES):
        raise DynamoItemTooLarge(
            f"DynamoDB candidate item is {estimated} bytes; "
            f"safe limit is {config.MAX_DYNAMODB_WIRE_BYTES}"
        )


def update_status(
    repo: str,
    pr_number: int,
    *,
    expected_status: str,
    next_status: str,
    attributes: Optional[Dict[str, Any]] = None,
    phase_claim: Optional[Mapping[str, Any]] = None,
    expected_attributes: Optional[Mapping[str, Any]] = None,
    expected_missing_attributes: tuple[str, ...] = (),
    table=None,
) -> bool:
    table = table or get_table()
    safe_attributes = _dynamodb_safe(attributes or {})
    claim_field = ""
    claim_owner = ""
    if phase_claim:
        phase = str(phase_claim.get("phase") or "")
        claim_owner = str(phase_claim.get("owner_id") or "")
        if phase not in {"context", "review"} or not claim_owner:
            raise ValueError("phase_claim must identify context/review owner")
        claim_field = f"{phase}_claim"
    _ensure_update_fits(
        repo,
        pr_number,
        next_status=next_status,
        attributes=safe_attributes,
        remove_attributes=((claim_field,) if claim_field else ()),
        table=table,
    )
    names = {"#s": "status"}
    values = {":expected": expected_status, ":next": next_status, ":now": iso_now()}
    set_parts = ["#s = :next", "updated_at = :now"]
    for idx, (key, value) in enumerate(safe_attributes.items()):
        name_key = f"#k{idx}"
        value_key = f":v{idx}"
        names[name_key] = key
        values[value_key] = value
        set_parts.append(f"{name_key} = {value_key}")
    condition = "#s = :expected"
    remove_clause = ""
    if claim_field:
        names["#claim"] = claim_field
        names["#owner_id"] = "owner_id"
        values[":owner_id"] = claim_owner
        condition += " AND #claim.#owner_id = :owner_id"
        remove_clause = " REMOVE #claim"
    for idx, (key, value) in enumerate(
        (expected_attributes or {}).items()
    ):
        name_key = f"#expected{idx}"
        value_key = f":expected{idx}"
        names[name_key] = str(key)
        values[value_key] = _dynamodb_safe(value)
        condition += f" AND {name_key} = {value_key}"
    for idx, key in enumerate(expected_missing_attributes):
        name_key = f"#missing{idx}"
        names[name_key] = str(key)
        condition += f" AND attribute_not_exists({name_key})"
    try:
        table.update_item(
            Key={"repo": repo, "pr_number": int(pr_number)},
            UpdateExpression=(
                "SET " + ", ".join(set_parts) + remove_clause
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except Exception as exc:  # boto3 is optional in unit tests
        if _is_conditional_failure(exc):
            logger.info("Conditional status write lost: %s#%s %s->%s", repo, pr_number, expected_status, next_status)
            return False
        raise


def mark_error(
    repo: str,
    pr_number: int,
    expected_status: str,
    error_message: str,
    *,
    error_kind: str = "pipeline_error",
    error_stage: str = "unknown",
    retryable: bool = False,
    retry_exhausted: bool = False,
    attempt: Optional[int] = None,
    extra_attrs: Optional[Dict[str, Any]] = None,
    phase_claim: Optional[Mapping[str, Any]] = None,
    table=None,
) -> bool:
    attrs: Dict[str, Any] = {
        "error_message": str(error_message)[:3500],
        "error_kind": str(error_kind),
        "error_stage": str(error_stage),
        "error_retryable": bool(retryable),
        "error_retry_exhausted": bool(retry_exhausted),
    }
    if attempt is not None:
        attrs["error_attempt"] = int(attempt)
    if extra_attrs:
        attrs.update(extra_attrs)
    return update_status(
        repo,
        pr_number,
        expected_status=expected_status,
        next_status="ERROR",
        attributes=attrs,
        phase_claim=phase_claim,
        table=table,
    )


def store_review_failure(
    repo: str,
    pr_number: int,
    *,
    expected_status: str,
    artifact: Dict[str, Any],
    error_kind: str,
    error_stage: str,
    retryable: bool,
    retry_exhausted: bool,
    attempt: int,
    head_sha: str,
    run_id: str,
    extra_attrs: Optional[Dict[str, Any]] = None,
    phase_claim: Optional[Mapping[str, Any]] = None,
    table=None,
    s3_client=None,
) -> bool:
    """Persist a non-publishable review attempt and atomically end it as ERROR."""

    attrs: Dict[str, Any] = {
        **_review_artifact_attrs(
            repo,
            pr_number,
            artifact,
            head_sha=head_sha,
            run_id=run_id,
            require_main_comment=False,
            s3_client=s3_client,
        ),
        **(extra_attrs or {}),
        "error_message": "The review service did not produce a publishable review.",
        "error_kind": str(error_kind or "review_generation_incomplete"),
        "error_stage": str(error_stage or "review"),
        "error_retryable": bool(retryable),
        "error_retry_exhausted": bool(retry_exhausted),
        "error_attempt": int(attempt),
        "review_artifact_persisted": True,
    }
    return update_status(
        repo,
        pr_number,
        expected_status=expected_status,
        next_status="ERROR",
        attributes=attrs,
        phase_claim=phase_claim,
        table=table,
    )


def mark_superseded(
    repo: str,
    pr_number: int,
    expected_status: str,
    *,
    expected_head_sha: str,
    actual_head_sha: str,
    stage: str,
    superseded_kind: str = "head_changed",
    current_state: str = "",
    merged: Optional[bool] = None,
    extra_attrs: Optional[Dict[str, Any]] = None,
    phase_claim: Optional[Mapping[str, Any]] = None,
    expected_publication_intent: Optional[Mapping[str, Any]] = None,
    table=None,
) -> bool:
    attributes = {
        "superseded_stage": stage,
        "superseded_kind": superseded_kind,
        "queued_head_sha": expected_head_sha,
        "current_head_sha": actual_head_sha,
        "superseded_at": iso_now(),
    }
    if current_state:
        attributes["current_pr_state"] = current_state
    if merged is not None:
        attributes["current_pr_merged"] = bool(merged)
    if extra_attrs:
        attributes.update(extra_attrs)
    return update_status(
        repo,
        pr_number,
        expected_status=expected_status,
        next_status="SUPERSEDED",
        attributes=attributes,
        phase_claim=phase_claim,
        expected_attributes=(
            {"publication_intent": expected_publication_intent}
            if expected_publication_intent is not None
            else None
        ),
        expected_missing_attributes=(
            ("publication_receipt",)
            if expected_publication_intent is not None
            else ()
        ),
        table=table,
    )


def head_successor_run_id(repo: str, pr_number: int, head_sha: str) -> str:
    """Return the one stable queue identity for an early successor head."""

    identity = f"{str(repo)}\n{int(pr_number)}\n{str(head_sha)}".encode(
        "utf-8"
    )
    return f"head_successor_v1_{hashlib.sha256(identity).hexdigest()}"


def record_initial_admission(
    repo: str,
    pr_number: int,
    *,
    expected_status: str,
    expected_head_sha: str,
    phase_claim: Mapping[str, Any],
    table=None,
) -> bool:
    """Durably prove the first owner-bound open/same-head admission.

    A retry reuses the original receipt and therefore retains its original
    ``admitted_at`` rather than making an already-ended pull request look newly
    admitted.
    """

    table = table or get_table()
    phase = str(phase_claim.get("phase") or "")
    owner = str(phase_claim.get("owner_id") or "")
    stream_event_id = str(phase_claim.get("stream_event_id") or "")
    if phase not in {"context", "review"} or not owner:
        raise ValueError("initial admission requires an active phase owner")
    expected_phase_status = {
        "context": "PENDING",
        "review": "CONTEXT_READY",
    }[phase]
    if str(expected_status) != expected_phase_status:
        raise ValueError("initial admission phase and status do not agree")
    current = get_item(
        repo,
        pr_number,
        table=table,
        consistent_read=True,
    ) or {}
    run_identity = str(
        current.get("run_id")
        or current.get("delivery_id")
        or f"{str(repo).replace('/', '_')}_{int(pr_number)}"
    )
    existing = current.get("initial_admission")
    if isinstance(existing, Mapping):
        return bool(
            int(existing.get("schema_version") or 0) == 1
            and str(existing.get("disposition") or "")
            == "open_same_head"
            and str(existing.get("head_sha") or "")
            == str(expected_head_sha)
            and str(existing.get("run_id") or "") == run_identity
        )
    if (
        str(current.get("status") or "") != str(expected_status)
        or str(current.get("head_sha") or "") != str(expected_head_sha)
    ):
        return False
    receipt = {
        "schema_version": 1,
        "disposition": "open_same_head",
        "head_sha": str(expected_head_sha),
        "run_id": run_identity,
        "admitted_at": iso_now(),
    }
    try:
        table.update_item(
            Key={"repo": repo, "pr_number": int(pr_number)},
            UpdateExpression="SET #admission = :admission, updated_at = :now",
            ConditionExpression=(
                "#s = :expected AND #head = :head AND #run = :run "
                "AND #claim.#owner_id = :owner_id "
                "AND #claim.#stream_event_id = :stream_event_id "
                "AND attribute_not_exists(#admission)"
            ),
            ExpressionAttributeNames={
                "#s": "status",
                "#head": "head_sha",
                "#run": "run_id",
                "#claim": f"{phase}_claim",
                "#owner_id": "owner_id",
                "#stream_event_id": "stream_event_id",
                "#admission": "initial_admission",
            },
            ExpressionAttributeValues={
                ":expected": str(expected_status),
                ":head": str(expected_head_sha),
                ":run": run_identity,
                ":owner_id": owner,
                ":stream_event_id": stream_event_id,
                ":admission": receipt,
                ":now": iso_now(),
            },
        )
        return True
    except Exception as exc:
        if not _is_conditional_failure(exc):
            raise
        latest = get_item(
            repo,
            pr_number,
            table=table,
            consistent_read=True,
        ) or {}
        persisted = latest.get("initial_admission")
        return bool(
            isinstance(persisted, Mapping)
            and str(persisted.get("head_sha") or "")
            == str(expected_head_sha)
            and str(persisted.get("run_id") or "") == run_identity
            and str(persisted.get("disposition") or "")
            == "open_same_head"
        )


_SUCCESSOR_STALE_EXACT_FIELDS = {
    "analyzer_result",
    "current_head_sha",
    "current_pr_merged",
    "current_pr_state",
    "deepseek_all_attempt_model_phases",
    "deepseek_discarded_model_phases",
    "deepseek_discarded_usage_total",
    "deepseek_model_phases",
    "deepseek_usage_accounting",
    "deepseek_usage_total",
    "deepseek_winning_usage_total",
    "initial_admission",
    "provider_source_identity",
    "provider_source_sha256",
    "queued_head_sha",
    "route_reason",
}
_SUCCESSOR_STALE_PREFIXES = (
    "context_",
    "review_",
    "publication_",
    "error_",
    "superseded_",
)


def _successor_stale_fields(item: Mapping[str, Any]) -> tuple[str, ...]:
    retained = {"context_attempt", "review_attempt"}
    return tuple(
        key
        for key in item
        if key not in retained
        and not str(key).startswith(PROVIDER_CALL_ATTR_PREFIX)
        and (
            key in _SUCCESSOR_STALE_EXACT_FIELDS
            or str(key).startswith(_SUCCESSOR_STALE_PREFIXES)
        )
    )


def requeue_head_successor(
    repo: str,
    pr_number: int,
    *,
    expected_status: str,
    expected_head_sha: str,
    actual_head_sha: str,
    stage: str,
    phase_claim: Mapping[str, Any],
    table=None,
) -> bool:
    """Atomically replace one early superseded head with its sole successor.

    Provider attempt numbers and durable call records remain on the item. The
    new Context claim increments the monotonic attempt, so predecessor usage is
    projected as discarded rather than becoming the successor's winning work.
    """

    table = table or get_table()
    phase = str(phase_claim.get("phase") or "")
    owner = str(phase_claim.get("owner_id") or "")
    stream_event_id = str(phase_claim.get("stream_event_id") or "")
    try:
        claim_attempt = int(phase_claim.get("attempt") or 0)
    except (TypeError, ValueError):
        claim_attempt = 0
    if phase != "context" or not owner or not stream_event_id or claim_attempt <= 0:
        raise ValueError("head successor requires the active context owner")
    if not actual_head_sha or str(actual_head_sha) == str(expected_head_sha):
        raise ValueError("head successor requires a distinct nonempty head")

    current = get_item(
        repo,
        pr_number,
        table=table,
        consistent_read=True,
    ) or {}
    predecessor_run_id = str(current.get("run_id") or "")
    successor_count = int(current.get("head_successor_count") or 0)
    context_attempt = int(current.get("context_attempt") or 0)
    active_claim = current.get("context_claim")
    if (
        str(current.get("status") or "") != str(expected_status)
        or str(expected_status) != "PENDING"
        or str(current.get("head_sha") or "") != str(expected_head_sha)
        or not predecessor_run_id
        or successor_count != 0
        or context_attempt >= int(config.MAX_ATTEMPTS)
        or "publication_intent" in current
        or "publication_receipt" in current
        or not isinstance(active_claim, Mapping)
        or str(active_claim.get("owner_id") or "") != owner
        or str(active_claim.get("stream_event_id") or "") != stream_event_id
        or int(current.get("context_attempt") or 0) != claim_attempt
    ):
        return False
    unresolved = _unresolved_provider_dispatch(current)
    if unresolved is not None:
        return False

    provider_records = provider_call_records(current)
    retained_provider_ids = {
        str(value.get("call_id") or "")
        for key, value in current.items()
        if str(key).startswith(PROVIDER_CALL_ATTR_PREFIX)
        and isinstance(value, Mapping)
    }
    if len(provider_records) > 64:
        raise ArtifactIntegrityError(
            "Head predecessor provider ledger exceeds its bounded receipt"
        )
    call_ids = [str(record.get("call_id") or "") for record in provider_records]
    if any(not _PROVIDER_CALL_ID_RE.fullmatch(call_id) for call_id in call_ids):
        raise ArtifactIntegrityError(
            "Head predecessor provider ledger contains an invalid call identity"
        )
    if set(call_ids) != retained_provider_ids:
        # Historical aggregate-only records cannot be safely moved while also
        # clearing stale result projections. Fail closed instead of claiming
        # that their exact attempt ledger survived the successor transition.
        return False
    successor_run_id = head_successor_run_id(repo, pr_number, actual_head_sha)
    now = iso_now()
    receipt = {
        "schema_version": 1,
        "kind": "head_predecessor",
        "outcome": "SUPERSEDED",
        "stage": str(stage),
        "predecessor_head_sha": str(expected_head_sha),
        "predecessor_run_id": predecessor_run_id,
        "successor_head_sha": str(actual_head_sha),
        "successor_run_id": successor_run_id,
        "attempt": int(current.get("attempt") or 0),
        "context_attempt": context_attempt,
        "review_attempt": int(current.get("review_attempt") or 0),
        "provider_call_count": len(call_ids),
        "provider_call_ids": call_ids,
        "provider_calls_retained_on_item": True,
        "provider_calls_terminal": True,
        "superseded_at": now,
    }
    stale_fields = _successor_stale_fields(current)
    successor_attrs = {
        "head_sha": str(actual_head_sha),
        "run_id": successor_run_id,
        "head_successor_count": 1,
        "head_predecessor_receipt": receipt,
        "updated_at": now,
    }
    candidate = {**current, **successor_attrs}
    for key in stale_fields:
        candidate.pop(key, None)
    if estimate_dynamodb_wire_bytes(candidate) > int(config.MAX_DYNAMODB_WIRE_BYTES):
        raise DynamoItemTooLarge("Head successor candidate exceeds DynamoDB safe limit")

    names = {
        "#s": "status",
        "#head": "head_sha",
        "#run": "run_id",
        "#count": "head_successor_count",
        "#claim": "context_claim",
        "#owner_id": "owner_id",
        "#stream_event_id": "stream_event_id",
        "#context_attempt": "context_attempt",
        "#publication_intent": "publication_intent",
        "#publication_receipt": "publication_receipt",
    }
    values: Dict[str, Any] = {
        ":pending": "PENDING",
        ":expected_head": str(expected_head_sha),
        ":predecessor_run": predecessor_run_id,
        ":zero": 0,
        ":owner_id": owner,
        ":stream_event_id": stream_event_id,
        ":context_attempt": claim_attempt,
    }
    set_parts: list[str] = []
    for index, (key, value) in enumerate(successor_attrs.items()):
        name = f"#set{index}"
        placeholder = f":set{index}"
        names[name] = key
        values[placeholder] = _dynamodb_safe(value)
        set_parts.append(f"{name} = {placeholder}")
    remove_parts: list[str] = []
    for index, key in enumerate(stale_fields):
        name = f"#remove{index}"
        names[name] = key
        remove_parts.append(name)
    update_expression = "SET " + ", ".join(set_parts)
    if remove_parts:
        update_expression += " REMOVE " + ", ".join(remove_parts)
    try:
        table.update_item(
            Key={"repo": repo, "pr_number": int(pr_number)},
            UpdateExpression=update_expression,
            ConditionExpression=(
                "#s = :pending AND #head = :expected_head "
                "AND #run = :predecessor_run "
                "AND (attribute_not_exists(#count) OR #count = :zero) "
                "AND #claim.#owner_id = :owner_id "
                "AND #claim.#stream_event_id = :stream_event_id "
                "AND #context_attempt = :context_attempt "
                "AND attribute_not_exists(#publication_intent) "
                "AND attribute_not_exists(#publication_receipt)"
            ),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except Exception as exc:
        if _is_conditional_failure(exc):
            return False
        raise


def is_valid_head_successor_transition(
    old_item: Mapping[str, Any],
    new_item: Mapping[str, Any],
) -> bool:
    """Validate the sole PENDING-to-PENDING stream transition we dispatch."""

    if str(old_item.get("status") or "") != "PENDING" or str(
        new_item.get("status") or ""
    ) != "PENDING":
        return False
    try:
        old_count = int(old_item.get("head_successor_count") or 0)
        new_count = int(new_item.get("head_successor_count") or 0)
    except (TypeError, ValueError):
        return False
    repo = str(new_item.get("repo") or "")
    try:
        pr_number = int(new_item.get("pr_number"))
        old_pr_number = int(old_item.get("pr_number"))
    except (TypeError, ValueError):
        return False
    old_head = str(old_item.get("head_sha") or "")
    new_head = str(new_item.get("head_sha") or "")
    old_run = str(old_item.get("run_id") or "")
    new_run = str(new_item.get("run_id") or "")
    receipt = new_item.get("head_predecessor_receipt")
    receipt_call_ids = (
        list(receipt.get("provider_call_ids") or [])
        if isinstance(receipt, Mapping)
        else []
    )
    old_provider_keys = {
        str(key)
        for key in old_item
        if str(key).startswith(PROVIDER_CALL_ATTR_PREFIX)
    }
    new_provider_keys = {
        str(key)
        for key in new_item
        if str(key).startswith(PROVIDER_CALL_ATTR_PREFIX)
    }
    old_provider_attrs = {
        key: old_item.get(key)
        for key in old_provider_keys
        if isinstance(old_item.get(key), Mapping)
    }
    new_provider_attrs = {
        key: new_item.get(key)
        for key in new_provider_keys
        if isinstance(new_item.get(key), Mapping)
    }
    old_call_ids = {
        str(value.get("call_id") or "")
        for value in old_provider_attrs.values()
    }
    new_call_ids = {
        str(value.get("call_id") or "")
        for value in new_provider_attrs.values()
    }
    try:
        provider_keys_canonical = all(
            key == _provider_call_attr(str(value.get("call_id") or ""))
            for key, value in new_provider_attrs.items()
        )
    except ValueError:
        provider_keys_canonical = False
    retained_calls_valid = bool(
        len(old_provider_attrs) == len(old_provider_keys)
        and len(new_provider_attrs) == len(new_provider_keys)
        and old_provider_keys == new_provider_keys
        and old_provider_attrs == new_provider_attrs
        and old_call_ids == new_call_ids == set(receipt_call_ids)
        and "" not in old_call_ids
        and provider_keys_canonical
        and all(
            str(value.get("status") or "") != "dispatching"
            for value in new_provider_attrs.values()
        )
    )
    return bool(
        repo
        and repo == str(old_item.get("repo") or "")
        and pr_number == old_pr_number
        and old_head
        and new_head
        and old_head != new_head
        and old_run
        and old_count == 0
        and new_count == 1
        and new_run == head_successor_run_id(repo, pr_number, new_head)
        and isinstance(receipt, Mapping)
        and int(receipt.get("schema_version") or 0) == 1
        and str(receipt.get("kind") or "") == "head_predecessor"
        and str(receipt.get("outcome") or "") == "SUPERSEDED"
        and str(receipt.get("predecessor_head_sha") or "") == old_head
        and str(receipt.get("predecessor_run_id") or "") == old_run
        and str(receipt.get("successor_head_sha") or "") == new_head
        and str(receipt.get("successor_run_id") or "") == new_run
        and int(receipt.get("attempt") or 0)
        == int(old_item.get("attempt") or 0)
        and int(receipt.get("context_attempt") or 0)
        == int(old_item.get("context_attempt") or 0)
        and int(receipt.get("review_attempt") or 0)
        == int(old_item.get("review_attempt") or 0)
        and int(new_item.get("attempt") or 0)
        == int(old_item.get("attempt") or 0)
        and int(new_item.get("context_attempt") or 0)
        == int(old_item.get("context_attempt") or 0)
        and int(new_item.get("review_attempt") or 0)
        == int(old_item.get("review_attempt") or 0)
        and int(receipt.get("provider_call_count") or 0)
        == len(receipt_call_ids)
        and len(receipt_call_ids) == len(set(receipt_call_ids))
        and receipt.get("provider_calls_retained_on_item") is True
        and receipt.get("provider_calls_terminal") is True
        and retained_calls_valid
        and "initial_admission" not in new_item
        and "context_claim" not in new_item
        and "review_claim" not in new_item
    )


def _estimate_item_bytes(*parts: Any) -> int:
    return sum(len(_canonical_json(part)) for part in parts)


def store_context(
    repo: str,
    pr_number: int,
    *,
    context_text: str,
    pr_details_text: str,
    meta: Dict[str, Any],
    expected_status: str = "PENDING",
    review_mode: Optional[str] = None,
    extra_attrs: Optional[Dict[str, Any]] = None,
    context_runtime_identity: Optional[Dict[str, Any]] = None,
    phase_claim: Optional[Mapping[str, Any]] = None,
    head_sha: str = "",
    run_id: str = "",
    table=None,
    s3_client=None,
) -> Tuple[bool, Dict[str, Any]]:
    bucket = str(config.RUN_ARTIFACT_BUCKET or config.CONTEXT_S3_BUCKET or "")
    attrs: Dict[str, Any]
    if bucket:
        resolved_run_id = run_id or uuid.uuid4().hex
        bundle = {
            "schema_version": str(config.RUN_ARTIFACT_SCHEMA_VERSION),
            "kind": "context_bundle",
            "repo": repo,
            "pr_number": int(pr_number),
            "head_sha": head_sha,
            "run_id": resolved_run_id,
            "context_text": context_text or "",
            "pr_details_text": pr_details_text or "",
            "context_meta": meta or {},
            "context_runtime_identity": (
                context_runtime_identity or {}
            ),
        }
        pointer = _put_json_artifact(
            bundle,
            bucket=bucket,
            key=_artifact_key(
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                run_id=resolved_run_id,
                kind="context",
            ),
            kind="context_bundle",
            s3_client=s3_client,
        )
        attrs = {
            "context_codec": "s3-json-gzip-v1",
            "context_artifact": pointer,
            "context_meta": _compact_context_meta(meta),
            "context_artifact_complete": True,
        }
    else:
        context_blob = gzip_b64(context_text)
        pr_details_blob = gzip_b64(pr_details_text)
        total = _estimate_item_bytes(context_blob, pr_details_blob, meta)
        if total > config.MAX_CONTEXT_ITEM_BYTES:
            raise ArtifactIntegrityError(
                f"Complete context requires {total} inline bytes but no RUN_ARTIFACT_BUCKET is configured"
            )
        attrs = {
            "context_codec": "gzip-b64",
            "context_blob": context_blob,
            "pr_details_blob": pr_details_blob,
            "context_meta": meta,
            "context_artifact_complete": True,
        }
    if review_mode:
        attrs["review_mode"] = review_mode
    if context_runtime_identity is not None:
        attrs["context_runtime_identity"] = context_runtime_identity
    if extra_attrs:
        attrs.update(extra_attrs)
    ok = update_status(
        repo,
        pr_number,
        expected_status=expected_status,
        next_status="CONTEXT_READY",
        attributes=attrs,
        phase_claim=phase_claim,
        table=table,
    )
    return ok, attrs


def _compact_context_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Keep operationally useful facts in DynamoDB; full metadata stays in S3."""
    allowed = (
        "review_mode",
        "context_strategy",
        "finish_reason",
        "pfr_rounds",
        "files",
        "searches",
        "reads",
        "known_gap_count",
        "tool_calls",
        "total_tokens",
        "elapsed_seconds",
        "fetch_health",
        "pfr_terminal_reconcile_round",
        "pfr_terminal_reconcile_trigger",
        "pfr_post_terminal_tool_call_count",
        "pfr_sweep_hit_count",
        "pfr_evidence_index_event_count",
        "pfr_evidence_index_complete",
        "pfr_evidence_binding_failure_count",
        "pfr_fetch_degradation_reason_counts",
    )
    compact = {key: meta[key] for key in allowed if key in (meta or {})}
    planning_gap = (meta or {}).get("planning_coverage_gap")
    if isinstance(planning_gap, dict):
        compact["planning_coverage_gap"] = {
            "status": str(planning_gap.get("status") or "gap"),
            "reason_kinds": [
                str(item)
                for item in planning_gap.get("reason_kinds") or []
                if isinstance(item, str)
            ][:8],
            "route_risk_domain_count": int(
                planning_gap.get("route_risk_domain_count") or 0
            ),
            "covered_route_risk_domain_count": int(
                planning_gap.get("covered_route_risk_domain_count") or 0
            ),
            "critical_step_dropped_cap_count": int(
                planning_gap.get("critical_step_dropped_cap_count") or 0
            ),
            "critical_step_budget_skipped_count": int(
                planning_gap.get("critical_step_budget_skipped_count") or 0
            ),
        }
    ci_snapshot = (meta or {}).get("ci_snapshot")
    if isinstance(ci_snapshot, dict):
        actionable_details = ci_snapshot.get("actionable_detail_retrieval")
        if not isinstance(actionable_details, dict):
            actionable_details = {}
        compact["ci_snapshot"] = {
            "schema_version": ci_snapshot.get("schema_version"),
            "has_ci": bool(ci_snapshot.get("has_ci")),
            "commit_status_state": ci_snapshot.get("commit_status_state") or "",
            "aggregate_classification": ci_snapshot.get("aggregate_classification") or "none",
            "check_count": len(ci_snapshot.get("checks") or []),
            "blocking_count": len(ci_snapshot.get("blocking_checks") or []),
            "action_required_count": len(ci_snapshot.get("action_required_checks") or []),
            "pending_count": len(ci_snapshot.get("pending_checks") or []),
            "incomplete_count": len(ci_snapshot.get("incomplete_checks") or []),
            "actionable_detail_outcome": actionable_details.get("outcome") or "no_hit",
            "actionable_detail_attempted_count": int(
                actionable_details.get("attempted_check_count") or 0
            ),
            "actionable_detail_enriched_count": int(
                actionable_details.get("enriched_check_count") or 0
            ),
            "actionable_detail_unmatched_count": int(
                actionable_details.get("unmatched_actionable_check_count") or 0
            ),
            "annotation_count": int(actionable_details.get("annotation_count") or 0),
            "annotation_available_count": int(
                actionable_details.get("annotation_available_count") or 0
            ),
            "annotation_omitted_count": int(
                actionable_details.get("annotation_omitted_count") or 0
            ),
            "annotation_truncated_check_count": int(
                actionable_details.get("truncated_check_count") or 0
            ),
            "actionable_detail_error_count": int(
                actionable_details.get("error_count") or 0
            ),
        }
    return compact


def load_context_bundle_from_item(
    item: Dict[str, Any], *, s3_client=None
) -> Tuple[str, str, Dict[str, Any]]:
    expected_head = str(item.get("head_sha") or "")

    def exact_head_meta(raw_meta: Any) -> Dict[str, Any]:
        meta = dict(raw_meta or {})
        stored_head = str(meta.get("head_sha") or "")
        if stored_head and expected_head and stored_head != expected_head:
            raise ArtifactIntegrityError(
                "Context metadata head does not match the queued item"
            )
        if expected_head:
            meta["head_sha"] = expected_head
        return meta

    codec = item.get("context_codec", "gzip-b64")
    if codec == "s3-json-gzip-v1":
        pointer = item.get("context_artifact")
        if not isinstance(pointer, dict):
            raise ArtifactIntegrityError("Context artifact pointer is missing")
        bundle = _read_json_artifact(pointer, s3_client=s3_client)
        if bundle.get("kind") != "context_bundle":
            raise ArtifactIntegrityError("Context artifact has the wrong kind")
        _validate_bundle_identity(bundle, item)
        return (
            str(bundle.get("context_text") or ""),
            str(bundle.get("pr_details_text") or ""),
            exact_head_meta(bundle.get("context_meta")),
        )

    pr_details = gunzip_b64(item.get("pr_details_blob", ""))
    if codec == "s3":
        bucket = config.CONTEXT_S3_BUCKET
        key = item["context_s3_key"]
        s3_client = s3_client or get_s3_client()
        response = s3_client.get_object(Bucket=bucket, Key=key)
        context = gzip.decompress(response["Body"].read()).decode("utf-8")
        return context, pr_details, exact_head_meta(item.get("context_meta"))
    return (
        gunzip_b64(item.get("context_blob", "")),
        pr_details,
        exact_head_meta(item.get("context_meta")),
    )


def load_context_from_item(item: Dict[str, Any], *, s3_client=None) -> Tuple[str, str]:
    context, pr_details, _meta = load_context_bundle_from_item(item, s3_client=s3_client)
    return context, pr_details


def _artifact_to_gzip_bytes(artifact: Dict[str, Any]) -> bytes:
    return _gzip_json_bytes(artifact)


_PRIVATE_REASONING_KEYS = {
    "_deep_thinking_result",
    "chain_of_thought",
    "deepseek_trace",
    "full_trace",
    "reasoning_content",
    "thinking_trace",
}


def _assert_artifact_safe(value: Any, *, path: str = "artifact") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _PRIVATE_REASONING_KEYS:
                raise ArtifactIntegrityError(f"Private reasoning field is forbidden at {path}.{key}")
            _assert_artifact_safe(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_artifact_safe(child, path=f"{path}[{index}]")


def _review_artifact_attrs(
    repo: str,
    pr_number: int,
    artifact: Dict[str, Any],
    *,
    head_sha: str = "",
    run_id: str = "",
    require_main_comment: bool = True,
    s3_client=None,
) -> Dict[str, Any]:
    if not isinstance(artifact, dict):
        raise ArtifactIntegrityError("A complete review artifact must be an object")
    if require_main_comment and not str(artifact.get("main_comment") or "").strip():
        raise ArtifactIntegrityError("A complete review artifact requires a non-empty main_comment")
    _assert_artifact_safe(artifact)
    payload = _artifact_to_gzip_bytes(artifact)
    encoded = base64.b64encode(payload).decode("ascii")
    bucket = str(config.RUN_ARTIFACT_BUCKET or config.CONTEXT_S3_BUCKET or "")
    if bucket:
        resolved_run_id = run_id or uuid.uuid4().hex
        wrapper = {
            "schema_version": str(config.RUN_ARTIFACT_SCHEMA_VERSION),
            "kind": "review_artifact",
            "repo": repo,
            "pr_number": int(pr_number),
            "head_sha": head_sha or str(artifact.get("head_sha") or ""),
            "run_id": resolved_run_id,
            "artifact": artifact,
        }
        pointer = _put_json_artifact(
            wrapper,
            bucket=bucket,
            key=_artifact_key(
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha or str(artifact.get("head_sha") or ""),
                run_id=resolved_run_id,
                kind="review",
            ),
            kind="review_artifact",
            s3_client=s3_client,
        )
        return {
            "review_artifact_codec": "s3-json-gzip-v1",
            "review_artifact": pointer,
            "review_artifact_s3_key": pointer["key"],
            "review_artifact_sha256": pointer["sha256"],
            "review_artifact_schema_version": pointer["schema_version"],
            "review_artifact_complete": True,
        }

    if _estimate_item_bytes(encoded) > config.MAX_CONTEXT_ITEM_BYTES:
        raise ArtifactIntegrityError(
            "Complete review artifact exceeds the inline limit and no RUN_ARTIFACT_BUCKET is configured"
        )
    return {
        "review_artifact_codec": "gzip-b64",
        "review_artifact_blob": encoded,
        "review_artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "review_artifact_schema_version": str(config.RUN_ARTIFACT_SCHEMA_VERSION),
        "review_artifact_complete": True,
    }


def load_review_artifact_from_item(item: Dict[str, Any], *, s3_client=None) -> Optional[Dict[str, Any]]:
    codec = item.get("review_artifact_codec")
    if not codec and item.get("review_artifact_blob"):
        codec = "gzip-b64"
    if codec == "s3-json-gzip-v1":
        pointer = item.get("review_artifact")
        if not isinstance(pointer, dict):
            raise ArtifactIntegrityError("Review artifact pointer is missing")
        wrapper = _read_json_artifact(pointer, s3_client=s3_client)
        if wrapper.get("kind") != "review_artifact" or not isinstance(wrapper.get("artifact"), dict):
            raise ArtifactIntegrityError("Review artifact wrapper is incomplete")
        _validate_bundle_identity(wrapper, item)
        return wrapper["artifact"]
    if codec == "s3":
        bucket = config.CONTEXT_S3_BUCKET
        key = item["review_artifact_s3_key"]
        s3_client = s3_client or get_s3_client()
        response = s3_client.get_object(Bucket=bucket, Key=key)
        return json.loads(gzip.decompress(response["Body"].read()).decode("utf-8"))
    if codec in {"gzip-b64", "gzip-b64-truncated"}:
        encoded = item.get("review_artifact_blob", "")
        if codec == "gzip-b64" and item.get("review_artifact_sha256"):
            payload = base64.b64decode(str(encoded).encode("ascii"))
            if hashlib.sha256(payload).hexdigest() != str(item["review_artifact_sha256"]):
                raise ArtifactIntegrityError("Inline review artifact checksum mismatch")
        return gunzip_json_b64(encoded)
    return None


def store_review_result(
    repo: str,
    pr_number: int,
    *,
    expected_status: str,
    dry_run: bool,
    review_comment: str,
    artifact: Dict[str, Any],
    review_mode: Optional[str] = None,
    extra_attrs: Optional[Dict[str, Any]] = None,
    phase_claim: Optional[Mapping[str, Any]] = None,
    head_sha: str = "",
    run_id: str = "",
    expected_publication_intent: Optional[Mapping[str, Any]] = None,
    table=None,
    s3_client=None,
) -> bool:
    next_status = "PROCESSED_DRYRUN" if dry_run else "PROCESSED"
    attrs: Dict[str, Any] = {"review_comment": (review_comment or "")[:3500]}
    if review_mode:
        attrs["review_mode"] = review_mode
    if extra_attrs:
        attrs.update(extra_attrs)
    must_persist_artifact = bool(dry_run or config.PERSIST_REVIEW_ARTIFACT)
    if must_persist_artifact:
        attrs.update(
            _review_artifact_attrs(
                repo,
                pr_number,
                artifact,
                head_sha=head_sha,
                run_id=run_id,
                s3_client=s3_client,
            )
        )
    attrs["review_artifact_persisted"] = must_persist_artifact
    return update_status(
        repo,
        pr_number,
        expected_status=expected_status,
        next_status=next_status,
        attributes=attrs,
        phase_claim=phase_claim,
        expected_attributes=(
            {"publication_intent": expected_publication_intent}
            if expected_publication_intent is not None
            else None
        ),
        expected_missing_attributes=(
            ("publication_receipt",)
            if expected_publication_intent is not None
            else ()
        ),
        table=table,
    )

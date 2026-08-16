"""Immutable publication candidate storage and recovery binding.

This module owns the exact prepared request artifact and the invariants that
bind it to one repository, pull request, head, run, stream event and phase
claim.  It has no GitHub surface or transaction-coordination responsibilities.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Dict

from .. import persistence
from ..errors import PublicationIntegrityFailure, PublicationStateConflict
from .publish import (
    GITHUB_REVIEW_COMMENT_FIELDS,
    PUBLICATION_KIND_DISPOSITIONS,
    PreparedGitHubReview,
)


PUBLICATION_SCHEMA_VERSION = 2
LEGACY_PUBLICATION_SCHEMA_VERSION = 1


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_candidate(
    prepared: PreparedGitHubReview,
    *,
    repo: str,
    pr_number: int,
    run_id: str,
    phase: str,
    owner_event_id: str,
    owner_request_id: str,
    publication_generation_attempt: int,
    preflight_completed_at: str,
    generation_runtime_identity: Mapping[str, Any],
    terminal_attributes: Mapping[str, Any],
    publication_key: str = "",
) -> Dict[str, Any]:
    key = str(publication_key or secrets.token_hex(16))
    if len(key) < 32:
        raise ValueError("publication_key must contain at least 128 bits")
    candidate = {
        "publication_schema_version": PUBLICATION_SCHEMA_VERSION,
        "kind": "publication_candidate",
        "repo": str(repo),
        "pr_number": int(pr_number),
        "head_sha": prepared.head_sha,
        "publication_kind": prepared.publication_kind,
        "required_disposition": prepared.required_disposition,
        "run_id": str(run_id),
        "phase": str(phase),
        "owner_event_id": str(owner_event_id),
        "owner_request_id": str(owner_request_id),
        "publication_generation_phase": str(phase),
        "publication_generation_attempt": int(
            publication_generation_attempt
        ),
        "publication_key": key,
        "payload_sha256": prepared.payload_sha256,
        "main_body_sha256": prepared.main_body_sha256,
        "comments_sha256": _canonical_sha256(
            [dict(comment) for comment in prepared.comments]
        ),
        "preflight_completed_at": str(preflight_completed_at),
        "generation_runtime_identity": deepcopy(
            dict(generation_runtime_identity)
        ),
        "github_request": prepared.request_payload(),
        "review_artifact": deepcopy(prepared.artifact),
        "terminal_attributes": {
            **deepcopy(dict(terminal_attributes)),
            "publication_kind": prepared.publication_kind,
            "required_disposition": prepared.required_disposition,
        },
    }
    if phase == "review":
        candidate["review_generation_attempt"] = int(
            publication_generation_attempt
        )
    return candidate


def persist_prepared_intent(
    candidate: Mapping[str, Any],
    *,
    expected_status: str,
    phase_claim: Mapping[str, Any],
    table=None,
    s3_client=None,
) -> Dict[str, Any]:
    if (
        str(candidate.get("phase") or "")
        != str(phase_claim.get("phase") or "")
        or not str(candidate.get("owner_event_id") or "")
        or str(candidate.get("owner_event_id") or "")
        != str(phase_claim.get("stream_event_id") or "")
        or str(candidate.get("owner_request_id") or "")
        != str(phase_claim.get("owner_id") or "")
        or int(candidate.get("publication_generation_attempt") or 0)
        != int(phase_claim.get("attempt") or 0)
    ):
        raise PublicationStateConflict(
            "Publication candidate is not bound to the active generation claim.",
            stage="publication.intent",
        )
    pointer = persistence.store_publication_candidate(
        dict(candidate),
        repo=str(candidate.get("repo") or ""),
        pr_number=int(candidate.get("pr_number") or 0),
        head_sha=str(candidate.get("head_sha") or ""),
        run_id=str(candidate.get("run_id") or ""),
        publication_generation_phase=str(
            candidate.get("publication_generation_phase") or ""
        ),
        publication_generation_attempt=int(
            candidate.get("publication_generation_attempt") or 0
        ),
        s3_client=s3_client,
    )
    intent = {
        "schema_version": PUBLICATION_SCHEMA_VERSION,
        "publication_key": str(
            candidate.get("publication_key") or ""
        ),
        "state": "prepared",
        "repo": str(candidate.get("repo") or ""),
        "pr_number": int(candidate.get("pr_number") or 0),
        "phase": str(candidate.get("phase") or ""),
        "owner_event_id": str(
            candidate.get("owner_event_id") or ""
        ),
        "owner_request_id": str(
            candidate.get("owner_request_id") or ""
        ),
        "run_id": str(candidate.get("run_id") or ""),
        "head_sha": str(candidate.get("head_sha") or ""),
        "publication_kind": str(
            candidate.get("publication_kind") or ""
        ),
        "required_disposition": str(
            candidate.get("required_disposition") or ""
        ),
        "publication_generation_phase": str(
            candidate.get("publication_generation_phase") or ""
        ),
        "publication_generation_attempt": int(
            candidate.get("publication_generation_attempt") or 0
        ),
        "publication_attempt": 0,
        "publication_recovery_attempt": 0,
        "payload_sha256": str(
            candidate.get("payload_sha256") or ""
        ),
        "main_body_sha256": str(
            candidate.get("main_body_sha256") or ""
        ),
        "comments_sha256": str(
            candidate.get("comments_sha256") or ""
        ),
        "candidate_artifact": pointer,
        "candidate_artifact_sha256": str(
            pointer.get("sha256") or ""
        ),
        "preflight_completed_at": str(
            candidate.get("preflight_completed_at") or ""
        ),
        "prepared_at": persistence.iso_now(),
        "generation_runtime_identity": deepcopy(
            candidate.get("generation_runtime_identity") or {}
        ),
    }
    if intent["publication_generation_phase"] == "review":
        intent["review_generation_attempt"] = int(
            intent["publication_generation_attempt"]
        )
    stored = persistence.store_publication_intent(
        str(candidate.get("repo") or ""),
        int(candidate.get("pr_number") or 0),
        expected_status=expected_status,
        head_sha=str(candidate.get("head_sha") or ""),
        intent=intent,
        phase_claim=phase_claim,
        table=table,
    )
    if not stored:
        raise PublicationStateConflict(
            "Prepared publication intent lost its owner-bound write.",
            stage="publication.intent",
        )
    return intent


def load_candidate(
    intent: Mapping[str, Any],
    *,
    s3_client=None,
) -> Dict[str, Any]:
    candidate = persistence.load_publication_candidate(
        intent,
        s3_client=s3_client,
    )
    try:
        schema_version = int(candidate.get("publication_schema_version") or 0)
        intent_schema_version = int(intent.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise PublicationIntegrityFailure(
            "Publication candidate schema version is invalid.",
            stage="publication.candidate",
        ) from exc
    if schema_version != intent_schema_version:
        raise PublicationIntegrityFailure(
            "Publication candidate schema does not match its intent.",
            stage="publication.candidate",
        )
    candidate = deepcopy(candidate)
    if schema_version == LEGACY_PUBLICATION_SCHEMA_VERSION:
        lifecycle_fields = ("publication_kind", "required_disposition")
        if any(
            field in candidate or field in intent
            for field in lifecycle_fields
        ):
            raise PublicationIntegrityFailure(
                "Legacy publication lifecycle binding is ambiguous.",
                stage="publication.candidate",
            )
        candidate["publication_kind"] = "ordinary_review"
        candidate["required_disposition"] = "open_same_head"
    elif schema_version != PUBLICATION_SCHEMA_VERSION:
        raise PublicationIntegrityFailure(
            "Publication candidate schema version is unsupported.",
            stage="publication.candidate",
        )
    publication_kind = str(candidate.get("publication_kind") or "")
    required_disposition = str(candidate.get("required_disposition") or "")
    if schema_version == PUBLICATION_SCHEMA_VERSION and (
        publication_kind != str(intent.get("publication_kind") or "")
        or required_disposition
        != str(intent.get("required_disposition") or "")
    ):
        raise PublicationIntegrityFailure(
            "Publication candidate lifecycle binding does not match its intent.",
            stage="publication.candidate",
        )
    if required_disposition not in PUBLICATION_KIND_DISPOSITIONS.get(
        publication_kind,
        (),
    ):
        raise PublicationIntegrityFailure(
            "Publication candidate lifecycle binding is invalid.",
            stage="publication.candidate",
        )
    request = candidate.get("github_request")
    if not isinstance(request, Mapping):
        raise PublicationIntegrityFailure(
            "Publication candidate GitHub request is missing.",
            stage="publication.candidate",
        )
    expected_payload = {
        "head_sha": str(candidate.get("head_sha") or ""),
        "body": str(request.get("body") or ""),
        "event": str(request.get("event") or ""),
        "comments": request.get("comments") or [],
    }
    if (
        _canonical_sha256(expected_payload)
        != str(candidate.get("payload_sha256") or "")
        or hashlib.sha256(
            expected_payload["body"].encode("utf-8")
        ).hexdigest()
        != str(candidate.get("main_body_sha256") or "")
        or _canonical_sha256(expected_payload["comments"])
        != str(candidate.get("comments_sha256") or "")
    ):
        raise PublicationIntegrityFailure(
            "Publication candidate request digest is invalid.",
            stage="publication.candidate",
        )
    return candidate


def prepared_from_candidate(
    candidate: Mapping[str, Any],
) -> PreparedGitHubReview:
    request = candidate.get("github_request")
    if not isinstance(request, Mapping):
        raise PublicationIntegrityFailure(
            "Publication candidate request is missing.",
            stage="publication.candidate",
        )
    comments = request.get("comments")
    if not isinstance(comments, list) or not all(
        isinstance(comment, Mapping) for comment in comments
    ):
        raise PublicationIntegrityFailure(
            "Publication candidate comments are invalid.",
            stage="publication.candidate",
        )
    allowed_comments = tuple(
        {
            field: comment[field]
            for field in GITHUB_REVIEW_COMMENT_FIELDS
            if field in comment
        }
        for comment in comments
    )
    return PreparedGitHubReview(
        head_sha=str(candidate.get("head_sha") or ""),
        main_body=str(request.get("body") or ""),
        comments=allowed_comments,
        artifact=deepcopy(candidate.get("review_artifact") or {}),
        publication_kind=str(candidate.get("publication_kind") or ""),
        required_disposition=str(
            candidate.get("required_disposition") or ""
        ),
    )


def validate_recovery_binding(
    *,
    current_item: Mapping[str, Any],
    intent: Mapping[str, Any],
    candidate: Mapping[str, Any],
    expected_status: str,
    phase_claim: Mapping[str, Any],
) -> None:
    """Fail closed unless the current item, claim, intent and artifact agree."""

    repo = str(candidate.get("repo") or "")
    pr_number = int(candidate.get("pr_number") or 0)
    phase = str(candidate.get("phase") or "")
    event_id = str(candidate.get("owner_event_id") or "")
    claim_owner = str(phase_claim.get("owner_id") or "")
    claim_attempt = int(phase_claim.get("attempt") or 0)
    current_claim = current_item.get(f"{phase}_claim")
    current_run_id = str(
        current_item.get("run_id")
        or current_item.get("delivery_id")
        or f"{repo.replace('/', '_')}_{pr_number}"
    )
    expected = {
        "repo": repo,
        "pr_number": pr_number,
        "status": str(expected_status),
        "head_sha": str(candidate.get("head_sha") or ""),
    }
    for field, value in expected.items():
        actual = current_item.get(field)
        if field == "pr_number":
            actual = int(actual or 0)
        else:
            actual = str(actual or "")
        if actual != value:
            raise PublicationStateConflict(
                f"Current item does not match publication field {field}.",
                stage="publication.recovery_binding",
            )
    if current_run_id != str(candidate.get("run_id") or ""):
        raise PublicationStateConflict(
            "Current item does not match publication run identity.",
            stage="publication.recovery_binding",
        )
    if current_item.get("publication_intent") != dict(intent):
        raise PublicationStateConflict(
            "Current item publication intent changed before recovery.",
            stage="publication.recovery_binding",
        )
    if (
        phase not in {"context", "review"}
        or str(phase_claim.get("phase") or "") != phase
        or not event_id
        or str(phase_claim.get("stream_event_id") or "") != event_id
        or not claim_owner
        or claim_attempt <= 0
        or int(current_item.get(f"{phase}_attempt") or 0)
        != claim_attempt
        or not isinstance(current_claim, Mapping)
        or str(current_claim.get("owner_id") or "") != claim_owner
        or str(current_claim.get("stream_event_id") or "") != event_id
    ):
        raise PublicationStateConflict(
            "Current phase claim is not bound to the publication intent.",
            stage="publication.recovery_binding",
        )
    effective_intent = dict(intent)
    if int(candidate.get("publication_schema_version") or 0) == 1:
        effective_intent.update(
            {
                "publication_kind": "ordinary_review",
                "required_disposition": "open_same_head",
            }
        )
    bound_fields = (
        "repo",
        "pr_number",
        "phase",
        "owner_event_id",
        "head_sha",
        "publication_kind",
        "required_disposition",
        "run_id",
        "publication_key",
        "publication_generation_phase",
        "publication_generation_attempt",
        "payload_sha256",
        "main_body_sha256",
        "comments_sha256",
        "preflight_completed_at",
    )
    for field in bound_fields:
        if candidate.get(field) != effective_intent.get(field):
            raise PublicationIntegrityFailure(
                f"Publication artifact does not match intent field {field}.",
                stage="publication.recovery_binding",
            )

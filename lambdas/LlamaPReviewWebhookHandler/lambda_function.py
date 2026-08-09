"""Signed GitHub webhook admission for the hosted public-repository path.

The privacy boundary is intentionally first: after signature verification the
handler reads only the event kind and repository visibility.  A private event
is acknowledged before any product identity is built, logged, or persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
_pipeline_table_name = os.environ.get("DYNAMODB_PIPELINE_TABLE")
table = dynamodb.Table(_pipeline_table_name) if _pipeline_table_name else None


class WebhookConfigurationError(RuntimeError):
    """The one hosted admission path is not configured safely."""


def _emit_webhook_error_metric() -> None:
    """Emit a content-free application error metric before returning 500."""

    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "LlamaPReview/Webhook",
                            "Dimensions": [[]],
                            "Metrics": [{"Name": "Errors", "Unit": "Count"}],
                        }
                    ],
                },
                "Errors": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ttl_epoch(days: int = 30) -> int:
    return int(time.time()) + days * 24 * 3600


def create_response(status_code: int, message: str) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "body": json.dumps({"message": message}),
        "headers": {"Content-Type": "application/json"},
    }


def _headers(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key).lower(): value
        for key, value in (event.get("headers") or {}).items()
    }


def verify_signature(event: Mapping[str, Any]) -> bool:
    received_signature = _headers(event).get("x-hub-signature-256", "")
    if not received_signature:
        return False
    secret_text = os.environ.get("GITHUB_WEBHOOK_SECRET") or ""
    if not secret_text.strip():
        logger.error("GITHUB_WEBHOOK_SECRET is missing or empty")
        return False
    body = str(event.get("body") or "").encode("utf-8")
    expected_signature = "sha256=" + hmac.new(
        secret_text.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(str(received_signature), expected_signature)


def _repository_is_private(payload: Mapping[str, Any]) -> bool:
    repository = payload.get("repository")
    return isinstance(repository, Mapping) and repository.get("private") is True


def should_process_pull_request(payload: Mapping[str, Any]) -> bool:
    action = payload.get("action")
    pull_request = payload.get("pull_request") or {}
    return (
        (
            (action == "opened" and not bool(pull_request.get("draft")))
            or action == "ready_for_review"
        )
        and pull_request.get("state") == "open"
    )


def validate_public_pull_request_payload(payload: Mapping[str, Any]) -> None:
    pull_request = payload.get("pull_request") or {}
    repository = payload.get("repository") or {}
    if repository.get("private") is not False:
        raise ValueError("Pull request repository visibility is not public")
    required = {
        "repository full name": repository.get("full_name"),
        "pull request number": pull_request.get("number"),
        "installation id": (payload.get("installation") or {}).get("id"),
        "head SHA": (pull_request.get("head") or {}).get("sha"),
    }
    missing = [name for name, value in required.items() if value in (None, "")]
    if missing:
        raise ValueError("Public pull request payload is missing required fields")


def build_pipeline_item(
    payload: Mapping[str, Any],
    *,
    delivery_id: str = "",
) -> dict[str, Any]:
    """Build the minimum durable identity needed by the public Pipeline."""

    validate_public_pull_request_payload(payload)
    pull_request = payload.get("pull_request") or {}
    repository = payload.get("repository") or {}
    head = pull_request.get("head") or {}
    base = pull_request.get("base") or {}
    now = iso_now()
    return {
        "repo": repository.get("full_name"),
        "pr_number": pull_request.get("number"),
        "status": "PENDING",
        "installation_id": (payload.get("installation") or {}).get("id"),
        "head_sha": head.get("sha"),
        "head_ref": head.get("ref"),
        "base_ref": base.get("ref"),
        "default_branch": repository.get("default_branch"),
        "pr_title": pull_request.get("title"),
        "delivery_id": delivery_id,
        "run_id": delivery_id or uuid.uuid4().hex,
        "created_at": now,
        "updated_at": now,
        "ttl_epoch": ttl_epoch(),
    }


def _put_once(target_table: Any, item: Mapping[str, Any]) -> bool:
    try:
        target_table.put_item(
            Item=dict(item),
            ConditionExpression=(
                "attribute_not_exists(repo) AND attribute_not_exists(pr_number)"
            ),
        )
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            logger.info(
                "Public pipeline item already exists repo=%s pr=%s",
                item.get("repo"),
                item.get("pr_number"),
            )
            return False
        raise


def save_to_dynamodb(item: Mapping[str, Any]) -> bool:
    if table is None:
        raise WebhookConfigurationError("DYNAMODB_PIPELINE_TABLE is unset")
    try:
        return _put_once(table, item)
    except Exception:
        logger.exception("Failed writing public pipeline webhook item")
        raise


def process_public_pull_request(
    payload: Mapping[str, Any],
    *,
    delivery_id: str = "",
) -> dict[str, Any] | None:
    """Admit one supported public pull request event exactly once."""

    if not should_process_pull_request(payload):
        logger.info("Ignored unsupported public pull request event")
        return None
    item = build_pipeline_item(payload, delivery_id=delivery_id)
    if not save_to_dynamodb(item):
        return None
    logger.info(
        "Saved public pipeline item repo=%s pr=%s status=PENDING",
        item["repo"],
        item["pr_number"],
    )
    return item


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    try:
        if not verify_signature(event):
            return create_response(401, "Invalid signature")

        payload = json.loads(str(event.get("body") or "{}"))
        if not isinstance(payload, Mapping):
            raise ValueError("Webhook payload must be an object")
        event_name = str(_headers(event).get("x-github-event") or "").strip().lower()

        # This branch must remain before any identity-bearing validation, log,
        # state construction, AWS write, provider work, or GitHub API call.
        if _repository_is_private(payload):
            return create_response(200, "Webhook accepted")

        if event_name != "pull_request":
            return create_response(200, "Ignored event")

        delivery_id = str(
            _headers(event).get("x-github-delivery") or ""
        ).strip()
        process_public_pull_request(payload, delivery_id=delivery_id)
        return create_response(200, "Webhook processed successfully")
    except Exception as exc:
        # Exception messages may contain untrusted payload fragments.  Only the
        # exception class crosses the operational log boundary.
        logger.error("Error processing webhook class=%s", exc.__class__.__name__)
        _emit_webhook_error_metric()
        return create_response(500, "Internal server error")

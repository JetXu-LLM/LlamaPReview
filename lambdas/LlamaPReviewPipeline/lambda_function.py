"""DynamoDB Stream entrypoint for the LlamaPReview 2026 pipeline."""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict

from . import orchestrator
from .errors import PhaseClaimUnavailable

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _decode_attr(attr: Dict[str, Any]) -> Any:
    if "S" in attr:
        return attr["S"]
    if "N" in attr:
        raw = attr["N"]
        return int(raw) if str(raw).isdigit() else float(raw)
    if "BOOL" in attr:
        return bool(attr["BOOL"])
    if "NULL" in attr:
        return None
    if "B" in attr:
        value = attr["B"]
        if isinstance(value, bytes):
            return value.decode("utf-8")
        try:
            return base64.b64decode(value).decode("utf-8")
        except Exception:
            return value
    if "M" in attr:
        return {key: _decode_attr(value) for key, value in attr["M"].items()}
    if "L" in attr:
        return [_decode_attr(value) for value in attr["L"]]
    return next(iter(attr.values())) if attr else None


def from_dynamodb_image(image: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return {key: _decode_attr(value) for key, value in (image or {}).items()}


def process_record(record: Dict[str, Any], *, lambda_context=None) -> None:
    if record.get("eventName") not in ("INSERT", "MODIFY"):
        return
    new_image = record.get("dynamodb", {}).get("NewImage", {})
    old_image = record.get("dynamodb", {}).get("OldImage", {})
    item = from_dynamodb_image(new_image)
    old = from_dynamodb_image(old_image)
    status = item.get("status")
    if status == old.get("status"):
        return
    stream_event_id = str(record.get("eventID") or "").strip()
    if status in {"PENDING", "CONTEXT_READY"} and not stream_event_id:
        raise PhaseClaimUnavailable(
            "Pipeline stream record is missing its stable event ID.",
            stage="lambda.stream_event_identity",
        )
    if status == "PENDING":
        orchestrator.run_context_phase(
            item,
            lambda_context=lambda_context,
            stream_event_id=stream_event_id,
        )
    elif status == "CONTEXT_READY":
        orchestrator.run_review_phase(
            item,
            lambda_context=lambda_context,
            stream_event_id=stream_event_id,
        )


def lambda_handler(event, context):
    for record in event.get("Records", []):
        process_record(record, lambda_context=context)
    return {"statusCode": 200, "body": "ok"}

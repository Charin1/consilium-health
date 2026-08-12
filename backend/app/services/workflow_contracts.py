"""
Shared workflow event and error contract helpers.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

EVENT_SCHEMA_VERSION = "1.0"


def utc_now() -> datetime:
    """Return current UTC time."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 format."""
    return utc_now().isoformat()


def new_trace_id() -> str:
    """Create a trace id for correlating workflow events."""
    return str(uuid4())


def build_error(
    code: str,
    message: str,
    *,
    retryable: bool,
    stage: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a normalized JSON error payload."""
    payload: Dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if stage:
        payload["stage"] = stage
    if details:
        payload["details"] = details
    return payload


def build_event(
    mission_id: str,
    event_type: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    source: str = "orchestrator",
    level: str = "info",
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build a normalized event envelope.

    Payload is kept in `payload` and also flattened at top level for backward
    compatibility with existing clients.
    """
    safe_payload = payload or {}

    event: Dict[str, Any] = {
        "type": event_type,
        "schema_version": EVENT_SCHEMA_VERSION,
        "mission_id": mission_id,
        "timestamp": utc_now_iso(),
        "source": source,
        "level": level,
        "trace_id": trace_id or new_trace_id(),
        "payload": safe_payload,
    }

    if isinstance(safe_payload, dict):
        for key, value in safe_payload.items():
            event.setdefault(key, value)

    return event


def serialize_datetimes(value: Any) -> Any:
    """Recursively convert date/datetime values to ISO-8601 strings."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: serialize_datetimes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize_datetimes(item) for item in value]
    return value

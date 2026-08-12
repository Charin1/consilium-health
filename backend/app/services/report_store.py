"""
Thread-safe in-memory stores for generated reports and downloadable assets.
"""
from __future__ import annotations

import copy
from threading import Lock
from typing import Any, Dict, List, Optional

from app.services.workflow_contracts import utc_now

_store_lock = Lock()

reports_store: Dict[str, Dict[str, Any]] = {}
report_files_store: Dict[str, bytes] = {}


def create_report_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Insert a report record and return a copy."""
    report_id = record["id"]
    with _store_lock:
        reports_store[report_id] = copy.deepcopy(record)
        return copy.deepcopy(reports_store[report_id])


def get_report_record(report_id: str) -> Optional[Dict[str, Any]]:
    """Get report by id."""
    with _store_lock:
        report = reports_store.get(report_id)
        return copy.deepcopy(report) if report else None


def list_report_records(*, mission_id: Optional[str] = None, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """List reports with optional mission/workspace filters."""
    with _store_lock:
        records = list(reports_store.values())

    if mission_id is not None:
        records = [record for record in records if record.get("mission_id") == mission_id]
    if workspace_id is not None:
        records = [record for record in records if record.get("workspace_id") == workspace_id]

    records.sort(key=lambda item: item.get("created_at"), reverse=True)
    return [copy.deepcopy(record) for record in records]


def update_report_record(report_id: str, **updates: Any) -> Optional[Dict[str, Any]]:
    """Update report record fields and return updated copy."""
    with _store_lock:
        report = reports_store.get(report_id)
        if report is None:
            return None

        report.update(updates)
        report["updated_at"] = utc_now()
        return copy.deepcopy(report)


def store_report_file(report_id: str, file_bytes: bytes) -> None:
    """Persist report file bytes in memory."""
    with _store_lock:
        report_files_store[report_id] = file_bytes


def get_report_file(report_id: str) -> Optional[bytes]:
    """Fetch report bytes by report id."""
    with _store_lock:
        file_bytes = report_files_store.get(report_id)
        return bytes(file_bytes) if file_bytes is not None else None

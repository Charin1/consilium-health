"""
Thread-safe mission store helpers.
"""
from __future__ import annotations

import copy
from threading import Lock
from typing import Any, Dict, List, Optional

from app.services.workflow_contracts import serialize_datetimes, utc_now

_store_lock = Lock()

# In-memory store (replace with DB in production)
missions_store: Dict[str, Dict[str, Any]] = {}


def create_mission_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Create and return a mission record."""
    mission_id = record["id"]
    with _store_lock:
        missions_store[mission_id] = copy.deepcopy(record)
        return copy.deepcopy(missions_store[mission_id])


def get_mission_record(mission_id: str) -> Optional[Dict[str, Any]]:
    """Fetch mission by id."""
    with _store_lock:
        mission = missions_store.get(mission_id)
        return copy.deepcopy(mission) if mission else None


def list_mission_records() -> List[Dict[str, Any]]:
    """Fetch all mission records."""
    with _store_lock:
        return [copy.deepcopy(mission) for mission in missions_store.values()]


def update_mission_record(mission_id: str, **updates: Any) -> Optional[Dict[str, Any]]:
    """Update mission fields and return updated mission."""
    with _store_lock:
        mission = missions_store.get(mission_id)
        if mission is None:
            return None

        mission.update(updates)
        mission["updated_at"] = utc_now()
        return copy.deepcopy(mission)


def bump_mission_counters(
    mission_id: str,
    *,
    completed_delta: int = 0,
    running_delta: int = 0,
) -> Optional[Dict[str, Any]]:
    """Increment/decrement mission task counters safely."""
    with _store_lock:
        mission = missions_store.get(mission_id)
        if mission is None:
            return None

        mission["tasks_completed"] = max(0, mission.get("tasks_completed", 0) + completed_delta)
        mission["tasks_running"] = max(0, mission.get("tasks_running", 0) + running_delta)
        mission["updated_at"] = utc_now()
        return copy.deepcopy(mission)


def clear_mission_error(mission_id: str) -> Optional[Dict[str, Any]]:
    """Clear the mission's last error without removing history."""
    with _store_lock:
        mission = missions_store.get(mission_id)
        if mission is None:
            return None

        mission["last_error"] = None
        mission["updated_at"] = utc_now()
        return copy.deepcopy(mission)


def register_mission_failure(mission_id: str, error: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Record a workflow error and move mission to failed state."""
    with _store_lock:
        mission = missions_store.get(mission_id)
        if mission is None:
            return None

        errors = mission.setdefault("errors", [])
        errors.append(error)
        mission["last_error"] = error
        mission["status"] = "failed"
        mission["tasks_running"] = 0
        mission["updated_at"] = utc_now()
        return copy.deepcopy(mission)


def prepare_mission_retry(mission_id: str) -> Optional[Dict[str, Any]]:
    """Prepare a failed mission for a retry run."""
    with _store_lock:
        mission = missions_store.get(mission_id)
        if mission is None:
            return None

        mission["status"] = "planning"
        mission["tasks_running"] = 0
        mission["last_error"] = None
        mission["retry_count"] = mission.get("retry_count", 0) + 1
        mission["updated_at"] = utc_now()
        return copy.deepcopy(mission)


def serialize_mission_record(mission: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize a mission dict for JSON transport."""
    return serialize_datetimes(copy.deepcopy(mission))

"""
Rounds API - polling and stopping an in-progress "Convene the board" debate.

Starting a round is a chat action (`POST /api/chat/sessions/{id}/convene-async`,
mirroring `assign-async`'s shape) because it needs the session and the opening
brief. Watching and stopping one, once it exists, does not need either -- a
`job_id` is enough, which is why those live in their own router rather than
nested under `/api/chat`.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import CurrentUser, require_roles
from app.services import round_service

router = APIRouter()


@router.get("/{job_id}")
async def get_round(
    job_id: str,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> Dict[str, Any]:
    job = round_service.get(job_id, workspace_id=user.workspace_id)
    if job is None:
        # 404 rather than 403 for another tenant's round: confirming it exists
        # but is not theirs is itself a disclosure (security.md, same rule
        # jobs.py follows for seat jobs).
        raise HTTPException(status_code=404, detail="Round not found")
    return job


@router.post("/{job_id}/stop")
async def stop_round(
    job_id: str,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> Dict[str, Any]:
    """
    Ask a running round to stop after the current speaker finishes.

    Not instant: a live model call is a blocking network request with nothing
    to interrupt inside it short of killing the thread. This flags the round;
    the graph's `route` node checks the flag once per turn, between speakers.
    """
    job = round_service.request_cancel(job_id, workspace_id=user.workspace_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Round not found")
    return job

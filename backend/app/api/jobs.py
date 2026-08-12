"""
Jobs API - what each seat is doing right now.

This is the floor view's data source. It is a read endpoint by design: work is
started through the domain endpoint that owns it (`/api/chat/sessions/{id}/
assign-async`), and watched here. A generic "run anything" job endpoint would
be an unauthenticated remote execution surface wearing a helpful name.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import CurrentUser, require_roles
from app.services import job_service

router = APIRouter()
logger = logging.getLogger("consilium.jobs.api")


@router.get("")
async def list_jobs(
    session_id: Optional[str] = Query(default=None),
    active_only: bool = Query(default=False, description="Only queued and running work."),
    limit: int = Query(default=100, ge=1, le=500),
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> Dict[str, Any]:
    """
    Recent jobs for this workspace, newest first.

    `active_only=true` is the poll the floor runs on a loop: the busy desks are
    the entire signal, and shipping every finished row to draw four of them is
    how a 2-second poll becomes expensive.
    """
    jobs = job_service.list_jobs(
        workspace_id=user.workspace_id,
        session_id=session_id,
        active_only=active_only,
        limit=limit,
    )
    return {
        "jobs": jobs,
        "active": sum(1 for j in jobs if not j["is_terminal"]),
        "by_seat": {j["seat_id"]: j for j in reversed(jobs)},
    }


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> Dict[str, Any]:
    job = job_service.get(job_id, workspace_id=user.workspace_id)
    if job is None:
        # 404 rather than 403 for another tenant's job: telling a caller that a
        # job exists but is not theirs is itself a disclosure (security.md).
        raise HTTPException(status_code=404, detail="Job not found")
    return job

"""
Missions API - mission lifecycle and workflow controls.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.services.mission_store import (
    create_mission_record,
    get_mission_record,
    list_mission_records,
    missions_store,
    prepare_mission_retry,
    update_mission_record,
)
from app.services.orchestrator import orchestrator_service
from app.services.workflow_contracts import utc_now

logger = logging.getLogger("consilium.missions")

router = APIRouter()

MissionStatus = Literal["planning", "active", "waiting_input", "paused", "completed", "failed"]


class WorkflowError(BaseModel):
    """Structured mission workflow error."""

    code: str
    message: str
    retryable: bool = True
    stage: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class MissionCreate(BaseModel):
    """Request model for creating a mission."""

    business: str = Field(..., min_length=3, max_length=500)
    metric: str = Field(..., min_length=3, max_length=80)
    goal: str = Field(..., min_length=3, max_length=300)

    @field_validator("business", "metric", "goal")
    @classmethod
    def strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field cannot be empty")
        return cleaned


class Mission(BaseModel):
    """Mission model."""

    id: str
    business: str
    metric: str
    goal: str
    progress: float = 0.0
    target_progress: float = 0.20
    status: MissionStatus = "planning"
    tasks_completed: int = 0
    tasks_running: int = 0
    errors: List[WorkflowError] = Field(default_factory=list)
    last_error: Optional[WorkflowError] = None
    retry_count: int = 0
    created_at: datetime
    updated_at: datetime


class MissionUpdate(BaseModel):
    """Request model for updating a mission."""

    status: Optional[MissionStatus] = None
    progress: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class DecisionRequest(BaseModel):
    """Decision checkpoint input payload."""

    choice: str = Field(..., min_length=1, max_length=120)

    @field_validator("choice")
    @classmethod
    def normalize_choice(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("choice cannot be empty")
        return cleaned


class MissionActionResponse(BaseModel):
    """Generic mission action response."""

    status: str
    mission_id: str


@router.post("", response_model=Mission)
async def create_mission(mission_data: MissionCreate, background_tasks: BackgroundTasks) -> Mission:
    """
    Create a new mission and queue background strategy generation.
    """
    mission_id = str(uuid4())
    now = utc_now()

    mission = Mission(
        id=mission_id,
        business=mission_data.business,
        metric=mission_data.metric,
        goal=mission_data.goal,
        created_at=now,
        updated_at=now,
    )

    create_mission_record(mission.model_dump())

    logger.info(
        "Mission created: id=%s metric=%s goal='%s'",
        mission_id[:8],
        mission_data.metric,
        mission_data.goal[:60],
    )

    background_tasks.add_task(
        orchestrator_service.generate_strategy,
        mission_id,
        mission_data.model_dump(),
    )

    return mission


@router.get("/{mission_id}", response_model=Mission)
async def get_mission(mission_id: str) -> Mission:
    """Get a mission by ID."""
    mission = get_mission_record(mission_id)
    if mission is None:
        logger.warning("Mission not found: %s", mission_id[:8])
        raise HTTPException(status_code=404, detail="Mission not found")
    return Mission(**mission)


@router.get("", response_model=List[Mission])
async def list_missions() -> List[Mission]:
    """List all missions."""
    logger.debug("Listing %d missions", len(missions_store))
    return [Mission(**record) for record in list_mission_records()]


@router.patch("/{mission_id}", response_model=Mission)
async def update_mission(mission_id: str, update: MissionUpdate) -> Mission:
    """Update mutable mission fields."""
    mission = get_mission_record(mission_id)
    if mission is None:
        logger.warning("Mission not found for update: %s", mission_id[:8])
        raise HTTPException(status_code=404, detail="Mission not found")

    updates: Dict[str, Any] = {}
    if update.status is not None:
        updates["status"] = update.status
    if update.progress is not None:
        updates["progress"] = update.progress

    if not updates:
        return Mission(**mission)

    updated_mission = update_mission_record(mission_id, **updates)
    if updated_mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    return Mission(**updated_mission)


@router.post("/{mission_id}/decision", response_model=MissionActionResponse)
async def submit_decision(
    mission_id: str,
    decision: DecisionRequest,
    background_tasks: BackgroundTasks,
) -> MissionActionResponse:
    """Submit a user decision for a pending checkpoint."""
    if get_mission_record(mission_id) is None:
        logger.warning("Mission not found for decision: %s", mission_id[:8])
        raise HTTPException(status_code=404, detail="Mission not found")

    logger.info("Decision received for %s: %s", mission_id[:8], decision.choice)

    background_tasks.add_task(
        orchestrator_service.process_decision,
        mission_id,
        decision.model_dump(),
    )

    return MissionActionResponse(status="decision_received", mission_id=mission_id)


@router.post("/{mission_id}/retry", response_model=MissionActionResponse)
async def retry_mission(mission_id: str, background_tasks: BackgroundTasks) -> MissionActionResponse:
    """Retry a failed mission workflow."""
    mission = get_mission_record(mission_id)
    if mission is None:
        logger.warning("Mission not found for retry: %s", mission_id[:8])
        raise HTTPException(status_code=404, detail="Mission not found")

    if mission.get("status") != "failed":
        raise HTTPException(status_code=409, detail="Mission is not in failed state")

    retry_mission_record = prepare_mission_retry(mission_id)
    if retry_mission_record is None:
        raise HTTPException(status_code=404, detail="Mission not found")

    logger.info("Mission retry queued for %s", mission_id[:8])
    background_tasks.add_task(orchestrator_service.retry_mission, mission_id)

    return MissionActionResponse(status="retry_queued", mission_id=mission_id)

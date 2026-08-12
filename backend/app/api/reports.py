"""
Reports API - async report generation, history, and downloadable exports.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from app.api.auth import CurrentUser, can_view_workspace_data, require_roles
from app.services.mission_store import get_mission_record
from app.services.report_service import report_service
from app.services.report_store import get_report_file, get_report_record

logger = logging.getLogger("consilium.reports.api")

router = APIRouter()

ReportStatus = Literal["queued", "running", "completed", "failed"]
ReportType = Literal["gtm_sales_strategy", "sales_execution_pack", "quarterly_growth_review"]


class ReportCreate(BaseModel):
    report_type: ReportType = "gtm_sales_strategy"
    mission_id: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=120)
    client_name: Optional[str] = Field(default=None, max_length=120)
    business_summary: Optional[str] = Field(default=None, max_length=1000)
    primary_goal: Optional[str] = Field(default=None, max_length=300)
    target_audience: str = Field(default="SMB buyers", min_length=3, max_length=300)
    offer_summary: str = Field(default="Consulting services", min_length=3, max_length=300)
    industry: Optional[str] = Field(default=None, max_length=120)
    time_horizon: str = Field(default="90 days", min_length=3, max_length=60)
    constraints: Optional[str] = Field(default=None, max_length=500)
    additional_context: Optional[str] = Field(default=None, max_length=1200)

    @field_validator("title", "client_name", "business_summary", "primary_goal", "industry", "constraints", "additional_context")
    @classmethod
    def clean_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ReportSection(BaseModel):
    heading: str
    content: str


class ReportKPI(BaseModel):
    name: str
    target: str
    timeline: str


class WorkflowError(BaseModel):
    code: str
    message: str
    retryable: bool
    stage: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class Report(BaseModel):
    id: str
    report_type: ReportType
    title: str
    status: ReportStatus
    mission_id: Optional[str] = None
    workspace_id: str
    created_by: str
    prompt_version: str
    file_name: str
    sections: List[ReportSection] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    kpis: List[ReportKPI] = Field(default_factory=list)
    error: Optional[WorkflowError] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


def _validate_workspace_access(report: Dict[str, Any], user: CurrentUser) -> None:
    if report.get("workspace_id") != user.workspace_id:
        raise HTTPException(status_code=404, detail="Report not found")


@router.post("", response_model=Report)
async def create_report(
    report_data: ReportCreate,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant")),
) -> Report:
    """Queue a new async report generation job."""
    payload = report_data.model_dump()

    mission_id = payload.get("mission_id")
    if mission_id:
        mission = get_mission_record(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail="Mission not found")
        payload["business_summary"] = payload.get("business_summary") or mission.get("business")
        payload["primary_goal"] = payload.get("primary_goal") or mission.get("goal")

    if not payload.get("business_summary"):
        raise HTTPException(status_code=422, detail="business_summary is required")
    if not payload.get("primary_goal"):
        raise HTTPException(status_code=422, detail="primary_goal is required")

    if not payload.get("title"):
        client_label = payload.get("client_name") or "Client"
        payload["title"] = f"{client_label} GTM Strategy Pack"

    report = report_service.create_report_job(
        payload,
        workspace_id=user.workspace_id,
        created_by=user.user_id,
    )

    logger.info("Report job accepted report_id=%s workspace=%s", report["id"][:8], user.workspace_id)
    background_tasks.add_task(report_service.run_report_job, report["id"])

    return Report(**report)


@router.get("", response_model=List[Report])
async def list_reports(
    mission_id: Optional[str] = Query(default=None),
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> List[Report]:
    """List report history for current workspace."""
    if not can_view_workspace_data(user.role):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    reports = report_service.list_reports(mission_id=mission_id, workspace_id=user.workspace_id)
    return [Report(**report) for report in reports]


@router.get("/{report_id}", response_model=Report)
async def get_report(
    report_id: str,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> Report:
    """Get report details by id."""
    report = get_report_record(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    _validate_workspace_access(report, user)

    return Report(**report)


@router.get("/{report_id}/download")
async def download_report(
    report_id: str,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> Response:
    """Download completed report PDF."""
    report = get_report_record(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    _validate_workspace_access(report, user)

    if report.get("status") != "completed":
        raise HTTPException(status_code=409, detail="Report is not ready for download")

    file_bytes = get_report_file(report_id)
    if not file_bytes:
        raise HTTPException(status_code=404, detail="Report file not found")

    file_name = report.get("file_name", f"report-{report_id[:8]}.pdf")
    headers = {"Content-Disposition": f'attachment; filename="{file_name}"'}
    return Response(content=file_bytes, media_type="application/pdf", headers=headers)

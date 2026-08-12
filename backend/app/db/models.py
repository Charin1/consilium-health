"""
ORM models for tenant-aware persistence.
"""
from __future__ import annotations

from typing import Optional
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.services.workflow_contracts import utc_now


class OrganizationModel(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class WorkspaceModel(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), ForeignKey("organizations.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="consultant")
    password_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class MissionModel(Base):
    __tablename__ = "missions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False, index=True)

    business: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(String(80), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)

    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    target_progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.20)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planning")
    tasks_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_running: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    errors: Mapped[dict] = mapped_column(JSON, nullable=False, default=list)
    last_error: Mapped[dict] = mapped_column(JSON, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ReportModel(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    mission_id: Mapped[str] = mapped_column(String(64), ForeignKey("missions.id"), nullable=True, index=True)

    report_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    file_name: Mapped[str] = mapped_column(String(240), nullable=False)

    input_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    sections: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    recommendations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    kpis: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[dict] = mapped_column(JSON, nullable=True)

    file_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    completed_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatSessionModel(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mission_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    selected_agent_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Which persona packs this session was seated from. Plural because packs
    # are views, not partitions: a session can seat a CFO next to a risk
    # adjustment specialist, and that combination is the point.
    persona_packs: Mapped[list] = mapped_column(JSON, nullable=False, default=lambda: ["core"])
    turn_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="automatic")
    stance_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")
    manual_agent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    memory_summary: Mapped[str] = mapped_column(Text, nullable=True)
    memory_key_points: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    memory_open_questions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    action_items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class SeatJobModel(Base):
    """
    One unit of work handed to one seat, tracked while it runs.

    The floor view needs to answer "what is this person doing right now", and
    a row that only exists once the work is finished cannot answer that. So the
    row is written at `queued` before any model call, and every state change is
    persisted as it happens rather than at the end.

    `status` is a small closed set: queued -> running -> delivered | failed.
    **Terminal states are guaranteed** -- the exception path finalizes the row
    as `failed` with the error class before re-raising, or the floor shows a
    seat thinking forever about a job that died ten minutes ago.
    """

    __tablename__ = "seat_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    seat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="task")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    # Human-readable, and shown over the seat's head on the floor. Not a state
    # machine value -- `status` is. This is the caption.
    progress_label: Mapped[str] = mapped_column(String(120), nullable=False, default="Queued")

    brief: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="Medium")
    result_message_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action_item_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    error_class: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    started_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), nullable=True)


class RoundJobModel(Base):
    """
    One "Convene the board" round, tracked while it runs.

    A round is not one seat's work, it is the whole debate, so it does not fit
    `SeatJobModel` -- it needs its own row for `current_speaker`, the turn
    count, and a `cancel_requested` flag the background thread polls between
    turns. That poll happens in `boardroom_graph`'s `route` node, once per
    turn, never mid-generation: a live model call is a blocking network
    request, so "Stop" takes effect after whoever is currently speaking
    finishes, not instantly.

    `status` is the same closed set as `SeatJobModel`, plus `cancelled` --
    a round can end because the user asked it to, and that is a different
    fact from failing.
    """

    __tablename__ = "round_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    progress_label: Mapped[str] = mapped_column(String(140), nullable=False, default="Queued")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    current_speaker: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    turn_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    turns_delivered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error_class: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    started_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatMessageModel(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), ForeignKey("chat_sessions.id"), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    message_meta: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

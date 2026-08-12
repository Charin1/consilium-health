"""
Chat API - multi-persona brainstorming sessions with persistence.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.auth import CurrentUser, require_roles
from app.services import round_service
from app.services.chat_service import chat_service
from app.services.template_service import list_strategic_templates

router = APIRouter()
logger = logging.getLogger("consilium.chat.api")

TurnMode = Literal["automatic", "manual"]
StanceMode = Literal["neutral", "support", "critique"]


class ChatSessionCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=180)
    mission_id: Optional[str] = None
    selected_agent_ids: Optional[List[str]] = None
    persona_packs: Optional[List[str]] = Field(
        default=None,
        description="Packs to seat this session from. Defaults to the deployment's packs.",
    )
    turn_mode: TurnMode = "automatic"
    stance_mode: StanceMode = "neutral"
    manual_agent_id: Optional[str] = Field(default=None, max_length=64)
    initial_prompt: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("title", "manual_agent_id", "initial_prompt")
    @classmethod
    def strip_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("selected_agent_ids")
    @classmethod
    def normalize_agent_ids(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        cleaned = []
        for item in value:
            normalized = item.strip()
            if normalized:
                cleaned.append(normalized)
        return cleaned or None


class ChatMessageRequest(BaseModel):
    message: Optional[str] = Field(default=None, max_length=2000)
    agent_id: Optional[str] = Field(default=None, description="Target a specific persona. Null = roundtable.")
    continue_dialogue: bool = False
    is_interjection: bool = False

    @field_validator("message", "agent_id")
    @classmethod
    def strip_optional_fields(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def ensure_turn_instruction(self) -> "ChatMessageRequest":
        if not self.message and not self.continue_dialogue:
            raise ValueError("Provide a message or request a continuation turn.")
        return self


class ChatSessionUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=180)
    selected_agent_ids: Optional[List[str]] = None
    turn_mode: Optional[TurnMode] = None
    stance_mode: Optional[StanceMode] = None
    manual_agent_id: Optional[str] = Field(default=None, max_length=64)

    @field_validator("title", "manual_agent_id")
    @classmethod
    def strip_update_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("selected_agent_ids")
    @classmethod
    def normalize_update_agent_ids(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        cleaned = []
        for item in value:
            normalized = item.strip()
            if normalized:
                cleaned.append(normalized)
        return cleaned or None


class ChatSession(BaseModel):
    id: str
    title: str
    mission_id: Optional[str]
    status: str
    selected_agent_ids: List[str]
    persona_packs: List[str] = Field(default_factory=lambda: ["core"])
    ladder: Dict[str, Any] = Field(default_factory=dict)
    disclaimer: str = ""
    turn_mode: TurnMode
    stance_mode: StanceMode
    manual_agent_id: Optional[str] = None
    memory_summary: Optional[str] = None
    memory_key_points: List[str] = Field(default_factory=list)
    memory_open_questions: List[str] = Field(default_factory=list)
    action_items: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: Any
    updated_at: Any


class ChatMessage(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    agent_id: Optional[str]
    metadata: Dict[str, Any]
    created_at: Any


class ChatSessionWithMessages(ChatSession):
    messages: List[ChatMessage] = Field(default_factory=list)


class ChatSendResponse(BaseModel):
    session: ChatSessionWithMessages
    messages: List[ChatMessage]


@router.get("/agents")
async def list_personas(packs: Optional[str] = None):
    """
    List seatable personas.

    `?packs=core,healthcare` scopes the roster. For the browsable directory
    with tags, tiers, and declared conflicts, use `GET /api/org/seats` -- this
    endpoint returns full system prompts and exists for the chat client.
    """
    selected = [p.strip() for p in packs.split(",") if p.strip()] if packs else None
    return chat_service.list_personas(selected)


@router.get("/templates")
async def list_templates():
    """List strategic boardroom templates."""
    return list_strategic_templates()


@router.post("/sessions", response_model=ChatSession)
async def create_session(
    payload: ChatSessionCreate,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> ChatSession:
    try:
        session = chat_service.create_session(
            workspace_id=user.workspace_id,
            created_by=user.user_id,
            mission_id=payload.mission_id,
            title=payload.title,
            selected_agent_ids=payload.selected_agent_ids,
            turn_mode=payload.turn_mode,
            stance_mode=payload.stance_mode,
            manual_agent_id=payload.manual_agent_id,
            persona_packs=payload.persona_packs,
        )
        if payload.initial_prompt and payload.initial_prompt.strip():
            chat_service.post_message(
                session_id=session["id"],
                workspace_id=user.workspace_id,
                user_id=user.user_id,
                message=payload.initial_prompt.strip(),
                target_agent_id=None,
                continue_dialogue=False,
                is_interjection=True,
            )
            session = chat_service.get_session(session["id"], workspace_id=user.workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ChatSession(**session)


@router.get("/sessions", response_model=List[ChatSession])
async def list_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> List[ChatSession]:
    """Past sessions, most recently touched first."""
    sessions = chat_service.list_sessions(workspace_id=user.workspace_id, limit=limit)
    return [ChatSession(**s) for s in sessions]


@router.get("/tasks")
async def list_tasks(
    state: Literal["all", "delivered", "outstanding"] = Query(default="all"),
    limit: int = Query(default=200, ge=1, le=500),
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> Dict[str, Any]:
    """
    Every task ever assigned, across sessions.

    "What did I ask for, and did I get it?" is a question that spans sessions,
    so it cannot be answered from inside one.
    """
    tasks = chat_service.list_tasks(
        workspace_id=user.workspace_id, state=state, limit=limit,
    )
    return {
        "tasks": tasks,
        "total": len(tasks),
        "delivered": sum(1 for t in tasks if t["delivered"]),
        "outstanding": sum(1 for t in tasks if not t["delivered"]),
    }


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
):
    """Delete a chat session."""
    deleted = chat_service.delete_session(session_id, workspace_id=user.workspace_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "success", "session_id": session_id}


@router.get("/sessions/{session_id}", response_model=ChatSessionWithMessages)
async def get_session(
    session_id: str,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> ChatSessionWithMessages:
    session = chat_service.get_session(session_id, workspace_id=user.workspace_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return ChatSessionWithMessages(**session)


@router.patch("/sessions/{session_id}", response_model=ChatSession)
async def update_session(
    session_id: str,
    payload: ChatSessionUpdate,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> ChatSession:
    updates = payload.model_dump(exclude_unset=True)
    try:
        session = chat_service.update_session(
            session_id=session_id,
            workspace_id=user.workspace_id,
            **updates,
        )
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "not found" in msg.lower() else 422
        raise HTTPException(status_code=code, detail=msg) from exc

    return ChatSession(**session)


@router.post("/sessions/{session_id}/messages", response_model=ChatSendResponse)
async def send_message(
    session_id: str,
    payload: ChatMessageRequest,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> ChatSendResponse:
    try:
        response = chat_service.post_message(
            session_id=session_id,
            workspace_id=user.workspace_id,
            user_id=user.user_id,
            message=payload.message,
            target_agent_id=payload.agent_id,
            continue_dialogue=payload.continue_dialogue,
            is_interjection=payload.is_interjection,
        )
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "not found" in msg.lower() else 422
        raise HTTPException(status_code=code, detail=msg) from exc
    return ChatSendResponse(**response)


class ConveneRequest(BaseModel):
    message: Optional[str] = Field(default=None, max_length=2000)
    continue_dialogue: bool = False

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


@router.post("/sessions/{session_id}/convene-async", status_code=202)
async def convene_async(
    session_id: str,
    payload: ConveneRequest,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> Dict[str, Any]:
    """
    Convene the board without blocking on the whole debate.

    Returns 202 and a round to poll. Validation and the opening brief are
    handled here, synchronously, before the round job is created -- a brief
    that was never going to be accepted must not get a job row (`assign-async`
    follows the same shape, for the same reason). The debate itself runs on a
    background thread, persisting each turn as it lands so `GET
    /api/chat/sessions/{id}` grows a live transcript instead of returning one
    response after everyone has spoken.
    """
    try:
        prepared = chat_service.prepare_convene(
            session_id=session_id,
            workspace_id=user.workspace_id,
            user_id=user.user_id,
            message=payload.message,
            continue_dialogue=payload.continue_dialogue,
        )
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "not found" in msg.lower() else 422
        raise HTTPException(status_code=code, detail=msg) from exc

    job = round_service.enqueue(workspace_id=user.workspace_id, session_id=session_id)
    round_service.spawn(
        job["id"],
        workspace_id=user.workspace_id,
        session_id=session_id,
        history=prepared["history"],
        normalized_message=prepared["normalized_message"],
        continue_dialogue=prepared["continue_dialogue"],
    )
    return job


@router.get("/sessions/{session_id}/round")
async def get_current_round(
    session_id: str,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> Dict[str, Any]:
    """The most recent round for this session, so a reload can find it."""
    job = round_service.get_for_session(session_id, workspace_id=user.workspace_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No round has run in this session")
    return job


@router.post("/sessions/{session_id}/synthesize", response_model=ChatSessionWithMessages)
async def synthesize_session(
    session_id: str,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant", "viewer")),
) -> ChatSessionWithMessages:
    try:
        session = chat_service.synthesize_session(
            session_id=session_id,
            workspace_id=user.workspace_id,
        )
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "not found" in msg.lower() else 422
        raise HTTPException(status_code=code, detail=msg) from exc
    return ChatSessionWithMessages(**session)


@router.delete("/messages/{message_id}")
async def delete_message(
    message_id: str,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant")),
) -> Dict[str, Any]:
    deleted = chat_service.delete_message(message_id=message_id, workspace_id=user.workspace_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Message not found")
    return {"status": "ok", "message_id": message_id}


@router.delete("/sessions/{session_id}/messages/{message_id}/truncate")
async def truncate_messages(
    session_id: str,
    message_id: str,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant")),
) -> Dict[str, Any]:
    try:
        count = chat_service.truncate_messages_from(
            session_id=session_id,
            message_id=message_id,
            workspace_id=user.workspace_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "deleted_count": count, "session_id": session_id}


class ActionItemCreate(BaseModel):
    task: str = Field(min_length=1, max_length=500)
    owner: str = Field(default="ceo", max_length=64)
    priority: str = Field(default="Medium", max_length=32)


class TaskAssignment(BaseModel):
    task: str = Field(min_length=1, max_length=1500)
    agent_id: str = Field(min_length=1, max_length=64)
    priority: str = Field(default="Medium", max_length=32)

    @field_validator("task", "agent_id")
    @classmethod
    def strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("This field cannot be blank.")
        return cleaned


class TaskAssignmentResponse(BaseModel):
    session: ChatSessionWithMessages
    messages: List[ChatMessage] = Field(default_factory=list)
    deliverable: Optional[ChatMessage] = None


@router.post("/sessions/{session_id}/assign", response_model=TaskAssignmentResponse)
async def assign_task(
    session_id: str,
    payload: TaskAssignment,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant")),
) -> TaskAssignmentResponse:
    """
    Assign work to one seat and get the deliverable in the same call.

    Creating the action item and asking for the work are one operation on
    purpose: split them and you get a task list where every item is owned and
    none of them are done.
    """
    try:
        result = chat_service.assign_task(
            session_id=session_id,
            workspace_id=user.workspace_id,
            user_id=user.user_id,
            task=payload.task,
            owner=payload.agent_id,
            priority=payload.priority,
        )
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "not found" in msg.lower() else 422
        raise HTTPException(status_code=code, detail=msg) from exc
    return TaskAssignmentResponse(**result)


@router.post("/sessions/{session_id}/assign-async", status_code=202)
async def assign_task_async(
    session_id: str,
    payload: TaskAssignment,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant")),
) -> Dict[str, Any]:
    """
    Assign work without waiting for it.

    Returns 202 and a job to poll. This is what the floor view uses: an order
    given should land instantly and the seat should visibly start working,
    rather than the whole UI holding still for a model call.
    """
    try:
        job = chat_service.assign_task_async(
            session_id=session_id,
            workspace_id=user.workspace_id,
            user_id=user.user_id,
            task=payload.task,
            owner=payload.agent_id,
            priority=payload.priority,
        )
    except ValueError as exc:
        msg = str(exc)
        code = 404 if "not found" in msg.lower() else 422
        raise HTTPException(status_code=code, detail=msg) from exc
    return job


class ActionItemUpdate(BaseModel):
    completed: Optional[bool] = None
    task: Optional[str] = Field(default=None, max_length=500)
    owner: Optional[str] = Field(default=None, max_length=64)
    priority: Optional[str] = Field(default=None, max_length=32)


@router.post("/sessions/{session_id}/action-items", response_model=ChatSessionWithMessages)
async def create_action_item(
    session_id: str,
    payload: ActionItemCreate,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant")),
) -> ChatSessionWithMessages:
    try:
        session = chat_service.add_action_item(
            session_id,
            workspace_id=user.workspace_id,
            task=payload.task,
            owner=payload.owner,
            priority=payload.priority,
        )
        messages = chat_service.get_messages(session_id, workspace_id=user.workspace_id)
        return ChatSessionWithMessages(**session, messages=messages)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/sessions/{session_id}/action-items/{item_id}", response_model=ChatSessionWithMessages)
async def update_action_item(
    session_id: str,
    item_id: str,
    payload: ActionItemUpdate,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant")),
) -> ChatSessionWithMessages:
    try:
        session = chat_service.update_action_item(
            session_id,
            item_id,
            workspace_id=user.workspace_id,
            completed=payload.completed,
            task=payload.task,
            owner=payload.owner,
            priority=payload.priority,
        )
        messages = chat_service.get_messages(session_id, workspace_id=user.workspace_id)
        return ChatSessionWithMessages(**session, messages=messages)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/sessions/{session_id}/action-items/{item_id}", response_model=ChatSessionWithMessages)
async def delete_action_item(
    session_id: str,
    item_id: str,
    user: CurrentUser = Depends(require_roles("owner", "admin", "consultant")),
) -> ChatSessionWithMessages:
    try:
        session = chat_service.delete_action_item(
            session_id,
            item_id,
            workspace_id=user.workspace_id,
        )
        messages = chat_service.get_messages(session_id, workspace_id=user.workspace_id)
        return ChatSessionWithMessages(**session, messages=messages)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class OTelClientLogPayload(BaseModel):
    timestamp: str
    severity_text: str
    severity_number: int
    body: str
    attributes: Dict[str, Any] = Field(default_factory=dict)


@router.post("/logs/client")
async def ingest_client_log(payload: Dict[str, Any]) -> Dict[str, str]:
    """Ingest frontend OpenTelemetry (OTel) JSON log records."""
    import json
    from app.utils.logger import get_logs_directory

    frontend_logs_dir = get_logs_directory("frontend")
    log_file = frontend_logs_dir / "frontend.log"
    
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    return {"status": "ok"}


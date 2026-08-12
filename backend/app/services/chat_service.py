"""
Brainstorm chat service with multi-persona agents, persistence, and memory.
"""
from __future__ import annotations

import logging
import os
import textwrap
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from uuid import uuid4

from sqlalchemy import desc, func, select
from app.config import UnifiedLLMClient, load_config
from app.db.database import get_session, init_db
from app.db.models import ChatMessageModel, ChatSessionModel
from app.services.boardroom_graph import BoardroomGraphEngine
from app.services.guardrails import guardrails_for_pack
from app.services.persona_loader import available_packs, load_personas
from app.services.phase_ladders import format_phase, ladder_for_pack
from app.services.workflow_contracts import utc_now

logger = logging.getLogger("consilium.chat")

DEFAULT_PACKS: Tuple[str, ...] = ("core",)


@dataclass(frozen=True)
class RosterBundle:
    """
    Everything a session's pack selection determines: who may speak, which
    phase ladder the debate walks, and which guardrails bind every seat.

    Sessions in different domains run in the same process, so this cannot be
    process-global state. It is keyed by the pack tuple and cached, because
    resolving 44 personas off disk on every turn would be absurd.
    """
    packs: Tuple[str, ...]
    personas: List[Dict[str, Any]]
    ladder: Dict[str, Any]
    guardrails: Dict[str, Any]
    allowed_agent_ids: Set[str] = field(default_factory=set)

    def persona(self, agent_id: Optional[str]) -> Optional[Dict[str, Any]]:
        return next((p for p in self.personas if p["id"] == agent_id), None)


class ChatService:
    """Chat orchestration with persona agents and lightweight memory."""

    def __init__(self, packs: Optional[List[str]] = None) -> None:
        init_db()
        self.config = load_config()
        self.llm_client = UnifiedLLMClient(self.config)
        self.max_history_messages = 24
        self.max_history_tokens = 1200
        self.summary_interval = 10
        self.turn_delay_ms = 1500
        self.max_auto_turns = 10

        self._bundles: Dict[Tuple[str, ...], RosterBundle] = {}
        self.__all_seats: Optional[List[Dict[str, Any]]] = None
        self.packs = list(packs or self._configured_packs())
        self.default_bundle = self.roster_for(self.packs)

        logger.info(
            "Chat service initialized: provider=%s default_packs=%s seats=%d "
            "ladder=%s guardrails=%s",
            self.config.provider, ",".join(self.packs), len(self.personas),
            self.ladder.get("id"), self.guardrails.get("id"),
        )

    # -- Roster bundles ----------------------------------------------------

    @staticmethod
    def normalize_packs(packs: Optional[Sequence[str]]) -> Tuple[str, ...]:
        """Requested packs, filtered to what exists, `core` if nothing survives."""
        if not packs:
            return DEFAULT_PACKS
        known = set(available_packs())
        cleaned: List[str] = []
        unknown: List[str] = []
        for pack in packs:
            name = str(pack or "").strip()
            if not name or name in cleaned:
                continue
            (cleaned if name in known else unknown).append(name)
        if unknown:
            logger.warning(
                "Ignoring unknown pack(s) %s; known packs are %s.",
                unknown, sorted(known),
            )
        return tuple(cleaned) or DEFAULT_PACKS

    def roster_for(self, packs: Optional[Sequence[str]]) -> RosterBundle:
        """The cached roster, ladder, and guardrails for a pack selection."""
        key = self.normalize_packs(packs)
        cached = self._bundles.get(key)
        if cached is not None:
            return cached

        personas = load_personas(list(key))
        # The most specific pack owns the ladder and the guardrails: with
        # ["core", "healthcare"] the debate runs the clinical ladder under
        # healthcare guardrails, while still seating the core C-suite.
        primary = key[-1]
        guardrails = guardrails_for_pack(primary)
        if guardrails.get("degraded"):
            logger.error(
                "Pack %s declares guardrails %r which do not exist - "
                "sessions on this pack run WITHOUT guardrails",
                primary, guardrails.get("requested"),
            )

        bundle = RosterBundle(
            packs=key,
            personas=personas,
            ladder=ladder_for_pack(primary),
            guardrails=guardrails,
            allowed_agent_ids={p["id"] for p in personas},
        )
        self._bundles[key] = bundle
        return bundle

    def bundle_for_session(self, session: ChatSessionModel) -> RosterBundle:
        """The roster a stored session was created against."""
        return self.roster_for(getattr(session, "persona_packs", None) or self.packs)

    # The deployment default, kept as attributes so existing callers and tests
    # that reach for `chat_service.personas` keep working.
    @property
    def personas(self) -> List[Dict[str, Any]]:
        return self.default_bundle.personas

    @property
    def allowed_agent_ids(self) -> Set[str]:
        return self.default_bundle.allowed_agent_ids

    @property
    def ladder(self) -> Dict[str, Any]:
        return self.default_bundle.ladder

    @property
    def guardrails(self) -> Dict[str, Any]:
        return self.default_bundle.guardrails

    @staticmethod
    def _configured_packs() -> List[str]:
        """
        The default pack selection for sessions that do not declare one.

        Defaults to `core` so an unconfigured deployment behaves exactly as it
        did before packs existed. A session that declares `persona_packs`
        overrides this entirely.
        """
        raw = os.getenv("CONSILIUM_PACKS", "").strip()
        if not raw:
            return list(DEFAULT_PACKS)
        return list(ChatService.normalize_packs(
            [p.strip() for p in raw.split(",") if p.strip()]
        ))

    # Public API
    def list_personas(self, packs: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
        return self.roster_for(packs).personas if packs else self.personas

    def create_session(
        self,
        *,
        workspace_id: str,
        created_by: str,
        mission_id: Optional[str],
        title: Optional[str],
        selected_agent_ids: Optional[List[str]] = None,
        turn_mode: str = "automatic",
        stance_mode: str = "neutral",
        manual_agent_id: Optional[str] = None,
        persona_packs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        bundle = self.roster_for(persona_packs or self.packs)
        normalized_agent_ids = self._normalize_selected_agent_ids(selected_agent_ids, bundle)
        normalized_turn_mode = self._normalize_turn_mode(turn_mode)
        normalized_stance_mode = self._normalize_stance_mode(stance_mode)
        normalized_manual_agent_id = self._normalize_manual_agent_id(
            manual_agent_id,
            normalized_agent_ids,
            normalized_turn_mode,
        )
        session_id = str(uuid4())
        now = utc_now()

        with get_session() as db:
            session = ChatSessionModel(
                id=session_id,
                workspace_id=workspace_id,
                created_by=created_by,
                mission_id=mission_id,
                title=title or "Brainstorm session",
                selected_agent_ids=normalized_agent_ids,
                persona_packs=list(bundle.packs),
                turn_mode=normalized_turn_mode,
                stance_mode=normalized_stance_mode,
                manual_agent_id=normalized_manual_agent_id,
                status="active",
                created_at=now,
                updated_at=now,
            )
            db.add(session)
            db.flush()

        return self._serialize_session(session)

    def list_sessions(self, *, workspace_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        with get_session() as db:
            rows = (
                db.execute(
                    select(ChatSessionModel)
                    .where(ChatSessionModel.workspace_id == workspace_id)
                    .order_by(desc(ChatSessionModel.updated_at), desc(ChatSessionModel.created_at))
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [self._serialize_session(row) for row in rows]

    def list_tasks(
        self,
        *,
        workspace_id: str,
        state: str = "all",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        Every assigned task across every session, newest first.

        Tasks live inside their session's `action_items`, which is fine for
        working *in* a session and useless for the question people actually
        ask afterwards: "what did I ask for, and did I get it?" That question
        spans sessions, so this flattens them and carries enough session
        context for each row to stand alone.

        `delivered` is derived from `message_id`, not from a status field.
        A task is done when there is a message fulfilling it; anything else is
        a flag that can disagree with reality.
        """
        wanted = (state or "all").strip().lower()
        collected: List[Dict[str, Any]] = []

        for session in self.list_sessions(workspace_id=workspace_id, limit=limit):
            bundle = self.roster_for(session.get("persona_packs"))
            for item in session.get("action_items") or []:
                delivered = bool(item.get("message_id"))
                if wanted == "delivered" and not delivered:
                    continue
                if wanted == "outstanding" and delivered:
                    continue
                owner_id = item.get("owner")
                persona = bundle.persona(owner_id)
                collected.append({
                    **item,
                    "delivered": delivered,
                    "owner_name": persona["name"] if persona else owner_id,
                    "session_id": session["id"],
                    "session_title": session["title"],
                    "session_updated_at": session["updated_at"],
                })

        collected.sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)
        return collected[:limit]

    def get_session(self, session_id: str, *, workspace_id: str) -> Optional[Dict[str, Any]]:
        with get_session() as db:
            row = (
                db.execute(
                    select(ChatSessionModel)
                    .where(ChatSessionModel.id == session_id, ChatSessionModel.workspace_id == workspace_id)
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            session = self._serialize_session(row)
            session["messages"] = self.get_messages(session_id, workspace_id=workspace_id)
            return session

    def update_session(self, *, session_id: str, workspace_id: str, **updates: Any) -> Dict[str, Any]:
        with get_session() as db:
            row = (
                db.execute(
                    select(ChatSessionModel)
                    .where(ChatSessionModel.id == session_id, ChatSessionModel.workspace_id == workspace_id)
                )
                .scalars()
                .first()
            )
            if row is None:
                raise ValueError("Session not found")

            if "title" in updates:
                title = (updates.get("title") or "").strip()
                if not title:
                    raise ValueError("Session title cannot be empty")
                row.title = title

            selected_agent_ids = self._normalize_selected_agent_ids(
                updates.get("selected_agent_ids", row.selected_agent_ids)
            )
            turn_mode = self._normalize_turn_mode(updates.get("turn_mode", row.turn_mode or "automatic"))
            stance_mode = self._normalize_stance_mode(updates.get("stance_mode", row.stance_mode or "neutral"))
            manual_agent_id = self._normalize_manual_agent_id(
                updates["manual_agent_id"] if "manual_agent_id" in updates else row.manual_agent_id,
                selected_agent_ids,
                turn_mode,
            )

            row.selected_agent_ids = selected_agent_ids
            row.turn_mode = turn_mode
            row.stance_mode = stance_mode
            row.manual_agent_id = manual_agent_id
            row.updated_at = utc_now()
            db.add(row)
            db.flush()

            return self._serialize_session(row)

    def get_messages(self, session_id: str, *, workspace_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        with get_session() as db:
            rows = (
                db.execute(
                    select(ChatMessageModel)
                    .where(
                        ChatMessageModel.session_id == session_id,
                        ChatMessageModel.workspace_id == workspace_id,
                    )
                    .order_by(ChatMessageModel.created_at)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [self._serialize_message(row) for row in rows]

    def delete_session(self, session_id: str, *, workspace_id: str) -> bool:
        """Delete a chat session and all associated messages."""
        with get_session() as db:
            # Delete messages first
            db.execute(
                ChatMessageModel.__table__.delete().where(
                    ChatMessageModel.session_id == session_id,
                    ChatMessageModel.workspace_id == workspace_id,
                )
            )
            # Delete session
            res = db.execute(
                ChatSessionModel.__table__.delete().where(
                    ChatSessionModel.id == session_id,
                    ChatSessionModel.workspace_id == workspace_id,
                )
            )
            db.commit()
            return res.rowcount > 0

    def delete_message(self, *, message_id: str, workspace_id: str) -> bool:
        with get_session() as db:
            res = db.execute(
                ChatMessageModel.__table__.delete().where(
                    ChatMessageModel.id == message_id,
                    ChatMessageModel.workspace_id == workspace_id,
                )
            )
            db.commit()
            return res.rowcount > 0

    def add_action_item(
        self,
        session_id: str,
        *,
        workspace_id: str,
        task: str,
        owner: str = "ceo",
        priority: str = "Medium"
    ) -> Dict[str, Any]:
        with get_session() as db:
            session = db.execute(
                select(ChatSessionModel).where(
                    ChatSessionModel.id == session_id,
                    ChatSessionModel.workspace_id == workspace_id,
                )
            ).scalar_one_or_none()
            if not session:
                raise ValueError("Chat session not found")

            items = list(getattr(session, "action_items", []) or [])
            new_item = {
                "id": str(uuid4()),
                "task": task.strip(),
                "owner": owner.strip() if owner else "ceo",
                "priority": priority.strip() if priority else "Medium",
                "completed": False,
                "created_at": utc_now().isoformat()
            }
            items.append(new_item)
            session.action_items = items
            session.updated_at = utc_now()
            db.add(session)
            db.flush()
            return self._serialize_session(session)

    def assign_task(
        self,
        session_id: str,
        *,
        workspace_id: str,
        user_id: str,
        task: str,
        owner: str,
        priority: str = "Medium",
    ) -> Dict[str, Any]:
        """
        Assign a piece of work to one seat and get the deliverable back.

        This is deliberately one call, not "create an action item" followed by
        "now ask someone about it". Splitting them is how you end up with a
        task list nobody has actually done: the item exists, the owner is set,
        and no work was produced. Here the item and the answer arrive together,
        and the item carries the id of the message that fulfilled it.

        The owner must be seated in this session. Assigning to someone who is
        not in the room would create an item that can never be worked.
        """
        session = self._get_session_row(session_id, workspace_id)
        if session is None:
            raise ValueError("Session not found")

        cleaned_task = (task or "").strip()
        if not cleaned_task:
            raise ValueError("A task description is required")

        bundle = self.bundle_for_session(session)
        owner_id = (owner or "").strip()
        persona = bundle.persona(owner_id)
        if persona is None:
            raise ValueError(f"Unknown seat: {owner_id}")
        if owner_id not in set(self._resolve_session_agent_ids(session, bundle)):
            raise ValueError("That seat is not active in this session")

        self.add_action_item(
            session_id,
            workspace_id=workspace_id,
            task=cleaned_task,
            owner=owner_id,
            priority=priority,
        )

        # The brief is shaped as an assignment rather than a discussion prompt:
        # the reply is meant to be a deliverable, not another opinion in a
        # debate that nobody asked for.
        brief = (
            f"TASK ASSIGNED TO YOU ({persona['name']}):\n{cleaned_task}\n\n"
            "Deliver the work itself, not a plan to do it later. State your "
            "assumptions where the brief is thin, give the numbers you would "
            "actually use, and finish with what you need from whom to close it out."
        )
        response = self.post_message(
            session_id=session_id,
            workspace_id=workspace_id,
            user_id=user_id,
            message=brief,
            target_agent_id=owner_id,
            continue_dialogue=False,
            is_interjection=True,
        )

        # Link the deliverable to the item so the log can show the work next to
        # the task, and so a task with no output is visibly unfinished.
        deliverable = next(
            (m for m in reversed(response.get("messages", []))
             if m.get("role") == "assistant" and m.get("agent_id") == owner_id),
            None,
        )
        if deliverable is not None:
            with get_session() as db:
                row = db.execute(
                    select(ChatSessionModel).where(
                        ChatSessionModel.id == session_id,
                        ChatSessionModel.workspace_id == workspace_id,
                    )
                ).scalar_one_or_none()
                if row is not None:
                    items = list(getattr(row, "action_items", []) or [])
                    if items:
                        items[-1] = {
                            **items[-1],
                            "message_id": deliverable["id"],
                            "delivered_at": utc_now().isoformat(),
                        }
                        row.action_items = items
                        row.updated_at = utc_now()
                        db.add(row)
                        db.flush()

        session_dict = self.get_session(session_id, workspace_id=workspace_id)
        return {
            "session": session_dict,
            "messages": response.get("messages", []),
            "deliverable": deliverable,
        }

    def assign_task_async(
        self,
        session_id: str,
        *,
        workspace_id: str,
        user_id: str,
        task: str,
        owner: str,
        priority: str = "Medium",
    ) -> Dict[str, Any]:
        """
        Assign work and return immediately with a job to watch.

        Same validation as `assign_task`, run *before* enqueueing: a job row for
        work that was never going to be accepted is a permanently failed desk
        on the floor and a confusing error one poll later.
        """
        from app.services import job_service

        session = self._get_session_row(session_id, workspace_id)
        if session is None:
            raise ValueError("Session not found")

        cleaned_task = (task or "").strip()
        if not cleaned_task:
            raise ValueError("A task description is required")

        bundle = self.bundle_for_session(session)
        owner_id = (owner or "").strip()
        if bundle.persona(owner_id) is None:
            raise ValueError(f"Unknown seat: {owner_id}")
        if owner_id not in set(self._resolve_session_agent_ids(session, bundle)):
            raise ValueError("That seat is not active in this session")

        job = job_service.enqueue(
            workspace_id=workspace_id,
            session_id=session_id,
            seat_id=owner_id,
            brief=cleaned_task,
            priority=priority,
        )

        def work(report):
            report(job_service.LABEL_WORKING)
            result = self.assign_task(
                session_id,
                workspace_id=workspace_id,
                user_id=user_id,
                task=cleaned_task,
                owner=owner_id,
                priority=priority,
            )
            report(job_service.LABEL_WRITING)
            deliverable = result.get("deliverable") or {}
            items = result.get("session", {}).get("action_items") or []
            meta = deliverable.get("metadata") or {}
            return {
                "message_id": deliverable.get("id"),
                "action_item_id": items[-1]["id"] if items else None,
                "provider": meta.get("provider"),
                "model": meta.get("model"),
            }

        job_service.spawn(job["id"], work)
        return job

    def update_action_item(
        self,
        session_id: str,
        item_id: str,
        *,
        workspace_id: str,
        completed: Optional[bool] = None,
        task: Optional[str] = None,
        owner: Optional[str] = None,
        priority: Optional[str] = None
    ) -> Dict[str, Any]:
        with get_session() as db:
            session = db.execute(
                select(ChatSessionModel).where(
                    ChatSessionModel.id == session_id,
                    ChatSessionModel.workspace_id == workspace_id,
                )
            ).scalar_one_or_none()
            if not session:
                raise ValueError("Chat session not found")

            items = list(getattr(session, "action_items", []) or [])
            found = False
            for item in items:
                if item.get("id") == item_id:
                    found = True
                    if completed is not None:
                        item["completed"] = completed
                    if task is not None:
                        item["task"] = task.strip()
                    if owner is not None:
                        item["owner"] = owner.strip()
                    if priority is not None:
                        item["priority"] = priority.strip()
                    break

            if not found:
                raise ValueError("Action item not found")

            session.action_items = items
            session.updated_at = utc_now()
            db.add(session)
            db.flush()
            return self._serialize_session(session)

    def delete_action_item(self, session_id: str, item_id: str, *, workspace_id: str) -> Dict[str, Any]:
        with get_session() as db:
            session = db.execute(
                select(ChatSessionModel).where(
                    ChatSessionModel.id == session_id,
                    ChatSessionModel.workspace_id == workspace_id,
                )
            ).scalar_one_or_none()
            if not session:
                raise ValueError("Chat session not found")

            items = list(getattr(session, "action_items", []) or [])
            session.action_items = [it for it in items if it.get("id") != item_id]
            session.updated_at = utc_now()
            db.add(session)
            db.flush()
            return self._serialize_session(session)

    def truncate_messages_from(self, *, session_id: str, message_id: str, workspace_id: str) -> int:
        with get_session() as db:
            target_msg = db.execute(
                select(ChatMessageModel).where(
                    ChatMessageModel.id == message_id,
                    ChatMessageModel.session_id == session_id,
                    ChatMessageModel.workspace_id == workspace_id,
                )
            ).scalar_one_or_none()

            if target_msg is None:
                raise ValueError("Target message not found in session")

            cutoff_time = target_msg.created_at

            res = db.execute(
                ChatMessageModel.__table__.delete().where(
                    ChatMessageModel.session_id == session_id,
                    ChatMessageModel.workspace_id == workspace_id,
                    ChatMessageModel.created_at >= cutoff_time,
                )
            )
            db.commit()
            return res.rowcount

    def prepare_convene(
        self,
        *,
        session_id: str,
        workspace_id: str,
        user_id: str,
        message: Optional[str],
        continue_dialogue: bool = False,
    ) -> Dict[str, Any]:
        """
        Validate and persist the opening brief for a round, before any round
        job is enqueued.

        Split out of `run_convene_round` so validation happens synchronously,
        on the request that asked for it: work that was never going to be
        accepted must not get a job row, or the caller gets a permanently
        failed desk and a confusing error one poll later instead of an
        immediate 422 (`job_service`'s docstring covers the same trade for
        seat assignments).
        """
        session = self._get_session_row(session_id, workspace_id)
        if session is None:
            raise ValueError("Session not found")

        normalized_message = (message or "").strip()
        if normalized_message and len(normalized_message) > 2000:
            raise ValueError("Message too long (max 2000 characters)")

        history = self._recent_history(session_id, workspace_id)
        if continue_dialogue and not normalized_message and not history:
            raise ValueError("Start the discussion with a message before requesting continuation turns")

        user_message = None
        if normalized_message:
            msg = self._create_message(
                session_id=session_id,
                workspace_id=workspace_id,
                role="user",
                content=normalized_message,
                agent_id=None,
                metadata={"author": user_id},
            )
            user_message = self._serialize_message(msg)
            history.append({"role": "user", "agent_id": None, "content": normalized_message})

        return {
            "history": history,
            "normalized_message": normalized_message,
            "user_message": user_message,
            "continue_dialogue": continue_dialogue,
        }

    def run_convene_round(
        self,
        *,
        session_id: str,
        workspace_id: str,
        history: List[Dict[str, Any]],
        normalized_message: str = "",
        continue_dialogue: bool = False,
        should_stop: Optional[Callable[[], bool]] = None,
        on_turn: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Run the debate itself: every seat's turn, then the chair's recap.

        Always the automatic round, regardless of the session's stored
        `turn_mode` -- "Convene the board" is a distinct, deliberate action,
        not a targeted message, so it does not inherit manual-mode routing.

        Called two ways: synchronously from the request that also calls
        `prepare_convene` (the pre-async behaviour), or from a background
        thread with `should_stop`/`on_turn` wired to a round job, which is
        what lets a user watch the transcript grow and press Stop.
        """
        session = self._get_session_row(session_id, workspace_id)
        if session is None:
            raise ValueError("Session not found")

        bundle = self.bundle_for_session(session)
        agent_ids = self._resolve_session_agent_ids(session, bundle)
        agents = [p for p in bundle.personas if p["id"] in agent_ids]
        if not agents:
            agents = [p for p in bundle.personas if p["id"] != "moderator"]

        memory = {
            "summary": session.memory_summary,
            "key_points": session.memory_key_points,
            "open_questions": session.memory_open_questions,
        }
        turn_mode = self._normalize_turn_mode(session.turn_mode or "automatic")
        stance_mode = self._normalize_stance_mode(session.stance_mode or "neutral")
        turn_brief = self._build_continuation_brief(normalized_message, history, memory)

        replies = self._run_automatic_round(
            session=session,
            workspace_id=workspace_id,
            bundle=bundle,
            agents=agents,
            memory=memory,
            turn_brief=turn_brief,
            stance_mode=stance_mode,
            turn_mode=turn_mode,
            continue_dialogue=continue_dialogue,
            rolling_history=list(history),
            should_stop=should_stop,
            on_turn=on_turn,
        )

        # `post_message` does this bookkeeping for its own callers; this path
        # bypasses post_message entirely (it is driven by a round job, not a
        # single request/response), so the session still needs its own
        # updated_at bump and its periodic memory summary.
        total_messages = self._count_messages(session_id, workspace_id)
        if total_messages % self.summary_interval == 0:
            self._update_memory(session_id, workspace_id)
        else:
            self._touch_session(session_id, workspace_id)

        return replies

    def post_message(
        self,
        *,
        session_id: str,
        workspace_id: str,
        user_id: str,
        message: Optional[str],
        target_agent_id: Optional[str],
        continue_dialogue: bool,
        is_interjection: bool = False,
    ) -> Dict[str, Any]:
        session = self._get_session_row(session_id, workspace_id)
        if session is None:
            raise ValueError("Session not found")
        normalized_message = (message or "").strip()
        if normalized_message and len(normalized_message) > 2000:
            raise ValueError("Message too long (max 2000 characters)")

        session_agent_ids = set(self._resolve_session_agent_ids(session))
        if target_agent_id and target_agent_id not in session_agent_ids:
            raise ValueError("Selected agent is not active in this session")

        history = self._recent_history(session_id, workspace_id)
        if continue_dialogue and not normalized_message and not history:
            raise ValueError("Start the discussion with a message before requesting continuation turns")

        user_msg = None
        if normalized_message:
            user_msg = self._create_message(
                session_id=session_id,
                workspace_id=workspace_id,
                role="user",
                content=normalized_message,
                agent_id=None,
                metadata={"author": user_id, "is_interjection": is_interjection},
            )
            prefix = "INTERJECTION: " if is_interjection else ""
            history.append({"role": "user", "agent_id": None, "content": f"{prefix}{normalized_message}"})

        replies = []
        # In manual mode, if user posts an interjection note without specifying target_agent_id,
        # save the note and let the user manually select who responds next.
        if not (session.turn_mode == "manual" and is_interjection and not target_agent_id):
            replies = self._generate_agent_replies(
                session=session,
                workspace_id=workspace_id,
                user_message=normalized_message,
                target_agent_id=target_agent_id,
                continue_dialogue=continue_dialogue,
                history=history,
            )

        # Update session memory periodically
        total_messages = self._count_messages(session_id, workspace_id)
        if total_messages % self.summary_interval == 0:
            self._update_memory(session_id, workspace_id)
        else:
            self._touch_session(session_id, workspace_id)

        current_session = self._get_session_row(session_id, workspace_id)
        if current_session is None:
            raise ValueError("Session not found")

        session_dict = self._serialize_session(current_session)
        session_dict["messages"] = self.get_messages(session_id, workspace_id=workspace_id)

        response_messages = list(replies)
        if user_msg is not None:
            response_messages.insert(0, self._serialize_message(user_msg))

        return {
            "session": session_dict,
            "messages": response_messages,
        }

    def synthesize_session(self, session_id: str, workspace_id: str) -> Dict[str, Any]:
        """Force a consultant-grade executive synthesis of the session using the Board Chair persona."""
        current_session = self._get_session_row(session_id, workspace_id)
        if current_session is None:
            raise ValueError("Session not found")
        
        bundle = self.bundle_for_session(current_session)
        history = self._recent_history(session_id, workspace_id)
        if history:
            mod = bundle.persona("moderator")
            mod_prompt = mod["system_prompt"] if mod else "You are the Board Chair."
            # The chair's synthesis is the artefact a user exports. It carries
            # the same boundary every advisor turn did, plus the addendum that
            # tells the chair to strip anything that reads as patient advice.
            guard = "\n".join(part for part in (
                bundle.guardrails.get("prompt_block", ""),
                bundle.guardrails.get("moderator_addendum", ""),
            ) if part).strip()
            if guard:
                mod_prompt = f"{guard}\n\n{mod_prompt}"
            try:
                system_prompt = (
                    f"{mod_prompt}\n\n"
                    "Produce a consultant-grade Executive Synthesis Briefing covering:\n"
                    "1. Executive Consensus & Vision Alignment\n"
                    "2. Key Decided Action Items & Department Ownership\n"
                    "3. Critical Unresolved Risks & Mitigations\n"
                    "4. Immediate 30-Day Execution Next Steps."
                )
                formatted_items = [
                    f"{msg.get('agent_id', msg.get('role', 'user')).upper()}: {msg.get('content', '')}"
                    for msg in history
                ]
                summary_msg = self.llm_client.generate_with_sliding_window(
                    system_prompt=system_prompt,
                    items=formatted_items,
                    window_size=10,
                    overlap=3,
                    temperature=0.3,
                    max_tokens=4000,
                )
                
                self._create_message(
                    session_id=session_id,
                    workspace_id=workspace_id,
                    role="assistant",
                    content=summary_msg,
                    agent_id="moderator",
                    metadata={"is_synthesis": True}
                )
            except Exception as e:
                logger.error(f"Synthesis failed: {e}")
                
        self._update_memory(session_id, workspace_id)
        current_session = self._get_session_row(session_id, workspace_id)
        session_dict = self._serialize_session(current_session)
        session_dict["messages"] = self.get_messages(session_id, workspace_id=workspace_id)
        return session_dict

    # Helpers
    def _get_session_row(self, session_id: str, workspace_id: str) -> Optional[ChatSessionModel]:
        with get_session() as db:
            return (
                db.execute(
                    select(ChatSessionModel)
                    .where(ChatSessionModel.id == session_id, ChatSessionModel.workspace_id == workspace_id)
                )
                .scalars()
                .first()
            )

    def _count_messages(self, session_id: str, workspace_id: str) -> int:
        with get_session() as db:
            return (
                db.execute(
                    select(func.count())
                    .select_from(ChatMessageModel)
                    .where(
                        ChatMessageModel.session_id == session_id,
                        ChatMessageModel.workspace_id == workspace_id,
                    )
                )
                .scalar_one()
            )

    def _touch_session(self, session_id: str, workspace_id: str) -> None:
        with get_session() as db:
            row = (
                db.execute(
                    select(ChatSessionModel)
                    .where(ChatSessionModel.id == session_id, ChatSessionModel.workspace_id == workspace_id)
                )
                .scalars()
                .first()
            )
            if row is None:
                return
            row.updated_at = utc_now()
            db.add(row)
            db.flush()

    def _create_message(
        self,
        *,
        session_id: str,
        workspace_id: str,
        role: str,
        content: str,
        agent_id: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ChatMessageModel:
        msg_id = str(uuid4())
        meta = metadata or {}
        
        # Inject Langfuse Tracing Schema
        meta["trace_id"] = session_id
        meta["span_id"] = msg_id
        meta["provider"] = self.config.provider
        meta["model"] = getattr(self.llm_client.last_result, "model", None) or self.config.model
        # Degradation travels with the message. A canned turn that looks like
        # an advisor's opinion is the failure a user acts on believing a
        # specialist said it.
        last = self.llm_client.last_result
        if last is not None and last.degraded:
            meta["degraded"] = True
            meta["degraded_reason"] = last.reason

        msg = ChatMessageModel(
            id=msg_id,
            session_id=session_id,
            workspace_id=workspace_id,
            role=role,
            content=content,
            agent_id=agent_id,
            message_meta=meta,
            created_at=utc_now(),
        )
        with get_session() as db:
            db.add(msg)
            db.flush()

        from app.services.tracing import trace_exporter
        trace_exporter.log_generation(
            trace_id=session_id,
            span_id=msg_id,
            name=f"turn:{agent_id or role}",
            model=meta["model"],
            provider=meta["provider"],
            prompt="",
            completion=content,
            metadata=meta,
        )

        return msg

    def _generate_agent_replies(
        self,
        *,
        session: ChatSessionModel,
        workspace_id: str,
        user_message: str,
        target_agent_id: Optional[str],
        continue_dialogue: bool,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> List[Dict[str, Any]]:
        bundle = self.bundle_for_session(session)
        agent_ids = self._resolve_session_agent_ids(session, bundle)
        agents = [p for p in bundle.personas if p["id"] in agent_ids]
        if not agents:
            agents = [p for p in bundle.personas if p["id"] != "moderator"]

        history = list(history if history is not None else self._recent_history(session.id, workspace_id))
        memory = {
            "summary": session.memory_summary,
            "key_points": session.memory_key_points,
            "open_questions": session.memory_open_questions,
        }
        turn_mode = self._normalize_turn_mode(session.turn_mode or "automatic")
        stance_mode = self._normalize_stance_mode(session.stance_mode or "neutral")
        turn_brief = self._build_continuation_brief(user_message, history, memory)

        replies: List[Dict[str, Any]] = []
        rolling_history = list(history)

        if target_agent_id:
            persona = next((item for item in agents if item["id"] == target_agent_id), None)
            if persona is None:
                raise ValueError("Selected agent is not active in this session")

            prior_speaker_id = self._last_assistant_speaker(rolling_history)
            text = self._call_model(
                persona,
                rolling_history,
                memory,
                turn_brief,
                prior_speaker_id=prior_speaker_id,
                stance_mode=stance_mode,
                selection_reason=f"{persona['name']} was chosen directly by the user.",
                bundle=bundle,
            )
            msg = self._create_message(
                session_id=session.id,
                workspace_id=workspace_id,
                role="assistant",
                content=text,
                agent_id=persona["id"],
                metadata={
                    "persona": persona["name"],
                    "turn_index": 1,
                    "turn_total": 1,
                    "delay_ms": self.turn_delay_ms,
                    "turn_mode": turn_mode,
                    "stance_mode": stance_mode,
                    "chosen_by": "direct",
                    "selection_reason": f"{persona['name']} was chosen directly by the user.",
                    "continue_dialogue": continue_dialogue,
                },
            )
            return [self._serialize_message(msg)]

        if turn_mode == "manual":
            last_speaker = self._last_assistant_speaker(rolling_history)
            manual_agent_id = target_agent_id or session.manual_agent_id
            
            # If manual_agent_id matches last speaker or isn't explicitly targeted, pick next via selection engine
            if not target_agent_id or manual_agent_id == last_speaker:
                persona, reason = self._select_next_agent(
                    session_id=session.id,
                    agents=agents,
                    history=rolling_history,
                    memory=memory,
                    turn_brief=turn_brief,
                    stance_mode=stance_mode,
                )
            else:
                manual_agent_id = self._normalize_manual_agent_id(manual_agent_id, agent_ids, turn_mode)
                persona = next((item for item in agents if item["id"] == manual_agent_id), None)
                if persona is None:
                    persona = agents[0]
                reason = f"Manual mode routed turn to {persona['name']}."

            prior_speaker_id = last_speaker
            text = self._call_model(
                persona,
                rolling_history,
                memory,
                turn_brief,
                prior_speaker_id=prior_speaker_id,
                stance_mode=stance_mode,
                selection_reason=reason,
                bundle=bundle,
            )
            msg = self._create_message(
                session_id=session.id,
                workspace_id=workspace_id,
                role="assistant",
                content=text,
                agent_id=persona["id"],
                metadata={
                    "persona": persona["name"],
                    "turn_index": 1,
                    "turn_total": 1,
                    "delay_ms": self.turn_delay_ms,
                    "turn_mode": turn_mode,
                    "stance_mode": stance_mode,
                    "chosen_by": "manual",
                    "selection_reason": reason,
                    "continue_dialogue": continue_dialogue,
                },
            )
            return [self._serialize_message(msg)]

        return self._run_automatic_round(
            session=session,
            workspace_id=workspace_id,
            bundle=bundle,
            agents=agents,
            memory=memory,
            turn_brief=turn_brief,
            stance_mode=stance_mode,
            turn_mode=turn_mode,
            continue_dialogue=continue_dialogue,
            rolling_history=rolling_history,
        )

    def _run_automatic_round(
        self,
        *,
        session: ChatSessionModel,
        workspace_id: str,
        bundle: RosterBundle,
        agents: List[Dict[str, Any]],
        memory: Dict[str, Any],
        turn_brief: str,
        stance_mode: str,
        turn_mode: str,
        continue_dialogue: bool,
        rolling_history: List[Dict[str, Any]],
        should_stop: Optional[Callable[[], bool]] = None,
        on_turn: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Run the automatic round: pick a speaker, let them speak, decide whether
        to continue, close with the chair. `boardroom_graph` owns that control
        flow as a compiled state machine; this closure keeps prompt
        construction and persistence here, where they already live.

        `should_stop` and `on_turn` exist for the async "Convene the board"
        path: a background thread runs this same method, polling `should_stop`
        between turns (never mid-generation -- a live model call cannot be
        interrupted without killing the thread) and reporting progress through
        `on_turn` after each message is persisted, so a poller can show a live
        transcript rather than one response after the whole round finishes.
        The synchronous call site above passes neither, so its behaviour is
        unchanged.
        """
        planned_turn_count = min(self.max_auto_turns, len(agents))
        total_turns = planned_turn_count + (1 if planned_turn_count > 1 else 0)
        agents_by_id = {p["id"]: p for p in agents}

        def select(state, remaining):
            persona, reason = self._select_next_agent(
                session_id=session.id,
                agents=[agents_by_id[a] for a in remaining],
                history=state["history"],
                memory=memory,
                turn_brief=turn_brief,
                stance_mode=stance_mode,
            )
            return (persona["id"], reason)

        def speak(state, seat_id, reason):
            persona = agents_by_id[seat_id]
            text = self._call_model(
                persona,
                state["history"],
                memory,
                turn_brief,
                prior_speaker_id=self._last_assistant_speaker(state["history"]),
                stance_mode=stance_mode,
                selection_reason=reason,
                bundle=bundle,
            )
            msg = self._create_message(
                session_id=session.id,
                workspace_id=workspace_id,
                role="assistant",
                content=text,
                agent_id=seat_id,
                metadata={
                    "persona": persona["name"],
                    "turn_index": state.get("turn_index", 0) + 1,
                    "turn_total": total_turns,
                    "delay_ms": self.turn_delay_ms,
                    "turn_mode": turn_mode,
                    "stance_mode": stance_mode,
                    "chosen_by": "automatic",
                    "selection_reason": reason,
                    "continue_dialogue": continue_dialogue,
                },
            )
            serialized = self._serialize_message(msg)
            if on_turn:
                on_turn(serialized)
            return serialized

        def recap(state):
            moderator = bundle.persona("moderator")
            if moderator is None:
                return None
            reason = "Moderator recap closes the automatic round with alignment and next steps."
            text = self._call_model(
                moderator,
                state["history"],
                memory,
                turn_brief,
                prior_speaker_id=self._last_assistant_speaker(state["history"]),
                stance_mode="neutral",
                selection_reason=reason,
                is_summary=True,
                bundle=bundle,
            )
            msg = self._create_message(
                session_id=session.id,
                workspace_id=workspace_id,
                role="assistant",
                content=text,
                agent_id=moderator["id"],
                metadata={
                    "persona": moderator["name"],
                    "turn_index": total_turns,
                    "turn_total": total_turns,
                    "delay_ms": self.turn_delay_ms,
                    "turn_mode": turn_mode,
                    "stance_mode": "neutral",
                    "chosen_by": "automatic",
                    "selection_reason": reason,
                    "continue_dialogue": continue_dialogue,
                },
            )
            serialized = self._serialize_message(msg)
            if on_turn:
                on_turn(serialized)
            return serialized

        engine = BoardroomGraphEngine(
            bundle.personas,
            ladder_id=bundle.ladder.get("id"),
            guardrails=bundle.guardrails.get("id"),
        )
        return engine.run_round(
            session_id=session.id,
            brief=turn_brief,
            history=rolling_history,
            active_advisors=[p["id"] for p in agents],
            select=select,
            speak=speak,
            recap=recap,
            max_turns=planned_turn_count,
            stance_mode=stance_mode,
            memory_summary=memory,
            should_stop=should_stop,
        )

    def _normalize_selected_agent_ids(
        self,
        selected_agent_ids: Optional[List[str]],
        bundle: Optional[RosterBundle] = None,
    ) -> List[str]:
        bundle = bundle or self.default_bundle
        default_ids = [p["id"] for p in bundle.personas if p["id"] != "moderator"]
        if selected_agent_ids is None:
            return default_ids

        normalized_ids: List[str] = []
        invalid_ids: List[str] = []
        seen: set[str] = set()
        for raw_agent_id in selected_agent_ids:
            agent_id = str(raw_agent_id or "").strip()
            if not agent_id or agent_id == "moderator" or agent_id in seen:
                continue
            if agent_id not in bundle.allowed_agent_ids:
                invalid_ids.append(agent_id)
                continue
            seen.add(agent_id)

        if invalid_ids:
            raise ValueError(f"Unknown agent(s): {', '.join(invalid_ids)}")

        normalized_ids = [
            persona["id"]
            for persona in bundle.personas
            if persona["id"] in seen and persona["id"] != "moderator"
        ]
        if not normalized_ids:
            raise ValueError("At least one active discussion agent is required")
        return normalized_ids

    @staticmethod
    def _normalize_turn_mode(turn_mode: str) -> str:
        normalized = str(turn_mode or "automatic").strip().lower()
        if normalized not in {"automatic", "manual"}:
            raise ValueError("Invalid turn mode")
        return normalized

    @staticmethod
    def _normalize_stance_mode(stance_mode: str) -> str:
        normalized = str(stance_mode or "neutral").strip().lower()
        if normalized not in {"neutral", "support", "critique"}:
            raise ValueError("Invalid stance mode")
        return normalized

    def _normalize_manual_agent_id(
        self,
        manual_agent_id: Optional[str],
        selected_agent_ids: List[str],
        turn_mode: str,
    ) -> Optional[str]:
        if manual_agent_id:
            normalized = manual_agent_id.strip()
            if normalized not in selected_agent_ids:
                raise ValueError("Manual next speaker must be one of the active agents")
            return normalized
        if turn_mode == "manual" and not selected_agent_ids:
            raise ValueError("Manual mode requires at least one active agent")
        return selected_agent_ids[0] if selected_agent_ids else None

    def _resolve_session_agent_ids(
        self,
        session: ChatSessionModel,
        bundle: Optional[RosterBundle] = None,
    ) -> List[str]:
        bundle = bundle or self.bundle_for_session(session)
        raw_ids = session.selected_agent_ids
        if isinstance(raw_ids, str):
            try:
                import json
                raw_ids = json.loads(raw_ids)
            except Exception:
                raw_ids = []
        if not isinstance(raw_ids, list):
            raw_ids = []

        selected_ids = [
            agent_id
            for agent_id in raw_ids
            if agent_id in bundle.allowed_agent_ids
        ]
        if not selected_ids:
            return [persona["id"] for persona in bundle.personas]
        return [persona["id"] for persona in bundle.personas if persona["id"] in selected_ids]

    def _build_continuation_brief(
        self,
        user_message: str,
        history: List[Dict[str, str]],
        memory: Dict[str, Any],
    ) -> str:
        if user_message:
            return user_message

        latest_user_message = next(
            (item["content"] for item in reversed(history) if item.get("role") == "user" and item.get("content")),
            "",
        )
        latest_assistant_message = next(
            (item["content"] for item in reversed(history) if item.get("role") == "assistant" and item.get("content")),
            "",
        )

        parts = []
        if latest_user_message:
            parts.append(f"Original brief: {latest_user_message}")
        if latest_assistant_message:
            parts.append(f"Latest room point: {latest_assistant_message[:260]}")
        if memory.get("summary"):
            parts.append(f"Session context: {str(memory['summary'])[:260]}")
        parts.append("Continue the discussion by reacting to the latest point and moving toward a stronger decision.")
        return "\n".join(parts)

    def _automatic_followup_map(self, stance_mode: str) -> Dict[str, Dict[str, float]]:
        if stance_mode == "support":
            return {
                "strategist": {"sales": 1.0, "marketing": 1.0, "bd": 0.8, "ops": 0.7},
                "sales": {"marketing": 1.0, "bd": 0.8, "strategist": 0.7, "ops": 0.5},
                "marketing": {"sales": 1.0, "bd": 0.9, "strategist": 0.6, "ops": 0.4},
                "bd": {"marketing": 0.9, "sales": 0.8, "strategist": 0.7, "ops": 0.4},
                "ops": {"strategist": 0.9, "sales": 0.5, "marketing": 0.5, "bd": 0.4},
            }
        if stance_mode == "critique":
            return {
                "strategist": {"ops": 1.3, "sales": 0.7, "marketing": 0.5, "bd": 0.4},
                "sales": {"ops": 1.2, "strategist": 0.9, "marketing": 0.4, "bd": 0.4},
                "marketing": {"ops": 1.1, "strategist": 1.0, "sales": 0.5, "bd": 0.3},
                "bd": {"strategist": 1.0, "ops": 0.9, "sales": 0.6, "marketing": 0.4},
                "ops": {"strategist": 0.9, "sales": 0.8, "marketing": 0.7, "bd": 0.6},
            }
        return {
            "strategist": {"sales": 1.1, "marketing": 1.0, "ops": 1.0, "bd": 0.6},
            "sales": {"ops": 1.0, "marketing": 0.8, "strategist": 0.6, "bd": 0.5},
            "marketing": {"sales": 1.0, "bd": 0.9, "strategist": 0.6, "ops": 0.4},
            "bd": {"sales": 0.9, "strategist": 0.8, "marketing": 0.6, "ops": 0.5},
            "ops": {"strategist": 1.2, "sales": 0.7, "marketing": 0.7, "bd": 0.5},
        }

    def _select_next_agent(
        self,
        *,
        session_id: str,
        agents: List[Dict[str, str]],
        history: List[Dict[str, str]],
        memory: Dict[str, Any],
        turn_brief: str,
        stance_mode: str,
    ) -> tuple[Dict[str, str], str]:
        focus_text = turn_brief.lower()
        latest_assistant_text = next(
            (item["content"] for item in reversed(history) if item.get("role") == "assistant" and item.get("content")),
            "",
        )
        if latest_assistant_text:
            focus_text = f"{focus_text}\n{latest_assistant_text.lower()}"

        recent_assistant_ids = [
            item["agent_id"]
            for item in history
            if item.get("role") == "assistant" and item.get("agent_id")
        ][-8:]
        last_speaker = recent_assistant_ids[-1] if recent_assistant_ids else None
        open_questions = " ".join(str(item) for item in (memory.get("open_questions") or [])).lower()
        key_points = " ".join(str(item) for item in (memory.get("key_points") or [])).lower()
        followup_map = self._automatic_followup_map(stance_mode)

        scored: List[tuple[float, Dict[str, str], str]] = []
        for persona in agents:
            persona_id = persona["id"]
            name_tokens = persona["name"].lower().replace("/", " ").split()
            role_tokens = persona["role"].lower().replace("+", " ").replace("/", " ").split()
            score = 1.0

            if persona_id in focus_text:
                score += 2.4
            if any(token in focus_text for token in name_tokens):
                score += 1.2
            if any(token in focus_text for token in role_tokens[:4]):
                score += 0.8
            if persona_id in open_questions or any(token in open_questions for token in role_tokens[:2]):
                score += 0.6
            if persona_id in key_points or any(token in key_points for token in role_tokens[:2]):
                score += 0.5
            if last_speaker:
                score += followup_map.get(last_speaker, {}).get(persona_id, 0.0)

            if stance_mode == "critique" and persona_id in {"ops", "strategist"}:
                score += 0.5
            if stance_mode == "support" and persona_id in {"sales", "marketing", "bd"}:
                score += 0.3

            if recent_assistant_ids and persona_id in recent_assistant_ids:
                reverse_idx = next(
                    (index for index, agent_id in enumerate(reversed(recent_assistant_ids), start=1) if agent_id == persona_id),
                    9,
                )
                if reverse_idx == 1:
                    score -= 50.0  # Heavily penalize back-to-back turns by the same advisor
                else:
                    score -= max(0.0, 2.0 - (reverse_idx * 0.35))

            jitter_seed = f"{session_id}:{persona_id}:{turn_brief}:{stance_mode}:{len(history)}"
            score += (sum(ord(ch) for ch in jitter_seed) % 17) / 100.0

            if last_speaker:
                if stance_mode == "critique":
                    reason = f"{persona['name']} was chosen to pressure-test {self._persona_name(last_speaker)}'s latest point."
                elif stance_mode == "support":
                    reason = f"{persona['name']} was chosen to reinforce {self._persona_name(last_speaker)}'s latest point."
                else:
                    reason = f"{persona['name']} was chosen to advance the room after {self._persona_name(last_speaker)}."
            elif persona_id in focus_text or any(token in focus_text for token in name_tokens):
                reason = f"{persona['name']} matched the current brief best."
            else:
                reason = f"{persona['name']} adds a {persona['role'].lower()} angle to the discussion."

            scored.append((score, persona, reason))

        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            raise ValueError("No active agent available to respond")
        return scored[0][1], scored[0][2]

    @staticmethod
    def _last_assistant_speaker(history: List[Dict[str, str]]) -> Optional[str]:
        for item in reversed(history):
            if item.get("role") == "assistant" and item.get("agent_id"):
                return str(item["agent_id"])
        return None

    def _recent_history(self, session_id: str, workspace_id: str) -> List[Dict[str, str]]:
        with get_session() as db:
            rows = (
                db.execute(
                    select(ChatMessageModel)
                    .where(
                        ChatMessageModel.session_id == session_id,
                        ChatMessageModel.workspace_id == workspace_id,
                    )
                    .order_by(desc(ChatMessageModel.created_at))
                    .limit(self.max_history_messages * 2)
                )
                .scalars()
                .all()
            )
        rows = list(reversed(rows))
        messages = [
            {"role": row.role, "agent_id": row.agent_id, "content": row.content}
            for row in rows
        ]
        return self._trim_history_by_tokens(messages, self.max_history_tokens)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)

    def _trim_history_by_tokens(self, history: List[Dict[str, str]], budget: int) -> List[Dict[str, str]]:
        kept: List[Dict[str, str]] = []
        total = 0
        for item in reversed(history):  # iterate from latest backwards
            cost = self._estimate_tokens(item["content"])
            if total + cost > budget:
                break
            kept.append(item)
            total += cost
        return list(reversed(kept))

    def _call_model(
        self,
        persona: Dict[str, str],
        history: List[Dict[str, str]],
        memory: Dict[str, Any],
        user_message: str,
        prior_speaker_id: Optional[str] = None,
        stance_mode: str = "neutral",
        selection_reason: Optional[str] = None,
        is_summary: bool = False,
        bundle: Optional[RosterBundle] = None,
    ) -> str:
        """Call Groq model or deterministic fallback."""
        bundle = bundle or self.default_bundle
        system_prompt = self._build_system_prompt(
            persona,
            memory,
            is_summary=is_summary,
            prior_speaker_id=prior_speaker_id,
            stance_mode=stance_mode,
            selection_reason=selection_reason,
            bundle=bundle,
            assistant_turns=sum(1 for m in history if m.get("role") == "assistant"),
        )
        history_text = self._format_history(history[-8:])  # keep prompt lean
        stance_rule = {
            "support": "Take a support stance: strengthen the strongest point and add execution detail.",
            "critique": "Take a critique stance: respectfully challenge assumptions and propose a stronger alternative.",
            "neutral": "Take a neutral stance: weigh tradeoffs and move the room forward.",
        }
        user_prompt = textwrap.dedent(
            f"""
            Turn brief:
            {user_message}

            Meeting rules:
            - Sound like a real participant in a live room, not a generic assistant.
            - React to the latest relevant speaker before adding your own point.
            - Keep concrete and measurable output.
            - End with one next action including owner and timeline.
            - {stance_rule.get(stance_mode, stance_rule["neutral"])}
            """
        ).strip()
        if prior_speaker_id:
            user_prompt += f"\n- Last speaker: {prior_speaker_id}"
        if selection_reason:
            user_prompt += f"\n- Why you were selected: {selection_reason}"
        prompt = "\n\n".join(part for part in [system_prompt, history_text, user_prompt] if part)

        try:
            return self.llm_client.generate(
                system_prompt=system_prompt,
                user_prompt=prompt,
                temperature=0.5 if not is_summary else 0.3,
                max_tokens=5000,
            ).strip()
        except Exception as exc:
            logger.warning("Unified LLM call failed: %s", exc)

        return self._build_fallback_response(
            persona,
            history,
            memory,
            user_message,
            prior_speaker_id=prior_speaker_id,
            stance_mode=stance_mode,
            is_summary=is_summary,
        )

    def _build_fallback_response(
        self,
        persona: Dict[str, str],
        history: List[Dict[str, str]],
        memory: Dict[str, Any],
        user_message: str,
        *,
        prior_speaker_id: Optional[str],
        stance_mode: str,
        is_summary: bool,
    ) -> str:
        topic = " ".join(user_message.split())
        if len(topic) > 160:
            topic = topic[:157].rstrip() + "..."

        prior_label = self._persona_name(prior_speaker_id) if prior_speaker_id else "the room"
        latest_points = [
            item["content"][:120].strip()
            for item in history
            if item.get("role") == "assistant"
        ][-2:]
        memory_anchor = ""
        if memory.get("key_points"):
            memory_anchor = str(memory["key_points"][0])[:110]

        if is_summary:
            alignment = latest_points[-1] if latest_points else "Team agrees on fast tests before heavy investment."
            tension = memory.get("open_questions", [])
            tension_line = tension[0] if tension else "Main tension is speed vs execution capacity."
            return "\n".join(
                [
                    f"• Alignment: {alignment}",
                    f"• Tension: {tension_line}",
                    "• Next action: Strategist and Ops lock one weekly scorecard and owners by tomorrow.",
                ]
            )

        agenda_map = {
            "ceo": "provide executive vision, clear priorities, and overall company leadership",
            "strategist": "prioritize the highest-leverage path",
            "sales": "convert interest into qualified meetings quickly",
            "marketing": "turn the message into repeatable campaign experiments",
            "bd": "find leverage through partnerships and channel distribution",
            "ops": "keep plan feasible with clear owners and constraints",
            "moderator": "keep the room aligned and focused on decisions",
        }
        agenda = agenda_map.get(persona["id"], "add practical direction")

        if stance_mode == "critique":
            reaction = f"Respectfully challenging {prior_label}, the weakest assumption is \"{topic}\"."
            if memory_anchor:
                reaction = f"Respectfully challenging {prior_label}, we should stress-test this point: {memory_anchor}."
        elif stance_mode == "support":
            reaction = f"Building on {prior_label}, the strongest thread to double down on is \"{topic}\"."
            if memory_anchor:
                reaction = f"Building on {prior_label}, we should compound this point: {memory_anchor}."
        else:
            reaction = f"Building on {prior_label}, I think the core issue is \"{topic}\"."
            if memory_anchor:
                reaction = f"Building on {prior_label}, we should protect this point: {memory_anchor}."

        return "\n".join(
            [
                f"• Reaction: {reaction}",
                f"• {persona['name']} agenda: I will {agenda} with one measurable signal this week.",
                "• Next action: Owner for this thread drafts a 7-day test plan with metric baseline before EOD.",
            ]
        )

    def _build_system_prompt(
        self,
        persona: Dict[str, str],
        memory: Dict[str, Any],
        *,
        is_summary: bool,
        prior_speaker_id: Optional[str],
        stance_mode: str,
        selection_reason: Optional[str],
        bundle: Optional[RosterBundle] = None,
        assistant_turns: int = 0,
    ) -> str:
        bundle = bundle or self.default_bundle
        memory_lines = []
        if memory.get("summary"):
            memory_lines.append(f"Conversation summary: {memory['summary']}")
        if memory.get("key_points"):
            memory_lines.append("Key points: " + "; ".join(memory["key_points"]))
        if memory.get("open_questions"):
            memory_lines.append("Open questions: " + "; ".join(memory["open_questions"]))

        memory_block = "\n".join(memory_lines)

        # Guardrails go in before the persona, not after, and they go to EVERY
        # seat rather than only the chair: an advisor without the boundary can
        # breach it on its own turn, and the chair only sees that afterwards.
        guardrail_block = bundle.guardrails.get("prompt_block", "")
        if is_summary and bundle.guardrails.get("moderator_addendum"):
            guardrail_block = (
                f"{guardrail_block}\n{bundle.guardrails['moderator_addendum']}".strip()
            )

        base = textwrap.dedent(
            f"""
            Persona: {persona['name']} — {persona['role']}
            Tone: {persona['tone']}
            Role directive: {persona.get('system_prompt', '')}
            Behaviors:
            - Provide thorough, detailed, and comprehensive responses whenever requested by the user or required by complex topics (e.g. technical specs, feature lists, financial models, legal analysis, or execution roadmaps).
            - Give concrete, high-signal, actionable executive recommendations.
            - If data is missing, explicitly state assumptions first.
            - Reference another speaker when relevant.
            - Avoid placeholder text and generic platitudes.
            - Use structured bullet formatting or sections for clarity.
            """
        ).strip()

        if guardrail_block:
            base = f"{guardrail_block}\n\n{base}"

        # The phase ladder is per-pack data. It used to be a hardcoded product
        # ladder warning every advisor off "dev timelines and AWS choices",
        # which is the wrong nudge in a clinical or drug-development debate.
        phase = format_phase(assistant_turns, bundle.ladder)
        focus_warning = bundle.ladder.get(
            "focus_warning",
            "Do NOT jump ahead to later phases before this one is settled.",
        )
        base += f"\n\nCURRENT BOARDROOM PHASE:\n{phase}\n{focus_warning}"

        if is_summary:
            base += "\nYou are summarizing the room: capture alignment, tension, and next action."
        elif prior_speaker_id:
            base += f"\nThe previous speaker was: {self._persona_name(prior_speaker_id)}."
            if stance_mode == "support":
                base += "\nOpen by reinforcing the previous point before extending it."
            elif stance_mode == "critique":
                base += "\nOpen by challenging the previous point respectfully before proposing a stronger path."
            else:
                base += "\nOpen by acknowledging or challenging the previous point before your recommendation."

        if not is_summary:
            if stance_mode == "support":
                base += "\nStance: support. Strengthen the most promising idea and add practical detail."
            elif stance_mode == "critique":
                base += "\nStance: critique. Surface risks, broken assumptions, and a better alternative."
            else:
                base += "\nStance: neutral. Balance tradeoffs and move the conversation toward a decision."
            if selection_reason:
                base += f"\nSelection context: {selection_reason}"

        if memory_block:
            base += f"\nContext:\n{memory_block}"
        return base

    def _persona_name(self, agent_id: Optional[str]) -> str:
        """
        Display name for any seat in the org, not just the deployment default.

        A transcript can name a seat from a pack this deployment does not
        serve by default -- rendering a raw id like `vbc_retro` in prose is a
        worse failure than resolving it across the whole roster.
        """
        if not agent_id:
            return "another participant"
        persona = next((p for p in self._all_seats() if p["id"] == agent_id), None)
        return persona["name"] if persona else agent_id

    def _all_seats(self) -> List[Dict[str, Any]]:
        if self.__all_seats is None:
            self.__all_seats = load_personas(available_packs())
        return self.__all_seats

    def _format_history(self, history: List[Dict[str, str]]) -> str:
        if not history:
            return ""
        lines = []
        for item in history:
            speaker = item["agent_id"] or item["role"]
            lines.append(f"{speaker}: {item['content']}")
        return "Recent discussion:\n" + "\n".join(lines)

    def _update_memory(self, session_id: str, workspace_id: str) -> None:
        history = self._recent_history(session_id, workspace_id)
        summary = self._build_memory_summary(history)

        with get_session() as db:
            row = (
                db.execute(
                    select(ChatSessionModel)
                    .where(ChatSessionModel.id == session_id, ChatSessionModel.workspace_id == workspace_id)
                )
                .scalars()
                .first()
            )
            if row is None:
                return
            row.memory_summary = summary.get("summary")
            row.memory_key_points = summary.get("key_points", [])
            row.memory_open_questions = summary.get("open_questions", [])
            row.updated_at = utc_now()
            db.add(row)
            db.flush()

    def _build_memory_summary(self, history: List[Dict[str, str]]) -> Dict[str, Any]:
        if not history:
            return {"summary": None, "key_points": [], "open_questions": []}

        try:
            prompt = (
                "You are the Executive Board Chair. Analyze the boardroom debate transcript above.\n"
                "Extract structured executive insights under these exact section headers:\n"
                "### CONSENSUS\n(Provide a concise 2-3 sentence executive summary of agreed direction)\n\n"
                "### KEY DECISIONS\n- Decision point 1\n- Decision point 2\n- Decision point 3\n\n"
                "### OPEN RISKS\n- Risk/Question 1\n- Risk/Question 2\n- Risk/Question 3"
            )
            formatted_lines = [
                f"{msg.get('agent_id', msg.get('role', 'user')).upper()}: {msg.get('content', '')}"
                for msg in history
            ]
            raw = self.llm_client.generate_with_sliding_window(
                system_prompt=prompt,
                items=formatted_lines,
                window_size=10,
                overlap=3,
                temperature=0.3,
                max_tokens=900,
            )

            consensus_parts = []
            key_points = []
            open_questions = []

            current_section = None
            for line in raw.splitlines():
                line_str = line.strip()
                if not line_str:
                    continue
                upper_line = line_str.upper()
                if "CONSENSUS" in upper_line:
                    current_section = "consensus"
                    continue
                elif "KEY DECISION" in upper_line or "KEY POINT" in upper_line:
                    current_section = "key_points"
                    continue
                elif "OPEN RISK" in upper_line or "QUESTION" in upper_line:
                    current_section = "open_questions"
                    continue

                clean_text = line_str.lstrip("-*# ").strip()
                if not clean_text:
                    continue

                if current_section == "consensus":
                    consensus_parts.append(clean_text)
                elif current_section == "key_points":
                    key_points.append(clean_text)
                elif current_section == "open_questions":
                    open_questions.append(clean_text)

            summary_text = " ".join(consensus_parts).strip()
            if not summary_text:
                summary_text = raw[:350]

            return {
                "summary": summary_text[:600],
                "key_points": key_points if key_points else [line.lstrip("-*# ") for line in raw.splitlines() if line.strip()][:3],
                "open_questions": open_questions,
            }
        except Exception as e:
            logger.warning(f"Memory summary generation failed: {e}")

        # fallback heuristic
        takeaways = [item["content"][:120] for item in history[-3:]]
        return {
            "summary": " | ".join(takeaways),
            "key_points": takeaways,
            "open_questions": [],
        }

    def _serialize_session(self, session: ChatSessionModel) -> Dict[str, Any]:
        bundle = self.bundle_for_session(session)
        selected_agent_ids = self._resolve_session_agent_ids(session, bundle)
        turn_mode = self._normalize_turn_mode(session.turn_mode or "automatic")
        stance_mode = self._normalize_stance_mode(session.stance_mode or "neutral")
        manual_agent_id = self._normalize_manual_agent_id(session.manual_agent_id, selected_agent_ids, turn_mode)
        return {
            "id": session.id,
            "workspace_id": session.workspace_id,
            "created_by": session.created_by,
            "mission_id": session.mission_id,
            "title": session.title,
            "status": session.status,
            "selected_agent_ids": selected_agent_ids,
            "persona_packs": list(bundle.packs),
            "ladder": {"id": bundle.ladder.get("id"), "label": bundle.ladder.get("label")},
            "disclaimer": bundle.guardrails.get("disclaimer", ""),
            "turn_mode": turn_mode,
            "stance_mode": stance_mode,
            "manual_agent_id": manual_agent_id,
            "memory_summary": session.memory_summary,
            "memory_key_points": session.memory_key_points or [],
            "memory_open_questions": session.memory_open_questions or [],
            "action_items": getattr(session, "action_items", []) or [],
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }

    @staticmethod
    def _serialize_message(message: ChatMessageModel) -> Dict[str, Any]:
        return {
            "id": message.id,
            "session_id": message.session_id,
            "workspace_id": message.workspace_id,
            "role": message.role,
            "content": message.content,
            "agent_id": message.agent_id,
            "metadata": message.message_meta,
            "created_at": message.created_at,
        }


chat_service = ChatService()

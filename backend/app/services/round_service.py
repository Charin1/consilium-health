"""
Async "Convene the board" rounds.

Before this, convening a board was one blocking HTTP request: the client sent
a brief and waited for every seat's turn plus the chair's recap to finish
before anything came back. Nothing was watchable mid-round, and nothing could
be stopped -- the frontend had no handle on work that only existed inside a
request it was still waiting on.

This mirrors `job_service.py`'s contract for a different shape of work: a
round is not one seat's output, it is the whole debate, tracked in
`RoundJobModel` with a live `current_speaker` and turn count, and a
`cancel_requested` flag the round polls between turns.

The same three properties matter here as they did for seat jobs:

1. **Terminal states are guaranteed.** The exception path finalizes the row as
   `failed` before re-raising.
2. **A restart reaps orphans.** Threads do not survive a process exit.
3. **Validation runs before enqueueing.** `chat_service.prepare_convene` is
   called synchronously, before the job row exists, so a rejected brief never
   creates a permanently failed round.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import desc, select

from app.db.database import get_session
from app.db.models import RoundJobModel
from app.services.workflow_contracts import utc_now

logger = logging.getLogger("consilium.rounds")

QUEUED = "queued"
RUNNING = "running"
DELIVERED = "delivered"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = {DELIVERED, FAILED, CANCELLED}

LABEL_QUEUED = "Waiting to convene"
LABEL_STOPPING = "Stopping after the current speaker"


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def serialize(job: RoundJobModel) -> Dict[str, Any]:
    return {
        "id": job.id,
        "session_id": job.session_id,
        "status": job.status,
        "progress_label": job.progress_label,
        "cancel_requested": job.cancel_requested,
        "current_speaker": job.current_speaker,
        "turn_index": job.turn_index,
        "turn_total": job.turn_total,
        "turns_delivered": job.turns_delivered,
        "error_class": job.error_class,
        "error_detail": job.error_detail,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
        "is_terminal": job.status in TERMINAL,
    }


def enqueue(*, workspace_id: str, session_id: str) -> Dict[str, Any]:
    """Record the round as queued and return it. Nothing has run yet."""
    job = RoundJobModel(
        id=str(uuid4()),
        workspace_id=workspace_id,
        session_id=session_id,
        status=QUEUED,
        progress_label=LABEL_QUEUED,
        created_at=utc_now(),
    )
    with get_session() as db:
        db.add(job)
        db.flush()
    logger.info("round %s queued for session %s", job.id, session_id)
    return serialize(job)


def update(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Persist a step immediately, so a poller sees it as it happens."""
    with get_session() as db:
        job = db.execute(
            select(RoundJobModel).where(RoundJobModel.id == job_id)
        ).scalar_one_or_none()
        if job is None:
            return None
        for key, value in fields.items():
            setattr(job, key, value)
        db.add(job)
        db.flush()
        return serialize(job)


def get(job_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
    with get_session() as db:
        job = db.execute(
            select(RoundJobModel).where(
                RoundJobModel.id == job_id,
                RoundJobModel.workspace_id == workspace_id,
            )
        ).scalar_one_or_none()
        return serialize(job) if job else None


def get_for_session(session_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
    """The most recent round for a session, terminal or not."""
    with get_session() as db:
        job = db.execute(
            select(RoundJobModel)
            .where(
                RoundJobModel.session_id == session_id,
                RoundJobModel.workspace_id == workspace_id,
            )
            .order_by(desc(RoundJobModel.created_at))
            .limit(1)
        ).scalar_one_or_none()
        return serialize(job) if job else None


def list_active(workspace_id: str) -> List[Dict[str, Any]]:
    with get_session() as db:
        rows = db.execute(
            select(RoundJobModel).where(
                RoundJobModel.workspace_id == workspace_id,
                RoundJobModel.status.in_([QUEUED, RUNNING]),
            )
        ).scalars().all()
        return [serialize(r) for r in rows]


def request_cancel(job_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
    """
    Ask a running round to stop after the current speaker finishes.

    A no-op on a job that is already terminal, rather than an error: pressing
    Stop on a round that just delivered is a race the user will lose sometimes,
    and it should read as "nothing to stop", not as a failure.
    """
    job = get(job_id, workspace_id)
    if job is None or job["is_terminal"]:
        return job
    return update(job_id, cancel_requested=True, progress_label=LABEL_STOPPING)


def _should_stop(job_id: str, workspace_id: str):
    """A closure `chat_service` polls once per turn, never mid-generation."""
    def check() -> bool:
        current = get(job_id, workspace_id)
        return bool(current and current["cancel_requested"])
    return check


def run_round(
    job_id: str,
    *,
    workspace_id: str,
    session_id: str,
    history: List[Dict[str, Any]],
    normalized_message: str,
    continue_dialogue: bool,
) -> Dict[str, Any]:
    """
    Execute a round, reporting progress and guaranteeing a terminal state.

    Imports `chat_service` lazily: `chat_service` does not import this module,
    but keeping the dependency one-directional in the import graph (services
    depend down, not sideways) avoids a future circular import if that ever
    changes.
    """
    from app.services.chat_service import chat_service

    started = time.monotonic()
    update(job_id, status=RUNNING, started_at=utc_now())

    def on_turn(message: Dict[str, Any]) -> None:
        meta = message.get("metadata") or {}
        turn_index = meta.get("turn_index", 0)
        turn_total = meta.get("turn_total")
        speaker = meta.get("persona", message.get("agent_id", "A seat"))
        update(
            job_id,
            current_speaker=message.get("agent_id"),
            turn_index=turn_index,
            turn_total=turn_total,
            turns_delivered=turn_index,
            progress_label=f"{speaker} spoke ({turn_index}/{turn_total or '?'})",
        )

    try:
        replies = chat_service.run_convene_round(
            session_id=session_id,
            workspace_id=workspace_id,
            history=history,
            normalized_message=normalized_message,
            continue_dialogue=continue_dialogue,
            should_stop=_should_stop(job_id, workspace_id),
            on_turn=on_turn,
        )
    except Exception as exc:
        logger.exception("round %s failed", job_id)
        update(
            job_id,
            status=FAILED,
            progress_label="Could not finish the round",
            error_class=exc.__class__.__name__,
            error_detail=str(exc)[:2000],
            completed_at=utc_now(),
        )
        raise

    cancelled = bool(get(job_id, workspace_id)["cancel_requested"])
    final = update(
        job_id,
        status=CANCELLED if cancelled else DELIVERED,
        progress_label="Stopped" if cancelled else "Debate complete",
        completed_at=utc_now(),
    )
    logger.info(
        "round %s %s: %d turn(s) in %dms",
        job_id, final["status"], len(replies), int((time.monotonic() - started) * 1000),
    )
    return final


def spawn(job_id: str, **kwargs: Any) -> None:
    """Run a round on a background thread. The thread swallows the exception
    `run_round` re-raises: the row is already finalized as `failed`."""
    def target() -> None:
        # A new thread starts with no active OTel span, so without this every
        # DB query and LLM call inside the round becomes its own *root* trace
        # (see docs/architecture.md 6a). That flooded Tempo with hundreds of
        # one-span `connect`/`SELECT` traces and pushed the interesting
        # `llm.call` ones off the end of any unfiltered search. One root span
        # per round means one trace per round, with the seat turns and their
        # queries nested underneath where they belong.
        from app.services.telemetry import span

        try:
            with span("round.run", **{"round.job_id": job_id}):
                run_round(job_id, **kwargs)
        except Exception:
            pass  # already recorded on the job row

    threading.Thread(target=target, name=f"round-{job_id[:8]}", daemon=True).start()


def reap_orphans(workspace_id: Optional[str] = None) -> int:
    """Fail rounds left mid-flight by a restart, same as `job_service`."""
    with get_session() as db:
        query = select(RoundJobModel).where(RoundJobModel.status.in_([QUEUED, RUNNING]))
        if workspace_id:
            query = query.where(RoundJobModel.workspace_id == workspace_id)
        rows = db.execute(query).scalars().all()
        for job in rows:
            job.status = FAILED
            job.progress_label = "Interrupted by a restart"
            job.error_class = "ServerRestart"
            job.completed_at = utc_now()
            db.add(job)
        if rows:
            db.flush()
            logger.warning("reaped %d round(s) orphaned by a restart", len(rows))
        return len(rows)

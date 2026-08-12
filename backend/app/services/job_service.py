"""
Async work for seats.

The floor view asks a question the synchronous API cannot answer: *what is this
person doing right now?* Answering it means the work has to be observable while
it runs, not only after it finishes. So:

1. `enqueue` writes a `queued` row and returns immediately. The caller gets a
   job id, not a wait.
2. A background worker moves the row through `running` with a live
   `progress_label`, persisted **as each step happens**.
3. Terminal states are guaranteed. The exception path finalizes the row as
   `failed` with the error class before re-raising -- a floor showing a seat
   thinking forever about a job that died is worse than one showing an error.

(`.agents/skills/engineering/backend.md` §2, the async-work contract.)

Background jobs open their own database session. Borrowing a request's session
means the work outlives the connection that owns it.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import desc, select

from app.db.database import get_session
from app.db.models import SeatJobModel
from app.services.workflow_contracts import utc_now

logger = logging.getLogger("consilium.jobs")

QUEUED = "queued"
RUNNING = "running"
DELIVERED = "delivered"
FAILED = "failed"
TERMINAL = {DELIVERED, FAILED}

# Labels the floor shows over a seat's head. Short, present-tense, and about
# the work rather than the machinery -- "Reading the room" beats "invoking LLM".
LABEL_QUEUED = "Waiting to start"
LABEL_READING = "Reading the brief"
LABEL_WORKING = "Working on it"
LABEL_WRITING = "Writing it up"
LABEL_DONE = "Delivered"


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def serialize(job: SeatJobModel) -> Dict[str, Any]:
    return {
        "id": job.id,
        "session_id": job.session_id,
        "seat_id": job.seat_id,
        "kind": job.kind,
        "status": job.status,
        "progress_label": job.progress_label,
        "brief": job.brief,
        "priority": job.priority,
        "result_message_id": job.result_message_id,
        "action_item_id": job.action_item_id,
        "error_class": job.error_class,
        "error_detail": job.error_detail,
        "provider": job.provider,
        "model": job.model,
        "duration_ms": job.duration_ms,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
        "is_terminal": job.status in TERMINAL,
    }


def enqueue(
    *,
    workspace_id: str,
    session_id: str,
    seat_id: str,
    brief: str,
    priority: str = "Medium",
    kind: str = "task",
) -> Dict[str, Any]:
    """Record the job as queued and return it. Nothing has run yet."""
    job = SeatJobModel(
        id=str(uuid4()),
        workspace_id=workspace_id,
        session_id=session_id,
        seat_id=seat_id,
        kind=kind,
        status=QUEUED,
        progress_label=LABEL_QUEUED,
        brief=brief,
        priority=priority,
        created_at=utc_now(),
    )
    with get_session() as db:
        db.add(job)
        db.flush()
    logger.info("job %s queued for %s", job.id, seat_id)
    return serialize(job)


def update(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """
    Persist a step immediately.

    Buffering these until the end would make the whole point moot: progress
    that is only written on completion is not progress, it is a result.
    """
    with get_session() as db:
        job = db.execute(
            select(SeatJobModel).where(SeatJobModel.id == job_id)
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
            select(SeatJobModel).where(
                SeatJobModel.id == job_id,
                SeatJobModel.workspace_id == workspace_id,
            )
        ).scalar_one_or_none()
        return serialize(job) if job else None


def list_jobs(
    *,
    workspace_id: str,
    session_id: Optional[str] = None,
    active_only: bool = False,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Recent jobs, newest first.

    The floor polls this. `active_only` is what it uses on a busy floor: the
    seats that are working are the whole signal, and shipping a thousand
    finished rows to draw four busy desks is the loop that gets expensive.
    """
    with get_session() as db:
        query = select(SeatJobModel).where(SeatJobModel.workspace_id == workspace_id)
        if session_id:
            query = query.where(SeatJobModel.session_id == session_id)
        if active_only:
            query = query.where(SeatJobModel.status.in_([QUEUED, RUNNING]))
        rows = db.execute(
            query.order_by(desc(SeatJobModel.created_at)).limit(limit)
        ).scalars().all()
        return [serialize(row) for row in rows]


def run_job(job_id: str, work: Callable[[Callable[[str], None]], Dict[str, Any]]) -> Dict[str, Any]:
    """
    Execute a job, reporting progress and guaranteeing a terminal state.

    `work` is handed a `report(label)` callback and returns a dict that may
    carry `message_id`, `action_item_id`, `provider`, and `model`.

    Runs in the calling thread. `spawn` puts it on a background one.
    """
    started = time.monotonic()
    update(job_id, status=RUNNING, progress_label=LABEL_READING, started_at=utc_now())

    def report(label: str) -> None:
        update(job_id, progress_label=label)

    try:
        outcome = work(report) or {}
    except Exception as exc:
        # The failure path finalizes BEFORE re-raising. A job that dies without
        # writing a terminal row leaves the floor showing a seat that never
        # stops thinking.
        logger.exception("job %s failed", job_id)
        update(
            job_id,
            status=FAILED,
            progress_label="Could not finish",
            error_class=exc.__class__.__name__,
            error_detail=str(exc)[:2000],
            completed_at=utc_now(),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise

    final = update(
        job_id,
        status=DELIVERED,
        progress_label=LABEL_DONE,
        result_message_id=outcome.get("message_id"),
        action_item_id=outcome.get("action_item_id"),
        provider=outcome.get("provider"),
        model=outcome.get("model"),
        completed_at=utc_now(),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    logger.info("job %s delivered in %sms", job_id, final and final.get("duration_ms"))
    return final or {}


def spawn(job_id: str, work: Callable[[Callable[[str], None]], Dict[str, Any]]) -> None:
    """
    Run a job on a background thread.

    A thread rather than `BackgroundTasks` because the work must survive the
    response, and rather than a task queue because adding Redis and a worker
    process to run four LLM calls is not a trade this earns yet. The swap point
    is here and nowhere else.

    The thread swallows the exception `run_job` re-raises: the row is already
    finalized as `failed`, and an unhandled exception on a daemon thread only
    prints a traceback nobody reads.
    """
    def target() -> None:
        try:
            run_job(job_id, work)
        except Exception:
            pass  # already recorded on the job row

    threading.Thread(target=target, name=f"seat-job-{job_id[:8]}", daemon=True).start()


def reap_orphans(workspace_id: Optional[str] = None) -> int:
    """
    Fail jobs left mid-flight by a restart.

    Threads do not survive a process exit, so any row still `queued` or
    `running` at startup belongs to work that is never coming back. Leaving
    them is how a floor ends up with permanently busy ghosts.
    """
    with get_session() as db:
        query = select(SeatJobModel).where(SeatJobModel.status.in_([QUEUED, RUNNING]))
        if workspace_id:
            query = query.where(SeatJobModel.workspace_id == workspace_id)
        rows = db.execute(query).scalars().all()
        for job in rows:
            job.status = FAILED
            job.progress_label = "Interrupted by a restart"
            job.error_class = "ServerRestart"
            job.completed_at = utc_now()
            db.add(job)
        if rows:
            db.flush()
            logger.warning("reaped %d job(s) orphaned by a restart", len(rows))
        return len(rows)

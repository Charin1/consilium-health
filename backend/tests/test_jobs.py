"""
Async seat work.

The floor view's whole premise is "what is this person doing *right now*", so
the tests here are about observability while work runs, not about the result:

- progress is persisted as it happens, not buffered until the end
- a terminal state is guaranteed even when the work explodes
- a restart does not leave permanently busy desks
- one tenant cannot read another's jobs
"""
import threading

import pytest
from fastapi.testclient import TestClient

from app.services import job_service
from app.services.chat_service import ChatService
from main import app

client = TestClient(app)

WS = "w-jobs"


@pytest.fixture
def service():
    return ChatService(packs=["core"])


@pytest.fixture
def session(service):
    return service.create_session(
        workspace_id=WS, created_by="u", mission_id=None,
        title="job tests", selected_agent_ids=["finance", "strategist"],
    )


def queue(session, seat_id="finance", brief="Do the thing."):
    return job_service.enqueue(
        workspace_id=WS, session_id=session["id"], seat_id=seat_id, brief=brief,
    )


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------

def test_enqueue_returns_before_any_work_happens(session):
    job = queue(session)
    assert job["status"] == "queued"
    assert job["is_terminal"] is False
    assert job["started_at"] is None
    assert job["result_message_id"] is None


def test_progress_is_persisted_as_it_happens_not_at_the_end(session):
    """
    Progress written only on completion is not progress, it is a result. The
    floor would show "queued" for thirty seconds and then jump to done.
    """
    job = queue(session)
    seen = []

    def work(report):
        seen.append(job_service.get(job["id"], WS)["status"])
        report("Reading the claims file")
        seen.append(job_service.get(job["id"], WS)["progress_label"])
        report("Running the numbers")
        seen.append(job_service.get(job["id"], WS)["progress_label"])
        return {"message_id": "m-1"}

    job_service.run_job(job["id"], work)
    assert seen == ["running", "Reading the claims file", "Running the numbers"]


def test_a_delivered_job_records_what_produced_it(session):
    job = queue(session)
    job_service.run_job(job["id"], lambda report: {
        "message_id": "m-42", "provider": "groq", "model": "llama-3.3-70b-versatile",
    })
    final = job_service.get(job["id"], WS)
    assert final["status"] == "delivered"
    assert final["result_message_id"] == "m-42"
    assert final["model"] == "llama-3.3-70b-versatile"
    assert final["duration_ms"] is not None
    assert final["is_terminal"] is True


def test_a_job_that_explodes_still_reaches_a_terminal_state(session):
    """
    The one that matters. A job dying without finalizing leaves a desk on the
    floor thinking forever about work that ended ten minutes ago.
    """
    job = queue(session)

    def work(report):
        report("Halfway there")
        raise RuntimeError("the provider hung up")

    with pytest.raises(RuntimeError):
        job_service.run_job(job["id"], work)

    final = job_service.get(job["id"], WS)
    assert final["status"] == "failed"
    assert final["is_terminal"] is True
    assert final["error_class"] == "RuntimeError"
    assert "hung up" in final["error_detail"]
    assert final["completed_at"] is not None


def test_a_spawned_job_finishes_on_its_own_thread(session):
    job = queue(session)
    done = threading.Event()

    def work(report):
        report("Working")
        done.set()
        return {"message_id": "m-async"}

    job_service.spawn(job["id"], work)
    assert done.wait(timeout=5), "the background job never ran"
    for _ in range(50):
        if job_service.get(job["id"], WS)["is_terminal"]:
            break
        threading.Event().wait(0.05)
    assert job_service.get(job["id"], WS)["status"] == "delivered"


def test_a_spawned_failure_does_not_escape_the_thread(session):
    """The row records it; an unhandled daemon-thread traceback helps nobody."""
    job = queue(session)
    job_service.spawn(job["id"], lambda report: (_ for _ in ()).throw(ValueError("nope")))
    for _ in range(60):
        if job_service.get(job["id"], WS)["is_terminal"]:
            break
        threading.Event().wait(0.05)
    assert job_service.get(job["id"], WS)["status"] == "failed"


# --------------------------------------------------------------------------
# Restart
# --------------------------------------------------------------------------

def test_a_restart_reaps_jobs_it_can_never_finish(session):
    """
    Threads do not survive a process exit. Those rows must not stay busy.

    Own workspace: a job another test spawned can still be mid-flight, and it
    would set itself back to `running` after the reap. That is correct
    behaviour for a live thread and a false failure here.
    """
    restart_ws = "w-restart"
    for seat in ("finance", "strategist"):
        job_service.enqueue(
            workspace_id=restart_ws, session_id=session["id"],
            seat_id=seat, brief="Interrupted work.",
        )

    assert job_service.reap_orphans(workspace_id=restart_ws) == 2

    for job in job_service.list_jobs(workspace_id=restart_ws):
        assert job["is_terminal"], "a job survived the reap still busy"
        assert job["error_class"] == "ServerRestart"
        assert job["progress_label"] == "Interrupted by a restart"


def test_the_reap_leaves_other_workspaces_alone(session):
    other = job_service.enqueue(
        workspace_id="w-untouched", session_id=session["id"],
        seat_id="finance", brief="Not yours.",
    )
    job_service.reap_orphans(workspace_id="w-restart")
    assert job_service.get(other["id"], "w-untouched")["status"] == "queued"


# --------------------------------------------------------------------------
# Reading them back
# --------------------------------------------------------------------------

def test_active_only_excludes_finished_work(session):
    job_service.reap_orphans(workspace_id=WS)
    running = queue(session, brief="Still going.")
    finished = queue(session, seat_id="strategist", brief="Done.")
    job_service.run_job(finished["id"], lambda report: {})

    active = job_service.list_jobs(workspace_id=WS, active_only=True)
    ids = [j["id"] for j in active]
    assert running["id"] in ids
    assert finished["id"] not in ids


def test_jobs_are_scoped_to_their_workspace(session):
    job = queue(session)
    assert job_service.get(job["id"], "someone-elses-workspace") is None


def test_the_endpoint_reports_a_job_per_seat(session):
    job_service.reap_orphans(workspace_id=WS)
    queue(session, seat_id="finance", brief="Finance work.")
    body = client.get("/api/jobs?active_only=true").json()
    assert "jobs" in body and "by_seat" in body
    assert isinstance(body["active"], int)


def test_another_tenants_job_is_404_not_403(session):
    """Confirming a job exists but is not yours is itself a disclosure."""
    job = queue(session)
    response = client.get(
        f"/api/jobs/{job['id']}", headers={"X-Workspace-Id": "other-tenant"},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Through the assignment endpoint
# --------------------------------------------------------------------------

def test_async_assign_returns_202_with_a_job_to_watch():
    created = client.post("/api/chat/sessions", json={
        "title": "async assign", "selected_agent_ids": ["finance"],
    }).json()

    response = client.post(
        f"/api/chat/sessions/{created['id']}/assign-async",
        json={"task": "Model the cash impact.", "agent_id": "finance"},
    )
    assert response.status_code == 202
    job = response.json()
    assert job["seat_id"] == "finance"
    assert job["status"] in {"queued", "running", "delivered"}


def test_a_rejected_assignment_never_creates_a_job():
    """
    Validation runs before enqueueing. A job row for work that was never going
    to be accepted is a permanently failed desk plus a confusing error later.
    """
    created = client.post("/api/chat/sessions", json={
        "title": "async assign", "selected_agent_ids": ["finance"],
    }).json()
    before = len(job_service.list_jobs(workspace_id="default-workspace", limit=500))

    response = client.post(
        f"/api/chat/sessions/{created['id']}/assign-async",
        json={"task": "Do a thing.", "agent_id": "legal"},
    )
    assert response.status_code == 422
    after = len(job_service.list_jobs(workspace_id="default-workspace", limit=500))
    assert after == before

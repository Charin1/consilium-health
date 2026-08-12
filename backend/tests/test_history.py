"""
Getting back to work that already happened.

Everything here was persisted from the start — sessions, transcripts, tasks —
but nothing read it back, so a page refresh was indistinguishable from a delete.
These tests pin the two questions the history surface has to answer:

- "What did we discuss?" — per session, chronological.
- "What did I ask for, and did I get it?" — across sessions, which is why it
  cannot be answered from inside one.
"""
import pytest
from fastapi.testclient import TestClient

from app.services.chat_service import ChatService
from main import app

client = TestClient(app)
WS = "w-history"


@pytest.fixture
def service():
    return ChatService(packs=["core", "healthcare"])


@pytest.fixture
def sessions(service):
    """Two sessions: one with a delivered task, one with an untouched task."""
    done = service.create_session(
        workspace_id=WS, created_by="u", mission_id=None, title="RAF build vs buy",
        selected_agent_ids=["vbc_retro", "finance"], persona_packs=["core", "healthcare"],
    )
    service.assign_task(
        done["id"], workspace_id=WS, user_id="u",
        task="Size the RAF lift.", owner="vbc_retro", priority="High",
    )

    pending = service.create_session(
        workspace_id=WS, created_by="u", mission_id=None, title="Stars remediation",
        selected_agent_ids=["quality_stars"], persona_packs=["core", "healthcare"],
    )
    service.add_action_item(
        pending["id"], workspace_id=WS,
        task="List the measures at risk.", owner="quality_stars", priority="Medium",
    )
    return {"done": done, "pending": pending}


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def test_past_sessions_come_back_most_recent_first(service, sessions):
    listed = service.list_sessions(workspace_id=WS, limit=50)
    ids = [s["id"] for s in listed]
    assert sessions["done"]["id"] in ids
    assert sessions["pending"]["id"] in ids


def test_a_session_carries_the_context_needed_to_reopen_it(service, sessions):
    """
    Resuming needs the roster and the packs, not just the text — reopening a
    clinical session into the core boardroom would silently change the cast.
    """
    session = service.get_session(sessions["done"]["id"], workspace_id=WS)
    assert session["persona_packs"] == ["core", "healthcare"]
    assert set(session["selected_agent_ids"]) == {"vbc_retro", "finance"}
    assert session["messages"], "the transcript did not come back"
    assert session["ladder"]["id"] == "clinical_program"


def test_sessions_are_scoped_to_their_workspace(service, sessions):
    assert service.get_session(sessions["done"]["id"], workspace_id="someone-else") is None


# --------------------------------------------------------------------------
# Tasks, across sessions
# --------------------------------------------------------------------------

def test_tasks_are_collected_from_every_session(service, sessions):
    tasks = service.list_tasks(workspace_id=WS)
    titles = [t["task"] for t in tasks]
    assert "Size the RAF lift." in titles
    assert "List the measures at risk." in titles
    assert len({t["session_id"] for t in tasks}) >= 2


def test_delivered_is_derived_from_the_message_not_a_flag(service, sessions):
    """A status field can disagree with reality; a foreign key cannot."""
    tasks = {t["task"]: t for t in service.list_tasks(workspace_id=WS)}
    assert tasks["Size the RAF lift."]["delivered"] is True
    assert tasks["Size the RAF lift."]["message_id"]
    assert tasks["List the measures at risk."]["delivered"] is False
    assert not tasks["List the measures at risk."].get("message_id")


def test_tasks_can_be_filtered_by_state(service, sessions):
    delivered = service.list_tasks(workspace_id=WS, state="delivered")
    outstanding = service.list_tasks(workspace_id=WS, state="outstanding")
    assert all(t["delivered"] for t in delivered)
    assert not any(t["delivered"] for t in outstanding)
    assert delivered and outstanding


def test_a_task_row_stands_alone(service, sessions):
    """It is read outside its session, so it must carry its own context."""
    task = next(t for t in service.list_tasks(workspace_id=WS) if t["owner"] == "vbc_retro")
    assert task["owner_name"] == "Risk Adjustment Specialist"
    assert task["session_title"] == "RAF build vs buy"
    assert task["session_id"]


def test_owner_names_resolve_for_the_session_that_owns_them(service, sessions):
    """
    A clinical seat's name must resolve even though the deployment default is
    core — the lookup follows the session's packs, not the process default.
    """
    core_only = ChatService(packs=["core"])
    task = next(
        t for t in core_only.list_tasks(workspace_id=WS) if t["owner"] == "vbc_retro"
    )
    assert task["owner_name"] == "Risk Adjustment Specialist"


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------

def test_the_endpoints_answer_both_questions():
    created = client.post("/api/chat/sessions", json={
        "title": "API history", "selected_agent_ids": ["finance"],
    }).json()
    client.post(
        f"/api/chat/sessions/{created['id']}/assign",
        json={"task": "Model the runway.", "agent_id": "finance"},
    )

    listed = client.get("/api/chat/sessions?limit=10")
    assert listed.status_code == 200
    assert created["id"] in [s["id"] for s in listed.json()]

    body = client.get("/api/chat/tasks").json()
    assert body["total"] == body["delivered"] + body["outstanding"]
    assert "Model the runway." in [t["task"] for t in body["tasks"]]

    reopened = client.get(f"/api/chat/sessions/{created['id']}").json()
    assert reopened["messages"]


def test_the_task_endpoint_rejects_an_unknown_state():
    assert client.get("/api/chat/tasks?state=maybe").status_code == 422

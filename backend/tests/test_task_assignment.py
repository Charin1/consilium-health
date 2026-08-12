"""
Assigning work to one seat.

The design decision under test: creating the action item and asking for the
work are **one call**. Split them and you get a task list where every item has
an owner and none of them are done — the item exists, the work does not, and
nothing in the data says which. Here the item carries the id of the message
that fulfilled it, so an unfulfilled task is visibly unfulfilled.
"""
import pytest
from fastapi.testclient import TestClient

from app.services.chat_service import ChatService
from main import app

client = TestClient(app)


@pytest.fixture
def service():
    return ChatService(packs=["core", "healthcare"])


@pytest.fixture
def session(service):
    return service.create_session(
        workspace_id="w-test",
        created_by="u-test",
        mission_id=None,
        title="Assignment tests",
        selected_agent_ids=["vbc_retro", "finance", "hipaa_officer"],
        persona_packs=["core", "healthcare"],
    )


def assign(service, session, task, owner, priority="High"):
    return service.assign_task(
        session["id"],
        workspace_id="w-test",
        user_id="u-test",
        task=task,
        owner=owner,
        priority=priority,
    )


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------

def test_assigning_creates_the_item_and_produces_the_work(service, session):
    result = assign(
        service, session,
        "Size the RAF lift from closing our top 200 suspect gaps.",
        "vbc_retro",
    )

    items = result["session"]["action_items"]
    assert len(items) == 1
    assert items[0]["owner"] == "vbc_retro"
    assert items[0]["priority"] == "High"
    assert "RAF lift" in items[0]["task"]

    assert result["deliverable"] is not None
    assert result["deliverable"]["agent_id"] == "vbc_retro"
    assert result["deliverable"]["role"] == "assistant"


def test_the_item_records_which_message_delivered_it(service, session):
    """A task with no linked output is visibly unfinished, not silently so."""
    result = assign(service, session, "Draft the BAA review checklist.", "hipaa_officer")
    item = result["session"]["action_items"][0]
    assert item["message_id"] == result["deliverable"]["id"]
    assert item["delivered_at"]


def test_only_the_assigned_seat_answers(service, session):
    """An assignment is not a debate. Nobody else gets a turn."""
    result = assign(service, session, "Model the 12-month cash impact.", "finance")
    speakers = {
        m["agent_id"] for m in result["messages"] if m["role"] == "assistant"
    }
    assert speakers == {"finance"}


def test_assignments_accumulate(service, session):
    assign(service, session, "First task.", "finance")
    result = assign(service, session, "Second task.", "vbc_retro")
    owners = [i["owner"] for i in result["session"]["action_items"]]
    assert owners == ["finance", "vbc_retro"]


def test_the_brief_asks_for_the_work_not_a_plan_to_do_it(service, session):
    """
    An advisor prompted like a boardroom turn answers with an opinion. The
    assignment brief has to ask for the deliverable, or the reply is "we should
    look into that" and nothing has been produced.
    """
    result = assign(service, session, "Size the RAF lift.", "vbc_retro")
    prompt = next(m for m in result["messages"] if m["role"] == "user")["content"]
    assert "TASK ASSIGNED TO YOU" in prompt
    assert "not a plan to do it later" in prompt


# --------------------------------------------------------------------------
# What must be refused
# --------------------------------------------------------------------------

def test_a_seat_in_the_roster_but_not_in_the_room_is_refused(service, session):
    """An item owned by someone not in the room can never be worked."""
    with pytest.raises(ValueError, match="not active in this session"):
        assign(service, session, "Review the contract terms.", "legal")


def test_a_seat_outside_the_sessions_packs_is_refused(service, session):
    """`clinical_dev` is pharma; this session was seated from core+healthcare."""
    with pytest.raises(ValueError, match="Unknown seat"):
        assign(service, session, "Review the trial protocol.", "clinical_dev")


def test_an_unknown_seat_is_refused(service, session):
    with pytest.raises(ValueError, match="Unknown seat"):
        assign(service, session, "Do a thing.", "chief_vibes_officer")


def test_an_empty_task_is_refused(service, session):
    with pytest.raises(ValueError, match="task description is required"):
        assign(service, session, "   ", "finance")


def test_a_missing_session_is_refused(service):
    with pytest.raises(ValueError, match="not found"):
        service.assign_task(
            "no-such-session", workspace_id="w-test", user_id="u-test",
            task="x", owner="finance",
        )


def test_a_failed_assignment_leaves_no_orphan_item(service, session):
    """A refused assignment must not leave an unownable task behind."""
    with pytest.raises(ValueError):
        assign(service, session, "Review the contract terms.", "legal")
    reloaded = service.get_session(session["id"], workspace_id="w-test")
    assert reloaded["action_items"] == []


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------

def test_assign_endpoint_round_trips():
    created = client.post("/api/chat/sessions", json={
        "title": "API assignment",
        "selected_agent_ids": ["finance"],
    }).json()

    response = client.post(
        f"/api/chat/sessions/{created['id']}/assign",
        json={"task": "Model the cash impact.", "agent_id": "finance", "priority": "High"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deliverable"]["agent_id"] == "finance"
    assert body["session"]["action_items"][0]["owner"] == "finance"


def test_assign_endpoint_rejects_a_seat_not_in_the_room():
    created = client.post("/api/chat/sessions", json={
        "title": "API assignment",
        "selected_agent_ids": ["finance"],
    }).json()
    response = client.post(
        f"/api/chat/sessions/{created['id']}/assign",
        json={"task": "Do a thing.", "agent_id": "legal"},
    )
    assert response.status_code == 422


def test_assign_endpoint_rejects_a_blank_task():
    created = client.post("/api/chat/sessions", json={
        "selected_agent_ids": ["finance"],
    }).json()
    response = client.post(
        f"/api/chat/sessions/{created['id']}/assign",
        json={"task": "   ", "agent_id": "finance"},
    )
    assert response.status_code == 422

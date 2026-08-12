"""
Async "Convene the board": start a debate without blocking on it, watch it
grow one turn at a time, and stop it.

Before this, convening a board was one blocking request -- nothing was
watchable mid-round and nothing could be stopped, because the frontend had no
handle on work that only existed inside a request it was still waiting on.

The properties under test mirror `job_service`'s (`test_jobs.py`), for the
same reasons, plus the one specific to a round: stopping is a request, not an
interrupt, and it only ever takes effect between turns.
"""
import time

import pytest
from fastapi.testclient import TestClient

from app.services import round_service
from app.services.chat_service import ChatService
from main import app

client = TestClient(app)
WS = "w-rounds"


@pytest.fixture
def service():
    return ChatService(packs=["core", "healthcare"])


@pytest.fixture
def session(service):
    return service.create_session(
        workspace_id=WS, created_by="u", mission_id=None,
        title="convene tests", selected_agent_ids=["vbc_retro", "finance", "hipaa_officer"],
        persona_packs=["core", "healthcare"],
    )


def _wait_terminal(job_id, workspace_id=WS, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = round_service.get(job_id, workspace_id)
        if job and job["is_terminal"]:
            return job
        time.sleep(0.02)
    raise AssertionError(f"round {job_id} never reached a terminal state")


def _stub_generate(monkeypatch, texts_by_seat=None):
    """
    Deterministic, instant replies instead of a real model call.

    `_call_model` is patched rather than the LLM client, so `_create_message`
    still runs for real -- these tests are about persistence and polling, not
    about generation.
    """
    from app.services import chat_service as chat_service_module

    def fake(self, persona, history, memory, user_message, **kwargs):
        seat = persona["id"]
        if texts_by_seat and seat in texts_by_seat:
            return texts_by_seat[seat]
        return f"{seat} weighs in."

    monkeypatch.setattr(chat_service_module.ChatService, "_call_model", fake)


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------

def test_enqueue_returns_before_any_turn_happens(session):
    job = round_service.enqueue(workspace_id=WS, session_id=session["id"])
    assert job["status"] == "queued"
    assert job["is_terminal"] is False
    assert job["turn_index"] == 0
    assert job["current_speaker"] is None


def test_progress_is_persisted_turn_by_turn(monkeypatch, service, session):
    """
    The whole point: a poller sees the transcript grow, not one response after
    the whole round finishes.
    """
    _stub_generate(monkeypatch)
    prepared = service.prepare_convene(
        session_id=session["id"], workspace_id=WS, user_id="u",
        message="Should we build RAF chart chase in-house?",
    )
    job = round_service.enqueue(workspace_id=WS, session_id=session["id"])

    seen_speakers = []
    from app.services import round_service as rs

    original_update = rs.update

    def watch(job_id, **fields):
        result = original_update(job_id, **fields)
        if result and result.get("current_speaker"):
            seen_speakers.append(result["current_speaker"])
        return result

    monkeypatch.setattr(rs, "update", watch)

    round_service.run_round(
        job["id"], workspace_id=WS, session_id=session["id"],
        history=prepared["history"], normalized_message=prepared["normalized_message"],
        continue_dialogue=False,
    )
    # Each turn's arrival was observable individually as it happened -- not
    # all at once at the end. Order is whatever `_select_next_agent` chose
    # (not declaration order); the chair's recap is itself a reported turn.
    # The final bookkeeping update (setting status=delivered) does not touch
    # current_speaker but still reports it, since serialize() always returns
    # the whole row -- collapse that trailing repeat before asserting.
    distinct = [s for i, s in enumerate(seen_speakers) if i == 0 or s != seen_speakers[i - 1]]
    assert set(distinct[:3]) == {"vbc_retro", "finance", "hipaa_officer"}
    assert distinct[3] == "moderator"
    assert len(distinct) == 4


def test_a_delivered_round_records_every_turn(monkeypatch, service, session):
    _stub_generate(monkeypatch)
    prepared = service.prepare_convene(
        session_id=session["id"], workspace_id=WS, user_id="u", message="Go.",
    )
    job = round_service.enqueue(workspace_id=WS, session_id=session["id"])
    final = round_service.run_round(
        job["id"], workspace_id=WS, session_id=session["id"],
        history=prepared["history"], normalized_message=prepared["normalized_message"],
        continue_dialogue=False,
    )
    assert final["status"] == "delivered"
    assert final["turn_total"] == 4  # 3 seats + chair recap
    # turns_delivered tracks the highest turn index reported, which includes
    # the recap -- the chair's close is itself a delivered turn in the feed.
    assert final["turns_delivered"] == 4
    assert final["completed_at"] is not None


def test_a_round_that_explodes_still_reaches_a_terminal_state(monkeypatch, service, session):
    from app.services import chat_service as chat_service_module

    def boom(self, *args, **kwargs):
        raise RuntimeError("the provider hung up")

    monkeypatch.setattr(chat_service_module.ChatService, "_call_model", boom)

    prepared = service.prepare_convene(
        session_id=session["id"], workspace_id=WS, user_id="u", message="Go.",
    )
    job = round_service.enqueue(workspace_id=WS, session_id=session["id"])
    with pytest.raises(RuntimeError):
        round_service.run_round(
            job["id"], workspace_id=WS, session_id=session["id"],
            history=prepared["history"], normalized_message=prepared["normalized_message"],
            continue_dialogue=False,
        )
    final = round_service.get(job["id"], WS)
    assert final["status"] == "failed"
    assert final["error_class"] == "RuntimeError"
    assert final["is_terminal"] is True


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------

def test_stop_takes_effect_after_the_current_speaker(monkeypatch, service, session):
    """
    Flip cancel_requested from inside the second turn's generation, whichever
    seat that turns out to be -- `_select_next_agent` scores relevance and
    does not guarantee declaration order. The turn already in flight must
    complete; the third seat must never speak.
    """
    from app.services import chat_service as chat_service_module

    job_holder = {}
    turns_seen = {"count": 0}

    def fake(self, persona, history, memory, user_message, **kwargs):
        turns_seen["count"] += 1
        if turns_seen["count"] == 2:
            round_service.request_cancel(job_holder["id"], WS)
        return f"{persona['id']} weighs in."

    monkeypatch.setattr(chat_service_module.ChatService, "_call_model", fake)

    prepared = service.prepare_convene(
        session_id=session["id"], workspace_id=WS, user_id="u", message="Go.",
    )
    job = round_service.enqueue(workspace_id=WS, session_id=session["id"])
    job_holder["id"] = job["id"]

    final = round_service.run_round(
        job["id"], workspace_id=WS, session_id=session["id"],
        history=prepared["history"], normalized_message=prepared["normalized_message"],
        continue_dialogue=False,
    )
    assert final["status"] == "cancelled"
    assert final["turns_delivered"] == 2, "the in-flight turn must complete before stopping"

    messages = service.get_messages(session["id"], workspace_id=WS)
    speakers = [m["agent_id"] for m in messages if m["role"] == "assistant"]
    assert len(speakers) == 2, "a third seat must never have been asked to speak"
    assert "moderator" not in speakers, "a stop must skip the recap"


def test_request_cancel_on_a_terminal_round_is_a_no_op(monkeypatch, service, session):
    """
    Pressing Stop on a round that just delivered is a race the user loses
    sometimes. That must read as nothing-to-stop, not as an error.
    """
    _stub_generate(monkeypatch)
    prepared = service.prepare_convene(
        session_id=session["id"], workspace_id=WS, user_id="u", message="Go.",
    )
    job = round_service.enqueue(workspace_id=WS, session_id=session["id"])
    round_service.run_round(
        job["id"], workspace_id=WS, session_id=session["id"],
        history=prepared["history"], normalized_message=prepared["normalized_message"],
        continue_dialogue=False,
    )
    result = round_service.request_cancel(job["id"], WS)
    assert result["status"] == "delivered"
    assert result["cancel_requested"] is False, "a terminal round is not flagged after the fact"


def test_request_cancel_on_an_unknown_job_returns_none():
    assert round_service.request_cancel("no-such-job", WS) is None


# --------------------------------------------------------------------------
# Restart and isolation
# --------------------------------------------------------------------------

def test_a_restart_reaps_rounds_it_can_never_finish(session):
    restart_ws = "w-rounds-restart"
    round_service.enqueue(workspace_id=restart_ws, session_id=session["id"])
    round_service.enqueue(workspace_id=restart_ws, session_id=session["id"])

    assert round_service.reap_orphans(workspace_id=restart_ws) == 2
    for job in round_service.list_active(restart_ws):
        pytest.fail(f"round {job['id']} survived the reap still active")


def test_rounds_are_scoped_to_their_workspace(session):
    job = round_service.enqueue(workspace_id=WS, session_id=session["id"])
    assert round_service.get(job["id"], "someone-elses-workspace") is None


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------

def test_convene_async_returns_202_with_a_round_to_watch():
    created = client.post("/api/chat/sessions", json={
        "title": "api convene", "selected_agent_ids": ["finance", "strategist"],
    }).json()

    response = client.post(
        f"/api/chat/sessions/{created['id']}/convene-async",
        json={"message": "What should we prioritise?"},
    )
    assert response.status_code == 202
    job = response.json()
    assert job["session_id"] == created["id"]
    assert job["status"] in {"queued", "running", "delivered"}


def test_the_opening_brief_is_persisted_before_the_202_returns():
    """
    Validation and the user message are synchronous, before the job exists --
    the same reason `assign-async` validates before enqueueing: a brief that
    was never going to be accepted must not get a job row.
    """
    created = client.post("/api/chat/sessions", json={
        "title": "brief timing", "selected_agent_ids": ["finance"],
    }).json()
    client.post(
        f"/api/chat/sessions/{created['id']}/convene-async",
        json={"message": "Model the runway."},
    )
    session = client.get(f"/api/chat/sessions/{created['id']}").json()
    user_turns = [m for m in session["messages"] if m["role"] == "user"]
    assert any(m["content"] == "Model the runway." for m in user_turns)


def test_convene_async_404s_a_missing_session():
    response = client.post(
        "/api/chat/sessions/no-such-session/convene-async",
        json={"message": "Go."},
    )
    assert response.status_code == 404


def test_get_current_round_finds_the_most_recent_one():
    created = client.post("/api/chat/sessions", json={
        "title": "poll by session", "selected_agent_ids": ["finance"],
    }).json()
    started = client.post(
        f"/api/chat/sessions/{created['id']}/convene-async",
        json={"message": "Go."},
    ).json()

    response = client.get(f"/api/chat/sessions/{created['id']}/round")
    assert response.status_code == 200
    assert response.json()["id"] == started["id"]


def test_get_current_round_404s_when_nothing_has_convened():
    created = client.post("/api/chat/sessions", json={
        "title": "no rounds yet", "selected_agent_ids": ["finance"],
    }).json()
    assert client.get(f"/api/chat/sessions/{created['id']}/round").status_code == 404


def test_stop_endpoint_flags_a_running_round():
    created = client.post("/api/chat/sessions", json={
        "title": "stop via api", "selected_agent_ids": ["finance", "strategist"],
    }).json()
    job = client.post(
        f"/api/chat/sessions/{created['id']}/convene-async",
        json={"message": "Go."},
    ).json()

    response = client.post(f"/api/rounds/{job['id']}/stop")
    assert response.status_code == 200
    assert response.json()["cancel_requested"] is True

    _wait_terminal(job["id"], workspace_id="default-workspace")
    final = client.get(f"/api/rounds/{job['id']}").json()
    assert final["status"] in {"cancelled", "delivered"}, (
        "a stop requested near the very end can still legitimately deliver"
    )


def test_stopping_an_unknown_round_is_404():
    assert client.post("/api/rounds/no-such-round/stop").status_code == 404


def test_another_tenants_round_is_404_not_403():
    """Confirming a round exists but is not yours is itself a disclosure."""
    created = client.post("/api/chat/sessions", json={
        "title": "tenant isolation", "selected_agent_ids": ["finance"],
    }).json()
    job = client.post(
        f"/api/chat/sessions/{created['id']}/convene-async",
        json={"message": "Go."},
    ).json()
    response = client.get(
        f"/api/rounds/{job['id']}", headers={"X-Workspace-Id": "some-other-tenant"},
    )
    assert response.status_code == 404

"""
Unit tests for ChatService orchestration & memory.
"""
import pytest
from app.db.database import init_db
from app.services.chat_service import chat_service

@pytest.fixture(autouse=True)
def setup_database():
    init_db()

def test_list_personas():
    personas = chat_service.list_personas()
    assert len(personas) >= 9
    persona_ids = {p["id"] for p in personas}
    assert "moderator" in persona_ids
    assert "strategist" in persona_ids
    assert "tech" in persona_ids
    assert "legal" in persona_ids

def test_create_session():
    session = chat_service.create_session(
        workspace_id="test_ws",
        created_by="test_user",
        mission_id=None,
        title="Test Executive Board",
        selected_agent_ids=["moderator", "strategist", "tech", "finance"],
        turn_mode="automatic",
    )
    assert session["id"] is not None
    assert session["title"] == "Test Executive Board"
    assert "tech" in session["selected_agent_ids"]

def test_post_message_and_interjection():
    session = chat_service.create_session(
        workspace_id="test_ws",
        created_by="test_user",
        mission_id=None,
        title="Interjection Session",
        selected_agent_ids=["moderator", "strategist", "tech"],
        turn_mode="manual",
    )
    
    res = chat_service.post_message(
        session_id=session["id"],
        workspace_id="test_ws",
        user_id="test_user",
        message="Focus on reducing tech debt.",
        target_agent_id=None,
        continue_dialogue=False,
        is_interjection=True,
    )
    
    assert len(res["messages"]) >= 1
    user_msg = res["messages"][0]
    assert user_msg["role"] == "user"
    assert user_msg["metadata"]["is_interjection"] is True


def test_delete_and_truncate_messages():
    session = chat_service.create_session(
        workspace_id="test_ws",
        created_by="test_user",
        mission_id=None,
        title="Test Session",
    )
    session_id = session["id"]
    workspace_id = session["workspace_id"]

    res1 = chat_service.post_message(
        session_id=session_id,
        workspace_id=workspace_id,
        user_id="user_1",
        message="Message 1",
        target_agent_id=None,
        continue_dialogue=False,
    )
    res2 = chat_service.post_message(
        session_id=session_id,
        workspace_id=workspace_id,
        user_id="user_1",
        message="Message 2",
        target_agent_id=None,
        continue_dialogue=False,
    )

    msg_to_delete = res1["messages"][0]["id"]
    deleted = chat_service.delete_message(message_id=msg_to_delete, workspace_id=workspace_id)
    assert deleted is True

    msg_to_truncate = res2["messages"][0]["id"]
    count = chat_service.truncate_messages_from(session_id=session_id, message_id=msg_to_truncate, workspace_id=workspace_id)
    assert count >= 1


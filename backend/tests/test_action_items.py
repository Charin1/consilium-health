import pytest
from app.services.chat_service import chat_service

def test_action_item_lifecycle():
    # 1. Create a chat session
    session = chat_service.create_session(
        workspace_id="default-ws",
        created_by="usr-test",
        mission_id=None,
        title="Action Item Test Board",
        turn_mode="manual"
    )
    session_id = session["id"]
    assert session["action_items"] == []

    # 2. Add an action item
    updated_session = chat_service.add_action_item(
        session_id,
        workspace_id="default-ws",
        task="Conduct Q3 cash runway audit",
        owner="finance",
        priority="High"
    )
    items = updated_session["action_items"]
    assert len(items) == 1
    assert items[0]["task"] == "Conduct Q3 cash runway audit"
    assert items[0]["owner"] == "finance"
    assert items[0]["priority"] == "High"
    assert items[0]["completed"] is False
    item_id = items[0]["id"]

    # 3. Toggle completion
    completed_session = chat_service.update_action_item(
        session_id,
        item_id,
        workspace_id="default-ws",
        completed=True
    )
    assert completed_session["action_items"][0]["completed"] is True

    # 4. Delete action item
    deleted_session = chat_service.delete_action_item(
        session_id,
        item_id,
        workspace_id="default-ws"
    )
    assert len(deleted_session["action_items"]) == 0

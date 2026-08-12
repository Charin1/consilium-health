"""
Unit tests for LangGraph Boardroom Debate Engine.
"""
import pytest
from app.services.boardroom_graph import BoardroomGraphEngine, BoardroomState

SAMPLE_PERSONAS = [
    {"id": "moderator", "name": "Board Chair", "system_prompt": "Prompt Chair"},
    {"id": "strategist", "name": "CSO", "system_prompt": "Prompt CSO"},
    {"id": "tech", "name": "CTO", "system_prompt": "Prompt CTO"},
    {"id": "finance", "name": "CFO", "system_prompt": "Prompt CFO"},
]

def test_speaker_selection_target():
    engine = BoardroomGraphEngine(SAMPLE_PERSONAS)
    speaker = engine.select_next_speaker(
        active_advisors=["moderator", "strategist", "tech"],
        history=[],
        target_agent_id="tech"
    )
    assert speaker == "tech"

def test_speaker_selection_round_robin():
    engine = BoardroomGraphEngine(SAMPLE_PERSONAS)
    history = [
        {"role": "assistant", "agent_id": "strategist", "content": "Hello"}
    ]
    speaker = engine.select_next_speaker(
        active_advisors=["moderator", "strategist", "tech", "finance"],
        history=history,
    )
    assert speaker == "tech"

def test_advisor_turn_node():
    engine = BoardroomGraphEngine(SAMPLE_PERSONAS)
    state: BoardroomState = {
        "session_id": "test-session",
        "history": [{"role": "user", "content": "How do we scale?"}],
        "current_speaker": None,
        "active_advisors": ["moderator", "strategist", "tech"],
        "turn_mode": "automatic",
        "is_interjection": False,
        "latest_reply": None,
        "memory_summary": None,
    }
    res = engine.advisor_turn_node(state)
    assert res["current_speaker"] == "strategist"
    assert res["latest_reply"]["role"] == "assistant"
    assert res["latest_reply"]["agent_id"] == "strategist"

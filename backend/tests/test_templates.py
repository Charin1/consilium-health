import pytest
from app.services.template_service import list_strategic_templates, get_strategic_template
from app.services.chat_service import chat_service


def test_list_strategic_templates():
    templates = list_strategic_templates()
    assert len(templates) >= 9
    categories = {t["category"] for t in templates}
    assert "Strategy" in categories
    assert "Fundraising" in categories
    assert "Operations" in categories
    assert "Growth" in categories
    assert "Product & AI" in categories


def test_get_strategic_template():
    tpl = get_strategic_template("saas_pricing_overhaul")
    assert tpl is not None
    assert tpl["title"] == "SaaS Pricing & Packaging Overhaul"
    assert "ceo" in tpl["recommended_agent_ids"]


def test_create_session_with_initial_prompt():
    session = chat_service.create_session(
        workspace_id="test_ws",
        created_by="test_user",
        mission_id=None,
        title="Series A Pitch Critique Session",
        selected_agent_ids=["ceo", "finance"],
    )
    assert session["id"] is not None

    # Post initial prompt
    res = chat_service.post_message(
        session_id=session["id"],
        workspace_id="test_ws",
        user_id="test_user",
        message="Critique our ARR valuation and CAC payback.",
        target_agent_id=None,
        continue_dialogue=False,
        is_interjection=True,
    )
    assert len(res["messages"]) >= 1
    assert res["messages"][0]["content"] == "Critique our ARR valuation and CAC payback."

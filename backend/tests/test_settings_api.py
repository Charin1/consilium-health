"""
Settings API contracts.

The one that matters most: **a key goes up and never comes back down.** Every
other test here is about not letting the page lie — "saved" is not "working",
and a provider with a typo'd key is indistinguishable from a configured one
until something actually calls it.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import clear_runtime_overrides
from main import app

client = TestClient(app)

SECRET = "sk-test-do-not-leak-me-0000"


@pytest.fixture(autouse=True)
def clean_overrides():
    clear_runtime_overrides()
    yield
    clear_runtime_overrides()


def test_catalogue_lists_every_provider():
    body = client.get("/api/config").json()
    assert {p["id"] for p in body["providers"]} == {
        "anthropic", "openai", "google", "groq", "ollama",
    }
    assert body["degraded"] is False
    assert body["current"]["provider"] in {p["id"] for p in body["providers"]}


def test_a_saved_key_is_never_readable_back(monkeypatch):
    """A settings page that can read a key back leaks one in a screenshot."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    saved = client.post("/api/config", json={
        "provider": "anthropic",
        "api_key": SECRET,
    })
    assert saved.status_code == 200
    assert SECRET not in saved.text
    assert saved.json()["has_key"] is True

    assert SECRET not in client.get("/api/config").text


def test_every_provider_can_be_selected(monkeypatch):
    for provider in ("anthropic", "openai", "google", "groq", "ollama"):
        monkeypatch.setenv(f"{provider.upper()}_API_KEY", "test-key")
        body = client.post("/api/config", json={"provider": provider}).json()
        assert body["provider"] == provider
        assert body["model"], f"{provider} resolved to no model"


def test_unknown_provider_is_rejected():
    response = client.post("/api/config", json={"provider": "skynet"})
    assert response.status_code == 422
    assert "skynet" in response.json()["error"]["message"]


def test_a_model_from_the_wrong_provider_is_rejected():
    """Cross-provider model ids are the most likely typo on this form."""
    response = client.post("/api/config", json={"provider": "anthropic", "model": "gpt-4o"})
    assert response.status_code == 422
    assert "does not serve" in response.json()["error"]["message"]


def test_a_local_provider_refuses_an_api_key():
    response = client.post("/api/config", json={"provider": "ollama", "api_key": "nope"})
    assert response.status_code == 422
    assert "takes no API key" in response.json()["error"]["message"]


def test_readiness_explains_itself(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = client.post("/api/config", json={"provider": "openai"}).json()
    assert body["ready"] is False
    assert "OPENAI_API_KEY" in body["reason"]


def test_connection_test_reports_failure_rather_than_raising(monkeypatch):
    """
    "Saved" is not "working". A typo'd key saves fine, so the test endpoint has
    to make a real call and report what came back.
    """
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    client.post("/api/config", json={"provider": "google"})
    body = client.post("/api/config/test").json()
    assert body["ok"] is False
    assert body["provider"] == "google"
    assert "GOOGLE_API_KEY" in body["reason"]


def test_switching_provider_drops_cached_clients(monkeypatch):
    """
    A cached LangChain client outliving a settings change means the next turn
    still goes to the old provider — the change appears to do nothing.
    """
    from app.services import llm_client

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    client.post("/api/config", json={"provider": "groq"})
    llm_client._model_cache[("sentinel",)] = object()

    client.post("/api/config", json={"provider": "ollama"})
    assert ("sentinel",) not in llm_client._model_cache


def test_ollama_unreachable_is_reported_not_raised():
    """Ollama not running is the normal case, not an error."""
    body = client.get("/api/ollama/models?base_url=http://127.0.0.1:9").json()
    assert body["reachable"] is False
    assert body["models"] == []

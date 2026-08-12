"""
Provider selection and the unified client.

The suite runs with every provider key blanked (`backend.md` §5), so these
tests exercise the unavailable path by default. That is deliberate: the
degraded path is the one that ships to anyone who has not configured a key
yet, and it used to be the least tested code in the repo.
"""
import pytest

from app.config import (
    LLMConfig,
    UnifiedLLMClient,
    clear_runtime_overrides,
    load_config,
    set_runtime_override,
)
from app.services.model_registry import (
    available_providers,
    get_model,
    model_for_seat_tier,
    price_for,
    public_catalogue,
    resolve_model,
)

ALL_PROVIDERS = ["anthropic", "openai", "google", "groq", "ollama"]


@pytest.fixture(autouse=True)
def no_overrides():
    clear_runtime_overrides()
    yield
    clear_runtime_overrides()


@pytest.fixture
def no_keys(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------

def test_every_expected_provider_is_offered():
    assert sorted(available_providers()) == sorted(ALL_PROVIDERS)


def test_catalogue_never_exposes_a_key(monkeypatch):
    """`has_key` is a boolean. The value must not be reachable through the API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    body = public_catalogue()
    assert "sk-ant-secret-value" not in str(body)
    anthropic = next(p for p in body["providers"] if p["id"] == "anthropic")
    assert anthropic["has_key"] is True


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_every_provider_has_a_usable_default(provider):
    body = next(p for p in public_catalogue()["providers"] if p["id"] == provider)
    assert body["models"], f"{provider} lists no models"
    assert get_model(provider, body["default_model"]), (
        f"{provider}'s default model is not in its own catalogue"
    )


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_every_provider_can_serve_both_seat_tiers(provider):
    """A room mixes chairs and specialists; both must resolve to a real model."""
    for seat_tier in (0, 4):
        model = model_for_seat_tier(provider, seat_tier)
        assert get_model(provider, model), (
            f"{provider} tier {seat_tier} resolved to unknown model {model!r}"
        )


def test_unknown_model_falls_back_to_the_provider_default(capsys):
    assert resolve_model("anthropic", "claude-2") == "claude-sonnet-5"
    assert "does not list model" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------

def test_unknown_model_is_unpriced_not_free():
    """A confident $0.00 is a worse answer than "unpriced"."""
    _, _, priced = price_for("anthropic", "claude-2")
    assert priced is False


def test_intro_pricing_expires():
    from datetime import date

    during_in, during_out, _ = price_for("anthropic", "claude-sonnet-5", on=date(2026, 8, 15))
    after_in, after_out, _ = price_for("anthropic", "claude-sonnet-5", on=date(2026, 9, 15))
    assert (during_in, during_out) == (2.0, 10.0)
    assert (after_in, after_out) == (3.0, 15.0)


def test_local_models_are_free_and_priced():
    price_in, price_out, priced = price_for("ollama", "llama3")
    assert (price_in, price_out) == (0.0, 0.0)
    assert priced is True, "local is free, not unknown -- the distinction matters"


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def test_provider_defaults_to_one_with_a_credential(monkeypatch, no_keys):
    """
    Defaulting to a provider whose key is missing gives a boardroom where every
    seat returns the degraded notice: configured-looking, entirely useless.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert load_config().provider == "openai"


def test_explicit_provider_wins_over_credentials(monkeypatch, no_keys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert load_config().provider == "ollama"


def test_unknown_provider_degrades_loudly(monkeypatch, no_keys, capsys):
    monkeypatch.setenv("LLM_PROVIDER", "skynet")
    config = load_config()
    assert config.provider in available_providers()
    assert "not a known provider" in capsys.readouterr().out


def test_runtime_override_beats_the_environment(monkeypatch, no_keys):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    set_runtime_override(provider="ollama", model="mistral")
    config = load_config()
    assert config.provider == "ollama"
    assert config.model == "mistral"


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def test_generate_never_raises_when_the_provider_is_unreachable(no_keys):
    """A raising client turns one bad turn into a dead session."""
    client = UnifiedLLMClient(LLMConfig(provider="anthropic"))
    text = client.generate("system", "user")
    assert isinstance(text, str) and text


def test_a_failed_turn_is_marked_degraded_not_dressed_up(no_keys):
    """
    Regression: the old fallback returned a confident executive briefing about
    API contracts and SOC 2. In a clinical session that was both off-domain and
    indistinguishable from a real advisor turn.
    """
    client = UnifiedLLMClient(LLMConfig(provider="anthropic"))
    result = client.generate_detailed("system", "user")
    assert result.degraded is True
    assert result.reason and "ANTHROPIC_API_KEY" in result.reason
    assert "No model answered" in result.text
    for leaked in ("EXECUTIVE CONSENSUS", "SOC 2", "database schema"):
        assert leaked not in result.text


def test_is_ready_explains_why_not(no_keys):
    ready, reason = UnifiedLLMClient(LLMConfig(provider="openai")).is_ready()
    assert ready is False
    assert "OPENAI_API_KEY" in reason

    ready, reason = UnifiedLLMClient(LLMConfig(provider="ollama")).is_ready()
    assert ready is True and reason is None


def test_seat_tier_picks_the_model(no_keys):
    client = UnifiedLLMClient(LLMConfig(provider="anthropic"))
    assert client.model_for(seat_tier=0) == "claude-opus-5"
    assert client.model_for(seat_tier=4) == "claude-sonnet-5"


def test_an_explicit_model_overrides_the_tier(no_keys):
    client = UnifiedLLMClient(LLMConfig(provider="anthropic", model="claude-haiku-4-5"))
    assert client.model_for(seat_tier=0) == "claude-haiku-4-5"


def test_sliding_window_survives_a_dead_provider(no_keys):
    """Long transcripts must degrade to a string, not to an exception."""
    client = UnifiedLLMClient(LLMConfig(provider="openai"))
    items = [f"Message {i}" for i in range(25)]
    result = client.generate_with_sliding_window("chair", items, window_size=5, overlap=2)
    assert isinstance(result, str) and result

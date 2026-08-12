"""
Shared test setup.

**Every provider credential is blanked for the whole suite** (`backend.md` §5).
Two reasons, and the second is the one that bit:

1. The fallback and degraded paths are what ship to anyone who has not
   configured a key. They should be exercised by default, not by accident.
2. Without this, any test that reaches a code path with a network call picks up
   whatever is in the developer's `.env` and makes a real request. That turned
   a 4-second suite into a 62-second one the moment the AI seat-picker landed,
   and it would have billed a working key.

A test that wants a provider sets the variable itself with `monkeypatch`.
"""
import os

import pytest

PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "LLM_PROVIDER",
)


@pytest.fixture(autouse=True, scope="session")
def _blank_provider_credentials():
    saved = {key: os.environ.pop(key, None) for key in PROVIDER_KEYS}
    yield
    for key, value in saved.items():
        if value is not None:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _reset_runtime_overrides():
    """A settings change in one test must not leak into the next."""
    from app.config import clear_runtime_overrides

    clear_runtime_overrides()
    yield
    clear_runtime_overrides()

"""
Consilium provider configuration.

The model provider is a *setting*, not a code path. Which providers exist and
what they cost lives in `app/providers.json`; how to reach them lives in
`app/services/llm_client.py`; this module only resolves the current selection.

Selection order, most specific first:
  1. the runtime override set through `POST /api/config` (settings page)
  2. environment variables
  3. the catalogue default

Runtime overrides are process-local and deliberately not persisted to disk. A
settings page that writes API keys into a file in the repo is a credential
leak waiting for someone to commit it.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel

from app.services.model_registry import (
    available_providers,
    get_provider,
    has_credential,
    load_catalogue,
    resolve_model,
)

# Re-exported so `from app.config import UnifiedLLMClient` keeps working.
from app.services.llm_client import (  # noqa: F401
    GenerationResult,
    ProviderUnavailable,
    UnifiedLLMClient,
    clear_model_cache,
)


class LLMConfig(BaseModel):
    provider: str = "groq"
    model: Optional[str] = None
    ollama_base_url: str = "http://localhost:11434"

    @property
    def label(self) -> str:
        provider = get_provider(self.provider)
        return provider["label"] if provider else self.provider


_overrides: Dict[str, Any] = {}
_override_lock = threading.Lock()


def _load_dotenv_once() -> None:
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    from dotenv import load_dotenv

    backend_env = Path(__file__).parent.parent / ".env"
    if backend_env.exists():
        load_dotenv(dotenv_path=backend_env, override=False)
    load_dotenv(override=False)


def load_config() -> LLMConfig:
    _load_dotenv_once()
    catalogue = load_catalogue()

    with _override_lock:
        override_provider = _overrides.get("provider")
        override_model = _overrides.get("model")
        override_base_url = _overrides.get("ollama_base_url")

    provider = (
        override_provider
        or os.getenv("LLM_PROVIDER", "").strip().lower()
        or _first_provider_with_a_credential(catalogue)
    )
    if provider not in catalogue["providers"]:
        if provider:
            print(f"Warning: LLM_PROVIDER='{provider}' is not a known provider; "
                  f"known providers are {', '.join(available_providers())}.")
        provider = _first_provider_with_a_credential(catalogue)

    model = override_model or os.getenv(f"{provider.upper()}_MODEL", "").strip() or None

    return LLMConfig(
        provider=provider,
        model=resolve_model(provider, model) if model else None,
        ollama_base_url=(
            override_base_url
            or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        ),
    )


def _first_provider_with_a_credential(catalogue: Dict[str, Any]) -> str:
    """
    Pick a provider that can actually be called.

    Defaulting to one whose key is missing produces a boardroom where every
    seat returns the degraded notice -- technically configured, entirely
    useless. Ollama is last because it is only reachable if it is running.
    """
    preferred = catalogue.get("default_provider")
    if preferred and has_credential(preferred):
        return preferred
    for provider_id in catalogue["providers"]:
        entry = catalogue["providers"][provider_id]
        if entry.get("requires_key") and has_credential(provider_id):
            return provider_id
    return preferred or next(iter(catalogue["providers"]), "ollama")


def set_runtime_override(**values: Any) -> LLMConfig:
    """Apply a settings change for this process and drop cached clients."""
    with _override_lock:
        for key, value in values.items():
            if value is None:
                _overrides.pop(key, None)
            else:
                _overrides[key] = value
    clear_model_cache()
    return load_config()


def clear_runtime_overrides() -> None:
    with _override_lock:
        _overrides.clear()
    clear_model_cache()

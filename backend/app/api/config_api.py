"""
Settings API - provider catalogue, current selection, and connection checks.

The one rule: **an API key goes in and never comes out.** Every response
reports `has_key` as a boolean. A settings page that can read a key back is a
settings page that leaks one the first time a screenshot is shared.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.config import load_config, set_runtime_override
from app.services import model_registry
from app.services.llm_client import UnifiedLLMClient, clear_model_cache
from app.services.model_registry import (
    available_providers,
    get_provider,
    has_credential,
    public_catalogue,
    resolve_model,
)

router = APIRouter()
logger = logging.getLogger("consilium.settings")


class SettingsUpdate(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = Field(default=None, max_length=400)
    ollama_base_url: Optional[str] = None

    @field_validator("provider", "model", "api_key", "ollama_base_url")
    @classmethod
    def strip_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


def _current_state() -> Dict[str, Any]:
    cfg = load_config()
    provider = get_provider(cfg.provider) or {}
    model = cfg.model or provider.get("default_model")
    client = UnifiedLLMClient(cfg)
    ready, reason = client.is_ready()
    return {
        "provider": cfg.provider,
        "provider_label": provider.get("label", cfg.provider),
        "model": model,
        "has_key": has_credential(cfg.provider),
        "requires_key": bool(provider.get("requires_key")),
        "env_key": provider.get("env_key"),
        "ollama_base_url": cfg.ollama_base_url,
        "ready": ready,
        "reason": reason,
    }


@router.get("/config")
async def get_settings() -> Dict[str, Any]:
    """The provider catalogue plus what is currently selected."""
    catalogue = public_catalogue()
    if catalogue["degraded"]:
        logger.error("Provider catalogue is unreadable; only local models are offered.")
    return {**catalogue, "current": _current_state()}


@router.post("/config")
async def update_settings(payload: SettingsUpdate) -> Dict[str, Any]:
    """
    Change the active provider, model, or credential.

    The key is written to this process's environment, not to disk. It survives
    until restart, which is the right lifetime for something typed into a form:
    persisting it would put a credential in a file next to the source tree.
    """
    provider_id = payload.provider or load_config().provider
    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown provider '{provider_id}'. "
                   f"Available: {', '.join(available_providers())}.",
        )

    if payload.model and not model_registry.get_model(provider_id, payload.model):
        raise HTTPException(
            status_code=422,
            detail=f"{provider['label']} does not serve model '{payload.model}'.",
        )

    if payload.api_key:
        env_key = provider.get("env_key")
        if not env_key:
            raise HTTPException(
                status_code=422,
                detail=f"{provider['label']} runs locally and takes no API key.",
            )
        os.environ[env_key] = payload.api_key
        logger.info("Credential set for %s (%s)", provider_id, env_key)

    set_runtime_override(
        provider=provider_id,
        model=resolve_model(provider_id, payload.model) if payload.model else None,
        ollama_base_url=payload.ollama_base_url,
    )

    # The chat service caches nothing about the provider, but the LangChain
    # clients are cached per (provider, model) and must not outlive a change.
    clear_model_cache()

    state = _current_state()
    logger.info(
        "Settings updated: provider=%s model=%s ready=%s",
        state["provider"], state["model"], state["ready"],
    )
    return state


@router.post("/config/test")
async def test_connection() -> Dict[str, Any]:
    """
    Prove the current selection can actually answer.

    A settings page that only checks "is a key present" is checking the wrong
    thing -- a typo'd key is present. This spends a handful of tokens on a real
    round trip and reports what came back.
    """
    cfg = load_config()
    client = UnifiedLLMClient(cfg)
    result = client.generate_detailed(
        "You are a connection test. Reply with exactly: OK",
        "Reply with exactly: OK",
        temperature=0.0,
        max_tokens=16,
    )
    return {
        "ok": not result.degraded,
        "provider": result.provider,
        "model": result.model,
        "reply": result.text[:200],
        "reason": result.reason,
        "usage": result.usage,
    }


@router.get("/ollama/models")
async def get_ollama_models(base_url: Optional[str] = None) -> Dict[str, Any]:
    """What the local Ollama server actually has pulled."""
    import httpx

    url = base_url or load_config().ollama_base_url
    try:
        response = httpx.get(f"{url}/api/tags", timeout=5.0)
        response.raise_for_status()
        models = response.json().get("models", [])
        return {
            "reachable": True,
            "base_url": url,
            "models": [
                {"id": m.get("name"), "label": m.get("name"), "size": m.get("size")}
                for m in models if m.get("name")
            ],
        }
    except Exception as exc:
        # Not an error: Ollama not running is the normal case for most users.
        return {"reachable": False, "base_url": url, "models": [], "reason": str(exc)}


@router.get("/providers")
async def list_providers() -> Dict[str, List[Dict[str, Any]]]:
    """Just the catalogue, for a client that already knows the selection."""
    return {"providers": public_catalogue()["providers"]}

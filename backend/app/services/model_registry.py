"""
The model catalogue: which providers exist, which models they serve, what each
costs, and whether a credential is actually present.

Everything here is read from `app/providers.json`. Adding a model is a data
edit, because the model list is the part that changes weekly.

Two rules this module exists to enforce:

1. **Keys never leave the process.** The API reports `has_key: true/false` and
   nothing else. A settings page that can read a key back is a settings page
   that leaks one.
2. **A price is only quoted for a model we can name.** An unknown model costs
   `0.0` and is stamped `priced: False`, so the console can say "unpriced"
   rather than showing a confident $0.00.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROVIDERS_PATH = Path(__file__).parent.parent / "providers.json"

FALLBACK_CATALOGUE: Dict[str, Any] = {
    "default_provider": "ollama",
    "providers": {
        "ollama": {
            "label": "Ollama (local)",
            "langchain_provider": "ollama",
            "env_key": None,
            "requires_key": False,
            "local": True,
            "default_model": "llama3",
            "models": [{"id": "llama3", "label": "Llama 3", "tier": "balanced",
                        "input": 0.0, "output": 0.0}],
        }
    },
    "seat_tier_to_model_tier": {"0": "frontier", "1": "frontier", "2": "balanced"},
    "degraded": True,
}


def load_catalogue() -> Dict[str, Any]:
    """The provider catalogue. Degrades to local-only rather than to nothing."""
    try:
        catalogue = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: provider catalogue at {PROVIDERS_PATH} unreadable ({exc}); "
              f"falling back to local Ollama only.")
        return dict(FALLBACK_CATALOGUE)
    catalogue.setdefault("providers", {})
    catalogue.setdefault("seat_tier_to_model_tier", {})
    catalogue.setdefault("default_provider", "ollama")
    return catalogue


def available_providers() -> List[str]:
    return list(load_catalogue()["providers"].keys())


def get_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    entry = load_catalogue()["providers"].get(provider_id)
    if entry is None:
        return None
    return {**entry, "id": provider_id}


def has_credential(provider_id: str) -> bool:
    """Whether this provider could actually be called right now."""
    provider = get_provider(provider_id)
    if provider is None:
        return False
    if not provider.get("requires_key"):
        return True
    env_key = provider.get("env_key")
    return bool(env_key and os.getenv(env_key, "").strip())


def get_model(provider_id: str, model_id: str) -> Optional[Dict[str, Any]]:
    provider = get_provider(provider_id)
    if provider is None:
        return None
    return next((m for m in provider.get("models", []) if m["id"] == model_id), None)


def resolve_model(provider_id: str, model_id: Optional[str]) -> str:
    """A model id that provider actually serves. Falls back to its default."""
    provider = get_provider(provider_id)
    if provider is None:
        return model_id or ""
    if model_id and get_model(provider_id, model_id):
        return model_id
    if model_id:
        print(f"Warning: {provider_id} does not list model '{model_id}'; "
              f"using its default '{provider.get('default_model')}'.")
    return provider.get("default_model") or ""


def model_for_seat_tier(provider_id: str, seat_tier: int) -> str:
    """
    Which model a seat of this tier runs on.

    The chair and the executives synthesize across the whole table, so they get
    the frontier model; specialists argue from one discipline and do not need
    it. If a provider has no model at the wanted tier, this degrades to that
    provider's default rather than silently picking the cheapest thing.
    """
    catalogue = load_catalogue()
    provider = get_provider(provider_id)
    if provider is None:
        return ""
    wanted = catalogue["seat_tier_to_model_tier"].get(str(seat_tier), "balanced")
    match = next((m for m in provider.get("models", []) if m.get("tier") == wanted), None)
    if match:
        return match["id"]
    return provider.get("default_model") or ""


def price_for(provider_id: str, model_id: str,
              on: Optional[date] = None) -> Tuple[float, float, bool]:
    """
    ($/1M input, $/1M output, priced) for a model.

    `priced` is False when the model is not in the catalogue. The caller must
    surface that rather than presenting an unpriced model as free -- a
    confident $0.00 is a worse answer than "unpriced".
    """
    model = get_model(provider_id, model_id)
    if model is None:
        return (0.0, 0.0, False)
    intro_until = model.get("intro_until")
    if intro_until and (on or date.today()).isoformat() <= intro_until:
        return (float(model["intro_input"]), float(model["intro_output"]), True)
    return (float(model.get("input", 0.0)), float(model.get("output", 0.0)), True)


def public_catalogue() -> Dict[str, Any]:
    """
    The catalogue as the settings page sees it.

    Credentials are reported as a boolean and never as a value.
    """
    catalogue = load_catalogue()
    providers = []
    for provider_id, entry in catalogue["providers"].items():
        providers.append({
            "id": provider_id,
            "label": entry.get("label", provider_id),
            "requires_key": bool(entry.get("requires_key")),
            "env_key": entry.get("env_key"),
            "has_key": has_credential(provider_id),
            "local": bool(entry.get("local")),
            "docs": entry.get("docs"),
            "default_model": entry.get("default_model"),
            "models": [
                {
                    "id": m["id"],
                    "label": m.get("label", m["id"]),
                    "tier": m.get("tier", "balanced"),
                    "context": m.get("context"),
                    "input": m.get("input", 0.0),
                    "output": m.get("output", 0.0),
                }
                for m in entry.get("models", [])
            ],
        })
    return {
        "providers": providers,
        "default_provider": catalogue["default_provider"],
        "degraded": bool(catalogue.get("degraded")),
    }

"""
Guardrail policy registry.

A pack manifest declares a guardrail policy by id (`guardrails: healthcare_v1`).
This module resolves it to the text injected into every advisor and moderator
prompt for that session, plus the user-facing disclaimer.

Why every turn and not just the moderator's: an advisor prompt that lacks the
boundary can produce individual-patient guidance in its own turn, and the Chair
only sees it afterwards. The constraint has to travel with each seat.

One wording, three surfaces. `disclaimer` is served to the UI header and the PDF
export from here rather than being restated in each, so they cannot drift.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

GUARDRAILS_PATH = Path(__file__).parent.parent / "guardrails.json"

# A pack that declares no policy gets no injected block. That is a real state -
# the core boardroom is domain-neutral and carries no clinical boundary.
_NO_GUARDRAILS: Dict[str, Any] = {
    "id": None,
    "label": "None",
    "prompt_block": "",
    "disclaimer": "",
    "moderator_addendum": "",
}


def _load_registry() -> Dict[str, Any]:
    if not GUARDRAILS_PATH.exists():
        print(f"Warning: guardrail registry missing at {GUARDRAILS_PATH}.")
        return {}
    try:
        raw = json.loads(GUARDRAILS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: guardrail registry {GUARDRAILS_PATH} unreadable ({exc}).")
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def available_guardrails() -> List[str]:
    return sorted(_load_registry())


def get_guardrails(guardrail_id: Optional[str]) -> Dict[str, Any]:
    """
    Resolve a guardrail policy by id.

    A pack that declares a policy which does not exist is the dangerous case:
    the session would run with no boundary while appearing configured. That
    resolves to empty guardrails but is stamped `degraded` and printed, so it
    surfaces rather than passing silently.
    """
    if not guardrail_id:
        return dict(_NO_GUARDRAILS)

    registry = _load_registry()
    policy = registry.get(guardrail_id)
    if policy is None:
        print(f"Warning: pack declares guardrails {guardrail_id!r} but no such "
              f"policy exists (known: {sorted(registry)}). "
              f"THIS SESSION WILL RUN WITHOUT GUARDRAILS.")
        return dict(_NO_GUARDRAILS, degraded=True, requested=guardrail_id)

    return dict(policy, id=guardrail_id)


def guardrails_for_pack(pack: str) -> Dict[str, Any]:
    """Resolve the guardrail policy a pack manifest declares."""
    from app.services.persona_loader import load_pack_manifest

    return get_guardrails(load_pack_manifest(pack).get("guardrails"))

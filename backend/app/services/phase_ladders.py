"""
Phase ladder registry.

A boardroom debate advances through phases. Which phases depends on the kind of
decision being made - a clinical program is not a product build - so the ladder
is data, keyed by id in `app/ladders.json`, and a pack manifest references one
by name through its `phase_ladder` field.

Resolution is deterministic and needs no model call: pack -> manifest ->
ladder id -> phases. The engine only asks "which phase am I in", and the answer
comes from a message-count heuristic that is unchanged from the original
implementation.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

LADDERS_PATH = Path(__file__).parent.parent / "ladders.json"
DEFAULT_LADDER = "product_build"

# The original engine advanced at <2 and <5 assistant messages. Kept as data so
# a ladder could later declare its own thresholds without touching the engine.
DEFAULT_THRESHOLDS = (2, 5)

_FALLBACK_LADDER: Dict[str, Any] = {
    "label": "General discussion",
    "phases": [
        {"title": "Scope & Evidence",
         "guidance": "Establish what is being decided and what evidence supports it."},
        {"title": "Feasibility & Risk",
         "guidance": "Discuss how it would be done, what it costs, and what could go wrong."},
        {"title": "Execution & Follow-through",
         "guidance": "Discuss sequencing, ownership, and how success will be measured."},
    ],
    "focus_warning": "Do NOT jump ahead to execution before the decision itself is clear.",
    "degraded": True,
}


def _load_registry() -> Dict[str, Any]:
    """Read ladders.json. A broken registry degrades loudly, never silently."""
    if not LADDERS_PATH.exists():
        print(f"Warning: ladder registry missing at {LADDERS_PATH}; "
              f"using the generic fallback ladder.")
        return {}
    try:
        raw = json.loads(LADDERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: ladder registry {LADDERS_PATH} unreadable ({exc}); "
              f"using the generic fallback ladder.")
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def available_ladders() -> List[str]:
    return sorted(_load_registry())


def get_ladder(ladder_id: Optional[str]) -> Dict[str, Any]:
    """
    Resolve a ladder by id.

    An unknown id is a configuration error, not a crash: it falls back to a
    generic ladder and stamps `degraded` on the result so callers and traces
    can see that the pack's intended ladder never loaded.
    """
    registry = _load_registry()
    if not registry:
        return dict(_FALLBACK_LADDER, id=ladder_id or DEFAULT_LADDER)

    if ladder_id and ladder_id in registry:
        return dict(registry[ladder_id], id=ladder_id)

    if ladder_id:
        print(f"Warning: unknown phase ladder {ladder_id!r}; "
              f"known ladders are {sorted(registry)}. Falling back.")
        fallback = registry.get(DEFAULT_LADDER, _FALLBACK_LADDER)
        return dict(fallback, id=DEFAULT_LADDER, degraded=True,
                    requested_ladder=ladder_id)

    return dict(registry.get(DEFAULT_LADDER, _FALLBACK_LADDER), id=DEFAULT_LADDER)


def ladder_for_pack(pack: str) -> Dict[str, Any]:
    """Resolve the ladder a pack manifest declares."""
    # Imported here so the ladder registry stays usable without the persona
    # loader, and to keep the two modules from importing each other.
    from app.services.persona_loader import load_pack_manifest

    manifest = load_pack_manifest(pack)
    return get_ladder(manifest.get("phase_ladder"))


def phase_index(assistant_message_count: int, ladder: Dict[str, Any]) -> int:
    """Which phase the debate is in, clamped to the ladder's length."""
    thresholds = ladder.get("thresholds") or DEFAULT_THRESHOLDS
    index = sum(1 for t in thresholds if assistant_message_count >= t)
    return min(index, len(ladder["phases"]) - 1)


def format_phase(assistant_message_count: int, ladder: Dict[str, Any]) -> str:
    """The `CURRENT BOARDROOM PHASE` block injected into the advisor prompt."""
    index = phase_index(assistant_message_count, ladder)
    phase = ladder["phases"][index]
    return f"PHASE {index + 1}: {phase['title']} ({phase['guidance']})"

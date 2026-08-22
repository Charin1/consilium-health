"""
The LLM seat-picker: the fallback path when no keyword rule matches a brief.

Rules cover the briefs somebody thought of. This covers the rest. It runs
*after* the deterministic router has failed, never instead of it, so a brief
that matches a rule keeps its explainable "matched RAF, chart chase" rationale
and its guarantee of a stable room.

Three constraints make an LLM safe to put here:

1. **It chooses, it does not invent.** The model is given the roster and must
   answer with seat ids from it. Anything it returns that is not a real seat is
   dropped, and if nothing survives the caller falls back to the exec bench.
   A router that can hallucinate a seat is a router that can 500 a summon.

2. **It does not get the last word on tension.** The same deterministic tension
   pass runs on whatever it picks. The model is good at "who is relevant" and
   has no opinion worth trusting about who will argue -- that is declared data.

3. **The path is visible.** The response says `chosen_by: "ai"`, so a room you
   cannot explain from keywords is labelled rather than passed off as the
   deterministic result.

The prompt is data-injection territory: the brief is user text, and it is
placed in the *user* turn, never concatenated into the system prompt. The
system prompt states the output contract; the brief cannot rewrite it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("consilium.ai_router")

MAX_BRIEF_CHARS = 4000
MAX_PICKS = 12

SYSTEM_PROMPT = """You staff advisory boards. You are given a roster of seats \
and a decision brief, and you choose who should be in the room.

Rules you must follow:
- Choose ONLY from the seat ids provided. Never invent an id.
- Choose the specialists whose specific expertise the brief actually needs, \
not the most senior people available.
- Prefer a room that will disagree. A board where everyone agrees only \
confirms what the user already believes.
- Choose between 3 and {max_picks} seats. Fewer, sharper seats beat a crowd.
- Do not include the board chair; the chair is always seated separately.

Reply with JSON only, in exactly this shape:
{{"seats": ["seat_id", "seat_id"], "reason": "one sentence on why this room"}}

No prose before or after the JSON."""


def _roster_lines(seats: Sequence[Dict[str, Any]]) -> str:
    """The roster as the model sees it: id, role, and what it fights about."""
    lines = []
    for seat in seats:
        if seat["id"] == "moderator":
            continue
        tags = ", ".join(seat.get("tags", [])) or "—"
        lines.append(f"{seat['id']} | {seat.get('name', '')} | {seat.get('role', '')} | tags: {tags}")
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """
    Pull the JSON object out of a reply.

    Smaller models wrap JSON in prose or a ```json fence no matter how firmly
    the prompt says not to. Failing the whole summon over a code fence would
    make this path useless on exactly the local models people run first.
    """
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = brace.group(0) if brace else None
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def pick_seats(
    brief: str,
    seats: Sequence[Dict[str, Any]],
    limit: int = MAX_PICKS,
    client: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Ask a model who belongs in the room.

    Returns `{"seat_ids": [...], "reason": str, "ok": bool, "error": str|None}`.
    Never raises: a failed pick degrades to `ok: False` and the caller keeps
    the deterministic fallback it already had.
    """
    valid_ids = {s["id"] for s in seats if s["id"] != "moderator"}
    if not valid_ids:
        return {"seat_ids": [], "reason": "", "ok": False, "error": "empty roster"}

    if client is None:
        from app.config import UnifiedLLMClient
        client = UnifiedLLMClient()

    ready, why = client.is_ready()
    if not ready:
        return {"seat_ids": [], "reason": "", "ok": False, "error": why}

    user_prompt = (
        "ROSTER (id | name | role | tags):\n"
        f"{_roster_lines(seats)}\n\n"
        "DECISION BRIEF:\n"
        f"{brief[:MAX_BRIEF_CHARS]}\n\n"
        "Which seats should be in this room?"
    )

    result = client.generate_detailed(
        SYSTEM_PROMPT.format(max_picks=limit),
        user_prompt,
        temperature=0.0,   # same brief, same room, as far as the model allows
        max_tokens=500,
        seat_tier=1,
        node="seat_router",
        # This response is JSON, not prose -- generate_detailed's default
        # quality gate checks for a recommendation/action-item signal that a
        # seat-id array can never contain, so it would misfire every time.
        # _extract_json + the seat-id validation below already IS this
        # call's structural quality gate, just shaped for what it actually
        # returns.
        quality_gate=False,
    )
    if result.degraded:
        return {"seat_ids": [], "reason": "", "ok": False, "error": result.reason}

    parsed = _extract_json(result.text)
    if parsed is None:
        logger.warning("AI router returned unparseable output: %r", result.text[:200])
        return {"seat_ids": [], "reason": "", "ok": False,
                "error": "the model did not return usable JSON"}

    raw = parsed.get("seats")
    if not isinstance(raw, list):
        return {"seat_ids": [], "reason": "", "ok": False,
                "error": "the model returned no seat list"}

    # Drop anything that is not a real seat. A hallucinated id here would
    # otherwise reach the session and fail seat validation much later, where
    # the cause is no longer visible.
    picked: List[str] = []
    invented: List[str] = []
    for item in raw:
        seat_id = str(item).strip()
        if seat_id in valid_ids and seat_id not in picked:
            picked.append(seat_id)
        elif seat_id and seat_id not in valid_ids:
            invented.append(seat_id)
    if invented:
        logger.warning("AI router invented seats that do not exist: %s", invented)

    if not picked:
        return {"seat_ids": [], "reason": "", "ok": False,
                "error": "the model named no seat that exists"}

    reason = parsed.get("reason")
    return {
        "seat_ids": picked[:limit],
        "reason": str(reason).strip() if isinstance(reason, str) else "",
        "ok": True,
        "error": None,
        "invented": invented,
        "model": result.model,
        "provider": result.provider,
    }

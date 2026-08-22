"""
Turn planner: decides whether a seat's turn is answered directly, or
decomposed into angles, answered in parallel, and synthesized.

Mirrors ai_router.py's shape for the same reason: a cheap, structured,
JSON-only LLM call making one bounded decision, never in charge of the
actual content.

There is no retrieval layer in this project yet (no web search, no vector
store) -- every sub-answer below is still the model's own knowledge. The
value of decomposition here is forcing a genuinely complex turn to reason
through distinct angles separately instead of blending them into one
shallow pass, which is the first, retrieval-free lever on response quality.
Wiring real retrieval into the sub-answer stage is a separate piece of work.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("consilium.turn_planner")

MIN_ANGLES = 2
MAX_ANGLES = 3

SYSTEM_PROMPT = """You decide how much thinking a single turn in an advisory \
boardroom conversation deserves.

Most turns are a quick reaction to what was just said -- one direct answer \
is correct, and forcing extra steps would only slow the room down and dilute \
a sharp reaction into a hedged one.

Some turns are genuinely complex: a strategy question, a plan, a comparison \
of real alternatives, or a synthesis across several distinct concerns that a \
single pass tends to answer shallowly or one-sidedly. Those deserve \
decomposition first.

For a complex turn ONLY, break it into between {min_angles} and {max_angles} \
distinct angles a specialist would actually think through separately before \
answering (e.g. for a market-entry question: regulatory exposure, \
competitive positioning, capital requirement -- not three rephrasings of the \
same angle).

Reply with JSON only, in exactly one of these two shapes:
{{"mode": "direct"}}
{{"mode": "plan", "angles": ["angle one", "angle two", "angle three"]}}

No prose before or after the JSON."""


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
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


def plan_turn(persona: Dict[str, Any], turn_brief: str, client: Any) -> Dict[str, Any]:
    """
    Ask whether this turn needs decomposition, and into what.

    Returns `{"mode": "direct"}` or `{"mode": "plan", "angles": [...]}`.
    Never raises and never blocks a turn that would have worked before this
    existed: any failure to reach a model, parse JSON, or get a sane angle
    list degrades to `{"mode": "direct"}`, the pre-existing single-call path.
    """
    user_prompt = (
        f"SEAT: {persona.get('name', '')} ({persona.get('role', '')})\n\n"
        f"TURN BRIEF:\n{turn_brief[:4000]}\n\n"
        "Direct answer, or does this deserve decomposition first?"
    )

    result = client.generate_detailed(
        SYSTEM_PROMPT.format(min_angles=MIN_ANGLES, max_angles=MAX_ANGLES),
        user_prompt,
        temperature=0.0,
        max_tokens=400,
        node="turn_planner",
        persona_id=persona.get("id"),
        pack=persona.get("pack"),
        # JSON only -- the prose quality gate has nothing to check here,
        # same reasoning as ai_router.pick_seats.
        quality_gate=False,
    )
    if result.degraded:
        return {"mode": "direct"}

    parsed = _extract_json(result.text)
    if parsed is None or parsed.get("mode") != "plan":
        return {"mode": "direct"}

    raw_angles = parsed.get("angles")
    if not isinstance(raw_angles, list):
        return {"mode": "direct"}

    angles = [str(a).strip() for a in raw_angles if str(a).strip()]
    if len(angles) < MIN_ANGLES:
        return {"mode": "direct"}

    return {"mode": "plan", "angles": angles[:MAX_ANGLES]}

"""
The org layer: the roster as a directory, and the router that seats a room.

This sits above `persona_loader` and below the API. Two things live here that
the boardroom engine deliberately does not own:

1. **The directory.** What the console renders — seats without their system
   prompts. A 44-seat roster is ~120KB of prompt text; none of it belongs in a
   browser payload, and shipping it hands an attacker the whole prompt library.

2. **The router.** Which seats a brief should summon. It is a *hybrid*, and
   the order matters:

   - Keyword rules in `routing_rules.json` run first. When they match, the
     room is deterministic and explainable -- the response names the rules
     that fired and the text that fired them, so the same brief always seats
     the same room and a surprising one can be accounted for.
   - Only when nothing matches does an LLM pick from the roster
     (`ai_router.py`). Rules cover the briefs somebody thought of; this covers
     the rest, which is a real gap -- the alternative was dumping every
     unmatched brief on the generic executive bench.
   - Either way the **deterministic tension pass runs last**. The model has no
     say in who argues with whom; that is declared data.

   This keeps the deterministic sandwich (`design-lessons.md` #3): the
   non-deterministic step is bounded on both sides by code, its output is
   validated against the roster, and the path taken is reported as
   `chosen_by`. (`.agents/skills/engineering/ai-agents.md` #1.)
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.config import load_config
from app.services.guardrails import get_guardrails, guardrails_for_pack
from app.services.model_registry import model_for_seat_tier, price_for
from app.services.persona_loader import (
    available_packs,
    load_pack_manifest,
    load_personas,
)
from app.services.phase_ladders import get_ladder, ladder_for_pack

RULES_PATH = Path(__file__).parent.parent / "routing_rules.json"

DEFAULT_ROOM_CAP = 8
MIN_ROOM_CAP = 2
MAX_ROOM_CAP = 16

# Seats held back from the keyword ranking so the tension pass has somewhere to
# put a dissenter. The bug this prevents: rank, slice to the cap, *then* append
# a conflicting seat -- which the slice had already discarded. Rooms shipped
# with zero tension while the code claimed to guarantee it.
TENSION_RESERVE = 2

TIER_LABELS = {
    0: "Chair",
    1: "Executive",
    2: "Functional",
    3: "Adversarial",
    4: "Domain specialist",
}

CHAIR_ID = "moderator"


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------
# Prices come from the provider catalogue, not from a table in here. The
# earlier version hardcoded Anthropic rates while the app could only call
# Groq, so the console quoted a number roughly ten times the real one.

EST_INPUT_TOKENS_PER_TURN = 2_500
EST_OUTPUT_TOKENS_PER_TURN = 700
EST_SYNTHESIS_INPUT_TOKENS = 8_000
EST_SYNTHESIS_OUTPUT_TOKENS = 2_000
DEFAULT_TURNS_PER_SEAT = 2

CHAIR_TIER = 0


def active_provider() -> str:
    return load_config().provider


def model_for_seat(seat: Dict[str, Any], provider: Optional[str] = None) -> str:
    """
    Which model this seat runs on under the active provider.

    Tier 0 (chair) and tier 1 (execs) synthesize across the whole table; the
    rest argue from one discipline. That is the split, not seniority theatre.
    """
    return model_for_seat_tier(provider or active_provider(), seat.get("tier", 2))


def estimate_cost(
    seats: Sequence[Dict[str, Any]],
    turns_per_seat: int = DEFAULT_TURNS_PER_SEAT,
    include_synthesis: bool = True,
    on: Optional[date] = None,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Per-seat cost attribution for one debate, not a single blended number.

    A room is billed by who is in it, so the readout has to say which seat is
    expensive -- otherwise the only lever a user has is "use fewer seats".
    (`design-lessons.md` #9.)

    `unpriced` counts seats whose model is not in the catalogue. Those cost
    0.0 here, and the caller must show that rather than presenting an unknown
    model as free.
    """
    provider = provider or active_provider()
    per_seat = []
    total = 0.0
    unpriced = 0

    for seat in seats:
        model = model_for_seat(seat, provider)
        in_rate, out_rate, priced = price_for(provider, model, on)
        if not priced:
            unpriced += 1
        cost = (
            EST_INPUT_TOKENS_PER_TURN * turns_per_seat * in_rate
            + EST_OUTPUT_TOKENS_PER_TURN * turns_per_seat * out_rate
        ) / 1_000_000
        total += cost
        per_seat.append({
            "id": seat["id"],
            "name": seat.get("name", seat["id"]),
            "model": model,
            "turns": turns_per_seat,
            "usd": round(cost, 4),
            "priced": priced,
        })

    synthesis = None
    if include_synthesis:
        chair_model = model_for_seat_tier(provider, CHAIR_TIER)
        in_rate, out_rate, priced = price_for(provider, chair_model, on)
        cost = (
            EST_SYNTHESIS_INPUT_TOKENS * in_rate
            + EST_SYNTHESIS_OUTPUT_TOKENS * out_rate
        ) / 1_000_000
        total += cost
        synthesis = {"model": chair_model, "usd": round(cost, 4), "priced": priced}

    return {
        "provider": provider,
        "per_seat": sorted(per_seat, key=lambda s: -s["usd"]),
        "synthesis": synthesis,
        "total_usd": round(total, 4),
        "unpriced_seats": unpriced,
        "is_free": total == 0.0 and unpriced == 0,
        "basis": {
            "input_tokens_per_turn": EST_INPUT_TOKENS_PER_TURN,
            "output_tokens_per_turn": EST_OUTPUT_TOKENS_PER_TURN,
            "turns_per_seat": turns_per_seat,
        },
        "is_estimate": True,
    }


# ---------------------------------------------------------------------------
# Directory
# ---------------------------------------------------------------------------

def normalize_packs(packs: Optional[Sequence[str]]) -> Tuple[List[str], List[str]]:
    """
    Resolve a requested pack list to (known, unknown).

    Unknown packs are returned rather than raised on so the caller can decide:
    the API rejects them, but an internal caller may prefer to carry on with
    what resolved. Empty input means the whole org.
    """
    known_packs = available_packs()
    if not packs:
        return (list(known_packs), [])
    resolved, unknown = [], []
    for pack in packs:
        cleaned = (pack or "").strip()
        if not cleaned:
            continue
        if cleaned in known_packs and cleaned not in resolved:
            resolved.append(cleaned)
        elif cleaned not in known_packs:
            unknown.append(cleaned)
    return (resolved or list(known_packs), unknown)


def _public_seat(record: Dict[str, Any]) -> Dict[str, Any]:
    """A seat as the console sees it. `system_prompt` is intentionally absent."""
    tier = record.get("tier", 2)
    return {
        "id": record["id"],
        "name": record.get("name", record["id"]),
        "role": record.get("role", ""),
        "tone": record.get("tone", ""),
        "pack": record.get("pack", "core"),
        "tier": tier,
        "tier_label": TIER_LABELS.get(tier, "Functional"),
        "tags": list(record.get("tags", [])),
        "conflicts_with": list(record.get("conflicts_with", [])),
        "inherited_from": record.get("inherited_from"),
    }


def seat_directory(packs: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """The merged roster for `packs`, prompt-free, in manifest order."""
    resolved, _ = normalize_packs(packs)
    return [_public_seat(r) for r in load_personas(resolved)]


def pack_catalogue() -> List[Dict[str, Any]]:
    """
    Every pack with the metadata the console needs to offer it.

    Ladder and guardrail policy are resolved here rather than echoed from the
    manifest, so a pack pointing at a policy id that does not exist shows up as
    `degraded` in the API response instead of looking configured.
    """
    catalogue = []
    for pack in available_packs():
        manifest = load_pack_manifest(pack)
        ladder = ladder_for_pack(pack)
        guard = guardrails_for_pack(pack)
        own = [p["id"] for p in manifest.get("personas", [])]
        inherits = list(manifest.get("inherits", []))
        catalogue.append({
            "id": pack,
            "display_name": manifest.get("display_name", pack.title()),
            "description": manifest.get("description", ""),
            "own_seats": len(own),
            "inherited_seats": len(inherits),
            "total_seats": len(own) + len(inherits),
            "inherits": inherits,
            "ladder": {
                "id": ladder.get("id"),
                "label": ladder.get("label"),
                "phases": [p.get("title") for p in ladder.get("phases", [])],
                "degraded": bool(ladder.get("degraded")),
            },
            "guardrails": {
                "id": guard.get("id"),
                "disclaimer": guard.get("disclaimer", ""),
                "enforced": bool(guard.get("prompt_block")),
                "degraded": bool(guard.get("degraded")),
            },
            "degraded": bool(
                manifest.get("degraded") or ladder.get("degraded") or guard.get("degraded")
            ),
        })
    return catalogue


def disclaimer_for(packs: Optional[Sequence[str]] = None) -> str:
    """
    The one wording every surface shows. Served from the guardrail policy so
    the header, the export, and the advisor prompt cannot drift apart.
    """
    resolved, _ = normalize_packs(packs)
    for pack in reversed(resolved):
        text = guardrails_for_pack(pack).get("disclaimer", "").strip()
        if text:
            return text
    return get_guardrails(None).get("disclaimer", "").strip()


# ---------------------------------------------------------------------------
# Tension
# ---------------------------------------------------------------------------

def find_tensions(
    seat_ids: Iterable[str],
    roster: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Declared disagreements between seats in the same room.

    Conflicts are stored directionally (A lists B) but read undirected: one
    side declaring the fight is enough for the room to have it, and reporting
    the pair twice would inflate the count the console shows.
    """
    if roster is None:
        roster = seat_directory()
    by_id = {s["id"]: s for s in roster}
    present = [sid for sid in seat_ids if sid in by_id]

    seen: Set[Tuple[str, str]] = set()
    tensions: List[Dict[str, Any]] = []
    for sid in present:
        for other in by_id[sid].get("conflicts_with", []):
            if other not in present or other == sid:
                continue
            key = tuple(sorted((sid, other)))
            if key in seen:
                continue
            seen.add(key)
            a, b = key
            tensions.append({
                "a": a,
                "a_name": by_id[a]["name"],
                "b": b,
                "b_name": by_id[b]["name"],
                "mutual": a in by_id[b].get("conflicts_with", []),
            })
    return sorted(tensions, key=lambda t: (t["a"], t["b"]))


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def load_routing_rules() -> Dict[str, Any]:
    try:
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: routing rules at {RULES_PATH} unreadable ({exc}); "
              f"every brief will fall back to the core executive bench.")
        return {"rules": [], "fallback_seats": ["ceo", "strategist", "finance", "ops"],
                "degraded": True}
    rules.setdefault("rules", [])
    rules.setdefault("fallback_seats", ["ceo", "strategist", "finance", "ops"])
    return rules


def _score_rules(brief: str, rules: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Which topic rules the brief triggers, with the text that triggered them."""
    hits = []
    for rule in rules:
        # finditer + group(0), never findall: findall returns the *capture
        # groups* once a pattern has any, so every alternative written outside
        # a group ("risk.adjust", "rna.?seq") comes back as an empty string and
        # the rule looks like it never matched. That silently unseated the
        # entire clinical bench on the briefs it was written for.
        try:
            matches = list(re.finditer(rule["pattern"], brief, flags=re.IGNORECASE))
        except re.error as exc:
            print(f"Warning: routing rule '{rule.get('id')}' has a bad pattern ({exc}); skipped.")
            continue
        terms: List[str] = []
        lowered: Set[str] = set()
        for m in matches:
            text = m.group(0).strip()
            if text and text.lower() not in lowered:
                lowered.add(text.lower())
                terms.append(text)
        if terms:
            hits.append({
                "id": rule["id"],
                "label": rule.get("label", rule["id"]),
                "seats": list(rule.get("seats", [])),
                "matched": terms,
                "score": len(terms),
            })
    return sorted(hits, key=lambda h: (-h["score"], h["id"]))


def summon(
    brief: str,
    packs: Optional[Sequence[str]] = None,
    cap: int = DEFAULT_ROOM_CAP,
    pinned: Optional[Sequence[str]] = None,
    use_ai: bool = True,
    ai_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Propose a room for `brief`, with the reasoning that produced it.

    `pinned` seats are always seated -- the user overruling the router is a
    first-class case, not an exception path.
    """
    cap = max(MIN_ROOM_CAP, min(MAX_ROOM_CAP, cap))
    resolved_packs, unknown_packs = normalize_packs(packs)
    roster = seat_directory(resolved_packs)
    by_id = {s["id"]: s for s in roster}

    config = load_routing_rules()
    matched = _score_rules(brief or "", config["rules"])

    # Rank seats: a rule's score, weighted down the further a seat sits from
    # the front of that rule's list.
    scores: Dict[str, float] = {}
    credited: Dict[str, List[str]] = {}
    for hit in matched:
        for position, seat_id in enumerate(hit["seats"]):
            if seat_id not in by_id:
                continue  # seat lives in a pack this session did not select
            scores[seat_id] = scores.get(seat_id, 0.0) + hit["score"] * (1.0 - 0.1 * position)
            credited.setdefault(seat_id, []).append(hit["id"])

    # Rules matched nothing. Before falling back to the generic executive
    # bench -- which is almost never the right room -- ask a model to pick
    # from the roster. Its choice is validated against real seat ids and then
    # handed to the same tension pass every other room goes through.
    chosen_by = "rules"
    ai_reason = None
    ai_error = None
    used_fallback = not scores

    if used_fallback and use_ai:
        from app.services.ai_router import pick_seats

        picked_by_ai = pick_seats(brief or "", roster, limit=cap - 1, client=ai_client)
        if picked_by_ai["ok"]:
            chosen_by = "ai"
            ai_reason = picked_by_ai["reason"]
            used_fallback = False
            for position, seat_id in enumerate(picked_by_ai["seat_ids"]):
                scores[seat_id] = 100.0 - position  # above any rule score
                credited.setdefault(seat_id, []).append("ai")
        else:
            ai_error = picked_by_ai["error"]

    if used_fallback:
        chosen_by = "fallback"
        for position, seat_id in enumerate(config["fallback_seats"]):
            if seat_id in by_id:
                scores[seat_id] = 1.0 - 0.1 * position
                credited.setdefault(seat_id, []).append("fallback")

    ranked = sorted(
        scores,
        key=lambda sid: (-scores[sid], by_id[sid]["tier"], sid),
    )

    picked: List[str] = []
    if CHAIR_ID in by_id:
        picked.append(CHAIR_ID)
    for seat_id in (pinned or []):
        if seat_id in by_id and seat_id not in picked:
            picked.append(seat_id)
    picked = picked[:cap]

    # Reserve room for the tension pass BEFORE slicing, not after.
    specialist_budget = max(0, cap - len(picked) - TENSION_RESERVE)
    seated_from_ranking = 0
    for seat_id in ranked:
        if seated_from_ranking >= specialist_budget:
            break
        if seat_id not in picked:
            picked.append(seat_id)
            seated_from_ranking += 1

    # Tension pass: a room where everyone agrees only confirms what the user
    # already thinks. Spend the reserve on someone who will push back.
    tensions = find_tensions(picked, roster)
    added_for_tension: List[str] = []
    if not tensions:
        for seat_id in ranked + [s["id"] for s in roster]:
            if len(picked) >= cap:
                break
            if seat_id in picked:
                continue
            if find_tensions(picked + [seat_id], roster):
                picked.append(seat_id)
                added_for_tension.append(seat_id)
                break
        tensions = find_tensions(picked, roster)

    # Any reserve the tension pass did not need goes back to the ranking.
    for seat_id in ranked:
        if len(picked) >= cap:
            break
        if seat_id not in picked:
            picked.append(seat_id)

    seats = [by_id[sid] for sid in picked]
    rationale = [
        {
            "rule": hit["id"],
            "label": hit["label"],
            "matched": hit["matched"],
            "seated": [s for s in hit["seats"] if s in picked],
            "not_available": [s for s in hit["seats"] if s not in by_id],
        }
        for hit in matched
    ]

    return {
        "brief": brief,
        "packs": resolved_packs,
        "unknown_packs": unknown_packs,
        "cap": cap,
        "seats": seats,
        "seat_ids": picked,
        "tensions": tensions,
        "rationale": rationale,
        "seated_for_tension": added_for_tension,
        "used_fallback": used_fallback,
        "chosen_by": chosen_by,
        "ai_reason": ai_reason,
        "ai_error": ai_error,
        "cost_estimate": estimate_cost(seats),
        "disclaimer": disclaimer_for(resolved_packs),
        "degraded": bool(config.get("degraded")),
    }

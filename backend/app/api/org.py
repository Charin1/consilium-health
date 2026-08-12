"""
Org API - the roster as a directory, and the router that seats a room.

Thin by design: parse, validate, delegate to `org_service`, serialize.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.services import org_service
from app.services.org_service import (
    DEFAULT_ROOM_CAP,
    MAX_ROOM_CAP,
    MIN_ROOM_CAP,
)

router = APIRouter()
logger = logging.getLogger("consilium.org.api")


def _parse_packs(packs: Optional[str]) -> Optional[List[str]]:
    """`?packs=core,healthcare` -> ["core", "healthcare"]. Absent means all."""
    if packs is None:
        return None
    return [p.strip() for p in packs.split(",") if p.strip()]


def _require_known_packs(requested: Optional[List[str]]) -> List[str]:
    resolved, unknown = org_service.normalize_packs(requested)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown pack(s): {', '.join(sorted(unknown))}. "
                   f"Available: {', '.join(org_service.available_packs())}.",
        )
    return resolved


class SummonRequest(BaseModel):
    brief: str = Field(min_length=1, max_length=4000)
    packs: Optional[List[str]] = None
    cap: int = Field(default=DEFAULT_ROOM_CAP, ge=MIN_ROOM_CAP, le=MAX_ROOM_CAP)
    pinned: List[str] = Field(default_factory=list)
    use_ai: bool = Field(
        default=True,
        description="Let a model pick the room when no keyword rule matches. "
                    "Set false to force the deterministic path.",
    )

    @field_validator("brief")
    @classmethod
    def strip_brief(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A brief is required to seat a room.")
        return cleaned

    @field_validator("packs", "pinned")
    @classmethod
    def clean_ids(cls, value):
        if value is None:
            return None
        return [v.strip() for v in value if v and v.strip()]


class TensionRequest(BaseModel):
    seat_ids: List[str] = Field(min_length=1, max_length=MAX_ROOM_CAP)
    packs: Optional[List[str]] = None


@router.get("/packs")
async def list_packs() -> Dict[str, Any]:
    """Every pack, with its ladder, guardrail policy, and seat counts."""
    catalogue = org_service.pack_catalogue()
    degraded = [p["id"] for p in catalogue if p["degraded"]]
    if degraded:
        logger.error("Packs resolved in a degraded state: %s", ", ".join(degraded))
    return {
        "packs": catalogue,
        "total_seats": len(org_service.seat_directory()),
        "degraded_packs": degraded,
    }


@router.get("/seats")
async def list_seats(
    packs: Optional[str] = Query(default=None, description="Comma-separated pack ids."),
    tag: Optional[str] = Query(default=None, description="Filter to seats carrying this tag."),
    q: Optional[str] = Query(default=None, max_length=120, description="Match name, role, or tag."),
) -> Dict[str, Any]:
    """The merged roster. System prompts are never included."""
    resolved = _require_known_packs(_parse_packs(packs))
    seats = org_service.seat_directory(resolved)

    if tag:
        needle = tag.strip().lower()
        seats = [s for s in seats if needle in [t.lower() for t in s["tags"]]]
    if q:
        needle = q.strip().lower()
        seats = [
            s for s in seats
            if needle in s["name"].lower()
            or needle in s["role"].lower()
            or any(needle in t.lower() for t in s["tags"])
        ]

    return {
        "seats": seats,
        "packs": resolved,
        "count": len(seats),
        "tags": sorted({t for s in org_service.seat_directory(resolved) for t in s["tags"]}),
        "disclaimer": org_service.disclaimer_for(resolved),
    }


@router.get("/seats/{seat_id}")
async def get_seat(seat_id: str) -> Dict[str, Any]:
    """One seat's dossier, plus who it is on record as arguing with."""
    roster = org_service.seat_directory()
    seat = next((s for s in roster if s["id"] == seat_id), None)
    if seat is None:
        raise HTTPException(status_code=404, detail="Seat not found")

    by_id = {s["id"]: s for s in roster}
    return {
        **seat,
        "conflicts": [
            {"id": cid, "name": by_id[cid]["name"], "pack": by_id[cid]["pack"]}
            for cid in seat["conflicts_with"] if cid in by_id
        ],
        "conflicted_by": [
            {"id": s["id"], "name": s["name"], "pack": s["pack"]}
            for s in roster if seat_id in s["conflicts_with"] and s["id"] != seat_id
        ],
    }


@router.post("/summon")
async def summon_room(payload: SummonRequest) -> Dict[str, Any]:
    """
    Propose a room for a brief.

    Deterministic: the same brief and packs always seat the same room. The
    response carries the rules that fired and the text that fired them, so a
    surprising room can be explained rather than re-rolled.
    """
    resolved = _require_known_packs(payload.packs)
    result = org_service.summon(
        brief=payload.brief,
        packs=resolved,
        cap=payload.cap,
        pinned=payload.pinned,
        use_ai=payload.use_ai,
    )
    if result["degraded"]:
        logger.error("Routing rules unreadable; summon fell back to the core bench.")
    if result["ai_error"]:
        logger.warning("AI seat-picker unavailable, fell back: %s", result["ai_error"])
    logger.info(
        "summon: %d seats, %d tensions, chosen_by=%s, rules=%s",
        len(result["seats"]),
        len(result["tensions"]),
        result["chosen_by"],
        ",".join(r["rule"] for r in result["rationale"]) or "none",
    )
    return result


@router.post("/tensions")
async def inspect_tensions(payload: TensionRequest) -> Dict[str, Any]:
    """
    Declared disagreements for a hand-picked room.

    The console calls this as the user adds and removes seats, so an
    all-agreeing room can be flagged before the debate is paid for.
    """
    resolved = _require_known_packs(payload.packs)
    roster = org_service.seat_directory(resolved)
    by_id = {s["id"]: s for s in roster}

    unknown = [sid for sid in payload.seat_ids if sid not in by_id]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown seat(s) for the selected packs: {', '.join(sorted(unknown))}.",
        )

    seats = [by_id[sid] for sid in payload.seat_ids]
    tensions = org_service.find_tensions(payload.seat_ids, roster)
    return {
        "seat_ids": payload.seat_ids,
        "tensions": tensions,
        "has_tension": bool(tensions),
        "warning": None if tensions else (
            "No seat in this room is on record as disagreeing with another. "
            "The debate will confirm what you already think."
        ),
        "cost_estimate": org_service.estimate_cost(seats),
    }

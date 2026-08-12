"""
Modular Persona Loader.

`from __future__ import annotations` keeps the signatures below evaluable on
Python 3.9, so this module does not silently become the reason a deployment
needs 3.10+.

Loads executive persona prompts from backend/app/personas/<pack>/*.md.

A *pack* is a bundle of personas plus its own metadata manifest (pack.json).
`core` is the domain-neutral C-suite; `healthcare` (Consilium Health) is a
domain pack that inherits four core seats rather than restating them.

Packs are NOT mutually exclusive. `load_personas(["core", "healthcare"])`
returns one merged roster, because the useful sessions mix a CFO with a risk
adjustment specialist. Selection is done downstream by tag, not by directory.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

PERSONAS_DIR = Path(__file__).parent.parent / "personas"
DEFAULT_PACK = "core"

# Number of leading lines scanned for `Name:` / `Role:` / `Tone:`.
# Persona files are written to put those on lines 3-5; widening this is safe,
# narrowing it silently drops metadata.
META_SCAN_LINES = 6

_META_KEYS = ("Name", "Role", "Tone")


def available_packs() -> List[str]:
    """Pack ids that exist on disk, `core` first."""
    if not PERSONAS_DIR.exists():
        return []
    packs = sorted(p.name for p in PERSONAS_DIR.iterdir() if p.is_dir())
    if DEFAULT_PACK in packs:
        packs.remove(DEFAULT_PACK)
        packs.insert(0, DEFAULT_PACK)
    return packs


def load_pack_manifest(pack: str) -> Dict[str, Any]:
    """Read pack.json. Missing or malformed manifests degrade to discovery-only."""
    manifest_path = PERSONAS_DIR / pack / "pack.json"
    if not manifest_path.exists():
        return {"id": pack, "display_name": pack.title(), "inherits": [], "personas": []}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Degradation must be observable, not silent.
        print(f"Warning: pack manifest {manifest_path} unreadable ({exc}); "
              f"falling back to filesystem discovery for pack '{pack}'.")
        return {"id": pack, "display_name": pack.title(), "inherits": [],
                "personas": [], "degraded": True}
    manifest.setdefault("id", pack)
    manifest.setdefault("display_name", pack.title())
    manifest.setdefault("inherits", [])
    manifest.setdefault("personas", [])
    return manifest


def _parse_persona_file(path: Path, meta: Dict[str, Any]) -> str:
    """Return the system prompt, mutating `meta` with any front-matter found."""
    content = path.read_text(encoding="utf-8")
    for line in content.splitlines()[:META_SCAN_LINES]:
        for key in _META_KEYS:
            prefix = f"{key}:"
            if line.startswith(prefix):
                meta[key.lower()] = line.split(prefix, 1)[1].strip()
    prompt_lines = [
        line for line in content.splitlines()
        if not line.startswith("# Persona:") and not line.startswith("ID:")
    ]
    return "\n".join(prompt_lines).strip()


def _resolve_pack(pack: str) -> List[Dict[str, Any]]:
    """Ordered persona records for one pack, including inherited seats."""
    manifest = load_pack_manifest(pack)
    pack_dir = PERSONAS_DIR / pack
    declared = {entry["id"]: entry for entry in manifest["personas"]}

    # Declared order first, then any undeclared .md file still on disk so a
    # dropped-in persona keeps working without a manifest edit.
    on_disk = [p.stem for p in sorted(pack_dir.glob("*.md"))] if pack_dir.exists() else []
    ordered_ids = [pid for pid in declared if pid in on_disk]
    ordered_ids += [pid for pid in on_disk if pid not in ordered_ids]

    records: List[Dict[str, Any]] = []

    # Inherited seats resolve against their owning pack, so healthcare's `ceo`
    # is the same file and prompt the core boardroom uses.
    for inherited_id in manifest["inherits"]:
        record = _load_one(DEFAULT_PACK, inherited_id)
        if record:
            record["inherited_from"] = DEFAULT_PACK
            records.append(record)

    for pid in ordered_ids:
        record = _load_one(pack, pid, declared.get(pid))
        if record:
            records.append(record)
    return records


def _load_one(pack: str, persona_id: str,
              declared: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Build a single persona record from its manifest entry and .md file."""
    if declared is None:
        manifest = load_pack_manifest(pack)
        declared = next((e for e in manifest["personas"] if e["id"] == persona_id), None)

    meta: Dict[str, Any] = {
        "name": persona_id.title(),
        "role": "Executive Advisor",
        "tone": "professional",
    }
    if declared:
        for key in ("name", "role", "tone"):
            if declared.get(key):
                meta[key] = declared[key]

    file_path = PERSONAS_DIR / pack / f"{persona_id}.md"
    prompt = ""
    if file_path.exists():
        try:
            prompt = _parse_persona_file(file_path, meta)
        except OSError as exc:
            print(f"Warning: Failed to load persona file {file_path}: {exc}")

    if not prompt:
        prompt = f"You are the {meta['name']}. Provide executive insights."

    return {
        "id": persona_id,
        "name": meta["name"],
        "role": meta["role"],
        "tone": meta["tone"],
        "system_prompt": prompt,
        "pack": pack,
        "tier": (declared or {}).get("tier", 2),
        "tags": (declared or {}).get("tags", []),
        "conflicts_with": (declared or {}).get("conflicts_with", []),
    }


def load_personas(packs: Sequence[str] | str = DEFAULT_PACK) -> List[Dict[str, Any]]:
    """
    Load personas for one or more packs as a single merged roster.

    `load_personas()` and `load_personas("core")` return the core boardroom
    unchanged, so existing sessions keep the roster they were built against.
    Duplicates across packs resolve to first-seen, which is why inherited
    seats keep their core identity.
    """
    if isinstance(packs, str):
        packs = [packs]

    merged: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for pack in packs:
        for record in _resolve_pack(pack):
            if record["id"] in seen:
                continue
            seen.add(record["id"])
            merged.append(record)
    return merged

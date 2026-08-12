"""
Contracts for the persona pack loader.

The load-bearing one is `test_core_pack_matches_golden`: existing sessions were
built against the flat-directory roster, so `core` must keep returning it
exactly. The golden file was captured from the pre-pack loader; if a change
here is deliberate, regenerate it in the same commit and say why.
"""
import json
from pathlib import Path

import pytest

from app.services import persona_loader
from app.services.persona_loader import (
    available_packs,
    load_pack_manifest,
    load_personas,
)

GOLDEN = Path(__file__).parent / "golden_core_personas.json"
OLD_CONTRACT_FIELDS = ("id", "name", "role", "tone", "system_prompt")

# Every domain pack inherits the same four core seats rather than restating them.
CORE_INHERITED = ["moderator", "ceo", "finance", "ops"]

# Seats each domain pack OWNS, excluding inherited ones. Update deliberately:
# a change here means a seat was added to or removed from the org.
DOMAIN_PACKS = {"healthcare": 15, "pharma": 9, "lifesciences": 6}
CORE_SEATS = 14
FULL_ORG_SEATS = CORE_SEATS + sum(DOMAIN_PACKS.values())


def all_packs():
    return ["core", *DOMAIN_PACKS]


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN.read_text())


# --------------------------------------------------------------------------
# No regression for existing sessions
# --------------------------------------------------------------------------

def test_default_load_is_core():
    assert load_personas() == load_personas("core")


def test_core_pack_matches_golden(golden):
    """core must be byte-identical to the pre-pack roster on the old fields."""
    core = load_personas("core")
    assert [p["id"] for p in core] == [p["id"] for p in golden], "roster order changed"
    by_id = {p["id"]: p for p in golden}
    for persona in core:
        expected = by_id[persona["id"]]
        for field in OLD_CONTRACT_FIELDS:
            assert persona[field] == expected[field], (
                f"{persona['id']}.{field} changed from the golden roster"
            )


def test_core_personas_are_not_generic():
    """A persona whose metadata fell back to defaults means the manifest missed it."""
    for persona in load_personas("core"):
        assert persona["role"] != "Executive Advisor", f"{persona['id']} lost its role"
        assert not persona["system_prompt"].startswith("You are the ") or (
            len(persona["system_prompt"]) > 100
        ), f"{persona['id']} fell back to the stub prompt"


# --------------------------------------------------------------------------
# Packs
# --------------------------------------------------------------------------

def test_available_packs_lists_core_first():
    packs = available_packs()
    assert packs[0] == "core"
    assert "healthcare" in packs


@pytest.mark.parametrize("pack,own_seats", sorted(DOMAIN_PACKS.items()))
def test_domain_pack_seat_counts(pack, own_seats):
    """Each domain pack is its own seats plus the four inherited core seats."""
    seats = load_personas(pack)
    own = [p for p in seats if not p.get("inherited_from")]
    assert len(own) == own_seats
    assert len(seats) == own_seats + len(CORE_INHERITED)


@pytest.mark.parametrize("pack", sorted(DOMAIN_PACKS))
def test_domain_packs_inherit_core_seats_unchanged(pack):
    loaded = {p["id"]: p for p in load_personas(pack)}
    core = {p["id"]: p for p in load_personas("core")}
    for pid in CORE_INHERITED:
        assert pid in loaded, f"{pack} did not inherit {pid}"
        assert loaded[pid]["inherited_from"] == "core"
        assert loaded[pid]["system_prompt"] == core[pid]["system_prompt"], (
            f"inherited {pid} drifted from the core prompt in {pack}"
        )


@pytest.mark.parametrize("pack", sorted(DOMAIN_PACKS))
def test_domain_packs_declare_guardrails_and_ladder(pack):
    manifest = load_pack_manifest(pack)
    assert manifest.get("guardrails"), f"{pack} declares no guardrail policy"
    assert manifest.get("phase_ladder"), f"{pack} declares no phase ladder"


def test_packs_merge_without_duplicates():
    """Packs are views, not partitions - a session may mix any of them."""
    merged = load_personas(all_packs())
    ids = [p["id"] for p in merged]
    assert len(ids) == len(set(ids)), "merged roster contains duplicates"
    assert len(merged) == FULL_ORG_SEATS


def test_merge_order_is_first_seen():
    merged = load_personas(all_packs())
    assert merged[0]["id"] == "moderator"
    assert not [p for p in merged if p.get("inherited_from")], (
        "core-first merge should not re-add inherited seats"
    )


def test_org_has_cross_pack_tension():
    """The point of one merged org: a pharma seat can argue with a healthcare seat."""
    merged = load_personas(all_packs())
    pack_of = {p["id"]: p["pack"] for p in merged}
    cross = {
        tuple(sorted((p["id"], c)))
        for p in merged for c in p["conflicts_with"]
        if c in pack_of and pack_of[c] != p["pack"]
    }
    assert len(cross) >= 20, f"only {len(cross)} cross-pack tensions; packs are too siloed"


# --------------------------------------------------------------------------
# Manifest enrichment - what the router selects on
# --------------------------------------------------------------------------

def test_every_persona_carries_routing_metadata():
    for persona in load_personas(all_packs()):
        assert isinstance(persona["tags"], list) and persona["tags"], (
            f"{persona['id']} has no tags, so the router can never select it"
        )
        assert isinstance(persona["tier"], int)
        assert persona["pack"] in all_packs()


def test_conflict_targets_all_resolve():
    """A conflict pointing at a nonexistent seat silently never fires."""
    merged = load_personas(all_packs())
    ids = {p["id"] for p in merged}
    dangling = {
        p["id"]: [c for c in p["conflicts_with"] if c not in ids]
        for p in merged
    }
    assert not {k: v for k, v in dangling.items() if v}


def test_every_seat_declares_a_conflict():
    """Spec section 3: a persona that never disagrees is a paragraph, not a seat."""
    merged = load_personas(all_packs())
    silent = [p["id"] for p in merged if p["tier"] != 0 and not p["conflicts_with"]]
    assert not silent, f"seats with no declared conflict: {silent}"


def test_available_packs_covers_the_whole_org():
    assert set(available_packs()) == set(all_packs())


# --------------------------------------------------------------------------
# Parse contract and degradation
# --------------------------------------------------------------------------

def test_persona_files_put_metadata_in_the_scan_window():
    """The loader only scans the first META_SCAN_LINES lines for front matter."""
    root = persona_loader.PERSONAS_DIR
    for pack_dir in (d for d in root.iterdir() if d.is_dir()):
        for md in pack_dir.glob("*.md"):
            head = md.read_text().splitlines()[: persona_loader.META_SCAN_LINES]
            found = {
                key for line in head for key in ("Name:", "Role:", "Tone:")
                if line.startswith(key)
            }
            assert found == {"Name:", "Role:", "Tone:"}, (
                f"{md} keeps metadata outside the {persona_loader.META_SCAN_LINES}-line window"
            )


def test_persona_id_matches_filename():
    root = persona_loader.PERSONAS_DIR
    for pack_dir in (d for d in root.iterdir() if d.is_dir()):
        for md in pack_dir.glob("*.md"):
            head = md.read_text().splitlines()[: persona_loader.META_SCAN_LINES]
            declared = next(
                (l.split("ID:", 1)[1].strip() for l in head if l.startswith("ID:")), None
            )
            assert declared == md.stem, f"{md} declares ID {declared!r}"


def test_undeclared_markdown_still_loads(tmp_path, monkeypatch):
    """Filesystem-discovery fallback survives, per spec section 2.1."""
    pack = tmp_path / "scratch"
    pack.mkdir()
    (pack / "wildcard.md").write_text(
        "# Persona: Wildcard\nID: wildcard\nName: Wildcard\n"
        "Role: Unlisted\nTone: curious\n\n## System Prompt\nYou improvise."
    )
    monkeypatch.setattr(persona_loader, "PERSONAS_DIR", tmp_path)
    loaded = load_personas("scratch")
    assert [p["id"] for p in loaded] == ["wildcard"]
    assert loaded[0]["name"] == "Wildcard"
    assert "improvise" in loaded[0]["system_prompt"]


def test_missing_manifest_degrades_to_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(persona_loader, "PERSONAS_DIR", tmp_path)
    (tmp_path / "empty").mkdir()
    manifest = load_pack_manifest("empty")
    assert manifest["personas"] == []
    assert load_personas("empty") == []


def test_malformed_manifest_is_flagged_not_swallowed(tmp_path, monkeypatch, capsys):
    """Degradation must be observable (design-lessons #6)."""
    pack = tmp_path / "broken"
    pack.mkdir()
    (pack / "pack.json").write_text("{ not json")
    monkeypatch.setattr(persona_loader, "PERSONAS_DIR", tmp_path)
    manifest = load_pack_manifest("broken")
    assert manifest.get("degraded") is True
    assert "unreadable" in capsys.readouterr().out

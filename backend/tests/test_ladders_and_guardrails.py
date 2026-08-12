"""
Contracts for phase ladders and guardrail policies.

Both are declared in pack manifests and resolved at engine construction. The
two failure modes that matter:

1. A pack declares a ladder or policy id that does not exist. The session then
   runs with generic phases, or with NO guardrails, while looking configured.
   Both must degrade loudly and stamp the result.
2. Ladder content leaks across domains. The original engine told every advisor
   not to "jump ahead into dev timelines or AWS choices" - nonsense in a
   clinical debate, and the reason this is data now.
"""
import pytest

from app.services import guardrails as guardrails_mod
from app.services import phase_ladders as ladders_mod
from app.services.boardroom_graph import BoardroomGraphEngine
from app.services.guardrails import (
    available_guardrails,
    get_guardrails,
    guardrails_for_pack,
)
from app.services.persona_loader import available_packs, load_personas
from app.services.phase_ladders import (
    available_ladders,
    format_phase,
    get_ladder,
    ladder_for_pack,
    phase_index,
)

PACK_LADDERS = {
    "core": "product_build",
    "healthcare": "clinical_program",
    "pharma": "drug_development",
    "lifesciences": "research_program",
}
PACK_GUARDRAILS = {
    "core": None,
    "healthcare": "healthcare_v1",
    "pharma": "lifesciences_v1",
    "lifesciences": "lifesciences_v1",
}


def _history(n):
    return [{"role": "assistant", "content": "x"} for _ in range(n)]


def _prompt_for(pack, history=None):
    """The system prompt an advisor actually receives, without an LLM call."""
    personas = load_personas(pack)
    engine = BoardroomGraphEngine(
        personas,
        ladder_id=ladder_for_pack(pack).get("id"),
        guardrails=guardrails_for_pack(pack).get("id"),
    )
    captured = {}

    def fake_generate(system_prompt, user_prompt, **kwargs):
        captured["system"] = system_prompt
        return "noted."

    engine.llm_client.generate = fake_generate
    engine.advisor_turn_node({
        "session_id": "t",
        "history": history or [],
        "current_speaker": None,
        "active_advisors": [p["id"] for p in personas],
        "turn_mode": "auto",
        "is_interjection": False,
        "latest_reply": None,
        "memory_summary": None,
    })
    return captured["system"]


# --------------------------------------------------------------------------
# Every declared reference resolves
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pack,ladder_id", sorted(PACK_LADDERS.items()))
def test_pack_ladder_resolves(pack, ladder_id):
    ladder = ladder_for_pack(pack)
    assert ladder["id"] == ladder_id
    assert not ladder.get("degraded"), f"{pack} fell back to a generic ladder"
    assert len(ladder["phases"]) == 3


@pytest.mark.parametrize("pack,policy", sorted(PACK_GUARDRAILS.items()))
def test_pack_guardrails_resolve(pack, policy):
    resolved = guardrails_for_pack(pack)
    assert resolved.get("id") == policy
    assert not resolved.get("degraded"), (
        f"{pack} declares a guardrail policy that does not exist"
    )


def test_every_pack_on_disk_is_covered_by_these_tests():
    """A new pack must not slip past the ladder and guardrail checks."""
    assert set(available_packs()) == set(PACK_LADDERS) == set(PACK_GUARDRAILS)


def test_registries_are_loadable():
    assert "product_build" in available_ladders()
    assert "healthcare_v1" in available_guardrails()


# --------------------------------------------------------------------------
# Degradation is loud
# --------------------------------------------------------------------------

def test_unknown_ladder_degrades_and_is_stamped(capsys):
    ladder = get_ladder("no_such_ladder")
    assert ladder["degraded"] is True
    assert ladder["requested_ladder"] == "no_such_ladder"
    assert ladder["phases"], "fallback must still be a usable ladder"
    assert "unknown phase ladder" in capsys.readouterr().out


def test_unknown_guardrails_run_unguarded_but_shout(capsys):
    """The dangerous case: configured-looking, actually unprotected."""
    resolved = get_guardrails("no_such_policy")
    assert resolved["degraded"] is True
    assert resolved["prompt_block"] == ""
    out = capsys.readouterr().out
    assert "WITHOUT GUARDRAILS" in out, "silent unguarded session"


def test_no_guardrails_is_a_valid_state():
    """core is domain-neutral and carries no clinical boundary."""
    resolved = get_guardrails(None)
    assert resolved["prompt_block"] == ""
    assert not resolved.get("degraded")


def test_missing_registry_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(ladders_mod, "LADDERS_PATH", tmp_path / "gone.json")
    ladder = get_ladder("clinical_program")
    assert ladder["degraded"] is True
    assert len(ladder["phases"]) == 3


def test_malformed_registry_falls_back(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "ladders.json"
    bad.write_text("{ not json")
    monkeypatch.setattr(ladders_mod, "LADDERS_PATH", bad)
    assert get_ladder("clinical_program")["degraded"] is True
    assert "unreadable" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Phase advancement
# --------------------------------------------------------------------------

@pytest.mark.parametrize("count,expected", [(0, 0), (1, 0), (2, 1), (4, 1), (5, 2), (50, 2)])
def test_phase_advances_on_message_count(count, expected):
    """The trigger heuristic is unchanged from the pre-pack engine."""
    assert phase_index(count, get_ladder("clinical_program")) == expected


def test_phase_index_never_overruns_the_ladder():
    ladder = get_ladder("clinical_program")
    assert phase_index(10_000, ladder) == len(ladder["phases"]) - 1


def test_format_phase_numbers_from_one():
    assert format_phase(0, get_ladder("clinical_program")).startswith("PHASE 1:")
    assert format_phase(9, get_ladder("clinical_program")).startswith("PHASE 3:")


# --------------------------------------------------------------------------
# What actually reaches the model
# --------------------------------------------------------------------------

def test_healthcare_prompt_carries_clinical_phase_and_guardrails():
    prompt = _prompt_for("healthcare")
    assert "Clinical Validity & Data Feasibility" in prompt
    assert "NON-NEGOTIABLE GUARDRAILS" in prompt
    assert "do not practise medicine" in prompt
    assert "individual patient" in prompt


def test_no_product_build_language_leaks_into_other_domains():
    """The regression this refactor exists to fix."""
    for pack in ("healthcare", "pharma", "lifesciences"):
        prompt = _prompt_for(pack)
        lowered = prompt.lower()
        for leaked in ("aws", "dev timelines", "tech stack", "product features"):
            assert leaked not in lowered, (
                f"{pack} advisor prompt still contains product-build language: {leaked!r}"
            )


def test_core_prompt_is_unchanged_shape():
    """core keeps the boardroom it had: product ladder, no guardrail block."""
    prompt = _prompt_for("core")
    assert "Product Scope & Core Feature Breakdown" in prompt
    assert "NON-NEGOTIABLE GUARDRAILS" not in prompt


def test_guardrails_reach_every_seat_not_just_the_chair():
    """An advisor without the boundary can breach it in its own turn."""
    personas = load_personas("healthcare")
    non_chair = [p for p in personas if p["id"] != "moderator"]
    assert non_chair
    prompt = _prompt_for("healthcare")
    assert "NON-NEGOTIABLE GUARDRAILS" in prompt


def test_phase_advances_in_the_prompt_as_history_grows():
    early = _prompt_for("pharma", _history(0))
    late = _prompt_for("pharma", _history(9))
    assert "PHASE 1:" in early and "PHASE 3:" in late
    assert "Target, Evidence" in early
    assert "Access, Launch & Lifecycle" in late


def test_engine_defaults_stay_backward_compatible():
    """BoardroomGraphEngine(personas) alone must behave as it did before."""
    engine = BoardroomGraphEngine(load_personas("core"))
    assert engine.ladder["id"] == "product_build"
    assert engine.guardrails["prompt_block"] == ""


# --------------------------------------------------------------------------
# Disclaimer - one wording, served to every surface
# --------------------------------------------------------------------------

@pytest.mark.parametrize("policy", sorted(available_guardrails()))
def test_every_policy_supplies_a_disclaimer(policy):
    resolved = get_guardrails(policy)
    assert resolved["disclaimer"].strip(), f"{policy} has no user-facing disclaimer"
    assert resolved["moderator_addendum"].strip()

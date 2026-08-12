"""
Per-session packs, and the guardrail seam that the live chat path actually uses.

The bug this file exists for: `BoardroomGraphEngine` carried the guardrail and
ladder injection, `ChatService` constructed one in `__init__`, and then never
called it. Every real turn went through `ChatService._build_system_prompt`,
which injected neither. The guardrail tests passed because they exercised the
engine directly -- they tested a seam production did not use, which is the
worst kind of green.

So these tests assert against the prompt `_call_model` would send, reached the
same way a turn reaches it.
"""
import pytest

from app.services.chat_service import ChatService, RosterBundle
from app.services.persona_loader import available_packs


@pytest.fixture(scope="module")
def service():
    return ChatService(packs=["core"])


def prompt_for(service, pack, agent_id, *, is_summary=False, assistant_turns=0):
    """The system prompt a seat receives on a turn, without an LLM call."""
    bundle = service.roster_for([pack] if isinstance(pack, str) else pack)
    persona = bundle.persona(agent_id)
    assert persona is not None, f"{agent_id} is not seated in {bundle.packs}"
    return service._build_system_prompt(
        persona,
        {"summary": None, "key_points": [], "open_questions": []},
        is_summary=is_summary,
        prior_speaker_id=None,
        stance_mode="neutral",
        selection_reason=None,
        bundle=bundle,
        assistant_turns=assistant_turns,
    )


# --------------------------------------------------------------------------
# Guardrails reach the path production uses
# --------------------------------------------------------------------------

def test_guardrails_reach_the_live_turn_prompt(service):
    """The regression. A healthcare turn used to run entirely unguarded."""
    prompt = prompt_for(service, "healthcare", "cmo_clinical")
    assert "NON-NEGOTIABLE GUARDRAILS" in prompt
    assert "individual patient" in prompt


@pytest.mark.parametrize("agent_id", ["ceo", "finance", "vbc_retro", "hipaa_officer"])
def test_guardrails_bind_every_seat_including_inherited_ones(service, agent_id):
    """
    An advisor without the boundary can breach it on its own turn, and the
    chair only sees that afterwards. Inherited core seats are not exempt.
    """
    assert "NON-NEGOTIABLE GUARDRAILS" in prompt_for(service, "healthcare", agent_id)


def test_the_chair_gets_the_addendum_on_synthesis(service):
    """The synthesis is the artefact a user exports."""
    turn = prompt_for(service, "healthcare", "moderator")
    summary = prompt_for(service, "healthcare", "moderator", is_summary=True)
    addendum = service.roster_for(["healthcare"]).guardrails["moderator_addendum"]
    assert addendum in summary
    assert addendum not in turn


def test_core_stays_unguarded(service):
    """Domain-neutral is a valid state, not a missing policy."""
    assert "NON-NEGOTIABLE GUARDRAILS" not in prompt_for(service, "core", "ceo")


# --------------------------------------------------------------------------
# The ladder reaches it too
# --------------------------------------------------------------------------

def test_the_pack_ladder_drives_the_live_prompt(service):
    prompt = prompt_for(service, "healthcare", "cmo_clinical")
    assert "PHASE 1:" in prompt
    assert "Clinical Validity & Data Feasibility" in prompt


def test_the_phase_advances_with_the_transcript(service):
    early = prompt_for(service, "pharma", "clinical_dev", assistant_turns=0)
    late = prompt_for(service, "pharma", "clinical_dev", assistant_turns=9)
    assert "PHASE 1:" in early and "PHASE 3:" in late
    assert "Access, Launch & Lifecycle" in late


def test_no_product_build_language_leaks_into_the_live_prompt(service):
    """
    The original engine warned every advisor off "dev timelines or AWS
    choices" -- nonsense in a clinical debate, and why ladders are data.
    """
    for pack, seat in [
        ("healthcare", "cmo_clinical"),
        ("pharma", "reg_affairs"),
        ("lifesciences", "discovery_bio"),
    ]:
        lowered = prompt_for(service, pack, seat).lower()
        for leaked in ("aws", "dev timelines", "tech stack", "product features"):
            assert leaked not in lowered, f"{pack}/{seat} prompt leaked {leaked!r}"


# --------------------------------------------------------------------------
# Roster bundles
# --------------------------------------------------------------------------

def test_bundles_are_cached_not_rebuilt_per_turn(service):
    first = service.roster_for(["core", "healthcare"])
    second = service.roster_for(["core", "healthcare"])
    assert first is second


def test_the_last_pack_owns_the_ladder_and_the_guardrails(service):
    bundle = service.roster_for(["core", "healthcare"])
    assert bundle.ladder["id"] == "clinical_program"
    assert bundle.guardrails["id"] == "healthcare_v1"
    assert bundle.persona("ceo") is not None, "core seats must still be seated"


def test_packs_merge_rather_than_partition(service):
    """The premise of the roster: a CFO next to a risk adjustment specialist."""
    bundle = service.roster_for(["core", "healthcare", "pharma"])
    ids = {p["id"] for p in bundle.personas}
    assert {"finance", "vbc_retro", "market_access"} <= ids


def test_unknown_packs_fall_back_to_core_with_a_warning(service, caplog):
    bundle = service.roster_for(["cardiology"])
    assert bundle.packs == ("core",)
    assert "cardiology" in caplog.text


def test_empty_selection_is_core(service):
    assert service.roster_for([]).packs == ("core",)
    assert service.roster_for(None).packs == ("core",)


def test_every_pack_produces_a_usable_bundle(service):
    for pack in available_packs():
        bundle = service.roster_for([pack])
        assert bundle.personas
        assert bundle.ladder["phases"]
        assert not bundle.ladder.get("degraded"), f"{pack} has no working ladder"


def test_default_bundle_is_unchanged_by_other_packs(service):
    """A healthcare session must not widen the deployment default roster."""
    before = [p["id"] for p in service.personas]
    service.roster_for(["core", "healthcare", "pharma", "lifesciences"])
    assert [p["id"] for p in service.personas] == before
    assert len(before) == 14


def test_persona_names_resolve_across_the_whole_org(service):
    """
    A transcript can name a seat from a pack this deployment does not serve by
    default. Rendering `vbc_retro` in prose is worse than resolving it.
    """
    assert service._persona_name("vbc_retro") == "Risk Adjustment Specialist"
    assert service._persona_name("ceo") == "Chief Executive Officer"
    assert service._persona_name("nobody") == "nobody"
    assert service._persona_name(None) == "another participant"


def test_bundle_is_frozen():
    """A shared cached bundle that a turn can mutate is a cross-session bug."""
    bundle = RosterBundle(packs=("core",), personas=[], ladder={}, guardrails={})
    with pytest.raises(Exception):
        bundle.packs = ("healthcare",)

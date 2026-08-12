"""
The debate round as a compiled LangGraph.

Why this file exists: `boardroom_graph.py` was titled "LangGraph Multi-Agent
Boardroom Debate Engine" while importing nothing from LangGraph, and
`ChatService` built one and never called it. Both halves of that were invisible
because nothing asserted either — the module's tests exercised its methods
directly, which pass just as well when no graph exists and nothing runs it.

So these tests assert the two things that were false:

1. A graph is genuinely compiled and walked (nodes fire, in order, and the
   conditional edge terminates the round).
2. `ChatService`'s automatic round is what walks it.
"""
import pytest

from app.services.boardroom_graph import BoardroomGraphEngine, BoardroomState
from app.services.chat_service import ChatService

PERSONAS = [
    {"id": "moderator", "name": "Board Chair", "role": "Chair", "tier": 0, "system_prompt": "Chair"},
    {"id": "strategist", "name": "CSO", "role": "Strategy", "tier": 2, "system_prompt": "CSO"},
    {"id": "tech", "name": "CTO", "role": "Engineering", "tier": 1, "system_prompt": "CTO"},
    {"id": "finance", "name": "CFO", "role": "Money", "tier": 1, "system_prompt": "CFO"},
]


class Recorder:
    """Stubs for the three injected callables, plus a log of what fired."""

    def __init__(self, with_recap=True):
        self.calls = []
        self.with_recap = with_recap

    def select(self, state, remaining):
        self.calls.append(("select", tuple(remaining)))
        return (remaining[0], f"{remaining[0]} was next")

    def speak(self, state, seat_id, reason):
        self.calls.append(("speak", seat_id))
        return {"id": f"m-{seat_id}", "agent_id": seat_id,
                "content": f"{seat_id} says something", "role": "assistant"}

    def recap(self, state):
        self.calls.append(("recap", len(state["spoken"])))
        if not self.with_recap:
            return None
        return {"id": "m-recap", "agent_id": "moderator",
                "content": "recap", "role": "assistant"}

    def fired(self, node):
        return [c for c in self.calls if c[0] == node]


@pytest.fixture
def engine():
    return BoardroomGraphEngine(PERSONAS)


def run(engine, rec, advisors, max_turns=10, history=None, should_stop=None):
    return engine.run_round(
        session_id="s1",
        brief="What should we do?",
        history=history or [{"role": "user", "content": "What should we do?"}],
        active_advisors=advisors,
        select=rec.select,
        speak=rec.speak,
        recap=rec.recap,
        max_turns=max_turns,
        should_stop=should_stop,
    )


# --------------------------------------------------------------------------
# It is a real graph
# --------------------------------------------------------------------------

def test_a_graph_is_actually_compiled(engine):
    """The claim the module made for months without it being true."""
    from langgraph.graph.state import CompiledStateGraph

    rec = Recorder()
    compiled = engine.build_graph(select=rec.select, speak=rec.speak, recap=rec.recap)
    assert isinstance(compiled, CompiledStateGraph)
    assert {"route", "speak", "recap"} <= set(compiled.get_graph().nodes)


def test_the_round_walks_route_then_speak_for_every_seat(engine):
    rec = Recorder()
    replies = run(engine, rec, ["strategist", "tech", "finance"])

    assert [c[1] for c in rec.fired("speak")] == ["strategist", "tech", "finance"]
    # route runs before each speak, and the pool shrinks as seats are used.
    assert [c[1] for c in rec.fired("select")] == [
        ("strategist", "tech", "finance"),
        ("tech", "finance"),
        ("finance",),
    ]
    assert [r["agent_id"] for r in replies] == [
        "strategist", "tech", "finance", "moderator",
    ]


def test_history_grows_as_the_round_walks(engine):
    """Each speaker must see what the previous ones said."""
    seen = []

    class Watcher(Recorder):
        def speak(self, state, seat_id, reason):
            seen.append(len(state["history"]))
            return super().speak(state, seat_id, reason)

    run(engine, Watcher(), ["strategist", "tech", "finance"])
    assert seen == [1, 2, 3], "history did not accumulate between turns"


def test_nobody_speaks_twice(engine):
    rec = Recorder()
    run(engine, rec, ["strategist", "tech", "finance"])
    spoken = [c[1] for c in rec.fired("speak")]
    assert len(spoken) == len(set(spoken))


# --------------------------------------------------------------------------
# Termination
# --------------------------------------------------------------------------

def test_the_round_stops_when_everyone_has_spoken(engine):
    rec = Recorder()
    run(engine, rec, ["strategist", "tech"])
    assert len(rec.fired("speak")) == 2


def test_max_turns_caps_a_large_room(engine):
    """A 40-seat room must not run 40 turns because it can."""
    rec = Recorder()
    replies = run(engine, rec, ["strategist", "tech", "finance"], max_turns=2)
    assert len(rec.fired("speak")) == 2
    assert len(replies) == 3  # two turns plus the recap


def test_a_single_speaker_gets_no_recap(engine):
    """Nothing to synthesize, and the chair turn would be pure cost."""
    rec = Recorder()
    replies = run(engine, rec, ["strategist"])
    assert rec.fired("recap") == []
    assert [r["agent_id"] for r in replies] == ["strategist"]


def test_an_empty_room_terminates_without_speaking(engine):
    rec = Recorder()
    assert run(engine, rec, []) == []
    assert rec.fired("speak") == []


def test_a_recap_that_declines_adds_nothing(engine):
    rec = Recorder(with_recap=False)
    replies = run(engine, rec, ["strategist", "tech"])
    assert [r["agent_id"] for r in replies] == ["strategist", "tech"]


def test_a_full_room_does_not_hit_the_recursion_limit(engine):
    """
    LangGraph's default recursion limit is 25, and each turn costs two node
    visits. A ten-seat round would silently truncate on the default.
    """
    rec = Recorder()
    seats = [f"seat_{i}" for i in range(10)]
    replies = run(engine, rec, seats, max_turns=10)
    assert len(rec.fired("speak")) == 10
    assert len(replies) == 11


# --------------------------------------------------------------------------
# ChatService actually uses it
# --------------------------------------------------------------------------

def test_chat_service_runs_its_automatic_round_through_the_graph(monkeypatch):
    """
    The regression that made all of this invisible: an engine that is built and
    never invoked. If the round stops going through `run_round`, this fails.
    """
    service = ChatService(packs=["core"])
    session = service.create_session(
        workspace_id="w-graph", created_by="u", mission_id=None,
        title="graph round", selected_agent_ids=["strategist", "finance"],
    )

    walked = {}
    original = BoardroomGraphEngine.run_round

    def spy(self, **kwargs):
        walked["advisors"] = kwargs["active_advisors"]
        walked["max_turns"] = kwargs["max_turns"]
        return original(self, **kwargs)

    monkeypatch.setattr(BoardroomGraphEngine, "run_round", spy)

    response = service.post_message(
        session_id=session["id"], workspace_id="w-graph", user_id="u",
        message="Should we raise now or wait?",
        target_agent_id=None, continue_dialogue=False,
    )

    assert walked, "the automatic round did not go through the graph"
    assert set(walked["advisors"]) == {"strategist", "finance"}
    speakers = [m["agent_id"] for m in response["messages"] if m["role"] == "assistant"]
    assert set(speakers) == {"strategist", "finance", "moderator"}


def test_a_targeted_turn_does_not_run_the_graph(monkeypatch):
    """Asking one person a question is not a round and must not become one."""
    service = ChatService(packs=["core"])
    session = service.create_session(
        workspace_id="w-graph", created_by="u", mission_id=None,
        title="direct", selected_agent_ids=["strategist", "finance"],
    )

    calls = []
    monkeypatch.setattr(
        BoardroomGraphEngine, "run_round",
        lambda self, **kw: calls.append(kw) or [],
    )
    response = service.post_message(
        session_id=session["id"], workspace_id="w-graph", user_id="u",
        message="Just you, please.", target_agent_id="finance",
        continue_dialogue=False,
    )
    assert calls == []
    speakers = [m["agent_id"] for m in response["messages"] if m["role"] == "assistant"]
    assert speakers == ["finance"]


# --------------------------------------------------------------------------
# The nodes still carry the guardrails
# --------------------------------------------------------------------------

def test_the_graphs_own_prompt_still_carries_pack_guardrails():
    from app.services.persona_loader import load_personas

    engine = BoardroomGraphEngine(
        load_personas("healthcare"),
        ladder_id="clinical_program",
        guardrails="healthcare_v1",
    )
    prompt = engine.build_system_prompt(
        engine.personas_map["cmo_clinical"],
        [{"role": "user", "content": "x"}],
    )
    assert "NON-NEGOTIABLE GUARDRAILS" in prompt
    assert "Clinical Validity & Data Feasibility" in prompt


def test_state_is_a_real_typed_dict():
    assert "history" in BoardroomState.__annotations__
    assert "active_advisors" in BoardroomState.__annotations__


# --------------------------------------------------------------------------
# Cancellation ("Stop" in the console)
# --------------------------------------------------------------------------
#
# A "Stop" button only means something if it takes effect at a predictable
# point. The contract here is: `should_stop` is polled once per turn, in
# `route`, before the next speaker is even chosen -- never mid-generation,
# because a live model call is a blocking network request with nothing to
# interrupt inside it short of killing the thread. So stopping is always
# "after whoever is currently speaking finishes", never instant, and a stop
# always skips the recap: the user asked to stop, so this does not spend one
# more call synthesizing what was said.

def test_should_stop_ends_the_round_before_the_next_turn(engine):
    rec = Recorder()
    replies = run(engine, rec, ["strategist", "tech", "finance"], should_stop=lambda: True)
    assert rec.fired("speak") == [], "should_stop must be checked before the first turn too"
    assert replies == []


def test_should_stop_takes_effect_after_the_current_speaker_not_mid_turn(engine):
    """
    Flip the flag true from inside a turn. The turn already in flight must
    still complete and be counted; only the NEXT one is skipped.
    """
    stop_after = {"strategist"}
    stopped = {"flag": False}

    class StopAfter(Recorder):
        def speak(self, state, seat_id, reason):
            result = super().speak(state, seat_id, reason)
            if seat_id in stop_after:
                stopped["flag"] = True
            return result

    rec = StopAfter()
    replies = run(
        engine, rec, ["strategist", "tech", "finance"],
        should_stop=lambda: stopped["flag"],
    )
    assert [c[1] for c in rec.fired("speak")] == ["strategist"]
    assert [r["agent_id"] for r in replies] == ["strategist"]


def test_a_stopped_round_skips_the_recap(engine):
    """The point of stopping: no extra call, no synthesis nobody asked for."""
    stopped = {"flag": False}

    class StopAfterOne(Recorder):
        def speak(self, state, seat_id, reason):
            result = super().speak(state, seat_id, reason)
            stopped["flag"] = True
            return result

    rec = StopAfterOne()
    replies = run(
        engine, rec, ["strategist", "tech", "finance"],
        should_stop=lambda: stopped["flag"],
    )
    assert rec.fired("recap") == []
    assert "moderator" not in [r["agent_id"] for r in replies]


def test_no_should_stop_behaves_exactly_as_before(engine):
    """The default (None) must reproduce the pre-cancellation behaviour."""
    rec = Recorder()
    replies = run(engine, rec, ["strategist", "tech"], should_stop=None)
    assert [r["agent_id"] for r in replies] == ["strategist", "tech", "moderator"]


# --------------------------------------------------------------------------
# ChatService: the extraction that made the async path possible
# --------------------------------------------------------------------------

def test_run_automatic_round_is_the_same_path_the_sync_endpoint_uses(monkeypatch):
    """
    Regression guard for the refactor: `_generate_agent_replies`'s automatic
    branch must still go through `_run_automatic_round`, or the sync and async
    "Convene the board" paths silently diverge.
    """
    service = ChatService(packs=["core"])
    session = service.create_session(
        workspace_id="w-extract", created_by="u", mission_id=None,
        title="extraction check", selected_agent_ids=["strategist", "finance"],
    )
    calls = []
    original = ChatService._run_automatic_round

    def spy(self, **kwargs):
        calls.append(kwargs.get("turn_brief"))
        return original(self, **kwargs)

    monkeypatch.setattr(ChatService, "_run_automatic_round", spy)
    service.post_message(
        session_id=session["id"], workspace_id="w-extract", user_id="u",
        message="Should we raise now?", target_agent_id=None, continue_dialogue=False,
    )
    assert calls == ["Should we raise now?"]


def test_run_convene_round_drives_the_same_extracted_method(monkeypatch):
    """The async path (`run_convene_round`) must be the same underlying logic
    as the sync path, not a second implementation that can drift from it."""
    service = ChatService(packs=["core"])
    session = service.create_session(
        workspace_id="w-extract2", created_by="u", mission_id=None,
        title="convene extraction check", selected_agent_ids=["strategist", "finance"],
    )
    calls = []
    original = ChatService._run_automatic_round

    def spy(self, **kwargs):
        calls.append(True)
        return original(self, **kwargs)

    monkeypatch.setattr(ChatService, "_run_automatic_round", spy)
    prepared = service.prepare_convene(
        session_id=session["id"], workspace_id="w-extract2", user_id="u",
        message="What should we prioritise?",
    )
    service.run_convene_round(
        session_id=session["id"], workspace_id="w-extract2",
        history=prepared["history"], normalized_message=prepared["normalized_message"],
    )
    assert calls == [True]

"""
The boardroom debate as an actual LangGraph state machine.

This module used to be titled "LangGraph Multi-Agent Boardroom Debate Engine"
while importing no part of LangGraph: it defined a `BoardroomState` TypedDict
and some methods named `*_node`, and nothing ever compiled a graph or walked
one. Worse, `ChatService` constructed one of these in `__init__` and never
called it -- the real turn loop was a `for` loop elsewhere, which is how the
guardrail injection that lived in here reached exactly nobody.

So: a real `StateGraph` now, and the automatic round genuinely runs through it.

    START -> route -> speak -> (route again | recap) -> END

**The graph owns control flow and nothing else.** Selecting a speaker,
generating a turn, and persisting a message are passed in as callables by the
caller. Two reasons that split is deliberate:

- Database writes inside graph nodes make the graph untestable without a
  database, and make a partially-walked graph leave half-written rows.
- `ChatService` already owns prompt construction, guardrail injection, and
  persistence. Duplicating any of that here is how the two copies drift, which
  is the exact failure this module is being rewritten to undo.

The routing step is deterministic. A graph whose edges depend on a model call
cannot be reasoned about or replayed (`design-lessons.md` #3).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, TypedDict

from app.config import UnifiedLLMClient
from app.services.guardrails import get_guardrails
from app.services.phase_ladders import format_phase, get_ladder

logger = logging.getLogger("consilium.boardroom")

DEFAULT_MAX_TURNS = 10


class BoardroomState(TypedDict, total=False):
    """
    What the graph carries between nodes.

    `total=False` because the graph fills most of this in as it walks; a caller
    supplies the brief, the roster, and the history it starts from.
    """
    session_id: str
    brief: str
    history: List[Dict[str, Any]]
    active_advisors: List[str]
    spoken: List[str]
    replies: List[Dict[str, Any]]
    current_speaker: Optional[str]
    selection_reason: Optional[str]
    latest_reply: Optional[Dict[str, Any]]
    turn_index: int
    max_turns: int
    turn_mode: str
    stance_mode: str
    is_interjection: bool
    memory_summary: Optional[Dict[str, Any]]
    recap: Optional[Dict[str, Any]]
    stopped: bool


class BoardroomGraphEngine:
    """
    Compiles and runs the debate graph.

    Constructed with just personas it keeps the pre-graph behaviour, so the
    original node-level tests still describe something real.
    """

    def __init__(
        self,
        personas: List[Dict[str, Any]],
        ladder_id: Optional[str] = None,
        guardrails: Optional[str] = None,
    ):
        self.llm_client = UnifiedLLMClient()
        self.personas_map = {p["id"]: p for p in personas}
        self.ladder = get_ladder(ladder_id)
        self.guardrails = get_guardrails(guardrails)
        self._compiled = None

    # ------------------------------------------------------------------
    # Deterministic routing
    # ------------------------------------------------------------------

    def select_next_speaker(
        self,
        active_advisors: List[str],
        history: List[Dict[str, Any]],
        target_agent_id: Optional[str] = None,
    ) -> str:
        """Round-robin that never lets the same seat speak twice running."""
        candidates = [a for a in active_advisors if a != "moderator"]
        if not candidates:
            candidates = [p for p in self.personas_map if p != "moderator"]
        if not candidates:
            candidates = ["ceo", "strategist", "tech", "ai_lead", "ciso", "growth", "ops"]

        recent = [
            m.get("agent_id") for m in history
            if m.get("role") == "assistant" and m.get("agent_id")
        ]
        last_speaker = recent[-1] if recent else None

        if target_agent_id and target_agent_id in active_advisors and target_agent_id != last_speaker:
            return target_agent_id
        for c in candidates:
            if c not in recent:
                return c
        for c in candidates:
            if c != last_speaker:
                return c
        return candidates[0]

    def _determine_discussion_phase(self, history: List[Dict[str, Any]]) -> str:
        """Phase text for this session's ladder; the trigger is unchanged."""
        msg_count = len([m for m in history if m.get("role") == "assistant"])
        return format_phase(msg_count, self.ladder)

    def build_system_prompt(self, persona: Dict[str, Any], history: List[Dict[str, Any]]) -> str:
        guardrail_block = self.guardrails.get("prompt_block", "")
        focus_warning = self.ladder.get(
            "focus_warning",
            "Do NOT jump ahead to later phases before this one is settled.",
        )
        return (
            f"You are {persona['name']} ({persona.get('role', 'C-suite Executive')}).\n"
            f"{persona['system_prompt']}\n\n"
            + (f"{guardrail_block}\n\n" if guardrail_block else "")
            + f"CURRENT BOARDROOM PHASE:\n{self._determine_discussion_phase(history)}\n\n"
            "BOARDROOM DIALOGUE RULES:\n"
            "- Speak naturally in conversational executive English as a real C-suite leader at the table.\n"
            "- React directly to the previous speakers by name or role.\n"
            f"- Stay focused on the CURRENT BOARDROOM PHASE above. {focus_warning}\n"
            "- Do NOT output meta-instructions, meeting rule bullets, or repetitive templates.\n"
            "- Provide a thorough, detailed response whenever the topic requires it."
        )

    def _format_history(self, history: List[Dict[str, Any]]) -> str:
        lines = []
        for msg in history[-12:]:
            if msg.get("role") == "user":
                speaker = "Managing Director (User)"
            else:
                persona = self.personas_map.get(msg.get("agent_id"), {})
                speaker = persona.get("name", msg.get("agent_id") or "Advisor")
            lines.append(f"{speaker}: {msg.get('content', '')}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Nodes, as plain methods so they stay unit-testable
    # ------------------------------------------------------------------

    def advisor_turn_node(
        self,
        state: BoardroomState,
        target_agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        speaker_id = self.select_next_speaker(
            state.get("active_advisors", []), state.get("history", []), target_agent_id
        )
        persona = self.personas_map.get(speaker_id, {
            "name": "Advisor",
            "system_prompt": "Provide concise executive insights.",
        })
        history = state.get("history", [])

        reply = self.llm_client.generate(
            self.build_system_prompt(persona, history),
            f"Executive Boardroom Transcript:\n{self._format_history(history)}\n\n"
            f"What is your direct perspective and recommendation as {persona['name']}?",
            temperature=0.4,
            max_tokens=5000,
            seat_tier=persona.get("tier", 2),
            node="advisor_turn",
            session_id=state.get("session_id"),
            persona_id=speaker_id,
            pack=persona.get("pack"),
        )

        clean = reply.strip()
        for prefix in (
            f"Persona: {persona['name']}", f"As {persona['name']}:",
            "Meeting rules:", "CRITICAL INSTRUCTIONS:", "CURRENT BOARDROOM PHASE:",
        ):
            if clean.startswith(prefix):
                clean = clean[len(prefix):].strip()

        return {
            "current_speaker": speaker_id,
            "latest_reply": {"role": "assistant", "agent_id": speaker_id, "content": clean},
        }

    def chair_synthesis_node(self, state: BoardroomState) -> Dict[str, Any]:
        history = state.get("history", [])
        guard = "\n".join(part for part in (
            self.guardrails.get("prompt_block", ""),
            self.guardrails.get("moderator_addendum", ""),
        ) if part).strip()

        system_prompt = (
            (f"{guard}\n\n" if guard else "")
            + "You are the Board Chair. Synthesize the debate above into a "
              "consultant-grade Executive Briefing:\n"
              "1. EXECUTIVE CONSENSUS: Unified vision & strategic direction.\n"
              "2. KEY DECIDED ACTIONS: Concrete initiatives with owners.\n"
              "3. UNRESOLVED RISKS: Trade-offs to monitor.\n"
              "4. IMMEDIATE NEXT STEPS: An actionable 30-day roadmap."
        )
        content = self.llm_client.generate_with_sliding_window(
            system_prompt=system_prompt,
            items=[
                f"{m.get('agent_id', m.get('role', 'user')).upper()}: {m.get('content', '')}"
                for m in history
            ],
            window_size=10,
            overlap=3,
            temperature=0.3,
            max_tokens=4000,
            seat_tier=0,
            node="chair_synthesis",
            session_id=state.get("session_id"),
        )
        return {
            "current_speaker": "moderator",
            "latest_reply": {
                "role": "assistant",
                "agent_id": "moderator",
                "content": content,
                "metadata": {"is_synthesis": True},
            },
        }

    # ------------------------------------------------------------------
    # The graph
    # ------------------------------------------------------------------

    def build_graph(
        self,
        *,
        select: Callable[[BoardroomState, List[str]], tuple],
        speak: Callable[[BoardroomState, str, str], Dict[str, Any]],
        recap: Optional[Callable[[BoardroomState], Optional[Dict[str, Any]]]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ):
        """
        Compile the round.

        `select` returns `(seat_id, reason)` from the seats not yet used.
        `speak` produces and persists one turn, returning the stored message.
        `recap` closes a multi-speaker round with the chair, or returns None.

        All three are injected so the graph can be walked in a test with three
        stubs and no database, no model, and no network.

        `should_stop` is polled once per turn, in `route`, before the next
        speaker is picked -- never mid-generation. A live model call is a
        blocking network request; there is nothing to interrupt inside one
        without killing the thread, so a "Stop" takes effect after whoever is
        currently speaking finishes, not instantly. A stop skips the recap: the
        user asked to stop, so this does not spend one more call synthesizing.
        """
        from langgraph.graph import END, START, StateGraph

        def route(state: BoardroomState) -> Dict[str, Any]:
            if should_stop and should_stop():
                return {"current_speaker": None, "selection_reason": None, "stopped": True}
            remaining = [a for a in state["active_advisors"] if a not in state.get("spoken", [])]
            if not remaining:
                return {"current_speaker": None, "selection_reason": None}
            seat_id, reason = select(state, remaining)
            return {"current_speaker": seat_id, "selection_reason": reason}

        def speak_node(state: BoardroomState) -> Dict[str, Any]:
            seat_id = state["current_speaker"]
            message = speak(state, seat_id, state.get("selection_reason") or "")
            return {
                "replies": state.get("replies", []) + [message],
                "spoken": state.get("spoken", []) + [seat_id],
                "history": state.get("history", []) + [{
                    "role": "assistant",
                    "agent_id": seat_id,
                    "content": message.get("content", ""),
                }],
                "turn_index": state.get("turn_index", 0) + 1,
                "latest_reply": message,
            }

        def recap_node(state: BoardroomState) -> Dict[str, Any]:
            message = recap(state) if recap else None
            if message is None:
                return {}
            return {"replies": state.get("replies", []) + [message], "recap": message}

        def after_speak(state: BoardroomState) -> str:
            """
            The only branch in the graph, and it is pure arithmetic.

            An LLM deciding whether the debate continues would make the round
            length unpredictable and the cost unquotable.
            """
            spoken = state.get("spoken", [])
            if state.get("turn_index", 0) >= state.get("max_turns", DEFAULT_MAX_TURNS):
                return "recap" if len(spoken) > 1 else "end"
            if len([a for a in state["active_advisors"] if a not in spoken]) == 0:
                return "recap" if len(spoken) > 1 else "end"
            return "route"

        def after_route(state: BoardroomState) -> str:
            return "speak" if state.get("current_speaker") else "end"

        graph = StateGraph(BoardroomState)
        graph.add_node("route", route)
        graph.add_node("speak", speak_node)
        graph.add_node("recap", recap_node)

        graph.add_edge(START, "route")
        graph.add_conditional_edges("route", after_route, {"speak": "speak", "end": END})
        graph.add_conditional_edges(
            "speak", after_speak, {"route": "route", "recap": "recap", "end": END}
        )
        graph.add_edge("recap", END)
        return graph.compile()

    def run_round(
        self,
        *,
        session_id: str,
        brief: str,
        history: List[Dict[str, Any]],
        active_advisors: List[str],
        select: Callable[[BoardroomState, List[str]], tuple],
        speak: Callable[[BoardroomState, str, str], Dict[str, Any]],
        recap: Optional[Callable[[BoardroomState], Optional[Dict[str, Any]]]] = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        stance_mode: str = "neutral",
        memory_summary: Optional[Dict[str, Any]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> List[Dict[str, Any]]:
        """Walk one automatic round and return the messages it produced."""
        compiled = self.build_graph(select=select, speak=speak, recap=recap, should_stop=should_stop)
        initial: BoardroomState = {
            "session_id": session_id,
            "brief": brief,
            "history": list(history),
            "active_advisors": list(active_advisors),
            "spoken": [],
            "replies": [],
            "turn_index": 0,
            "max_turns": max_turns,
            "turn_mode": "automatic",
            "stance_mode": stance_mode,
            "is_interjection": False,
            "memory_summary": memory_summary,
        }
        # The recursion limit has to clear route+speak per turn plus the recap;
        # the default of 25 would silently truncate a full 10-seat round.
        final = compiled.invoke(
            initial,
            config={"recursion_limit": max_turns * 2 + 8},
        )
        return final.get("replies", [])

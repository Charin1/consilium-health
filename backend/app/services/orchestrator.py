"""
Agent orchestrator service.
Coordinates the multi-agent workflow and emits structured mission events.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

from app.config import UnifiedLLMClient
from app.api.websocket import get_connection_manager
from app.services.mission_store import (
    bump_mission_counters,
    clear_mission_error,
    get_mission_record,
    register_mission_failure,
    serialize_mission_record,
    update_mission_record,
)
from app.services.workflow_contracts import build_error, build_event, new_trace_id

logger = logging.getLogger("consilium.orchestrator")


class OrchestratorService:
    """Orchestrates multi-agent workflows with failure-safe eventing."""

    def __init__(self):
        self.llm_client = UnifiedLLMClient()
        logger.info(f"Orchestrator initialized with LLM provider={self.llm_client.config.provider}")

    async def _emit_event(
        self,
        mission_id: str,
        event_type: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        level: str = "info",
        trace_id: Optional[str] = None,
    ) -> None:
        """Emit a structured event to mission websocket subscribers."""
        manager = get_connection_manager()
        event = build_event(
            mission_id,
            event_type,
            payload=payload,
            source="orchestrator",
            level=level,
            trace_id=trace_id,
        )
        await manager.broadcast_to_mission(mission_id, event)

    async def _emit_mission_state(
        self,
        mission_id: str,
        mission: Optional[Dict[str, Any]],
        *,
        trace_id: Optional[str],
    ) -> None:
        """Broadcast full mission state snapshot."""
        if mission is None:
            return
        await self._emit_event(
            mission_id,
            "mission_state",
            payload={"mission": serialize_mission_record(mission)},
            trace_id=trace_id,
        )

    async def _handle_workflow_failure(
        self,
        mission_id: str,
        *,
        stage: str,
        error: Exception,
        trace_id: str,
    ) -> None:
        """Record and broadcast workflow failures in a standardized JSON envelope."""
        logger.exception("Workflow failed for mission=%s stage=%s", mission_id[:8], stage)
        failure = build_error(
            "WORKFLOW_EXECUTION_FAILED",
            f"Workflow failed while executing '{stage}'.",
            retryable=True,
            stage=stage,
            details={
                "exception_type": error.__class__.__name__,
                "exception": str(error),
            },
        )
        mission = register_mission_failure(mission_id, failure)

        await self._emit_event(
            mission_id,
            "workflow_failed",
            payload={"stage": stage, "error": failure},
            level="error",
            trace_id=trace_id,
        )
        await self._emit_mission_state(mission_id, mission, trace_id=trace_id)

    async def _start_agent(
        self,
        mission_id: str,
        *,
        agent: str,
        task: str,
        stage: str,
        trace_id: str,
    ) -> None:
        """Mark an agent as running and emit start event."""
        mission = bump_mission_counters(mission_id, running_delta=1)
        await self._emit_mission_state(mission_id, mission, trace_id=trace_id)
        await self._emit_event(
            mission_id,
            "agent_started",
            payload={
                "agent": agent,
                "task": task,
                "stage": stage,
            },
            trace_id=trace_id,
        )

    async def _complete_agent(
        self,
        mission_id: str,
        *,
        agent: str,
        task: str,
        result: str,
        stage: str,
        trace_id: str,
        progress: Optional[float] = None,
    ) -> None:
        """Mark an agent as complete and emit completion/progress events."""
        mission = bump_mission_counters(mission_id, completed_delta=1, running_delta=-1)

        if progress is not None:
            mission = update_mission_record(mission_id, progress=progress)

        await self._emit_mission_state(mission_id, mission, trace_id=trace_id)
        await self._emit_event(
            mission_id,
            "agent_completed",
            payload={
                "agent": agent,
                "task": task,
                "result": result,
                "stage": stage,
            },
            trace_id=trace_id,
        )

        if progress is not None:
            await self._emit_event(
                mission_id,
                "progress_update",
                payload={"progress": progress},
                trace_id=trace_id,
            )

    async def generate_strategy(self, mission_id: str, mission_data: Dict[str, Any]) -> None:
        """Generate strategy and execute initial department workflow."""
        trace_id = new_trace_id()
        if get_mission_record(mission_id) is None:
            logger.warning("Mission missing; skipping workflow start for %s", mission_id[:8])
            return

        try:
            clear_mission_error(mission_id)
            mission = update_mission_record(mission_id, status="active", tasks_running=0)
            await self._emit_mission_state(mission_id, mission, trace_id=trace_id)

            strategy = await self._run_strategist_agent(mission_id, mission_data, trace_id=trace_id)
            await self._dispatch_to_departments(mission_id, strategy, mission_data, trace_id=trace_id)
        except Exception as exc:  # pylint: disable=broad-except
            await self._handle_workflow_failure(
                mission_id,
                stage="initial_workflow",
                error=exc,
                trace_id=trace_id,
            )

    async def _run_strategist_agent(
        self,
        mission_id: str,
        mission_data: Dict[str, Any],
        *,
        trace_id: str,
    ) -> str:
        """Execute strategy generation step."""
        await self._start_agent(
            mission_id,
            agent="strategist",
            stage="strategy_generation",
            task="Analyzing your business context and creating growth strategy...",
            trace_id=trace_id,
        )

        await asyncio.sleep(1.5)
        strategy_prompt = self._build_strategy_prompt(mission_data)

        strategy: str
        try:
            t0 = time.time()
            strategy = await self.llm_client.generate_async(
                system_prompt=self._get_strategist_system_prompt(),
                user_prompt=strategy_prompt,
                max_tokens=2000,
                node="mission_strategy",
                session_id=trace_id,
                tags=[f"mission:{mission_id}"],
            )
            logger.info(
                "Strategy generated by provider=%s mission=%s in %.1fs",
                self.llm_client.config.provider,
                mission_id[:8],
                time.time() - t0,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Strategy model call failed for mission=%s: %s", mission_id[:8], exc)
            warning = build_error(
                "STRATEGY_MODEL_FALLBACK",
                "LLM call failed; fallback strategy was generated.",
                retryable=True,
                stage="strategy_generation",
                details={"exception": str(exc)},
            )
            await self._emit_event(
                mission_id,
                "agent_warning",
                payload={
                    "agent": "strategist",
                    "task": "Strategy generation",
                    "error": warning,
                },
                level="warning",
                trace_id=trace_id,
            )
            strategy = self._generate_demo_strategy(mission_data)

        await self._complete_agent(
            mission_id,
            agent="strategist",
            stage="strategy_generation",
            task="Strategy generation",
            result=strategy,
            progress=0.03,
            trace_id=trace_id,
        )

        return strategy

    async def _dispatch_to_departments(
        self,
        mission_id: str,
        strategy: str,
        mission_data: Dict[str, Any],
        *,
        trace_id: str,
    ) -> None:
        """Dispatch tasks to department agents based on strategy."""
        _ = strategy
        _ = mission_data

        await self._run_department_step(
            mission_id,
            agent="sales",
            stage="sales_outreach",
            task="Drafting personalized outreach emails...",
            result="12 personalized outreach emails drafted",
            progress=0.06,
            duration_seconds=2.5,
            trace_id=trace_id,
        )
        await self._run_department_step(
            mission_id,
            agent="marketing",
            stage="marketing_content",
            task="Creating content outline...",
            result="1 blog post outline created",
            progress=0.12,
            duration_seconds=2.0,
            trace_id=trace_id,
        )
        await self._run_department_step(
            mission_id,
            agent="bd",
            stage="partner_research",
            task="Identifying potential partners...",
            result="3 potential partners identified",
            progress=0.18,
            duration_seconds=2.0,
            trace_id=trace_id,
        )

        mission = update_mission_record(mission_id, status="waiting_input")
        await self._emit_mission_state(mission_id, mission, trace_id=trace_id)
        await self._emit_event(
            mission_id,
            "decision_needed",
            payload={
                "question": "Should I prioritize healthcare or fintech verticals for partnership outreach?",
                "options": ["Healthcare", "Fintech", "Both", "Defer"],
            },
            trace_id=trace_id,
        )

    async def _run_department_step(
        self,
        mission_id: str,
        *,
        agent: str,
        stage: str,
        task: str,
        result: str,
        progress: float,
        duration_seconds: float,
        trace_id: str,
    ) -> None:
        """Run a deterministic department step with standardized events."""
        await self._start_agent(
            mission_id,
            agent=agent,
            task=task,
            stage=stage,
            trace_id=trace_id,
        )
        await asyncio.sleep(duration_seconds)
        await self._complete_agent(
            mission_id,
            agent=agent,
            task=task,
            result=result,
            stage=stage,
            progress=progress,
            trace_id=trace_id,
        )

    async def process_decision(self, mission_id: str, decision: Dict[str, Any]) -> None:
        """Process a user decision and continue workflow."""
        trace_id = new_trace_id()
        mission = get_mission_record(mission_id)
        if mission is None:
            logger.warning("Mission missing; skipping decision for %s", mission_id[:8])
            return

        choice = decision.get("choice", "Both")

        try:
            mission = update_mission_record(mission_id, status="active")
            await self._emit_mission_state(mission_id, mission, trace_id=trace_id)

            await self._start_agent(
                mission_id,
                agent="bd",
                stage="decision_execution",
                task=f"Proceeding with {choice} vertical focus...",
                trace_id=trace_id,
            )

            await asyncio.sleep(1.0)
            current_progress = mission.get("progress", 0.18)
            next_progress = max(current_progress, 0.20)

            await self._complete_agent(
                mission_id,
                agent="bd",
                stage="decision_execution",
                task="Vertical selection",
                result=f"Now focusing partnership efforts on {choice}",
                progress=next_progress,
                trace_id=trace_id,
            )
        except Exception as exc:  # pylint: disable=broad-except
            await self._handle_workflow_failure(
                mission_id,
                stage="decision_processing",
                error=exc,
                trace_id=trace_id,
            )

    async def retry_mission(self, mission_id: str) -> None:
        """Restart mission workflow from mission metadata."""
        mission = get_mission_record(mission_id)
        if mission is None:
            logger.warning("Mission missing; cannot retry %s", mission_id[:8])
            return

        mission_data = {
            "business": mission.get("business", ""),
            "metric": mission.get("metric", ""),
            "goal": mission.get("goal", ""),
        }
        await self.generate_strategy(mission_id, mission_data)

    def _get_strategist_system_prompt(self) -> str:
        """System prompt for the Apex Strategist agent."""
        return (
            "You are the Apex Strategist, the CEO-level strategic brain of Consilium. "
            "Your job is to take vague SME goals and transform them into actionable, "
            "department-specific strategies.\n\n"
            "For each goal, you create a clear execution plan that includes:\n"
            "1. Strategic Overview: The big picture approach\n"
            "2. Sales Tasks: Specific outreach and lead generation activities\n"
            "3. Marketing Tasks: Content and brand awareness activities\n"
            "4. Business Development Tasks: Partnership and market expansion activities\n\n"
            "Be specific, actionable, and prioritize quick wins that build momentum.\n"
            "Output should be structured and easy to parse programmatically."
        )

    def _build_strategy_prompt(self, mission_data: Dict[str, Any]) -> str:
        """Build the strategy generation prompt."""
        business = mission_data.get("business", "Unknown business")
        metric = mission_data.get("metric", "growth")
        goal = mission_data.get("goal", "improve business outcomes")

        return (
            "Create a 90-day growth strategy for:\n\n"
            f"Business: {business}\n"
            f"Primary Metric: {metric}\n"
            f"Success Goal: {goal}\n\n"
            "Provide a structured strategy with specific tasks for Sales, Marketing, "
            "and Business Development departments. Focus on high-impact, low-effort "
            "activities that can show results within the first 2 weeks."
        )

    def _generate_demo_strategy(self, mission_data: Dict[str, Any]) -> str:
        """Generate demo strategy when API is not configured."""
        metric = mission_data.get("metric", "growth")
        goal = mission_data.get("goal", "Improve core KPI")

        return (
            f"## 90-Day Strategy: {goal}\n\n"
            "### Strategic Overview\n"
            f"Focus on {metric} through targeted outreach and strategic partnerships.\n\n"
            "### Phase 1: Quick Wins (Weeks 1-2)\n"
            "- Sales: Identify and reach out to 50 high-potential leads\n"
            "- Marketing: Create 3 pieces of thought leadership content\n"
            "- BD: Research 10 potential partnership opportunities\n\n"
            "### Phase 2: Build Momentum (Weeks 3-6)\n"
            "- Sales: Nurture leads and convert first customers\n"
            "- Marketing: Launch social media campaign\n"
            "- BD: Initiate conversations with top 3 partners\n\n"
            "### Phase 3: Scale (Weeks 7-12)\n"
            "- Sales: Implement referral program\n"
            "- Marketing: Case study publication\n"
            "- BD: Close first partnership deal"
        )


# Singleton instance
orchestrator_service = OrchestratorService()

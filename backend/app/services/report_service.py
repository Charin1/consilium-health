"""
Consultant-focused report generation service.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.config import UnifiedLLMClient
from app.services.pdf_renderer import render_text_pdf
from app.services.report_store import (
    create_report_record,
    get_report_record,
    list_report_records,
    store_report_file,
    update_report_record,
)
from app.services.workflow_contracts import build_error, utc_now

logger = logging.getLogger("consilium.reports")


class ReportService:
    """OOP service responsible for async report generation and export assets."""

    def __init__(self) -> None:
        self.llm_client = UnifiedLLMClient()
        self.prompt_version = "consultant-gtm-v1"
        logger.info(f"Report service initialized with LLM provider={self.llm_client.config.provider}")

    def create_report_job(self, payload: Dict[str, Any], *, workspace_id: str, created_by: str) -> Dict[str, Any]:
        """Create a queued report record."""
        now = utc_now()
        report_id = str(uuid4())
        title = payload.get("title") or f"{payload.get('report_type', 'strategy').replace('_', ' ').title()} Report"

        record = {
            "id": report_id,
            "report_type": payload.get("report_type", "gtm_sales_strategy"),
            "title": title,
            "status": "queued",
            "mission_id": payload.get("mission_id"),
            "workspace_id": workspace_id,
            "created_by": created_by,
            "input_payload": payload,
            "sections": [],
            "recommendations": [],
            "kpis": [],
            "prompt_version": self.prompt_version,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "file_name": f"{title.lower().replace(' ', '-')}-{report_id[:8]}.pdf",
        }

        created = create_report_record(record)
        logger.info("Report queued: report_id=%s type=%s", report_id[:8], record["report_type"])
        return created

    def list_reports(self, *, mission_id: Optional[str], workspace_id: str) -> List[Dict[str, Any]]:
        """List reports for a workspace with optional mission filter."""
        return list_report_records(mission_id=mission_id, workspace_id=workspace_id)

    async def run_report_job(self, report_id: str) -> None:
        """Execute queued report generation in background."""
        report = get_report_record(report_id)
        if report is None:
            logger.warning("Report missing; skipping job %s", report_id[:8])
            return

        try:
            update_report_record(report_id, status="running")

            sections, recommendations, kpis = await self._generate_report_content(report)
            pdf_bytes = self._build_pdf(report, sections, recommendations, kpis)
            store_report_file(report_id, pdf_bytes)

            update_report_record(
                report_id,
                status="completed",
                sections=sections,
                recommendations=recommendations,
                kpis=kpis,
                completed_at=utc_now(),
                error=None,
            )
            logger.info("Report completed: report_id=%s", report_id[:8])
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Report generation failed for %s", report_id[:8])
            error = build_error(
                "REPORT_GENERATION_FAILED",
                "Report generation failed unexpectedly.",
                retryable=True,
                stage="report_generation",
                details={"exception": str(exc), "exception_type": exc.__class__.__name__},
            )
            update_report_record(report_id, status="failed", error=error, completed_at=utc_now())

    async def _generate_report_content(
        self,
        report: Dict[str, Any],
    ) -> tuple[List[Dict[str, str]], List[str], List[Dict[str, str]]]:
        """Generate a structured consultant report."""
        payload = report.get("input_payload", {})

        ai_summary = None
        try:
            prompt = self._build_compact_prompt(payload)
            ai_summary = await self.llm_client.generate_async(
                system_prompt="You are a management consulting partner. Return concise, implementation-ready recommendations with measurable KPIs.",
                user_prompt=prompt,
                max_tokens=700,
                node="report_summary",
                session_id=report.get("id"),
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("LLM summary failed for report=%s: %s", report["id"][:8], exc)

        sections = self._build_sections(payload, ai_summary)
        recommendations = self._build_recommendations(payload)
        kpis = self._build_kpis(payload)
        return sections, recommendations, kpis

    def _build_compact_prompt(self, payload: Dict[str, Any]) -> str:
        """Compact but complete prompt template for consulting tasks."""
        return (
            "Task: Produce consultant-grade GTM + sales strategy inputs.\n"
            "Output style: compact, precise, action-oriented.\n"
            f"Business summary: {payload.get('business_summary', 'N/A')}\n"
            f"Primary goal: {payload.get('primary_goal', 'N/A')}\n"
            f"Target audience: {payload.get('target_audience', 'N/A')}\n"
            f"Offer summary: {payload.get('offer_summary', 'N/A')}\n"
            f"Industry: {payload.get('industry', 'N/A')}\n"
            f"Time horizon: {payload.get('time_horizon', '90 days')}\n"
            f"Constraints: {payload.get('constraints', 'N/A')}\n"
            f"Additional context: {payload.get('additional_context', 'N/A')}\n"
            "Return:\n"
            "1) Executive summary (5 lines)\n"
            "2) Top 5 GTM bets\n"
            "3) 30-60-90 plan\n"
            "4) 6 KPIs with targets\n"
            "5) Risks + mitigations"
        )

    def _build_sections(self, payload: Dict[str, Any], ai_summary: Optional[str]) -> List[Dict[str, str]]:
        business = payload.get("business_summary", "Business context not provided")
        goal = payload.get("primary_goal", "Achieve measurable growth")
        audience = payload.get("target_audience", "Defined ICP")
        offer = payload.get("offer_summary", "Core service offer")
        horizon = payload.get("time_horizon", "90 days")

        sections = [
            {
                "heading": "Executive Summary",
                "content": (
                    f"This engagement focuses on {goal.lower()} over {horizon}. "
                    f"The strategy prioritizes a focused ICP ({audience}), clear value articulation of {offer}, "
                    "and measurable weekly execution cadences."
                ),
            },
            {
                "heading": "Current State and Opportunity",
                "content": (
                    f"Business context: {business}. The immediate opportunity is to tighten positioning and "
                    "increase pipeline quality through channel and messaging discipline."
                ),
            },
            {
                "heading": "GTM Strategy",
                "content": (
                    "Use a two-track motion: outbound account penetration and demand capture via authority-led content. "
                    "Prioritize one core vertical before broad expansion."
                ),
            },
            {
                "heading": "Sales Execution Plan",
                "content": (
                    "Build weekly outreach sprints, enforce qualification criteria, and run objection-library based talk tracks. "
                    "Track conversion from first touch to qualified meeting and proposal close rate."
                ),
            },
            {
                "heading": "30-60-90 Day Roadmap",
                "content": (
                    "Day 0-30: positioning refresh, ICP list, outbound sequences. "
                    "Day 31-60: campaign scaling and channel optimization. "
                    "Day 61-90: expansion, partner leverage, and repeatable playbook documentation."
                ),
            },
        ]

        if ai_summary:
            sections.append(
                {
                    "heading": "AI Strategic Notes",
                    "content": ai_summary.strip()[:1400],
                }
            )

        return sections

    def _build_recommendations(self, payload: Dict[str, Any]) -> List[str]:
        goal = payload.get("primary_goal", "growth")
        return [
            "Define a single primary ICP and disqualify low-fit segments early.",
            "Create one canonical value proposition and reuse across all channels.",
            "Institute weekly GTM review with leading and lagging indicators.",
            f"Tie all campaign spend and effort directly to '{goal}'.",
            "Publish a client-facing monthly insights brief to build authority and referrals.",
        ]

    def _build_kpis(self, payload: Dict[str, Any]) -> List[Dict[str, str]]:
        horizon = payload.get("time_horizon", "90 days")
        return [
            {"name": "Qualified Meetings", "target": "+35%", "timeline": horizon},
            {"name": "Pipeline Value", "target": "+25%", "timeline": horizon},
            {"name": "Proposal Win Rate", "target": "+15%", "timeline": horizon},
            {"name": "CAC Payback", "target": "< 6 months", "timeline": horizon},
            {"name": "Response Rate", "target": "+20%", "timeline": horizon},
            {"name": "Referral Leads", "target": "+10 per quarter", "timeline": horizon},
        ]

    def _build_pdf(
        self,
        report: Dict[str, Any],
        sections: List[Dict[str, str]],
        recommendations: List[str],
        kpis: List[Dict[str, str]],
    ) -> bytes:
        """Render report data into downloadable PDF bytes."""
        lines: List[str] = []
        lines.append(f"Report ID: {report['id']}")
        lines.append(f"Type: {report.get('report_type', 'gtm_sales_strategy')}")
        lines.append("")

        for section in sections:
            lines.append(section["heading"])
            lines.append(section["content"])
            lines.append("")

        lines.append("Recommendations")
        for index, item in enumerate(recommendations, start=1):
            lines.append(f"{index}. {item}")

        lines.append("")
        lines.append("KPI Framework")
        for kpi in kpis:
            lines.append(f"- {kpi['name']}: {kpi['target']} ({kpi['timeline']})")

        return render_text_pdf(report["title"], lines)


report_service = ReportService()

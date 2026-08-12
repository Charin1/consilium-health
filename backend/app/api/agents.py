"""
Agents API - agent definitions and lightweight status endpoints.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()

AGENTS = {
    "ceo": {
        "id": "ceo",
        "name": "Chief Executive Officer",
        "role": "Vision & Enterprise Value",
        "description": "Cross-functional leadership, resource allocation, and final strategic decisions",
        "model": "llama3-70b-8192",
        "status": "idle",
    },
    "strategist": {
        "id": "strategist",
        "name": "Apex Strategist",
        "role": "Strategy & Planning",
        "description": "High-level goal setting, resource allocation, and cross-department synchronization",
        "model": "llama3-70b-8192",
        "status": "idle",
    },
    "sales": {
        "id": "sales",
        "name": "Sales Hunter",
        "role": "Sales & Outreach",
        "description": "Identifying target profiles, drafting personalized cold outreach, and CRM management",
        "model": "llama3-8b-8192",
        "status": "idle",
    },
    "marketing": {
        "id": "marketing",
        "name": "Creative Director",
        "role": "Marketing & Content",
        "description": "Campaign conceptualization, content calendars, and brand voice alignment",
        "model": "llama3-8b-8192",
        "status": "idle",
    },
    "bd": {
        "id": "bd",
        "name": "Partnership Scout",
        "role": "Business Development",
        "description": "Analyzing market gaps, finding potential collaborators, and competitor analysis",
        "model": "llama3-70b-8192",
        "status": "idle",
    },
}


@router.get("")
async def list_agents():
    """List all available agents."""
    return list(AGENTS.values())


@router.get("/{agent_id}")
async def get_agent(agent_id: str):
    """Get agent details."""
    agent = AGENTS.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.get("/{agent_id}/status")
async def get_agent_status(agent_id: str):
    """Get current agent status."""
    agent = AGENTS.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {
        "agent_id": agent_id,
        "status": agent["status"],
        "current_task": None,
    }

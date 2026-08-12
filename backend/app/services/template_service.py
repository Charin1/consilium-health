from typing import List, Dict, Any

STRATEGIC_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "strategy_quarterly",
        "title": "Quarterly Strategy & Alignment Review",
        "category": "Strategy",
        "desc": "Critique quarterly OKRs, resource allocation, and core strategic priorities across departments.",
        "icon": "🎯",
        "recommended_agent_ids": ["moderator", "ceo", "strategist", "finance", "ops", "tech"],
        "initial_prompt": "We are conducting our Quarterly Strategy & Alignment Review. Please analyze our current market positioning, evaluate top strategic initiatives, call out major alignment risks, and recommend capital/resource allocations for the upcoming quarter."
    },
    {
        "id": "expansion_international",
        "title": "International Market Expansion",
        "category": "Strategy",
        "desc": "Evaluate market entry, regulatory compliance, localized GTM, and operational setup for new regions.",
        "icon": "🌐",
        "recommended_agent_ids": ["moderator", "ceo", "strategist", "legal", "sales", "ops", "finance"],
        "initial_prompt": "Our leadership team is considering expanding into international target markets. Evaluate the market entry playbook, regulatory/legal hurdles, localized sales strategies, and operational budget required to execute safely."
    },
    {
        "id": "pitch_series_a",
        "title": "Series A / B Pitch Deck Critique",
        "category": "Fundraising",
        "desc": "Simulate venture investor Q&A, scrutinizing ARR growth metrics, CAC/LTV, TAM, and defensibility.",
        "icon": "💎",
        "recommended_agent_ids": ["moderator", "ceo", "finance", "growth", "product", "sales"],
        "initial_prompt": "We are preparing for our upcoming Series A/B institutional fundraising round. Act as prospective venture capital board partners: interrogate our metrics (ARR, NDR, CAC/LTV payback), challenge our moat/defensibility, and highlight red flags in our narrative."
    },
    {
        "id": "ma_diligence",
        "title": "M&A Due Diligence & Valuation",
        "category": "Fundraising",
        "desc": "Assess acquisition targets, technical stack integration, legal liabilities, and cultural synergy.",
        "icon": "🤝",
        "recommended_agent_ids": ["moderator", "ceo", "finance", "tech", "legal", "ops"],
        "initial_prompt": "We are evaluating a strategic acquisition target. Conduct a multi-perspective due diligence review assessing valuation multiples, tech stack technical debt, legal/IP liabilities, and post-merger integration risks."
    },
    {
        "id": "incident_postmortem",
        "title": "Crisis Management & Incident Post-Mortem",
        "category": "Operations",
        "desc": "Conduct root-cause analysis for major system outages, security breaches, or PR escalations.",
        "icon": "🚨",
        "recommended_agent_ids": ["moderator", "cto", "ciso", "ops", "legal", "customer_success"],
        "initial_prompt": "We have just experienced a critical operational incident / security escalation. Lead a transparent post-mortem analysis: identify root causes, immediate customer remediation steps, legal disclosures, and long-term preventative fixes."
    },
    {
        "id": "tech_architecture_audit",
        "title": "Tech Stack & Cybersecurity Audit",
        "category": "Operations",
        "desc": "Audit system scalability, cloud infrastructure costs, compliance (SOC2/GDPR), and security risks.",
        "icon": "🛡️",
        "recommended_agent_ids": ["moderator", "tech", "ciso", "ops", "finance"],
        "initial_prompt": "Perform a comprehensive Tech Architecture & Cybersecurity Audit. Review infrastructure scalability limitations, cloud spend efficiency, SOC2/GDPR compliance gaps, and urgent technical debt priorities."
    },
    {
        "id": "saas_pricing_overhaul",
        "title": "SaaS Pricing & Packaging Overhaul",
        "category": "Growth",
        "desc": "Debate tier restructuring, usage-based metrics, value metric alignment, and expansion revenue.",
        "icon": "📊",
        "recommended_agent_ids": ["moderator", "ceo", "finance", "growth", "product", "marketing"],
        "initial_prompt": "We are planning a total overhaul of our SaaS pricing tiers and packaging. Evaluate usage-based vs seat-based models, gross margin impacts, risk of customer churn, and strategies to maximize net dollar retention (NDR)."
    },
    {
        "id": "outbound_growth_pipeline",
        "title": "Q3 Growth & Outbound Pipeline Strategy",
        "category": "Growth",
        "desc": "Optimize Product-Led Growth (PLG) funnels, enterprise sales motion, and CAC reduction.",
        "icon": "🚀",
        "recommended_agent_ids": ["moderator", "growth", "sales", "marketing", "customer_success"],
        "initial_prompt": "Review our GTM funnel performance and outline our Q3 Growth & Outbound Pipeline Strategy. Address enterprise sales velocity, PLG trial conversions, marketing channel ROI, and customer expansion opportunities."
    },
    {
        "id": "ai_enterprise_governance",
        "title": "Enterprise AI Rollout & Governance",
        "category": "Product & AI",
        "desc": "Design Generative AI product integrations, LLM security guardrails, vendor lock-in, and ROI.",
        "icon": "🤖",
        "recommended_agent_ids": ["moderator", "ai_lead", "tech", "product", "ciso", "legal"],
        "initial_prompt": "We are architecting an Enterprise AI & Generative AI Product Integration strategy. Evaluate proprietary vs open-source model selection, data privacy guardrails, latency/cost trade-offs, and expected business ROI."
    }
]


def list_strategic_templates() -> List[Dict[str, Any]]:
    """Return list of strategic boardroom templates."""
    return STRATEGIC_TEMPLATES


def get_strategic_template(template_id: str) -> Dict[str, Any] | None:
    """Get a strategic template by ID."""
    for template in STRATEGIC_TEMPLATES:
        if template["id"] == template_id:
            return template
    return None

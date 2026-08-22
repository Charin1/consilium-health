"""
Domain metrics: LLM cost, tokens, duration, and fallback rate.

Companion to `telemetry.py` (traces) and `langfuse_client.py` (LLM traces).
RED metrics for HTTP (rate, errors, duration) and SQL come free once a
MeterProvider exists, from the same FastAPI/httpx/SQLAlchemy instrumentors
`telemetry.py` already wires up for tracing -- nothing here duplicates that.

Everything below is the layer auto-instrumentation cannot see: which pipeline
*stage* is spending the money, not just that an HTTP request happened.
Emitted from the single place that already computes these numbers --
`UnifiedLLMClient._finish_generation` in `llm_client.py`, the same call site
that feeds the Langfuse generation and the `llm.call` OTel span. One call
site, three destinations, so they can't drift out of agreement with each
other.

CARDINALITY RULE: every label here is a bounded value (a node name, a model
id, true/false). Never add session_id, persona_id, run_id, or anything
per-request -- unbounded label values make Prometheus fall over. Per-call
detail (which persona, which session) lives on the trace/span, not the
metric -- `llm.call`'s span attributes and the Langfuse generation already
carry that.
"""

from __future__ import annotations

from typing import Optional

from opentelemetry import metrics

_meter = metrics.get_meter("app.domain")

_llm_duration = _meter.create_histogram(
    "llm.call.duration",
    unit="s",
    description="Wall time of one LLM call, per pipeline node",
)
_llm_tokens = _meter.create_counter(
    "llm.tokens",
    unit="{token}",
    description="Tokens consumed, split by direction (input/output)",
)
_llm_cost = _meter.create_counter(
    "llm.cost.usd",
    unit="USD",
    description="Attributed spend per node/model, from model_registry.price_for",
)
_llm_calls = _meter.create_counter(
    "llm.calls",
    unit="{call}",
    description="One per generate_detailed call, tagged degraded=true/false",
)
_llm_quality_retry = _meter.create_counter(
    "llm.quality_retry",
    unit="{retry}",
    description=(
        "Loop engineering (ai-agents.md #6/#7): the first attempt failed the "
        "deterministic quality gate and got one corrective re-ask. A rising "
        "rate for one node is a prompt problem for that node, not noise -- "
        "same read as node_fallback_total, but for quality instead of outages."
    ),
)


def record_llm_call(
    *,
    node: str,
    provider: str,
    model: str,
    duration_s: float,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: Optional[float] = None,
    degraded: bool = False,
    quality_retried: bool = False,
) -> None:
    """Call once per `generate_detailed` invocation -- never once per round
    or per session. Per-node attribution is the entire point: when spend
    spikes, "which stage" has to be answerable without archaeology.
    """
    labels = {"node": node, "provider": provider, "model": model}
    _llm_duration.record(duration_s, {**labels, "degraded": degraded})
    _llm_calls.add(1, {**labels, "degraded": degraded})
    if tokens_in:
        _llm_tokens.add(tokens_in, {**labels, "direction": "input"})
    if tokens_out:
        _llm_tokens.add(tokens_out, {**labels, "direction": "output"})
    if cost_usd is not None:
        _llm_cost.add(cost_usd, labels)
    if quality_retried:
        _llm_quality_retry.add(1, labels)

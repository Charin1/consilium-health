"""
OpenTelemetry bootstrap: traces only.

Companion to `langfuse_client.py`, not a replacement for it. Langfuse traces
prompts/completions/cost for LLM calls specifically; this traces everything
else in a request (HTTP, SQL, outbound calls) so a slow request can be
attributed to "the database" vs "the LLM call" vs something else, instead of
guessed at. See `generate_detailed` in `llm_client.py` for where the two
meet: it opens both a Langfuse generation and a plain OTel span for the same
call, and stamps each system's trace id onto the other as metadata, so a
trace found in one tool can be pasted into the other's search.

No metrics here deliberately -- this stack is Collector -> Tempo -> Grafana,
traces-only (see scripts/setup-tempo-grafana.sh). Metrics would need
Prometheus, which isn't part of this deployment; adding a MeterProvider with
nowhere for the Collector to route it just fills the log with dropped
exports.

Best-effort like the Langfuse client: `setup_telemetry()` never raises, and
a missing/unreachable Collector degrades to no tracing, not a broken app.
"""

from __future__ import annotations

import atexit
import logging
import os
from contextlib import contextmanager
from typing import Any, Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger("consilium.telemetry")

_initialised = False
# Kept separately from the OTel *global* provider: Langfuse's own SDK is
# OTel-based internally and, since it initialises first (langfuse_client.py
# runs before this in main.py's lifespan), it already claims the process-wide
# global TracerProvider by the time this runs -- `trace.set_tracer_provider()`
# below silently no-ops (OTel only allows the global to be set once; logs a
# warning, does not raise). `span()` uses this reference directly instead of
# `trace.get_tracer()`'s implicit global lookup, so our spans still export to
# Tempo rather than being created against Langfuse's provider. Trace/span
# *context* (parent-child nesting, the shared trace_id) is unaffected either
# way -- that's propagated via contextvars, not tied to which provider made a
# given span.
_tracer_provider: Optional[TracerProvider] = None


def setup_telemetry(app: Optional[Any] = None, engine: Optional[Any] = None) -> None:
    """Call once at startup, before the app serves traffic. Idempotent."""
    global _initialised
    if _initialised:
        return

    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        logger.info("OTel tracing disabled by env (OTEL_SDK_DISABLED)")
        _initialised = True
        return

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("OTel tracing off (OTEL_EXPORTER_OTLP_ENDPOINT not set)")
        _initialised = True
        return

    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        resource = Resource.create()  # service.name etc. from OTEL_* env vars
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, insecure=endpoint.startswith("http://")),
                max_queue_size=2048,
                # 5000ms (the OTel SDK default) makes sense for a busy
                # production service batching to amortize export cost; on a
                # single-developer local stack it just means every trace you
                # produce is invisible for up to 5s for no benefit. 500ms
                # trades a negligible amount of batching efficiency for
                # traces that show up while you're still looking at the
                # screen.
                schedule_delay_millis=500,
            )
        )
        global _tracer_provider
        _tracer_provider = provider
        try:
            trace.set_tracer_provider(provider)
        except Exception:
            # Expected when Langfuse's SDK (also OTel-based) already claimed
            # the global provider by running first -- see the comment on
            # _tracer_provider above. Every instrumentor call below is passed
            # `provider` explicitly, and span()/current_trace_id() use
            # _tracer_provider directly, so this is harmless either way.
            logger.debug("global TracerProvider already set (expected if Langfuse initialised first)")
        atexit.register(_shutdown, provider)
    except Exception:
        logger.warning("OTel tracer setup failed - continuing untraced", exc_info=True)
        _initialised = True
        return

    # Each import is optional: a missing instrumentation package must not
    # take the app down.
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(
                app, tracer_provider=provider, excluded_urls="health,healthz,docs,openapi.json"
            )
        except Exception:
            logger.warning("FastAPI instrumentation unavailable", exc_info=True)

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    except Exception:
        logger.warning("httpx instrumentation unavailable", exc_info=True)

    if engine is not None:
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            SQLAlchemyInstrumentor().instrument(
                engine=engine, tracer_provider=provider, enable_commenter=False
            )
        except Exception:
            logger.warning("SQLAlchemy instrumentation unavailable", exc_info=True)

    _initialised = True
    logger.info("OTel tracing enabled -> %s", endpoint)


def _shutdown(provider: TracerProvider) -> None:
    try:
        provider.shutdown()
    except Exception:
        pass


@contextmanager
def span(name: str, **attributes: Any):
    """Manual span for work auto-instrumentation can't see (the LLM call
    itself, background jobs, boardroom nodes). No-op if tracing isn't set up
    -- always safe to wrap a call in this.

    Uses `_tracer_provider` directly rather than `trace.get_tracer()` (which
    would use the ambient *global* provider) -- see the comment on
    `_tracer_provider` above for why that global can't be trusted here.
    """
    tracer = (_tracer_provider or trace).get_tracer("app")
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, str(value))
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise


def current_trace_id() -> Optional[str]:
    """Hex trace id of the active span, or None if tracing is off / no span
    is active. Stamped onto Langfuse generations so the two systems can
    cross-reference (see generate_detailed in llm_client.py)."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None

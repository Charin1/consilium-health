"""
Langfuse client bootstrap: PHI masking, cost pricing, and the correlation ids
that make a trace findable from a message, a round, a mission, or a report.

Replaces `tracing.py`, whose `LangfuseTraceExporter.log_generation` logged
locally and never actually reached Langfuse (`push payload` was a bare
`pass`). The generation-level detail that stub was missing -- the actual
prompt, not `prompt=""` -- is now captured once, at the one place every
provider call passes through: `UnifiedLLMClient.generate_detailed`
(llm_client.py).

Contract this module keeps, matching `llm_client.py`'s:

- Every function here is best-effort. A Langfuse outage, a missing key, or a
  bad host must never fail a boardroom turn -- exceptions are caught and
  logged, never raised.
- Disabled by default without keys. `LANGFUSE_PUBLIC_KEY` +
  `LANGFUSE_SECRET_KEY` both present is what turns tracing on; anything else
  runs the app exactly as it did before this module existed.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("consilium.langfuse")

_client: Optional[Any] = None
_enabled = False
_init_lock = threading.Lock()

# Langfuse's own SaaS endpoint. Session transcripts and advisor prompts are
# PHI the moment a patient population, a payer contract, or a named account is
# discussed -- which is most boardroom sessions in this product. Silently
# defaulting a health app to a third-party SaaS host is the mistake; an
# explicit host you chose is not.
_CLOUD_HOSTS = ("cloud.langfuse.com", "us.cloud.langfuse.com", "eu.cloud.langfuse.com")

# ---------------------------------------------------------------------------
# PHI / PII masking -- runs on every prompt and completion before transport.
# Deliberately blunt: over-masking costs debugging detail, under-masking is a
# breach. Extend the patterns; never remove the hook.
# ---------------------------------------------------------------------------
_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[ssn]"),
    (re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[phone]"),
    (re.compile(r"\b[A-Z]{3}\d{6,12}\b"), "[mrn]"),  # member / medical record ids
]


def mask_phi(data: Any) -> Any:
    """Recursively redact identifiers from anything sent to Langfuse."""
    if isinstance(data, str):
        for pattern, replacement in _PATTERNS:
            data = pattern.sub(replacement, data)
        return data
    if isinstance(data, dict):
        return {k: mask_phi(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask_phi(v) for v in data]
    return data


def init_langfuse() -> Optional[Any]:
    """Call once at startup (`main.py` lifespan). Idempotent, never raises."""
    global _client, _enabled

    if _client is not None:
        return _client

    with _init_lock:
        if _client is not None:
            return _client

        public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY")
        if not public_key or not secret_key:
            logger.info(
                "Langfuse tracing is off (LANGFUSE_PUBLIC_KEY/SECRET_KEY not set). "
                "Generation cost/usage still lands in message_meta; nothing leaves the process."
            )
            return None

        host = os.getenv("LANGFUSE_HOST", "").strip()
        if not host:
            logger.warning(
                "Langfuse keys are set but LANGFUSE_HOST is not -- refusing to default to "
                "the SaaS endpoint for a health app. Set LANGFUSE_HOST explicitly "
                "(self-hosted, in your own VPC) to enable tracing."
            )
            return None
        if any(cloud in host for cloud in _CLOUD_HOSTS):
            logger.warning(
                "LANGFUSE_HOST=%s is Langfuse's SaaS endpoint. Boardroom sessions carry "
                "PHI-adjacent content (clinical, payer, and account-specific detail) -- "
                "self-host Langfuse in your own VPC before sending real sessions through it. "
                "Tracing is enabled, masking is on, but this is not a BAA-covered path.",
                host,
            )

        try:
            from langfuse import Langfuse

            _client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host,
                mask=mask_phi,
                sample_rate=float(os.getenv("LANGFUSE_SAMPLE_RATE", "1.0")),
                environment=os.getenv("DEPLOY_ENV", "dev"),
            )
            _enabled = True
            logger.info("Langfuse tracing enabled -> %s", host)
        except Exception:
            logger.warning("Langfuse init failed; continuing untraced", exc_info=True)
            _client = None

    return _client


def get_langfuse() -> Optional[Any]:
    return _client if _enabled else None


def shutdown_langfuse() -> None:
    """Call on app shutdown. Batched events are lost without this."""
    if _client is not None:
        try:
            _client.flush()
            _client.shutdown()
        except Exception:
            logger.warning("Langfuse shutdown failed", exc_info=True)


# ---------------------------------------------------------------------------
# Cost -- reuses the pricing table this repo already maintains
# (`model_registry.price_for`) instead of a second, driftable copy.
# ---------------------------------------------------------------------------
def cost_usd(provider_id: str, model_id: str, usage: Dict[str, Any]) -> Tuple[float, bool]:
    """(cost_in_usd, priced) for one call, from actual token counts."""
    from app.services.model_registry import price_for

    input_per_m, output_per_m, priced = price_for(provider_id, model_id)
    if not priced:
        return (0.0, False)
    tokens_in = usage.get("input_tokens") or 0
    tokens_out = usage.get("output_tokens") or 0
    cost = (tokens_in / 1_000_000) * input_per_m + (tokens_out / 1_000_000) * output_per_m
    return (round(cost, 6), True)

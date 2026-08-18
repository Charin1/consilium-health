"""
One LLM interface over five providers.

Anthropic, OpenAI, Google Gemini, Groq and Ollama all reach the model through
LangChain's chat integrations, which share an `.invoke()` contract. The
provider-specific part is therefore a name and a credential, both of which live
in `providers.json` -- not in branching code here.

The contract this module keeps:

- `generate()` returns a `str` and never raises. Every caller in the debate
  loop assumes that; a raising client turns one bad turn into a dead session.
- **Degradation is observable.** When the model is unreachable the result is
  still a string, but `last_result` carries `degraded=True` and a reason, and
  the caller stamps it onto the message. A canned answer presented as an
  advisor's opinion is the failure mode that matters here -- the user acts on
  it believing a specialist said it.
- Chat models are cached per (provider, model, params). Constructing an
  Anthropic client per turn would re-read credentials and rebuild an HTTP pool
  for every seat in the room.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.services.model_registry import (
    get_provider,
    has_credential,
    load_catalogue,
    model_for_seat_tier,
    resolve_model,
)

logger = logging.getLogger("consilium.llm")

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_RETRIES = 2


@dataclass
class GenerationResult:
    """What came back, and whether it came from a model at all."""
    text: str
    provider: str
    model: str
    degraded: bool = False
    reason: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    # Populated when Langfuse tracing is on (langfuse_client.py). Callers that
    # persist a message/job can store `langfuse_url` so "why did this seat say
    # that" is one click instead of a timestamp search.
    cost_usd: Optional[float] = None
    latency_ms: Optional[float] = None
    langfuse_trace_id: Optional[str] = None
    langfuse_url: Optional[str] = None


class ProviderUnavailable(RuntimeError):
    """The provider cannot be constructed -- missing package or credential."""


_model_cache: Dict[Tuple[Any, ...], Any] = {}
_cache_lock = threading.Lock()


def _build_chat_model(provider_id: str, model_id: str, temperature: float,
                      max_tokens: int, base_url: Optional[str]) -> Any:
    """Construct a LangChain chat model, or raise ProviderUnavailable."""
    provider = get_provider(provider_id)
    if provider is None:
        raise ProviderUnavailable(f"Unknown provider '{provider_id}'.")

    if provider.get("requires_key") and not has_credential(provider_id):
        raise ProviderUnavailable(
            f"{provider.get('label', provider_id)} needs {provider.get('env_key')} "
            f"and it is not set."
        )

    try:
        from langchain.chat_models import init_chat_model
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ProviderUnavailable(f"LangChain is not installed: {exc}") from exc

    kwargs: Dict[str, Any] = {
        "model_provider": provider["langchain_provider"],
        "temperature": temperature,
    }
    # Ollama runs locally and takes no key; everything else takes a token cap
    # and a timeout. Ollama's LangChain binding names the cap differently, so
    # it is left to the server's own default rather than guessed at.
    if provider_id == "ollama":
        kwargs["base_url"] = base_url or os.getenv(
            "OLLAMA_BASE_URL", "http://localhost:11434"
        )
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["timeout"] = DEFAULT_TIMEOUT_SECONDS
        kwargs["max_retries"] = DEFAULT_MAX_RETRIES

    try:
        return init_chat_model(model_id, **kwargs)
    except Exception as exc:
        raise ProviderUnavailable(
            f"Could not initialise {provider_id}/{model_id}: {exc}"
        ) from exc


def get_chat_model(provider_id: str, model_id: str, *, temperature: float = 0.4,
                   max_tokens: int = 4000, base_url: Optional[str] = None) -> Any:
    key = (provider_id, model_id, round(temperature, 2), max_tokens, base_url)
    with _cache_lock:
        cached = _model_cache.get(key)
        if cached is not None:
            return cached
    model = _build_chat_model(provider_id, model_id, temperature, max_tokens, base_url)
    with _cache_lock:
        _model_cache[key] = model
    return model


def clear_model_cache() -> None:
    """Drop cached clients. Called when settings change mid-process."""
    with _cache_lock:
        _model_cache.clear()


def _usage_from(message: Any) -> Dict[str, Any]:
    raw = getattr(message, "usage_metadata", None) or {}
    if not isinstance(raw, dict):
        return {}
    return {
        "input_tokens": raw.get("input_tokens"),
        "output_tokens": raw.get("output_tokens"),
        "total_tokens": raw.get("total_tokens"),
    }


def _text_from(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    # Anthropic and Gemini can return a list of content blocks.
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return str(content)


class UnifiedLLMClient:
    """
    Provider-agnostic client for the debate loop.

    `generate()` keeps the signature the engine has always called, so switching
    providers is a settings change rather than a code change.
    """

    def __init__(self, config: Optional[Any] = None):
        from app.config import load_config  # local import: config imports us

        self.config = config or load_config()
        self.last_result: Optional[GenerationResult] = None

    # -- resolution --------------------------------------------------------

    @property
    def provider(self) -> str:
        return getattr(self.config, "provider", None) or load_catalogue()["default_provider"]

    def model_for(self, seat_tier: Optional[int] = None) -> str:
        """The model this seat runs on: explicit override, else its tier."""
        explicit = getattr(self.config, "model", None)
        if explicit:
            return resolve_model(self.provider, explicit)
        if seat_tier is None:
            provider = get_provider(self.provider) or {}
            return provider.get("default_model", "")
        return model_for_seat_tier(self.provider, seat_tier)

    def is_ready(self) -> Tuple[bool, Optional[str]]:
        """Whether a real call is possible, and why not if it is not."""
        provider = get_provider(self.provider)
        if provider is None:
            return (False, f"Unknown provider '{self.provider}'.")
        if provider.get("requires_key") and not has_credential(self.provider):
            return (False, f"{provider['label']} needs {provider['env_key']} and it is not set.")
        return (True, None)

    # -- generation --------------------------------------------------------

    def generate_detailed(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.4,
        max_tokens: int = 4000,
        seat_tier: Optional[int] = None,
        fallback: Optional[str] = None,
        *,
        node: str = "unspecified",
        session_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        pack: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> GenerationResult:
        """
        `node`/`session_id`/`persona_id`/`pack`/`tags` are Langfuse context only
        -- every call is traced (model, prompt, completion, usage, cost,
        latency) regardless of whether a caller passes them. They just decide
        how the trace is grouped and filtered: `session_id` groups every LLM
        call in one boardroom session into one Langfuse session; `node` is
        what answers "which stage is spending the money" on the cost
        dashboard (ai-agents.md #2).
        """
        provider_id = self.provider
        model_id = self.model_for(seat_tier)
        started = time.perf_counter()

        from app.services.langfuse_client import cost_usd, get_langfuse
        from app.services.telemetry import current_trace_id, span as otel_span_cm

        # The OTel span always exists (no-op if OTel isn't configured) and
        # wraps the Langfuse generation, not the other way round, so its
        # trace id is available to attach as Langfuse metadata below. This is
        # the cross-link: a trace found in Tempo carries the Langfuse trace
        # id as a span attribute, and the Langfuse generation carries the
        # OTel trace id as metadata -- paste either into the other tool.
        with otel_span_cm(
            "llm.call", **{
                "llm.node": node, "llm.provider": provider_id, "llm.model": model_id,
                "llm.persona_id": persona_id, "llm.pack": pack,
            }
        ) as otel_span:
            otel_trace_id = current_trace_id()

            langfuse = get_langfuse()
            generation = None
            # Both entered manually (not `with`) so the same try/except/finally
            # shape below can stay a single control-flow path for the traced
            # and untraced cases. Unwound in reverse in the `finally` block.
            _open_ctxs: List[Any] = []
            if langfuse is not None:
                try:
                    from langfuse import propagate_attributes

                    gen_cm = langfuse.start_as_current_observation(
                        name=node,
                        as_type="generation",
                        model=f"{provider_id}/{model_id}",
                        input={"system": system_prompt, "user": user_prompt},
                        model_parameters={"temperature": temperature, "max_tokens": max_tokens},
                        metadata={
                            "seat_tier": seat_tier, "persona_id": persona_id, "pack": pack,
                            "otel_trace_id": otel_trace_id,
                        },
                    )
                    generation = gen_cm.__enter__()
                    _open_ctxs.append(gen_cm)

                    # session_id/tags are trace-level attributes; propagate_attributes
                    # is the mechanism for that in this SDK (there is no
                    # generation.update_trace()). Entered *inside* the observation
                    # so it still stamps the already-active root span (per its
                    # docstring), matching the nesting in Langfuse's own example.
                    prop_cm = propagate_attributes(
                        session_id=session_id,
                        tags=[t for t in [f"node:{node}", f"pack:{pack}" if pack else None,
                                          *(tags or [])] if t] or None,
                    )
                    prop_cm.__enter__()
                    _open_ctxs.append(prop_cm)
                except Exception:
                    logger.debug("Langfuse generation start failed", exc_info=True)
                    generation = None

            try:
                chat = get_chat_model(
                    provider_id,
                    model_id,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    base_url=getattr(self.config, "ollama_base_url", None),
                )
                response = chat.invoke([
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ])
                text = _text_from(response).strip()
                if not text:
                    raise RuntimeError("provider returned an empty message")
                usage = _usage_from(response)
                cost, priced = cost_usd(provider_id, model_id, usage)
                result = GenerationResult(
                    text=text, provider=provider_id, model=model_id,
                    usage=usage,
                    cost_usd=cost if priced else None,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                if generation is not None:
                    self._finish_generation(generation, result)
            except ProviderUnavailable as exc:
                logger.error("Provider unavailable (%s/%s): %s", provider_id, model_id, exc)
                result = GenerationResult(
                    text=fallback if fallback is not None else _degraded_notice(str(exc)),
                    provider=provider_id, model=model_id,
                    degraded=True, reason=str(exc),
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                if generation is not None:
                    self._finish_generation(generation, result, error=str(exc))
            except Exception as exc:
                logger.warning("Generation failed (%s/%s): %s", provider_id, model_id, exc)
                result = GenerationResult(
                    text=fallback if fallback is not None else _degraded_notice(str(exc)),
                    provider=provider_id, model=model_id,
                    degraded=True, reason=f"{exc.__class__.__name__}: {exc}",
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                if generation is not None:
                    self._finish_generation(generation, result, error=f"{exc.__class__.__name__}: {exc}")
            finally:
                for ctx in reversed(_open_ctxs):
                    try:
                        ctx.__exit__(None, None, None)
                    except Exception:
                        logger.debug("Langfuse context close failed", exc_info=True)

            if result.langfuse_trace_id:
                otel_span.set_attribute("langfuse.trace.id", result.langfuse_trace_id)
            if result.langfuse_url:
                otel_span.set_attribute("langfuse.trace.url", result.langfuse_url)
            if result.degraded:
                otel_span.set_attribute("llm.degraded", True)

            # Unconditional (unlike _finish_generation, which only runs when
            # Langfuse is configured) -- Prometheus metrics work independently
            # of whether Langfuse is on, same as the OTel span above.
            try:
                from app.services.metrics import record_llm_call

                record_llm_call(
                    node=node, provider=provider_id, model=model_id,
                    duration_s=(result.latency_ms or 0) / 1000,
                    tokens_in=result.usage.get("input_tokens") or 0,
                    tokens_out=result.usage.get("output_tokens") or 0,
                    cost_usd=result.cost_usd,
                    degraded=result.degraded,
                )
            except Exception:
                logger.debug("metric emit failed", exc_info=True)

        self.last_result = result
        return result

    @staticmethod
    def _finish_generation(generation: Any, result: GenerationResult,
                           error: Optional[str] = None) -> None:
        """Stamp the outcome onto the Langfuse generation and back onto the
        result, so a persisted message/job can carry a real deep link instead
        of a guessed one."""
        try:
            update: Dict[str, Any] = {
                "output": result.text,
                "usage_details": {k: v for k, v in result.usage.items() if v is not None},
            }
            if result.cost_usd is not None:
                update["cost_details"] = {"total": result.cost_usd}
            if error:
                update["level"] = "ERROR"
                update["status_message"] = error
            generation.update(**update)

            from app.services.langfuse_client import get_langfuse

            client = get_langfuse()
            if client is not None:
                trace_id = client.get_current_trace_id()
                if trace_id:
                    result.langfuse_trace_id = trace_id
                    try:
                        result.langfuse_url = client.get_trace_url(trace_id=trace_id)
                    except Exception:
                        pass
        except Exception:
            logger.debug("Langfuse generation update failed", exc_info=True)

    def generate(self, system_prompt: str, user_prompt: str,
                 temperature: float = 0.4, max_tokens: int = 4000,
                 seat_tier: Optional[int] = None,
                 fallback: Optional[str] = None,
                 *,
                 node: str = "unspecified",
                 session_id: Optional[str] = None,
                 persona_id: Optional[str] = None,
                 pack: Optional[str] = None,
                 tags: Optional[List[str]] = None) -> str:
        """Text only. `self.last_result` carries whether it degraded."""
        return self.generate_detailed(
            system_prompt, user_prompt,
            temperature=temperature, max_tokens=max_tokens,
            seat_tier=seat_tier, fallback=fallback,
            node=node, session_id=session_id, persona_id=persona_id,
            pack=pack, tags=tags,
        ).text

    async def generate_async(self, system_prompt: str, user_prompt: str,
                             temperature: float = 0.4, max_tokens: int = 4000,
                             seat_tier: Optional[int] = None,
                             *,
                             node: str = "unspecified",
                             session_id: Optional[str] = None,
                             persona_id: Optional[str] = None,
                             pack: Optional[str] = None,
                             tags: Optional[List[str]] = None) -> str:
        import asyncio
        return await asyncio.to_thread(
            self.generate, system_prompt, user_prompt,
            temperature=temperature, max_tokens=max_tokens, seat_tier=seat_tier,
            node=node, session_id=session_id, persona_id=persona_id,
            pack=pack, tags=tags,
        )

    # -- long transcripts --------------------------------------------------

    def generate_with_sliding_window(
        self,
        system_prompt: str,
        items: List[str],
        window_size: int = 8,
        overlap: int = 2,
        temperature: float = 0.3,
        max_tokens: int = 4000,
        seat_tier: Optional[int] = None,
        *,
        node: str = "sliding_window_synthesis",
        session_id: Optional[str] = None,
        pack: Optional[str] = None,
    ) -> str:
        """
        Map-reduce a transcript that does not fit one context window.

        Kept because Ollama models and Groq's smaller models have 8k contexts
        where the frontier providers have a million. The behaviour is unchanged
        from the pre-provider engine.

        Each chunk is its own Langfuse generation tagged `chunk:<i>/<n>` under
        the same `node` -- a map-reduce synthesis that silently costs 4x a
        normal call is exactly the prompt-bloat pattern ai-agents.md #2 wants
        visible per stage, not folded into one number.
        """
        def _gen(prompt: str, chunk_idx: int, chunk_total: int) -> str:
            return self.generate(
                system_prompt, prompt, temperature=temperature, max_tokens=max_tokens,
                seat_tier=seat_tier, node=node, session_id=session_id, pack=pack,
                tags=[f"chunk:{chunk_idx}/{chunk_total}"],
            )

        if not items:
            return _gen("No history available.", 1, 1)

        if len(items) <= window_size:
            return _gen("\n".join(items), 1, 1)

        step = max(1, window_size - overlap)
        chunks = []
        for i in range(0, len(items), step):
            chunks.append(items[i:i + window_size])
            if i + window_size >= len(items):
                break

        running = ""
        for idx, chunk in enumerate(chunks):
            chunk_text = "\n".join(chunk)
            if idx == 0:
                prompt = f"Transcript Section 1:\n{chunk_text}\n\nProduce initial executive synthesis:"
            else:
                prompt = (
                    f"Current Executive Summary (from earlier debate):\n{running}\n\n"
                    f"Next Transcript Section (with overlap):\n{chunk_text}\n\n"
                    "Refine, update, and integrate the new decisions, risks, and "
                    "action items into a cohesive updated synthesis:"
                )
            running = _gen(prompt, idx + 1, len(chunks))
        return running


def _degraded_notice(reason: str) -> str:
    """
    The only thing said when no model answered.

    Deliberately not a plausible-looking executive briefing. The previous
    fallback returned a confident block about API contracts and SOC 2 -- which,
    in a clinical session, was both off-domain and indistinguishable from a
    real advisor turn. A user acting on that believes a specialist said it.
    """
    return (
        "**No model answered this turn.**\n\n"
        f"Reason: {reason}\n\n"
        "This is not advisory output. Configure a provider in Settings and "
        "re-run the turn."
    )

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
    ) -> GenerationResult:
        provider_id = self.provider
        model_id = self.model_for(seat_tier)

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
            result = GenerationResult(
                text=text, provider=provider_id, model=model_id,
                usage=_usage_from(response),
            )
        except ProviderUnavailable as exc:
            logger.error("Provider unavailable (%s/%s): %s", provider_id, model_id, exc)
            result = GenerationResult(
                text=fallback if fallback is not None else _degraded_notice(str(exc)),
                provider=provider_id, model=model_id,
                degraded=True, reason=str(exc),
            )
        except Exception as exc:
            logger.warning("Generation failed (%s/%s): %s", provider_id, model_id, exc)
            result = GenerationResult(
                text=fallback if fallback is not None else _degraded_notice(str(exc)),
                provider=provider_id, model=model_id,
                degraded=True, reason=f"{exc.__class__.__name__}: {exc}",
            )

        self.last_result = result
        return result

    def generate(self, system_prompt: str, user_prompt: str,
                 temperature: float = 0.4, max_tokens: int = 4000,
                 seat_tier: Optional[int] = None,
                 fallback: Optional[str] = None) -> str:
        """Text only. `self.last_result` carries whether it degraded."""
        return self.generate_detailed(
            system_prompt, user_prompt,
            temperature=temperature, max_tokens=max_tokens,
            seat_tier=seat_tier, fallback=fallback,
        ).text

    async def generate_async(self, system_prompt: str, user_prompt: str,
                             temperature: float = 0.4, max_tokens: int = 4000,
                             seat_tier: Optional[int] = None) -> str:
        import asyncio
        return await asyncio.to_thread(
            self.generate, system_prompt, user_prompt,
            temperature=temperature, max_tokens=max_tokens, seat_tier=seat_tier,
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
    ) -> str:
        """
        Map-reduce a transcript that does not fit one context window.

        Kept because Ollama models and Groq's smaller models have 8k contexts
        where the frontier providers have a million. The behaviour is unchanged
        from the pre-provider engine.
        """
        if not items:
            return self.generate(system_prompt, "No history available.",
                                 temperature=temperature, max_tokens=max_tokens,
                                 seat_tier=seat_tier)

        if len(items) <= window_size:
            return self.generate(system_prompt, "\n".join(items),
                                 temperature=temperature, max_tokens=max_tokens,
                                 seat_tier=seat_tier)

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
            running = self.generate(system_prompt, prompt, temperature=temperature,
                                    max_tokens=max_tokens, seat_tier=seat_tier)
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

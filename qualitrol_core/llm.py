"""LLM client (the "AI-explained" layer) backed by Azure AI Foundry / Anthropic.

The pipeline is rules-first and always works offline. When Foundry credentials
are configured (see qualitrol_core/config.py), ``get_client()`` returns an
``AnthropicFoundryClient`` (Claude Opus 4.8) used by the augmentation layer in
``qualitrol_core.llm_extract``. Otherwise a ``NullLLMClient`` is returned and
the deterministic engine is used on its own.

Every call is defensive: failures return empty / None so the caller falls back
to the rules result rather than crashing the pipeline.
"""

from __future__ import annotations

import json
import re
from typing import Optional, Protocol

from . import config


class LLMClient(Protocol):
    @property
    def available(self) -> bool: ...

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str: ...

    def complete_json(self, system: str, user: str, *, max_tokens: int | None = None): ...


# --------------------------------------------------------------------------- #
# JSON extraction helper (LLMs sometimes wrap JSON in prose / code fences)
# --------------------------------------------------------------------------- #
def _extract_json(text: str):
    if not text:
        return None
    cleaned = text.strip()
    # Strip ```json ... ``` or ``` ... ``` fences.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back: grab the first {...} or [...] block.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


class NullLLMClient:
    """No-op client used when no LLM is configured."""

    available = False

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        return ""

    def complete_json(self, system: str, user: str, *, max_tokens: int | None = None):
        return None


class AnthropicFoundryClient:
    """Claude (Opus 4.8) via Azure AI Foundry's Anthropic-compatible endpoint."""

    def __init__(self) -> None:
        self._client = None
        self._init_error: Optional[str] = None
        s = config.SETTINGS
        self.endpoint = s.llm_endpoint
        self.api_key = s.llm_api_key
        self.deployment = s.llm_deployment
        self.max_tokens = s.llm_max_tokens
        self.temperature = s.llm_temperature
        self.timeout = s.llm_timeout
        if self.endpoint and self.api_key:
            try:
                from anthropic import AnthropicFoundry

                self._client = AnthropicFoundry(
                    api_key=self.api_key,
                    base_url=self.endpoint,
                    timeout=self.timeout,
                )
            except Exception as exc:  # pragma: no cover - import/init issues
                self._init_error = str(exc)

    @property
    def available(self) -> bool:
        return self._client is not None

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        if not self.available:
            return ""
        try:
            kwargs = {
                "model": self.deployment,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "max_tokens": max_tokens or self.max_tokens,
            }
            # Some Foundry models (e.g. Opus 4.8) reject the temperature param.
            if self.temperature is not None:
                kwargs["temperature"] = self.temperature
            try:
                message = self._client.messages.create(**kwargs)
            except Exception:
                kwargs.pop("temperature", None)
                message = self._client.messages.create(**kwargs)
            parts = [
                block.text
                for block in message.content
                if getattr(block, "type", None) == "text"
            ]
            return "".join(parts).strip()
        except Exception:  # pragma: no cover - network / API errors
            return ""

    def complete_json(self, system: str, user: str, *, max_tokens: int | None = None):
        return _extract_json(self.complete(system, user, max_tokens=max_tokens))


_client: Optional[LLMClient] = None


def get_client() -> LLMClient:
    """Return the configured LLM client (cached), or a NullLLMClient."""
    global _client
    if _client is not None:
        return _client
    if config.SETTINGS.use_llm:
        candidate = AnthropicFoundryClient()
        _client = candidate if candidate.available else NullLLMClient()
    else:
        _client = NullLLMClient()
    return _client


def reset_client() -> None:
    """Clear the cached client (useful in tests / after config changes)."""
    global _client
    _client = None

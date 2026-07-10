"""LLM client (the "AI-explained" layer) backed by Azure AI Foundry.

The pipeline is rules-first and always works offline. Clients are selected by
*role* via ``get_client(role=...)``:

  * ``"judge"`` (default) -> ``AnthropicFoundryClient`` (Claude Opus 4.8). Used
    for the precision-critical judgment tasks (scenario scope decisions, BOQ
    explanations, feedback re-decisions) in ``qualitrol_core.llm_extract``.
  * ``"bulk"``  -> ``OpenAIFoundryClient`` (e.g. gpt-5.6-sol) for high-volume
    datasheet extraction (Step 0b).
  * ``"vision"`` -> ``OpenAIFoundryClient`` for SLD/drawing image analysis.

The GPT roles fall back to the Claude client when no GPT endpoint is configured,
and everything falls back to ``NullLLMClient`` (rules-only) when no LLM is
available. Every call is defensive: failures return empty / None so the caller
falls back to the rules result rather than crashing the pipeline.
"""

from __future__ import annotations

import json
import os
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

    def complete_with_image(
        self,
        system: str,
        text: str,
        image_b64: str,
        media_type: str = "image/png",
        *,
        max_tokens: int | None = None,
    ) -> str:
        return ""

    def complete_json_with_image(
        self,
        system: str,
        text: str,
        image_b64: str,
        media_type: str = "image/png",
        *,
        max_tokens: int | None = None,
    ):
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

    def complete_with_image(
        self,
        system: str,
        text: str,
        image_b64: str,
        media_type: str = "image/png",
        *,
        max_tokens: int | None = None,
    ) -> str:
        """Send a text prompt together with a base64-encoded image (vision call)."""
        if not self.available:
            return ""
        try:
            kwargs: dict = {
                "model": self.deployment,
                "system": system,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": image_b64,
                                },
                            },
                            {"type": "text", "text": text},
                        ],
                    }
                ],
                "max_tokens": max_tokens or self.max_tokens,
            }
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

    def complete_json_with_image(
        self,
        system: str,
        text: str,
        image_b64: str,
        media_type: str = "image/png",
        *,
        max_tokens: int | None = None,
    ):
        return _extract_json(
            self.complete_with_image(
                system, text, image_b64, media_type, max_tokens=max_tokens
            )
        )


class OpenAIFoundryClient:
    """GPT (e.g. gpt-5.6-sol) via Azure AI Foundry's OpenAI-compatible endpoint.

    Reached through ``get_client(role="bulk"|"vision")`` for high-volume
    datasheet extraction and SLD vision. Judgment tasks stay on Claude. Uses the
    Responses API (``client.responses.create``). Auth is an API key when one is
    configured, otherwise Azure AD via ``DefaultAzureCredential`` (the bearer
    token is refreshed on every call so long-running workers don't expire).
    """

    def __init__(self) -> None:
        self._client = None
        self._token_provider = None
        self._init_error: Optional[str] = None
        s = config.SETTINGS
        self.endpoint = s.gpt_endpoint
        self.api_key = s.gpt_api_key
        self.deployment = s.gpt_deployment
        self.token_scope = s.gpt_token_scope
        self.max_tokens = s.gpt_max_tokens
        self.timeout = s.gpt_timeout
        if not self.endpoint:
            return
        try:
            from openai import OpenAI

            if self.api_key:
                self._client = OpenAI(
                    base_url=self.endpoint,
                    api_key=self.api_key,
                    timeout=self.timeout,
                )
            else:
                from azure.identity import (
                    DefaultAzureCredential,
                    get_bearer_token_provider,
                )

                self._token_provider = get_bearer_token_provider(
                    DefaultAzureCredential(), self.token_scope
                )
                # Seed with an initial token; _refresh_auth() renews per call.
                self._client = OpenAI(
                    base_url=self.endpoint,
                    api_key=self._token_provider(),
                    timeout=self.timeout,
                )
        except Exception as exc:  # pragma: no cover - import/init/auth issues
            self._init_error = str(exc)
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def _refresh_auth(self) -> None:
        """Renew the AAD bearer token (no-op for static API-key auth)."""
        if self._token_provider is not None and self._client is not None:
            try:
                self._client.api_key = self._token_provider()
            except Exception:  # pragma: no cover - token refresh failure
                pass

    def _output_text(self, response) -> str:
        # The Responses API exposes a convenience concatenation of text output.
        text = getattr(response, "output_text", None)
        return (text or "").strip()

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        if not self.available:
            return ""
        try:
            self._refresh_auth()
            response = self._client.responses.create(
                model=self.deployment,
                instructions=system,
                input=user,
                max_output_tokens=max_tokens or self.max_tokens,
            )
            return self._output_text(response)
        except Exception:  # pragma: no cover - network / API errors
            return ""

    def complete_json(self, system: str, user: str, *, max_tokens: int | None = None):
        return _extract_json(self.complete(system, user, max_tokens=max_tokens))

    def complete_with_image(
        self,
        system: str,
        text: str,
        image_b64: str,
        media_type: str = "image/png",
        *,
        max_tokens: int | None = None,
    ) -> str:
        if not self.available:
            return ""
        try:
            self._refresh_auth()
            response = self._client.responses.create(
                model=self.deployment,
                instructions=system,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": text},
                            {
                                "type": "input_image",
                                "image_url": f"data:{media_type};base64,{image_b64}",
                            },
                        ],
                    }
                ],
                max_output_tokens=max_tokens or self.max_tokens,
            )
            return self._output_text(response)
        except Exception:  # pragma: no cover - network / API errors
            return ""

    def complete_json_with_image(
        self,
        system: str,
        text: str,
        image_b64: str,
        media_type: str = "image/png",
        *,
        max_tokens: int | None = None,
    ):
        return _extract_json(
            self.complete_with_image(
                system, text, image_b64, media_type, max_tokens=max_tokens
            )
        )


# --------------------------------------------------------------------------- #
# Role-based client factory
# --------------------------------------------------------------------------- #
_ROLES = ("judge", "bulk", "vision")
_clients: dict[str, LLMClient] = {}


def _anthropic_client() -> LLMClient:
    if config.SETTINGS.use_llm:
        candidate = AnthropicFoundryClient()
        return candidate if candidate.available else NullLLMClient()
    return NullLLMClient()


def _openai_client() -> LLMClient:
    # Honour the global kill switch so QUALITROL_USE_LLM=0 disables everything.
    if os.getenv("QUALITROL_USE_LLM") == "0":
        return NullLLMClient()
    if config.SETTINGS.gpt_credentials_present:
        candidate = OpenAIFoundryClient()
        if candidate.available:
            return candidate
    return NullLLMClient()


def _build_client(role: str) -> LLMClient:
    if role in ("bulk", "vision"):
        gpt = _openai_client()
        if not isinstance(gpt, NullLLMClient):
            return gpt
        # No GPT endpoint configured -> fall back to Claude (offline-first).
        return _anthropic_client()
    # "judge" and any unknown role default to Claude.
    return _anthropic_client()


def get_client(role: str = "judge") -> LLMClient:
    """Return the LLM client for ``role`` (cached), or a NullLLMClient.

    Roles: ``"judge"`` (Claude, default), ``"bulk"`` and ``"vision"`` (GPT when
    configured, else Claude). Unknown roles behave like ``"judge"``.
    """
    if role not in _ROLES:
        role = "judge"
    cached = _clients.get(role)
    if cached is not None:
        return cached
    client = _build_client(role)
    _clients[role] = client
    return client


def reset_client() -> None:
    """Clear all cached clients (useful in tests / after config changes)."""
    _clients.clear()

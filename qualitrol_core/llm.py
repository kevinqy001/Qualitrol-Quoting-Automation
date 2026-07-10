"""LLM client (the "AI-explained" layer) backed by Azure AI Foundry.

The pipeline is rules-first and always works offline. Every client is Claude via
Azure AI Foundry's Anthropic-compatible endpoint, selected by *role* through
``get_client(role=...)``:

  * ``"judge"`` (default) -> Claude Opus 4.8. Precision-critical judgment tasks
    (scenario scope decisions, BOQ explanations, feedback re-decisions).
  * ``"analyze"`` -> Claude Sonnet-5. Step 1 requirement/product locator that
    reads documents against the product family/model catalog. Sonnet is the
    fastest deployment on the shared resource, so the grounded path completes
    instead of timing out.
  * ``"vision"`` -> Claude Sonnet-5. Page-image OCR and SLD/drawing analysis.
  * ``"bulk"``  -> Claude Sonnet-5. High-volume datasheet extraction (Step 0b).

Per-role deployments are overridable via env (see ``config``). Everything falls
back to ``NullLLMClient`` (rules-only) when no LLM is configured. Every call is
defensive: failures return empty / None so the caller falls back to the rules
result rather than crashing the pipeline.
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

    def __init__(self, deployment: Optional[str] = None) -> None:
        self._client = None
        self._init_error: Optional[str] = None
        s = config.SETTINGS
        self.endpoint = s.llm_endpoint
        self.api_key = s.llm_api_key
        # Same Anthropic Foundry endpoint/key; the deployment may be overridden
        # per role (e.g. a faster Sonnet model for the grounded analyze role).
        self.deployment = deployment or s.llm_deployment
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


# --------------------------------------------------------------------------- #
# Role-based client factory
# --------------------------------------------------------------------------- #
_ROLES = ("judge", "bulk", "vision", "analyze")
_clients: dict[str, LLMClient] = {}


def _anthropic_client(deployment: str | None = None) -> LLMClient:
    if config.SETTINGS.use_llm:
        candidate = AnthropicFoundryClient(deployment=deployment)
        return candidate if candidate.available else NullLLMClient()
    return NullLLMClient()


def _build_client(role: str) -> LLMClient:
    """Map a role to a Claude deployment on the Anthropic Foundry endpoint.

    ``judge`` uses Opus for precision-critical judgment; ``analyze`` (grounded
    Step 1 locator), ``vision`` (OCR / SLD) and ``bulk`` (datasheet extraction)
    default to the faster Sonnet-5. Each is overridable via env (see config).
    """
    s = config.SETTINGS
    if role == "analyze":
        return _anthropic_client(s.analyze_deployment)
    if role == "vision":
        return _anthropic_client(s.vision_deployment)
    if role == "bulk":
        return _anthropic_client(s.bulk_deployment)
    # "judge" and any unknown role default to Claude Opus (llm_deployment).
    return _anthropic_client()


def get_client(role: str = "judge") -> LLMClient:
    """Return the LLM client for ``role`` (cached), or a NullLLMClient.

    All roles are Claude on Azure AI Foundry: ``judge`` (Opus, default),
    ``analyze`` / ``vision`` / ``bulk`` (Sonnet-5). Unknown roles behave like
    ``judge``. Everything falls back to rules-only when no LLM is configured.
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

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
import logging
import os
import random
import re
import time
from typing import Optional, Protocol

from . import config


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip())
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip())
    except (TypeError, ValueError):
        return default


# Transient HTTP statuses worth retrying (rate limit + gateway/overload errors).
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def _is_retryable_error(exc: Exception) -> bool:
    """Best-effort classification of a transient (retryable) LLM error.

    Works without importing the anthropic exception classes: rate limits,
    timeouts, connection resets and 5xx gateway errors are retryable; a 4xx
    request error (e.g. malformed prompt) is not.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int):
        return status in _RETRYABLE_STATUS
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "ratelimit", "overloaded",
                               "apistatus", "internalserver", "serviceunavailable")):
        return True
    text = str(exc).lower()
    return any(k in text for k in ("time", "429", "rate limit", "overloaded",
                                   "temporar", "502", "503", "504"))


def _call_with_retries(fn, *, label: str = "llm"):
    """Call ``fn`` with bounded exponential backoff on transient errors.

    Returns ``fn()``'s result, or re-raises the last exception once retries are
    exhausted (callers already treat any exception as an empty result). Under a
    thread-pool fan-out the backoff also self-throttles against Foundry's TPM
    quota, which is why silent per-chunk timeouts drop so much less often.
    """
    max_retries = max(0, _int_env("QUALITROL_LLM_MAX_RETRIES", 2))
    base = max(0.1, _float_env("QUALITROL_LLM_RETRY_BASE", 2.0))
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classify then retry or raise
            if attempt >= max_retries or not _is_retryable_error(exc):
                raise
            delay = base * (2 ** attempt) + random.uniform(0, base)
            logging.warning(
                "%s transient error (attempt %d/%d): %s; retrying in %.1fs",
                label, attempt + 1, max_retries, exc, delay,
            )
            time.sleep(delay)
            attempt += 1


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

        def _create():
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
                return self._client.messages.create(**kwargs)
            except Exception:
                if "temperature" not in kwargs:
                    raise
                kwargs.pop("temperature", None)
                return self._client.messages.create(**kwargs)

        try:
            # Bounded exponential backoff on transient (throttle/timeout/5xx)
            # errors; still fails safe to "" so the caller falls back to rules.
            message = _call_with_retries(_create, label=self.deployment)
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

        def _create():
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
                return self._client.messages.create(**kwargs)
            except Exception:
                if "temperature" not in kwargs:
                    raise
                kwargs.pop("temperature", None)
                return self._client.messages.create(**kwargs)

        try:
            message = _call_with_retries(_create, label=f"{self.deployment}/vision")
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

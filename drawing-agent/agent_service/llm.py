"""Thin Claude client for the agent service.

Wraps either the Azure AI Foundry Anthropic endpoint (preferred, in-tenant) or a
direct Anthropic key. Exposes the two calls the service needs: a vision call
(image + text) and a tool-use turn (messages + tools) — the second is the agent
loop primitive. Mirrors the transport choice of the main app so credentials and
data residency are identical.
"""
from __future__ import annotations

from . import config


class ClaudeClient:
    def __init__(self) -> None:
        self._client = None
        self._model = config.FOUNDRY_DEPLOYMENT
        self.transport = config.claude_transport()
        if not config.claude_available():
            return
        try:
            if config.USE_BEDROCK:
                from anthropic import AnthropicBedrock

                kwargs = {"aws_region": config.AWS_REGION, "timeout": 120}
                if config.BEDROCK_BASE_URL:
                    kwargs["base_url"] = config.BEDROCK_BASE_URL
                self._client = AnthropicBedrock(**kwargs)
                self._model = config.BEDROCK_MODEL_ID
            elif config.FOUNDRY_ENDPOINT and config.FOUNDRY_API_KEY:
                from anthropic import AnthropicFoundry

                self._client = AnthropicFoundry(
                    api_key=config.FOUNDRY_API_KEY,
                    base_url=config.FOUNDRY_ENDPOINT,
                    timeout=120,
                )
                self._model = config.FOUNDRY_DEPLOYMENT
            else:
                import anthropic

                self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
                self._model = config.ANTHROPIC_MODEL
        except Exception:
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def model(self) -> str:
        return self._model

    def vision(self, system: str, text: str, image_b64: str, max_tokens: int = 8192) -> str:
        if not self.available:
            return ""
        msg = self._client.messages.create(
            model=self._model,
            system=system,
            max_tokens=max_tokens,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                        },
                        {"type": "text", "text": text},
                    ],
                }
            ],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    def turn(self, system: str, messages: list, tools: list, max_tokens: int = 2048):
        """One assistant turn with tools. Returns the raw message object."""
        return self._client.messages.create(
            model=self._model,
            system=system,
            max_tokens=max_tokens,
            tools=tools,
            messages=messages,
        )


_client: ClaudeClient | None = None


def get_client() -> ClaudeClient:
    global _client
    if _client is None:
        _client = ClaudeClient()
    return _client

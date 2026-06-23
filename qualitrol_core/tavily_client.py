"""Tavily web-research client (the data-acquisition layer for Step 0).

Wraps the Tavily Python SDK behind a small, defensive interface used by
``qualitrol_core.product_research`` to discover Qualitrol product families,
models and datasheet parameters from the public web.

Mirrors ``qualitrol_core.llm`` design:
  * ``NullTavilyClient`` when no API key is configured (Step 0 then only emits
    the planned query set rather than executing it),
  * every call fails safe (errors -> empty result) so the step never crashes.

Configure the key via env ``TAVILY_API_KEY`` or
``qualitrol_core/tavily_config.local.json`` (gitignored).
"""

from __future__ import annotations

from typing import Optional, Protocol

from . import config


class TavilyClient(Protocol):
    @property
    def available(self) -> bool: ...

    def search(self, query: str, **kwargs) -> dict: ...

    def extract(self, urls: list[str], **kwargs) -> dict: ...


class NullTavilyClient:
    """No-op client used when no Tavily API key is configured."""

    available = False

    def search(self, query: str, **kwargs) -> dict:
        return {"query": query, "results": []}

    def extract(self, urls: list[str], **kwargs) -> dict:
        return {"results": [], "failed_results": []}


class RealTavilyClient:
    """Thin wrapper over the official ``tavily-python`` SDK."""

    def __init__(self) -> None:
        self._client = None
        self._init_error: Optional[str] = None
        s = config.SETTINGS
        self.api_key = s.tavily_api_key
        self.search_depth = s.tavily_search_depth
        self.max_results = s.tavily_max_results
        self.extract_depth = s.tavily_extract_depth
        self.timeout = s.tavily_timeout
        if self.api_key:
            try:
                from tavily import TavilyClient as _SDK

                self._client = _SDK(api_key=self.api_key)
            except Exception as exc:  # pragma: no cover - import/init issues
                self._init_error = str(exc)

    @property
    def available(self) -> bool:
        return self._client is not None

    def search(self, query: str, **kwargs) -> dict:
        if not self.available:
            return {"query": query, "results": []}
        params = {
            "query": query[:400],
            "search_depth": kwargs.pop("search_depth", self.search_depth),
            "max_results": kwargs.pop("max_results", self.max_results),
        }
        params.update(kwargs)
        try:
            return self._client.search(**params)
        except Exception as exc:  # pragma: no cover - network / API errors
            return {"query": query, "results": [], "error": str(exc)}

    def extract(self, urls: list[str], **kwargs) -> dict:
        if not self.available or not urls:
            return {"results": [], "failed_results": []}
        params = {
            "urls": urls[:20],
            "extract_depth": kwargs.pop("extract_depth", self.extract_depth),
        }
        params.update(kwargs)
        try:
            return self._client.extract(**params)
        except Exception as exc:  # pragma: no cover - network / API errors
            return {"results": [], "failed_results": urls, "error": str(exc)}


_client: Optional[TavilyClient] = None


def get_client() -> TavilyClient:
    """Return the configured Tavily client (cached), or a NullTavilyClient."""
    global _client
    if _client is not None:
        return _client
    if config.SETTINGS.tavily_available:
        candidate = RealTavilyClient()
        _client = candidate if candidate.available else NullTavilyClient()
    else:
        _client = NullTavilyClient()
    return _client


def reset_client() -> None:
    """Clear the cached client (useful in tests / after config changes)."""
    global _client
    _client = None

"""Tavily web search provider."""

from typing import Any

import httpx

from provider.web_search.base import SearchResult, WebSearchProvider
from utils.config import TavilySearchConfig


class TavilyWebSearchProvider(WebSearchProvider):
    """Web search provider backed by Tavily Search API."""

    endpoint = "https://api.tavily.com/search"


    def __init__(self, config: TavilySearchConfig) -> None:
        self.config = config

    async def search(self, query: str) -> list[SearchResult]:
        """Search the web and return normalized results."""
        payload: dict[str, Any] = {
            "api_key": self.config.api_key,
            "query": query,
            "search_depth": self.config.search_depth,
            "topic": self.config.topic,
            "max_results": self.config.max_results,
            "chunks_per_source": self.config.chunks_per_source,
            "include_answer": self.config.include_answer,
            "include_raw_content": self.config.include_raw_content,
            "include_images": self.config.include_images,
            "include_image_descriptions": self.config.include_image_descriptions,
            "include_favicon": self.config.include_favicon,
            "auto_parameters": self.config.auto_parameters,
        }
        if self.config.include_domains:
            payload["include_domains"] = self.config.include_domains
        if self.config.exclude_domains:
            payload["exclude_domains"] = self.config.exclude_domains

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(self.endpoint, json=payload)
            response.raise_for_status()
            data = response.json()

        return [self._normalize_result(result) for result in data.get("results", [])]

    @staticmethod
    def _normalize_result(result: dict[str, Any]) -> SearchResult:
        return SearchResult(
            title=str(result.get("title") or ""),
            url=str(result.get("url") or ""),
            snippet=str(result.get("content") or result.get("snippet") or ""),
        )

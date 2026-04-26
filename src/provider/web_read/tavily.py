"""Tavily web read provider."""

from typing import Any

import httpx

from provider.web_read.base import ReadResult, WebReadProvider
from utils.config import TavilyWebReadConfig


class TavilyWebReadProvider(WebReadProvider):
    """Web page reader backed by Tavily Extract API."""

    endpoint = "https://api.tavily.com/extract"

    def __init__(self, config: TavilyWebReadConfig) -> None:
        self.config = config

    async def read(self, url: str) -> ReadResult:
        """Read a web page and return normalized content."""
        payload: dict[str, Any] = {
            "api_key": self.config.api_key,
            "urls": [url],
            "extract_depth": self.config.extract_depth,
            "format": self.config.format,
            "include_images": self.config.include_images,
            "include_favicon": self.config.include_favicon,
            "chunks_per_source": self.config.chunks_per_source,
        }

        async with httpx.AsyncClient(timeout=self.config.timeout or 30.0) as client:
            response = await client.post(self.endpoint, json=payload)
            response.raise_for_status()
            data = response.json()

        results = data.get("results") or []
        failed_results = data.get("failed_results") or []
        if results:
            return self._normalize_result(results[0], url)
        if failed_results:
            return self._normalize_failed_result(failed_results[0], url)
        return ReadResult(url=url, title="", content="", error="No content extracted")

    @staticmethod
    def _normalize_result(result: dict[str, Any], fallback_url: str) -> ReadResult:
        result_url = str(result.get("url") or fallback_url)
        title = str(result.get("title") or result_url)
        content = str(
            result.get("raw_content")
            or result.get("content")
            or result.get("text")
            or ""
        )
        return ReadResult(url=result_url, title=title, content=content)

    @staticmethod
    def _normalize_failed_result(result: dict[str, Any], fallback_url: str) -> ReadResult:
        result_url = str(result.get("url") or fallback_url)
        error = str(
            result.get("error")
            or result.get("message")
            or "Failed to extract content"
        )
        return ReadResult(url=result_url, title="", content="", error=error)

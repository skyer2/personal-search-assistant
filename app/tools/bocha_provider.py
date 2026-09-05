"""Bocha Web Search adapter.

The adapter intentionally returns the same payload shape as the existing
Tavily path so callers do not need to know which provider is configured.
"""

from __future__ import annotations

import os
import time
from typing import Any, Literal

import requests


SearchTopic = Literal["news", "finance", "general"]

DEFAULT_TIMEOUT_SEC = 20.0


class BochaSearchError(RuntimeError):
    """Raised when Bocha returns an unusable or failed response."""


class BochaSearchProvider:
    def __init__(
        self,
        api_key: str | None = None,
        timeout: float | None = None,
        endpoint: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._endpoint = endpoint
        self._session = session or requests.Session()

    @property
    def api_key(self) -> str:
        key = self._api_key or os.getenv("BOCHA_API_KEY", "")
        if not key:
            raise RuntimeError("BOCHA_API_KEY is not configured")
        return key

    def search(
        self,
        query: str,
        topic: SearchTopic = "general",
        max_results: int = 5,
        include_raw_content: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        request_timeout = timeout if timeout is not None else self._resolve_timeout()
        endpoint = self._endpoint or os.getenv(
            "BOCHA_SEARCH_ENDPOINT", "https://api.bocha.cn/v1/web-search"
        )
        payload = {
            "query": query,
            "summary": include_raw_content,
            "freshness": _freshness(topic),
            "count": max(1, min(int(max_results), 50)),
        }
        started = time.perf_counter()
        response = self._session.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=request_timeout,
        )
        elapsed = time.perf_counter() - started
        if response.status_code >= 400:
            raise BochaSearchError(
                f"Bocha web search failed with HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise BochaSearchError("Bocha web search returned invalid JSON") from exc
        return _to_tavily_compatible_payload(query, body, elapsed, include_raw_content)

    def _resolve_timeout(self) -> float:
        if self._timeout is not None:
            return self._timeout
        configured = os.getenv("BOCHA_TIMEOUT_SEC", os.getenv("TAVILY_TIMEOUT_SEC", ""))
        return float(configured or DEFAULT_TIMEOUT_SEC)


def _freshness(topic: SearchTopic) -> str:
    if topic == "news":
        return "oneWeek"
    if topic == "finance":
        return "oneMonth"
    return "noLimit"


def _to_tavily_compatible_payload(
    query: str,
    body: dict[str, Any],
    elapsed: float,
    include_raw_content: bool,
) -> dict[str, Any]:
    code = body.get("code")
    if code not in (None, 200):
        raise BochaSearchError(f"Bocha web search failed: {body}")
    pages = body.get("data", {}).get("webPages", {}).get("value", [])
    if not isinstance(pages, list):
        raise BochaSearchError("Bocha web search response has invalid webPages.value")
    results: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        content = str(page.get("summary") or page.get("snippet") or "")
        results.append(
            {
                "title": str(page.get("name") or ""),
                "url": str(page.get("url") or page.get("link") or ""),
                "content": content,
                "raw_content": (content or None) if include_raw_content else None,
                "score": page.get("score"),
                "site_name": page.get("siteName"),
                "published_at": page.get("datePublished"),
            }
        )
    return {
        "query": query,
        "answer": None,
        "results": results,
        "response_time": round(elapsed, 3),
        "provider": "bocha",
    }

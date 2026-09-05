"""Tests for the Bocha Web Search provider adapter."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.tools import tavily_core
from app.tools.bocha_provider import BochaSearchProvider
from app.tools.retrieval_cache import clear_retrieval_cache


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def post(self, endpoint: str, *, headers: dict, json: dict, timeout: float) -> FakeResponse:
        self.calls.append(
            {
                "endpoint": endpoint,
                "headers": headers,
                "payload": json,
                "timeout": timeout,
            }
        )
        return FakeResponse(self.payload)


def test_bocha_provider_maps_response_to_tavily_shape(monkeypatch) -> None:
    monkeypatch.setenv("BOCHA_API_KEY", "test-key")
    monkeypatch.setenv("BOCHA_TIMEOUT_SEC", "7")
    session = FakeSession(
        {
            "code": 200,
            "data": {
                "webPages": {
                    "value": [
                        {
                            "name": "OpenAI",
                            "url": "https://openai.com",
                            "summary": "AI research and deployment",
                            "siteName": "OpenAI",
                            "datePublished": "2026-01-01",
                            "score": 0.9,
                        }
                    ]
                }
            },
        }
    )
    provider = BochaSearchProvider(session=session)

    result = provider.search(
        query="OpenAI",
        topic="news",
        max_results=10,
        include_raw_content=True,
    )

    call = session.calls[0]
    assert call["endpoint"] == "https://api.bocha.cn/v1/web-search"
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["payload"] == {
        "query": "OpenAI",
        "summary": True,
        "freshness": "oneWeek",
        "count": 10,
    }
    assert call["timeout"] == 7.0
    assert result["query"] == "OpenAI"
    assert result["answer"] is None
    assert result["provider"] == "bocha"
    assert result["results"] == [
        {
            "title": "OpenAI",
            "url": "https://openai.com",
            "content": "AI research and deployment",
            "raw_content": "AI research and deployment",
            "score": 0.9,
            "site_name": "OpenAI",
            "published_at": "2026-01-01",
        }
    ]


def test_search_internet_dispatches_to_bocha(monkeypatch) -> None:
    clear_retrieval_cache()


def test_bocha_provider_omits_raw_content_when_not_requested(monkeypatch) -> None:
    monkeypatch.setenv("BOCHA_API_KEY", "test-key")
    session = FakeSession(
        {
            "code": 200,
            "data": {
                "webPages": {
                    "value": [
                        {
                            "name": "OpenAI",
                            "url": "https://openai.com",
                            "snippet": "AI research and deployment",
                        }
                    ]
                }
            },
        }
    )

    result = BochaSearchProvider(session=session).search("OpenAI")

    assert result["results"][0]["content"] == "AI research and deployment"
    assert result["results"][0]["raw_content"] is None
    monkeypatch.setenv("SEARCH_PROVIDER", "bocha")

    class Provider:
        def search(self, **kwargs):
            assert kwargs["timeout"] == 20.0
            return {"query": kwargs["query"], "results": [], "provider": "bocha"}

    monkeypatch.setattr(tavily_core, "BochaSearchProvider", Provider)

    result = tavily_core.search_internet("博查 AI", max_results=3)

    assert result == {"query": "博查 AI", "results": [], "provider": "bocha"}
    clear_retrieval_cache()


def test_unsupported_provider_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", "unsupported")
    try:
        tavily_core.search_internet("query")
    except RuntimeError as exc:
        assert str(exc) == "Unsupported SEARCH_PROVIDER: unsupported"
    else:
        raise AssertionError("expected RuntimeError")


def test_health_check_uses_provider_specific_key(monkeypatch) -> None:
    from app.api.health import _check_tavily

    monkeypatch.setenv("SEARCH_PROVIDER", "bocha")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)
    assert _check_tavily() == "down"

    monkeypatch.setenv("BOCHA_API_KEY", "bocha-key")
    assert _check_tavily() == "ok"

"""Deterministic search/fetch backend for Harness control-plane eval.

Live web pages change. L2 live scenario tests that measure Plan / Progress / Replan
must not depend on Tavily. Enable with:

    HARNESS_EVAL_FIXTURE=1
    HARNESS_EVAL_FIXTURE_PATH=/path/to/corpus.json

Unknown URLs do not fall through to the network.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_TOKEN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CORPUS = _ROOT / "tests" / "eval" / "fixtures" / "corpus.json"


def fixture_enabled() -> bool:
    return os.getenv("HARNESS_EVAL_FIXTURE", "").strip().lower() in {"1", "true", "yes", "on"}


def _corpus_path() -> Path:
    override = os.getenv("HARNESS_EVAL_FIXTURE_PATH", "").strip()
    return Path(override) if override else _DEFAULT_CORPUS


@lru_cache(maxsize=4)
def _load_corpus(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    if not path.exists():
        raise RuntimeError(f"HARNESS_EVAL_FIXTURE=true but corpus not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    documents = list(data.get("documents") or [])
    by_url = {str(doc.get("url") or ""): doc for doc in documents if doc.get("url")}
    return {"documents": documents, "by_url": by_url}


def reset_fixture_cache() -> None:
    _load_corpus.cache_clear()


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN.findall(text or "")]


def _score(query: str, document: dict[str, Any]) -> float:
    haystack = " ".join(
        [
            str(document.get("title") or ""),
            str(document.get("text") or ""),
            " ".join(str(term) for term in (document.get("terms") or [])),
        ]
    )
    hay_tokens = set(_tokens(haystack))
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    hits = sum(1 for token in query_tokens if token in hay_tokens or token in haystack.lower())
    return hits / len(query_tokens)


def search_fixture(query: str, max_results: int = 5, include_raw_content: bool = False) -> dict[str, Any]:
    corpus = _load_corpus(str(_corpus_path()))
    ranked = sorted(
        corpus["documents"],
        key=lambda doc: _score(query, doc),
        reverse=True,
    )
    scored = [(doc, _score(query, doc)) for doc in ranked if _score(query, doc) > 0]
    if not scored:
        scored = [(doc, 0.0) for doc in ranked[: max(1, max_results)]]
    selected = scored[: max(1, int(max_results or 5))]
    return {
        "query": query,
        "answer": None,
        "results": [
            {
                "title": str(doc.get("title") or doc.get("doc_id") or "fixture"),
                "url": str(doc.get("url") or ""),
                "content": str(doc.get("text") or "")[:1200],
                "raw_content": str(doc.get("text") or "") if include_raw_content else None,
                "score": round(score, 4),
                "doc_id": str(doc.get("doc_id") or ""),
            }
            for doc, score in selected
        ],
        "response_time": 0,
        "provider": "harness-eval-fixture",
    }


def fetch_fixture(url: str, max_chars: int = 8000) -> dict[str, Any]:
    corpus = _load_corpus(str(_corpus_path()))
    document = corpus["by_url"].get((url or "").strip())
    if not document:
        return {
            "ok": False,
            "error": "not_in_fixture_corpus",
            "url": url,
            "hint": "Eval fixture is isolated from live web.",
        }
    text = str(document.get("text") or "")[: max(200, int(max_chars or 8000))]
    title = str(document.get("title") or url)
    return {
        "ok": True,
        "url": url,
        "title": title,
        "snippet": text[:280],
        "char_count": len(text),
        "text": text,
        "content_type": "text/plain",
        "provider": "harness-eval-fixture",
    }

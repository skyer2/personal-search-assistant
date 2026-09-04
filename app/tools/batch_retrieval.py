"""Batch retrieval tools: parallel search + parallel fetch inside one Worker turn.

Prefer these over serial internet_search / fetch_url loops.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Literal

from langchain_core.tools import tool

from app.agent.harness.step_budget import consume_n_retrieval_or_block
from app.tools.fetch_url import fetch_url_content
from app.tools.tavily_core import search_internet

_MAX_BATCH_SEARCH = 5
_MAX_BATCH_FETCH = 5
_DEFAULT_SEARCH_WORKERS = 4
_DEFAULT_FETCH_WORKERS = 4


def _search_timeout_sec() -> float:
    return float(os.getenv("TAVILY_TIMEOUT_SEC", "20"))


def _fetch_timeout_sec() -> float:
    return float(os.getenv("FETCH_URL_TIMEOUT_SEC", "10"))


def search_one(
    query: str,
    *,
    topic: Literal["news", "finance", "general"] = "general",
    max_results: int = 5,
    include_raw_content: bool = False,
) -> dict[str, Any]:
    q = str(query or "").strip()
    if not q:
        return {"ok": False, "error": "empty_query", "query": q}
    # 缓存由 tavily_core.search_internet 统一处理，避免嵌套 single-flight 死锁
    try:
        raw = search_internet(
            query=q,
            topic=topic,
            max_results=max_results,
            include_raw_content=include_raw_content,
        )
        if isinstance(raw, dict):
            out = dict(raw)
            out.setdefault("ok", True)
            out["query"] = q
            return out
        return {"ok": True, "query": q, "results": raw}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:240], "query": q}


def fetch_one(url: str, *, max_chars: int = 8000) -> dict[str, Any]:
    target = str(url or "").strip()
    if not target:
        return {"ok": False, "error": "empty_url", "url": target}
    # 缓存由 fetch_url_content 统一处理
    return fetch_url_content(
        target,
        max_chars=max_chars,
        timeout=_fetch_timeout_sec(),
    )


def run_batch_search(
    queries: list[str],
    *,
    topic: Literal["news", "finance", "general"] = "general",
    max_results: int = 5,
    include_raw_content: bool = False,
) -> dict[str, Any]:
    cleaned = [str(q).strip() for q in (queries or []) if str(q).strip()]
    cleaned = list(dict.fromkeys(cleaned))[:_MAX_BATCH_SEARCH]
    if not cleaned:
        return {"ok": False, "error": "empty_queries", "results": []}

    blocked = consume_n_retrieval_or_block(len(cleaned), tool_name="batch_search")
    if blocked:
        return {"ok": False, "error": "step_retrieval_budget", "message": blocked, "results": []}

    results: list[dict[str, Any]] = []
    workers = min(_DEFAULT_SEARCH_WORKERS, len(cleaned))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {
            pool.submit(
                search_one,
                q,
                topic=topic,
                max_results=max_results,
                include_raw_content=include_raw_content,
            ): q
            for q in cleaned
        }
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append({"ok": False, "error": str(exc)[:240], "query": futs[fut]})

    # Preserve query order
    by_q = {str(r.get("query") or ""): r for r in results}
    ordered = [by_q.get(q) or {"ok": False, "error": "missing", "query": q} for q in cleaned]
    ok_count = sum(1 for r in ordered if r.get("ok"))
    return {
        "ok": ok_count > 0,
        "query_count": len(cleaned),
        "ok_count": ok_count,
        "timeout_sec": _search_timeout_sec(),
        "results": ordered,
        "hint": "并行搜索完成。挑高相关 URL 后用 batch_fetch 拉正文；不够再补一轮。",
    }


def run_batch_fetch(urls: list[str], *, max_chars: int = 8000) -> dict[str, Any]:
    cleaned = [str(u).strip() for u in (urls or []) if str(u).strip()]
    cleaned = list(dict.fromkeys(cleaned))[:_MAX_BATCH_FETCH]
    if not cleaned:
        return {"ok": False, "error": "empty_urls", "results": []}

    blocked = consume_n_retrieval_or_block(len(cleaned), tool_name="batch_fetch")
    if blocked:
        return {"ok": False, "error": "step_retrieval_budget", "message": blocked, "results": []}

    results: list[dict[str, Any]] = []
    workers = min(_DEFAULT_FETCH_WORKERS, len(cleaned))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(fetch_one, u, max_chars=max_chars): u for u in cleaned}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append({"ok": False, "error": str(exc)[:240], "url": futs[fut]})

    by_u = {str(r.get("url") or ""): r for r in results}
    ordered = [by_u.get(u) or {"ok": False, "error": "missing", "url": u} for u in cleaned]
    ok_count = sum(1 for r in ordered if r.get("ok"))
    return {
        "ok": ok_count > 0,
        "url_count": len(cleaned),
        "ok_count": ok_count,
        "timeout_sec": _fetch_timeout_sec(),
        "results": ordered,
        "hint": "正文已进 Artifact。需要细节时 read_artifact / read_evidence。",
    }


@tool
def batch_search(
    queries: list[str],
    topic: Literal["news", "finance", "general"] = "general",
    max_results: int = 5,
    include_raw_content: bool = False,
) -> dict[str, Any]:
    """并行执行多个搜索查询（一次工具调用）。优先使用本工具替代多次 internet_search。

    :param queries: 2~5 条互不重复的搜索词
    :param topic: news / finance / general
    :param max_results: 每条查询最多返回条数
    """
    return run_batch_search(
        queries,
        topic=topic,
        max_results=max_results,
        include_raw_content=include_raw_content,
    )


@tool
def batch_fetch(urls: list[str], max_chars: int = 8000) -> dict[str, Any]:
    """并行拉取多个 URL 正文（一次工具调用）。优先使用本工具替代多次 fetch_url。

    :param urls: 2~5 个 http(s) URL
    :param max_chars: 每个页面保留的最大正文字符数
    """
    return run_batch_fetch(urls, max_chars=max_chars)

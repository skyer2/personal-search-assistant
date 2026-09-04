"""
Tavily 搜索核心逻辑

供 LangChain @tool 与 MCP Server 共用，避免重复实现。
"""

from __future__ import annotations

import os
from typing import Any, Literal

from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv()

_client: TavilyClient | None = None
_browsecomp_retriever: Any = None
_browsecomp_database_path = ""


def get_tavily_client() -> TavilyClient:
    global _client
    if _client is None:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            raise RuntimeError("TAVILY_API_KEY is not configured")
        _client = TavilyClient(api_key=api_key)
    return _client


def search_internet(
    query: str,
    topic: Literal["news", "finance", "general"] = "general",
    max_results: int = 5,
    include_raw_content: bool = False,
) -> dict[str, Any]:
    """调用搜索后端并返回 Tavily 兼容结构。

    Benchmark 模式下强制查询 BrowseComp-Plus 固定语料，禁止访问实时网络，
    从而让同一查询的结果可复现且可计算 Gold Document Recall。
    """
    if os.getenv("BROWSECOMP_PLUS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return _search_browsecomp_plus(query, max_results, include_raw_content)

    from app.tools.eval_fixture import fixture_enabled, search_fixture

    if fixture_enabled():
        return search_fixture(query, max_results, include_raw_content)

    # Soft online timeout：宁可 miss 一个源，也不要把整个 Worker 拖成 straggler
    timeout = float(os.getenv("TAVILY_TIMEOUT_SEC", "20"))
    try:
        from app.tools.retrieval_cache import cached_call, search_cache_key

        def _call() -> dict[str, Any]:
            return get_tavily_client().search(
                query=query,
                topic=topic,
                max_results=max_results,
                include_raw_content=include_raw_content,
                timeout=timeout,
            )

        return cached_call(
            kind="search",
            cache_key=search_cache_key(str(query or ""), str(topic), int(max_results or 5)),
            producer=_call,
        )
    except Exception:
        return get_tavily_client().search(
            query=query,
            topic=topic,
            max_results=max_results,
            include_raw_content=include_raw_content,
            timeout=timeout,
        )


def _search_browsecomp_plus(
    query: str,
    max_results: int,
    include_raw_content: bool,
) -> dict[str, Any]:
    global _browsecomp_retriever, _browsecomp_database_path

    database_path = os.getenv("BROWSECOMP_PLUS_CORPUS_DB", "").strip()
    if not database_path:
        raise RuntimeError(
            "BROWSECOMP_PLUS_ENABLED=true but BROWSECOMP_PLUS_CORPUS_DB is not configured"
        )
    if _browsecomp_retriever is None or database_path != _browsecomp_database_path:
        from app.tools.browsecomp_plus import BrowseCompPlusRetriever

        _browsecomp_retriever = BrowseCompPlusRetriever(
            database_path,
            max_context_chars=int(
                os.getenv("BROWSECOMP_PLUS_MAX_CONTEXT_CHARS", "2048")
            ),
        )
        _browsecomp_database_path = database_path

    benchmark_top_k = max(1, int(os.getenv("BROWSECOMP_PLUS_TOP_K", "5")))
    results = _browsecomp_retriever.search(
        query,
        top_k=min(max(1, max_results), benchmark_top_k),
    )
    log_path = os.getenv("BROWSECOMP_PLUS_RETRIEVAL_LOG", "").strip()
    if log_path:
        from app.tools.browsecomp_plus import append_retrieval_log

        append_retrieval_log(log_path, query, results)

    return {
        "query": query,
        "answer": None,
        "results": [
            {
                "title": f"BrowseComp-Plus document {item['docid']}",
                "url": item["url"],
                "content": (
                    f"[docid:{item['docid']}] {item['text']}"
                ),
                "raw_content": item["text"] if include_raw_content else None,
                "score": item["score"],
                "doc_id": item["docid"],
            }
            for item in results
        ],
        "response_time": 0,
        "provider": "browsecomp-plus-fixed-corpus",
    }

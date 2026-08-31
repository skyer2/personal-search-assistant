"""Quick 路径：search cards → fetch_url → 轻量综合。不进 Progress / Replan。"""

from __future__ import annotations

from typing import Any


def pick_urls(cards: list[dict[str, Any]], *, limit: int = 2) -> list[str]:
    urls: list[str] = []
    for card in cards:
        url = str(card.get("url") or "").strip()
        if url.startswith("http") and url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def compose_quick_answer(
    query: str,
    cards: list[dict[str, Any]],
    fetched: list[dict[str, Any]] | None = None,
) -> str:
    """无 LLM 时的对话式回答：答案要点 + Sources。"""
    lines = [f"关于「{query.strip()[:80]}」的检索结果如下：", ""]
    used = fetched or cards
    for i, item in enumerate(used[:5], 1):
        title = str(item.get("title") or item.get("url") or f"来源 {i}")
        snippet = str(item.get("snippet") or "")[:220]
        url = str(item.get("url") or "")
        prefix = f"{i}. {title}"
        if snippet:
            prefix += f"：{snippet}"
        lines.append(prefix)
        if url:
            lines.append(f"   来源：{url}")
    if not used:
        lines.append("未检索到可用来源。")
    else:
        lines.append("")
        lines.append("Sources")
        for i, item in enumerate(used[:8], 1):
            url = str(item.get("url") or "")
            title = str(item.get("title") or url)
            if url:
                lines.append(f"[{i}] {title} {url}")
    return "\n".join(lines).strip()


def run_quick_search(query: str, *, max_queries: int = 2, max_results: int = 4) -> list[dict[str, Any]]:
    from app.tools.tavily_core import search_internet

    payload = search_internet(
        query=query,
        topic="general",
        max_results=max(1, min(int(max_results or 4), 5)),
        include_raw_content=False,
    )
    results = payload.get("results") if isinstance(payload, dict) else []
    cards: list[dict[str, Any]] = []
    for item in (results or [])[:8]:
        if not isinstance(item, dict):
            continue
        cards.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("content") or item.get("snippet") or "")[:280],
                "score": item.get("score"),
            }
        )
    _ = max_queries
    return cards


def run_quick_fetch(cards: list[dict[str, Any]], *, limit: int = 2) -> list[dict[str, Any]]:
    from app.tools.fetch_url import fetch_url_content

    fetched: list[dict[str, Any]] = []
    for url in pick_urls(cards, limit=limit):
        result = fetch_url_content(url)
        if result.get("ok"):
            fetched.append(result)
    return fetched

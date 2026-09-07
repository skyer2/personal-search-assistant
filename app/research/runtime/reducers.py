"""Send fan-out 之后的证据合并。Graph 里只合并 refs 与结构化摘要。"""

from __future__ import annotations

from typing import Any


def merge_dicts(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def keep_last(left: Any, right: Any) -> Any:
    return right if right is not None else left


def merge_worker_payloads(results: list[dict[str, Any]]) -> dict[str, Any]:
    facts: list[str] = []
    sources: list[str] = []
    seen_f: set[str] = set()
    seen_s: set[str] = set()
    for row in results:
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        for fact in payload.get("facts") or []:
            key = str(fact).strip().lower()
            if key and key not in seen_f:
                seen_f.add(key)
                facts.append(str(fact))
        for src in payload.get("sources") or []:
            key = str(src).strip().lower()
            if key and key not in seen_s:
                seen_s.add(key)
                sources.append(str(src))
    return {
        "facts": facts[:40],
        "sources": sources[:30],
        "worker_count": len(results),
    }

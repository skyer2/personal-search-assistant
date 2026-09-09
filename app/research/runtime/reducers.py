"""Send fan-out 之后的证据合并。Graph 里只合并 refs 与结构化摘要。"""

from __future__ import annotations

from typing import Any


def merge_dicts(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def keep_last(left: Any, right: Any) -> Any:
    return right if right is not None else left


def merge_records(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    """Merge record lists by stable ID fields without duplicates."""
    merged: list[Any] = []
    indexes: dict[str, int] = {}
    for row in [*(left or []), *(right or [])]:
        if not isinstance(row, dict):
            if row not in merged:
                merged.append(row)
            continue
        record_id = str(
            row.get("evidence_id")
            or row.get("claim_id")
            or row.get("edge_id")
            or row.get("gap_id")
            or row.get("candidate_id")
            or ""
        )
        if record_id and record_id in indexes:
            merged[indexes[record_id]] = row
            continue
        merged.append(row)
        if record_id:
            indexes[record_id] = len(merged) - 1
    return merged


def merge_strings(left: list[str] | None, right: list[str] | None) -> list[str]:
    return list(dict.fromkeys([*(left or []), *(right or [])]))


def merge_semantic_waves(
    left: list[dict[str, Any]] | None,
    right: list[dict[str, Any]] | None,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Merge semantic wave history by wave ID without losing earlier waves."""
    merged: dict[int | str, dict[str, Any]] = {}
    for row in [*(left or []), *(right or [])]:
        if not isinstance(row, dict):
            continue
        wave_id = row.get("wave_id")
        key = int(wave_id) if isinstance(wave_id, (int, float)) or str(wave_id).isdigit() else str(wave_id)
        merged[key] = row
    return sorted(
        merged.values(),
        key=lambda row: int(row.get("wave_id") or 0),
    )[-max(1, int(limit)):]


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

"""Deterministic identities for Supervisor research requests."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[，。！？；：、,.!?;:\n\r\t]")


def _normalize(value: Any) -> str:
    text = _PUNCTUATION.sub(" ", str(value or "").casefold())
    return _WHITESPACE.sub(" ", text).strip()


def semantic_fingerprint(
    *,
    objective: str,
    target_gaps: Iterable[str] = (),
    target_criteria: Iterable[str] = (),
) -> str:
    payload = "|".join(
        [
            _normalize(objective),
            "|".join(sorted(_normalize(item) for item in target_gaps if _normalize(item))),
            "|".join(sorted(_normalize(item) for item in target_criteria if _normalize(item))),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def execution_task_id(wave_id: int, fingerprint: str) -> str:
    return f"task_{int(wave_id):03d}_{fingerprint[:12]}"


def request_fingerprint(row: dict[str, Any] | None) -> str:
    item = row or {}
    return semantic_fingerprint(
        objective=str(item.get("objective") or ""),
        target_gaps=item.get("target_gaps") or [],
        target_criteria=item.get("target_criteria") or [],
    )


def worker_result_id(row: dict[str, Any]) -> str:
    task_id = str(row.get("task_id") or "")
    attempt = str(row.get("attempt") or "1")
    wave_id = str(row.get("dispatch_wave_id") or row.get("wave_id") or "0")
    raw_payload = row.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    summary = str(row.get("summary") or payload.get("summary") or "")
    evidence_ids = "|".join(
        str(item) for item in payload.get("evidence_ids") or row.get("evidence_ids") or []
    )
    digest = hashlib.sha256(
        f"{task_id}|{wave_id}|{attempt}|{summary}|{evidence_ids}".encode("utf-8")
    ).hexdigest()[:16]
    return f"worker_result_{digest}"


def normalize_search_query(query: str) -> str:
    return _normalize(query)


__all__ = [
    "execution_task_id",
    "normalize_search_query",
    "request_fingerprint",
    "semantic_fingerprint",
    "worker_result_id",
]

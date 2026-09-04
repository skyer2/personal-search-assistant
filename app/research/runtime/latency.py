"""Latency / critical-path helpers for Deep Research runs."""

from __future__ import annotations

import time
from typing import Any


def _latency_bucket(meta: dict[str, Any]) -> dict[str, Any]:
    bucket = meta.get("latency")
    if not isinstance(bucket, dict):
        bucket = {
            "first_evidence_ms": None,
            "enough_evidence_ms": None,
            "final_answer_ms": None,
            "waves": [],
        }
        meta["latency"] = bucket
    return bucket


def run_elapsed_ms(meta: dict[str, Any] | None) -> int | None:
    if not isinstance(meta, dict):
        return None
    started = meta.get("run_started_monotonic")
    try:
        return int((time.perf_counter() - float(started)) * 1000)
    except (TypeError, ValueError):
        return None


def note_first_evidence(state: Any, *, evidence_count: int = 1) -> None:
    if evidence_count <= 0:
        return
    meta = getattr(state, "metadata", None)
    if not isinstance(meta, dict):
        return
    bucket = _latency_bucket(meta)
    if bucket.get("first_evidence_ms") is not None:
        return
    elapsed = run_elapsed_ms(meta)
    if elapsed is not None:
        bucket["first_evidence_ms"] = elapsed
        meta["time_to_first_evidence_ms"] = elapsed


def note_enough_evidence(state: Any, *, reason: str = "") -> None:
    meta = getattr(state, "metadata", None)
    if not isinstance(meta, dict):
        return
    bucket = _latency_bucket(meta)
    if bucket.get("enough_evidence_ms") is not None:
        return
    elapsed = run_elapsed_ms(meta)
    if elapsed is not None:
        bucket["enough_evidence_ms"] = elapsed
        bucket["enough_reason"] = reason
        meta["time_to_enough_evidence_ms"] = elapsed


def note_final_answer(state: Any) -> None:
    meta = getattr(state, "metadata", None)
    if not isinstance(meta, dict):
        return
    bucket = _latency_bucket(meta)
    elapsed = run_elapsed_ms(meta)
    if elapsed is not None:
        bucket["final_answer_ms"] = elapsed
        meta["time_to_final_answer_ms"] = elapsed


def note_dispatch_wave(
    state: Any,
    *,
    task_ids: list[str],
    include_optional: bool,
) -> int:
    meta = getattr(state, "metadata", None)
    if not isinstance(meta, dict):
        return 0
    bucket = _latency_bucket(meta)
    waves = list(bucket.get("waves") or [])
    wave_id = len(waves) + 1
    waves.append(
        {
            "wave_id": wave_id,
            "task_ids": list(task_ids),
            "include_optional": include_optional,
            "started_ms": run_elapsed_ms(meta),
        }
    )
    bucket["waves"] = waves
    return wave_id


def critical_path_summary(meta: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    bucket = meta.get("latency") if isinstance(meta.get("latency"), dict) else {}
    return {
        "time_to_first_evidence_ms": bucket.get("first_evidence_ms"),
        "time_to_enough_evidence_ms": bucket.get("enough_evidence_ms"),
        "time_to_final_answer_ms": bucket.get("final_answer_ms"),
        "wave_count": len(bucket.get("waves") or []),
        "enough_reason": bucket.get("enough_reason") or "",
    }

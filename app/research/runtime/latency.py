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
            "supervisor_ms": [],
            "worker_queue_ms": [],
            "worker_execution_ms": [],
            "coverage_ms": [],
        }
        meta["latency"] = bucket
    return bucket


def run_elapsed_ms(meta: dict[str, Any] | None) -> int | None:
    if not isinstance(meta, dict):
        return None
    started = meta.get("run_started_monotonic")
    if started is None:
        return None
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


def note_stage_duration(state: Any, stage: str, duration_ms: int) -> None:
    meta = getattr(state, "metadata", None)
    if not isinstance(meta, dict) or duration_ms < 0:
        return
    bucket = _latency_bucket(meta)
    if stage in {"supervisor", "coverage"}:
        values = list(bucket.get(f"{stage}_ms") or [])
        values.append(int(duration_ms))
        bucket[f"{stage}_ms"] = values
        return
    if stage in {"brief", "synthesis", "quality", "finalize"}:
        bucket[f"{stage}_ms"] = int(duration_ms)


def note_worker_durations(state: Any, results: list[Any]) -> None:
    meta = getattr(state, "metadata", None)
    if not isinstance(meta, dict):
        return
    bucket = _latency_bucket(meta)
    queue_values = list(bucket.get("worker_queue_ms") or [])
    execution_values = list(bucket.get("worker_execution_ms") or [])
    for result in results:
        queue_values.append(int(getattr(result, "queue_ms", 0) or 0))
        execution_values.append(int(getattr(result, "execution_ms", 0) or 0))
    bucket["worker_queue_ms"] = queue_values
    bucket["worker_execution_ms"] = execution_values


def critical_path_summary(meta: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    raw_bucket = meta.get("latency")
    bucket = raw_bucket if isinstance(raw_bucket, dict) else {}
    return {
        "brief_ms": bucket.get("brief_ms"),
        "supervisor_ms": list(bucket.get("supervisor_ms") or []),
        "worker_queue_ms": list(bucket.get("worker_queue_ms") or []),
        "worker_execution_ms": list(bucket.get("worker_execution_ms") or []),
        "coverage_ms": list(bucket.get("coverage_ms") or []),
        "synthesis_ms": bucket.get("synthesis_ms"),
        "quality_ms": bucket.get("quality_ms"),
        "finalize_ms": bucket.get("finalize_ms"),
        "time_to_first_evidence_ms": bucket.get("first_evidence_ms"),
        "time_to_enough_evidence_ms": bucket.get("enough_evidence_ms"),
        "time_to_final_answer_ms": bucket.get("final_answer_ms"),
        "wave_count": len(bucket.get("waves") or []),
        "enough_reason": bucket.get("enough_reason") or "",
    }

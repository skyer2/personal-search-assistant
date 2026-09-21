"""Latency / critical-path helpers for Deep Research runs.

The runtime used to expose one coarse ``finalize_ms`` value.  That made it
impossible to distinguish research latency from delivery, persistence and
post-run exporters.  This module keeps the old keys for compatibility while
maintaining a structured, stage/substep breakdown for every run.
"""

from __future__ import annotations

import time
from typing import Any


def _latency_bucket(meta: dict[str, Any]) -> dict[str, Any]:
    bucket = meta.get("latency")
    if not isinstance(bucket, dict):
        bucket = {
            "schema_version": "latency.v2",
            "stages": {},
            "substeps": {},
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
    else:
        # Checkpointed runs may contain the pre-v2 shape.  Upgrade in place
        # without dropping fields written by older workers.
        bucket.setdefault("schema_version", "latency.v2")
        bucket.setdefault("stages", {})
        bucket.setdefault("substeps", {})
        defaults: dict[str, Any] = {
            "first_evidence_ms": None,
            "enough_evidence_ms": None,
            "final_answer_ms": None,
            "waves": [],
            "supervisor_ms": [],
            "worker_queue_ms": [],
            "worker_execution_ms": [],
            "coverage_ms": [],
        }
        for key, default in defaults.items():
            bucket.setdefault(key, default)
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
    # A graph can revisit the cheap projection node after the real operation
    # has already completed (for example after a checkpoint resume).  Keep
    # the slowest measurement and retain the aggregate so a later projection
    # cannot hide the provider time with a 3 ms bookkeeping pass.
    previous = bucket["stages"].get(stage)
    previous_duration = (
        int(previous.get("duration_ms") or 0) if isinstance(previous, dict) else 0
    )
    previous_total = (
        int(previous.get("total_ms") or previous_duration)
        if isinstance(previous, dict)
        else 0
    )
    previous_attempts = (
        int(previous.get("attempts") or 1) if isinstance(previous, dict) else 0
    )
    bucket["stages"][stage] = {
        "duration_ms": max(previous_duration, int(duration_ms)),
        "total_ms": previous_total + int(duration_ms),
        "attempts": previous_attempts + 1,
        "status": "ok",
    }
    if stage in {"supervisor", "coverage"}:
        values = list(bucket.get(f"{stage}_ms") or [])
        values.append(int(duration_ms))
        bucket[f"{stage}_ms"] = values
        return
    if stage in {
        "understand",
        "topology",
        "plan",
        "research",
        "gap_check",
        "brief",
        "synthesis",
        "quality",
        "finalize",
        "delivery",
        "post_run",
    }:
        # Understand/brief/topology may be observed once during the initial
        # compile and again by the graph projection node.  Preserve the
        # meaningful (usually slower) operation for the scalar compatibility
        # fields as well.
        prior = bucket.get(f"{stage}_ms")
        bucket[f"{stage}_ms"] = max(int(prior or 0), int(duration_ms))


def note_substep_duration(
    state: Any,
    stage: str,
    substep: str,
    duration_ms: int,
    *,
    status: str = "ok",
    input_size: int | None = None,
    output_size: int | None = None,
    model: str = "",
    tokens: int | None = None,
    retry_count: int | None = None,
) -> None:
    """Record an idempotent, JSON-serializable substep measurement.

    ``substep`` is intentionally a stable name (for example
    ``memory_extract`` or ``persist_result``), so UI and eval code can group
    runs without depending on implementation-specific log messages.
    """
    meta = getattr(state, "metadata", None)
    if not isinstance(meta, dict) or duration_ms < 0:
        return
    bucket = _latency_bucket(meta)
    key = f"{stage}.{substep}"
    existing = bucket["substeps"].get(key)
    existing_duration = (
        int(existing.get("duration_ms") or 0) if isinstance(existing, dict) else 0
    )
    existing_total = (
        int(existing.get("total_ms") or existing_duration)
        if isinstance(existing, dict)
        else 0
    )
    existing_attempts = (
        int(existing.get("attempts") or 1) if isinstance(existing, dict) else 0
    )
    row: dict[str, Any] = {
        "duration_ms": max(existing_duration, int(duration_ms)),
        "total_ms": existing_total + int(duration_ms),
        "attempts": existing_attempts + 1,
        "status": str(status or "ok"),
    }
    for name, value in (
        ("input_size", input_size),
        ("output_size", output_size),
        ("tokens", tokens),
        ("retry_count", retry_count),
    ):
        if value is not None:
            row[name] = int(value)
    if model:
        row["model"] = str(model)
    bucket["substeps"][key] = row


def note_worker_durations(state: Any, results: list[Any]) -> None:
    meta = getattr(state, "metadata", None)
    if not isinstance(meta, dict):
        return
    bucket = _latency_bucket(meta)
    workers = list(bucket.get("workers") or [])
    for result in results:
        queue_ms = int(getattr(result, "queue_ms", 0) or 0)
        execution_ms = int(getattr(result, "execution_ms", 0) or 0)
        duration_ms = int(getattr(result, "duration_ms", 0) or queue_ms + execution_ms)
        metrics = dict(getattr(result, "metrics", None) or {})
        worker_id = str(getattr(result, "task_id", "") or "")
        wave_id = int(metrics.get("dispatch_wave_id") or 0)
        attempt = int(metrics.get("worker_attempt") or 1)
        row = {
            "worker_id": worker_id,
            "wave_id": wave_id,
            "attempt": attempt,
            "queue_ms": queue_ms,
            "execution_ms": execution_ms,
            "wall_ms": duration_ms,
            "llm_ms": int(metrics.get("llm_ms") or metrics.get("llm_duration_ms") or 0),
            "ttft_ms": int(metrics.get("ttft_ms") or 0),
            "tool_ms": int(metrics.get("tool_ms") or metrics.get("tool_duration_ms") or 0),
            "context_ms": int(metrics.get("context_ms") or 0),
            "compression_ms": int(metrics.get("compression_ms") or 0),
            "evidence_ms": int(metrics.get("evidence_ms") or 0),
            "idle_ms": int(metrics.get("idle_ms") or 0),
            "llm_calls": int(metrics.get("llm_calls") or 0),
            "tool_calls": int(metrics.get("tool_calls") or 0),
            "tokens": int(metrics.get("total_tokens") or metrics.get("tokens") or 0),
        }
        identity = (worker_id, wave_id, attempt)
        replaced = False
        for index, previous in enumerate(workers):
            if not isinstance(previous, dict):
                continue
            previous_identity = (
                str(previous.get("worker_id") or ""),
                int(previous.get("wave_id") or 0),
                int(previous.get("attempt") or 1),
            )
            if previous_identity == identity:
                workers[index] = row
                replaced = True
                break
        if not replaced:
            workers.append(row)
    # Re-project compatibility arrays from the de-duplicated worker rows so a
    # checkpoint replay cannot inflate the critical-path inputs.
    bucket["worker_queue_ms"] = [
        int(row.get("queue_ms") or 0) for row in workers if isinstance(row, dict)
    ]
    bucket["worker_execution_ms"] = [
        int(row.get("execution_ms") or 0) for row in workers if isinstance(row, dict)
    ]
    bucket["workers"] = workers


def note_resource_totals(
    state: Any,
    *,
    llm_ms: int | None = None,
    ttft_ms: int | None = None,
    llm_calls: int | None = None,
    tokens: int | None = None,
    tool_ms: int | None = None,
    storage_ms: int | None = None,
    telemetry_ms: int | None = None,
) -> None:
    """Attach cross-stage resource totals without inventing missing values."""
    meta = getattr(state, "metadata", None)
    if not isinstance(meta, dict):
        return
    bucket = _latency_bucket(meta)
    for name, value in (
        ("llm_ms", llm_ms),
        ("ttft_ms", ttft_ms),
        ("llm_calls", llm_calls),
        ("tokens", tokens),
        ("tool_ms", tool_ms),
        ("storage_ms", storage_ms),
        ("telemetry_ms", telemetry_ms),
    ):
        if value is not None:
            bucket[name] = int(value)


def critical_path_summary(meta: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    raw_bucket = meta.get("latency")
    bucket = raw_bucket if isinstance(raw_bucket, dict) else {}
    stages = dict(bucket.get("stages") or {})
    substeps = dict(bucket.get("substeps") or {})
    # Persisted run summaries expose this list as ``worker_metrics`` while an
    # in-flight checkpoint uses the shorter ``workers`` key.  Accept both so
    # post-run readers can recompute the same critical path from older traces.
    worker_rows = list(bucket.get("workers") or bucket.get("worker_metrics") or [])
    if not worker_rows:
        # Older/current graph projections may preserve only the compatibility
        # arrays when parallel Send branches merge back into LoopState.  Keep
        # the critical path useful instead of reporting ``null`` merely
        # because the richer row list was lost during that merge.
        queue_values = list(bucket.get("worker_queue_ms") or [])
        execution_values = list(bucket.get("worker_execution_ms") or [])
        for index, execution_ms in enumerate(execution_values):
            worker_rows.append(
                {
                    "worker_id": f"worker_{index + 1}",
                    "wave_id": 0,
                    "attempt": 1,
                    "queue_ms": int(queue_values[index] or 0)
                    if index < len(queue_values)
                    else 0,
                    "execution_ms": int(execution_ms or 0),
                    "wall_ms": int(execution_ms or 0)
                    + (
                        int(queue_values[index] or 0)
                        if index < len(queue_values)
                        else 0
                    ),
                }
            )
    worker_sum = sum(int(row.get("wall_ms") or 0) for row in worker_rows if isinstance(row, dict))
    worker_wall = 0
    waves = list(bucket.get("waves") or [])
    if worker_rows:
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in worker_rows:
            if not isinstance(row, dict):
                continue
            grouped.setdefault(int(row.get("wave_id") or 0), []).append(row)
        if len(grouped) > 1 or (grouped and 0 not in grouped):
            worker_wall = sum(
                max(int(row.get("wall_ms") or 0) for row in rows)
                for rows in grouped.values()
                if rows
            )
        else:
            worker_wall = max(
                int(row.get("wall_ms") or 0)
                for row in worker_rows
                if isinstance(row, dict)
            )
    parallel_saved = max(0, worker_sum - worker_wall) if worker_rows else 0
    worker_llm_ms = sum(int(row.get("llm_ms") or 0) for row in worker_rows if isinstance(row, dict))
    worker_ttft_ms = sum(int(row.get("ttft_ms") or 0) for row in worker_rows if isinstance(row, dict))
    worker_tool_ms = sum(int(row.get("tool_ms") or 0) for row in worker_rows if isinstance(row, dict))
    worker_idle_ms = sum(int(row.get("idle_ms") or 0) for row in worker_rows if isinstance(row, dict))
    worker_llm_calls = sum(int(row.get("llm_calls") or 0) for row in worker_rows if isinstance(row, dict))
    worker_tool_calls = sum(int(row.get("tool_calls") or 0) for row in worker_rows if isinstance(row, dict))
    worker_tokens = sum(int(row.get("tokens") or 0) for row in worker_rows if isinstance(row, dict))
    stage_ms = {
        str(name): int((value or {}).get("duration_ms") or 0)
        for name, value in stages.items()
        if isinstance(value, dict)
    }
    # Worker nodes can execute through LangGraph fan-out, so there is no
    # single ``research`` node whose wall time can be measured directly.  The
    # critical-path wall time is the sum of the slowest worker in each wave.
    # Project that derived value as a real top-level stage so consumers do not
    # have to reconstruct it from worker rows.
    derived_stages = dict(stages)
    if worker_wall > 0:
        research_row = derived_stages.get("research")
        if not isinstance(research_row, dict) or int(
            research_row.get("duration_ms") or 0
        ) < worker_wall:
            derived_stages["research"] = {
                "duration_ms": worker_wall,
                "total_ms": worker_wall,
                "attempts": max(1, len(waves)),
                "status": "ok",
            }
        stage_ms["research"] = worker_wall

    def _substep_ms(name: str) -> int | None:
        row = substeps.get(name)
        if not isinstance(row, dict):
            return None
        value = row.get("duration_ms")
        return int(value) if value is not None else None

    total_ms = run_elapsed_ms(meta)
    if total_ms is None:
        total_ms = int(meta.get("total_latency_ms") or 0) or None
    return {
        "schema_version": "latency.v2",
        "total_ms": total_ms,
        "stages": derived_stages,
        "substeps": substeps,
        "stage_ms": stage_ms,
        # Stable SDD names; retain the legacy names below for existing UI and
        # evaluators.
        "understand_ms": stage_ms.get("understand", bucket.get("brief_ms")),
        "topology_ms": stage_ms.get("topology"),
        "planning_ms": stage_ms.get("plan", bucket.get("plan_ms")),
        "research_wall_ms": worker_wall or None,
        "gap_check_ms": stage_ms.get("gap_check", sum(int(x or 0) for x in bucket.get("coverage_ms") or [])),
        "quality_blocking_ms": stage_ms.get("quality", bucket.get("quality_ms")),
        "brief_ms": bucket.get("brief_ms"),
        "supervisor_ms": list(bucket.get("supervisor_ms") or []),
        "worker_queue_ms": list(bucket.get("worker_queue_ms") or []),
        "worker_execution_ms": list(bucket.get("worker_execution_ms") or []),
        "coverage_ms": list(bucket.get("coverage_ms") or []),
        "synthesis_ms": bucket.get("synthesis_ms"),
        "synthesis_evidence_select_ms": _substep_ms("synthesis.evidence_select"),
        "synthesis_evidence_pack_ms": _substep_ms("synthesis.evidence_pack"),
        "synthesis_prompt_build_ms": _substep_ms("synthesis.prompt_build"),
        "synthesis_provider_queue_ms": _substep_ms("synthesis.provider_queue"),
        "synthesis_ttft_ms": _substep_ms("synthesis.ttft"),
        "synthesis_generation_ms": _substep_ms("synthesis.generation"),
        "synthesis_parse_ms": _substep_ms("synthesis.parse"),
        "synthesis_citation_ms": _substep_ms("synthesis.citation_binding"),
        "synthesis_validation_ms": _substep_ms("synthesis.validation"),
        "quality_ms": bucket.get("quality_ms"),
        "finalize_ms": bucket.get("finalize_ms"),
        "delivery_ms": bucket.get("delivery_ms"),
        "post_run_ms": bucket.get("post_run_ms"),
        "research_worker_sum_ms": worker_sum or None,
        "research_parallel_saved_ms": parallel_saved or None,
        "worker_metrics": worker_rows,
        "worker_llm_ratio": round(worker_llm_ms / worker_sum, 4) if worker_sum else 0.0,
        "worker_tool_ratio": round(worker_tool_ms / worker_sum, 4) if worker_sum else 0.0,
        "worker_idle_ratio": round(worker_idle_ms / worker_sum, 4) if worker_sum else 0.0,
        "llm_ms": bucket.get("llm_ms") if bucket.get("llm_ms") is not None else worker_llm_ms or None,
        "ttft_ms": bucket.get("ttft_ms") if bucket.get("ttft_ms") is not None else worker_ttft_ms or None,
        "llm_calls": bucket.get("llm_calls") if bucket.get("llm_calls") is not None else worker_llm_calls or None,
        "tokens": bucket.get("tokens") if bucket.get("tokens") is not None else worker_tokens or None,
        "tool_calls": bucket.get("tool_calls") if bucket.get("tool_calls") is not None else worker_tool_calls or None,
        "tool_ms": bucket.get("tool_ms") if bucket.get("tool_ms") is not None else worker_tool_ms or None,
        "storage_ms": bucket.get("storage_ms"),
        "telemetry_ms": bucket.get("telemetry_ms"),
        "time_to_first_evidence_ms": bucket.get("first_evidence_ms"),
        "time_to_enough_evidence_ms": bucket.get("enough_evidence_ms"),
        "time_to_final_answer_ms": bucket.get("final_answer_ms"),
        "wave_count": len(waves),
        "enough_reason": bucket.get("enough_reason") or "",
    }

"""Trace retention / sampling helpers for durable JSONL observability."""

from __future__ import annotations

import os
import random
import time
from pathlib import Path


def sample_rate() -> float:
    raw = (os.getenv("OBS_TRACE_SAMPLE_RATE") or os.getenv("HARNESS_OBS_TRACE_SAMPLE_RATE") or "1").strip()
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    return max(0.0, min(1.0, value))


def should_sample(event_type: str) -> bool:
    """Always keep workflow/semantic events; optionally sample high-volume gen_ai/tool noise."""
    rate = sample_rate()
    if rate >= 1.0:
        return True
    if event_type in {
        "run.started",
        "run.completed",
        "run.failed",
        "run_summary",
        "brief.compiled",
        "plan.created",
        "plan.validated",
        "progress.evaluated",
        "replan.applied",
        "replan.proposed",
        "replan.rejected",
        "synthesis.started",
        "synthesis.completed",
        "synthesis.failed",
        "quality.evaluated",
        "eval.scored",
        "checkpoint.saved",
        "checkpoint.resumed",
        "budget.exhausted",
        "recovery.decided",
        "context.built",
        "context.compressed",
    }:
        return True
    return random.random() <= rate


def retention_days() -> int:
    raw = (os.getenv("OBS_TRACE_RETENTION_DAYS") or os.getenv("HARNESS_OBS_TRACE_RETENTION_DAYS") or "14").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 14


def prune_trace_files(log_dir: Path, *, now: float | None = None) -> int:
    """Delete aged JSONL/payload files. Returns number of removed paths."""
    if not log_dir.exists():
        return 0
    cutoff = (now or time.time()) - retention_days() * 86400
    removed = 0
    candidates = list(log_dir.rglob("*.jsonl")) + list((log_dir / "payloads").rglob("*.json") if (log_dir / "payloads").exists() else [])
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed

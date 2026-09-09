"""
【Phase 9】从 JSONL run_summary 聚合 Harness 在线指标

供 GET /api/metrics/summary 与 Prometheus scrape 使用。
口径：扫描 logs/traces/{session}/{run}.jsonl 中 phase=run 且 event=run_summary 的记录。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        text = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


@dataclass
class AggregatedMetrics:
    """滚动窗口内 Harness run 聚合指标。"""

    window_hours: int = 168
    runs_total: int = 0
    runs_success: int = 0
    runs_partial: int = 0
    runs_failed: int = 0
    avg_latency_ms: float = 0.0
    avg_tool_calls: float = 0.0
    avg_step_success_rate: float = 0.0
    avg_compression_ratio: float = 1.0
    avg_citation_coverage_rate: float | None = None
    avg_hallucination_rate: float | None = None
    structured_output_compliance_rate: float | None = None
    orchestration_violation_rate: float | None = None
    avg_tokens_saved: float = 0.0
    parallel_batch_total: int = 0
    parallel_steps_total: int = 0
    latency_p50_ms: float = 0.0
    latency_p90_ms: float = 0.0
    latency_p95_ms: float = 0.0
    supervisor_iteration_rate: float | None = None
    supervisor_recovery_rate: float | None = None
    avg_supervisor_iterations: float = 0.0
    source_files_scanned: int = 0
    oldest_run_at: str | None = None
    newest_run_at: str | None = None
    by_status: dict[str, int] = field(default_factory=dict)

    @property
    def task_success_rate(self) -> float:
        if self.runs_total <= 0:
            return 0.0
        return round(self.runs_success / self.runs_total, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_hours": self.window_hours,
            "runs_total": self.runs_total,
            "runs_success": self.runs_success,
            "runs_partial": self.runs_partial,
            "runs_failed": self.runs_failed,
            "task_success_rate": self.task_success_rate,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "avg_tool_calls": round(self.avg_tool_calls, 2),
            "avg_step_success_rate": round(self.avg_step_success_rate, 3),
            "avg_compression_ratio": round(self.avg_compression_ratio, 3),
            "avg_citation_coverage_rate": self.avg_citation_coverage_rate,
            "avg_hallucination_rate": self.avg_hallucination_rate,
            "structured_output_compliance_rate": self.structured_output_compliance_rate,
            "orchestration_violation_rate": self.orchestration_violation_rate,
            "avg_tokens_saved": round(self.avg_tokens_saved, 1),
            "parallel_batch_total": self.parallel_batch_total,
            "parallel_steps_total": self.parallel_steps_total,
            "latency_p50_ms": round(self.latency_p50_ms, 1),
            "latency_p90_ms": round(self.latency_p90_ms, 1),
            "latency_p95_ms": round(self.latency_p95_ms, 1),
            "supervisor_iteration_rate": self.supervisor_iteration_rate,
            "supervisor_recovery_rate": self.supervisor_recovery_rate,
            "avg_supervisor_iterations": round(self.avg_supervisor_iterations, 2),
            "source_files_scanned": self.source_files_scanned,
            "oldest_run_at": self.oldest_run_at,
            "newest_run_at": self.newest_run_at,
            "by_status": self.by_status,
        }


def collect_run_summaries(
    log_dir: Path,
    window_hours: int = 168,
) -> list[dict[str, Any]]:
    """扫描 JSONL，返回窗口内的 run_summary 记录（含 metadata）。"""
    if not log_dir.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, window_hours))
    summaries: list[dict[str, Any]] = []

    for path in sorted(log_dir.glob("*/*.jsonl")):
        if path.name == "index.jsonl":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("phase") != "run":
                continue
            extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
            event_name = str(extra.get("event") or record.get("event") or "")
            if event_name != "run_summary":
                continue
            ts = _parse_ts(record.get("timestamp"))
            if ts is not None and ts < cutoff:
                continue
            metadata = extra.get("metadata") or record.get("metadata") or {}
            summaries.append(
                {
                    "timestamp": record.get("timestamp"),
                    "session_id": record.get("session_id"),
                    "run_id": record.get("run_id") or record.get("session_id"),
                    "trace_id": record.get("trace_id"),
                    "status": record.get("status"),
                    "duration_ms": record.get("duration_ms"),
                    "metadata": metadata,
                }
            )
    return summaries


def aggregate_metrics(
    log_dir: Path,
    window_hours: int = 168,
) -> AggregatedMetrics:
    """【Phase 9】聚合 JSONL run_summary → 在线指标。"""
    summaries = collect_run_summaries(log_dir, window_hours=window_hours)
    agg = AggregatedMetrics(window_hours=window_hours)
    agg.source_files_scanned = len(list(log_dir.glob("*/*.jsonl"))) if log_dir.exists() else 0

    if not summaries:
        return agg

    latencies: list[float] = []
    tool_calls: list[float] = []
    step_rates: list[float] = []
    compression: list[float] = []
    ccr_list: list[float] = []
    hr_list: list[float] = []
    jcr_checks = 0
    jcr_passes = 0
    ovr_violations = 0
    ovr_checks = 0
    tokens_saved: list[float] = []
    supervisor_iterations: list[float] = []

    timestamps: list[str] = []

    for item in summaries:
        status = str(item.get("status") or "unknown")
        agg.by_status[status] = agg.by_status.get(status, 0) + 1
        agg.runs_total += 1
        if status == "success":
            agg.runs_success += 1
        elif status == "partial":
            agg.runs_partial += 1
        else:
            agg.runs_failed += 1

        meta = item.get("metadata") or {}
        if item.get("duration_ms") is not None:
            latencies.append(float(item["duration_ms"]))
        if meta.get("tool_calls_count") is not None:
            tool_calls.append(float(meta["tool_calls_count"]))
        if meta.get("step_success_rate") is not None:
            step_rates.append(float(meta["step_success_rate"]))
        cr = _safe_float(meta.get("avg_compression_ratio"))
        if cr is not None and cr < 1.0:
            compression.append(cr)
        ccr = _safe_float(meta.get("citation_coverage_rate"))
        if ccr is not None:
            ccr_list.append(ccr)
        hr = _safe_float(meta.get("hallucination_rate"))
        if hr is not None:
            hr_list.append(hr)

        obs = meta.get("observability") or {}
        checks = int(obs.get("structured_output_checks") or 0)
        passes = int(obs.get("structured_output_passes") or 0)
        jcr_checks += checks
        jcr_passes += passes
        ovr_violations += int(obs.get("orchestration_violation_count") or 0)
        ovr_checks += checks + int(obs.get("binding_violation_count") or 0)
        agg.parallel_batch_total += int(obs.get("parallel_batch_count") or 0)
        agg.parallel_steps_total += int(obs.get("parallel_steps_executed") or 0)
        saved = int(obs.get("estimated_tokens_saved") or 0)
        if saved > 0:
            tokens_saved.append(float(saved))
        supervisor_iterations.append(float(meta.get("supervisor_iterations") or 0))

        ts = item.get("timestamp")
        if ts:
            timestamps.append(str(ts))

    agg.avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0.0
    from app.observability.metrics import _percentile

    ordered = sorted(latencies)
    agg.latency_p50_ms = _percentile(ordered, 0.50)
    agg.latency_p90_ms = _percentile(ordered, 0.90)
    agg.latency_p95_ms = _percentile(ordered, 0.95)
    agg.avg_supervisor_iterations = (
        sum(supervisor_iterations) / len(supervisor_iterations) if supervisor_iterations else 0.0
    )
    iterated_runs = sum(1 for item in supervisor_iterations if item > 1)
    iterated_recovered = 0
    for item, row in zip(supervisor_iterations, summaries):
        if item > 1 and str(row.get("status") or "") == "success":
            iterated_recovered += 1
    agg.supervisor_iteration_rate = (
        round(iterated_runs / len(supervisor_iterations), 3) if supervisor_iterations else None
    )
    agg.supervisor_recovery_rate = (
        round(iterated_recovered / iterated_runs, 3) if iterated_runs else None
    )
    agg.avg_tool_calls = sum(tool_calls) / len(tool_calls) if tool_calls else 0.0
    agg.avg_step_success_rate = sum(step_rates) / len(step_rates) if step_rates else 0.0
    agg.avg_compression_ratio = sum(compression) / len(compression) if compression else 1.0
    agg.avg_citation_coverage_rate = (
        round(sum(ccr_list) / len(ccr_list), 3) if ccr_list else None
    )
    agg.avg_hallucination_rate = (
        round(sum(hr_list) / len(hr_list), 3) if hr_list else None
    )
    agg.structured_output_compliance_rate = (
        round(jcr_passes / jcr_checks, 3) if jcr_checks > 0 else None
    )
    agg.orchestration_violation_rate = (
        round(ovr_violations / ovr_checks, 3) if ovr_checks > 0 else None
    )
    agg.avg_tokens_saved = sum(tokens_saved) / len(tokens_saved) if tokens_saved else 0.0
    if timestamps:
        agg.oldest_run_at = min(timestamps)
        agg.newest_run_at = max(timestamps)
    return agg


def render_prometheus_text(metrics: AggregatedMetrics) -> str:
    """Prometheus exposition format（企业监控栈可直接 scrape）。"""
    lines = [
        "# HELP harness_window_runs Total harness runs in rolling JSONL window (gauge, not a counter)",
        "# TYPE harness_window_runs gauge",
        f"harness_window_runs {metrics.runs_total}",
        "# HELP harness_runs_total Deprecated alias of harness_window_runs; not a true counter",
        "# TYPE harness_runs_total gauge",
        f"harness_runs_total {metrics.runs_total}",
        "# HELP harness_task_success_rate Success rate in rolling window",
        "# TYPE harness_task_success_rate gauge",
        f"harness_task_success_rate {metrics.task_success_rate}",
        "# HELP harness_avg_latency_ms Average end-to-end latency ms",
        "# TYPE harness_avg_latency_ms gauge",
        f"harness_avg_latency_ms {metrics.avg_latency_ms:.1f}",
        "# HELP harness_latency_ms Run duration percentiles from JSONL window",
        "# TYPE harness_latency_ms summary",
        f"harness_latency_ms{{quantile=\"0.5\"}} {metrics.latency_p50_ms:.1f}",
        f"harness_latency_ms{{quantile=\"0.9\"}} {metrics.latency_p90_ms:.1f}",
        f"harness_latency_ms{{quantile=\"0.95\"}} {metrics.latency_p95_ms:.1f}",
        "# HELP harness_avg_tool_calls Average tool calls per run",
        "# TYPE harness_avg_tool_calls gauge",
        f"harness_avg_tool_calls {metrics.avg_tool_calls:.2f}",
        "# HELP harness_supervisor_iteration_rate Share of runs with a second supervisor research iteration",
        "# TYPE harness_supervisor_iteration_rate gauge",
        f"harness_supervisor_iteration_rate {metrics.supervisor_iteration_rate if metrics.supervisor_iteration_rate is not None else 0}",
        "# HELP harness_supervisor_recovery_rate Success rate among runs that iterated",
        "# TYPE harness_supervisor_recovery_rate gauge",
        f"harness_supervisor_recovery_rate {metrics.supervisor_recovery_rate if metrics.supervisor_recovery_rate is not None else 0}",
        "# HELP harness_structured_output_compliance_rate Worker JSON compliance",
        "# TYPE harness_structured_output_compliance_rate gauge",
    ]
    jcr = metrics.structured_output_compliance_rate
    lines.append(
        f"harness_structured_output_compliance_rate {jcr if jcr is not None else 0}"
    )
    # Quality scores (accuracy / grounding) intentionally stay in Eval Store / Trace summary,
    # not Prometheus counters — see docs/OBSERVABILITY.md.
    lines.extend(
        [
            "# HELP harness_estimated_tokens_saved_avg Avg tokens saved by compression",
            "# TYPE harness_estimated_tokens_saved_avg gauge",
            f"harness_estimated_tokens_saved_avg {metrics.avg_tokens_saved:.1f}",
            "# HELP harness_parallel_batches_total Parallel retrieval batches",
            "# TYPE harness_parallel_batches_total gauge",
            f"harness_parallel_batches_total {metrics.parallel_batch_total}",
        ]
    )
    return "\n".join(lines) + "\n"

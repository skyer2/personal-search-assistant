"""In-process Agent metrics: true counters + histograms (P50/P90/P95)."""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * pct
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(sorted_values[lo])
    weight = rank - lo
    return float(sorted_values[lo] * (1 - weight) + sorted_values[hi] * weight)


@dataclass
class Histogram:
    values: list[float] = field(default_factory=list)

    def observe(self, value: float) -> None:
        if value < 0:
            return
        self.values.append(float(value))

    def snapshot(self) -> dict[str, float]:
        ordered = sorted(self.values)
        return {
            "count": float(len(ordered)),
            "sum": float(sum(ordered)),
            "p50": _percentile(ordered, 0.50),
            "p90": _percentile(ordered, 0.90),
            "p95": _percentile(ordered, 0.95),
        }


class InProcessMetrics:
    """进程内单调计数器 + 直方图。Prometheus scrape 读这份快照，不再把窗口扫描值标成 Counter。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, float] = defaultdict(float)
        self.histograms: dict[str, Histogram] = defaultdict(Histogram)
        self.gauges: dict[str, float] = {}

    def inc(self, name: str, value: float = 1.0, *, labels: dict[str, str] | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            self.counters[key] += value

    def observe(self, name: str, value: float, *, labels: dict[str, str] | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            self.histograms[key].observe(value)

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self.gauges[name] = float(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self.counters)
            gauges = dict(self.gauges)
            histograms = {name: hist.snapshot() for name, hist in self.histograms.items()}
        return {"counters": counters, "gauges": gauges, "histograms": histograms}

    def render_prometheus(self) -> str:
        snap = self.snapshot()
        mapping = [
            ("harness.runs.started", "harness_live_runs_started_total", "Process-lifetime run starts"),
            ("harness.runs.completed", "harness_live_runs_completed_total", "Process-lifetime successful/partial runs"),
            ("harness.runs.failed", "harness_live_runs_failed_total", "Process-lifetime failed runs"),
            ("harness.replan.applied", "harness_live_replan_applied_total", "Applied plan patches"),
            ("harness.replan.rejected", "harness_live_replan_rejected_total", "Rejected plan patches"),
            ("harness.replan.recovered", "harness_live_replan_recovered_total", "Replans that later closed target_gap_ids"),
            ("harness.replan.waste", "harness_live_replan_waste_total", "Runs that replanned and still failed"),
            ("harness.progress.gap", "harness_live_progress_gap_total", "Progress evaluator GAP verdicts"),
            ("harness.worker.failed", "harness_live_worker_failed_total", "Failed worker executions"),
            ("harness.worker.retry", "harness_live_worker_retry_total", "Worker attempts > 1"),
            ("harness.tool.calls", "harness_live_tool_calls_total", "Tool starts"),
            ("harness.budget.exhausted", "harness_live_budget_exhausted_total", "Budget/deadline aborts"),
            ("harness.plan.validation_failed", "harness_live_plan_validation_failed_total", "Plans with validation issues"),
            ("harness.quality.failed", "harness_live_quality_failed_total", "Quality gate failures"),
            ("harness.llm.calls", "harness_live_llm_calls_total", "gen_ai.chat events"),
        ]
        counters = snap["counters"]
        lines: list[str] = []
        for internal, prom, help_text in mapping:
            lines.extend(
                [
                    f"# HELP {prom} {help_text}",
                    f"# TYPE {prom} counter",
                    f"{prom} {counters.get(internal, 0.0):.0f}",
                ]
            )

        hist_names = [
            ("harness.run.duration_ms", "harness_live_run_duration_ms"),
            ("harness.worker.duration_ms", "harness_live_worker_duration_ms"),
            ("harness.tool.duration_ms", "harness_live_tool_duration_ms"),
            ("harness.llm.tokens", "harness_live_llm_tokens"),
        ]
        for internal, prom in hist_names:
            stats = snap["histograms"].get(internal) or {
                "count": 0,
                "sum": 0,
                "p50": 0,
                "p90": 0,
                "p95": 0,
            }
            lines.extend(
                [
                    f"# HELP {prom} Duration histogram (process lifetime)",
                    f"# TYPE {prom} summary",
                    f"{prom}{{quantile=\"0.5\"}} {stats['p50']:.1f}",
                    f"{prom}{{quantile=\"0.9\"}} {stats['p90']:.1f}",
                    f"{prom}{{quantile=\"0.95\"}} {stats['p95']:.1f}",
                    f"{prom}_count {stats['count']:.0f}",
                    f"{prom}_sum {stats['sum']:.1f}",
                ]
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _key(name: str, labels: dict[str, str] | None) -> str:
        if not labels:
            return name
        packed = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}|{packed}"


_METRICS = InProcessMetrics()


def get_metrics() -> InProcessMetrics:
    return _METRICS

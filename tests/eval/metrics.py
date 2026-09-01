"""
评测指标计算

支持 dry-run（仅 planner）与 live（完整 Harness）两种模式的结果聚合。
Phase 3：补全 SSR / ATC / AL / CR / MRH 共 8 项指标。
Phase 6：CCR / Hallucination Rate / Trajectory Similarity。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskEvalResult:
    task_id: str
    query: str
    mode: str
    success: bool
    tool_selection_ok: bool = True
    recovery_used: bool = False
    recovery_success: bool = False
    artifacts: list[str] = field(default_factory=list)
    assistants_called: list[str] = field(default_factory=list)
    retry_count: int = 0
    status: str = ""
    error: str = ""
    step_success_rate: float = 0.0
    tool_calls_count: int = 0
    latency_ms: int = 0
    avg_compression_ratio: float = 1.0
    memory_recall_hit: bool | None = None
    memory_recall_at_k: float | None = None
    citation_coverage_rate: float | None = None
    hallucination_rate: float | None = None
    trajectory_similarity: float | None = None
    trajectory_diff: dict[str, Any] = field(default_factory=dict)
    # 【Phase 9】编排与报告质量
    structured_output_compliance: float | None = None
    orchestration_violation_count: int = 0
    estimated_tokens_saved: int = 0
    report_judge_score: float | None = None
    report_judge_passed: bool | None = None
    # Agent Flight Recorder
    session_id: str = ""
    run_id: str = ""
    trace_id: str = ""
    variant: str = ""
    # 【Phase 14】Intent / Plan 评测
    intent_deliverable_ok: bool = True
    intent_slots_ok: bool = True
    plan_validation_ok: bool = True
    intent_confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalReport:
    total: int = 0
    passed: int = 0
    results: list[TaskEvalResult] = field(default_factory=list)

    @property
    def task_success_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def tool_selection_accuracy(self) -> float:
        if not self.results:
            return 0.0
        ok = sum(1 for r in self.results if r.tool_selection_ok)
        return ok / len(self.results)

    @property
    def step_success_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.step_success_rate for r in self.results) / len(self.results)

    @property
    def recovery_rate(self) -> float:
        recovered = [r for r in self.results if r.recovery_used]
        if not recovered:
            return 0.0
        ok = sum(1 for r in recovered if r.recovery_success or r.success)
        return ok / len(recovered)

    @property
    def avg_tool_calls(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.tool_calls_count for r in self.results) / len(self.results)

    @property
    def avg_latency_ms(self) -> float:
        live = [r for r in self.results if r.latency_ms > 0]
        if not live:
            return 0.0
        return sum(r.latency_ms for r in live) / len(live)

    @property
    def avg_compression_ratio(self) -> float:
        ratios = [r.avg_compression_ratio for r in self.results if r.avg_compression_ratio < 1.0]
        if not ratios:
            return 1.0
        return sum(ratios) / len(ratios)

    @property
    def memory_recall_hit_rate(self) -> float | None:
        memory_tasks = [r for r in self.results if r.memory_recall_hit is not None]
        if not memory_tasks:
            return None
        ok = sum(1 for r in memory_tasks if r.memory_recall_hit)
        return ok / len(memory_tasks)

    @property
    def avg_citation_coverage_rate(self) -> float | None:
        items = [r for r in self.results if r.citation_coverage_rate is not None]
        if not items:
            return None
        return sum(r.citation_coverage_rate for r in items) / len(items)

    @property
    def avg_hallucination_rate(self) -> float | None:
        items = [r for r in self.results if r.hallucination_rate is not None]
        if not items:
            return None
        return sum(r.hallucination_rate for r in items) / len(items)

    @property
    def avg_trajectory_similarity(self) -> float | None:
        items = [r for r in self.results if r.trajectory_similarity is not None]
        if not items:
            return None
        return sum(r.trajectory_similarity for r in items) / len(items)

    @property
    def avg_structured_output_compliance(self) -> float | None:
        items = [r for r in self.results if r.structured_output_compliance is not None]
        if not items:
            return None
        return sum(r.structured_output_compliance for r in items) / len(items)

    @property
    def avg_report_judge_score(self) -> float | None:
        items = [r for r in self.results if r.report_judge_score is not None]
        if not items:
            return None
        return sum(r.report_judge_score for r in items) / len(items)

    @property
    def report_judge_pass_rate(self) -> float | None:
        items = [r for r in self.results if r.report_judge_passed is not None]
        if not items:
            return None
        ok = sum(1 for r in items if r.report_judge_passed)
        return ok / len(items)

    @property
    def avg_tokens_saved(self) -> float | None:
        items = [r for r in self.results if r.estimated_tokens_saved > 0]
        if not items:
            return None
        return sum(r.estimated_tokens_saved for r in items) / len(items)

    @property
    def intent_deliverable_accuracy(self) -> float | None:
        items = [r for r in self.results if r.metadata.get("expected_deliverable")]
        if not items:
            items = [r for r in self.results if "deliverable" in r.metadata]
        if not items:
            return None
        ok = sum(1 for r in items if r.intent_deliverable_ok)
        return ok / len(items)

    @property
    def plan_validation_pass_rate(self) -> float | None:
        if not self.results:
            return None
        ok = sum(1 for r in self.results if r.plan_validation_ok)
        return ok / len(self.results)

    @property
    def avg_intent_confidence(self) -> float | None:
        items = [r for r in self.results if r.intent_confidence is not None]
        if not items:
            return None
        return sum(r.intent_confidence for r in items) / len(items)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "total": self.total,
            "passed": self.passed,
            "task_success_rate": round(self.task_success_rate, 3),
            "tool_selection_accuracy": round(self.tool_selection_accuracy, 3),
            "step_success_rate": round(self.step_success_rate, 3),
            "recovery_rate": round(self.recovery_rate, 3),
            "avg_tool_calls": round(self.avg_tool_calls, 2),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "avg_compression_ratio": round(self.avg_compression_ratio, 3),
            "results": [
                {
                    "task_id": r.task_id,
                    "success": r.success,
                    "tool_selection_ok": r.tool_selection_ok,
                    "recovery_used": r.recovery_used,
                    "recovery_success": r.recovery_success,
                    "status": r.status,
                    "retry_count": r.retry_count,
                    "artifacts": r.artifacts,
                    "assistants_called": r.assistants_called,
                    "step_success_rate": round(r.step_success_rate, 3),
                    "tool_calls_count": r.tool_calls_count,
                    "latency_ms": r.latency_ms,
                    "avg_compression_ratio": round(r.avg_compression_ratio, 3),
                    "memory_recall_hit": r.memory_recall_hit,
                    "citation_coverage_rate": r.citation_coverage_rate,
                    "hallucination_rate": r.hallucination_rate,
                    "trajectory_similarity": r.trajectory_similarity,
                    "trajectory_diff": r.trajectory_diff,
                    "structured_output_compliance": r.structured_output_compliance,
                    "orchestration_violation_count": r.orchestration_violation_count,
                    "estimated_tokens_saved": r.estimated_tokens_saved,
                    "report_judge_score": r.report_judge_score,
                    "report_judge_passed": r.report_judge_passed,
                    "intent_deliverable_ok": r.intent_deliverable_ok,
                    "intent_slots_ok": r.intent_slots_ok,
                    "plan_validation_ok": r.plan_validation_ok,
                    "intent_confidence": r.intent_confidence,
                    "session_id": r.session_id,
                    "run_id": r.run_id,
                    "trace_id": r.trace_id,
                    "variant": r.variant,
                    "error": r.error,
                }
                for r in self.results
            ],
        }
        mrh = self.memory_recall_hit_rate
        if mrh is not None:
            payload["memory_recall_hit_rate"] = round(mrh, 3)
        ccr = self.avg_citation_coverage_rate
        if ccr is not None:
            payload["citation_coverage_rate"] = round(ccr, 3)
        hr = self.avg_hallucination_rate
        if hr is not None:
            payload["hallucination_rate"] = round(hr, 3)
        ts = self.avg_trajectory_similarity
        if ts is not None:
            payload["trajectory_similarity"] = round(ts, 3)
        jcr = self.avg_structured_output_compliance
        if jcr is not None:
            payload["structured_output_compliance_rate"] = round(jcr, 3)
        rjs = self.avg_report_judge_score
        if rjs is not None:
            payload["report_judge_score"] = round(rjs, 3)
        rjp = self.report_judge_pass_rate
        if rjp is not None:
            payload["report_judge_pass_rate"] = round(rjp, 3)
        ts_saved = self.avg_tokens_saved
        if ts_saved is not None:
            payload["avg_tokens_saved"] = round(ts_saved, 1)
        ida = self.intent_deliverable_accuracy
        if ida is not None:
            payload["intent_deliverable_accuracy"] = round(ida, 3)
        pvr = self.plan_validation_pass_rate
        if pvr is not None:
            payload["plan_validation_pass_rate"] = round(pvr, 3)
        aic = self.avg_intent_confidence
        if aic is not None:
            payload["avg_intent_confidence"] = round(aic, 3)
        return payload


def evaluate_tool_selection(
    expected_agents: list[str],
    assistants_called: list[str],
) -> bool:
    if not expected_agents:
        return True
    return all(agent in assistants_called for agent in expected_agents)


def evaluate_artifacts(expected_artifacts: list[str], artifacts: list[str]) -> bool:
    if not expected_artifacts:
        return True
    lower_names = [name.lower() for name in artifacts]
    for artifact in expected_artifacts:
        if artifact == "md" and not any(name.endswith(".md") for name in lower_names):
            return False
        if artifact == "pdf" and not any(name.endswith(".pdf") for name in lower_names):
            return False
    return True


def evaluate_memory_recall(expected: bool, recalled: bool, recall_at_k: float | None = None) -> bool:
    """任务是否召回了记忆。recall_at_k 参数实际是 mean_recall_score，不是 IR Recall@K。"""
    if not expected:
        return True
    if recalled:
        return True
    if recall_at_k is not None and recall_at_k > 0:
        return True
    return False


def build_report(results: list[TaskEvalResult]) -> EvalReport:
    report = EvalReport(total=len(results), results=results)
    report.passed = sum(1 for item in results if item.success)
    return report


def compare_with_baseline(
    current: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    metric_keys = [
        "task_success_rate",
        "tool_selection_accuracy",
        "step_success_rate",
        "recovery_rate",
        "avg_tool_calls",
        "avg_latency_ms",
        "avg_compression_ratio",
        "memory_recall_hit_rate",
        "citation_coverage_rate",
        "hallucination_rate",
        "trajectory_similarity",
        "structured_output_compliance_rate",
        "report_judge_pass_rate",
        "avg_tokens_saved",
        "intent_deliverable_accuracy",
        "plan_validation_pass_rate",
        "avg_intent_confidence",
    ]
    deltas: dict[str, float | None] = {}
    for key in metric_keys:
        cur = current.get(key)
        base = baseline.get(key)
        if isinstance(cur, (int, float)) and isinstance(base, (int, float)):
            deltas[key] = round(float(cur) - float(base), 3)
        else:
            deltas[key] = None

    regressions = []
    if deltas.get("task_success_rate") is not None and deltas["task_success_rate"] < -0.05:
        regressions.append("TSR dropped > 5%")
    jcr_delta = deltas.get("structured_output_compliance_rate")
    if jcr_delta is not None and jcr_delta < -0.1:
        regressions.append("JCR dropped > 10%")
    ida_delta = deltas.get("intent_deliverable_accuracy")
    if ida_delta is not None and ida_delta < -0.05:
        regressions.append("IDA dropped > 5%")
    pvr = current.get("plan_validation_pass_rate")
    if isinstance(pvr, (int, float)) and pvr < 1.0:
        regressions.append("Plan validation pass rate < 100%")

    return {
        "baseline_generated_at": baseline.get("generated_at"),
        "baseline_mode": baseline.get("mode"),
        "deltas": deltas,
        "regressions": regressions,
        "blocked_merge": bool(regressions),
    }

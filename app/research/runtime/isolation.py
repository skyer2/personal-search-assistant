"""并行 Worker：本地 LoopState 增量，join 时再 merge。不要让工人同时 mutate 父状态。"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Optional

from app.agent.harness.state import LoopState, PlanStep, StepResult, StepStatus


_INCREMENTAL_ZERO_FIELDS = (
    "obs_structured_checks",
    "obs_structured_passes",
    "obs_structured_retries",
    "obs_orchestration_violations",
    "obs_binding_violations",
    "obs_unauthorized_tool_hits",
    "obs_estimated_tokens_saved",
    "obs_step_message_tokens_peak",
    "obs_context_budget_trims",
    "obs_fresh_threads",
    "obs_retention_patches",
    "obs_tool_results_cleared",
)


def snapshot_worker_loop_state(state: LoopState) -> LoopState:
    """fan-out 任务只读父状态，在独立副本上累计 trace / counter。"""
    child = copy.deepcopy(state)
    child.metadata["_parallel_child"] = True
    child.trace = []
    child.assistants_called = []
    child.compression_ratios = []
    child.tool_calls_count = 0
    child.obs_entity_retention_rates = []
    child.graph_thread_ids = []
    for field_name in _INCREMENTAL_ZERO_FIELDS:
        if hasattr(child, field_name):
            setattr(child, field_name, 0)
    return child


@dataclass
class IsolatedWorkerOutcome:
    step_index: int
    task_id: str
    ok: bool
    result: Optional[StepResult]
    child_state: LoopState
    fail_reason: str = ""


def worker_row(task_id: str, step: PlanStep, ok: bool, result: StepResult | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if result is not None and isinstance(getattr(result, "metadata", None), dict):
        raw = result.metadata.get("worker_payload") or {}
        if isinstance(raw, dict):
            payload = dict(raw)
    summary = str(
        payload.get("summary")
        or (getattr(result, "content", "") if result is not None else "")
        or ""
    )[:400]
    return {
        "task_id": task_id,
        "ok": bool(ok),
        "summary": summary,
        "step_type": step.step_type,
        "queue_ms": int(step.metadata.get("queue_ms") or 0),
        "execution_ms": int(step.metadata.get("execution_ms") or 0),
        "duration_ms": int(step.metadata.get("duration_ms") or step.metadata.get("execution_ms") or 0),
        "payload": {
            "summary": summary,
            "facts": list(payload.get("facts") or [])[:20],
            "sources": list(payload.get("sources") or [])[:20],
            "gaps": list(payload.get("gaps") or [])[:8],
            "conflicts": list(payload.get("conflicts") or [])[:8],
            "confidence": float(payload.get("confidence") or (1.0 if ok else 0.0)),
            "findings": list(payload.get("findings") or [])[:12],
            "evidence_ids": list(payload.get("evidence_ids") or [])[:20],
        },
    }


def apply_isolated_outcome(parent: LoopState, outcome: IsolatedWorkerOutcome) -> None:
    """单线程 join：只合并本步增量，不覆盖父计划对象。"""
    child = outcome.child_state
    parent.trace.extend(child.trace)
    parent.tool_calls_count += child.tool_calls_count
    parent.compression_ratios.extend(child.compression_ratios)
    for field_name in _INCREMENTAL_ZERO_FIELDS:
        if field_name == "obs_step_message_tokens_peak":
            continue
        if hasattr(parent, field_name) and hasattr(child, field_name):
            setattr(
                parent,
                field_name,
                getattr(parent, field_name) + getattr(child, field_name),
            )
    parent.obs_step_message_tokens_peak = max(
        getattr(parent, "obs_step_message_tokens_peak", 0),
        getattr(child, "obs_step_message_tokens_peak", 0),
    )
    parent.obs_entity_retention_rates.extend(
        getattr(child, "obs_entity_retention_rates", []) or []
    )
    parent.graph_thread_ids.extend(getattr(child, "graph_thread_ids", []) or [])
    if parent.plan is not None and 0 <= outcome.step_index < len(parent.plan.steps):
        parent.plan.steps[outcome.step_index].metadata["status"] = (
            StepStatus.DONE.value if outcome.ok else StepStatus.FAILED.value
        )
    if outcome.result is not None:
        parent.step_results.append(outcome.result)
        if outcome.ok:
            parent.final_content = (
                outcome.result.compressed_content or outcome.result.content
            )

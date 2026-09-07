"""Deterministic, pure routing authority for the research workflow."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.agent.harness.state import ExecutionPlan
from app.research.assessment.delivery import DeliveryMode, assess_delivery
from app.research.assessment.evidence import EvidenceStatus, assess_evidence
from app.research.assessment.execution_health import ExecutionHealthStatus, assess_execution_health
from app.research.assessment.progress import SemanticProgress, assess_progress
from app.research.assessment.quality import QualityVerdict, assess_quality
from app.research.domain.contracts import (
    BudgetStatus,
    ControlAction,
    ControlDecision,
    replan_budget_exhausted,
    replan_budget_from_state,
)
from app.research.domain.task_state import (
    TaskExecutionStatus,
    TaskReadiness,
    normalize_tasks,
    task_readiness,
)

POLICY_VERSION = "control-policy.v2"
MAX_TASK_ATTEMPTS = 2


def plan_from_state(state: dict[str, Any]) -> ExecutionPlan | None:
    raw = state.get("plan")
    if not isinstance(raw, dict) or not raw:
        return None
    return ExecutionPlan.from_dict(raw)


def _required_steps(plan: ExecutionPlan) -> list[tuple[int, Any]]:
    return [
        (index, step)
        for index, step in enumerate(plan.steps)
        if not (isinstance(step.metadata, dict) and step.metadata.get("optional"))
    ]


def _readiness(state: dict[str, Any], plan: ExecutionPlan) -> list[tuple[str, TaskReadiness, int]]:
    return [
        (step.resolved_task_id(index), task_readiness(step, state.get("tasks")), index)
        for index, step in _required_steps(plan)
    ]


def _decision(
    state: dict[str, Any],
    action: ControlAction,
    *,
    mode: str = "",
    reasons: list[str] | None = None,
    task_ids: list[str] | None = None,
) -> ControlDecision:
    reasons = reasons or []
    task_ids = task_ids or []
    fingerprint = {
        "action": action.value,
        "mode": mode,
        "reasons": reasons,
        "task_ids": task_ids,
        "plan_version": int(state.get("plan_version") or 1),
        "tasks": {
            task_id: {
                "execution_status": task["execution_status"],
                "result_status": task["result_status"],
                "attempt": task["attempt"],
            }
            for task_id, task in normalize_tasks(state.get("tasks")).items()
        },
    }
    decision_id = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:20]
    return ControlDecision(
        decision_id=decision_id,
        action=action.value,
        mode=mode,
        reason_codes=reasons,
        task_ids=task_ids,
        state_version=int(state.get("state_version") or 1),
        plan_version=int(state.get("plan_version") or 1),
        assessment_refs=[
            name
            for name in ("progress_assessment", "evidence_assessment", "execution_health", "delivery_readiness", "quality_assessment")
            if isinstance(state.get(name), dict)
        ],
        policy_version=POLICY_VERSION,
    )


def decide_control(state: dict[str, Any]) -> ControlDecision:
    if str(state.get("cancel_reason") or ""):
        return _decision(state, ControlAction.CANCEL, reasons=["user_cancelled"])
    if str(state.get("abort_reason") or ""):
        return _decision(state, ControlAction.CANCEL, reasons=["policy_stop"])

    quality = assess_quality(state)
    if quality["verdict"] != QualityVerdict.UNKNOWN.value:
        if quality["verdict"] == QualityVerdict.PASS.value:
            return _decision(state, ControlAction.FINALIZE_SUCCESS, mode=DeliveryMode.NORMAL.value, reasons=["quality_pass"])
        if quality["repairable"] and quality["suggested_action"] == "repair":
            return _decision(state, ControlAction.REPAIR_SYNTHESIS, reasons=["quality_repairable"])
        if quality["suggested_action"] == "replan":
            budget = replan_budget_from_state(state)
            if not replan_budget_exhausted(budget):
                return _decision(state, ControlAction.REPLAN, reasons=["quality_replan"])
        if bool(state.get("final_content")):
            return _decision(state, ControlAction.FINALIZE_FAILURE, mode=DeliveryMode.DEGRADED.value, reasons=["quality_fail_partial"])
        return _decision(state, ControlAction.FINALIZE_FAILURE, reasons=["quality_fail"])

    plan = plan_from_state(state)
    if plan is None:
        return _decision(state, ControlAction.FINALIZE_FAILURE, reasons=["missing_plan"])

    readiness = _readiness(state, plan)
    running = [task_id for task_id, ready, _ in readiness if ready == TaskReadiness.WAITING_RESOURCE and _task_is_running(state, task_id)]
    if running:
        return _decision(state, ControlAction.WAIT, task_ids=running, reasons=["required_task_running"])
    runnable = [task_id for task_id, ready, _ in readiness if ready == TaskReadiness.RUNNABLE]
    if runnable:
        return _decision(state, ControlAction.DISPATCH, task_ids=runnable, reasons=["required_task_runnable"])

    health = assess_execution_health(state)
    progress = assess_progress(state)
    budget = replan_budget_from_state(state)
    evidence = assess_evidence(state)
    delivery = assess_delivery(state)
    usable_evidence = evidence["status"] in {
        EvidenceStatus.PARTIAL.value,
        EvidenceStatus.SUFFICIENT.value,
    }
    budget_allows_recovery = (
        str(state.get("budget_status") or BudgetStatus.UNKNOWN.value)
        != BudgetStatus.EXHAUSTED.value
    )
    retryable = [
        task_id
        for task_id in health["retryable_tasks"]
        if int(normalize_tasks(state.get("tasks")).get(task_id, {}).get("attempt") or 0) < MAX_TASK_ATTEMPTS
    ]

    if delivery["mode"] != DeliveryMode.NONE.value:
        return _decision(
            state,
            ControlAction.SYNTHESIZE,
            mode=delivery["mode"],
            reasons=["delivery_ready", *delivery["limitations"]],
        )

    should_retry = bool(
        retryable
        and health["status"] in {ExecutionHealthStatus.DEGRADED.value, ExecutionHealthStatus.FAILED.value}
        and budget_allows_recovery
        and delivery["mode"] == DeliveryMode.NONE.value
    )
    if should_retry:
        return _decision(
            state,
            ControlAction.RETRY,
            task_ids=retryable,
            reasons=["retryable_failure", "budget_allows_recovery", "delivery_not_ready"],
        )

    if (
        progress["status"] == SemanticProgress.GAP.value
        and _progress_gap_is_actionable(progress)
        and budget_allows_recovery
        and not replan_budget_exhausted(budget)
    ):
        return _decision(state, ControlAction.REPLAN, reasons=["semantic_gap", "replan_available"])

    if usable_evidence and replan_budget_exhausted(budget):
        return _decision(
            state,
            ControlAction.DELIVER_PARTIAL,
            mode=DeliveryMode.DEGRADED.value,
            reasons=["replan_exhausted", "usable_evidence"],
        )
    if usable_evidence and str(state.get("budget_status") or BudgetStatus.UNKNOWN.value) == BudgetStatus.EXHAUSTED.value:
        return _decision(
            state,
            ControlAction.DELIVER_PARTIAL,
            mode=DeliveryMode.DEGRADED.value,
            reasons=["budget_exhausted", "usable_evidence"],
        )
    return _decision(state, ControlAction.FINALIZE_FAILURE, reasons=[*delivery["blockers"], "delivery_not_ready"])


def _task_is_running(state: dict[str, Any], task_id: str) -> bool:
    task = normalize_tasks(state.get("tasks")).get(task_id)
    return task is not None and task["execution_status"] == TaskExecutionStatus.RUNNING.value


def _progress_gap_is_actionable(progress: dict[str, Any]) -> bool:
    return any(
        progress.get(key)
        for key in (
            "coverage_gaps",
            "missing_dimensions",
            "unresolved_conflicts",
            "low_confidence_claims",
            "stale_evidence",
            "unmet_success_criteria",
        )
    )


__all__ = ["POLICY_VERSION", "decide_control", "plan_from_state"]

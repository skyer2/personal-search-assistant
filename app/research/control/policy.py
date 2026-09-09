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
    action_budget_from_state,
    budget_status,
)
from app.research.domain.task_state import (
    TaskExecutionStatus,
    TaskReadiness,
    normalize_tasks,
    task_readiness,
)

POLICY_VERSION = "control-policy.v3"
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
        if step.step_type in {"research", "network_search", "file_read"}
        and not (isinstance(step.metadata, dict) and step.metadata.get("optional"))
    ]


def _readiness(state: dict[str, Any], plan: ExecutionPlan) -> list[tuple[str, TaskReadiness]]:
    return [
        (step.resolved_task_id(index), task_readiness(step, state.get("tasks")))
        for index, step in _required_steps(plan)
    ]


def _strategy_fingerprint(plan: ExecutionPlan | None) -> str:
    payload = plan.to_dict() if plan is not None else {}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:20]


def _decision(
    state: dict[str, Any],
    action: ControlAction,
    *,
    mode: str = "",
    reasons: list[str] | None = None,
    task_ids: list[str] | None = None,
    gap_ids: list[str] | None = None,
) -> ControlDecision:
    plan = plan_from_state(state)
    reasons = list(dict.fromkeys(reasons or []))
    task_ids = list(dict.fromkeys(task_ids or []))
    gap_ids = list(dict.fromkeys(gap_ids or []))
    candidate = state.get("candidate_set") if isinstance(state.get("candidate_set"), dict) else {}
    fingerprint = {
        "action": action.value,
        "mode": mode,
        "reasons": reasons,
        "task_ids": task_ids,
        "gap_ids": gap_ids,
        "candidate_set_id": str(candidate.get("candidate_set_id") or ""),
        "plan_version": int(state.get("plan_version") or 1),
        "coverage_ratio": float((state.get("coverage_state") or {}).get("coverage_ratio") or 0.0),
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
        gap_ids=gap_ids,
        candidate_set_id=str(candidate.get("candidate_set_id") or ""),
        strategy_fingerprint=_strategy_fingerprint(plan),
        state_version=int(state.get("state_version") or 1),
        plan_version=int(state.get("plan_version") or 1),
        assessment_refs=[
            name
            for name in (
                "progress_assessment",
                "evidence_assessment",
                "execution_health",
                "delivery_readiness",
                "quality_assessment",
            )
            if isinstance(state.get(name), dict)
        ],
        policy_version=POLICY_VERSION,
    )


def _usable_evidence(state: dict[str, Any]) -> bool:
    assessment = state.get("evidence_assessment")
    if isinstance(assessment, dict):
        if str(assessment.get("status") or "") in {EvidenceStatus.PARTIAL.value, EvidenceStatus.SUFFICIENT.value}:
            return True
        if int(assessment.get("evidence_count") or 0) > 0:
            return True
    return bool([row for row in state.get("evidence_records") or [] if isinstance(row, dict)])


def decide_control(state: dict[str, Any]) -> ControlDecision:
    if str(state.get("cancel_reason") or ""):
        return _decision(state, ControlAction.CANCEL, reasons=["user_cancelled"])

    internal_error = state.get("internal_error")
    if isinstance(internal_error, dict) and internal_error:
        if _usable_evidence(state):
            return _decision(state, ControlAction.DELIVER_PARTIAL, mode=DeliveryMode.PARTIAL_ONLY.value, reasons=["internal_error", "usable_partial_evidence"])
        return _decision(state, ControlAction.FINALIZE_FAILURE, reasons=["internal_error"])

    planning_failure = state.get("planning_failure")
    abort_reason = str(state.get("abort_reason") or "")
    if not isinstance(planning_failure, dict) or not planning_failure:
        if abort_reason.startswith("plan_validation_failed"):
            planning_failure = {
                "code": "plan_validation_failed",
                "message": abort_reason,
                "origin_stage": "plan_validate",
                "detected_stage": "plan_validate",
            }
        else:
            planning_failure = {}
    if isinstance(planning_failure, dict) and planning_failure:
        if _usable_evidence(state):
            return _decision(state, ControlAction.DELIVER_PARTIAL, mode=DeliveryMode.PARTIAL_ONLY.value, reasons=["planning_failed", "usable_partial_evidence"])
        return _decision(state, ControlAction.FINALIZE_FAILURE, reasons=["planning_failed"])

    stop_reason = str(state.get("stop_reason") or "")
    if stop_reason in {"timeout", "budget"}:
        if _usable_evidence(state):
            return _decision(
                state,
                ControlAction.DELIVER_PARTIAL,
                mode=DeliveryMode.PARTIAL_ONLY.value,
                reasons=[f"{stop_reason}_stop", "usable_partial_evidence"],
            )
        return _decision(state, ControlAction.FINALIZE_FAILURE, reasons=[f"{stop_reason}_stop", "no_usable_evidence"])

    quality = assess_quality(state)
    if quality["verdict"] != QualityVerdict.UNKNOWN.value:
        if quality["verdict"] == QualityVerdict.PASS.value:
            return _decision(state, ControlAction.FINALIZE_SUCCESS, mode=DeliveryMode.NORMAL.value, reasons=["quality_pass"])
        if quality["repairable"] and quality["suggested_action"] == "repair":
            if int(state.get("synthesis_attempts") or 0) < 2:
                return _decision(state, ControlAction.REPAIR_SYNTHESIS, reasons=["quality_repairable"])
            return _decision(state, ControlAction.FINALIZE_FAILURE, mode=DeliveryMode.DEGRADED.value, reasons=["quality_repair_exhausted"])
        if quality["suggested_action"] == "gap_fill":
            actions = action_budget_from_state(state)
            fillable = [
                gap_id
                for gap_id, gap in (state.get("semantic_gaps") or {}).items()
                if isinstance(gap, dict)
                and bool(gap.get("actionable", True))
                and int(gap.get("attempt_count") or 0) < 2
            ]
            if fillable and actions["gap_fill"] < actions["max_gap_fill"]:
                return _decision(
                    state,
                    ControlAction.GAP_FILL,
                    reasons=["quality_gate_failed", "explicit_semantic_gap"],
                    gap_ids=fillable,
                )
        if bool(state.get("final_content")):
            return _decision(state, ControlAction.FINALIZE_FAILURE, mode=DeliveryMode.DEGRADED.value, reasons=["quality_fail_partial"])
        return _decision(state, ControlAction.FINALIZE_FAILURE, reasons=["quality_fail"])

    plan = plan_from_state(state)
    if plan is None:
        return _decision(state, ControlAction.FINALIZE_FAILURE, reasons=["missing_plan"])

    progress = assess_progress(state)
    evidence = assess_evidence(state)
    health = assess_execution_health(state)
    if budget_status(state) == BudgetStatus.EXHAUSTED:
        if progress["status"] == SemanticProgress.SUFFICIENT.value and evidence["status"] != EvidenceStatus.INSUFFICIENT.value:
            return _decision(state, ControlAction.SYNTHESIZE, reasons=["budget_stop", "coverage_sufficient"])
        if evidence["evidence_count"]:
            return _decision(state, ControlAction.DELIVER_PARTIAL, mode=DeliveryMode.PARTIAL_ONLY.value, reasons=["budget_stop", "usable_partial_evidence"])
        return _decision(state, ControlAction.FINALIZE_FAILURE, reasons=["budget_stop", "no_usable_evidence"])

    actions = action_budget_from_state(state)
    candidate = state.get("candidate_set") if isinstance(state.get("candidate_set"), dict) else {}
    candidate_ready = (
        bool(candidate.get("available"))
        and not bool(candidate.get("expanded"))
        and bool(candidate.get("items") or candidate.get("candidates"))
    )
    if candidate_ready and actions["expand_plan"] < actions["max_expand_plan"]:
        return _decision(
            state,
            ControlAction.EXPAND_PLAN,
            reasons=["candidate_set_ready", "discovery_required"],
            gap_ids=[str(item) for item in candidate.get("source_task_ids") or []],
        )

    readiness = _readiness(state, plan)
    running = [
        task_id
        for task_id, ready in readiness
        if ready == TaskReadiness.WAITING_RESOURCE
        and normalize_tasks(state.get("tasks")).get(task_id, {}).get("execution_status") == TaskExecutionStatus.RUNNING.value
    ]
    if running:
        return _decision(state, ControlAction.WAIT, task_ids=running, reasons=["workers_running"])
    runnable = [task_id for task_id, ready in readiness if ready == TaskReadiness.RUNNABLE]
    if runnable:
        return _decision(state, ControlAction.DISPATCH, task_ids=runnable, reasons=["runnable_tasks"])

    tasks = normalize_tasks(state.get("tasks"))
    retryable = [
        task_id
        for task_id, task in tasks.items()
        if task["execution_status"] == TaskExecutionStatus.FAILED.value
        and bool(task.get("failure", {}).get("retryable"))
        and int(task.get("attempt") or 0) < min(actions["max_retry"], MAX_TASK_ATTEMPTS)
    ]
    if retryable and actions["retry"] < actions["max_retry"]:
        return _decision(state, ControlAction.RETRY, task_ids=retryable, reasons=["transient_failure"])

    if progress["status"] == SemanticProgress.SUFFICIENT.value and evidence["status"] == EvidenceStatus.SUFFICIENT.value:
        return _decision(state, ControlAction.SYNTHESIZE, reasons=["coverage_sufficient", "evidence_sufficient"])

    semantic_stall = int(state.get("semantic_stall") or 0)
    if semantic_stall >= 2 and actions["replan"] < actions["max_replan"]:
        return _decision(state, ControlAction.REPLAN, reasons=["semantic_gain_low", "alternative_strategy_available"])

    gaps = {
        gap_id: gap
        for gap_id, gap in (state.get("semantic_gaps") or {}).items()
        if isinstance(gap, dict) and bool(gap.get("actionable", True))
    }
    fillable = {
        gap_id: gap
        for gap_id, gap in gaps.items()
        if int(gap.get("attempt_count") or 0) < 2
    }
    if fillable and actions["gap_fill"] < actions["max_gap_fill"]:
        return _decision(
            state,
            ControlAction.GAP_FILL,
            reasons=["explicit_semantic_gap"],
            gap_ids=list(fillable),
        )

    marginal = state.get("marginal_gain") if isinstance(state.get("marginal_gain"), dict) else {}
    if bool(marginal.get("stalled")) and evidence["status"] in {EvidenceStatus.PARTIAL.value, EvidenceStatus.SUFFICIENT.value}:
        return _decision(state, ControlAction.DELIVER_PARTIAL, mode=DeliveryMode.PARTIAL_ONLY.value, reasons=["semantic_gain_low", "usable_evidence"])

    if actions["replan"] < actions["max_replan"] and gaps:
        return _decision(state, ControlAction.REPLAN, reasons=["semantic_gap", "replan_available"])

    if evidence["status"] in {EvidenceStatus.PARTIAL.value, EvidenceStatus.SUFFICIENT.value}:
        return _decision(state, ControlAction.DELIVER_PARTIAL, mode=DeliveryMode.PARTIAL_ONLY.value, reasons=["usable_partial_evidence"])
    if health["status"] == ExecutionHealthStatus.FAILED.value:
        return _decision(state, ControlAction.FINALIZE_FAILURE, reasons=["execution_failed_no_evidence"])
    return _decision(state, ControlAction.FINALIZE_FAILURE, reasons=["recovery_exhausted", "no_usable_evidence"])


__all__ = ["MAX_TASK_ATTEMPTS", "POLICY_VERSION", "decide_control", "plan_from_state"]

"""Thin deterministic runtime policy without research-topic heuristics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SEMANTIC_ACTIONS = frozenset()


@dataclass(frozen=True)
class RuntimeDecision:
    action: str
    reason_codes: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()


def _usable_evidence(state: dict[str, Any]) -> bool:
    if state.get("evidence_records"):
        return True
    assessment = state.get("evidence_assessment")
    return isinstance(assessment, dict) and int(assessment.get("evidence_count") or 0) > 0


def _coverage_sufficient(state: dict[str, Any]) -> bool:
    judgement = state.get("coverage_judgement")
    return isinstance(judgement, dict) and bool(judgement.get("sufficient"))


def decide_control(state: dict[str, Any]) -> RuntimeDecision:
    if str(state.get("cancel_reason") or ""):
        return RuntimeDecision("cancel", ("user_cancelled",))

    budget = state.get("budget") if isinstance(state.get("budget"), dict) else {}
    if bool(budget.get("exhausted")) or str(state.get("budget_status") or "") == "exhausted":
        if _coverage_sufficient(state) and _usable_evidence(state):
            return RuntimeDecision("synthesize", ("budget_stop", "coverage_sufficient"))
        if _usable_evidence(state):
            return RuntimeDecision("deliver_partial", ("budget_stop", "usable_evidence"))
        return RuntimeDecision("finalize_failure", ("budget_stop", "no_usable_evidence"))

    if isinstance(state.get("internal_error"), dict) and state.get("internal_error"):
        if _usable_evidence(state):
            return RuntimeDecision("deliver_partial", ("internal_error", "usable_evidence"))
        return RuntimeDecision("finalize_failure", ("internal_error",))

    supervisor = state.get("supervisor_action") if isinstance(state.get("supervisor_action"), dict) else {}
    action = str(supervisor.get("action") or "")
    supervisor_meta = state.get("supervisor") if isinstance(state.get("supervisor"), dict) else {}
    raw_iteration_limit = budget.get("max_replan_count")
    iteration_limit = 3 if raw_iteration_limit is None else max(0, int(raw_iteration_limit))
    if int(supervisor_meta.get("iteration") or 0) >= max(1, iteration_limit) and action != "COMPLETE":
        if _usable_evidence(state):
            return RuntimeDecision("deliver_partial", ("supervisor_iteration_limit", "usable_evidence"))
        return RuntimeDecision("finalize_failure", ("supervisor_iteration_limit",))
    tasks = dict(state.get("tasks") or {})
    running = [
        task_id for task_id, task in tasks.items()
        if isinstance(task, dict) and str(task.get("execution_status") or "") == "running"
    ]
    if action == "COMPLETE":
        if _usable_evidence(state):
            return RuntimeDecision("synthesize", ("supervisor_complete", "usable_evidence"))
        return RuntimeDecision("finalize_failure", ("supervisor_complete", "no_usable_evidence"))

    retryable = [
        task_id for task_id, task in tasks.items()
        if isinstance(task, dict)
        and str(task.get("execution_status") or "") == "failed"
        and bool((task.get("failure") or {}).get("retryable"))
        and int(task.get("attempt") or 0) < 2
    ]
    if retryable:
        return RuntimeDecision("retry", ("transient_execution_failure",), tuple(retryable))
    if running:
        return RuntimeDecision("wait", ("workers_running",), tuple(running))

    requested_tasks = [
        str(item.get("task_id") or "") for item in supervisor.get("research_tasks") or []
        if isinstance(item, dict) and str(item.get("task_id") or "").strip()
    ]
    if not requested_tasks:
        requested_tasks = [
            task_id
            for task_id, task in tasks.items()
            if str(task.get("execution_status") or "") == "pending"
        ]
    if action == "CONDUCT_RESEARCH" and requested_tasks:
        return RuntimeDecision("dispatch", ("supervisor_conduct_research",), tuple(requested_tasks))
    if _coverage_sufficient(state) and _usable_evidence(state):
        return RuntimeDecision("synthesize", ("coverage_sufficient",))
    if _usable_evidence(state):
        return RuntimeDecision("deliver_partial", ("usable_evidence",))
    return RuntimeDecision("run_supervisor", ("semantic_strategy_required",))


__all__ = ["RuntimeDecision", "SEMANTIC_ACTIONS", "decide_control"]

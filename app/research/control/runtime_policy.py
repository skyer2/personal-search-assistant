"""Thin deterministic runtime policy without research-topic heuristics.

Runtime owns budget, concurrency, timeout and retry. It never redefines the
user's question and never declares semantic completion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SEMANTIC_ACTIONS: frozenset[str] = frozenset()

_MAX_EXECUTION_ATTEMPTS = 3


@dataclass(frozen=True)
class RuntimeDecision:
    action: str
    reason_codes: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()


def _evidence_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in state.get("evidence_records") or [] if isinstance(row, dict)]


def _usable_evidence(state: dict[str, Any]) -> bool:
    if _evidence_records(state):
        return True
    assessment = state.get("evidence_assessment")
    return isinstance(assessment, dict) and int(assessment.get("evidence_count") or 0) > 0


def answerable_user_ask(state: dict[str, Any]) -> bool:
    """Partial delivery requires at least one answerable user ask.

    Evidence merely existing is not delivery-worthy: it must be bound to a
    research question that traces back to something the user actually asked.
    """
    if not _usable_evidence(state):
        return False
    answerability = state.get("answerability")
    if isinstance(answerability, dict):
        statuses = [row for row in answerability.get("question_status") or [] if isinstance(row, dict)]
        if statuses:
            return any(bool(row.get("answerable")) for row in statuses)
    judgement = state.get("coverage_judgement")
    if isinstance(judgement, dict):
        criteria = [row for row in judgement.get("criteria") or [] if isinstance(row, dict)]
        if criteria:
            return any(
                str(row.get("status") or "") in {"supported", "partial"}
                and bool(row.get("evidence_ids"))
                for row in criteria
            )
    # No semantic projection yet: fall back to grounded findings.
    return any(
        isinstance(row, dict)
        and str(row.get("claim") or row.get("summary") or "").strip()
        and (row.get("evidence_ids") or row.get("artifact_ids"))
        for row in state.get("findings") or []
    )


def _coverage_sufficient(state: dict[str, Any]) -> bool:
    judgement = state.get("coverage_judgement")
    return isinstance(judgement, dict) and bool(judgement.get("sufficient"))


def _retryable_tasks(state: dict[str, Any]) -> tuple[str, ...]:
    tasks = dict(state.get("tasks") or {})
    return tuple(
        task_id
        for task_id, task in tasks.items()
        if isinstance(task, dict)
        and str(task.get("execution_status") or "") == "failed"
        and bool((task.get("failure") or {}).get("retryable"))
        and int(task.get("attempt") or 0) < _MAX_EXECUTION_ATTEMPTS
    )


def _stop_decision(state: dict[str, Any], *, reason: str) -> RuntimeDecision:
    if answerable_user_ask(state):
        return RuntimeDecision("deliver_partial", (reason, "answerable_user_ask"))
    return RuntimeDecision("finalize_failure", (reason, "no_answerable_user_ask"))


def decide_control(state: dict[str, Any]) -> RuntimeDecision:
    if str(state.get("cancel_reason") or ""):
        return RuntimeDecision("cancel", ("user_cancelled",))

    raw_budget = state.get("budget")
    budget: dict[str, Any] = raw_budget if isinstance(raw_budget, dict) else {}
    if bool(budget.get("exhausted")) or str(state.get("budget_status") or "") == "exhausted":
        if _coverage_sufficient(state) and _usable_evidence(state):
            return RuntimeDecision("synthesize", ("budget_stop", "coverage_sufficient"))
        return _stop_decision(state, reason="budget_stop")

    if isinstance(state.get("internal_error"), dict) and state.get("internal_error"):
        return _stop_decision(state, reason="internal_error")

    raw_supervisor = state.get("supervisor_action")
    supervisor: dict[str, Any] = raw_supervisor if isinstance(raw_supervisor, dict) else {}
    action = str(supervisor.get("action") or "")
    raw_supervisor_meta = state.get("supervisor")
    supervisor_meta: dict[str, Any] = raw_supervisor_meta if isinstance(raw_supervisor_meta, dict) else {}
    raw_iteration_limit = budget.get("max_replan_count")
    iteration_limit = 3 if raw_iteration_limit is None else max(0, int(raw_iteration_limit))
    tasks = dict(state.get("tasks") or {})
    running = [
        task_id for task_id, task in tasks.items()
        if isinstance(task, dict) and str(task.get("execution_status") or "") == "running"
    ]
    retryable = _retryable_tasks(state)

    # Execution recovery is prioritised over semantic stop conditions: a worker
    # timeout is an infrastructure failure, not a research verdict.
    if retryable:
        return RuntimeDecision("retry", ("transient_execution_failure",), retryable)

    if action in {"STOP_BUDGET_PARTIAL", "STOP_FAILURE"}:
        return _stop_decision(state, reason=action.lower())

    # ``iteration`` identifies the repair currently under consideration; the
    # action at the configured limit is still permitted.
    if int(supervisor_meta.get("iteration") or 0) > iteration_limit and action != "COMPLETE":
        return _stop_decision(state, reason="supervisor_iteration_limit")
    if action == "COMPLETE":
        if _usable_evidence(state):
            return RuntimeDecision("synthesize", ("supervisor_complete", "usable_evidence"))
        return RuntimeDecision("finalize_failure", ("supervisor_complete", "no_usable_evidence"))

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
    if answerable_user_ask(state):
        return RuntimeDecision("deliver_partial", ("answerable_user_ask",))
    return RuntimeDecision("run_supervisor", ("semantic_strategy_required",))


__all__ = [
    "RuntimeDecision",
    "SEMANTIC_ACTIONS",
    "answerable_user_ask",
    "decide_control",
]

"""Deterministic gate before invoking a second supervisor."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ResearchDecision = Literal[
    "RETRY_EXECUTION",
    "REPAIR_GAP",
    "RESEARCH_COMPLETE",
    "STOP_BUDGET_PARTIAL",
    "STOP_FAILURE",
]


@dataclass(frozen=True)
class GapPrecheckResult:
    action: ResearchDecision
    blocking_gaps: tuple[str, ...] = ()
    actionable_gaps: tuple[str, ...] = ()
    reason: str = ""
    supervisor_calls_avoided: int = 0
    wave: int = 0
    budget_available: bool = True
    blocking_worker_failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def precheck_gap(
    state: dict[str, Any], *, max_waves: int = 0, max_repairs: int = 1
) -> GapPrecheckResult:
    raw_judgement = state.get("coverage_judgement")
    judgement: dict[str, Any] = raw_judgement if isinstance(raw_judgement, dict) else {}
    raw_budget = state.get("budget")
    budget: dict[str, Any] = raw_budget if isinstance(raw_budget, dict) else {}
    wave = int(state.get("dispatch_wave_id") or 0)
    exhausted = bool(budget.get("exhausted")) or str(state.get("budget_status") or "") == "exhausted"
    raw_gaps = judgement.get("gaps")
    gaps: list[Any] = raw_gaps if isinstance(raw_gaps, list) else []
    raw_missing = judgement.get("missing")
    missing: list[Any] = raw_missing if isinstance(raw_missing, list) else []
    blocking: list[str] = []
    actionable: list[str] = []
    for index, item in enumerate(gaps or missing, 1):
        if isinstance(item, dict):
            identifier = str(item.get("gap_id") or item.get("criterion_id") or item.get("description") or f"gap_{index}")
            is_blocking = bool(item.get("blocking", True))
        else:
            identifier = str(item).strip() or f"gap_{index}"
            is_blocking = True
        if is_blocking:
            blocking.append(identifier)
            if identifier not in actionable:
                actionable.append(identifier)
    failed_questions: list[str] = []
    for item in judgement.get("key_question_coverage") or []:
        if not isinstance(item, dict) or not bool(item.get("blocking")):
            continue
        question_id = str(item.get("question_id") or "").strip()
        missing_types = {str(value) for value in item.get("missing_evidence_types") or []}
        if question_id and "worker_failed" in missing_types:
            failed_questions.append(question_id)
            identifier = f"worker_failed:{question_id}"
            if identifier not in blocking:
                blocking.append(identifier)
            if identifier not in actionable:
                actionable.append(identifier)
    sufficient = bool(judgement.get("sufficient"))
    # ``max_replan_count`` is a control-plane limit.  Older state snapshots
    # omit it, which must mean the configured default rather than “no repair
    # budget”.  Only an explicit zero disables a repair wave.
    raw_repair_budget = budget.get("max_replan_count", max_repairs)
    repair_budget = int(raw_repair_budget) if raw_repair_budget is not None else max_repairs
    # Execution failure and semantic insufficiency are different budgets.
    # Retrying a timed-out worker must never consume a research repair.
    tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
    retryable_execution = [
        task_id
        for task_id, task in tasks.items()
        if isinstance(task, dict)
        and str(task.get("execution_status") or "") == "failed"
        and bool((task.get("failure") or {}).get("retryable"))
    ]
    if retryable_execution or (failed_questions and not tasks):
        return GapPrecheckResult(
            "RETRY_EXECUTION",
            tuple(blocking),
            tuple(actionable),
            "retryable_execution_failure",
            1,
            wave,
            not exhausted,
            tuple(failed_questions),
        )
    if sufficient:
        return GapPrecheckResult(
            "RESEARCH_COMPLETE",
            tuple(blocking),
            tuple(actionable),
            "coverage_sufficient",
            1,
            wave,
            not exhausted,
            (),
        )
    semantic_repairs = int(state.get("semantic_repairs") or 0)
    if blocking and not exhausted and repair_budget > semantic_repairs:
        return GapPrecheckResult(
            "REPAIR_GAP",
            tuple(blocking),
            tuple(actionable),
            "blocking_actionable_gap",
            0,
            wave,
            True,
            (),
        )
    if blocking and (exhausted or repair_budget <= semantic_repairs):
        return GapPrecheckResult(
            "STOP_BUDGET_PARTIAL",
            tuple(blocking),
            tuple(actionable),
            "budget_exhausted" if exhausted else "semantic_repair_budget_exhausted",
            1,
            wave,
            not exhausted,
            (),
        )
    return GapPrecheckResult(
        "STOP_FAILURE",
        tuple(blocking),
        tuple(actionable),
        "no_blocking_actionable_gap",
        1,
        wave,
        not exhausted,
        (),
    )


__all__ = ["GapPrecheckResult", "ResearchDecision", "precheck_gap"]

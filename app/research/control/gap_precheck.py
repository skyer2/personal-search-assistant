"""Deterministic gate before invoking a second supervisor."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class GapPrecheckResult:
    action: Literal["TARGETED_RESEARCH", "SYNTHESIZE"]
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
    state: dict[str, Any], *, max_waves: int = 2, max_repairs: int = 1
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
    if sufficient:
        reason = "coverage_sufficient"
    elif exhausted:
        reason = "budget_exhausted"
    elif repair_budget <= 0:
        reason = "repair_budget_unavailable"
    elif wave >= max_waves:
        reason = "max_research_waves"
    elif not blocking:
        reason = "no_blocking_actionable_gap"
    elif wave >= max_repairs + 1:
        reason = "repair_limit"
    else:
        return GapPrecheckResult("TARGETED_RESEARCH", tuple(blocking), tuple(actionable), "blocking_actionable_gap", 0, wave, True, tuple(failed_questions))
    return GapPrecheckResult("SYNTHESIZE", tuple(blocking), tuple(actionable), reason, 1, wave, not exhausted, tuple(failed_questions))


__all__ = ["GapPrecheckResult", "precheck_gap"]

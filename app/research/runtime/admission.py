"""Deterministic dispatch admission between Supervisor and Worker execution."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.research.runtime.task_identity import (
    execution_task_id,
    semantic_fingerprint,
)
from app.research.supervisor.models import ResearchTaskRequest


@dataclass(frozen=True)
class ApprovedResearchTask:
    request: ResearchTaskRequest
    task_id: str
    fingerprint: str


@dataclass(frozen=True)
class DispatchAdmission:
    approved: tuple[ApprovedResearchTask, ...]
    deferred_task_ids: tuple[str, ...]
    denied_reason: dict[str, str]
    requested_count: int
    approved_count: int

    @property
    def approved_task_ids(self) -> tuple[str, ...]:
        return tuple(item.task_id for item in self.approved)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved_task_ids": list(self.approved_task_ids),
            "deferred_task_ids": list(self.deferred_task_ids),
            "denied_reason": dict(self.denied_reason),
            "requested_count": self.requested_count,
            "approved_count": self.approved_count,
            "fingerprints": [item.fingerprint for item in self.approved],
        }


_EFFORT_TOKENS = {"small": 4_000, "medium": 10_000, "large": 20_000}
_PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}


def _method(manager: Any, name: str) -> Any:
    value = getattr(manager, name, None)
    return value if callable(value) else None


def admit_dispatch(
    requests: list[ResearchTaskRequest],
    *,
    wave_id: int,
    budget_manager: Any,
    state: dict[str, Any],
) -> DispatchAdmission:
    raw_budget = state.get("budget")
    budget = raw_budget if isinstance(raw_budget, dict) else {}
    previous = {
        str(item)
        for item in (state.get("task_fingerprints") or {}).keys()
        if str(item).strip()
    }
    max_workers = max(1, int(budget.get("max_parallel_workers") or getattr(budget_manager, "max_parallel_workers", 3) or 3))
    max_active = max(1, int(budget.get("max_active_tasks") or max_workers))
    max_slots = min(max_workers, max_active)

    remaining_tokens_method = _method(budget_manager, "remaining_for_research_tokens")
    remaining_tokens = int(remaining_tokens_method()) if remaining_tokens_method else max(
        0, int(budget.get("max_total_tokens") or 0) - int(budget.get("total_tokens") or 0)
    )
    research_allowed = _method(budget_manager, "research_allowed")
    allowed, block_reason = research_allowed() if research_allowed else (True, "")
    remaining_sec_method = _method(budget_manager, "remaining_for_research_sec")
    remaining_sec = float(remaining_sec_method()) if remaining_sec_method else float(
        budget.get("deadline_remaining_sec") or 1e9
    )
    snapshot_method = _method(budget_manager, "snapshot")
    snapshot = snapshot_method() if snapshot_method else None
    llm_calls = int(
        getattr(snapshot, "llm_calls", getattr(budget_manager, "llm_calls", budget.get("llm_calls") or 0))
        or 0
    )
    reserved_llm_calls = int(getattr(snapshot, "reserved_llm_calls", 0) or 0)
    max_llm_calls = int(
        getattr(snapshot, "llm_call_limit", getattr(budget_manager, "llm_call_limit", budget.get("max_llm_calls") or 0))
        or 0
    )

    ordered = sorted(
        enumerate(requests),
        key=lambda pair: (_PRIORITY_ORDER.get(pair[1].priority, 1), pair[0]),
    )
    approved: list[ApprovedResearchTask] = []
    deferred: list[str] = []
    denied: dict[str, str] = {}
    used_fingerprints = set(previous)

    for index, request in ordered:
        objective = str(request.objective or "").strip()
        if not objective:
            denied[f"request_{index}"] = "empty_objective"
            continue
        fingerprint = semantic_fingerprint(
            objective=objective,
            target_gaps=request.target_gaps,
            target_criteria=request.target_criteria,
        )
        if fingerprint in used_fingerprints:
            denied[f"request_{index}"] = "duplicate_semantic_task"
            continue
        if not allowed:
            denied[f"request_{index}"] = block_reason or "budget_unavailable"
            continue
        if remaining_sec <= 0:
            denied[f"request_{index}"] = "synthesis_time_reserve"
            continue
        estimated = _EFFORT_TOKENS.get(request.estimated_effort, _EFFORT_TOKENS["medium"])
        requested_llm_calls = max(1, int(request.max_llm_calls or 1))
        if len(approved) >= max_slots:
            deferred.append(f"request_{index}")
            continue
        if remaining_tokens < estimated:
            denied[f"request_{index}"] = "research_token_cap"
            continue
        if max_llm_calls and llm_calls + reserved_llm_calls + requested_llm_calls > max_llm_calls:
            denied[f"request_{index}"] = "budget_llm_calls"
            continue
        task_id = execution_task_id(wave_id, fingerprint)
        approved_request = replace(request, task_id=task_id)
        approved.append(
            ApprovedResearchTask(
                request=approved_request,
                task_id=task_id,
                fingerprint=fingerprint,
            )
        )
        used_fingerprints.add(fingerprint)
        remaining_tokens -= estimated
        llm_calls += requested_llm_calls

    return DispatchAdmission(
        approved=tuple(approved),
        deferred_task_ids=tuple(deferred),
        denied_reason=denied,
        requested_count=len(requests),
        approved_count=len(approved),
    )


__all__ = [
    "ApprovedResearchTask",
    "DispatchAdmission",
    "admit_dispatch",
]

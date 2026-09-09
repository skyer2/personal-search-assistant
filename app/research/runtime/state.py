"""Canonical ResearchState. Raw content stays in Artifact/Evidence stores."""

from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired, TypedDict

from app.research.domain.contracts import LifecycleStatus, WorkflowPhase
from app.research.runtime.reducers import merge_dicts
from app.research.runtime.reducers import merge_findings
from app.research.runtime.reducers import merge_records
from app.research.runtime.reducers import merge_strings
from app.research.runtime.reducers import merge_semantic_waves
from app.research.runtime.reducers import keep_last


class BudgetState(TypedDict):
    tool_calls: int
    max_tool_calls: int
    llm_calls: int
    max_llm_calls: int
    total_tokens: int
    max_total_tokens: int
    deadline_remaining_sec: float
    synthesis_reserve_sec: float
    max_parallel_workers: int
    max_replan_count: int
    max_task_attempts: int
    max_semantic_stall_cycles: int
    max_active_tasks: int
    exhausted: bool
    low: bool


class ResearchState(TypedDict):
    run_id: str
    session_id: str
    task_query: str
    user_id: str
    tenant_id: str
    project_id: str
    state_version: int

    search_mode: str
    search_mode_requested: str
    route_signals: list[str]
    conversation_summary: str
    resolved_query: str
    search_cards: list[dict[str, Any]]

    brief: dict[str, Any]
    fast_path: bool
    supervisor: dict[str, Any]
    supervisor_action: dict[str, Any]
    coverage_judgement: dict[str, Any]

    research_spec: dict[str, Any]
    coverage_contract: dict[str, Any]
    coverage_state: dict[str, Any]
    claims: Annotated[list[dict[str, Any]], merge_records]
    claim_conflicts: Annotated[list[dict[str, Any]], merge_records]
    claim_resolutions: Annotated[list[dict[str, Any]], merge_records]
    evidence_records: Annotated[list[dict[str, Any]], merge_records]
    semantic_gaps: Annotated[dict[str, dict[str, Any]], keep_last]
    artifact_refs: Annotated[list[str], merge_strings]

    intent: dict[str, Any] | None
    plan: dict[str, Any] | None
    plan_version: int
    tasks: Annotated[dict[str, dict[str, Any]], merge_dicts]
    worker_results: Annotated[list[dict[str, Any]], operator.add]
    findings: Annotated[list[dict[str, Any]], merge_findings]
    evidence_refs: Annotated[list[str], merge_strings]
    artifacts: list[str]

    budget: BudgetState
    budget_status: str
    dispatch_wave_id: int

    draft_ref: str | None
    final_ref: str | None
    final_content: str
    phase: Annotated[str, keep_last]
    lifecycle: dict[str, str]
    termination: dict[str, Any] | None
    cancel_reason: str
    abort_reason: str
    planning_failure: dict[str, Any]
    internal_error: dict[str, Any]
    stop_reason: str
    quality_failure: dict[str, Any]
    coverage_failure: dict[str, Any]

    needs_clarification: bool
    needs_plan_review: bool
    progress_assessment: dict[str, Any]
    evidence_assessment: dict[str, Any]
    execution_health: dict[str, Any]
    delivery_readiness: dict[str, Any]
    quality_assessment: dict[str, Any]
    control_decision: dict[str, Any]
    synthesis_attempts: int
    synthesis_failed: bool
    candidate_set: Annotated[dict[str, Any], merge_dicts]
    marginal_gain: dict[str, Any]
    semantic_wave_gains: Annotated[list[dict[str, Any]], merge_semantic_waves]
    semantic_stall: int


class WorkerTaskState(TypedDict):
    run_id: str
    session_id: str
    plan_version: int
    task_id: str
    step_index: int
    step_type: str
    description: str
    subagent: str
    task_query: str
    candidate_context: NotRequired[str]
    candidate_set: NotRequired[dict[str, Any]]
    user_id: NotRequired[str]
    tenant_id: NotRequired[str]
    project_id: NotRequired[str]


def empty_research_state(
    *,
    run_id: str,
    session_id: str,
    task_query: str,
    user_id: str = "",
    tenant_id: str = "",
    project_id: str = "",
    max_tool_calls: int = 80,
    max_replan_count: int = 3,
    search_mode: str = "agent",
) -> ResearchState:
    return {
        "run_id": run_id,
        "session_id": session_id,
        "task_query": task_query,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "state_version": 1,
        "search_mode": search_mode or "agent",
        "search_mode_requested": search_mode or "agent",
        "route_signals": [],
        "conversation_summary": "",
        "resolved_query": task_query,
        "search_cards": [],
        "brief": {},
        "fast_path": False,
        "supervisor": {"iteration": 0, "last_action": "", "reasoning_summary": ""},
        "supervisor_action": {},
        "coverage_judgement": {},
        "research_spec": {},
        "coverage_contract": {},
        "coverage_state": {},
        "claims": [],
        "claim_conflicts": [],
        "claim_resolutions": [],
        "evidence_records": [],
        "semantic_gaps": {},
        "artifact_refs": [],
        "intent": None,
        "plan": None,
        "plan_version": 1,
        "tasks": {},
        "worker_results": [],
        "findings": [],
        "evidence_refs": [],
        "artifacts": [],
        "budget": {
            "tool_calls": 0,
            "max_tool_calls": max_tool_calls,
            "llm_calls": 0,
            "max_llm_calls": 80,
            "total_tokens": 0,
            "max_total_tokens": 300000,
            "deadline_remaining_sec": 1800.0,
            "synthesis_reserve_sec": 180.0,
            "max_parallel_workers": 3,
            "max_replan_count": max_replan_count,
            "max_task_attempts": 2,
            "max_semantic_stall_cycles": 2,
            "max_active_tasks": 3,
            "exhausted": False,
            "low": False,
        },
        "budget_status": "available",
        "dispatch_wave_id": 0,
        "draft_ref": None,
        "final_ref": None,
        "final_content": "",
        "phase": WorkflowPhase.BOOTSTRAP.value,
        "lifecycle": {"status": LifecycleStatus.RUNNING.value},
        "termination": None,
        "cancel_reason": "",
        "abort_reason": "",
        "planning_failure": {},
        "internal_error": {},
        "stop_reason": "",
        "quality_failure": {},
        "coverage_failure": {},
        "needs_clarification": False,
        "needs_plan_review": False,
        "progress_assessment": {},
        "evidence_assessment": {},
        "execution_health": {},
        "delivery_readiness": {},
        "quality_assessment": {},
        "control_decision": {},
        "synthesis_attempts": 0,
        "synthesis_failed": False,
        "candidate_set": {},
        "marginal_gain": {},
        "semantic_wave_gains": [],
        "semantic_stall": 0,
    }


__all__ = ["BudgetState", "ResearchState", "WorkerTaskState", "empty_research_state"]

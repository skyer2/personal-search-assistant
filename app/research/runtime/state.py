"""
Graph State：只保留影响控制流的字段。

原始网页 / SQL 全文 / PDF 进 Evidence/Artifact Store，不进 checkpoint。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired, TypedDict

from app.research.runtime.reducers import merge_dicts
from app.research.domain.contracts import (
    OutcomeStatus,
    WorkflowPhase,
)


class BudgetState(TypedDict):
    tool_calls: int
    max_tool_calls: int
    replan_count: int
    max_replan_count: int
    max_parallel_workers: NotRequired[int]


class ResearchState(TypedDict):
    run_id: str
    session_id: str
    task_query: str
    user_id: str
    tenant_id: str
    project_id: str

    search_mode: str
    search_mode_requested: str
    route_signals: list[str]
    conversation_summary: str
    resolved_query: str
    search_cards: list[dict[str, Any]]

    brief: dict[str, Any]
    intent: dict[str, Any] | None
    plan: dict[str, Any] | None
    plan_version: int

    tasks: Annotated[dict[str, dict[str, Any]], merge_dicts]
    task_status: Annotated[dict[str, str], merge_dicts]

    worker_results: Annotated[list[dict[str, Any]], operator.add]
    findings: Annotated[list[dict[str, Any]], operator.add]
    evidence_refs: Annotated[list[str], operator.add]

    budget: BudgetState
    replan_count: int
    replan_attempts: int
    replan_applied_count: int

    draft_ref: str | None
    final_ref: str | None
    final_content: str
    artifacts: list[str]
    phase: str
    outcome: str
    status: str
    abort_reason: str
    termination: dict[str, Any] | None

    needs_clarification: bool
    needs_plan_review: bool
    progress: str
    quality_passed: bool
    quality_reason: str
    quality_repairable: bool
    quality_repair_action: str
    quality_attempts: int
    progress_assessment: dict[str, Any]
    candidate_set: Annotated[dict[str, Any], merge_dicts]
    replan_exhausted: bool
    control_no_progress: bool
    marginal_gain: dict[str, Any]
    synthesis_admission: bool
    synthesis_mode: str
    synthesis_admission_reason: str
    trusted_evidence_count: int
    control_fingerprint: str
    stagnant_cycles: int


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
        "search_mode": search_mode or "agent",
        "search_mode_requested": search_mode or "agent",
        "route_signals": [],
        "conversation_summary": "",
        "resolved_query": task_query,
        "search_cards": [],
        "brief": {},
        "intent": None,
        "plan": None,
        "plan_version": 1,
        "tasks": {},
        "task_status": {},
        "worker_results": [],
        "findings": [],
        "evidence_refs": [],
        "budget": {
            "tool_calls": 0,
            "max_tool_calls": max_tool_calls,
            "replan_count": 0,
            "max_replan_count": max_replan_count,
            "max_parallel_workers": 3,
        },
        "replan_count": 0,
        "replan_attempts": 0,
        "replan_applied_count": 0,
        "draft_ref": None,
        "final_ref": None,
        "final_content": "",
        "artifacts": [],
        "phase": WorkflowPhase.BOOTSTRAP.value,
        "outcome": OutcomeStatus.RUNNING.value,
        "status": "running",
        "abort_reason": "",
        "termination": None,
        "needs_clarification": False,
        "needs_plan_review": False,
        "progress": "run",
        "quality_passed": False,
        "quality_reason": "",
        "quality_repairable": False,
        "quality_repair_action": "",
        "quality_attempts": 0,
        "progress_assessment": {},
        "candidate_set": {},
        "replan_exhausted": False,
        "control_no_progress": False,
        "marginal_gain": {},
        "synthesis_admission": False,
        "synthesis_mode": "",
        "synthesis_admission_reason": "",
        "trusted_evidence_count": 0,
        "control_fingerprint": "",
        "stagnant_cycles": 0,
    }

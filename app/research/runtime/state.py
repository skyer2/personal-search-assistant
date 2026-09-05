"""
Graph State：只保留影响控制流的字段。

原始网页 / SQL 全文 / PDF 进 Evidence/Artifact Store，不进 checkpoint。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired, TypedDict

from app.research.runtime.reducers import merge_dicts


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

    task_status: Annotated[dict[str, str], merge_dicts]

    worker_results: Annotated[list[dict[str, Any]], operator.add]
    findings: Annotated[list[dict[str, Any]], operator.add]
    evidence_refs: Annotated[list[str], operator.add]

    budget: BudgetState
    replan_count: int

    draft_ref: str | None
    final_ref: str | None
    final_content: str
    artifacts: list[str]
    status: str
    abort_reason: str

    needs_clarification: bool
    needs_plan_review: bool
    progress: str
    quality_passed: bool
    progress_assessment: dict[str, Any]
    replan_exhausted: bool
    marginal_gain: dict[str, Any]


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
        "draft_ref": None,
        "final_ref": None,
        "final_content": "",
        "artifacts": [],
        "status": "running",
        "abort_reason": "",
        "needs_clarification": False,
        "needs_plan_review": False,
        "progress": "run",
        "quality_passed": False,
        "progress_assessment": {},
        "replan_exhausted": False,
        "marginal_gain": {},
    }

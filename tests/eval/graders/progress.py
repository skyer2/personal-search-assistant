"""ProgressEvaluator component grader。"""

from __future__ import annotations

from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.planning.progress import assess_progress


def plan_from_case(case: dict[str, Any]) -> ExecutionPlan:
    raw = dict(case.get("plan") or {})
    steps = []
    for item in raw.get("steps") or []:
        steps.append(
            PlanStep(
                step_type=str(item.get("step_type") or "research"),
                description=str(item.get("description") or item.get("objective") or ""),
                task_id=str(item.get("task_id") or ""),
                depends_on=list(item.get("depends_on") or []),
                allowed_tools=list(item.get("allowed_tools") or []),
                objective=str(item.get("objective") or ""),
                metadata=dict(item.get("metadata") or {}),
            )
        )
    return ExecutionPlan(
        steps=steps,
        summary=str(raw.get("summary") or case.get("query") or ""),
        planning_mode=str(raw.get("planning_mode") or "dynamic"),
        research_brief=str(raw.get("research_brief") or case.get("query") or ""),
        plan_version=int(raw.get("plan_version") or 1),
    )


def grade_progress_case(case: dict[str, Any]) -> dict[str, Any]:
    plan = plan_from_case(case)
    assessment = assess_progress(
        plan,
        task_status=dict(case.get("task_status") or {}),
        worker_results=list(case.get("worker_results") or []),
        query=str(case.get("query") or ""),
        aborted=bool(case.get("aborted")),
        current_year=int(case.get("current_year") or 2026),
        enabled=True,
    )
    expect = dict(case.get("expected") or {})
    issues: list[str] = []
    if expect.get("verdict") and assessment.verdict != expect["verdict"]:
        issues.append(f"verdict:{assessment.verdict}!={expect['verdict']}")
    for field_name in expect.get("must_have") or []:
        if not getattr(assessment, field_name, None):
            issues.append(f"missing_signal:{field_name}")
    for field_name in expect.get("must_not_have") or []:
        if getattr(assessment, field_name, None):
            issues.append(f"unexpected_signal:{field_name}")
    return {
        "ok": not issues,
        "issues": issues,
        "verdict": assessment.verdict,
        "assessment": assessment.to_dict(),
    }

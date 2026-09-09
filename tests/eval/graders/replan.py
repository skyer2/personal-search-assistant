"""Semantic gap-fill grader: focused tasks, bounded scope, source policy."""

from __future__ import annotations

from typing import Any

from app.research.coverage.gaps import stable_gap_id
from app.research.planning.gap_fill import gap_fill
from app.research.spec.compiler import compile_research_spec


def _semantic_gaps(assessment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    signals = [
        *(assessment.get("coverage_gaps") or []),
        *(assessment.get("missing_dimensions") or []),
        *(assessment.get("stale_evidence") or []),
    ]
    gaps: dict[str, dict[str, Any]] = {}
    for signal in dict.fromkeys(str(item) for item in signals if str(item).strip()):
        gap_id = stable_gap_id("subject_1", signal, "coverage")
        gaps[gap_id] = {
            "gap_id": gap_id,
            "gap_type": "coverage",
            "subject_id": "subject_1",
            "dimension_id": signal,
            "severity": "high",
            "blocking": True,
            "actionable": True,
            "attempt_count": 0,
        }
    return gaps


def grade_replan_case(case: dict[str, Any]) -> dict[str, Any]:
    spec = compile_research_spec(str(case["query"]))
    assessment = dict(case.get("assessment") or {})
    expect = dict(case.get("expected") or {})
    max_new = int(case.get("max_new_tasks") or expect.get("max_new_tasks") or 2)
    result = gap_fill(
        spec,
        _semantic_gaps(assessment),
        plan_version=1,
        max_tasks=max_new,
    )
    added = result.plan.steps if result.applied else []
    issues: list[str] = []
    if expect.get("must_add_tasks") and not added:
        issues.append(result.reason)
    if expect.get("must_add_tasks") is False and added:
        issues.append("unexpected_tasks")
    if len(added) > max_new:
        issues.append("exceeds_max_new_tasks")
    if expect.get("plan_version_bump") and added and result.plan.plan_version != 1 + int(expect["plan_version_bump"]):
        issues.append("plan_version_not_bumped")
    task_ids = [step.task_id for step in added if step.task_id]
    if expect.get("no_duplicate_task_ids") and len(task_ids) != len(set(task_ids)):
        issues.append("duplicate_task_id")
    for tool in expect.get("forbidden_tools") or []:
        if any(tool in (step.allowed_tools or []) for step in added):
            issues.append(f"forbidden_tool:{tool}")
    if added and not all((step.metadata or {}).get("resolves_gap_ids") for step in added):
        issues.append("task_missing_gap_binding")
    useful = bool(added) and assessment.get("status") == "gap"
    return {
        "ok": not issues,
        "issues": issues,
        "added_tasks": task_ids,
        "from_plan_version": 1,
        "to_plan_version": result.plan.plan_version,
        "reason": result.reason,
        "useful": useful,
    }

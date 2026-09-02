"""Replan grader：是否针对缺口补 task，而不是重复旧任务或越权。"""

from __future__ import annotations

from typing import Any

from app.agent.harness.planner import understand_task
from app.research.planning.compose import compose_execution_plan_sync
from app.research.planning.plan_patch import apply_plan_patch, build_progress_patch
from app.research.planning.policy import parse_source_policy
from app.research.planning.validator import RESEARCH_TYPES


def grade_replan_case(case: dict[str, Any]) -> dict[str, Any]:
    intent = understand_task(case["query"], bool(case.get("requires_upload")))
    plan, compose_issues = compose_execution_plan_sync(intent)
    for step in plan.steps:
        if step.step_type in RESEARCH_TYPES:
            step.metadata["status"] = "done"
    expect = dict(case.get("expected") or {})
    max_new = int(case.get("max_new_tasks") or expect.get("max_new_tasks") or 2)
    assessment = dict(case.get("assessment") or {})
    patch = build_progress_patch(
        plan,
        intent,
        assessment=assessment,
        max_new_tasks=max_new,
    )
    added = [item for item in (patch.get("add_tasks") or []) if isinstance(item, dict)]
    issues: list[str] = []
    if expect.get("must_add_tasks") and not added:
        issues.append("no_tasks_added")
    if expect.get("empty_or_reject_ok") and assessment.get("verdict") == "enough":
        added = []
    elif expect.get("must_add_tasks") is False and added:
        issues.append("unexpected_tasks")
    if len(added) > max_new:
        issues.append("exceeds_max_new_tasks")

    policy = parse_source_policy(intent.raw_query)
    updated, apply_issues = apply_plan_patch(
        plan,
        patch if added else {"add_tasks": []},
        intent,
        policy=policy,
        max_new_tasks=max_new,
    )
    if added and apply_issues:
        issues.extend(apply_issues)
    if expect.get("plan_version_bump") and added:
        if int(updated.plan_version or 1) != int(plan.plan_version or 1) + int(expect["plan_version_bump"]):
            issues.append("plan_version_not_bumped")
    ids = [s.task_id for s in updated.steps if s.task_id]
    if expect.get("no_duplicate_task_ids") and len(ids) != len(set(ids)):
        issues.append("duplicate_task_id")
    for tool in expect.get("forbidden_tools") or []:
        for step in updated.steps:
            if tool in (step.allowed_tools or []):
                issues.append(f"forbidden_tool:{tool}")
                break
    reason = str(patch.get("reason") or "")
    needle = expect.get("reason_contains")
    if needle and needle not in reason and not added:
        issues.append("reason_mismatch")

    useful = bool(added) and assessment.get("verdict") == "gap" and not apply_issues
    return {
        "ok": not issues and not compose_issues,
        "issues": issues + compose_issues,
        "added_tasks": [item.get("task_id") for item in added],
        "from_plan_version": int(plan.plan_version or 1),
        "to_plan_version": int(updated.plan_version or 1),
        "reason": reason,
        "useful": useful,
    }

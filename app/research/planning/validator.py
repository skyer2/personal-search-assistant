"""Plan / PlanPatch 校验：DAG、预算、来源策略（个人版 web + file）。"""

from __future__ import annotations

from app.agent.harness.state import ExecutionPlan, PlanStep, TaskIntent
from app.research.planning.policy import (
    SourcePolicy,
    parse_source_policy,
    tools_for_sources,
)

SYNTHESIS_TYPES = frozenset({"generate_markdown", "summarize", "convert_pdf"})
RESEARCH_TYPES = frozenset({"research", "network_search", "file_read"})


def _covers_source(plan: ExecutionPlan, source: str) -> bool:
    need_tools = set(tools_for_sources([source]))
    for step in plan.steps:
        if step.step_type == {
            "web": "network_search",
            "file": "file_read",
        }.get(source):
            return True
        allowed = set(step.allowed_tools or [])
        if allowed & need_tools:
            return True
    return False


def _has_cycle(steps: list[PlanStep]) -> bool:
    ids = {step.task_id for step in steps if step.task_id}
    visiting: set[str] = set()
    seen: set[str] = set()
    deps = {step.task_id: list(step.depends_on or []) for step in steps if step.task_id}

    def visit(nid: str) -> bool:
        if nid in seen:
            return False
        if nid in visiting:
            return True
        visiting.add(nid)
        for dep in deps.get(nid, []):
            if dep in ids and visit(dep):
                return True
        visiting.remove(nid)
        seen.add(nid)
        return False

    return any(visit(tid) for tid in ids)


def validate_hybrid_plan(
    intent: TaskIntent,
    plan: ExecutionPlan,
    *,
    policy: SourcePolicy | None = None,
    max_plan_steps: int = 12,
    max_research_tasks: int = 6,
) -> list[str]:
    issues: list[str] = []
    policy = policy or parse_source_policy(intent.raw_query)
    if not plan.steps:
        return ["empty_plan"]

    if len(plan.steps) > max_plan_steps:
        issues.append("too_many_steps")

    research_count = sum(1 for s in plan.steps if s.step_type in RESEARCH_TYPES)
    if research_count > max_research_tasks:
        issues.append("too_many_research_tasks")

    ids = [s.task_id for s in plan.steps if s.task_id]
    if len(ids) != len(set(ids)):
        issues.append("duplicate_task_id")
    id_set = set(ids)

    for step in plan.steps:
        for dep in step.depends_on or []:
            if dep and dep not in id_set:
                issues.append("missing_dependency")

    if _has_cycle(plan.steps):
        issues.append("cycle_in_dependencies")

    if intent.needs_network and "web" not in policy.forbidden_sources:
        if not _covers_source(plan, "web"):
            issues.append("missing_network_search")
    if intent.needs_file_read and "file" not in policy.forbidden_sources:
        if not _covers_source(plan, "file"):
            issues.append("missing_file_read")

    if intent.deliverable == "md" and not any(s.step_type == "generate_markdown" for s in plan.steps):
        issues.append("missing_generate_markdown")
    if intent.deliverable == "pdf":
        if not any(s.step_type == "generate_markdown" for s in plan.steps):
            issues.append("missing_generate_markdown")
        if not any(s.step_type == "convert_pdf" for s in plan.steps):
            issues.append("missing_convert_pdf")
    if intent.deliverable == "text" and not any(s.step_type == "summarize" for s in plan.steps):
        issues.append("missing_summarize")

    return issues

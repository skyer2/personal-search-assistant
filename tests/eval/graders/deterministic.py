"""L0 / L1 确定性 invariants：Plan DAG、预算、来源策略、并行就绪。"""

from __future__ import annotations

from typing import Any

from app.agent.harness.state import ExecutionPlan
from app.research.planning.validator import RESEARCH_TYPES, SYNTHESIS_TYPES, _has_cycle


def _research_steps(plan: ExecutionPlan) -> list:
    return [s for s in plan.steps if s.step_type in RESEARCH_TYPES]


def independent_research(plan: ExecutionPlan) -> list:
    return [s for s in _research_steps(plan) if not s.depends_on]


def grade_plan_invariants(plan: ExecutionPlan, expect: dict[str, Any] | None = None) -> dict[str, Any]:
    """不要求固定 step type 序列，只检查 Agent-native 约束。"""
    expect = dict(expect or {})
    issues: list[str] = []
    research = _research_steps(plan)
    independent = independent_research(plan)
    tools = {tool for step in plan.steps for tool in (step.allowed_tools or [])}
    step_types = [s.step_type for s in plan.steps]

    if expect.get("acyclic", True) and _has_cycle(plan.steps):
        issues.append("cycle_in_dependencies")
    min_ind = int(expect.get("min_independent_research") or 0)
    if min_ind and len(independent) < min_ind:
        issues.append(f"independent_research<{min_ind}")
    max_research = expect.get("max_research_tasks")
    if max_research is not None and len(research) > int(max_research):
        issues.append("too_many_research_tasks")
    if expect.get("has_synthesis") and not any(s.step_type in SYNTHESIS_TYPES for s in plan.steps):
        issues.append("missing_synthesis")
    if expect.get("has_convert_pdf") and "convert_pdf" not in step_types:
        issues.append("missing_convert_pdf")
    for tool in expect.get("forbidden_tools") or []:
        if tool in tools:
            issues.append(f"forbidden_tool:{tool}")
    for step_type in expect.get("forbidden_step_types") or []:
        if step_type in step_types:
            issues.append(f"forbidden_step:{step_type}")
    sources = expect.get("required_sources") or []
    if "web" in sources and not (
        any(s.step_type == "network_search" for s in plan.steps)
        or {"internet_search", "fetch_url"} & tools
    ):
        issues.append("missing_web_source")
    if "file" in sources and not (
        any(s.step_type == "file_read" for s in plan.steps) or "read_file_content" in tools
    ):
        issues.append("missing_file_source")
    if expect.get("independent_can_parallel") and len(independent) < 2:
        issues.append("not_parallel_ready")
    mode = expect.get("planning_mode")
    if mode and str(plan.planning_mode or "") != str(mode):
        issues.append(f"planning_mode!={mode}")

    return {
        "ok": not issues,
        "issues": issues,
        "research_count": len(research),
        "independent_research": len(independent),
        "step_types": step_types,
        "planning_mode": plan.planning_mode,
        "plan_version": int(plan.plan_version or 1),
    }

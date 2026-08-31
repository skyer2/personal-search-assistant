"""组装执行计划：policy 分流 +（可选）Lead Planner + 代码校验。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.harness.planner import build_plan, finalize_plan
from app.agent.harness.state import ExecutionPlan, TaskIntent
from app.research.planning.lead_planner import heuristic_dynamic_plan, lead_plan_with_llm
from app.research.planning.policy import apply_source_policy, parse_source_policy, select_planning_mode
from app.research.planning.validator import validate_hybrid_plan


@dataclass
class PlanningLimits:
    hybrid_enabled: bool = True
    dynamic_lead_enabled: bool = True
    max_research_tasks: int = 6
    max_plan_patch_tasks: int = 2
    max_plan_steps: int = 12

    @classmethod
    def from_config(cls, config: Any | None) -> "PlanningLimits":
        if config is None:
            return cls()
        return cls(
            hybrid_enabled=bool(getattr(config, "planner_hybrid_enabled", True)),
            dynamic_lead_enabled=bool(getattr(config, "planner_dynamic_lead_enabled", True)),
            max_research_tasks=int(getattr(config, "planner_max_research_tasks", 6) or 6),
            max_plan_patch_tasks=int(getattr(config, "planner_max_plan_patch_tasks", 2) or 2),
            max_plan_steps=int(getattr(config, "max_plan_steps", 12) or 12),
        )


def _stamp(plan: ExecutionPlan, intent: TaskIntent, mode: str, brief: str = "") -> ExecutionPlan:
    plan.planning_mode = mode
    objective = brief or plan.research_brief
    attached = getattr(intent, "brief", None)
    if attached is not None and getattr(attached, "objective", ""):
        objective = objective or attached.objective
    plan.research_brief = objective or intent.summary
    intent.planning_mode = mode
    return plan


def compose_execution_plan_sync(
    intent: TaskIntent,
    *,
    limits: PlanningLimits | None = None,
) -> tuple[ExecutionPlan, list[str]]:
    """无 LLM 路径：DYNAMIC 用启发式 objective DAG，否则走原 source template。"""
    limits = limits or PlanningLimits()
    intent = apply_source_policy(intent)
    policy = parse_source_policy(intent.raw_query)
    mode = select_planning_mode(intent, hybrid_enabled=limits.hybrid_enabled)
    if mode == "dynamic":
        plan = heuristic_dynamic_plan(intent, policy)
        plan = finalize_plan(plan)
        issues = validate_hybrid_plan(
            intent,
            plan,
            policy=policy,
            max_plan_steps=limits.max_plan_steps,
            max_research_tasks=limits.max_research_tasks,
        )
        if issues:
            plan = finalize_plan(_stamp(build_plan(intent), intent, "template"))
            issues = validate_hybrid_plan(
                intent,
                plan,
                policy=policy,
                max_plan_steps=limits.max_plan_steps,
                max_research_tasks=limits.max_research_tasks,
            )
            return _stamp(plan, intent, "template"), issues
        return _stamp(plan, intent, "dynamic", plan.research_brief), issues
    plan = finalize_plan(_stamp(build_plan(intent), intent, mode))
    issues = validate_hybrid_plan(
        intent,
        plan,
        policy=policy,
        max_plan_steps=limits.max_plan_steps,
        max_research_tasks=limits.max_research_tasks,
    )
    return plan, issues


async def compose_execution_plan(
    intent: TaskIntent,
    *,
    model: Any = None,
    session_id: str = "",
    llm_enabled: bool = True,
    limits: PlanningLimits | None = None,
) -> tuple[ExecutionPlan, list[str]]:
    limits = limits or PlanningLimits()
    intent = apply_source_policy(intent)
    policy = parse_source_policy(intent.raw_query)
    mode = select_planning_mode(intent, hybrid_enabled=limits.hybrid_enabled)
    if mode == "dynamic" and limits.dynamic_lead_enabled and llm_enabled and model is not None:
        llm_plan = await lead_plan_with_llm(
            intent,
            policy,
            model=model,
            session_id=session_id,
            max_tasks=limits.max_research_tasks,
        )
        if llm_plan is not None:
            llm_plan = finalize_plan(_stamp(llm_plan, intent, "dynamic", llm_plan.research_brief))
            issues = validate_hybrid_plan(
                intent,
                llm_plan,
                policy=policy,
                max_plan_steps=limits.max_plan_steps,
                max_research_tasks=limits.max_research_tasks,
            )
            if not issues:
                return llm_plan, issues
    return compose_execution_plan_sync(intent, limits=limits)

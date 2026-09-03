"""Hard Ceiling + Adaptive Effort Allocation.

Planner 提出软资源需求；Harness 用硬顶 clamp。
永远不能抬高 hard ceiling。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Complexity = Literal[
    "narrow",
    "compound",
    "breadth_heavy",
    "depth_heavy",
    "open_ended",
]
EffortTier = Literal["shallow", "standard", "thorough"]

_COMPARE = ("比较", "对比", " vs ", " VS ", "versus", "横向")
_LANDSCAPE = ("竞争格局", "多维度", "综合对比", "全面", "深入")
_CHAIN = ("执行链", "调用链", "端到端", "从", "到", "内核", "协议", "迁移", "重构")


@dataclass(frozen=True)
class HardCeiling:
    max_tool_calls: int = 40
    max_step_tool_calls: int = 8
    max_parallel_workers: int = 3
    max_research_tasks: int = 5
    max_plan_patch_tasks: int = 2
    max_replan_count: int = 2
    max_plan_steps: int = 8
    max_run_sec: int = 600
    max_total_tokens: int = 100_000

    @classmethod
    def from_config(cls, config: Any | None) -> "HardCeiling":
        if config is None:
            return cls()
        return cls(
            max_tool_calls=int(getattr(config, "max_tool_calls", 40) or 40),
            max_step_tool_calls=int(getattr(config, "max_step_tool_calls", 8) or 8),
            max_parallel_workers=int(getattr(config, "max_parallel_workers", 3) or 3),
            max_research_tasks=int(getattr(config, "planner_max_research_tasks", 5) or 5),
            max_plan_patch_tasks=int(getattr(config, "planner_max_plan_patch_tasks", 2) or 2),
            max_replan_count=int(getattr(config, "max_replan_count", 2) or 2),
            max_plan_steps=int(getattr(config, "max_plan_steps", 8) or 8),
            max_run_sec=int(getattr(config, "max_run_sec", 600) or 600),
            max_total_tokens=int(getattr(config, "max_total_tokens", 100_000) or 100_000),
        )

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class EffortPlan:
    complexity: Complexity = "narrow"
    tier: EffortTier = "standard"
    suggested_research_tasks: int = 2
    suggested_workers: int = 1
    initial_session_tool_budget: int = 12
    per_worker_tool_budget: int = 4
    replan_reserve_tasks: int = 1
    reserve_step_tool_calls: int = 2
    stop_criteria: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "complexity": self.complexity,
            "tier": self.tier,
            "suggested_research_tasks": self.suggested_research_tasks,
            "suggested_workers": self.suggested_workers,
            "initial_session_tool_budget": self.initial_session_tool_budget,
            "per_worker_tool_budget": self.per_worker_tool_budget,
            "replan_reserve_tasks": self.replan_reserve_tasks,
            "reserve_step_tool_calls": self.reserve_step_tool_calls,
            "stop_criteria": list(self.stop_criteria),
            "signals": list(self.signals),
        }


@dataclass(frozen=True)
class EffectiveBudget:
    """clamp(EffortPlan, HardCeiling) 后的实际配额。"""

    effort: EffortPlan
    hard: HardCeiling
    research_tasks: int
    parallel_workers: int
    session_tool_calls: int
    step_tool_calls: int
    replan_count: int
    plan_patch_tasks: int
    reserved_step_tool_calls: int
    max_run_sec: int
    max_plan_steps: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "research_tasks": self.research_tasks,
            "parallel_workers": self.parallel_workers,
            "session_tool_calls": self.session_tool_calls,
            "step_tool_calls": self.step_tool_calls,
            "replan_count": self.replan_count,
            "plan_patch_tasks": self.plan_patch_tasks,
            "reserved_step_tool_calls": self.reserved_step_tool_calls,
            "max_run_sec": self.max_run_sec,
            "max_plan_steps": self.max_plan_steps,
            "effort": self.effort.to_dict(),
            "hard_ceiling": self.hard.to_dict(),
        }

    def as_run_budget(self) -> dict[str, int]:
        """写入 LoopState.metadata['run_budget']，供护栏与步预算读取。"""
        return {
            "max_tool_calls": self.session_tool_calls,
            "max_step_tool_calls": self.step_tool_calls,
            "max_replan_count": self.replan_count,
            "max_plan_steps": self.max_plan_steps,
            "max_run_sec": self.max_run_sec,
            "max_plan_patch_tasks": self.plan_patch_tasks,
            "max_parallel_workers": self.parallel_workers,
            "reserved_step_tool_calls": self.reserved_step_tool_calls,
        }


def _brief_of(intent: Any) -> Any:
    return getattr(intent, "brief", None)


def estimate_complexity(intent: Any) -> EffortPlan:
    """确定性复杂度估计：不调 LLM，只读 Brief / query。"""
    query = str(getattr(intent, "raw_query", "") or "")
    brief = _brief_of(intent)
    entities = [str(x) for x in (getattr(brief, "entities", None) or []) if x]
    dimensions = [str(x) for x in (getattr(brief, "dimensions", None) or []) if x]
    depth = str(getattr(brief, "depth", "") or "standard")
    if depth not in {"shallow", "standard", "thorough"}:
        depth = "standard"
    criteria = [str(x) for x in (getattr(brief, "success_criteria", None) or []) if x]
    prefer_primary = bool(getattr(brief, "prefer_primary", False))
    freshness = str(getattr(brief, "freshness", "") or "any")
    deliverable = str(getattr(intent, "deliverable", "") or getattr(brief, "deliverable", "") or "text")

    signals: list[str] = []
    score = 0

    if any(m in query for m in _COMPARE) or len(entities) >= 2:
        score += 3
        signals.append("multi_entity_or_compare")
    if any(m in query for m in _LANDSCAPE) or len(dimensions) >= 3:
        score += 2
        signals.append("breadth_or_landscape")
    if depth == "thorough":
        score += 2
        signals.append("depth_thorough")
    elif depth == "shallow":
        score -= 1
        signals.append("depth_shallow")
    if len(dimensions) >= 2:
        score += 1
        signals.append("multi_dimension")
    if len(criteria) >= 2:
        score += 1
        signals.append("multi_success_criteria")
    if len(query) >= 80 or any(m in query for m in _CHAIN):
        score += 2
        signals.append("long_or_chain_query")
    if prefer_primary or freshness not in {"", "any"}:
        score += 1
        signals.append("primary_or_freshness")
    if deliverable in {"md", "pdf"}:
        score += 1
        signals.append("file_deliverable")

    if score <= 1:
        complexity: Complexity = "narrow"
        tier: EffortTier = "shallow" if depth == "shallow" else "standard"
        plan = EffortPlan(
            complexity=complexity,
            tier=tier,
            suggested_research_tasks=1,
            suggested_workers=1,
            initial_session_tool_budget=8,
            per_worker_tool_budget=3,
            replan_reserve_tasks=0,
            reserve_step_tool_calls=0,
            stop_criteria=("single_pass_enough",),
            signals=tuple(signals),
        )
    elif score <= 3:
        complexity = "compound"
        tier = "standard"
        plan = EffortPlan(
            complexity=complexity,
            tier=tier,
            suggested_research_tasks=2,
            suggested_workers=2,
            initial_session_tool_budget=16,
            per_worker_tool_budget=4,
            replan_reserve_tasks=1,
            reserve_step_tool_calls=2,
            stop_criteria=("coverage_ok", "no_major_conflict"),
            signals=tuple(signals),
        )
    elif "multi_entity_or_compare" in signals or "breadth_or_landscape" in signals:
        complexity = "breadth_heavy"
        tier = "thorough" if depth == "thorough" else "standard"
        # 实体任务 + 横向比较任务（启发式 DAG 需要）
        entity_n = len(entities) if len(entities) >= 2 else 3
        suggested_tasks = min(5, max(3, entity_n + 1))
        plan = EffortPlan(
            complexity=complexity,
            tier=tier,
            suggested_research_tasks=suggested_tasks,
            suggested_workers=3,
            initial_session_tool_budget=28,
            per_worker_tool_budget=6,
            replan_reserve_tasks=2,
            reserve_step_tool_calls=4,
            stop_criteria=("entity_coverage", "cross_compare_done"),
            signals=tuple(signals),
        )
    elif "depth_thorough" in signals or "long_or_chain_query" in signals:
        complexity = "depth_heavy"
        tier = "thorough"
        plan = EffortPlan(
            complexity=complexity,
            tier=tier,
            suggested_research_tasks=3,
            suggested_workers=2,
            initial_session_tool_budget=24,
            per_worker_tool_budget=7,
            replan_reserve_tasks=2,
            reserve_step_tool_calls=4,
            stop_criteria=("primary_sources", "chain_covered"),
            signals=tuple(signals),
        )
    else:
        complexity = "open_ended"
        tier = "thorough"
        plan = EffortPlan(
            complexity=complexity,
            tier=tier,
            suggested_research_tasks=4,
            suggested_workers=3,
            initial_session_tool_budget=32,
            per_worker_tool_budget=6,
            replan_reserve_tasks=2,
            reserve_step_tool_calls=4,
            stop_criteria=("diminishing_returns", "budget_reserve_ok"),
            signals=tuple(signals),
        )
    return plan


def apply_effort_to_hard_ceiling(effort: EffortPlan, hard: HardCeiling) -> EffectiveBudget:
    """LLM/估计器可申请；Harness clamp。"""
    research_tasks = max(1, min(effort.suggested_research_tasks, hard.max_research_tasks))
    parallel_workers = max(1, min(effort.suggested_workers, hard.max_parallel_workers))
    session_tool_calls = max(1, min(effort.initial_session_tool_budget, hard.max_tool_calls))
    step_tool_calls = max(1, min(effort.per_worker_tool_budget, hard.max_step_tool_calls))
    if effort.tier == "shallow" and effort.replan_reserve_tasks <= 0:
        replan_count = 0
    else:
        desired_replan = max(effort.replan_reserve_tasks, 1)
        replan_count = max(0, min(hard.max_replan_count, desired_replan))
    plan_patch_tasks = max(0, min(effort.replan_reserve_tasks, hard.max_plan_patch_tasks))
    reserved = max(0, min(effort.reserve_step_tool_calls, hard.max_step_tool_calls))
    return EffectiveBudget(
        effort=effort,
        hard=hard,
        research_tasks=research_tasks,
        parallel_workers=parallel_workers,
        session_tool_calls=session_tool_calls,
        step_tool_calls=step_tool_calls,
        replan_count=replan_count,
        plan_patch_tasks=plan_patch_tasks,
        reserved_step_tool_calls=reserved,
        max_run_sec=hard.max_run_sec,
        max_plan_steps=hard.max_plan_steps,
    )


def resolve_effective_budget(intent: Any, config: Any | None) -> EffectiveBudget:
    hard = HardCeiling.from_config(config)
    effort = estimate_complexity(intent)
    return apply_effort_to_hard_ceiling(effort, hard)


def brief_payload_for_lead_planner(
    intent: Any,
    *,
    effort: EffectiveBudget | EffortPlan | None = None,
) -> dict[str, Any]:
    """Lead Planner 必须吃完整 Research Brief，而不是只有 summary/slots。"""
    brief = _brief_of(intent)
    payload: dict[str, Any] = {}
    if brief is not None and hasattr(brief, "to_dict"):
        payload.update(brief.to_dict())
    elif isinstance(brief, dict):
        payload.update(brief)
    payload["summary"] = str(getattr(intent, "summary", "") or "")
    payload["deliverable"] = str(getattr(intent, "deliverable", "") or payload.get("deliverable") or "text")
    slots = getattr(intent, "slots", None)
    payload["slots"] = slots.to_dict() if slots is not None and hasattr(slots, "to_dict") else {}
    if effort is not None:
        payload["effort"] = effort.to_dict() if hasattr(effort, "to_dict") else dict(effort)
    return payload


def grant_on_gap(
    effective: EffectiveBudget,
    *,
    assessment: dict[str, Any] | None = None,
) -> dict[str, int]:
    """GAP 时发放 patch 任务数与新步检索额度；不抬会话硬顶。"""
    _ = assessment
    return {
        "max_new_tasks": int(effective.plan_patch_tasks),
        "max_retrieval_calls": int(effective.reserved_step_tool_calls or effective.step_tool_calls),
    }


def stamp_effort_on_plan(plan: Any, effective: EffectiveBudget) -> None:
    """把 effort 摘要与每研究步的检索预算写入 plan metadata / step.metadata。"""
    if plan is None:
        return
    meta = dict(getattr(plan, "metadata", None) or {})
    meta["effort_plan"] = effective.to_dict()
    plan.metadata = meta
    for step in list(getattr(plan, "steps", None) or []):
        if str(getattr(step, "step_type", "") or "") not in {"research", "network_search", "file_read"}:
            continue
        step_meta = dict(getattr(step, "metadata", None) or {})
        step_meta.setdefault("max_retrieval_calls", int(effective.step_tool_calls))
        step_meta.setdefault("effort_tier", effective.effort.tier)
        step_meta.setdefault("complexity", effective.effort.complexity)
        step.metadata = step_meta

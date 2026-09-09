"""Structured strategy-changing replan operator."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.coverage.gaps import SemanticGap
from app.research.domain.task_state import supersede_task
from app.research.planning.validator import PlanMutationProposal
from app.research.spec.models import ResearchSpec


@dataclass
class ReplanProposal:
    reason: str
    failed_strategy: str
    new_strategy: str
    changes: list[str]
    add_tasks: list[PlanStep] = field(default_factory=list)
    supersede_tasks: list[str] = field(default_factory=list)


@dataclass
class ReplanResult:
    applied: bool
    reason: str
    proposal: ReplanProposal
    plan: ExecutionPlan
    tasks: dict[str, dict[str, Any]]
    semantic_gaps: dict[str, dict[str, Any]]
    mutation: PlanMutationProposal | None = None


def strategy_fingerprint(plan: ExecutionPlan | dict[str, Any] | None) -> str:
    value = plan.to_dict() if isinstance(plan, ExecutionPlan) else dict(plan or {})
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:20]


def _source_contract(spec: ResearchSpec) -> tuple[list[str], list[str]]:
    allowed_sources = [str(item) for item in spec.source_policy.allowed if str(item).strip()] or ["web"]
    allowed_tools: list[str] = []
    if "web" in allowed_sources:
        allowed_tools.extend(["web_search", "fetch"])
    if "file" in allowed_sources:
        allowed_tools.append("read_file_content")
    if not allowed_tools:
        raise ValueError("spec_has_no_allowed_sources")
    return allowed_sources, allowed_tools


def replan(
    spec: ResearchSpec | dict[str, Any] | None,
    plan: ExecutionPlan | dict[str, Any] | None,
    semantic_gaps: dict[str, Any] | None,
    tasks: dict[str, Any] | None,
    *,
    reason: str,
    plan_version: int,
) -> ReplanResult:
    value = spec if isinstance(spec, ResearchSpec) else ResearchSpec.from_dict(spec)
    current_plan = plan if isinstance(plan, ExecutionPlan) else ExecutionPlan.from_dict(plan if isinstance(plan, dict) else None)
    allowed_sources, allowed_tools = _source_contract(value)
    gaps = [
        SemanticGap.from_dict(row)
        for row in (semantic_gaps or {}).values()
        if isinstance(row, dict) and bool(row.get("actionable", True))
    ]
    proposal = ReplanProposal(
        reason=reason or "semantic_gain_low",
        failed_strategy=strategy_fingerprint(current_plan),
        new_strategy="alternative_source_strategy_and_query_decomposition",
        changes=["source_strategy", "query_decomposition"],
    )
    if not proposal.changes:
        return ReplanResult(
            applied=False,
            reason="replan_requires_strategy_change",
            proposal=proposal,
            plan=current_plan,
            tasks=dict(tasks or {}),
            semantic_gaps={},
        )

    add_tasks: list[PlanStep] = []
    superseded: list[str] = []
    updated_gaps = {
        gap_id: dict(row)
        for gap_id, row in (semantic_gaps or {}).items()
        if isinstance(row, dict)
    }
    for gap in gaps[:3]:
        digest = hashlib.sha1(f"replan|{gap.gap_id}".encode("utf-8")).hexdigest()[:8]
        add_tasks.extend(
            [
                PlanStep(
                    step_type="research",
                    description=f"替代策略 A：官方/一手来源验证 {gap.subject_id} / {gap.dimension_id}",
                    subagent="研究工人",
                    task_id=f"t_replan_primary_{digest}",
                    allowed_tools=allowed_tools,
                    objective=f"使用官方或一手来源验证 {gap.subject_id} 的 {gap.dimension_id}",
                    metadata={
                        "allowed_sources": allowed_sources,
                        "source_strategy": "primary_only",
                        "kind": "research_task",
                        "task_kind": "strategy_replan",
                        "subject_id": gap.subject_id,
                        "coverage_keys": [gap.dimension_id],
                        "coverage_ids": [gap.coverage_id] if gap.coverage_id else [],
                        "resolves_gap_ids": [gap.gap_id],
                        "required": True,
                        "optional": False,
                        "priority": "P0",
                    },
                ),
                PlanStep(
                    step_type="research",
                    description=f"替代策略 B：多语言二手来源交叉验证 {gap.subject_id} / {gap.dimension_id}",
                    subagent="研究工人",
                    task_id=f"t_replan_cross_{digest}",
                    allowed_tools=allowed_tools,
                    objective=f"使用不同语言和独立二手来源交叉验证 {gap.subject_id} 的 {gap.dimension_id}",
                    metadata={
                        "allowed_sources": allowed_sources,
                        "source_strategy": "multilingual_cross_check",
                        "kind": "research_task",
                        "task_kind": "strategy_replan",
                        "subject_id": gap.subject_id,
                        "coverage_keys": [gap.dimension_id],
                        "coverage_ids": [gap.coverage_id] if gap.coverage_id else [],
                        "resolves_gap_ids": [gap.gap_id],
                        "required": True,
                        "optional": False,
                        "priority": "P0",
                    },
                ),
            ]
        )
        superseded.extend(
            step.task_id
            for step in current_plan.steps
            if gap.coverage_id in (step.metadata or {}).get("coverage_ids", [])
        )
        gap.attempt_count += 1
        gap.attempted_actions.append("REPLAN")
        updated_gaps[gap.gap_id] = gap.to_dict()

    if not add_tasks:
        return ReplanResult(
            applied=False,
            reason="no_actionable_gap",
            proposal=proposal,
            plan=current_plan,
            tasks=dict(tasks or {}),
            semantic_gaps={},
        )

    updated_tasks = dict(tasks or {})
    for old_task_id in dict.fromkeys(superseded):
        if old_task_id in updated_tasks:
            replacement = next(step.task_id for step in add_tasks if step.task_id)
            updated_tasks = supersede_task(updated_tasks, old_task_id, replacement_task_id=replacement)
    new_plan = ExecutionPlan(
        steps=add_tasks,
        summary=f"Strategy replan for {value.spec_id}: {proposal.new_strategy}",
        plan_version=plan_version + 1,
        planning_mode="strategy_replan",
    )
    if strategy_fingerprint(new_plan) == strategy_fingerprint(current_plan):
        return ReplanResult(
            applied=False,
            reason="replan_fingerprint_unchanged",
            proposal=proposal,
            plan=current_plan,
            tasks=updated_tasks,
            semantic_gaps={},
        )
    mutation = PlanMutationProposal(
        mutation_type="replan",
        proposed_plan=new_plan,
        proposed_tasks=updated_tasks,
        proposed_semantic_gaps=updated_gaps,
        metadata={
            "reason": "strategy_changed",
            "failed_strategy": proposal.failed_strategy,
            "new_strategy": proposal.new_strategy,
        },
    )
    return ReplanResult(
        applied=True,
        reason="strategy_changed",
        proposal=proposal,
        plan=new_plan,
        tasks=updated_tasks,
        semantic_gaps=updated_gaps,
        mutation=mutation,
    )


__all__ = ["ReplanProposal", "ReplanResult", "replan", "strategy_fingerprint"]

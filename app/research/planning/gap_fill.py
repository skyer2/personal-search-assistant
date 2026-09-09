"""SemanticGap → focused evidence task operator."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.coverage.gaps import SemanticGap
from app.research.spec.models import ResearchSpec


@dataclass
class GapFillResult:
    applied: bool
    reason: str
    plan: ExecutionPlan
    semantic_gaps: dict[str, dict[str, Any]]


def _task_id(gap: SemanticGap) -> str:
    digest = hashlib.sha1(gap.gap_id.encode("utf-8")).hexdigest()[:8]
    return f"t_gap_{digest}"


def gap_fill(
    spec: ResearchSpec | dict[str, Any] | None,
    semantic_gaps: dict[str, Any] | None,
    *,
    plan_version: int,
    max_tasks: int = 4,
) -> GapFillResult:
    value = spec if isinstance(spec, ResearchSpec) else ResearchSpec.from_dict(spec)
    gaps = [
        SemanticGap.from_dict(row)
        for row in (semantic_gaps or {}).values()
        if isinstance(row, dict) and bool(row.get("actionable", True)) and int(row.get("attempt_count") or 0) < 2
    ]
    if not gaps:
        return GapFillResult(
            applied=False,
            reason="no_actionable_gap",
            plan=ExecutionPlan(plan_version=plan_version, planning_mode="spec_driven"),
            semantic_gaps={},
        )
    allowed_sources = [str(item) for item in value.source_policy.allowed if str(item).strip()]
    allowed_tools: list[str] = []
    if "web" in allowed_sources:
        allowed_tools.extend(["web_search", "fetch"])
    if "file" in allowed_sources:
        allowed_tools.append("read_file_content")
    if not allowed_sources or not allowed_tools:
        return GapFillResult(
            applied=False,
            reason="no_allowed_source",
            plan=ExecutionPlan(plan_version=plan_version, planning_mode="spec_driven"),
            semantic_gaps={},
        )
    steps: list[PlanStep] = []
    updated: dict[str, dict[str, Any]] = {}
    for gap_id, row in (semantic_gaps or {}).items():
        if isinstance(row, dict):
            updated[gap_id] = dict(row)
    for gap in gaps[: max(0, int(max_tasks))]:
        dimension = gap.dimension_id
        subject = gap.subject_id
        steps.append(
            PlanStep(
                step_type="research",
                description=f"补证：{subject} / {dimension}（{gap.gap_type}）",
                subagent="研究工人",
                task_id=_task_id(gap),
                allowed_tools=allowed_tools,
                objective=f"只针对 {subject} 的 {dimension} 收集能关闭 {gap.gap_id} 的证据",
                metadata={
                    "allowed_sources": allowed_sources,
                    "kind": "research_task",
                    "task_kind": "gap_fill",
                    "subject_id": subject,
                    "coverage_keys": [dimension],
                    "coverage_ids": [gap.coverage_id] if gap.coverage_id else [],
                    "resolves_gap_ids": [gap.gap_id],
                    "gap_ids": [gap.gap_id],
                    "required": True,
                    "optional": False,
                    "priority": "P0",
                },
            )
        )
        gap.attempt_count += 1
        gap.attempted_actions.append("GAP_FILL")
        updated[gap.gap_id] = gap.to_dict()
    plan = ExecutionPlan(
        steps=steps,
        summary=f"Focused gap fill for {value.spec_id}",
        plan_version=plan_version + 1,
        planning_mode="semantic_gap_fill",
    )
    return GapFillResult(
        applied=True,
        reason="focused_gap_tasks_created",
        plan=plan,
        semantic_gaps=updated,
    )


__all__ = ["GapFillResult", "gap_fill"]

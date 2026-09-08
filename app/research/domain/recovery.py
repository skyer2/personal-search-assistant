"""Bounded, replacement-based recovery patches."""

from __future__ import annotations

import hashlib
import json
from typing import Any, TypedDict

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.domain.contracts import RecoveryLimits
from app.research.domain.gaps import active_research_steps, step_gap_ids, sync_business_gaps
from app.research.domain.task_state import new_task_state, normalize_tasks, supersede_task


class RecoveryTarget(TypedDict):
    gap_id: str
    task_id: str
    step_index: int
    recovery_generation: int
    fingerprint: str


class RecoveryPatch(TypedDict):
    applied: bool
    fingerprint: str
    target_gap_ids: list[str]
    superseded_task_ids: list[str]
    added_task_ids: list[str]
    recovery_generation: int
    plan: ExecutionPlan
    tasks: dict[str, Any]
    business_gaps: dict[str, Any]


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def eligible_recovery_targets(
    plan: ExecutionPlan,
    tasks: Any,
    gap_ids: list[str],
    limits: RecoveryLimits,
    rejected_hashes: list[str] | None = None,
) -> list[RecoveryTarget]:
    requested = list(dict.fromkeys(str(item) for item in gap_ids if str(item).strip()))
    rejected = {str(item) for item in rejected_hashes or []}
    targets: list[RecoveryTarget] = []
    for gap_id in requested:
        owners = [
            (index, step)
            for index, step in active_research_steps(plan)
            if gap_id in step_gap_ids(step)
            and not (isinstance(step.metadata, dict) and step.metadata.get("optional"))
        ]
        if not owners:
            continue
        index, step = owners[-1]
        task_id = step.resolved_task_id(index)
        metadata = dict(step.metadata or {})
        generation = max(0, int(metadata.get("generation") or 0))
        ceiling = min(
            max(0, int(limits["max_recovery_generation"])),
            max(0, int(limits["max_same_gap_recovery"])),
        )
        if generation >= ceiling:
            continue
        origin_task_id = str(metadata.get("origin_task_id") or task_id)
        next_generation = generation + 1
        replacement_task_id = f"{origin_task_id}_recovery_{next_generation}"
        if replacement_task_id in normalize_tasks(tasks) or any(
            replacement_task_id == item.resolved_task_id(item_index)
            for item_index, item in enumerate(plan.steps)
        ):
            continue
        fingerprint = _digest(
            {
                "gap_id": gap_id,
                "origin_task_id": origin_task_id,
                "from_generation": generation,
                "to_generation": next_generation,
                "strategy": "primary_evidence_only" if next_generation >= 2 else "targeted_lookup",
            }
        )
        if fingerprint in rejected:
            continue
        targets.append(
            RecoveryTarget(
                gap_id=gap_id,
                task_id=task_id,
                step_index=index,
                recovery_generation=next_generation,
                fingerprint=fingerprint,
            )
        )
    return targets


def _replacement_step(step: PlanStep, *, task_id: str, generation: int) -> PlanStep:
    metadata = dict(step.metadata or {})
    origin_task_id = str(metadata.get("origin_task_id") or step.task_id)
    objective = str(step.objective or step.description)
    recovery_objective = (
        f"仅使用一手或高质量来源定向复核：{objective}"
        if generation >= 2
        else f"定向补证：{objective}"
    )
    return PlanStep(
        step_type=step.step_type,
        description=recovery_objective,
        subagent=step.subagent,
        metadata={
            **metadata,
            "gap_ids": step_gap_ids(step),
            "generation": generation,
            "origin_task_id": origin_task_id,
            "supersedes_task_id": step.task_id,
        },
        task_id=task_id,
        depends_on=list(step.depends_on or []),
        allowed_tools=list(step.allowed_tools or []),
        objective=recovery_objective,
    )


def build_replacement_patch(
    plan: ExecutionPlan,
    tasks: Any,
    *,
    gap_ids: list[str],
    limits: RecoveryLimits,
    rejected_hashes: list[str] | None = None,
    previous_gaps: Any = None,
) -> RecoveryPatch:
    targets = eligible_recovery_targets(plan, tasks, gap_ids, limits, rejected_hashes)
    fingerprint = _digest(
        {
            "targets": [
                {
                    "gap_id": target["gap_id"],
                    "task_id": target["task_id"],
                    "recovery_generation": target["recovery_generation"],
                }
                for target in targets
            ]
        }
    )
    normalized_tasks = normalize_tasks(tasks)
    if not targets:
        return RecoveryPatch(
            applied=False,
            fingerprint=fingerprint,
            target_gap_ids=list(dict.fromkeys(str(item) for item in gap_ids if str(item).strip())),
            superseded_task_ids=[],
            added_task_ids=[],
            recovery_generation=0,
            plan=plan,
            tasks=normalized_tasks,
            business_gaps=sync_business_gaps(plan, normalized_tasks, previous_gaps),
        )

    superseded_task_ids: list[str] = []
    added_task_ids: list[str] = []
    task_replacements: dict[str, str] = {}
    for target in targets:
        step = plan.steps[target["step_index"]]
        old_task_id = step.resolved_task_id(target["step_index"])
        origin_task_id = str(dict(step.metadata or {}).get("origin_task_id") or old_task_id)
        new_task_id = f"{origin_task_id}_recovery_{target['recovery_generation']}"
        plan.steps[target["step_index"]] = _replacement_step(
            step,
            task_id=new_task_id,
            generation=target["recovery_generation"],
        )
        task_replacements[old_task_id] = new_task_id
        normalized_tasks = supersede_task(normalized_tasks, old_task_id, replacement_task_id=new_task_id)
        normalized_tasks.setdefault(new_task_id, new_task_state(new_task_id))
        superseded_task_ids.append(old_task_id)
        added_task_ids.append(new_task_id)

    for step in plan.steps:
        step.depends_on = [task_replacements.get(item, item) for item in step.depends_on]

    plan.plan_version += 1
    business_gaps = sync_business_gaps(plan, normalized_tasks, previous_gaps)
    return RecoveryPatch(
        applied=True,
        fingerprint=fingerprint,
        target_gap_ids=[target["gap_id"] for target in targets],
        superseded_task_ids=superseded_task_ids,
        added_task_ids=added_task_ids,
        recovery_generation=max(target["recovery_generation"] for target in targets),
        plan=plan,
        tasks=normalized_tasks,
        business_gaps=business_gaps,
    )


__all__ = [
    "RecoveryPatch",
    "RecoveryTarget",
    "build_replacement_patch",
    "eligible_recovery_targets",
]

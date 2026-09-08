"""Stable business-gap identity and active-plan obligations."""

from __future__ import annotations

import hashlib
import re
from typing import Any, TypedDict

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.domain.task_state import (
    ResultStatus,
    TaskExecutionStatus,
    normalize_tasks,
)


class GapState(TypedDict):
    gap_id: str
    dimension: str
    subject_id: str
    status: str
    reason_codes: list[str]
    attempt_count: int
    recovery_generation: int
    owner_task_ids: list[str]


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip()).strip("_").lower()
    return text[:48]


def stable_gap_id(step: PlanStep) -> str:
    metadata = dict(step.metadata or {})
    if str(metadata.get("produces_artifact") or "") == "candidate_set":
        return "candidate_pool"
    subject_id = str(metadata.get("subject_id") or "general")
    dimensions = [str(item) for item in metadata.get("coverage_keys") or [] if str(item).strip()]
    identity = "|".join([subject_id, dimensions[0] if dimensions else step.objective or step.description])
    slug = _slug(identity)
    if slug:
        return slug
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"gap_{digest}"


def step_gap_ids(step: PlanStep) -> list[str]:
    metadata = dict(step.metadata or {})
    values = [str(item) for item in metadata.get("gap_ids") or [] if str(item).strip()]
    return list(dict.fromkeys(values)) or [stable_gap_id(step)]


def stamp_initial_gap_metadata(plan: ExecutionPlan) -> ExecutionPlan:
    for index, step in enumerate(plan.steps):
        task_id = step.resolved_task_id(index)
        metadata = dict(step.metadata or {})
        metadata["gap_ids"] = step_gap_ids(step)
        metadata.setdefault("generation", 0)
        metadata.setdefault("origin_task_id", task_id)
        metadata.setdefault("supersedes_task_id", "")
        step.metadata = metadata
    return plan


def active_research_steps(plan: ExecutionPlan) -> list[tuple[int, PlanStep]]:
    return [
        (index, step)
        for index, step in enumerate(plan.steps)
        if step.step_type in {"research", "network_search", "file_read"}
        and not (isinstance(step.metadata, dict) and step.metadata.get("superseded"))
    ]


def normalize_gap_state(raw: Any, gap_id: str) -> GapState:
    value = dict(raw) if isinstance(raw, dict) else {}
    return GapState(
        gap_id=str(value.get("gap_id") or gap_id),
        dimension=str(value.get("dimension") or ""),
        subject_id=str(value.get("subject_id") or "general"),
        status=str(value.get("status") or "open"),
        reason_codes=[str(item) for item in value.get("reason_codes") or []],
        attempt_count=max(0, int(value.get("attempt_count") or 0)),
        recovery_generation=max(0, int(value.get("recovery_generation") or 0)),
        owner_task_ids=list(dict.fromkeys(str(item) for item in value.get("owner_task_ids") or [])),
    )


def normalize_business_gaps(raw: Any) -> dict[str, GapState]:
    if not isinstance(raw, dict):
        return {}
    return {str(gap_id): normalize_gap_state(value, str(gap_id)) for gap_id, value in raw.items()}


def sync_business_gaps(
    plan: ExecutionPlan,
    tasks: Any,
    previous: Any,
    *,
    resolved_gap_ids: list[str] | None = None,
) -> dict[str, GapState]:
    normalized_tasks = normalize_tasks(tasks)
    gaps = normalize_business_gaps(previous)
    resolved = set(resolved_gap_ids or [])

    for _index, step in active_research_steps(plan):
        metadata = dict(step.metadata or {})
        task_id = step.resolved_task_id(_index)
        dimensions = [str(item) for item in metadata.get("coverage_keys") or [] if str(item).strip()]
        for gap_id in step_gap_ids(step):
            gap = gaps.setdefault(
                gap_id,
                GapState(
                    gap_id=gap_id,
                    dimension=dimensions[0] if dimensions else "",
                    subject_id=str(metadata.get("subject_id") or "general"),
                    status="open",
                    reason_codes=[],
                    attempt_count=0,
                    recovery_generation=0,
                    owner_task_ids=[],
                ),
            )
            gap["dimension"] = gap["dimension"] or (dimensions[0] if dimensions else "")
            gap["subject_id"] = gap["subject_id"] or str(metadata.get("subject_id") or "general")
            if task_id not in gap["owner_task_ids"]:
                gap["owner_task_ids"].append(task_id)
            gap["recovery_generation"] = max(
                gap["recovery_generation"],
                max(0, int(metadata.get("generation") or 0)),
            )

    for gap_id, gap in gaps.items():
        current_plan_owners = {
            step.resolved_task_id(index)
            for index, step in active_research_steps(plan)
            if gap_id in step_gap_ids(step)
        }
        active_owners = [
            normalized_tasks[task_id]
            for task_id in sorted(current_plan_owners)
            if task_id in normalized_tasks
        ]
        if gap_id in resolved or not current_plan_owners:
            gap["status"] = "resolved"
            gap["reason_codes"] = list(dict.fromkeys([*gap["reason_codes"], "gap_closed"]))
            continue
        if all(
            task["execution_status"] == TaskExecutionStatus.SUCCEEDED.value
            and task["result_status"] == ResultStatus.COMPLETE.value
            for task in active_owners
        ) and active_owners:
            gap["status"] = "resolved"
            gap["reason_codes"] = list(dict.fromkeys([*gap["reason_codes"], "owner_complete"]))
            continue
        gap["status"] = "open"
        reasons: list[str] = []
        if any(task["execution_status"] == TaskExecutionStatus.FAILED.value for task in active_owners):
            reasons.append("owner_failed")
        if any(task["result_status"] == ResultStatus.PARTIAL.value for task in active_owners):
            reasons.append("owner_partial")
        if any(task["execution_status"] in {TaskExecutionStatus.PENDING.value, TaskExecutionStatus.RUNNING.value} for task in active_owners):
            reasons.append("owner_pending")
        gap["reason_codes"] = list(dict.fromkeys(reasons or ["owner_incomplete"]))
        gap["attempt_count"] = sum(
            max(0, int(normalized_tasks.get(task_id, {}).get("attempt") or 0))
            for task_id in gap["owner_task_ids"]
        )
    return gaps


def unresolved_gap_ids(gaps: Any) -> list[str]:
    return [
        gap_id
        for gap_id, gap in normalize_business_gaps(gaps).items()
        if gap["status"] != "resolved"
    ]


__all__ = [
    "GapState",
    "active_research_steps",
    "normalize_business_gaps",
    "normalize_gap_state",
    "stable_gap_id",
    "step_gap_ids",
    "stamp_initial_gap_metadata",
    "sync_business_gaps",
    "unresolved_gap_ids",
]

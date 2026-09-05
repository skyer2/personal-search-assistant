"""Task granularity guard for research workers.

The planner expresses intent, but the runtime enforces worker-sized tasks.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any

from app.agent.harness.state import PlanStep

MAX_ENTITIES_PER_TASK = 2
MAX_DIMENSIONS_PER_TASK = 4
MAX_EVIDENCE_CELLS_PER_TASK = 8


@dataclass(frozen=True)
class TaskComplexity:
    task_id: str
    entity_count: int
    dimension_count: int
    estimated_cells: int
    oversized: bool


def _brief_payload(brief: Any) -> dict[str, Any]:
    if isinstance(brief, dict):
        return brief
    if hasattr(brief, "to_dict"):
        value = brief.to_dict()
        return value if isinstance(value, dict) else {}
    return {}


def _entities(step: PlanStep, brief: dict[str, Any]) -> list[str]:
    metadata_entities = step.metadata.get("entities")
    candidates = [str(x).strip() for x in (metadata_entities or []) if str(x).strip()]
    if not candidates:
        candidates = [str(x).strip() for x in (brief.get("entities") or []) if str(x).strip()]
    if not candidates:
        candidates = [str(x).strip() for x in (step.metadata.get("entity") or []) if str(x).strip()]
    objective = step.objective or step.description
    mentioned = [entity for entity in candidates if entity and entity.lower() in objective.lower()]
    return mentioned or candidates[:1]


def _dimensions(step: PlanStep, brief: dict[str, Any]) -> list[str]:
    raw = step.metadata.get("coverage_keys")
    candidates = [str(x).strip() for x in (raw or []) if str(x).strip()]
    if not candidates:
        candidates = [str(x).strip() for x in (brief.get("dimensions") or []) if str(x).strip()]
    return candidates


def analyze_task_granularity(step: PlanStep, brief: Any = None) -> TaskComplexity:
    payload = _brief_payload(brief)
    entities = _entities(step, payload)
    dimensions = _dimensions(step, payload)
    entity_count = len(entities)
    dimension_count = len(dimensions)
    estimated_cells = max(entity_count, 1) * max(dimension_count, 1)
    oversized = (
        entity_count > MAX_ENTITIES_PER_TASK
        or dimension_count > MAX_DIMENSIONS_PER_TASK
        or estimated_cells > MAX_EVIDENCE_CELLS_PER_TASK
    )
    return TaskComplexity(
        task_id=step.task_id,
        entity_count=entity_count,
        dimension_count=dimension_count,
        estimated_cells=estimated_cells,
        oversized=oversized,
    )


def split_oversized_step(step: PlanStep, brief: Any = None) -> list[PlanStep]:
    complexity = analyze_task_granularity(step, brief)
    if not complexity.oversized:
        return [step]

    payload = _brief_payload(brief)
    entities = _entities(step, payload)
    dimensions = _dimensions(step, payload)
    if not entities:
        entities = [""]
    if not dimensions:
        dimensions = [""]

    entity_groups = [
        entities[index : index + MAX_ENTITIES_PER_TASK]
        for index in range(0, len(entities), MAX_ENTITIES_PER_TASK)
    ]
    dimension_groups = [
        dimensions[index : index + MAX_DIMENSIONS_PER_TASK]
        for index in range(0, len(dimensions), MAX_DIMENSIONS_PER_TASK)
    ]

    split_steps: list[PlanStep] = []
    source_id = step.task_id or ""
    for entity_index, entity_group in enumerate(entity_groups, start=1):
        for dimension_index, dimension_group in enumerate(dimension_groups, start=1):
            suffix = f"g{entity_index}_{dimension_index}"
            split = copy.deepcopy(step)
            split.task_id = f"{source_id}_{suffix}" if source_id else suffix
            entity_label = "、".join(x for x in entity_group if x)
            dimension_label = "、".join(x for x in dimension_group if x)
            objective_parts = [part for part in (entity_label, dimension_label) if part]
            objective = "：".join(objective_parts) if objective_parts else (step.objective or step.description)
            split.description = objective
            split.objective = objective
            split.metadata = dict(step.metadata or {})
            if entity_group and entity_group != [""]:
                split.metadata["entities"] = entity_group
            if dimension_group and dimension_group != [""]:
                split.metadata["coverage_keys"] = dimension_group
            split.metadata["granularity_split_from"] = source_id
            split.metadata["estimated_cells"] = len(entity_group) * len(dimension_group)
            split_steps.append(split)

    return split_steps


def normalize_plan_granularity(
    steps: list[PlanStep],
    brief: Any = None,
    *,
    max_research_tasks: int = 8,
) -> list[PlanStep]:
    """Split oversized research tasks while preserving DAG dependencies."""
    research_count = sum(1 for step in steps if step.step_type in {"research", "network_search", "file_read"})
    normalized: list[PlanStep] = []
    split_ids_by_source: dict[str, list[str]] = {}

    for step in steps:
        if step.step_type not in {"research", "network_search", "file_read"}:
            normalized.append(step)
            continue
        split = split_oversized_step(step, brief)
        source_id = step.task_id or ""
        if len(split) > 1 and source_id:
            split_ids_by_source[source_id] = [item.task_id for item in split]
        normalized.extend(split)

    expanded_research_count = sum(
        1 for step in normalized if step.step_type in {"research", "network_search", "file_read"}
    )
    if expanded_research_count > max_research_tasks and expanded_research_count > research_count:
        return steps

    for step in normalized:
        rewritten: list[str] = []
        for dependency in step.depends_on or []:
            replacements = split_ids_by_source.get(dependency)
            rewritten.extend(replacements if replacements else [dependency])
        if rewritten != list(step.depends_on or []):
            step.depends_on = rewritten
    return normalized


def desired_split_count(step: PlanStep, brief: Any = None) -> int:
    complexity = analyze_task_granularity(step, brief)
    if not complexity.oversized:
        return 1
    return math.ceil(complexity.entity_count / MAX_ENTITIES_PER_TASK) * math.ceil(
        complexity.dimension_count / MAX_DIMENSIONS_PER_TASK
    )

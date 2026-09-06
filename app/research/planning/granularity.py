"""Task granularity guard for research workers.

The planner expresses intent, but the runtime enforces worker-sized tasks.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from app.agent.harness.state import PlanStep

MAX_ENTITIES_PER_TASK = 2
MAX_DIMENSIONS_PER_TASK = 4
MAX_EVIDENCE_CELLS_PER_TASK = 8
MAX_DISCOVERY_ITEMS_PER_TASK = 4

TaskKind = Literal["discovery", "deep_dive", "verification", "gap_fill"]

_DISCOVERY_MARKERS = (
    "候选",
    "发现",
    "landscape",
    "全景",
    "扫描",
    "候选池",
    "值得加入",
)
_TARGET_ITEM_PATTERN = re.compile(r"([0-9]+)\s*(?:~|～|到|-|—)?\s*[0-9]*\s*(家|个|条|项|家公司|公司)")


@dataclass(frozen=True)
class TaskComplexity:
    task_id: str
    entity_count: int
    dimension_count: int
    estimated_cells: int
    oversized: bool
    task_kind: TaskKind = "deep_dive"
    target_items: int = 1


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
        subjects = brief.get("subjects") or []
        for subject in subjects:
            if not isinstance(subject, dict):
                continue
            canonical = str(subject.get("canonical") or "").strip()
            if canonical:
                candidates.append(canonical)
            candidates.extend(
                str(alias).strip()
                for alias in (subject.get("aliases") or [])
                if str(alias).strip()
            )
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


def _task_kind(step: PlanStep) -> TaskKind:
    metadata_kind = str((step.metadata or {}).get("task_kind") or "").strip().lower()
    if metadata_kind == "landscape_discovery":
        metadata_kind = "discovery"
    if metadata_kind in {"discovery", "deep_dive", "verification", "gap_fill"}:
        return metadata_kind  # type: ignore[return-value]
    objective = f"{step.objective or ''} {step.description or ''}".lower()
    if any(marker in objective for marker in _DISCOVERY_MARKERS):
        return "discovery"
    return "deep_dive"


def _target_items(step: PlanStep, brief: dict[str, Any]) -> int:
    raw = (step.metadata or {}).get("target_items")
    try:
        if raw is not None:
            return max(1, min(int(raw), 50))
    except (TypeError, ValueError):
        pass
    try:
        brief_count = int(brief.get("item_count") or brief.get("target_items") or 0)
        if brief_count > 0:
            return max(1, min(brief_count, 50))
    except (TypeError, ValueError):
        pass
    objective = f"{step.objective or ''} {step.description or ''}"
    match = _TARGET_ITEM_PATTERN.search(objective)
    if match:
        return max(1, min(int(match.group(1)), 50))
    return MAX_DISCOVERY_ITEMS_PER_TASK if _task_kind(step) == "discovery" else 1


def analyze_task_granularity(step: PlanStep, brief: Any = None) -> TaskComplexity:
    payload = _brief_payload(brief)
    task_kind = _task_kind(step)
    entities = _entities(step, payload)
    dimensions = _dimensions(step, payload)
    if task_kind == "discovery" and not dimensions:
        dimensions = ["赛道", "融资", "潜力信号"]
    target_items = _target_items(step, payload)
    entity_count = len(entities)
    dimension_count = len(dimensions)
    if task_kind == "discovery":
        entity_count = target_items
        estimated_cells = target_items * max(dimension_count, 1)
        oversized = estimated_cells > MAX_EVIDENCE_CELLS_PER_TASK
    else:
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
        task_kind=task_kind,
        target_items=target_items,
    )


def _split_discovery_step(step: PlanStep, brief: dict[str, Any]) -> list[PlanStep]:
    complexity = analyze_task_granularity(step, brief)
    if not complexity.oversized:
        return [step]

    dimensions = _dimensions(step, brief) or ["赛道", "融资", "潜力信号"]
    max_dimensions = max(1, MAX_EVIDENCE_CELLS_PER_TASK // max(1, complexity.target_items))
    groups = [
        dimensions[index : index + max_dimensions]
        for index in range(0, len(dimensions), max_dimensions)
    ]
    source_id = step.task_id or ""
    split_steps: list[PlanStep] = []
    lane_index = 0
    for group in groups:
        item_group_size = max(1, MAX_EVIDENCE_CELLS_PER_TASK // max(1, len(group)))
        item_starts = list(range(1, complexity.target_items + 1, item_group_size))
        for item_start in item_starts:
            item_end = min(complexity.target_items, item_start + item_group_size - 1)
            item_count = item_end - item_start + 1
            lane_index += 1
            split = copy.deepcopy(step)
            split.task_id = (
                f"{source_id}_d{lane_index}" if source_id else f"discovery_d{lane_index}"
            )
            split.metadata = dict(step.metadata or {})
            split.metadata["task_kind"] = "discovery"
            split.metadata["target_items"] = item_count
            split.metadata["target_item_range"] = [item_start, item_end]
            split.metadata["coverage_keys"] = group
            split.metadata["estimated_cells"] = item_count * len(group)
            split.metadata["granularity_split_from"] = source_id
            split.metadata["produces_artifact"] = "candidate_set"
            lane = "、".join(group)
            split.objective = (
                f"Discovery lane {lane_index}（候选 {item_start}-{item_end} / {lane}）："
                f"发现第 {item_start}-{item_end} 个候选并输出候选集"
            )
            split.description = split.objective
            split_steps.append(split)
    return split_steps


def split_oversized_step(step: PlanStep, brief: Any = None) -> list[PlanStep]:
    complexity = analyze_task_granularity(step, brief)
    if not complexity.oversized:
        return [step]

    payload = _brief_payload(brief)
    if complexity.task_kind == "discovery":
        return _split_discovery_step(step, payload)

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
    if complexity.task_kind == "discovery":
        return max(1, math.ceil(complexity.estimated_cells / MAX_EVIDENCE_CELLS_PER_TASK))
    return math.ceil(complexity.entity_count / MAX_ENTITIES_PER_TASK) * math.ceil(
        complexity.dimension_count / MAX_DIMENSIONS_PER_TASK
    )

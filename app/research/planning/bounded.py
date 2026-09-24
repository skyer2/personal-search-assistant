"""Bounded planner contract (v1).

The existing planning models remain source-compatible, while this module gives
the runtime a small deterministic guard: each worker handles at most two
entities, two dimensions and seven queries (70% of the default ten-query
worker budget).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_ENTITIES = 2
MAX_DIMENSIONS = 2
DEFAULT_WORKER_QUERY_BUDGET = 10
MAX_ESTIMATED_QUERIES = 7


@dataclass(frozen=True)
class PlanIssue:
    code: str
    task_id: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "task_id": self.task_id, "detail": self.detail}


def _values(task: Any, key: str) -> list[str]:
    metadata = getattr(task, "metadata", None)
    if isinstance(task, dict):
        metadata = task.get("metadata") or {}
    metadata = metadata if isinstance(metadata, dict) else {}
    raw = metadata.get(key)
    if raw is None and key == "dimensions":
        raw = metadata.get("coverage_keys")
    if raw is None and key == "queries":
        raw = metadata.get("estimated_queries")
    if raw is None and key == "entities":
        raw = metadata.get("entities")
    if isinstance(raw, (list, tuple, set)):
        return [str(value).strip() for value in raw if str(value).strip()]
    if raw is None:
        return []
    return [str(raw).strip()] if str(raw).strip() else []


def _task_id(task: Any) -> str:
    return str((task.get("task_id") if isinstance(task, dict) else getattr(task, "task_id", "")) or "")


def estimated_queries(task: Any, default: int = 1) -> int:
    metadata = getattr(task, "metadata", None)
    if isinstance(task, dict):
        metadata = task.get("metadata") or {}
    metadata = metadata if isinstance(metadata, dict) else {}
    raw = metadata.get("estimated_queries")
    if raw is None:
        raw = getattr(task, "estimated_queries", None) if not isinstance(task, dict) else task.get("estimated_queries")
    try:
        return max(0, int(raw)) if raw is not None else max(0, int(default))
    except (TypeError, ValueError):
        return max(0, int(default))


def validate_task(task: Any, *, query_budget: int = DEFAULT_WORKER_QUERY_BUDGET) -> list[PlanIssue]:
    entities = _values(task, "entities")
    dimensions = _values(task, "dimensions")
    issues: list[PlanIssue] = []
    task_id = _task_id(task)
    if len(entities) > MAX_ENTITIES:
        issues.append(PlanIssue("entities_limit", task_id, f"{len(entities)}>{MAX_ENTITIES}"))
    if len(dimensions) > MAX_DIMENSIONS:
        issues.append(PlanIssue("dimensions_limit", task_id, f"{len(dimensions)}>{MAX_DIMENSIONS}"))
    allowed = max(1, int(query_budget * 0.7))
    if estimated_queries(task) > allowed:
        issues.append(PlanIssue("estimated_queries_limit", task_id, f"{estimated_queries(task)}>{allowed}"))
    return issues


def validate_plan(tasks: list[Any], *, query_budget: int = DEFAULT_WORKER_QUERY_BUDGET) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    for task in tasks:
        issues.extend(validate_task(task, query_budget=query_budget))
    return issues


def split_task(task: Any, *, query_budget: int = DEFAULT_WORKER_QUERY_BUDGET) -> list[Any]:
    """Return bounded copies of a task; objects with ``metadata`` are copied safely."""
    issues = validate_task(task, query_budget=query_budget)
    if not issues:
        return [task]
    import copy

    entities = _values(task, "entities") or [""]
    dimensions = _values(task, "dimensions") or [""]
    chunks: list[tuple[list[str], list[str]]] = []
    for start in range(0, len(entities), MAX_ENTITIES):
        for dstart in range(0, len(dimensions), MAX_DIMENSIONS):
            chunks.append((entities[start : start + MAX_ENTITIES], dimensions[dstart : dstart + MAX_DIMENSIONS]))
    if not chunks:
        chunks = [([], [])]
    output: list[Any] = []
    for index, (entity_group, dimension_group) in enumerate(chunks, 1):
        item = copy.deepcopy(task)
        metadata = getattr(item, "metadata", None)
        if isinstance(item, dict):
            metadata = dict(item.get("metadata") or {})
            item["metadata"] = metadata
        else:
            metadata = dict(metadata or {})
            item.metadata = metadata
        metadata["entities"] = entity_group if entity_group != [""] else []
        metadata["coverage_keys"] = dimension_group if dimension_group != [""] else []
        metadata["dimensions"] = dimension_group if dimension_group != [""] else []
        metadata["all_dimensions"] = dimension_group if dimension_group != [""] else []
        metadata["estimated_queries"] = min(max(1, estimated_queries(task, 1)), max(1, int(query_budget * 0.7)))
        metadata["bounded_split_from"] = _task_id(task)
        bounded_id = _task_id(task) if index == 1 else f"{_task_id(task)}_b{index}"
        if isinstance(item, dict):
            item["task_id"] = bounded_id
        else:
            item.task_id = bounded_id
        output.append(item)
    return output


__all__ = [
    "DEFAULT_WORKER_QUERY_BUDGET", "MAX_DIMENSIONS", "MAX_ENTITIES", "MAX_ESTIMATED_QUERIES",
    "PlanIssue", "estimated_queries", "split_task", "validate_plan", "validate_task",
]

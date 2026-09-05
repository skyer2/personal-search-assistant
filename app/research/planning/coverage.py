"""Evidence-oriented coverage matrix for research progress."""

from __future__ import annotations

from typing import Any

from app.agent.harness.state import ExecutionPlan


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("payload")
    return value if isinstance(value, dict) else {}


def _entities(step: Any, brief: Any) -> list[str]:
    metadata = getattr(step, "metadata", None) or {}
    raw = metadata.get("entities")
    entities = [str(x).strip() for x in (raw or []) if str(x).strip()]
    if entities:
        return entities
    objective = str(getattr(step, "objective", "") or getattr(step, "description", "")).lower()
    brief_entities = [
        str(x).strip()
        for x in (getattr(brief, "entities", None) or [])
        if str(x).strip()
    ]
    mentioned = [x for x in brief_entities if x.lower() in objective]
    return mentioned or ["general"]


def _dimensions(step: Any, brief: Any) -> list[str]:
    metadata = getattr(step, "metadata", None) or {}
    raw = metadata.get("coverage_keys")
    dimensions = [str(x).strip() for x in (raw or []) if str(x).strip()]
    if dimensions:
        return dimensions
    return [
        str(x).strip()
        for x in (getattr(brief, "dimensions", None) or [])
        if str(x).strip()
    ][:4] or ["关键事实"]


def _dimension_matches(finding: dict[str, Any], dimension: str) -> bool:
    explicit = str(finding.get("dimension") or "").strip()
    if explicit and explicit == dimension:
        return True
    text = " ".join(
        str(x)
        for x in [
            finding.get("summary"),
            finding.get("title"),
            *(finding.get("facts") or []),
        ]
        if x
    ).lower()
    aliases = {
        "融资": ("融资", "融资轮", "估值", "funding", "financing", "series"),
        "招聘": ("招聘", "岗位", "hiring", "jobs"),
        "技术": ("技术", "产品", "模型", "technology", "product"),
        "商业化": ("商业化", "收入", "客户", "commercial", "revenue"),
        "团队": ("团队", "创始", "高管", "team", "founder"),
        "风险": ("风险", "合规", "监管", "risk", "compliance"),
    }.get(dimension, (dimension.lower(),))
    return any(alias.lower() in text for alias in aliases)


def build_coverage_matrix(
    plan: ExecutionPlan | None,
    worker_results: list[Any] | None,
    *,
    brief: Any = None,
) -> dict[str, Any]:
    rows = {
        str(row.get("task_id") or ""): row
        for row in (worker_results or [])
        if isinstance(row, dict) and str(row.get("task_id") or "")
    }
    cells: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    if plan is None:
        return {
            "cells": [],
            "coverage_ratio": 0.0,
            "covered_count": 0,
            "partial_count": 0,
            "missing_count": 0,
        }

    for index, step in enumerate(plan.steps):
        if str(getattr(step, "step_type", "") or "") not in {
            "research",
            "network_search",
            "file_read",
        }:
            continue
        task_id = str(step.resolved_task_id(index))
        row = rows.get(task_id, {})
        payload = _payload(row)
        explicit_dimensions = bool((getattr(step, "metadata", None) or {}).get("coverage_keys"))
        findings = [item for item in (payload.get("findings") or []) if isinstance(item, dict)]
        evidence_ids = [str(x) for x in (payload.get("evidence_ids") or []) if str(x).strip()]
        sources = [str(x) for x in (payload.get("sources") or []) if str(x).strip()]
        try:
            confidence = float(payload.get("confidence") or (1.0 if row.get("ok") else 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        for entity in _entities(step, brief):
            for dimension in _dimensions(step, brief):
                key = (entity, dimension)
                if key in seen:
                    continue
                seen.add(key)
                cell_findings = [
                    finding
                    for finding in findings
                    if _dimension_matches(finding, dimension)
                ]
                if cell_findings and evidence_ids:
                    status = "covered"
                elif cell_findings or (
                    not explicit_dimensions and (sources or evidence_ids)
                ):
                    status = "partial"
                else:
                    status = "missing"
                cells.append(
                    {
                        "entity": entity,
                        "dimension": dimension,
                        "status": status,
                        "task_id": task_id,
                        "evidence_ids": evidence_ids[:8],
                        "finding_ids": [
                            str(item.get("finding_id") or "")
                            for item in findings[:8]
                            if str(item.get("finding_id") or "").strip()
                        ],
                        "confidence": round(confidence, 3),
                    }
                )

    covered = sum(1 for cell in cells if cell["status"] == "covered")
    partial = sum(1 for cell in cells if cell["status"] == "partial")
    missing = sum(1 for cell in cells if cell["status"] == "missing")
    total = len(cells)
    ratio = round((covered + 0.5 * partial) / total, 3) if total else 0.0
    return {
        "cells": cells,
        "coverage_ratio": ratio,
        "covered_count": covered,
        "partial_count": partial,
        "missing_count": missing,
    }


def coverage_gap_items(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = []
    for cell in matrix.get("cells") or []:
        if not isinstance(cell, dict) or cell.get("status") != "missing":
            continue
        entity = str(cell.get("entity") or "general")
        dimension = str(cell.get("dimension") or "关键事实")
        gaps.append(
            {
                "type": "coverage_gap",
                "entity": entity,
                "dimension": dimension,
                "description": f"{entity}：{dimension} 缺少可用证据",
                "blocking": True,
                "actionable": True,
                "severity": "high" if dimension in {"风险", "商业化", "融资"} else "medium",
            }
        )
    return gaps

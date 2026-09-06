"""CandidateSet dependency model for degraded discovery execution."""

from __future__ import annotations

import re
from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep


_DISCOVERY_MARKERS = ("候选", "发现", "landscape", "全景", "扫描", "候选池", "值得加入")
_DEEP_DIVE_MARKERS = ("深挖", "单家", "单公司", "专项分析")
_ITEM_SPLIT = re.compile(r"[、,，;；\n]")


def _is_discovery(step: PlanStep) -> bool:
    if str((step.metadata or {}).get("task_kind") or "") in {
        "discovery",
        "landscape_discovery",
    }:
        return True
    if str((step.metadata or {}).get("produces_artifact") or "") == "candidate_set":
        return True
    objective = f"{step.objective or ''} {step.description or ''}".lower()
    if any(marker in objective for marker in _DEEP_DIVE_MARKERS):
        return False
    return any(marker in objective for marker in _DISCOVERY_MARKERS)


def annotate_candidate_dependencies(plan: ExecutionPlan) -> ExecutionPlan:
    """Convert discovery task dependencies into CandidateSet artifact dependencies."""
    explicit_discovery_ids = {
        step.task_id
        for step in plan.steps
        if step.task_id
        and (
            str((step.metadata or {}).get("task_kind") or "")
            in {"discovery", "landscape_discovery"}
            or bool((step.metadata or {}).get("produces_artifact"))
        )
    }
    heuristic_discovery_ids = {
        step.task_id
        for step in plan.steps
        if step.task_id
        and not step.depends_on
        and _is_discovery(step)
        and str((step.metadata or {}).get("task_kind") or "") != "deep_dive"
        and not (step.metadata or {}).get("requires_artifacts")
    }
    heuristic_discovery_ids -= {
        step.task_id
        for step in plan.steps
        if set(step.depends_on or []) & explicit_discovery_ids
    }
    discovery_ids = explicit_discovery_ids | heuristic_discovery_ids
    if not discovery_ids:
        return plan
    for step in plan.steps:
        meta = dict(step.metadata or {})
        if step.task_id in discovery_ids:
            meta["task_kind"] = "discovery"
            meta["produces_artifact"] = "candidate_set"
            step.metadata = meta
            continue
        if step.step_type not in {"research", "network_search", "file_read"}:
            continue
        deps = [dep for dep in (step.depends_on or []) if dep not in discovery_ids]
        step.depends_on = deps
        required = set(meta.get("requires_artifacts") or [])
        required.add("candidate_set")
        meta["requires_artifacts"] = sorted(required)
        meta.setdefault("task_kind", "deep_dive")
        step.metadata = meta
    promote_artifact_producers(plan)
    return plan


def promote_artifact_producers(plan: ExecutionPlan) -> ExecutionPlan:
    """A required artifact consumer must not wait on an optional producer."""
    producers: dict[str, list[PlanStep]] = {}
    required_artifacts: set[str] = set()
    for step in plan.steps:
        meta = step.metadata if isinstance(step.metadata, dict) else {}
        artifact = str(meta.get("produces_artifact") or "")
        if artifact:
            producers.setdefault(artifact, []).append(step)
        if not bool(meta.get("optional")):
            required_artifacts.update(str(x) for x in meta.get("requires_artifacts") or [])

    for artifact in required_artifacts:
        for producer in producers.get(artifact, []):
            meta = dict(producer.metadata or {})
            if bool(meta.get("required")) and not bool(meta.get("optional")):
                continue
            meta["required"] = True
            meta["optional"] = False
            meta["priority"] = 0
            meta["promoted_for_artifact"] = artifact
            producer.metadata = meta
    return plan


def _candidate_items_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        fragments: list[str] = []
        fragments.extend(str(item) for item in payload.get("facts") or [])
        fragments.extend(
            str(item)
            for finding in payload.get("findings") or []
            if isinstance(finding, dict)
            for item in finding.get("facts") or []
        )
        fragments.extend(
            str(item.get("summary") or "")
            for item in payload.get("findings") or []
            if isinstance(item, dict)
        )
        fragments.append(str(payload.get("summary") or ""))
        for fragment in fragments:
            for part in _ITEM_SPLIT.split(fragment):
                item = part.strip(" \t\r\n.。；;：:")
                if 2 <= len(item) <= 120 and item.lower() not in seen:
                    seen.add(item.lower())
                    items.append(item)
                if len(items) >= 16:
                    return items
    return items


def build_candidate_set(
    plan: ExecutionPlan,
    *,
    worker_rows: list[dict[str, Any]],
    task_status: dict[str, str],
    query: str = "",
    brief: Any = None,
) -> dict[str, Any]:
    discovery_steps = [step for step in plan.steps if _is_discovery(step)]
    discovery_ids = {step.task_id for step in discovery_steps}
    discovery_terminal = {
        task_id: task_status.get(task_id or "", "pending")
        in {"done", "failed", "skipped"}
        for task_id in discovery_ids
    }
    if discovery_ids and not all(discovery_terminal.values()):
        return {
            "available": False,
            "status": "pending",
            "source_task_ids": sorted(tid for tid in discovery_ids if tid),
            "items": [],
            "context": "",
            "query": query,
            "fallback": False,
        }
    rows = [
        row
        for row in worker_rows
        if str(row.get("task_id") or "") in discovery_ids
    ]
    items = _candidate_items_from_rows(rows)
    any_done = any(
        task_status.get(step.task_id or "", "") == "done" for step in discovery_steps
    )
    any_failed = any(
        task_status.get(step.task_id or "", "") == "failed" for step in discovery_steps
    )
    if items and any_done:
        status = "complete"
    elif items:
        status = "partial"
    else:
        status = "fallback"

    brief_payload = brief if isinstance(brief, dict) else {}
    if not items:
        items = [
            str(item)
            for item in brief_payload.get("entities") or []
            if str(item).strip() and not str(item).strip().lower().startswith("国内")
        ][:12]

    if items:
        context = "CandidateSet（%s）：\n- %s" % (status, "\n- ".join(items[:12]))
    else:
        context = (
            "CandidateSet fallback：Discovery 未产出结构化候选。"
            "请先基于研究目标自行识别少量高潜力候选，再继续完成本任务。"
        )
    return {
        "available": True,
        "status": status,
        "source_task_ids": sorted(tid for tid in discovery_ids if tid),
        "items": items[:12],
        "context": context,
        "query": query,
        "fallback": status == "fallback",
    }


def candidate_artifact_status(candidate_set: Any) -> dict[str, str]:
    if not isinstance(candidate_set, dict) or not candidate_set.get("available"):
        return {}
    return {"artifact:candidate_set": "available"}


def objective_with_candidate_context(objective: str, candidate_set: Any) -> str:
    if not isinstance(candidate_set, dict) or not candidate_set.get("available"):
        return objective
    context = str(candidate_set.get("context") or "").strip()
    if not context:
        return objective
    return f"{objective}\n\n{context}"

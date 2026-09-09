"""CandidateSet dependency model for degraded discovery execution."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep


_DISCOVERY_MARKERS = ("候选", "发现", "landscape", "全景", "扫描", "候选池", "值得加入")
_DEEP_DIVE_MARKERS = ("深挖", "单家", "单公司", "专项分析")
_ITEM_SPLIT = re.compile(r"[、,，;；\n]")


@dataclass
class Candidate:
    candidate_id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    score: float = 0.0
    selection_reason: str = ""
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Candidate":
        row = data or {}
        name = str(row.get("name") or row.get("candidate_id") or "")
        return cls(
            candidate_id=str(row.get("candidate_id") or f"candidate_{abs(hash(name)) % 10_000_000}"),
            name=name,
            aliases=[str(item) for item in row.get("aliases") or []],
            score=max(0.0, min(1.0, float(row.get("score") or 0.0))),
            selection_reason=str(row.get("selection_reason") or ""),
            evidence_ids=[str(item) for item in row.get("evidence_ids") or []],
        )


@dataclass
class CandidateSet:
    candidate_set_id: str
    status: str
    candidates: list[Candidate] = field(default_factory=list)
    source_task_ids: list[str] = field(default_factory=list)
    expanded: bool = False
    expanded_plan_version: int | None = None
    context: str = ""
    query: str = ""
    fallback: bool = False

    @property
    def available(self) -> bool:
        return bool(self.candidates)

    @property
    def items(self) -> list[str]:
        return [candidate.name for candidate in self.candidates]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_set_id": self.candidate_set_id,
            "status": self.status,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "source_task_ids": list(self.source_task_ids),
            "expanded": self.expanded,
            "expanded_plan_version": self.expanded_plan_version,
            "available": self.available,
            "items": self.items,
            "context": self.context,
            "query": self.query,
            "fallback": self.fallback,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CandidateSet":
        row = data or {}
        raw_candidates = list(row.get("candidates") or [])
        if not raw_candidates:
            raw_candidates = [
                {"candidate_id": item, "name": item}
                for item in row.get("items") or []
                if isinstance(item, str) and item.strip()
            ]
        candidates = [
            Candidate.from_dict(item if isinstance(item, dict) else {"name": str(item)})
            for item in raw_candidates
        ]
        expanded_version = row.get("expanded_plan_version")
        return cls(
            candidate_set_id=str(row.get("candidate_set_id") or "candidate_set_default"),
            status=str(row.get("status") or ("fallback" if not candidates else "complete")),
            candidates=candidates,
            source_task_ids=[str(item) for item in row.get("source_task_ids") or []],
            expanded=bool(row.get("expanded")),
            expanded_plan_version=int(expanded_version) if expanded_version is not None else None,
            context=str(row.get("context") or ""),
            query=str(row.get("query") or ""),
            fallback=bool(row.get("fallback")),
        )


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
        structured = [
            str(item.get("name") if isinstance(item, dict) else item)
            for item in payload.get("candidates") or []
            if item
        ]
        if structured:
            return structured[:16]
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
        in {"done", "failed", "skipped", "succeeded", "stopped", "superseded"}
        for task_id in discovery_ids
    }
    if discovery_ids and not all(discovery_terminal.values()):
        return CandidateSet(
            candidate_set_id="candidate_set_pending",
            status="pending",
            source_task_ids=sorted(tid for tid in discovery_ids if tid),
            query=query,
        ).to_dict()
    rows = [
        row
        for row in worker_rows
        if str(row.get("task_id") or "") in discovery_ids
    ]
    items = _candidate_items_from_rows(rows)
    any_done = any(
        task_status.get(step.task_id or "", "") in {"done", "succeeded"}
        for step in discovery_steps
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

    if not items:
        brief_payload = brief if isinstance(brief, dict) else {}
        items = [
            str(item)
            for item in brief_payload.get("entities") or []
            if str(item).strip() and not str(item).strip().lower().startswith("国内")
        ][:12]

    context = (
        "CandidateSet（%s）：\n- %s" % (status, "\n- ".join(items[:12]))
        if items
        else "CandidateSet fallback：Discovery 未产出结构化候选。"
    )
    return CandidateSet(
        candidate_set_id="candidate_set_primary",
        status=status,
        candidates=[Candidate(candidate_id=item, name=item) for item in items[:12]],
        source_task_ids=sorted(tid for tid in discovery_ids if tid),
        context=context,
        query=query,
        fallback=status == "fallback",
    ).to_dict()


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

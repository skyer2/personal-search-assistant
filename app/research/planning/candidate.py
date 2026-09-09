"""CandidateSet dependency model for degraded discovery execution."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep


_DISCOVERY_MARKERS = ("候选", "发现", "landscape", "全景", "扫描", "候选池", "值得加入")
_DEEP_DIVE_MARKERS = ("深挖", "单家", "单公司", "专项分析")
_MIN_CANDIDATE_CONFIDENCE = 0.50


def _normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).strip().split()).casefold()


def stable_candidate_id(name: str) -> str:
    """Return a process-independent identity for a human-readable name."""
    normalized = _normalized_name(name)
    if not normalized:
        return ""
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"cand_{digest}"


@dataclass
class Candidate:
    name: str
    candidate_id: str = ""
    aliases: list[str] = field(default_factory=list)
    score: float = 0.0
    confidence: float = 0.0
    selection_reason: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    source_task_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("candidate_name_empty")
        if not self.candidate_id:
            self.candidate_id = stable_candidate_id(self.name)
        self.candidate_id = self.candidate_id or stable_candidate_id(self.name)
        self.confidence = self.confidence or self.score

    @property
    def admitted(self) -> bool:
        return bool(self.evidence_ids) and self.confidence >= _MIN_CANDIDATE_CONFIDENCE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Candidate":
        row = data or {}
        name = str(row.get("name") or row.get("candidate_id") or "")
        return cls(
            name=name,
            candidate_id=str(row.get("candidate_id") or ""),
            aliases=[str(item) for item in row.get("aliases") or []],
            score=max(0.0, min(1.0, float(row.get("score") or 0.0))),
            confidence=max(0.0, min(1.0, float(row.get("confidence") or row.get("score") or 0.0))),
            selection_reason=str(row.get("selection_reason") or ""),
            evidence_ids=[str(item) for item in row.get("evidence_ids") or []],
            source_task_ids=[str(item) for item in row.get("source_task_ids") or []],
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
        return any(candidate.admitted for candidate in self.candidates)

    @property
    def admitted_candidates(self) -> list[Candidate]:
        return [candidate for candidate in self.candidates if candidate.admitted]

    @property
    def items(self) -> list[str]:
        return [candidate.name for candidate in self.admitted_candidates]

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
            "admitted_count": len(self.admitted_candidates),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CandidateSet":
        row = data or {}
        raw_candidates = list(row.get("candidates") or [])
        candidates = [
            Candidate.from_dict(item if isinstance(item, dict) else {"name": str(item)})
            for item in raw_candidates
        ]
        expanded_version = row.get("expanded_plan_version")
        return cls(
            candidate_set_id=str(row.get("candidate_set_id") or "candidate_set_default"),
            status=str(row.get("status") or ("degraded" if not candidates else "partial")),
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


def _structured_candidates(rows: list[dict[str, Any]]) -> list[Candidate]:
    candidates: list[Candidate] = []
    by_id: dict[str, Candidate] = {}
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        task_id = str(row.get("task_id") or "")
        row_evidence = [str(item) for item in payload.get("evidence_ids") or [] if str(item).strip()]
        row_confidence = float(payload.get("confidence") or 0.0)
        for raw in payload.get("candidates") or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            evidence_ids = [
                str(item)
                for item in raw.get("evidence_ids") or row_evidence
                if str(item).strip()
            ]
            confidence = float(raw.get("confidence") or row_confidence or 0.0)
            candidate = Candidate(
                name=name,
                candidate_id=str(raw.get("candidate_id") or ""),
                aliases=[str(item) for item in raw.get("aliases") or []],
                confidence=confidence,
                selection_reason=str(raw.get("selection_reason") or ""),
                evidence_ids=evidence_ids,
                source_task_ids=[task_id],
            )
            existing = by_id.get(candidate.candidate_id)
            if existing is None:
                by_id[candidate.candidate_id] = candidate
                candidates.append(candidate)
                continue
            existing.aliases = list(dict.fromkeys([*existing.aliases, *candidate.aliases]))
            existing.evidence_ids = list(dict.fromkeys([*existing.evidence_ids, *candidate.evidence_ids]))
            existing.source_task_ids = list(dict.fromkeys([*existing.source_task_ids, *candidate.source_task_ids]))
            existing.confidence = max(existing.confidence, candidate.confidence)
            if not existing.selection_reason:
                existing.selection_reason = candidate.selection_reason
        if len(candidates) >= 16:
            break
    return candidates[:16]


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
    raw_candidates = _structured_candidates(rows)
    candidates = [candidate for candidate in raw_candidates if candidate.admitted]
    any_done = any(
        task_status.get(step.task_id or "", "") in {"done", "succeeded"}
        for step in discovery_steps
    )
    if candidates and len(candidates) == len(raw_candidates):
        status = "complete"
    elif candidates:
        status = "partial"
    else:
        status = "degraded"

    context = (
        "CandidateSet（%s）：\n- %s"
        % (status, "\n- ".join(candidate.name for candidate in candidates[:12]))
        if candidates
        else "CandidateSet degraded：Discovery 未产出通过证据准入的结构化候选。"
    )
    return CandidateSet(
        candidate_set_id="candidate_set_primary",
        status=status,
        candidates=candidates[:12],
        source_task_ids=sorted(tid for tid in discovery_ids if tid),
        context=context,
        query=query,
        fallback=not candidates,
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

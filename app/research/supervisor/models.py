"""Supervisor action contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ResearchTaskRequest:
    objective: str
    target_criteria: tuple[str, ...] = ()
    target_gaps: tuple[str, ...] = ()
    criterion_id: str = ""
    question_id: str = ""
    hypothesis_id: str = ""
    gap_id: str = ""
    missing_evidence_types: tuple[str, ...] = ()
    blocking_conflict_ids: tuple[str, ...] = ()
    priority: str = "normal"
    expected_evidence: tuple[str, ...] = ()
    source_hints: tuple[str, ...] = ()
    novelty_reason: str = ""
    estimated_effort: str = "medium"
    task_id: str = ""
    repair: bool = False
    max_queries: int = 7
    max_fetches: int = 7
    max_llm_calls: int = 4

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ResearchTaskRequest":
        row = data or {}
        expected = row.get("expected_evidence")
        if isinstance(expected, str):
            expected = [expected]
        return cls(
            objective=str(row.get("objective") or ""),
            target_criteria=tuple(str(item) for item in row.get("target_criteria") or []),
            target_gaps=tuple(str(item) for item in row.get("target_gaps") or []),
            criterion_id=str(row.get("criterion_id") or ""),
            question_id=str(row.get("question_id") or ""),
            hypothesis_id=str(row.get("hypothesis_id") or ""),
            gap_id=str(row.get("gap_id") or ""),
            missing_evidence_types=tuple(
                str(item) for item in row.get("missing_evidence_types") or []
            ),
            blocking_conflict_ids=tuple(
                str(item) for item in row.get("blocking_conflict_ids") or []
            ),
            priority=str(row.get("priority") or "normal"),
            expected_evidence=tuple(str(item) for item in expected or []),
            source_hints=tuple(str(item) for item in row.get("source_hints") or []),
            novelty_reason=str(row.get("novelty_reason") or ""),
            estimated_effort=str(row.get("estimated_effort") or "medium"),
            task_id=str(row.get("task_id") or ""),
            repair=bool(row.get("repair")),
            max_queries=max(1, min(7, int(row.get("max_queries") or 7))),
            max_fetches=max(1, min(7, int(row.get("max_fetches") or 7))),
            max_llm_calls=max(1, min(8, int(row.get("max_llm_calls") or 4))),
        )


@dataclass(frozen=True)
class SupervisorAction:
    action: Literal["CONDUCT_RESEARCH", "COMPLETE"]
    reason: str
    research_tasks: tuple[ResearchTaskRequest, ...] = field(default_factory=tuple)
    source: str = "deterministic_fallback"

    def to_dict(self) -> dict[str, Any]:
        semantic_action = "TARGETED_RESEARCH" if self.action == "CONDUCT_RESEARCH" else "SYNTHESIZE"
        return {
            "action": self.action,
            "semantic_action": semantic_action,
            "runtime_action": self.action,
            "reason": self.reason,
            "research_tasks": [item.to_dict() for item in self.research_tasks],
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SupervisorAction":
        row = data or {}
        action = str(row.get("action") or "CONDUCT_RESEARCH")
        if action not in {"CONDUCT_RESEARCH", "COMPLETE"}:
            action = "CONDUCT_RESEARCH"
        return cls(
            action=action,  # type: ignore[arg-type]
            reason=str(row.get("reason") or ""),
            research_tasks=tuple(
                ResearchTaskRequest.from_dict(item)
                for item in row.get("research_tasks") or []
                if isinstance(item, dict)
            ),
            source=str(row.get("source") or "deterministic_fallback"),
        )


__all__ = ["ResearchTaskRequest", "SupervisorAction"]

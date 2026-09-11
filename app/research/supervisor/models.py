"""Supervisor action contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ResearchTaskRequest:
    objective: str
    target_criteria: tuple[str, ...] = ()
    target_gaps: tuple[str, ...] = ()
    priority: str = "normal"
    expected_evidence: tuple[str, ...] = ()
    source_hints: tuple[str, ...] = ()
    novelty_reason: str = ""
    estimated_effort: str = "medium"
    task_id: str = ""

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
            priority=str(row.get("priority") or "normal"),
            expected_evidence=tuple(str(item) for item in expected or []),
            source_hints=tuple(str(item) for item in row.get("source_hints") or []),
            novelty_reason=str(row.get("novelty_reason") or ""),
            estimated_effort=str(row.get("estimated_effort") or "medium"),
            task_id=str(row.get("task_id") or ""),
        )


@dataclass(frozen=True)
class SupervisorAction:
    action: Literal["CONDUCT_RESEARCH", "COMPLETE"]
    reason: str
    research_tasks: tuple[ResearchTaskRequest, ...] = field(default_factory=tuple)
    source: str = "deterministic_fallback"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
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

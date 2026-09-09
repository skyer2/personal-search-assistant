"""Supervisor action contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ResearchTaskRequest:
    task_id: str
    objective: str
    priority: str = "normal"
    expected_evidence: str = ""
    source_hints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ResearchTaskRequest":
        row = data or {}
        return cls(
            task_id=str(row.get("task_id") or ""),
            objective=str(row.get("objective") or ""),
            priority=str(row.get("priority") or "normal"),
            expected_evidence=str(row.get("expected_evidence") or ""),
            source_hints=tuple(str(item) for item in row.get("source_hints") or []),
        )


@dataclass(frozen=True)
class SupervisorAction:
    action: Literal["THINK", "CONDUCT_RESEARCH", "COMPLETE"]
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
        if action not in {"THINK", "CONDUCT_RESEARCH", "COMPLETE"}:
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

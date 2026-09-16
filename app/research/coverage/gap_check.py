"""Coverage diagnostics reduced to a minimal, non-authoritative gap check."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ResearchGap:
    question_id: str
    missing_information: str
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GapCheckResult:
    enough_to_answer: bool
    gaps: list[ResearchGap] = field(default_factory=list)
    blocking_gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enough_to_answer": self.enough_to_answer,
            "gaps": [gap.to_dict() for gap in self.gaps],
            "blocking_gaps": list(self.blocking_gaps),
        }


def gap_check(*, brief: Any, answerability: dict[str, Any] | None = None) -> GapCheckResult:
    statuses = list((answerability or {}).get("question_status") or [])
    gaps: list[ResearchGap] = []
    for index, status in enumerate(statuses, 1):
        if not isinstance(status, dict) or bool(status.get("answerable")):
            continue
        qid = str(status.get("question_id") or f"q{index}")
        missing = ", ".join(str(item) for item in status.get("missing_requirements") or []) or "supporting evidence"
        gaps.append(ResearchGap(qid, missing, True))
    blocking = [gap.question_id for gap in gaps if gap.blocking]
    return GapCheckResult(not blocking, gaps, blocking)


__all__ = ["GapCheckResult", "ResearchGap", "gap_check"]

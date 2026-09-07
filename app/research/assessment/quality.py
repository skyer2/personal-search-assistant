"""Quality assessment without routing authority."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict


class QualityVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class QualityAssessment(TypedDict):
    verdict: str
    issues: list[str]
    repairable: bool
    suggested_action: str
    grounding: bool
    citation_metrics: dict[str, Any]


def assess_quality(state: dict[str, Any]) -> QualityAssessment:
    raw = state.get("quality_assessment")
    value = dict(raw) if isinstance(raw, dict) else {}
    verdict = str(value.get("verdict") or QualityVerdict.UNKNOWN.value)
    if verdict not in {item.value for item in QualityVerdict}:
        verdict = QualityVerdict.UNKNOWN.value
    metrics = value.get("citation_metrics")
    return QualityAssessment(
        verdict=verdict,
        issues=[str(item) for item in value.get("issues") or []],
        repairable=bool(value.get("repairable")),
        suggested_action=str(value.get("suggested_action") or ""),
        grounding=bool(value.get("grounding")),
        citation_metrics=dict(metrics) if isinstance(metrics, dict) else {},
    )


__all__ = ["QualityAssessment", "QualityVerdict", "assess_quality"]

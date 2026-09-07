"""Delivery readiness assessment."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict

from app.research.assessment.evidence import EvidenceStatus, assess_evidence
from app.research.assessment.execution_health import ExecutionHealthStatus, assess_execution_health
from app.research.assessment.progress import SemanticProgress, assess_progress


class DeliveryStatus(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"
    UNKNOWN = "unknown"


class DeliveryMode(StrEnum):
    NORMAL = "normal"
    DEGRADED = "degraded"
    PARTIAL_ONLY = "partial_only"
    NONE = "none"


class DeliveryReadiness(TypedDict):
    status: str
    mode: str
    blockers: list[str]
    limitations: list[str]
    reason_codes: list[str]


def assess_delivery(state: dict[str, Any]) -> DeliveryReadiness:
    progress = assess_progress(state)
    evidence = assess_evidence(state)
    health = assess_execution_health(state)
    blockers: list[str] = []
    limitations: list[str] = []
    reasons: list[str] = []
    if progress["status"] == SemanticProgress.UNKNOWN.value:
        blockers.append("progress_unknown")
    elif progress["status"] == SemanticProgress.GAP.value:
        blockers.append("progress_gap")
    if evidence["status"] == EvidenceStatus.UNKNOWN.value:
        blockers.append("evidence_unknown")
    elif evidence["status"] == EvidenceStatus.INSUFFICIENT.value:
        blockers.append("evidence_insufficient")
    if health["status"] == ExecutionHealthStatus.STALLED.value:
        limitations.append("execution_stalled")
    elif health["status"] == ExecutionHealthStatus.FAILED.value and not health["retryable_tasks"]:
        blockers.append("execution_failed")
    elif health["status"] == ExecutionHealthStatus.DEGRADED.value:
        limitations.append("execution_degraded")
    if evidence["status"] == EvidenceStatus.PARTIAL.value:
        limitations.append("evidence_partial")
    ready = not blockers
    if not ready:
        mode = DeliveryMode.NONE
    elif limitations or evidence["status"] == EvidenceStatus.PARTIAL.value:
        mode = DeliveryMode.DEGRADED
    else:
        mode = DeliveryMode.NORMAL
    return DeliveryReadiness(
        status=DeliveryStatus.READY.value if ready else DeliveryStatus.NOT_READY.value,
        mode=mode.value,
        blockers=blockers,
        limitations=limitations,
        reason_codes=reasons or [progress["status"], evidence["status"], health["status"]],
    )


__all__ = ["DeliveryMode", "DeliveryReadiness", "DeliveryStatus", "assess_delivery"]

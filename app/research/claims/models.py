"""Claim-level IR for cross-worker conflict detection and resolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ConflictKind = Literal[
    "expected_disagreement",
    "unresolved_conflict",
    "resolved",
]
ResolutionStatus = Literal["resolved", "disclosed", "unresolved"]


@dataclass
class ClaimRecord:
    claim_id: str
    text: str
    task_id: str = ""
    subject: str = ""
    metric: str = ""
    value: float | None = None
    unit: str = ""
    period: str = ""
    scope: str = ""
    sources: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0
    source_quality: str = "unknown"
    authority_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ClaimRecord":
        row = dict(data or {})
        value = row.get("value")
        try:
            parsed = float(value) if value is not None and value != "" else None
        except (TypeError, ValueError):
            parsed = None
        return cls(
            claim_id=str(row.get("claim_id") or ""),
            text=str(row.get("text") or row.get("claim") or ""),
            task_id=str(row.get("task_id") or ""),
            subject=str(row.get("subject") or ""),
            metric=str(row.get("metric") or ""),
            value=parsed,
            unit=str(row.get("unit") or ""),
            period=str(row.get("period") or ""),
            scope=str(row.get("scope") or ""),
            sources=[str(x) for x in (row.get("sources") or []) if x],
            evidence_ids=[str(x) for x in (row.get("evidence_ids") or []) if x],
            confidence=float(row.get("confidence") or 1.0),
            source_quality=str(row.get("source_quality") or "unknown"),
            authority_score=float(row.get("authority_score") or 0.0),
        )


@dataclass
class ConflictEdge:
    edge_id: str
    left_id: str
    right_id: str
    kind: ConflictKind
    reason: str = ""
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClaimResolution:
    edge_id: str
    status: ResolutionStatus
    kind: ConflictKind
    label: str
    note: str = ""
    winner_id: str = ""
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReconciliationResult:
    claims: list[ClaimRecord] = field(default_factory=list)
    edges: list[ConflictEdge] = field(default_factory=list)
    resolutions: list[ClaimResolution] = field(default_factory=list)
    unresolved_labels: list[str] = field(default_factory=list)
    disclosed_labels: list[str] = field(default_factory=list)
    resolved_labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claims": [c.to_dict() for c in self.claims],
            "edges": [e.to_dict() for e in self.edges],
            "resolutions": [r.to_dict() for r in self.resolutions],
            "unresolved_labels": list(self.unresolved_labels),
            "disclosed_labels": list(self.disclosed_labels),
            "resolved_labels": list(self.resolved_labels),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ReconciliationResult":
        row = dict(data or {})
        return cls(
            claims=[ClaimRecord.from_dict(x) for x in (row.get("claims") or []) if isinstance(x, dict)],
            edges=[
                ConflictEdge(
                    edge_id=str(x.get("edge_id") or ""),
                    left_id=str(x.get("left_id") or ""),
                    right_id=str(x.get("right_id") or ""),
                    kind=str(x.get("kind") or "unresolved_conflict"),  # type: ignore[arg-type]
                    reason=str(x.get("reason") or ""),
                    label=str(x.get("label") or ""),
                )
                for x in (row.get("edges") or [])
                if isinstance(x, dict)
            ],
            resolutions=[
                ClaimResolution(
                    edge_id=str(x.get("edge_id") or ""),
                    status=str(x.get("status") or "unresolved"),  # type: ignore[arg-type]
                    kind=str(x.get("kind") or "unresolved_conflict"),  # type: ignore[arg-type]
                    label=str(x.get("label") or ""),
                    note=str(x.get("note") or ""),
                    winner_id=str(x.get("winner_id") or ""),
                    evidence_ids=[str(e) for e in (x.get("evidence_ids") or []) if e],
                )
                for x in (row.get("resolutions") or [])
                if isinstance(x, dict)
            ],
            unresolved_labels=[str(x) for x in (row.get("unresolved_labels") or []) if x],
            disclosed_labels=[str(x) for x in (row.get("disclosed_labels") or []) if x],
            resolved_labels=[str(x) for x in (row.get("resolved_labels") or []) if x],
        )

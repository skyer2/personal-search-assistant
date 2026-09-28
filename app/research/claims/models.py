"""Claim-level IR for cross-worker conflict detection and resolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ClaimType = Literal["fact", "inference", "forecast", "attributed_opinion"]
ValidationStatus = Literal["pending", "supported", "rejected", "unknown"]
SupportRelation = Literal["supports", "contradicts", "context_only", "unknown"]

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
    ask_id: str = ""
    question_id: str = ""
    subject: str = ""
    subject_id: str = ""
    dimension_id: str = ""
    criterion_id: str = ""
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
    normalized_key: str = ""
    claim_type: ClaimType = "fact"
    provenance: str = "worker"
    validated: bool = False
    publishability_score: float = 0.0
    admission_reasons: list[str] = field(default_factory=list)
    field_ids: list[str] = field(default_factory=list)
    validation_status: ValidationStatus = "pending"
    validation_version: str = ""
    support_edge_ids: list[str] = field(default_factory=list)

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
            ask_id=str(row.get("ask_id") or ""),
            question_id=str(row.get("question_id") or ""),
            subject=str(row.get("subject") or ""),
            subject_id=str(row.get("subject_id") or ""),
            dimension_id=str(row.get("dimension_id") or ""),
            criterion_id=str(row.get("criterion_id") or ""),
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
            normalized_key=str(row.get("normalized_key") or ""),
            claim_type=str(row.get("claim_type") or "fact"),  # type: ignore[arg-type]
            provenance=str(row.get("provenance") or "worker"),
            validated=bool(row.get("validated", False)),
            publishability_score=max(0.0, min(1.0, float(row.get("publishability_score") or 0.0))),
            admission_reasons=[str(x) for x in (row.get("admission_reasons") or []) if str(x).strip()],
            field_ids=[str(x) for x in (row.get("field_ids") or []) if str(x).strip()],
            validation_status=(
                str(row.get("validation_status"))
                if str(row.get("validation_status")) in {"pending", "supported", "rejected", "unknown"}
                else "pending"
            ),  # type: ignore[arg-type]
            validation_version=str(row.get("validation_version") or ""),
            support_edge_ids=[str(x) for x in (row.get("support_edge_ids") or []) if str(x).strip()],
        )

    @property
    def is_supported(self) -> bool:
        return self.validation_status == "supported"


@dataclass(frozen=True)
class SupportEdge:
    edge_id: str
    claim_id: str
    evidence_id: str
    span_id: str
    relation: SupportRelation
    checked_by: str
    validator_version: str
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SupportEdge":
        relation = str(data.get("relation") or "unknown")
        if relation not in {"supports", "contradicts", "context_only", "unknown"}:
            relation = "unknown"
        return cls(
            edge_id=str(data.get("edge_id") or ""),
            claim_id=str(data.get("claim_id") or ""),
            evidence_id=str(data.get("evidence_id") or ""),
            span_id=str(data.get("span_id") or ""),
            relation=relation,  # type: ignore[arg-type]
            checked_by=str(data.get("checked_by") or ""),
            validator_version=str(data.get("validator_version") or ""),
            reason_codes=tuple(str(item) for item in data.get("reason_codes") or []),
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
    blocking: bool = False
    criterion_id: str = ""

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
                    blocking=bool(x.get("blocking", False)),
                    criterion_id=str(x.get("criterion_id") or ""),
                )
                for x in (row.get("resolutions") or [])
                if isinstance(x, dict)
            ],
            unresolved_labels=[str(x) for x in (row.get("unresolved_labels") or []) if x],
            disclosed_labels=[str(x) for x in (row.get("disclosed_labels") or []) if x],
            resolved_labels=[str(x) for x in (row.get("resolved_labels") or []) if x],
        )

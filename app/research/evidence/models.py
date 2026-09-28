"""Canonical raw-evidence references. Raw content stays outside graph state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass
class EvidenceRecord:
    evidence_id: str
    source_id: str
    source_kind: str
    locator: str
    canonical_source_id: str = ""
    publisher: str = ""
    title: str = ""
    retrieved_at: str = ""
    published_at: str = ""
    effective_at: str = ""
    source_tier: str = "SECONDARY"
    authority_score: float = 0.5
    source_type: str = "secondary"
    directness_score: float = 0.5
    freshness_score: float = 0.5
    independence_score: float = 1.0
    completeness_score: float = 0.5
    excerpt_ref: str = ""
    artifact_ref: str = ""
    language: str = ""
    task_id: str = ""
    question_id: str = ""
    ask_id: str = ""
    run_id: str = ""
    # Evidence quality is deliberately independent from URL/source quality.
    # It is populated by the artifact/extractor boundary when text is known.
    excerpt_quality: float = 0.0
    extraction_confidence: float = 0.0
    artifact_id: str = ""
    canonical_url: str = ""
    registrable_domain: str = ""
    content_kind: Literal["snippet", "excerpt", "fulltext", "structured_record"] = "snippet"
    content_hash: str = ""
    content_ref: str = ""
    spans: list[dict[str, Any]] = field(default_factory=list)
    publisher_relationship: Literal["first_party", "independent", "syndication", "unknown"] = "unknown"
    origin_group_id: str = ""
    admission_status: Literal["discovery_only", "admitted", "rejected"] = "discovery_only"
    reason_codes: list[str] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EvidenceRecord":
        row = data or {}
        return cls(
            evidence_id=str(row.get("evidence_id") or ""),
            source_id=str(row.get("source_id") or ""),
            source_kind=str(row.get("source_kind") or "web"),
            locator=str(row.get("locator") or ""),
            canonical_source_id=str(row.get("canonical_source_id") or ""),
            publisher=str(row.get("publisher") or ""),
            title=str(row.get("title") or ""),
            retrieved_at=str(row.get("retrieved_at") or ""),
            published_at=str(row.get("published_at") or ""),
            effective_at=str(row.get("effective_at") or ""),
            source_tier=str(row.get("source_tier") or "SECONDARY"),
            authority_score=max(0.0, min(1.0, float(row.get("authority_score") or 0.5))),
            source_type=str(row.get("source_type") or "secondary"),
            directness_score=max(0.0, min(1.0, float(row.get("directness_score") or 0.5))),
            freshness_score=max(0.0, min(1.0, float(row.get("freshness_score") or 0.5))),
            independence_score=max(0.0, min(1.0, float(row.get("independence_score") or 1.0))),
            completeness_score=max(0.0, min(1.0, float(row.get("completeness_score") or 0.5))),
            excerpt_ref=str(row.get("excerpt_ref") or ""),
            artifact_ref=str(row.get("artifact_ref") or ""),
            language=str(row.get("language") or ""),
            task_id=str(row.get("task_id") or ""),
            question_id=str(row.get("question_id") or ""),
            ask_id=str(row.get("ask_id") or ""),
            run_id=str(row.get("run_id") or ""),
            excerpt_quality=max(0.0, min(1.0, float(row.get("excerpt_quality") or 0.0))),
            extraction_confidence=max(0.0, min(1.0, float(row.get("extraction_confidence") or 0.0))),
            artifact_id=str(row.get("artifact_id") or row.get("artifact_ref") or ""),
            canonical_url=str(row.get("canonical_url") or row.get("locator") or ""),
            registrable_domain=str(row.get("registrable_domain") or ""),
            content_kind=(
                str(row.get("content_kind"))
                if str(row.get("content_kind")) in {"snippet", "excerpt", "fulltext", "structured_record"}
                else "snippet"
            ),  # type: ignore[arg-type]
            content_hash=str(row.get("content_hash") or ""),
            content_ref=str(row.get("content_ref") or row.get("excerpt_ref") or ""),
            spans=[dict(item) for item in row.get("spans") or [] if isinstance(item, dict)],
            publisher_relationship=(
                str(row.get("publisher_relationship"))
                if str(row.get("publisher_relationship")) in {"first_party", "independent", "syndication", "unknown"}
                else "unknown"
            ),  # type: ignore[arg-type]
            origin_group_id=str(row.get("origin_group_id") or ""),
            admission_status=(
                str(row.get("admission_status"))
                if str(row.get("admission_status")) in {"discovery_only", "admitted", "rejected"}
                else "discovery_only"
            ),  # type: ignore[arg-type]
            reason_codes=[str(item) for item in row.get("reason_codes") or []],
            provenance={str(key): str(value) for key, value in dict(row.get("provenance") or {}).items()},
        )


@dataclass(frozen=True)
class SalvageEvidence:
    """Artifact material recovered after a worker stops.

    This type intentionally cannot become a Finding by itself.  A later
    promotion must build a ClaimDraft and pass the same admission gate as a
    normal worker claim.
    """

    evidence_id: str
    task_id: str
    question_id: str
    source_id: str
    locator: str
    title: str
    excerpt: str
    source_type: str
    extraction_quality: float
    is_complete_sentence: bool
    is_navigation_text: bool
    is_search_snippet: bool
    provenance: Literal["worker_salvage"] = "worker_salvage"
    publishable_as_claim: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["EvidenceRecord", "SalvageEvidence"]

"""Deterministic source-quality policy used before evidence becomes canonical."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal


SourceType = Literal[
    "primary", "authoritative_secondary", "secondary", "community", "unknown"
]


@dataclass(frozen=True)
class SourceQuality:
    authority: float
    directness: float
    freshness: float
    independence: float
    completeness: float
    source_type: SourceType

    @property
    def score(self) -> float:
        return round(
            0.35 * self.authority
            + 0.25 * self.directness
            + 0.15 * self.freshness
            + 0.10 * self.independence
            + 0.15 * self.completeness,
            4,
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "score": self.score}


def score_source(
    locator: str,
    *,
    declared_quality: str = "",
    published_at: str = "",
    publisher_relationship: str = "unknown",
    content_kind: str = "snippet",
    as_of: datetime | None = None,
) -> SourceQuality:
    """Score declared source properties; URL wording never upgrades quality."""
    quality = declared_quality.casefold().strip()
    relationship = publisher_relationship.casefold().strip()
    if relationship == "first_party" or quality in {"primary", "official", "regulatory"}:
        source_type: SourceType = "primary"
        authority, directness = 0.92, 0.90
    elif quality in {"authoritative_secondary", "high_quality_secondary"}:
        source_type = "authoritative_secondary"
        authority, directness = 0.80, 0.78
    elif quality == "community":
        source_type = "community"
        authority, directness = 0.25, 0.35
    elif locator.startswith(("http://", "https://")):
        source_type = "secondary"
        authority, directness = 0.55, 0.60
    else:
        source_type = "unknown"
        authority, directness = 0.15, 0.20
    if content_kind in {"fulltext", "structured_record"}:
        directness = max(directness, 0.85)
        completeness = 0.9
    elif content_kind == "excerpt":
        directness = max(directness, 0.65)
        completeness = 0.65
    else:
        directness = min(directness, 0.35)
        completeness = 0.25
    freshness = 0.0
    if published_at:
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            reference = as_of or datetime.now(timezone.utc)
            age_days = max(0, (reference.astimezone(timezone.utc) - published.astimezone(timezone.utc)).days)
            freshness = 1.0 if age_days <= 30 else 0.85 if age_days <= 180 else 0.55 if age_days <= 365 else 0.25
        except ValueError:
            freshness = 0.0
    independence = 1.0 if relationship == "independent" else 0.5 if relationship == "first_party" else 0.0
    return SourceQuality(authority, directness, freshness, independence, completeness, source_type)


def is_high_authority(record: dict[str, Any]) -> bool:
    tier = str(record.get("source_tier") or "").upper()
    return tier in {"PRIMARY", "HIGH_QUALITY_SECONDARY"} or float(record.get("authority_score") or 0) >= 0.75


def source_quality_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in records if isinstance(row, dict)]
    types = [str(row.get("source_type") or "").lower() for row in rows]
    total = len(rows)
    primary = sum(item == "primary" for item in types)
    high = sum(is_high_authority(row) for row in rows)
    origins = {
        str(row.get("origin_group_id") or "")
        for row in rows
        if str(row.get("publisher_relationship") or "") == "independent"
        and str(row.get("origin_group_id") or "")
    }
    concentration = 0.0
    if total:
        counts: dict[str, int] = {}
        for row in rows:
            key = str(row.get("source_id") or row.get("locator") or "unknown")
            counts[key] = counts.get(key, 0) + 1
        concentration = max(counts.values()) / total
    return {
        "evidence_count": total,
        "primary_source_ratio": round(primary / total, 4) if total else 0.0,
        "high_authority_source_ratio": round(high / total, 4) if total else 0.0,
        "independent_source_count": len(origins),
        "source_domain_concentration": round(concentration, 4),
        "source_domain_concentration_warning": concentration > 0.5,
    }


__all__ = ["SourceQuality", "is_high_authority", "score_source", "source_quality_metrics"]

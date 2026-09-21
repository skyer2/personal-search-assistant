"""Deterministic source-quality policy used before evidence becomes canonical."""

from __future__ import annotations

from dataclasses import asdict, dataclass
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


_PRIMARY = (
    "sec.gov", "gov.", ".gov/", "europa.eu", "arxiv.org", "doi.org",
    "openai.com", "anthropic.com", "microsoft.com", "google.com", "meta.com",
    "/investor", "/ir/", "newsroom", "/blog/", "/docs/", "official",
)
_AUTHORITATIVE = (
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com",
    "nature.com", "science.org", "gartner.com", "idc.com",
)
_COMMUNITY = (
    "reddit.com", "news.ycombinator.com", "zhihu.com", "juejin.cn",
    "medium.com", "substack.com", "twitter.com", "x.com", "weibo.com",
)


def score_source(locator: str, *, declared_quality: str = "", published_at: str = "") -> SourceQuality:
    """Classify a source without network access; unknown remains a weak signal."""
    value = f"{locator} {declared_quality}".casefold()
    if any(token in value for token in _PRIMARY):
        source_type: SourceType = "primary"
        authority, directness = 0.92, 0.90
    elif any(token in value for token in _AUTHORITATIVE):
        source_type = "authoritative_secondary"
        authority, directness = 0.80, 0.78
    elif any(token in value for token in _COMMUNITY):
        source_type = "community"
        authority, directness = 0.25, 0.35
    elif locator.startswith(("http://", "https://")):
        source_type = "secondary"
        authority, directness = 0.55, 0.60
    else:
        source_type = "unknown"
        authority, directness = 0.15, 0.20
    freshness = 0.85 if published_at else 0.45
    completeness = 0.85 if locator.startswith(("http://", "https://")) else 0.35
    return SourceQuality(authority, directness, freshness, 1.0, completeness, source_type)


def is_high_authority(record: dict[str, Any]) -> bool:
    tier = str(record.get("source_tier") or "").upper()
    return tier in {"PRIMARY", "HIGH_QUALITY_SECONDARY"} or float(record.get("authority_score") or 0) >= 0.75


def source_quality_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in records if isinstance(row, dict)]
    types = [str(row.get("source_type") or "").lower() for row in rows]
    total = len(rows)
    primary = sum(item == "primary" for item in types)
    high = sum(is_high_authority(row) for row in rows)
    domains = {str(row.get("source_id") or row.get("locator") or "") for row in rows}
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
        "independent_source_count": len({item for item in domains if item}),
        "source_domain_concentration": round(concentration, 4),
        "source_domain_concentration_warning": concentration > 0.5,
    }


__all__ = ["SourceQuality", "is_high_authority", "score_source", "source_quality_metrics"]

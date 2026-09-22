"""Source tier policy for freshness-sensitive trend research.

Aggregators, reposts and community blogs can seed discovery, but they must not
be the only support for a core claim.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from app.research.evidence.policy import registrable_domain

TIER_1 = "tier1"
TIER_2 = "tier2"
TIER_3 = "tier3"

# Official announcements, docs, research, regulators and standards bodies.
_TIER1_HOST = re.compile(
    r"(?:^|\.)(?:openai\.com|anthropic\.com|deepmind\.google|ai\.google|blog\.google|"
    r"microsoft\.com|azure\.com|aws\.amazon\.com|cloud\.google\.com|meta\.com|ai\.meta\.com|"
    r"cursor\.com|anysphere\.co|modelcontextprotocol\.io|langchain\.com|langchain\.dev|"
    r"github\.blog|kubernetes\.io|cncf\.io|apache\.org|python\.org|"
    r"arxiv\.org|acm\.org|ieee\.org|nature\.com|science\.org|openreview\.net|"
    r"sec\.gov|europa\.eu|gov\.cn|gov\.uk|whitehouse\.gov|nist\.gov|iso\.org|w3\.org|ietf\.org)$",
    re.IGNORECASE,
)
_TIER1_PATH = re.compile(
    r"/(?:blog|news(?:room)?|research|docs?|announcements?|changelog|releases?|"
    r"investor|ir|press|whitepapers?)(?:/|$)",
    re.IGNORECASE,
)

# Reputable technical/business media and independent research houses.
_TIER2_HOST = re.compile(
    r"(?:^|\.)(?:reuters\.com|bloomberg\.com|ft\.com|wsj\.com|economist\.com|nytimes\.com|"
    r"theinformation\.com|techcrunch\.com|theverge\.com|arstechnica\.com|wired\.com|"
    r"venturebeat\.com|infoq\.com|thenewstack\.io|semianalysis\.com|stratechery\.com|"
    r"gartner\.com|idc\.com|forrester\.com|mckinsey\.com|statista\.com|"
    r"36kr\.com|jiqizhixin\.com|infoq\.cn|leiphone\.com|caixin\.com)$",
    re.IGNORECASE,
)

# Community, reposts and content aggregators.
_TIER3_HOST = re.compile(
    r"(?:^|\.)(?:csdn\.net|cnblogs\.com|jianshu\.com|zhihu\.com|juejin\.cn|segmentfault\.com|"
    r"51cto\.com|oschina\.net|baijiahao\.baidu\.com|sohu\.com|163\.com|sina\.com\.cn|"
    r"toutiao\.com|weixin\.qq\.com|medium\.com|substack\.com|dev\.to|hashnode\.dev|"
    r"blogspot\.com|wordpress\.com|reddit\.com|news\.ycombinator\.com|quora\.com|"
    r"wikipedia\.org|baike\.baidu\.com)$",
    re.IGNORECASE,
)


def classify_source_tier(row: dict[str, Any]) -> str:
    """Classify one evidence record into tier1 / tier2 / tier3."""
    declared = str(row.get("source_tier") or "").strip().lower()
    if declared in {TIER_1, TIER_2, TIER_3}:
        return declared
    if declared == "primary":
        return TIER_1
    if declared in {"high_quality_secondary", "secondary"}:
        return TIER_2
    locator = str(row.get("locator") or row.get("url") or row.get("source_id") or "")
    host = registrable_domain(locator) or ""
    if _TIER3_HOST.search(host):
        return TIER_3
    if _TIER1_HOST.search(host):
        return TIER_1
    if _TIER2_HOST.search(host):
        return TIER_2
    if host and _TIER1_PATH.search(locator):
        # A vendor's own docs/news/release path is a first-party announcement.
        return TIER_1
    return TIER_3


@dataclass(frozen=True)
class SourceQualityResult:
    """``passed`` gates SUCCESS; ``partial_passed`` only rejects tier3-only support."""

    passed: bool
    partial_passed: bool = False
    tier1_count: int = 0
    tier2_domains: int = 0
    tier3_count: int = 0
    total: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_source_quality(
    evidence_records: list[dict[str, Any]] | None,
    *,
    require_core_support: bool = True,
) -> SourceQualityResult:
    """A core claim needs one tier1 source or two independent tier2 domains."""
    rows = [row for row in evidence_records or [] if isinstance(row, dict)]
    if not rows:
        return SourceQualityResult(False, False, reason="no_evidence")
    tier1 = 0
    tier2_domains: set[str] = set()
    tier3 = 0
    for row in rows:
        tier = classify_source_tier(row)
        locator = str(row.get("locator") or row.get("url") or row.get("source_id") or "")
        domain = registrable_domain(locator) or locator
        if tier == TIER_1:
            tier1 += 1
        elif tier == TIER_2:
            if domain:
                tier2_domains.add(domain)
        else:
            tier3 += 1
    # A degraded partial may rest on a single reputable source; it may never
    # rest only on community reposts and aggregators.
    partial_passed = tier1 >= 1 or len(tier2_domains) >= 1
    if not require_core_support:
        return SourceQualityResult(
            True, True, tier1, len(tier2_domains), tier3, len(rows), "not_required"
        )
    passed = tier1 >= 1 or len(tier2_domains) >= 2
    reason = (
        ""
        if passed
        else "core_claim_supported_only_by_tier3"
        if not partial_passed
        else "core_claim_needs_primary_or_two_independent_secondary"
    )
    return SourceQualityResult(
        passed, partial_passed, tier1, len(tier2_domains), tier3, len(rows), reason
    )


__all__ = [
    "TIER_1",
    "TIER_2",
    "TIER_3",
    "SourceQualityResult",
    "classify_source_tier",
    "evaluate_source_quality",
]

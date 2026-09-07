"""Shared authority and sufficiency policy for fact-oriented research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol
from urllib.parse import urlsplit


class EvidenceLike(Protocol):
    locator: str
    source_tier: str


# Multi-label public suffixes encountered by the trusted-source registry.  Keeping
# this small, explicit list avoids network access while fixing the unsafe
# ``host.split('.')[-2:]`` approximation.  New authorities must add their public
# suffix here (or migrate this function to a packaged PSL implementation).
_MULTI_LABEL_PUBLIC_SUFFIXES = frozenset(
    {"co.uk", "com.au", "com.br", "com.cn", "com.sg", "co.jp", "co.kr", "co.nz"}
)


def registrable_domain(locator: str) -> str:
    """Return an offline eTLD+1 approximation for a URL hostname."""
    host = (urlsplit(str(locator)).hostname or "").lower().strip(".")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    suffix_width = 2 if ".".join(labels[-2:]) in _MULTI_LABEL_PUBLIC_SUFFIXES else 1
    return ".".join(labels[-(suffix_width + 1) :])


@dataclass(frozen=True)
class EvidencePolicy:
    """One source policy used by both search stopping and final quality gates."""

    primary_tier: str = "PRIMARY"
    secondary_tier: str = "HIGH_QUALITY_SECONDARY"

    def is_sufficient(self, evidence: Iterable[EvidenceLike]) -> bool:
        sources = list(evidence)
        if any(source.source_tier == self.primary_tier for source in sources):
            return True
        independent_secondary_domains = {
            registrable_domain(source.locator)
            for source in sources
            if source.source_tier == self.secondary_tier
            and registrable_domain(source.locator)
        }
        return len(independent_secondary_domains) >= 2


SIMPLE_FACT_EVIDENCE_POLICY = EvidencePolicy()


__all__ = ["EvidencePolicy", "SIMPLE_FACT_EVIDENCE_POLICY", "registrable_domain"]

"""Small deterministic diagnostics for insight-oriented delivery."""

from __future__ import annotations

import re
from typing import Any


_INSIGHT_MARKERS = (
    "因为", "因此", "驱动", "意味着", "关键在于", "风险", "取舍", "判断", "趋势", "未来", "原因", "机制",
)


def insight_density(*, content: str, findings: list[dict[str, Any]], analytical: bool) -> dict[str, Any]:
    """Return a transparent, non-LLM insight diagnostic.

    A sentence counts only when it contains a reasoning marker and is backed by
    at least one finding/evidence block.  This is deliberately a quality signal
    rather than a provider-dependent rewrite rule.
    """
    sentences = [item.strip() for item in re.split(r"[。！？!?\n]", str(content or "")) if item.strip()]
    marker_count = sum(1 for item in sentences if any(marker in item for marker in _INSIGHT_MARKERS))
    supported = sum(1 for row in findings if isinstance(row, dict) and (row.get("evidence_ids") or row.get("source_ids") or row.get("sources")))
    density = marker_count / max(1, len(sentences))
    minimum = 3 if analytical else 0
    return {
        "analytical": bool(analytical),
        "insight_count": int(marker_count),
        "supported_finding_count": int(supported),
        "density": round(density, 4),
        "minimum": minimum,
        "passed": (not analytical) or marker_count >= minimum,
    }


__all__ = ["insight_density"]

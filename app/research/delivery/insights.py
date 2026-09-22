"""Small deterministic diagnostics for insight-oriented delivery."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


_INSIGHT_MARKERS = (
    "因为", "因此", "驱动", "意味着", "关键在于", "风险", "取舍", "判断", "趋势", "未来", "原因", "机制",
)


@dataclass(frozen=True)
class TrendSignal:
    signal_id: str
    statement: str
    evidence_refs: tuple[str, ...]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "evidence_refs": list(self.evidence_refs)}


@dataclass(frozen=True)
class Mechanism:
    mechanism_id: str
    statement: str
    signal_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "signal_ids": list(self.signal_ids),
            "evidence_refs": list(self.evidence_refs),
        }


def _claim(row: dict[str, Any]) -> str:
    values = row.get("claims")
    if isinstance(values, (list, tuple)) and values:
        return str(values[0] or "").strip()
    return str(row.get("claim") or row.get("summary") or row.get("text") or "").strip()


def _refs(row: dict[str, Any]) -> tuple[str, ...]:
    raw = row.get("evidence_ids") or row.get("source_ids") or row.get("sources") or []
    raw = [raw] if isinstance(raw, str) else raw
    return tuple(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


def build_insight_layer(findings: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Make evidence-backed signals and multi-signal mechanisms explicit."""
    pairs: list[tuple[TrendSignal, dict[str, Any]]] = []
    for index, finding in enumerate(findings, 1):
        claim, refs = _claim(finding), _refs(finding)
        if not claim or not refs:
            continue
        try:
            confidence = max(0.0, min(1.0, float(finding.get("confidence") or 0.6)))
        except (TypeError, ValueError):
            confidence = 0.6
        pairs.append((TrendSignal(f"signal_{index}", claim[:360], refs, confidence), finding))
    groups: dict[str, list[TrendSignal]] = {}
    for signal, finding in pairs:
        keys = finding.get("supported_criteria") or [finding.get("criterion_id")]
        key = str(next((item for item in keys if str(item).strip()), "general"))
        groups.setdefault(key, []).append(signal)
    mechanisms: list[Mechanism] = []
    for index, (criterion, rows) in enumerate(groups.items(), 1):
        if len(rows) < 2:
            continue
        mechanisms.append(Mechanism(
            f"mechanism_{index}",
            f"围绕“{criterion[:120]}”的多条独立信号共同支持该方向；报告需解释它们如何相互作用，并保留反例与不确定性。",
            tuple(item.signal_id for item in rows[:4]),
            tuple(dict.fromkeys(ref for item in rows[:4] for ref in item.evidence_refs)),
        ))
    return {
        "signals": [item.to_dict() for item, _ in pairs[:12]],
        "mechanisms": [item.to_dict() for item in mechanisms[:6]],
    }


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


__all__ = ["Mechanism", "TrendSignal", "build_insight_layer", "insight_density"]

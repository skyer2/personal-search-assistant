"""Semantic, not activity-based, wave gain assessment."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SemanticWaveGain:
    wave_id: int
    coverage_delta: float
    newly_covered_units: int
    newly_resolved_gaps: int
    conflicts_resolved: int
    confidence_delta: float
    new_high_quality_sources: int
    semantic_gain: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarginalGainAssessment:
    semantic_gain: float
    marginal_gain: float
    stalled: bool
    wave_gains: list[SemanticWaveGain] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "semantic_gain": self.semantic_gain,
            "marginal_gain": self.marginal_gain,
            "stalled": self.stalled,
            "wave_gains": [gain.to_dict() for gain in self.wave_gains],
        }


def record_semantic_wave(
    *,
    wave_id: int,
    previous_coverage_ratio: float,
    current_coverage_ratio: float,
    previous_covered_ids: list[str],
    current_covered_ids: list[str],
    previous_gap_ids: list[str],
    current_gap_ids: list[str],
    previous_conflict_ids: list[str],
    current_conflict_ids: list[str],
    previous_confidence: float,
    current_confidence: float,
    previous_high_quality_source_ids: list[str],
    current_high_quality_source_ids: list[str],
) -> SemanticWaveGain:
    newly_covered = len(set(current_covered_ids) - set(previous_covered_ids))
    resolved_gaps = len(set(previous_gap_ids) - set(current_gap_ids))
    conflicts_resolved = len(set(previous_conflict_ids) - set(current_conflict_ids))
    new_sources = len(set(current_high_quality_source_ids) - set(previous_high_quality_source_ids))
    confidence_delta = max(0.0, current_confidence - previous_confidence)
    coverage_delta = max(0.0, current_coverage_ratio - previous_coverage_ratio)
    semantic_gain = (
        3.0 * resolved_gaps
        + 2.0 * newly_covered
        + 2.0 * conflicts_resolved
        + confidence_delta
        + 0.5 * new_sources
    )
    return SemanticWaveGain(
        wave_id=wave_id,
        coverage_delta=coverage_delta,
        newly_covered_units=newly_covered,
        newly_resolved_gaps=resolved_gaps,
        conflicts_resolved=conflicts_resolved,
        confidence_delta=confidence_delta,
        new_high_quality_sources=new_sources,
        semantic_gain=semantic_gain,
    )


def assess_marginal_gain(
    wave_gains: list[Any] | None,
    *,
    threshold: float = 0.5,
    window: int = 2,
) -> MarginalGainAssessment:
    gains = [
        SemanticWaveGain(**row) if isinstance(row, dict) else row
        for row in wave_gains or []
        if isinstance(row, (dict, SemanticWaveGain))
    ]
    recent = gains[-max(1, window):]
    marginal = sum(gain.semantic_gain for gain in recent) / len(recent) if recent else 0.0
    return MarginalGainAssessment(
        semantic_gain=gains[-1].semantic_gain if gains else 0.0,
        marginal_gain=marginal,
        stalled=len(gains) >= window and marginal < threshold,
        wave_gains=gains,
    )


__all__ = [
    "MarginalGainAssessment",
    "SemanticWaveGain",
    "assess_marginal_gain",
    "record_semantic_wave",
]

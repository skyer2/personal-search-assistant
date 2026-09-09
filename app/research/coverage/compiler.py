"""Compile a ResearchSpec into its semantic CoverageContract."""

from __future__ import annotations

from typing import Any

from app.research.coverage.models import CoverageContract, CoverageUnit, stable_coverage_id
from app.research.spec.models import ResearchSpec


def compile_coverage_contract(
    spec: ResearchSpec | dict[str, Any] | None,
    *,
    candidate_names: list[str] | None = None,
) -> CoverageContract:
    value = spec if isinstance(spec, ResearchSpec) else ResearchSpec.from_dict(spec)
    units: list[CoverageUnit] = []
    dimensions = [dimension for dimension in value.dimensions if dimension.required]
    if value.task_shape in {"BREADTH_HEAVY", "DYNAMIC_DISCOVERY"}:
        subjects = [value.subjects[0]] if value.subjects else []
        if not candidate_names:
            discovery_dimensions = [dimension for dimension in dimensions if dimension.dimension_id == "candidate_set"]
            if not discovery_dimensions:
                discovery_dimensions = dimensions[:1]
            for subject in subjects:
                for dimension in discovery_dimensions:
                    units.append(_unit(value, subject.subject_id, dimension.dimension_id, dimension.name))
        for candidate in candidate_names or []:
            subject_id = f"candidate:{candidate}"
            for dimension in dimensions:
                if dimension.dimension_id == "candidate_set":
                    continue
                units.append(_unit(value, subject_id, dimension.dimension_id, dimension.name))
    else:
        for subject in value.subjects:
            for dimension in dimensions:
                units.append(_unit(value, subject.subject_id, dimension.dimension_id, dimension.name))
    return CoverageContract(
        contract_id=f"contract_{value.spec_id.removeprefix('spec_')}",
        spec_id=value.spec_id,
        version=value.version,
        units=units,
    )


def _unit(spec: ResearchSpec, subject_id: str, dimension_id: str, dimension_name: str) -> CoverageUnit:
    return CoverageUnit(
        coverage_id=stable_coverage_id(subject_id, dimension_id),
        subject_id=subject_id,
        dimension_id=dimension_id,
        claim_requirement=f"{subject_id}:{dimension_name}",
        importance="required",
        min_evidence=1,
        min_independent_sources=max(1, spec.evidence_requirements.min_independent_sources),
        min_authority_score=0.25 if spec.evidence_requirements.prefer_primary else 0.0,
        freshness_policy={
            "required": spec.freshness.required,
            "max_age_days": spec.freshness.max_age_days,
        },
        conflict_policy={
            "blocking": spec.evidence_requirements.conflict_resolution_required
            or dimension_id != "key_fact"
        },
    )


__all__ = ["compile_coverage_contract"]

"""CandidateSet → deep-dive plan expansion operator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.coverage.compiler import compile_coverage_contract
from app.research.coverage.models import CoverageContract
from app.research.planning.candidate import CandidateSet
from app.research.planning.planner import _candidate_steps
from app.research.planning.validator import PlanMutationProposal
from app.research.spec.models import ResearchSpec


@dataclass
class ExpandPlanResult:
    applied: bool
    reason: str
    plan: ExecutionPlan
    candidate_set: dict[str, Any]
    coverage_contract: CoverageContract
    proposal: PlanMutationProposal | None = None


def expand_plan(
    spec: ResearchSpec | dict[str, Any] | None,
    candidate_set: dict[str, Any] | None,
    *,
    plan_version: int,
) -> ExpandPlanResult:
    value = spec if isinstance(spec, ResearchSpec) else ResearchSpec.from_dict(spec)
    candidates = CandidateSet.from_dict(candidate_set or {})
    if not candidates.available:
        return ExpandPlanResult(
            applied=False,
            reason="candidate_set_unavailable",
            plan=ExecutionPlan(plan_version=plan_version, planning_mode="spec_driven"),
            candidate_set=candidates.to_dict(),
            coverage_contract=compile_coverage_contract(value),
        )
    if candidates.expanded:
        return ExpandPlanResult(
            applied=False,
            reason="candidate_set_already_expanded",
            plan=ExecutionPlan(plan_version=plan_version, planning_mode="spec_driven"),
            candidate_set=candidates.to_dict(),
            coverage_contract=compile_coverage_contract(value, candidate_names=candidates.items),
        )
    dimension_count = max(
        1,
        len([dimension for dimension in value.dimensions if dimension.dimension_id != "candidate_set"]),
    )
    selected_candidates = CandidateSet(
        candidate_set_id=candidates.candidate_set_id,
        status=candidates.status,
        candidates=candidates.admitted_candidates[: max(1, 12 // dimension_count)],
        source_task_ids=candidates.source_task_ids,
        context=candidates.context,
        query=candidates.query,
        fallback=candidates.fallback,
    )
    candidate_ids = [candidate.candidate_id for candidate in selected_candidates.candidates]
    contract = compile_coverage_contract(value, candidate_ids=candidate_ids)
    steps = _candidate_steps(value, contract, selected_candidates, expanded=True)
    if not steps:
        return ExpandPlanResult(
            applied=False,
            reason="no_expandable_dimensions",
            plan=ExecutionPlan(plan_version=plan_version, planning_mode="spec_driven"),
            candidate_set=candidates.to_dict(),
            coverage_contract=contract,
        )
    plan = ExecutionPlan(
        steps=steps,
        summary=f"Deep-dive plan for candidate set {candidates.candidate_set_id}",
        plan_version=plan_version + 1,
        planning_mode="candidate_expansion",
    )
    proposal = PlanMutationProposal(
        mutation_type="expand_plan",
        proposed_plan=plan,
        proposed_candidate_set=selected_candidates.to_dict(),
        proposed_coverage_contract=contract,
        metadata={"reason": "candidate_deep_dive_created"},
    )
    return ExpandPlanResult(
        applied=True,
        reason="candidate_deep_dive_created",
        plan=plan,
        candidate_set=selected_candidates.to_dict(),
        coverage_contract=contract,
        proposal=proposal,
    )


__all__ = ["ExpandPlanResult", "expand_plan"]

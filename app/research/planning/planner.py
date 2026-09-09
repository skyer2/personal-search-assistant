"""Spec-driven planner for the current semantic phase."""

from __future__ import annotations

import re
from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.coverage.models import CoverageContract
from app.research.planning.candidate import CandidateSet
from app.research.spec.models import ResearchSpec


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(value)).strip("_").lower()
    return text[:48] or "x"


def _source_contract(spec: ResearchSpec) -> tuple[list[str], list[str]]:
    allowed_sources = [str(item) for item in spec.source_policy.allowed if str(item).strip()] or ["web"]
    allowed_tools: list[str] = []
    if "web" in allowed_sources:
        allowed_tools.extend(["web_search", "fetch"])
    if "file" in allowed_sources:
        allowed_tools.append("read_file_content")
    if not allowed_tools:
        raise ValueError("spec_has_no_allowed_sources")
    return allowed_sources, allowed_tools


def _step(
    *,
    task_id: str,
    objective: str,
    subject_id: str,
    coverage_ids: list[str],
    dimensions: list[str],
    task_kind: str,
    allowed_sources: list[str],
    allowed_tools: list[str],
    extra_metadata: dict[str, Any] | None = None,
) -> PlanStep:
    metadata: dict[str, Any] = {
        "allowed_sources": allowed_sources,
        "kind": "research_task",
        "task_kind": task_kind,
        "subject_id": subject_id,
        "coverage_keys": dimensions,
        "coverage_ids": coverage_ids,
        "required": True,
        "optional": False,
        "priority": "P0" if task_kind == "discovery" else "P1",
    }
    metadata.update(extra_metadata or {})
    return PlanStep(
        step_type="research",
        description=objective,
        subagent="研究工人",
        task_id=task_id,
        allowed_tools=allowed_tools,
        objective=objective,
        metadata=metadata,
    )


def plan_for_spec(
    spec: ResearchSpec | dict[str, Any] | None,
    coverage_contract: CoverageContract | dict[str, Any] | None = None,
    *,
    candidate_set: dict[str, Any] | None = None,
    plan_version: int = 1,
) -> ExecutionPlan:
    value = spec if isinstance(spec, ResearchSpec) else ResearchSpec.from_dict(spec)
    contract = (
        coverage_contract
        if isinstance(coverage_contract, CoverageContract)
        else CoverageContract.from_dict(coverage_contract)
    )
    steps: list[PlanStep] = []
    allowed_sources, allowed_tools = _source_contract(value)
    if value.task_shape in {"BREADTH_HEAVY", "DYNAMIC_DISCOVERY"} and value.reasoning_requirements.discovery:
        candidates = CandidateSet.from_dict(candidate_set or {})
        if not candidates.available:
            subject_id = value.subjects[0].subject_id if value.subjects else "general"
            discovery_units = [unit for unit in contract.units if unit.dimension_id == "candidate_set"]
            steps.append(
                _step(
                    task_id="t_discovery",
                    objective=f"Discovery：为「{value.objective}」识别并排序高相关候选",
                    subject_id=subject_id,
                    coverage_ids=[unit.coverage_id for unit in discovery_units],
                    dimensions=["candidate_set"],
                    task_kind="discovery",
                    allowed_sources=allowed_sources,
                    allowed_tools=allowed_tools,
                    extra_metadata={"produces_artifact": "candidate_set", "target_items": 8},
                )
            )
        else:
            steps.extend(_candidate_steps(value, contract, candidates, expanded=False))
    else:
        for unit in contract.units:
            steps.append(
                _step(
                    task_id=f"t_{_slug(unit.subject_id)}_{_slug(unit.dimension_id)}",
                    objective=f"收集 {unit.subject_id} / {unit.dimension_id} 的可靠证据",
                    subject_id=unit.subject_id,
                    coverage_ids=[unit.coverage_id],
                    dimensions=[unit.dimension_id],
                    task_kind="deep_dive",
                    allowed_sources=allowed_sources,
                    allowed_tools=allowed_tools,
                )
            )
    return ExecutionPlan(
        steps=steps[:12],
        summary=f"Research plan for {value.spec_id}",
        plan_version=plan_version,
        planning_mode="spec_driven",
    )


def _candidate_steps(
    spec: ResearchSpec,
    contract: CoverageContract,
    candidates: CandidateSet,
    *,
    expanded: bool,
) -> list[PlanStep]:
    steps: list[PlanStep] = []
    allowed_sources, allowed_tools = _source_contract(spec)
    for candidate in candidates.candidates[:8]:
        for dimension in spec.dimensions:
            if dimension.dimension_id == "candidate_set":
                continue
            units = [
                unit
                for unit in contract.units
                if unit.subject_id == f"candidate:{candidate.name}"
                and unit.dimension_id == dimension.dimension_id
            ]
            if not units:
                continue
            steps.append(
                _step(
                    task_id=f"t_{_slug(candidate.name)}_{_slug(dimension.dimension_id)}",
                    objective=f"深挖 {candidate.name} 的 {dimension.name}，并给出可引用证据",
                    subject_id=f"candidate:{candidate.name}",
                    coverage_ids=[unit.coverage_id for unit in units],
                    dimensions=[dimension.dimension_id],
                    task_kind="deep_dive",
                    allowed_sources=allowed_sources,
                    allowed_tools=allowed_tools,
                    extra_metadata={
                        "candidate_id": candidate.candidate_id,
                        "candidate_set_id": candidates.candidate_set_id,
                        "expanded_from_candidate_set": expanded,
                    },
                )
            )
    return steps[:12]


__all__ = ["plan_for_spec"]

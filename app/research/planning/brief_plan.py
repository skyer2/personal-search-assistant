"""Brief + initial bounded plan contract.

The first plan is deliberately deterministic.  It is derived from the
canonical brief after the single brief compiler call, so the graph never pays
for an initial ``Brief -> Supervisor -> Plan`` round trip.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.brief.models import StructuredResearchBrief
from app.research.planning.bounded import (
    DEFAULT_WORKER_QUERY_BUDGET,
    MAX_DIMENSIONS,
    MAX_ENTITIES,
    MAX_ESTIMATED_QUERIES,
    split_task,
    validate_plan as validate_bounded_plan,
)


@dataclass(frozen=True)
class ResearchTaskSpec:
    task_id: str
    question_id: str
    objective: str
    hypothesis: str
    evidence_needed: tuple[str, ...] = ()
    counter_evidence_needed: tuple[str, ...] = ()
    search_hints: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    max_queries: int = MAX_ESTIMATED_QUERIES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchBriefAndPlan:
    brief: StructuredResearchBrief
    tasks: tuple[ResearchTaskSpec, ...] = field(default_factory=tuple)
    source: str = "deterministic_after_brief"

    def to_dict(self) -> dict[str, Any]:
        return {"brief": self.brief.to_dict(), "tasks": [task.to_dict() for task in self.tasks], "source": self.source}


@dataclass(frozen=True)
class PlanCoverage:
    """Deterministic mapping from every key question to an initial task.

    Coverage is a planning invariant, not a best-effort diagnostic.  A
    question that is absent from the first wave cannot be silently delegated
    to an already constrained repair wave.
    """

    required_question_ids: tuple[str, ...]
    covered_question_ids: tuple[str, ...]
    uncovered_question_ids: tuple[str, ...]
    duplicate_question_ids: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.uncovered_question_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_question_ids": list(self.required_question_ids),
            "covered_question_ids": list(self.covered_question_ids),
            "uncovered_question_ids": list(self.uncovered_question_ids),
            "duplicate_question_ids": list(self.duplicate_question_ids),
            "complete": self.complete,
            "coverage_ratio": (
                len(self.covered_question_ids) / len(self.required_question_ids)
                if self.required_question_ids else 1.0
            ),
        }


def _analysis_type(brief: StructuredResearchBrief) -> bool:
    return brief.user_intent in {
        "comparison", "trend_forecast", "conflict_analysis", "recommendation", "structured_report", "explanation"
    }


def build_brief_and_plan(brief: StructuredResearchBrief, *, plan_version: int = 1) -> ResearchBriefAndPlan:
    # Every key question must be represented in the initial bounded DAG.
    # Parallelism is controlled at dispatch time; truncating questions here
    # turns a known delivery requirement into an impossible repair request.
    questions = list(brief.key_questions or (brief.objective,))
    entities = tuple(brief.explicit_subjects[:MAX_ENTITIES])
    if not entities:
        entities = (brief.objective[:100],)
    analysis = _analysis_type(brief)
    tasks: list[ResearchTaskSpec] = []
    for index, question in enumerate(questions, 1):
        qid = f"q{index}"
        dimensions = (question[:120],)
        evidence = tuple(brief.source_requirements.preferred[:2]) or ("reliable source",)
        counter = ("counter-evidence or competing estimate",) if analysis else ()
        tasks.append(
            ResearchTaskSpec(
                task_id=f"plan_{plan_version}_{index}",
                question_id=qid,
                objective=question,
                hypothesis=f"可通过来源证据回答：{question}",
                evidence_needed=evidence,
                counter_evidence_needed=counter,
                search_hints=(brief.objective[:180],),
                entities=entities,
                dimensions=dimensions,
                # Each focused task uses explicit primary/support/counter
                # lanes.  This preserves uncertainty handling without making
                # a worker a miniature open-ended research system.
                max_queries=min(MAX_ESTIMATED_QUERIES, 3 if len(questions) > 1 else 4),
            )
        )
    return ResearchBriefAndPlan(brief=brief, tasks=tuple(tasks))


def execution_plan_from_brief(
    brief: StructuredResearchBrief, *, plan_version: int = 1, query_budget: int = DEFAULT_WORKER_QUERY_BUDGET
) -> ExecutionPlan:
    from app.research.workers.registry import worker_tools_for_step
    from app.research.runtime.task_budget import task_budget_metadata, task_budget_profile

    contract = build_brief_and_plan(brief, plan_version=plan_version)
    analysis = _analysis_type(brief)
    steps: list[PlanStep] = []
    for task in contract.tasks:
        profile = task_budget_profile("medium")
        step = PlanStep(
            step_type="research",
            description=task.objective,
            objective=task.objective,
            task_id=task.task_id,
            allowed_tools=worker_tools_for_step("research"),
            metadata={
                "kind": "research_task",
                "task_kind": "initial_bounded_plan",
                "question_id": task.question_id,
                "hypothesis": task.hypothesis,
                "hypothesis_id": f"h_{task.question_id}",
                "criterion_id": task.objective,
                "target_criteria": [task.objective],
                "target_gaps": [task.objective],
                "evidence_needed": list(task.evidence_needed),
                "counter_evidence_needed": list(task.counter_evidence_needed),
                "source_strategy": ["primary_source", "independent_corroboration", "counter_evidence"] if analysis else ["primary_source", "independent_corroboration"],
                "research_lanes": ["primary_source", "supporting_evidence", "counter_evidence"] if analysis else ["primary_source", "supporting_evidence"],
                "search_hints": list(task.search_hints),
                "entities": list(task.entities[:MAX_ENTITIES]),
                "dimensions": list(task.dimensions[:MAX_DIMENSIONS]),
                "estimated_queries": min(task.max_queries, max(1, int(query_budget * 0.7))),
                "max_queries": min(task.max_queries, max(1, int(query_budget * 0.7))),
                "analysis_type": brief.user_intent,
                "token_ceiling": profile.token_ceiling,
                "max_llm_calls": profile.max_llm_calls,
                **task_budget_metadata(profile),
                "required": True,
                "optional": False,
            },
        )
        steps.extend(split_task(step, query_budget=query_budget))
    plan = ExecutionPlan(
        steps=steps,
        summary="Bounded initial research plan derived from brief",
        plan_version=plan_version,
        planning_mode="brief_and_plan",
        research_brief=brief.objective,
    )
    return plan


def plan_coverage(plan: ExecutionPlan, brief: StructuredResearchBrief) -> PlanCoverage:
    required = tuple(f"q{index}" for index, _ in enumerate(brief.key_questions or (brief.objective,), 1))
    counts: dict[str, int] = {}
    for step in plan.steps:
        question_id = str((step.metadata or {}).get("question_id") or "").strip()
        if question_id:
            counts[question_id] = counts.get(question_id, 0) + 1
    covered = tuple(question_id for question_id in required if counts.get(question_id, 0) > 0)
    return PlanCoverage(
        required_question_ids=required,
        covered_question_ids=covered,
        uncovered_question_ids=tuple(question_id for question_id in required if not counts.get(question_id, 0)),
        duplicate_question_ids=tuple(question_id for question_id in required if counts.get(question_id, 0) > 1),
    )


def validate_brief_plan(
    plan: ExecutionPlan,
    *,
    query_budget: int = DEFAULT_WORKER_QUERY_BUDGET,
    brief: StructuredResearchBrief | None = None,
) -> list[dict[str, str]]:
    issues = [issue.to_dict() for issue in validate_bounded_plan(plan.steps, query_budget=query_budget)]
    for step in plan.steps:
        metadata = step.metadata or {}
        if not str(metadata.get("question_id") or "").strip():
            issues.append({"code": "missing_question_id", "task_id": step.task_id, "detail": ""})
        if not str(metadata.get("hypothesis") or "").strip():
            issues.append({"code": "missing_hypothesis", "task_id": step.task_id, "detail": ""})
        analysis = str(metadata.get("analysis_type") or "")
        if analysis in {"comparison", "trend_forecast", "conflict_analysis", "recommendation", "structured_report", "explanation"} and not metadata.get("counter_evidence_needed"):
            issues.append({"code": "missing_counter_evidence", "task_id": step.task_id, "detail": analysis})
        if analysis and not metadata.get("research_lanes"):
            issues.append({"code": "missing_research_lanes", "task_id": step.task_id, "detail": analysis})
    if brief is not None:
        coverage = plan_coverage(plan, brief)
        for question_id in coverage.uncovered_question_ids:
            issues.append({"code": "uncovered_key_question", "task_id": "", "detail": question_id})
    return issues


__all__ = [
    "PlanCoverage", "ResearchBriefAndPlan", "ResearchTaskSpec", "build_brief_and_plan",
    "execution_plan_from_brief", "plan_coverage", "validate_brief_plan",
]

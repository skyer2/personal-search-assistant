"""Initial semantic planner and semantic plan validator.

The initial plan is not a mechanical ``key_question -> task`` expansion.  A
single control-plane model proposes the first research tasks, then this module
binds every task to ``ask_id`` / ``question_id`` and validates that the task is
specific, searchable, non-duplicative and preserves subject/time scope.

The repair Supervisor remains a separate authority and consumes CoverageGap
objects after the first wave.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Any

from app.research.brief.models import ResearchQuestion, StructuredResearchBrief
from app.research.supervisor.agent import SupervisorAgent
from app.research.supervisor.models import ResearchTaskRequest, SupervisorAction

_BROAD_GENERIC = {
    "当前主要关注点和工程路径是什么",
    "未来一段时间可验证的进展有哪些",
    "这些判断的主要不确定性和依据是什么",
}


def _norm(value: str) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def _contains_preserved(value: str, required: str) -> bool:
    target = _norm(required)
    if not target:
        return True
    blob = _norm(value)
    if target in blob:
        return True
    return sum(1 for ch in target if ch in blob) / max(1, len(target)) >= 0.75


@dataclass(frozen=True)
class PlanSemanticIssue:
    code: str
    task_index: int = -1
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "task_index": self.task_index,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PlanSemanticResult:
    passed: bool
    issues: tuple[PlanSemanticIssue, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": [item.to_dict() for item in self.issues],
        }


class PlanSemanticValidator:
    """Validate semantic legality, not merely structural shape."""

    def validate(
        self,
        brief: StructuredResearchBrief,
        tasks: tuple[ResearchTaskRequest, ...] | list[ResearchTaskRequest],
    ) -> PlanSemanticResult:
        issues: list[PlanSemanticIssue] = []
        ask_by_id = {ask.ask_id: ask for ask in brief.user_asks}
        question_by_id = {row.question_id: row for row in brief.research_questions}
        seen: list[str] = []

        for index, task in enumerate(tasks):
            objective = str(task.objective or "").strip()
            if not task.ask_id or task.ask_id not in ask_by_id:
                issues.append(PlanSemanticIssue("missing_or_invalid_ask_id", index, task.ask_id))
            if not task.question_id or task.question_id not in question_by_id:
                issues.append(
                    PlanSemanticIssue("missing_or_invalid_question_id", index, task.question_id)
                )
            if len(objective) < 8:
                issues.append(PlanSemanticIssue("objective_too_short", index, objective))
            if _norm(objective) in {_norm(item) for item in _BROAD_GENERIC}:
                issues.append(PlanSemanticIssue("generic_objective", index, objective))

            ask = ask_by_id.get(task.ask_id)
            if ask is not None:
                if ask.subject and not _contains_preserved(objective, ask.subject):
                    issues.append(
                        PlanSemanticIssue("subject_not_preserved", index, ask.subject)
                    )
                if ask.time_scope and not _contains_preserved(objective, ask.time_scope):
                    issues.append(
                        PlanSemanticIssue("time_scope_not_preserved", index, ask.time_scope)
                    )

            normalized = _norm(objective)
            if any(SequenceMatcher(None, normalized, previous).ratio() >= 0.9 for previous in seen):
                issues.append(PlanSemanticIssue("duplicate_task", index, objective))
            seen.append(normalized)

        required = {ask.ask_id for ask in brief.user_asks if ask.required}
        covered = {task.ask_id for task in tasks if task.ask_id}
        for ask_id in sorted(required - covered):
            issues.append(PlanSemanticIssue("required_ask_unplanned", -1, ask_id))
        return PlanSemanticResult(not issues, tuple(issues))


def _bind_task(
    task: ResearchTaskRequest,
    question: ResearchQuestion,
) -> ResearchTaskRequest:
    criteria = task.target_criteria or (question.text,)
    return replace(
        task,
        ask_id=question.ask_id,
        question_id=question.question_id,
        target_criteria=criteria,
    )


def _best_question(
    objective: str,
    questions: tuple[ResearchQuestion, ...],
) -> ResearchQuestion | None:
    if not questions:
        return None
    normalized = _norm(objective)
    return max(
        questions,
        key=lambda row: SequenceMatcher(None, normalized, _norm(row.text)).ratio(),
    )


def bind_plan_lineage(
    brief: StructuredResearchBrief,
    tasks: tuple[ResearchTaskRequest, ...],
) -> tuple[ResearchTaskRequest, ...]:
    """Attach ask/question lineage and restore any dropped required ask."""
    questions = brief.research_questions or tuple(
        ResearchQuestion(f"q{index}", "", text)
        for index, text in enumerate(brief.key_questions, 1)
    )
    bound: list[ResearchTaskRequest] = []
    for task in tasks:
        question = next(
            (row for row in questions if row.question_id == task.question_id),
            None,
        )
        if question is None:
            question = _best_question(task.objective, questions)
        bound.append(_bind_task(task, question) if question is not None else task)

    covered = {item.ask_id for item in bound if item.ask_id}
    for question in questions:
        if question.ask_id and question.ask_id in covered:
            continue
        bound.append(
            ResearchTaskRequest(
                objective=question.text,
                ask_id=question.ask_id,
                question_id=question.question_id,
                target_criteria=(question.text,),
                target_gaps=(question.text,),
                priority="high",
                expected_evidence=("一手来源", "高质量独立来源"),
                novelty_reason="覆盖尚未规划的原始用户 Ask",
                estimated_effort="medium",
            )
        )
        covered.add(question.ask_id)
    return tuple(bound)


class SemanticPlanner:
    """One-shot semantic planner for the initial research wave."""

    def __init__(self, agent: Any | None, budget_manager: Any | None = None):
        self.supervisor = SupervisorAgent(agent, budget_manager)
        self.validator = PlanSemanticValidator()

    async def plan(
        self,
        brief: StructuredResearchBrief,
        *,
        findings: list[dict[str, Any]] | None = None,
        budget: dict[str, Any] | None = None,
        previous_fingerprints: set[str] | None = None,
    ) -> tuple[SupervisorAction, PlanSemanticResult]:
        action = await self.supervisor.decide(
            brief,
            findings or [],
            None,
            budget or {},
            previous_fingerprints=previous_fingerprints,
        )
        tasks = bind_plan_lineage(brief, action.research_tasks)
        validation = self.validator.validate(brief, tasks)
        if not validation.passed:
            # Fail closed to exact query-preserving research questions.
            tasks = bind_plan_lineage(brief, ())
            validation = self.validator.validate(brief, tasks)
        return (
            SupervisorAction(
                "CONDUCT_RESEARCH",
                action.reason or "initial semantic plan",
                tasks,
                action.source,
            ),
            validation,
        )


__all__ = [
    "PlanSemanticIssue",
    "PlanSemanticResult",
    "PlanSemanticValidator",
    "SemanticPlanner",
    "bind_plan_lineage",
]

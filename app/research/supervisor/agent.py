"""Lead researcher supervisor and the only semantic strategy authority."""

from __future__ import annotations

import json
import time
from typing import Any

from app.research.brief.models import StructuredResearchBrief
from app.research.coverage.judge import CoverageJudgement
from app.research.execution.structured_llm_gateway import (
    StructuredLLMGateway,
    emit_semantic_fallback,
)
from app.research.runtime.task_budget import task_budget_profile
from app.research.runtime.task_identity import semantic_fingerprint
from app.research.supervisor.models import ResearchTaskRequest, SupervisorAction
from app.research.supervisor.prompt import SUPERVISOR_PROMPT


class SupervisorAgent:
    def __init__(self, agent: Any | None, budget_manager: Any | None = None):
        self.agent = agent
        self.budget_manager = budget_manager

    def _task(
        self,
        index: int,
        objective: str,
        *,
        target_criteria: tuple[str, ...] = (),
        target_gaps: tuple[str, ...] = (),
    ) -> ResearchTaskRequest:
        profile = task_budget_profile("small" if index > 2 else "medium")
        return ResearchTaskRequest(
            objective=objective,
            target_criteria=target_criteria,
            target_gaps=target_gaps,
            priority="high" if index == 1 else "normal",
            expected_evidence=("一手来源", "高质量独立来源"),
            novelty_reason="针对当前 Coverage 缺口收敛研究范围",
            estimated_effort="small" if index > 2 else "medium",
        )

    def fallback_action(
        self,
        brief: StructuredResearchBrief,
        judgement: CoverageJudgement | None,
        budget: dict[str, Any] | None,
        previous_fingerprints: set[str] | None = None,
    ) -> SupervisorAction:
        if bool((budget or {}).get("exhausted")):
            return SupervisorAction("COMPLETE", "budget exhausted; synthesize available evidence")
        if judgement is not None and judgement.sufficient:
            return SupervisorAction("COMPLETE", "coverage meets brief success criteria")
        candidates = self._actionable_questions(brief, judgement)
        criteria = tuple(brief.success_criteria or brief.key_questions)
        known = set(previous_fingerprints or set())
        tasks: list[ResearchTaskRequest] = []
        for index, objective in enumerate(candidates[:2], start=1):
            gap = judgement.missing[index - 1] if judgement and index <= len(judgement.missing) else objective
            task = self._task(
                index,
                objective,
                target_criteria=(criteria[index - 1],) if criteria else (),
                target_gaps=(gap,),
            )
            fingerprint = semantic_fingerprint(
                objective=task.objective,
                target_gaps=task.target_gaps,
                target_criteria=task.target_criteria,
            )
            if fingerprint not in known:
                tasks.append(task)
                known.add(fingerprint)
        return SupervisorAction(
            "CONDUCT_RESEARCH",
            judgement.reason if judgement and judgement.reason else "brief questions are not yet covered",
            tuple(tasks),
        )

    def resolve_action(
        self,
        action: SupervisorAction,
        judgement: CoverageJudgement | None = None,
        brief: StructuredResearchBrief | None = None,
        previous_fingerprints: set[str] | None = None,
    ) -> SupervisorAction:
        if action.action != "CONDUCT_RESEARCH" or action.research_tasks:
            return action
        questions = self._actionable_questions(brief, judgement)
        resolved = self.fallback_action(
            brief or StructuredResearchBrief("", 1, "", "research"),
            judgement,
            {},
            previous_fingerprints=previous_fingerprints,
        )
        return SupervisorAction(action.action, action.reason, resolved.research_tasks, action.source)

    @staticmethod
    def _sanitize_action(action: SupervisorAction) -> SupervisorAction:
        tasks: list[ResearchTaskRequest] = []
        for item in action.research_tasks:
            profile = task_budget_profile(item.estimated_effort)
            tasks.append(
                ResearchTaskRequest(
                    objective=item.objective,
                    target_criteria=item.target_criteria,
                    target_gaps=item.target_gaps,
                    priority=item.priority,
                    expected_evidence=item.expected_evidence,
                    source_hints=item.source_hints,
                    novelty_reason=item.novelty_reason,
                    estimated_effort=item.estimated_effort,
                    task_id="",
                )
            )
        return SupervisorAction(
            action=action.action,
            reason=action.reason,
            research_tasks=tuple(tasks),
            source="structured_llm",
        )

    @staticmethod
    def _actionable_questions(
        brief: StructuredResearchBrief | None,
        judgement: CoverageJudgement | None,
    ) -> tuple[str, ...]:
        if judgement and judgement.recommended_next_questions:
            return tuple(judgement.recommended_next_questions)
        if judgement and judgement.missing:
            return tuple(judgement.missing)
        return tuple(brief.key_questions) if brief else ()

    async def decide(
        self,
        brief: StructuredResearchBrief,
        findings: list[dict[str, Any]],
        judgement: CoverageJudgement | None,
        budget: dict[str, Any],
        *,
        research_history: str = "",
        previous_fingerprints: set[str] | None = None,
        duplicate_search_ratio: float = 0.0,
    ) -> SupervisorAction:
        fallback = self.fallback_action(
            brief,
            judgement,
            budget,
            previous_fingerprints=previous_fingerprints,
        )
        if self.agent is None:
            return fallback
        started = time.perf_counter()
        prompt = SUPERVISOR_PROMPT.format(
            brief=json.dumps(brief.to_dict(), ensure_ascii=False, indent=2),
            findings=json.dumps(findings[:24], ensure_ascii=False, indent=2),
            coverage=json.dumps(judgement.to_dict() if judgement else {}, ensure_ascii=False, indent=2),
            budget=json.dumps(budget, ensure_ascii=False, indent=2),
            value_signal=json.dumps(
                {
                    "evidence_count": len(findings),
                    "supported_criteria": [
                        row.get("supported_criteria")
                        for row in findings
                        if isinstance(row, dict) and row.get("supported_criteria")
                    ],
                },
                ensure_ascii=False,
            ),
            previous_fingerprints=sorted(previous_fingerprints or [])[-32:],
            duplicate_search_ratio=f"{max(0.0, min(1.0, float(duplicate_search_ratio))):.2%}",
        )
        try:
            gateway = StructuredLLMGateway(self.budget_manager)
            with gateway.gateway.execution_scope(phase="supervisor"):
                action = await gateway.ainvoke(
                    model=self.agent,
                    schema=SupervisorAction,
                    prompt=prompt,
                    phase="supervisor",
                    timeout_sec=30,
                )
        except Exception as exc:
            emit_semantic_fallback(
                phase="supervisor",
                component="StructuredLLMGateway",
                fallback="deterministic",
                exc=exc,
                schema=SupervisorAction,
                model=self.agent,
                started=started,
            )
            return fallback
        action = self._sanitize_action(action)
        return self.resolve_action(action, judgement, brief, previous_fingerprints)


__all__ = ["SupervisorAgent"]

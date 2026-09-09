"""Lead researcher supervisor and the only semantic strategy authority."""

from __future__ import annotations

import json
import re
from typing import Any

from app.api.tracing import build_run_config
from app.research.brief.models import StructuredResearchBrief
from app.research.coverage.judge import CoverageJudgement
from app.research.execution.llm_gateway import LLMGateway
from app.research.supervisor.models import ResearchTaskRequest, SupervisorAction
from app.research.supervisor.prompt import SUPERVISOR_PROMPT


class SupervisorAgent:
    def __init__(self, agent: Any | None, budget_manager: Any | None = None):
        self.agent = agent
        self.budget_manager = budget_manager

    def _task(self, index: int, objective: str) -> ResearchTaskRequest:
        return ResearchTaskRequest(
            task_id=f"supervisor_task_{index}",
            objective=objective,
            priority="high" if index == 1 else "normal",
            expected_evidence="一手来源或高质量独立来源",
        )

    def fallback_action(
        self,
        brief: StructuredResearchBrief,
        judgement: CoverageJudgement | None,
        budget: dict[str, Any] | None,
    ) -> SupervisorAction:
        if bool((budget or {}).get("exhausted")):
            return SupervisorAction("COMPLETE", "budget exhausted; synthesize available evidence")
        if judgement is not None and judgement.sufficient:
            return SupervisorAction("COMPLETE", "coverage meets brief success criteria")
        candidates = self._actionable_questions(brief, judgement)
        objectives = list(dict.fromkeys(item for item in candidates if str(item).strip()))
        tasks = tuple(self._task(index, objective) for index, objective in enumerate(objectives[:4], start=1))
        return SupervisorAction(
            "CONDUCT_RESEARCH",
            judgement.reason if judgement and judgement.reason else "brief questions are not yet covered",
            tasks,
        )

    def resolve_action(
        self,
        action: SupervisorAction,
        judgement: CoverageJudgement | None = None,
        brief: StructuredResearchBrief | None = None,
    ) -> SupervisorAction:
        if action.action != "CONDUCT_RESEARCH" or action.research_tasks:
            return action
        questions = self._actionable_questions(brief, judgement)
        objectives = list(dict.fromkeys(item for item in questions if str(item).strip()))
        tasks = tuple(self._task(index, objective) for index, objective in enumerate(objectives[:4], start=1))
        return SupervisorAction(action.action, action.reason, tasks, action.source)

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
    ) -> SupervisorAction:
        fallback = self.fallback_action(brief, judgement, budget)
        if self.agent is None:
            return fallback
        prompt = SUPERVISOR_PROMPT.format(
            brief=json.dumps(brief.to_dict(), ensure_ascii=False, indent=2),
            findings=json.dumps(findings[:24], ensure_ascii=False, indent=2),
            coverage=json.dumps(judgement.to_dict() if judgement else {}, ensure_ascii=False, indent=2),
            budget=json.dumps(budget, ensure_ascii=False, indent=2),
        )
        texts: list[str] = []
        try:
            gateway = LLMGateway(self.budget_manager)
            config = build_run_config("supervisor", metadata={"phase": "supervisor"})
            with gateway.execution_scope(phase="supervisor"):
                async for chunk in gateway.astream(self.agent, {"messages": [{"role": "user", "content": prompt}]}, config):
                    if isinstance(chunk, dict):
                        states = list(chunk.values()) if len(chunk) == 1 else [chunk]
                        for state in states:
                            if not isinstance(state, dict):
                                continue
                            for message in state.get("messages") or []:
                                content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
                                texts.append(str(content or ""))
        except Exception:
            return fallback
        raw = "\n".join(texts).strip()
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return fallback
        try:
            patch = json.loads(match.group(0))
        except json.JSONDecodeError:
            return fallback
        if not isinstance(patch, dict):
            return fallback
        action = SupervisorAction.from_dict({**patch, "source": "structured_llm"})
        return self.resolve_action(action, judgement, brief)


__all__ = ["SupervisorAgent"]

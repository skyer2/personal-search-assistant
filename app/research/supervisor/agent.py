"""Lead researcher supervisor and the only semantic strategy authority."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from app.config.timeouts import model_timeout_sec
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

    @staticmethod
    def _compact_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build a bounded routing view instead of sending raw worker data."""
        compact: list[dict[str, Any]] = []
        per_criterion: dict[str, int] = {}
        for finding in findings:
            criteria = [
                str(item)
                for item in finding.get("supported_criteria") or []
                if str(item).strip()
            ]
            criterion = criteria[0] if criteria else "general"
            if per_criterion.get(criterion, 0) >= 3:
                continue
            per_criterion[criterion] = per_criterion.get(criterion, 0) + 1
            compact.append(
                {
                    "finding_id": str(finding.get("finding_id") or ""),
                    "claim": str(
                        finding.get("claim") or finding.get("summary") or ""
                    )[:800],
                    "supported_criteria": criteria[:3],
                    "evidence_ids": [
                        str(item) for item in finding.get("evidence_ids") or []
                    ][:5],
                    "confidence": finding.get("confidence"),
                    "partial": bool(finding.get("partial")),
                }
            )
            if len(compact) >= 12:
                break
        return compact

    @staticmethod
    def _requires_semantic_decision(
        judgement: CoverageJudgement | None,
        budget: dict[str, Any],
    ) -> bool:
        # Runtime policy can decide these branches without a provider round
        # trip. This is especially important after a budget stop.
        if bool(budget.get("exhausted")):
            return False
        if judgement is not None and judgement.sufficient and not judgement.gaps:
            return False
        return True

    def _task(
        self,
        index: int,
        objective: str,
        *,
        ask_id: str = "",
        question_id: str = "",
        target_criteria: tuple[str, ...] = (),
        target_gaps: tuple[str, ...] = (),
        criterion_id: str = "",
        gap_id: str = "",
        missing_evidence_types: tuple[str, ...] = (),
        blocking_conflict_ids: tuple[str, ...] = (),
    ) -> ResearchTaskRequest:
        profile = task_budget_profile("small" if index > 2 else "medium")
        evidence_by_gap = {
            "supporting_finding": "至少一条直接回答该研究问题、含明确主张和来源绑定的 finding",
            "supporting_evidence": "为该 finding 抓取至少一条独立来源原文或可核验摘录",
            "evidence": "直接支持该问题结论的可核验来源证据",
            "independent_source": "至少一个与现有来源相互独立的 corroborating source",
            "primary_source": "primary source / 一手来源；必要时补充高质量独立来源",
            "fresh_evidence": "带发布日期的近期来源，足以验证当前状态",
            "conflict_resolution": "针对同一口径的权威来源，说明数据范围与日期",
            "mechanism_evidence": "来源明确陈述或可逐句验证的因果机制证据；不得把相关性写成因果",
            "worker_failed": "完成该问题的结构化 finding；已有证据不得只留在工具结果中",
            "direct_criterion_binding": "直接回答该标准并把 claim 绑定到 canonical evidence ID",
        }
        targeted_evidence = tuple(
            evidence_by_gap[item]
            for item in missing_evidence_types
            if item in evidence_by_gap
        )
        evidence = targeted_evidence or ("一手来源", "高质量独立来源")
        if "mechanism_evidence" in missing_evidence_types:
            evidence += ("来源明确支持的机制证据；若没有直接因果证据，记录为未知，不作推断",)
        if "counter_evidence" in missing_evidence_types:
            evidence += ("反向信号或竞争性解释及其来源",)
        return ResearchTaskRequest(
            objective=objective,
            ask_id=ask_id,
            question_id=question_id,
            target_criteria=target_criteria,
            target_gaps=target_gaps,
            criterion_id=criterion_id,
            hypothesis_id=f"repair:{criterion_id or question_id}",
            gap_id=gap_id,
            missing_evidence_types=missing_evidence_types,
            blocking_conflict_ids=blocking_conflict_ids,
            priority="high" if index == 1 else "normal",
            expected_evidence=evidence,
            novelty_reason="针对当前 Coverage 缺口收敛研究范围",
            repair_id=f"repair:{gap_id or question_id or index}",
            gap_reason="；".join(missing_evidence_types) or "Coverage 尚未达到问题证据要求",
            missing_evidence=evidence,
            estimated_effort="small" if index > 2 else "medium",
            repair=True,
            max_queries=5,
            max_fetches=5,
            # One focused retrieval loop needs a planning/tool round and one
            # reserved finalize-only structured-output round. Two calls can
            # exhaust the lease immediately after retrieval and strand all
            # recovered evidence as non-claims.
            max_llm_calls=min(4, profile.max_llm_calls),
        )

    def fallback_action(
        self,
        brief: StructuredResearchBrief,
        judgement: CoverageJudgement | None,
        budget: dict[str, Any] | None,
        previous_fingerprints: set[str] | None = None,
    ) -> SupervisorAction:
        coverage_sufficient = (
            judgement is not None and judgement.sufficient and not judgement.gaps
        )
        if coverage_sufficient:
            return SupervisorAction("COMPLETE", "coverage meets brief key questions")
        if bool((budget or {}).get("exhausted")):
            # Budget exhaustion is not research completion. Coverage still has a
            # gap, so stop explicitly and let delivery degrade to PARTIAL.
            return SupervisorAction(
                "STOP_BUDGET_PARTIAL",
                "budget exhausted while coverage gaps remain",
            )
        if judgement is not None and judgement.gaps:
            known = set(previous_fingerprints or set())
            tasks: list[ResearchTaskRequest] = []
            priority_rank = {"high": 0, "medium": 1, "low": 2}
            ordered_gaps = sorted(
                judgement.gaps,
                key=lambda item: (
                    priority_rank.get(item.priority, 1),
                    0 if item.blocking_conflict_ids else 1,
                    item.criterion_id,
                ),
            )
            for index, gap in enumerate(ordered_gaps[:2], start=1):
                question_id = gap.question_id
                question_index = (
                    int(question_id[1:])
                    if question_id.startswith("q") and question_id[1:].isdigit()
                    else 0
                )
                question_text = (
                    brief.key_questions[question_index - 1]
                    if 0 < question_index <= len(brief.key_questions)
                    else gap.description
                )
                missing_scope = "、".join(gap.missing_evidence_type) or gap.description
                repair_objective = (
                    f"针对研究问题「{question_text}」，只补足以下缺口：{missing_scope}。"
                    "复用已有证据，避免重做已覆盖部分；必须返回带 canonical evidence 绑定的结构化 finding。"
                )
                task = self._task(
                    index,
                    repair_objective,
                    ask_id=brief.ask_id_for_question_index(question_index),
                    target_criteria=(gap.criterion_id,),
                    target_gaps=(gap.description,),
                    criterion_id=gap.criterion_id,
                    question_id=question_id,
                    gap_id=gap.gap_id,
                    missing_evidence_types=gap.missing_evidence_type,
                    blocking_conflict_ids=gap.blocking_conflict_ids,
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
                judgement.reason if judgement.reason else "structured coverage gaps remain",
                tuple(tasks),
            )
        candidates = self._actionable_questions(brief, judgement)
        known = set(previous_fingerprints or set())
        fallback_tasks: list[ResearchTaskRequest] = []
        for index, objective in enumerate(candidates[:2], start=1):
            question_index = next(
                (
                    position
                    for position, question in enumerate(brief.key_questions, 1)
                    if question == objective
                ),
                0,
            )
            task = self._task(
                index,
                objective,
                ask_id=brief.ask_id_for_question_index(question_index),
                question_id=f"q{question_index}" if question_index else "",
                target_criteria=(objective,),
                target_gaps=(objective,),
            )
            fingerprint = semantic_fingerprint(
                objective=task.objective,
                target_gaps=task.target_gaps,
                target_criteria=task.target_criteria,
            )
            if fingerprint not in known:
                fallback_tasks.append(task)
                known.add(fingerprint)
        return SupervisorAction(
            "CONDUCT_RESEARCH",
            judgement.reason if judgement and judgement.reason else "brief questions are not yet covered",
            tuple(fallback_tasks),
        )

    def resolve_action(
        self,
        action: SupervisorAction,
        judgement: CoverageJudgement | None = None,
        brief: StructuredResearchBrief | None = None,
        previous_fingerprints: set[str] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> SupervisorAction:
        if (
            action.action == "COMPLETE"
            and judgement is not None
            and (not judgement.sufficient or judgement.gaps)
            and not bool((budget or {}).get("exhausted"))
        ):
            repair = self.fallback_action(
                brief or StructuredResearchBrief("", 1, "", "research"),
                judgement,
                budget or {},
                previous_fingerprints=previous_fingerprints,
            )
            return SupervisorAction(
                "CONDUCT_RESEARCH",
                "runtime_override:blocking_coverage_gap",
                repair.research_tasks,
                "runtime_blocking_gap_override",
            )
        action = self.enforce_completion_invariant(action, judgement, budget=budget)
        if action.action != "CONDUCT_RESEARCH" or action.research_tasks:
            return action
        resolved = self.fallback_action(
            brief or StructuredResearchBrief("", 1, "", "research"),
            judgement,
            {},
            previous_fingerprints=previous_fingerprints,
        )
        return SupervisorAction(action.action, action.reason, resolved.research_tasks, action.source)

    @staticmethod
    def enforce_completion_invariant(
        action: SupervisorAction,
        judgement: CoverageJudgement | None,
        *,
        budget: dict[str, Any] | None = None,
    ) -> SupervisorAction:
        """COMPLETE requires sufficient coverage; a gap can only produce a STOP.

        This keeps ``coverage=gap`` from ever being reported as research
        completion, no matter what the Supervisor model returned.
        """
        if action.action != "COMPLETE":
            return action
        if judgement is None:
            return action
        if judgement.sufficient and not judgement.gaps:
            return action
        exhausted = bool((budget or {}).get("exhausted"))
        return SupervisorAction(
            "STOP_BUDGET_PARTIAL" if exhausted else "STOP_FAILURE",
            f"coverage_gap_blocks_complete:{judgement.status or 'gap'}",
            (),
            action.source,
        )

    @staticmethod
    def _sanitize_action(action: SupervisorAction) -> SupervisorAction:
        if action.action != "CONDUCT_RESEARCH":
            return SupervisorAction(action.action, action.reason, (), "structured_llm")
        tasks: list[ResearchTaskRequest] = []
        for item in action.research_tasks:
            profile = task_budget_profile(item.estimated_effort)
            tasks.append(
                ResearchTaskRequest(
                    objective=item.objective,
                    ask_id=item.ask_id,
                    question_id=item.question_id,
                    target_criteria=item.target_criteria,
                    target_gaps=item.target_gaps,
                    criterion_id=item.criterion_id,
                    hypothesis_id=item.hypothesis_id,
                    gap_id=item.gap_id,
                    missing_evidence_types=item.missing_evidence_types,
                    blocking_conflict_ids=item.blocking_conflict_ids,
                    priority=item.priority,
                    expected_evidence=item.expected_evidence,
                    source_hints=item.source_hints,
                    novelty_reason=item.novelty_reason,
                    estimated_effort=item.estimated_effort,
                    task_id="",
                    repair=item.repair,
                    repair_id=item.repair_id,
                    gap_reason=item.gap_reason,
                    missing_evidence=item.missing_evidence,
                    max_queries=item.max_queries,
                    max_fetches=item.max_fetches,
                    max_llm_calls=item.max_llm_calls,
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
        if not self._requires_semantic_decision(judgement, budget):
            return fallback
        started = time.perf_counter()
        compact_findings = self._compact_findings(findings)
        prompt = SUPERVISOR_PROMPT.format(
            brief=json.dumps(brief.to_dict(), ensure_ascii=False, indent=2),
            findings=json.dumps(compact_findings, ensure_ascii=False, indent=2),
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
                    timeout_sec=min(
                        model_timeout_sec("LLM_SUPERVISOR_TIMEOUT_SEC"),
                        max(1.0, float(os.getenv("LLM_COMPACT_SUPERVISOR_TIMEOUT_SEC", "15") or 15)),
                    ),
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
        return self.resolve_action(
            action, judgement, brief, previous_fingerprints, budget=budget
        )


__all__ = ["SupervisorAgent"]

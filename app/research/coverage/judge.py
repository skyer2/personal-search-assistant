"""Lightweight Brief-aligned coverage judgement."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from app.api.tracing import build_run_config
from app.research.brief.models import StructuredResearchBrief
from app.research.execution.llm_gateway import LLMGateway
from app.research.findings.models import ResearchFinding


@dataclass(frozen=True)
class CoverageJudgement:
    sufficient: bool
    status: str
    missing: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    weak_claims: tuple[str, ...] = ()
    recommended_next_questions: tuple[str, ...] = ()
    reason: str = ""
    source: str = "deterministic_fallback"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CoverageJudgement":
        row = data or {}
        return cls(
            sufficient=bool(row.get("sufficient")),
            status=str(row.get("status") or ("sufficient" if row.get("sufficient") else "gap")),
            missing=tuple(str(item) for item in row.get("missing") or []),
            conflicts=tuple(str(item) for item in row.get("conflicts") or []),
            weak_claims=tuple(str(item) for item in row.get("weak_claims") or []),
            recommended_next_questions=tuple(str(item) for item in row.get("recommended_next_questions") or []),
            reason=str(row.get("reason") or ""),
            source=str(row.get("source") or "deterministic_fallback"),
        )


def _supported_count(brief: StructuredResearchBrief, findings: list[ResearchFinding]) -> int:
    supported = [item for item in findings if item.evidence_ids and item.claims]
    if brief.user_intent == "comparison" and len(brief.explicit_subjects) >= 2:
        return min(len(supported), len(brief.explicit_subjects))
    return len(supported)


def judge_coverage(
    brief: StructuredResearchBrief,
    findings: list[ResearchFinding] | list[dict[str, Any]],
    *,
    claim_conflicts: list[dict[str, Any]] | None = None,
) -> CoverageJudgement:
    normalized = [
        item if isinstance(item, ResearchFinding) else ResearchFinding.from_dict(item)
        for item in findings
        if isinstance(item, (ResearchFinding, dict))
    ]
    supported_count = _supported_count(brief, normalized)
    required = 1 if brief.user_intent == "atomic_fact" else max(1, min(len(brief.key_questions), 4))
    sufficient = supported_count >= required and not claim_conflicts
    weak = tuple(item.summary for item in normalized if not item.evidence_ids)
    conflicts = tuple(str(item.get("edge_id") or item.get("kind") or "conflict") for item in claim_conflicts or [] if isinstance(item, dict))
    if sufficient:
        return CoverageJudgement(
            sufficient=True, status="sufficient", conflicts=conflicts, weak_claims=weak,
            reason="evidence-backed findings satisfy the Brief success criteria",
        )
    missing = tuple(brief.key_questions)
    return CoverageJudgement(
        sufficient=False, status="gap", missing=missing, conflicts=conflicts,
        weak_claims=weak, recommended_next_questions=missing[:4],
        reason=f"only {supported_count} of {required} required evidence-backed findings are available",
    )


class CoverageJudge:
    def __init__(self, agent: Any | None, budget_manager: Any | None = None):
        self.agent = agent
        self.budget_manager = budget_manager

    async def evaluate(
        self, brief: StructuredResearchBrief,
        findings: list[ResearchFinding] | list[dict[str, Any]],
        *, claim_conflicts: list[dict[str, Any]] | None = None,
    ) -> CoverageJudgement:
        fallback = judge_coverage(brief, findings, claim_conflicts=claim_conflicts)
        if self.agent is None:
            return fallback
        serialized = [item.to_dict() if isinstance(item, ResearchFinding) else item for item in findings[:24]]
        prompt = (
            "依据 Brief success criteria 判断研究是否足够。只输出 JSON："
            "{\"sufficient\":false,\"status\":\"gap\",\"missing\":[\"可行动缺口\"],\"conflicts\":[],"
            "\"weak_claims\":[],\"recommended_next_questions\":[],\"reason\":\"...\"}\n"
            "用户没有要求穷尽时，不要追求穷尽。\n\n"
            f"Brief: {json.dumps(brief.to_dict(), ensure_ascii=False)}\n"
            f"Findings: {json.dumps(serialized, ensure_ascii=False)}\n"
        )
        texts: list[str] = []
        try:
            gateway = LLMGateway(self.budget_manager)
            config = build_run_config("coverage_judge", metadata={"phase": "coverage_judge"})
            with gateway.execution_scope(phase="coverage_judge"):
                async for chunk in gateway.astream(self.agent, {"messages": [{"role": "user", "content": prompt}]}, config):
                    if not isinstance(chunk, dict):
                        continue
                    states = list(chunk.values()) if len(chunk) == 1 else [chunk]
                    for state in states:
                        if not isinstance(state, dict):
                            continue
                        for message in state.get("messages") or []:
                            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
                            texts.append(str(content or ""))
        except Exception:
            return fallback
        match = re.search(r"\{[\s\S]*\}", "\n".join(texts))
        if not match:
            return fallback
        try:
            patch = json.loads(match.group(0))
        except json.JSONDecodeError:
            return fallback
        if not isinstance(patch, dict):
            return fallback
        sufficient = bool(patch.get("sufficient"))
        missing = tuple(str(item) for item in patch.get("missing") or [])[:8]
        recommended = tuple(str(item) for item in patch.get("recommended_next_questions") or [])[:8]
        if not sufficient:
            missing = missing or fallback.missing or tuple(brief.key_questions)
            recommended = recommended or fallback.recommended_next_questions
        return CoverageJudgement(
            sufficient=sufficient,
            status=str(patch.get("status") or ("sufficient" if sufficient else "gap")),
            missing=missing,
            conflicts=tuple(str(item) for item in patch.get("conflicts") or [])[:8],
            weak_claims=tuple(str(item) for item in patch.get("weak_claims") or [])[:8],
            recommended_next_questions=recommended,
            reason=str(patch.get("reason") or ""), source="structured_llm",
        )


__all__ = ["CoverageJudgement", "CoverageJudge", "judge_coverage"]

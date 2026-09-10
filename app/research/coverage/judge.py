"""Criterion-based, monotonic coverage judgement."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Any

from app.api.tracing import build_run_config
from app.research.brief.models import StructuredResearchBrief
from app.research.execution.llm_gateway import LLMGateway
from app.research.evidence.policy import registrable_domain
from app.research.findings.models import ResearchFinding


@dataclass(frozen=True)
class CriterionSupport:
    criterion_id: str
    status: str
    supporting_claim_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    missing: str = ""
    conflicts: tuple[str, ...] = ()
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CriterionSupport":
        row = data or {}
        return cls(
            criterion_id=str(row.get("criterion_id") or ""),
            status=str(row.get("status") or "indeterminate"),
            supporting_claim_ids=tuple(str(item) for item in row.get("supporting_claim_ids") or []),
            evidence_ids=tuple(str(item) for item in row.get("evidence_ids") or []),
            source_ids=tuple(str(item) for item in row.get("source_ids") or []),
            missing=str(row.get("missing") or ""),
            conflicts=tuple(str(item) for item in row.get("conflicts") or []),
            confidence=max(0.0, min(1.0, float(row.get("confidence") or 0.0))),
        )


@dataclass(frozen=True)
class CoverageDelta:
    new_evidence_ids: tuple[str, ...] = ()
    new_supported_claim_ids: tuple[str, ...] = ()
    closed_criterion_ids: tuple[str, ...] = ()
    resolved_conflict_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CoverageDelta":
        row = data or {}
        return cls(
            new_evidence_ids=tuple(str(item) for item in row.get("new_evidence_ids") or []),
            new_supported_claim_ids=tuple(str(item) for item in row.get("new_supported_claim_ids") or []),
            closed_criterion_ids=tuple(str(item) for item in row.get("closed_criterion_ids") or []),
            resolved_conflict_ids=tuple(str(item) for item in row.get("resolved_conflict_ids") or []),
        )


@dataclass(frozen=True)
class CoverageJudgement:
    sufficient: bool
    status: str
    criteria: tuple[CriterionSupport, ...] = ()
    delta: CoverageDelta = CoverageDelta()
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
            criteria=tuple(
                CriterionSupport.from_dict(item)
                for item in row.get("criteria") or []
                if isinstance(item, dict)
            ),
            delta=CoverageDelta.from_dict(row.get("delta") if isinstance(row.get("delta"), dict) else None),
            missing=tuple(str(item) for item in row.get("missing") or []),
            conflicts=tuple(str(item) for item in row.get("conflicts") or []),
            weak_claims=tuple(str(item) for item in row.get("weak_claims") or []),
            recommended_next_questions=tuple(
                str(item) for item in row.get("recommended_next_questions") or []
            ),
            reason=str(row.get("reason") or ""),
            source=str(row.get("source") or "deterministic_fallback"),
        )


def _criterion_id(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"coverage_{digest}"


def _criteria(brief: StructuredResearchBrief) -> tuple[tuple[str, str], ...]:
    rows = tuple(brief.success_criteria or brief.key_questions or (brief.objective,))
    return tuple((_criterion_id(item), item) for item in rows if str(item).strip())


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _significant_characters(text: str) -> set[str]:
    stopwords = set("的了和与及或在是有哪些为什么你觉得当前当下")
    return {item for item in _normalize(text) if item.isalnum() and item not in stopwords}


def _matches(criterion: str, text: str) -> bool:
    left = _significant_characters(criterion)
    right = _normalize(text)
    if not left:
        return False
    overlap = sum(1 for item in left if item in right)
    words = [item for item in re.split(r"\s+", _normalize(criterion)) if len(item) >= 3]
    word_match = any(word in right for word in words)
    return overlap / len(left) >= 0.34 or (word_match and overlap / len(left) >= 0.2)


def _source_identity(row: dict[str, Any]) -> str:
    source_id = str(row.get("source_id") or "").strip()
    if source_id:
        return source_id
    locator = str(row.get("locator") or row.get("url") or "").strip()
    if locator:
        return registrable_domain(locator) or locator
    return str(row.get("evidence_id") or "").strip()


def _explicitly_bound(criterion_id: str, criterion: str, finding: ResearchFinding) -> bool:
    target = _normalize(criterion)
    targets = {_normalize(item) for item in finding.supported_criteria}
    return criterion_id in finding.supported_criteria or target in targets


def _supported_rows(
    criterion_id: str,
    criterion: str,
    findings: list[ResearchFinding],
    claims: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
    *,
    atomic_fact: bool = False,
) -> tuple[list[str], list[str], float, list[str]]:
    claim_ids: list[str] = []
    evidence_ids: list[str] = []
    for row in claims:
        text = str(row.get("text") or row.get("claim") or "")
        refs = [str(item) for item in row.get("evidence_ids") or [] if str(item).strip()]
        if refs and _matches(criterion, text):
            claim_id = str(row.get("claim_id") or "")
            if claim_id:
                claim_ids.append(claim_id)
            evidence_ids.extend(refs)
    for finding in findings:
        if not finding.evidence_ids:
            continue
        text = " ".join([finding.summary, *finding.claims])
        if _explicitly_bound(criterion_id, criterion, finding) or _matches(criterion, text):
            evidence_ids.extend(finding.evidence_ids)
    source_ids = [
        _source_identity(evidence_by_id[evidence_id])
        for evidence_id in dict.fromkeys(evidence_ids)
        if evidence_id in evidence_by_id
    ]
    for finding in findings:
        if _explicitly_bound(criterion_id, criterion, finding):
            source_ids.extend(
                registrable_domain(source) or source
                for source in finding.source_ids
                if str(source).strip()
            )
    if atomic_fact and (claim_ids or evidence_ids):
        return claim_ids[:1], list(dict.fromkeys(evidence_ids)), 0.55, list(dict.fromkeys(source_ids))
    unique_sources = list(dict.fromkeys(source_ids))
    return (
        list(dict.fromkeys(claim_ids)),
        list(dict.fromkeys(evidence_ids)),
        min(1.0, 0.35 + 0.2 * len(unique_sources)),
        unique_sources,
    )


def _current_conflict_ids(conflicts: list[dict[str, Any]]) -> set[str]:
    return {
        str(row.get("edge_id") or row.get("kind") or row.get("label") or "conflict")
        for row in conflicts
        if isinstance(row, dict)
    }


def judge_coverage(
    brief: StructuredResearchBrief,
    findings: list[ResearchFinding] | list[dict[str, Any]],
    *,
    claim_conflicts: list[dict[str, Any]] | None = None,
    claims: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    previous: CoverageJudgement | None = None,
) -> CoverageJudgement:
    normalized_findings = [
        item if isinstance(item, ResearchFinding) else ResearchFinding.from_dict(item)
        for item in findings
        if isinstance(item, (ResearchFinding, dict))
    ]
    normalized_claims = [dict(row) for row in claims or [] if isinstance(row, dict)]
    evidence_by_id = {
        str(row.get("evidence_id")): dict(row)
        for row in evidence or []
        if isinstance(row, dict) and str(row.get("evidence_id") or "").strip()
    }
    conflicts = [dict(row) for row in claim_conflicts or [] if isinstance(row, dict)]
    conflict_ids = _current_conflict_ids(conflicts)
    required_sources = max(1, int(brief.source_requirements.min_independent_sources or 1))

    supports: list[CriterionSupport] = []
    for criterion_id, criterion in _criteria(brief):
        claim_ids, evidence_ids, confidence, source_ids = _supported_rows(
            criterion_id,
            criterion,
            normalized_findings,
            normalized_claims,
            evidence_by_id,
            atomic_fact=brief.user_intent == "atomic_fact",
        )
        if source_ids and len(source_ids) >= required_sources:
            status = "supported"
            missing = ""
        elif source_ids or evidence_ids:
            status = "partial"
            missing = f"{criterion}（仍缺少 {required_sources - len(source_ids)} 个独立来源）"
        else:
            status = "unsupported"
            missing = criterion
        supports.append(
            CriterionSupport(
                criterion_id=criterion_id,
                status=status,
                supporting_claim_ids=tuple(claim_ids),
                evidence_ids=tuple(evidence_ids),
                source_ids=tuple(source_ids),
                missing=missing,
                conflicts=(),
                confidence=confidence if status != "unsupported" else 0.0,
            )
        )

    previous_evidence = (
        {
            evidence_id
            for row in previous.criteria
            for evidence_id in row.evidence_ids
        }
        if previous is not None
        else set()
    )
    current_evidence = {
        evidence_id
        for support in supports
        for evidence_id in support.evidence_ids
    }
    previous_supports = {
        row.criterion_id: row for row in (previous.criteria or [])
    } if previous is not None else {}
    previous_claim_ids = {
        item
        for row in previous_supports.values()
        for item in row.supporting_claim_ids
    }
    current_claim_ids = {
        item
        for row in supports
        for item in row.supporting_claim_ids
    }
    previous_conflicts = set(previous.conflicts) if previous is not None else set()
    delta = CoverageDelta(
        new_evidence_ids=tuple(sorted(current_evidence - previous_evidence)),
        new_supported_claim_ids=tuple(sorted(current_claim_ids - previous_claim_ids)),
        closed_criterion_ids=tuple(
            sorted(
                row.criterion_id
                for row in supports
                if row.status == "supported"
                and row.criterion_id in previous_supports
                and previous_supports[row.criterion_id].status != "supported"
            )
        ),
        resolved_conflict_ids=tuple(sorted(previous_conflicts - conflict_ids)),
    )

    deterministic_sufficient = bool(supports) and all(
        row.status == "supported" for row in supports
    )
    if previous is not None and previous.sufficient and not conflict_ids:
        deterministic_sufficient = True
    progress = bool(
        delta.new_evidence_ids
        or delta.new_supported_claim_ids
        or delta.closed_criterion_ids
        or delta.resolved_conflict_ids
    )
    if previous is not None and not previous.sufficient and not progress:
        deterministic_sufficient = False

    missing = tuple(row.missing for row in supports if row.missing)
    weak = tuple(
        row.summary for row in normalized_findings if not row.evidence_ids
    )
    if deterministic_sufficient:
        return CoverageJudgement(
            sufficient=True,
            status="sufficient",
            criteria=tuple(supports),
            delta=delta,
            conflicts=tuple(sorted(conflict_ids)),
            weak_claims=weak,
            reason="all Brief success criteria have independently supported evidence",
        )
    return CoverageJudgement(
        sufficient=False,
        status="gap",
        criteria=tuple(supports),
        delta=delta,
        missing=missing or tuple(row[1] for row in _criteria(brief)),
        conflicts=tuple(sorted(conflict_ids)),
        weak_claims=weak,
        recommended_next_questions=missing[:4],
        reason=(
            "coverage cannot improve without an evidence, claim, gap-closure, or conflict delta"
            if previous is not None and not previous.sufficient and not progress
            else f"{sum(1 for row in supports if row.status == 'supported')} of {len(supports)} Brief criteria are independently supported"
        ),
    )


class CoverageJudge:
    def __init__(self, agent: Any | None, budget_manager: Any | None = None):
        self.agent = agent
        self.budget_manager = budget_manager

    async def evaluate(
        self,
        brief: StructuredResearchBrief,
        findings: list[ResearchFinding] | list[dict[str, Any]],
        *,
        claim_conflicts: list[dict[str, Any]] | None = None,
        claims: list[dict[str, Any]] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        previous: CoverageJudgement | None = None,
    ) -> CoverageJudgement:
        fallback = judge_coverage(
            brief,
            findings,
            claim_conflicts=claim_conflicts,
            claims=claims,
            evidence=evidence,
            previous=previous,
        )
        if self.agent is None:
            return fallback
        serialized = [
            item.to_dict() if isinstance(item, ResearchFinding) else item
            for item in findings[:24]
        ]
        prompt = (
            "依据 Brief success criteria 判断研究是否足够。只输出 JSON："
            "{\"sufficient\":false,\"status\":\"gap\",\"missing\":[\"可行动缺口\"],\"conflicts\":[],"
            "\"weak_claims\":[],\"recommended_next_questions\":[],\"reason\":\"...\"}\n"
            "无新增证据时不得把 gap 判为 sufficient。\n\n"
            f"Brief: {json.dumps(brief.to_dict(), ensure_ascii=False)}\n"
            f"Findings: {json.dumps(serialized, ensure_ascii=False)}\n"
            f"Claims: {json.dumps(claims or [], ensure_ascii=False)}\n"
            f"Evidence: {json.dumps(evidence or [], ensure_ascii=False)}\n"
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
            return replace(
                fallback,
                source="deterministic_fail_closed",
                reason="coverage judge failed; fail-closed to previous coverage state",
            )
        match = re.search(r"\{[\s\S]*\}", "\n".join(texts))
        if not match:
            return fallback
        try:
            patch = json.loads(match.group(0))
        except json.JSONDecodeError:
            return fallback
        if not isinstance(patch, dict):
            return fallback
        missing = tuple(str(item) for item in patch.get("missing") or [])[:8]
        recommended = tuple(str(item) for item in patch.get("recommended_next_questions") or [])[:8]
        if not fallback.sufficient:
            missing = missing or fallback.missing
            recommended = recommended or fallback.recommended_next_questions
        return CoverageJudgement(
            sufficient=fallback.sufficient,
            status=fallback.status,
            criteria=fallback.criteria,
            delta=fallback.delta,
            missing=missing,
            conflicts=tuple(str(item) for item in patch.get("conflicts") or [])[:8] or fallback.conflicts,
            weak_claims=tuple(str(item) for item in patch.get("weak_claims") or [])[:8] or fallback.weak_claims,
            recommended_next_questions=recommended,
            reason=str(patch.get("reason") or fallback.reason),
            source="structured_llm_fail_closed",
        )


__all__ = [
    "CoverageDelta",
    "CoverageJudgement",
    "CoverageJudge",
    "CriterionSupport",
    "judge_coverage",
]

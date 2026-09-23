"""Criterion-based, monotonic coverage judgement."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from dataclasses import asdict, dataclass, replace
from typing import Any

from app.api.tracing import build_run_config
from app.research.brief.models import StructuredResearchBrief
from app.research.execution.llm_gateway import LLMGateway
from app.research.evidence.policy import registrable_domain
from app.research.evidence.quality import is_high_authority
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
    missing_evidence_types: tuple[str, ...] = ()
    blocking_conflict_ids: tuple[str, ...] = ()
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
            missing_evidence_types=tuple(
                str(item) for item in row.get("missing_evidence_types") or []
            ),
            blocking_conflict_ids=tuple(
                str(item) for item in row.get("blocking_conflict_ids") or []
            ),
            confidence=max(0.0, min(1.0, float(row.get("confidence") or 0.0))),
        )


@dataclass(frozen=True)
class CoverageGap:
    gap_id: str
    criterion_id: str
    description: str
    current_evidence_ids: tuple[str, ...] = ()
    missing_evidence_type: tuple[str, ...] = ()
    blocking_conflict_ids: tuple[str, ...] = ()
    priority: str = "medium"
    blocking: bool = True
    question_id: str = ""
    dimension: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CoverageGap":
        row = data or {}
        missing = row.get("missing_evidence_type")
        if isinstance(missing, str):
            missing = [missing]
        priority = str(row.get("priority") or "medium").lower()
        return cls(
            gap_id=str(row.get("gap_id") or ""),
            criterion_id=str(row.get("criterion_id") or ""),
            description=str(row.get("description") or ""),
            current_evidence_ids=tuple(
                str(item) for item in row.get("current_evidence_ids") or []
            ),
            missing_evidence_type=tuple(str(item) for item in missing or []),
            blocking_conflict_ids=tuple(
                str(item) for item in row.get("blocking_conflict_ids") or []
            ),
            priority=priority if priority in {"high", "medium", "low"} else "medium",
            blocking=bool(row.get("blocking", True)),
            question_id=str(row.get("question_id") or ""),
            dimension=str(row.get("dimension") or ""),
        )


@dataclass(frozen=True)
class KeyQuestionCoverage:
    question_id: str
    status: str
    blocking: bool
    support_count: int = 0
    independent_source_count: int = 0
    high_authority_source_count: int = 0
    direct_evidence_count: int = 0
    counter_evidence_count: int = 0
    unresolved_conflicts: tuple[str, ...] = ()
    missing_evidence_types: tuple[str, ...] = ()
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "KeyQuestionCoverage":
        row = data or {}
        return cls(
            question_id=str(row.get("question_id") or ""),
            status=str(row.get("status") or "uncovered"),
            blocking=bool(row.get("blocking", True)),
            support_count=max(0, int(row.get("support_count") or 0)),
            independent_source_count=max(0, int(row.get("independent_source_count") or 0)),
            high_authority_source_count=max(0, int(row.get("high_authority_source_count") or 0)),
            direct_evidence_count=max(0, int(row.get("direct_evidence_count") or 0)),
            counter_evidence_count=max(0, int(row.get("counter_evidence_count") or 0)),
            unresolved_conflicts=tuple(str(item) for item in row.get("unresolved_conflicts") or []),
            missing_evidence_types=tuple(str(item) for item in row.get("missing_evidence_types") or []),
            confidence=max(0.0, min(1.0, float(row.get("confidence") or 0.0))),
        )


@dataclass(frozen=True)
class AskCoverage:
    """Coverage of one original user ask, rolled up from its criteria."""

    ask_id: str
    direct_answer_available: bool = False
    source_quality: str = "unknown"
    evidence_count: int = 0
    unresolved_gaps: tuple[str, ...] = ()
    criterion_ids: tuple[str, ...] = ()
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AskCoverage":
        row = data or {}
        return cls(
            ask_id=str(row.get("ask_id") or ""),
            direct_answer_available=bool(row.get("direct_answer_available")),
            source_quality=str(row.get("source_quality") or "unknown"),
            evidence_count=max(0, int(row.get("evidence_count") or 0)),
            unresolved_gaps=tuple(str(item) for item in row.get("unresolved_gaps") or []),
            criterion_ids=tuple(str(item) for item in row.get("criterion_ids") or []),
            required=bool(row.get("required", True)),
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
    ask_coverage: tuple[AskCoverage, ...] = ()
    delta: CoverageDelta = CoverageDelta()
    missing: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    weak_claims: tuple[str, ...] = ()
    recommended_next_questions: tuple[str, ...] = ()
    gaps: tuple[CoverageGap, ...] = ()
    key_question_coverage: tuple[KeyQuestionCoverage, ...] = ()
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
            ask_coverage=tuple(
                AskCoverage.from_dict(item)
                for item in row.get("ask_coverage") or []
                if isinstance(item, dict)
            ),
            delta=CoverageDelta.from_dict(row.get("delta") if isinstance(row.get("delta"), dict) else None),
            missing=tuple(str(item) for item in row.get("missing") or []),
            conflicts=tuple(str(item) for item in row.get("conflicts") or []),
            weak_claims=tuple(str(item) for item in row.get("weak_claims") or []),
            recommended_next_questions=tuple(
                str(item) for item in row.get("recommended_next_questions") or []
            ),
            gaps=tuple(
                CoverageGap.from_dict(item)
                for item in row.get("gaps") or []
                if isinstance(item, dict)
            ),
            key_question_coverage=tuple(
                KeyQuestionCoverage.from_dict(item)
                for item in row.get("key_question_coverage") or []
                if isinstance(item, dict)
            ),
            reason=str(row.get("reason") or ""),
            source=str(row.get("source") or "deterministic_fallback"),
        )


def _criterion_id(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"coverage_{digest}"


def _criteria(brief: StructuredResearchBrief) -> tuple[tuple[str, str], ...]:
    rows = tuple(brief.key_questions or (brief.objective,))
    return tuple((_criterion_id(item), item) for item in rows if str(item).strip())


def _criterion_ask_ids(brief: StructuredResearchBrief) -> dict[str, str]:
    """Map each criterion id onto the user ask it came from."""
    mapping: dict[str, str] = {}
    for index, (criterion_id, _text) in enumerate(_criteria(brief), 1):
        ask_id = brief.ask_id_for_question_index(index)
        if ask_id:
            mapping[criterion_id] = ask_id
    return mapping


def _ask_coverage(
    brief: StructuredResearchBrief,
    supports: list[CriterionSupport],
    evidence_by_id: dict[str, dict[str, Any]],
) -> tuple[AskCoverage, ...]:
    """Roll criterion support up to the user's asks."""
    # ``StructuredResearchBrief.user_asks`` is the canonical v1 lineage
    # field.  Keep the coverage judge tolerant of historical / fixture briefs
    # while a persisted run is being upgraded: ask coverage is an additional
    # safeguard, not a reason to crash the deterministic coverage diagnostic.
    asks = list(getattr(brief, "user_asks", ()) or ())
    if not asks:
        return ()
    criterion_to_ask = _criterion_ask_ids(brief)
    by_ask: dict[str, list[CriterionSupport]] = {}
    for support in supports:
        ask_id = criterion_to_ask.get(support.criterion_id, "")
        if ask_id:
            by_ask.setdefault(ask_id, []).append(support)
    rows: list[AskCoverage] = []
    for ask in asks:
        ask_id = str(ask.ask_id or "")
        mapped = by_ask.get(ask_id) or []
        evidence_ids = list(
            dict.fromkeys(item for support in mapped for item in support.evidence_ids)
        )
        tier = "unknown"
        if evidence_ids:
            from app.research.evidence.source_tier import classify_source_tier

            tiers = {
                classify_source_tier(evidence_by_id[item])
                for item in evidence_ids
                if item in evidence_by_id
            }
            tier = "tier1" if "tier1" in tiers else "tier2" if "tier2" in tiers else "tier3" if tiers else "unknown"
        rows.append(
            AskCoverage(
                ask_id=ask_id,
                direct_answer_available=bool(mapped) and all(
                    support.status == "supported" for support in mapped
                ),
                source_quality=tier,
                evidence_count=len(evidence_ids),
                unresolved_gaps=tuple(
                    support.missing for support in mapped if support.status != "supported" and support.missing
                )
                or (() if mapped else ("no_research_question",)),
                criterion_ids=tuple(support.criterion_id for support in mapped),
                required=bool(ask.required),
            )
        )
    return tuple(rows)


_PUNCT = re.compile(r"[?？!！。.,，、;；:：\s]+")


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _normalize_criterion(value: str) -> str:
    """Criterion identity must not depend on trailing punctuation."""
    return _PUNCT.sub("", str(value or "").casefold())


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
    target = _normalize_criterion(criterion)
    targets = {_normalize_criterion(item) for item in finding.supported_criteria}
    return criterion_id in finding.supported_criteria or target in targets


def _claim_bound(criterion_id: str, criterion: str, row: dict[str, Any]) -> bool:
    criteria = {
        *[
            str(item)
            for item in row.get("criterion_ids")
            or [row.get("criterion_id")]
            or []
            if str(item).strip()
        ],
        *[
            str(item)
            for item in row.get("supported_criteria") or []
            if str(item).strip()
        ],
    }
    return criterion_id in criteria or _normalize_criterion(criterion) in {
        _normalize_criterion(item) for item in criteria
    }


def _is_primary(row: dict[str, Any]) -> bool:
    tier = str(row.get("source_tier") or row.get("source_kind") or "").upper()
    locator = str(row.get("locator") or row.get("url") or "").lower()
    return tier == "PRIMARY" or any(
        token in locator
        for token in ("gov.cn", "sec.gov", "/ir.", "investor", "docs.", "official")
    )


def _parse_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _is_fresh(row: dict[str, Any], horizon: str) -> bool:
    published = _parse_date(row.get("published_at")) or _parse_date(row.get("effective_at"))
    if published is None:
        return False
    if horizon == "recent":
        return datetime.now(timezone.utc) - published <= timedelta(days=365)
    return True


def _supported_rows(
    criterion_id: str,
    criterion: str,
    findings: list[ResearchFinding],
    claims: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
    *,
    atomic_fact: bool = False,
) -> tuple[list[str], list[str], float, list[str], bool]:
    claim_ids: list[str] = []
    evidence_ids: list[str] = []
    directly_bound = False
    for row in claims:
        # Coverage is an authority consumer.  Raw findings and unadmitted
        # claim drafts are useful diagnostics, never support for a criterion.
        if not bool(row.get("validated", False)):
            continue
        text = str(row.get("text") or row.get("claim") or "")
        refs = [str(item) for item in row.get("evidence_ids") or [] if str(item).strip()]
        explicit_claim = _claim_bound(criterion_id, criterion, row)
        if refs and explicit_claim:
            claim_id = str(row.get("claim_id") or "")
            if claim_id:
                claim_ids.append(claim_id)
            evidence_ids.extend(refs)
            directly_bound = directly_bound or explicit_claim
    source_ids = [
        _source_identity(evidence_by_id[evidence_id])
        for evidence_id in dict.fromkeys(evidence_ids)
        if evidence_id in evidence_by_id
    ]
    if atomic_fact and (claim_ids or evidence_ids):
        return claim_ids[:1], list(dict.fromkeys(evidence_ids)), 0.55, list(dict.fromkeys(source_ids)), directly_bound
    unique_sources = list(dict.fromkeys(source_ids))
    confidence = min(1.0, 0.35 + 0.2 * len(unique_sources))
    if not directly_bound:
        confidence = min(0.45, confidence)
    return (
        list(dict.fromkeys(claim_ids)),
        list(dict.fromkeys(evidence_ids)),
        confidence,
        unique_sources,
        directly_bound,
    )


def _unresolved_conflicts(
    conflicts: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
) -> dict[str, tuple[bool, str]]:
    resolution_by_edge = {
        str(row.get("edge_id")): row
        for row in resolutions
        if isinstance(row, dict) and str(row.get("edge_id") or "").strip()
    }
    output: dict[str, tuple[bool, str]] = {}
    for row in conflicts:
        edge_id = str(
            row.get("edge_id") or row.get("kind") or row.get("label") or "conflict"
        )
        resolution = resolution_by_edge.get(edge_id, {})
        status = str(resolution.get("status") or "unresolved")
        if status == "unresolved":
            output[edge_id] = (
                bool(resolution.get("blocking", False)),
                str(resolution.get("criterion_id") or ""),
            )
    return output


def judge_coverage(
    brief: StructuredResearchBrief,
    findings: list[ResearchFinding] | list[dict[str, Any]],
    *,
    claim_conflicts: list[dict[str, Any]] | None = None,
    claims: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    claim_resolutions: list[dict[str, Any]] | None = None,
    previous: CoverageJudgement | None = None,
    worker_results: list[dict[str, Any]] | None = None,
    task_metadata: dict[str, dict[str, Any]] | None = None,
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
    resolutions = [dict(row) for row in claim_resolutions or [] if isinstance(row, dict)]
    unresolved = _unresolved_conflicts(conflicts, resolutions)
    conflict_ids = set(unresolved)
    blocking_conflict_ids = {
        edge_id for edge_id, (blocking, _) in unresolved.items() if blocking
    }
    required_sources = max(1, int(brief.source_requirements.min_independent_sources or 1))

    task_metadata = task_metadata or {}
    # A Worker that stopped after admitting evidence is partial research, not
    # a failed question. Its evidence must continue through coverage.
    failed_questions: set[str] = set()
    successful_questions: set[str] = set()
    for row in worker_results or []:
        task_id = str(row.get("task_id") or "")
        metadata = row.get("task_metadata") if isinstance(row.get("task_metadata"), dict) else task_metadata.get(task_id, {})
        question_id = str((metadata or {}).get("question_id") or "")
        if not question_id:
            continue
        status = str(row.get("result_status") or row.get("status") or "").casefold()
        accepted = row.get("finding_acceptance") or {}
        metrics = row.get("metrics") or {}
        admitted_evidence = int(
            row.get("admitted_evidence_count")
            or (accepted.get("admitted_evidence_count") if isinstance(accepted, dict) else 0)
            or (metrics.get("admitted_evidence_count") if isinstance(metrics, dict) else 0)
            or 0
        )
        accepted_findings = int(
            row.get("accepted_finding_count")
            or (accepted.get("accepted_finding_count") if isinstance(accepted, dict) else 0)
            or (metrics.get("accepted_finding_count") if isinstance(metrics, dict) else 0)
            or 0
        )
        terminal_failure = (
            not bool(row.get("ok", status not in {"failed", "error"}))
            or status in {"failed", "error", "none"}
        )
        failed = terminal_failure and admitted_evidence <= 0 and accepted_findings <= 0
        if failed:
            failed_questions.add(question_id)
        else:
            successful_questions.add(question_id)
    failed_questions -= successful_questions

    supports: list[CriterionSupport] = []
    gaps: list[CoverageGap] = []
    question_coverage: list[KeyQuestionCoverage] = []
    for question_index, (criterion_id, criterion) in enumerate(_criteria(brief), 1):
        question_id = f"q{question_index}"
        claim_ids, evidence_ids, confidence, source_ids, directly_bound = _supported_rows(
            criterion_id,
            criterion,
            normalized_findings,
            normalized_claims,
            evidence_by_id,
            atomic_fact=brief.user_intent == "atomic_fact",
        )
        unique_evidence_ids = list(dict.fromkeys(evidence_ids))
        unique_source_ids = list(dict.fromkeys(source_ids))
        bound_conflicts = {
            edge_id
            for edge_id, (_, bound_criterion) in unresolved.items()
            if bound_criterion in {criterion_id, criterion}
        }
        bound_blocking_conflicts = bound_conflicts & blocking_conflict_ids
        independent_ok = len(unique_source_ids) >= required_sources
        selected_records = [
            evidence_by_id[item]
            for item in unique_evidence_ids
            if item in evidence_by_id
        ]
        primary_ok = (
            not brief.source_requirements.primary_required
            or any(_is_primary(record) for record in selected_records)
        )
        fresh_ok = (
            not brief.freshness_requirements.required
            or any(
                _is_fresh(record, brief.freshness_requirements.time_horizon)
                for record in selected_records
            )
        )
        missing_types: list[str] = []
        if not directly_bound:
            missing_types.append("direct_criterion_binding")
        if not unique_evidence_ids:
            missing_types.append("evidence")
        if not independent_ok:
            missing_types.append("independent_source")
        if not primary_ok:
            missing_types.append("primary_source")
        if not fresh_ok:
            missing_types.append("fresh_evidence")
        if bound_blocking_conflicts:
            missing_types.append("conflict_resolution")

        if bound_blocking_conflicts:
            status = "conflicted"
            missing = f"{criterion}（存在 blocking unresolved conflict）"
        elif directly_bound and unique_evidence_ids and independent_ok and primary_ok and fresh_ok:
            status = "supported"
            missing = ""
        elif unique_source_ids or unique_evidence_ids:
            status = "partial"
            missing = f"{criterion}（缺少：{'、'.join(missing_types)}）"
        else:
            status = "unsupported"
            missing = criterion
        priority = "high" if status in {"unsupported", "conflicted"} else "medium"
        high_authority_count = sum(
            1 for evidence_id in unique_evidence_ids
            if (record := evidence_by_id.get(evidence_id)) is not None and is_high_authority(record)
        )
        direct_evidence_count = len(unique_evidence_ids) if directly_bound else 0
        coverage_status = "covered" if status == "supported" else "partially_covered" if status == "partial" else "uncovered"
        blocking = coverage_status != "covered" or question_id in failed_questions
        if question_id in failed_questions:
            missing_types.append("worker_failed")
        question_coverage.append(KeyQuestionCoverage(
            question_id=question_id,
            status=coverage_status,
            blocking=blocking,
            support_count=len(unique_evidence_ids),
            independent_source_count=len(unique_source_ids),
            high_authority_source_count=high_authority_count,
            direct_evidence_count=direct_evidence_count,
            counter_evidence_count=0,
            unresolved_conflicts=tuple(sorted(bound_conflicts)),
            missing_evidence_types=tuple(dict.fromkeys(missing_types)),
            confidence=confidence if coverage_status != "uncovered" else 0.0,
        ))
        if status != "supported" or question_id in failed_questions:
            gaps.append(
                CoverageGap(
                    gap_id=f"gap_{criterion_id.removeprefix('coverage_')}",
                    criterion_id=criterion_id,
                    description=missing or criterion,
                    current_evidence_ids=tuple(unique_evidence_ids),
                    missing_evidence_type=tuple(dict.fromkeys(missing_types)),
                    blocking_conflict_ids=tuple(sorted(bound_blocking_conflicts)),
                    priority=priority,
                    blocking=blocking,
                    question_id=question_id,
                    dimension=criterion,
                )
            )
        supports.append(
            CriterionSupport(
                criterion_id=criterion_id,
                status=status,
                supporting_claim_ids=tuple(claim_ids),
                evidence_ids=tuple(evidence_ids),
                source_ids=tuple(source_ids),
                missing=missing,
                conflicts=tuple(sorted(bound_conflicts)),
                missing_evidence_types=tuple(missing_types),
                blocking_conflict_ids=tuple(sorted(bound_blocking_conflicts)),
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

    ask_coverage = _ask_coverage(brief, supports, evidence_by_id)
    deterministic_sufficient = bool(supports) and all(
        row.status == "supported" for row in supports
    ) and not blocking_conflict_ids and not failed_questions
    # Every required user ask must have a direct answer available; a fully
    # supported criterion set that lost an ask is still a gap.
    if ask_coverage and not all(
        row.direct_answer_available for row in ask_coverage if row.required
    ):
        deterministic_sufficient = False
    if (
        previous is not None
        and previous.sufficient
        and not conflict_ids
        and not gaps
    ):
        deterministic_sufficient = True
    progress = bool(
        delta.new_evidence_ids
        or delta.new_supported_claim_ids
        or delta.closed_criterion_ids
        or delta.resolved_conflict_ids
    )
    if previous is not None and not previous.sufficient and not progress:
        deterministic_sufficient = False

    missing_descriptions = tuple(gap.description for gap in gaps)
    weak = tuple(
        row.summary for row in normalized_findings if not row.evidence_ids
    )
    if deterministic_sufficient:
        return CoverageJudgement(
            sufficient=True,
            status="sufficient",
            criteria=tuple(supports),
            ask_coverage=ask_coverage,
            delta=delta,
            conflicts=tuple(sorted(conflict_ids)),
            weak_claims=weak,
            gaps=(),
            key_question_coverage=tuple(question_coverage),
            reason="all Brief key questions have independently supported evidence",
        )
    return CoverageJudgement(
        sufficient=False,
        status="gap",
        criteria=tuple(supports),
        ask_coverage=ask_coverage,
        delta=delta,
        missing=missing_descriptions or tuple(row[1] for row in _criteria(brief)),
        conflicts=tuple(sorted(conflict_ids)),
        weak_claims=weak,
        recommended_next_questions=tuple(gap.description for gap in gaps[:4]),
        gaps=tuple(gaps),
        key_question_coverage=tuple(question_coverage),
        reason=(
            "coverage cannot improve without an evidence, claim, gap-closure, or conflict delta"
            if previous is not None and not previous.sufficient and not progress
            else f"{sum(1 for row in supports if row.status == 'supported')} of {len(supports)} Brief key questions are independently supported"
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
        claim_resolutions: list[dict[str, Any]] | None = None,
        previous: CoverageJudgement | None = None,
        worker_results: list[dict[str, Any]] | None = None,
        task_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> CoverageJudgement:
        fallback = judge_coverage(
            brief,
            findings,
            claim_conflicts=claim_conflicts,
            claims=claims,
            evidence=evidence,
            claim_resolutions=claim_resolutions,
            previous=previous,
            worker_results=worker_results,
            task_metadata=task_metadata,
        )
        if self.agent is None:
            return fallback
        serialized = [
            item.to_dict() if isinstance(item, ResearchFinding) else item
            for item in findings[:24]
        ]
        prompt = (
            "依据 Brief key questions 判断研究是否足够。只输出 JSON："
            "{\"sufficient\":false,\"status\":\"gap\",\"missing\":[\"可行动缺口\"],\"conflicts\":[],"
            "\"weak_claims\":[],\"recommended_next_questions\":[],\"reason\":\"...\"}\n"
            "无新增证据时不得把 gap 判为 sufficient。\n\n"
            f"Brief: {json.dumps(brief.to_dict(), ensure_ascii=False)}\n"
            f"Findings: {json.dumps(serialized, ensure_ascii=False)}\n"
            f"Claims: {json.dumps(claims or [], ensure_ascii=False)}\n"
            f"ConflictResolutions: {json.dumps(claim_resolutions or [], ensure_ascii=False)}\n"
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
            ask_coverage=fallback.ask_coverage,
            delta=fallback.delta,
            missing=missing,
            conflicts=tuple(str(item) for item in patch.get("conflicts") or [])[:8] or fallback.conflicts,
            weak_claims=tuple(str(item) for item in patch.get("weak_claims") or [])[:8] or fallback.weak_claims,
            recommended_next_questions=recommended,
            reason=str(patch.get("reason") or fallback.reason),
            source="structured_llm_fail_closed",
        )


__all__ = [
    "AskCoverage",
    "CoverageDelta",
    "CoverageGap",
    "CoverageJudgement",
    "KeyQuestionCoverage",
    "CoverageJudge",
    "CriterionSupport",
    "judge_coverage",
]

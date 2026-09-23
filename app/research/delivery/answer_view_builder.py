"""Compile validated claims into the unified user-facing AnswerViewModel."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.research.delivery.text_normalizer import normalize_claim_text
from app.research.delivery.view_model import (
    AnswerPoint,
    AnswerViewModel,
    QuestionAnswerSection,
    ReferenceEntry,
    ReferenceSourceType,
    UnresolvedItem,
)

MAX_POINTS_PER_QUESTION = 6

_SOURCE_RANK = {
    "primary": 5,
    "authoritative_secondary": 4,
    "secondary": 3,
    "community": 2,
    "unknown": 1,
}
_PUBLISHERS = {
    "openai.com": "OpenAI",
    "anthropic.com": "Anthropic",
    "google.com": "Google",
    "microsoft.com": "Microsoft",
    "reuters.com": "Reuters",
    "bloomberg.com": "Bloomberg",
    "ft.com": "Financial Times",
    "idc.com": "IDC",
    "gartner.com": "Gartner",
    "arxiv.org": "arXiv",
    "github.com": "GitHub",
    "huawei.com": "华为",
    "huaweicloud.com": "华为云",
}
_RUNTIME_DETAIL = re.compile(
    r"(?:worker|provider|synthesis|tool|llm|token|budget|timeout|"
    r"gap_|coverage_|evidence_|finding_|claim_)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Publishability:
    publishable: bool
    reason: str
    source_quality: str
    support_count: int


def _canonical_url(value: str) -> str:
    raw = str(value or "").strip().rstrip(".,;")
    if not raw.lower().startswith(("http://", "https://")):
        return raw
    parts = urlsplit(raw)
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parts.query)
            if not key.lower().startswith(("utm_", "ref", "source"))
        )
    )
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower().split(":", 1)[0],
            re.sub(r"/+", "/", parts.path).rstrip("/") or "/",
            query,
            "",
        )
    )


def _normalized_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def _source_type(row: dict[str, Any]) -> ReferenceSourceType:
    value = str(row.get("source_type") or row.get("source_tier") or "").lower()
    if value in {"primary", "official"}:
        return "primary"
    if value in {"authoritative_secondary", "high_quality_secondary"}:
        return "authoritative_secondary"
    if value == "secondary":
        return "secondary"
    if value == "community":
        return "community"
    return "unknown"


def _source_score(row: dict[str, Any]) -> tuple[float, ...]:
    source_type = _source_type(row)
    return (
        float(_SOURCE_RANK[source_type]),
        float(row.get("authority_score") or 0.0),
        float(row.get("directness_score") or 0.0),
        float(row.get("freshness_score") or 0.0),
        float(row.get("independence_score") or 0.0),
    )


def _publisher(row: dict[str, Any], url: str) -> str:
    declared = str(row.get("publisher") or row.get("organization") or "").strip()
    if declared:
        return declared
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    for domain, name in _PUBLISHERS.items():
        if host == domain or host.endswith(f".{domain}"):
            return name
    return host or "来源未知"


def _canonical_source_id(url: str, title: str = "") -> str:
    identity = _canonical_url(url) or _normalized_text(title)
    return f"SRC_{hashlib.sha1(identity.encode('utf-8')).hexdigest()[:12].upper()}"


def _same_syndicated_source(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_url = _canonical_url(str(left.get("locator") or left.get("url") or ""))
    right_url = _canonical_url(str(right.get("locator") or right.get("url") or ""))
    if left_url and left_url == right_url:
        return True
    left_title = _normalized_text(str(left.get("title") or ""))
    right_title = _normalized_text(str(right.get("title") or ""))
    if left_title and right_title:
        if left_title == right_title:
            return True
        if SequenceMatcher(None, left_title, right_title).ratio() >= 0.92:
            return True
    left_excerpt = _normalized_text(
        str(left.get("excerpt") or left.get("excerpt_ref") or "")
    )[:500]
    right_excerpt = _normalized_text(
        str(right.get("excerpt") or right.get("excerpt_ref") or "")
    )[:500]
    return bool(
        len(left_excerpt) >= 80
        and len(right_excerpt) >= 80
        and SequenceMatcher(None, left_excerpt, right_excerpt).ratio() >= 0.94
    )


@dataclass
class _CanonicalSource:
    canonical_id: str
    row: dict[str, Any]
    aliases: set[str]


def canonicalize_sources(
    records: Iterable[dict[str, Any]],
) -> tuple[list[_CanonicalSource], dict[str, str]]:
    """Deduplicate exact URLs and likely syndicated copies; prefer authority."""
    groups: list[_CanonicalSource] = []
    alias_map: dict[str, str] = {}
    for raw in records or []:
        row = dict(raw)
        url = _canonical_url(str(row.get("locator") or row.get("url") or ""))
        if not url:
            continue
        row["locator"] = url
        aliases = {
            str(row.get(key) or "").strip()
            for key in (
                "evidence_id",
                "source_id",
                "artifact_ref",
                "artifact_id",
                "canonical_source_id",
            )
            if str(row.get(key) or "").strip()
        }
        aliases.add(url)
        matching = next(
            (item for item in groups if _same_syndicated_source(item.row, row)),
            None,
        )
        if matching is None:
            canonical_id = str(row.get("canonical_source_id") or "") or _canonical_source_id(
                url, str(row.get("title") or "")
            )
            matching = _CanonicalSource(canonical_id, row, set())
            groups.append(matching)
        elif _source_score(row) > _source_score(matching.row):
            matching.row = row
        matching.aliases.update(aliases)
    for item in groups:
        for alias in item.aliases:
            alias_map[alias] = item.canonical_id
    return groups, alias_map


def _questions(brief: Any) -> list[tuple[str, str]]:
    if isinstance(brief, dict):
        research = brief.get("research_questions") or []
        key_questions = brief.get("key_questions") or []
    else:
        research = getattr(brief, "research_questions", None) or []
        key_questions = getattr(brief, "key_questions", None) or []
    rows: list[tuple[str, str]] = []
    for index, item in enumerate(research, 1):
        if isinstance(item, dict):
            qid = str(item.get("question_id") or f"q{index}")
            text = str(item.get("text") or "").strip()
        else:
            qid = str(getattr(item, "question_id", "") or f"q{index}")
            text = str(getattr(item, "text", "") or "").strip()
        if text:
            rows.append((qid, text))
    if rows:
        return rows
    return [
        (f"q{index}", str(text).strip())
        for index, text in enumerate(key_questions, 1)
        if str(text).strip()
    ]


def _coverage_index(
    coverage: dict[str, Any] | None,
    questions: list[tuple[str, str]],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Return criterion->question, question->status and question->gap."""
    criterion_to_question: dict[str, str] = {}
    status: dict[str, str] = {}
    gaps: dict[str, str] = {}
    row_by_position = list((coverage or {}).get("criteria") or [])
    for index, row in enumerate(row_by_position):
        if not isinstance(row, dict) or index >= len(questions):
            continue
        qid = questions[index][0]
        criterion_id = str(row.get("criterion_id") or "")
        if criterion_id:
            criterion_to_question[criterion_id] = qid
        status[qid] = str(row.get("status") or "")
        if row.get("missing"):
            gaps[qid] = str(row["missing"])
    for row in (coverage or {}).get("key_question_coverage") or []:
        if not isinstance(row, dict):
            continue
        qid = str(row.get("question_id") or "")
        if not qid:
            continue
        status[qid] = str(row.get("status") or "")
        missing = row.get("missing") or row.get("missing_requirements") or []
        if isinstance(missing, list):
            missing = "、".join(str(item) for item in missing if str(item))
        if missing:
            gaps[qid] = str(missing)
    for gap in (coverage or {}).get("gaps") or []:
        if not isinstance(gap, dict):
            continue
        criterion_id = str(gap.get("criterion_id") or "")
        qid = str(gap.get("question_id") or "") or criterion_to_question.get(
            criterion_id, ""
        )
        if qid and gap.get("description"):
            gaps[qid] = str(gap["description"])
    unbound_missing = [
        str(item)
        for item in (coverage or {}).get("missing") or []
        if str(item).strip()
    ]
    if unbound_missing and questions:
        if len(unbound_missing) == len(questions):
            for (qid, _question), item in zip(questions, unbound_missing):
                gaps.setdefault(qid, item)
        elif len(questions) == 1:
            gaps.setdefault(questions[0][0], "；".join(unbound_missing))
    return criterion_to_question, status, gaps


def _gap_note(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if _RUNTIME_DETAIL.search(text):
        return "该问题仍有部分信息未完成高质量来源交叉验证。"
    if "缺少" in text:
        return "现有结果仍缺少部分高质量来源交叉验证。"
    normalized = normalize_claim_text(text)
    return normalized or f"仍缺少：{text.rstrip('。')}。"


def _claim_refs(
    claim: dict[str, Any],
    binding_by_claim: dict[str, dict[str, Any]],
) -> list[str]:
    claim_id = str(
        claim.get("canonical_claim_id")
        or claim.get("claim_id")
        or claim.get("finding_id")
        or ""
    )
    binding = binding_by_claim.get(claim_id, {})
    values: list[str] = []
    for key in (
        "primary_evidence_refs",
        "supporting_evidence_refs",
        "evidence_refs",
        "evidence_ids",
        "source_refs",
        "source_ids",
    ):
        raw = binding.get(key) if key in binding else claim.get(key)
        raw = [raw] if isinstance(raw, str) else raw or []
        values.extend(str(item) for item in raw if str(item).strip())
    return list(dict.fromkeys(values))


def _first_supported_criterion(claim: dict[str, Any]) -> str:
    raw = claim.get("supported_criteria") or []
    if isinstance(raw, str):
        return raw
    return str(raw[0]) if isinstance(raw, (list, tuple)) and raw else ""


def evaluate_publishability(
    text: str,
    source_ids: list[str],
    sources_by_id: dict[str, _CanonicalSource],
) -> Publishability:
    normalized = normalize_claim_text(text)
    resolved = [sources_by_id[item] for item in source_ids if item in sources_by_id]
    if not normalized:
        return Publishability(False, "invalid_claim_text", "weak", len(resolved))
    if not resolved:
        return Publishability(False, "no_resolvable_source", "weak", 0)
    types = [_source_type(item.row) for item in resolved]
    independent_publishers = {
        _publisher(item.row, str(item.row.get("locator") or ""))
        for item in resolved
    }
    if "primary" in types or (
        types.count("authoritative_secondary") >= 2
        and len(independent_publishers) >= 2
    ):
        return Publishability(True, "", "strong", len(resolved))
    if "authoritative_secondary" in types or "secondary" in types:
        return Publishability(True, "needs_more_corroboration", "medium", len(resolved))
    return Publishability(True, "community_or_unknown_only", "weak", len(resolved))


def _reference(
    source: _CanonicalSource,
    citation_number: int,
) -> ReferenceEntry:
    row = source.row
    url = str(row.get("locator") or row.get("url") or "")
    source_type = _source_type(row)
    title = str(row.get("title") or "").strip() or "标题未知"
    if source_type in {"community", "unknown"} and title == "标题未知":
        title = "标题未知（低等级来源）"
    return ReferenceEntry(
        reference_id=source.canonical_id,
        citation_number=citation_number,
        publisher=_publisher(row, url),
        title=title,
        published_at=str(row.get("published_at") or row.get("effective_at") or "")[
            :10
        ]
        or None,
        url=url,
        source_type=source_type,
    )


def _build_view(
    *,
    status: str,
    brief: Any,
    claims: list[dict[str, Any]],
    bindings: list[dict[str, Any]] | None,
    coverage: dict[str, Any] | None,
    source_registry: list[dict[str, Any]],
    limitations: list[str] | None = None,
    delivery_note: str | None = None,
) -> AnswerViewModel:
    questions = _questions(brief)
    criterion_to_question, coverage_status, coverage_gaps = _coverage_index(
        coverage, questions
    )
    canonical_sources, alias_map = canonicalize_sources(source_registry)
    source_by_id = {item.canonical_id: item for item in canonical_sources}
    binding_by_claim = {
        str(
            item.get("claim_id")
            or item.get("canonical_claim_id")
            or ""
        ): item
        for item in bindings or []
        if isinstance(item, dict)
    }
    points_by_question: dict[str, list[tuple[AnswerPoint, Publishability]]] = {
        qid: [] for qid, _ in questions
    }
    fallback_qid = questions[0][0] if len(questions) == 1 else ""
    for claim in claims or []:
        text = str(
            claim.get("statement")
            or claim.get("text")
            or claim.get("claim")
            or ""
        )
        normalized = normalize_claim_text(text)
        if not normalized:
            continue
        raw_question_id = str(claim.get("question_id") or "")
        qid = (
            raw_question_id
            if raw_question_id in points_by_question
            else criterion_to_question.get(raw_question_id, "")
            or criterion_to_question.get(str(claim.get("criterion_id") or ""), "")
            or criterion_to_question.get(_first_supported_criterion(claim), "")
            or fallback_qid
        )
        if qid not in points_by_question:
            continue
        refs = _claim_refs(claim, binding_by_claim)
        canonical_refs = list(
            dict.fromkeys(alias_map[ref] for ref in refs if ref in alias_map)
        )
        publishability = evaluate_publishability(
            normalized, canonical_refs, source_by_id
        )
        if not publishability.publishable:
            continue
        try:
            confidence = max(0.0, min(1.0, float(claim.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        point = AnswerPoint(
            text=normalized,
            citation_refs=tuple(canonical_refs),
            confidence=confidence,
            evidence_quality=publishability.source_quality,  # type: ignore[arg-type]
        )
        points_by_question[qid].append((point, publishability))

    quality_rank = {"strong": 3, "medium": 2, "weak": 1}
    used_sources: list[str] = []
    sections: list[QuestionAnswerSection] = []
    unresolved: list[UnresolvedItem] = []
    for qid, question in questions:
        ranked = sorted(
            points_by_question.get(qid, []),
            key=lambda item: (
                quality_rank[item[0].evidence_quality],
                item[0].confidence,
                item[1].support_count,
                len(item[0].text),
            ),
            reverse=True,
        )
        deduped: list[AnswerPoint] = []
        seen_text: list[str] = []
        for point, _publishability in ranked:
            normalized = _normalized_text(point.text)
            if any(
                SequenceMatcher(None, normalized, old).ratio() >= 0.88
                for old in seen_text
            ):
                continue
            seen_text.append(normalized)
            deduped.append(point)
            if len(deduped) >= MAX_POINTS_PER_QUESTION:
                break
        for point in deduped:
            for source_id in point.citation_refs:
                if source_id not in used_sources:
                    used_sources.append(source_id)
        c_status = coverage_status.get(qid, "")
        has_confirmed = any(
            point.evidence_quality in {"strong", "medium"} for point in deduped
        )
        if not deduped:
            section_status = "unresolved"
        elif c_status in {"supported", "covered", "sufficient"} and has_confirmed:
            section_status = "confirmed"
        elif not coverage and has_confirmed and status == "success":
            section_status = "confirmed"
        else:
            section_status = "partial"
        gap = _gap_note(coverage_gaps.get(qid, ""))
        if not gap and any(point.evidence_quality == "weak" for point in deduped):
            gap = "现有结论仅有社区或未知来源支持，仍需高质量来源核验。"
        if section_status == "unresolved":
            gap = gap or "本轮尚未获得可发布且有来源绑定的结论。"
        sections.append(
            QuestionAnswerSection(
                question_id=qid,
                question=question,
                status=section_status,  # type: ignore[arg-type]
                answer_points=tuple(deduped),
                gap_note=gap,
            )
        )
        if section_status != "confirmed":
            unresolved.append(
                UnresolvedItem(
                    question_id=qid,
                    question=question,
                    description=gap
                    or "部分结论仍缺少高质量来源交叉验证。",
                )
            )

    for item in limitations or []:
        if _RUNTIME_DETAIL.search(str(item)):
            continue
        value = normalize_claim_text(str(item))
        if value:
            unresolved.append(UnresolvedItem("", "", value))

    references = tuple(
        _reference(source_by_id[source_id], index)
        for index, source_id in enumerate(used_sources, 1)
        if source_id in source_by_id
    )
    final_status = status if status in {"success", "partial", "failed"} else "partial"
    title = (
        "研究结果"
        if final_status == "success"
        else "部分研究结果"
        if final_status == "partial"
        else "研究未完成"
    )
    return AnswerViewModel(
        title=title,
        status=final_status,  # type: ignore[arg-type]
        sections=tuple(sections),
        unresolved_items=tuple(unresolved),
        references=references,
        delivery_note=delivery_note,
    )


def build_partial_answer_view(
    *,
    brief: Any,
    claims: list[dict[str, Any]],
    bindings: list[dict[str, Any]] | None,
    coverage: dict[str, Any] | None,
    source_registry: list[dict[str, Any]],
    limitations: list[str] | None = None,
) -> AnswerViewModel:
    return _build_view(
        status="partial",
        brief=brief,
        claims=claims,
        bindings=bindings,
        coverage=coverage,
        source_registry=source_registry,
        limitations=limitations,
        delivery_note=(
            "本轮研究部分完成，仅展示已有证据能够确认的内容；"
            "完整模型综合未完成，因此属于降级部分交付。"
            "未列出的内容不应视为已得到确认。"
        ),
    )


def build_recovery_view(**kwargs: Any) -> AnswerViewModel:
    return build_partial_answer_view(**kwargs)


def build_success_view(
    *,
    brief: Any,
    claims: list[dict[str, Any]],
    bindings: list[dict[str, Any]] | None,
    coverage: dict[str, Any] | None,
    source_registry: list[dict[str, Any]],
) -> AnswerViewModel:
    return _build_view(
        status="success",
        brief=brief,
        claims=claims,
        bindings=bindings,
        coverage=coverage,
        source_registry=source_registry,
    )


__all__ = [
    "MAX_POINTS_PER_QUESTION",
    "Publishability",
    "build_partial_answer_view",
    "build_recovery_view",
    "build_success_view",
    "canonicalize_sources",
    "evaluate_publishability",
]

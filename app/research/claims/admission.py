"""Claim admission: the only route from research material to publishable claim."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Literal

from app.research.evidence.models import EvidenceRecord
from app.research.findings.integrity import complete_sentence


ClaimType = Literal["fact", "inference", "forecast", "attributed_opinion"]


@dataclass(frozen=True)
class ClaimDraft:
    draft_id: str
    ask_id: str
    question_id: str
    task_id: str
    statement: str
    claim_type: ClaimType = "fact"
    evidence_refs: list[str] = field(default_factory=list)
    provenance: Literal["worker", "salvage_promotion"] = "worker"
    confidence: float = 0.0


@dataclass(frozen=True)
class ClaimTextQuality:
    grammatical_complete: bool
    subject_present: bool
    predicate_present: bool
    factual_or_analytical_statement: bool
    truncation_detected: bool
    ui_navigation_detected: bool
    seo_fragment_detected: bool

    @property
    def publishable(self) -> bool:
        return (
            self.grammatical_complete
            and self.subject_present
            and self.predicate_present
            and self.factual_or_analytical_statement
            and not self.truncation_detected
            and not self.ui_navigation_detected
            and not self.seo_fragment_detected
        )


@dataclass(frozen=True)
class ClaimAdmissionResult:
    admitted: bool
    reasons: list[str]
    semantic_complete: bool
    question_relevant: bool
    evidence_bound: bool
    evidence_direct: bool
    source_eligible: bool
    publishable_text: bool
    publishability_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_NAVIGATION = re.compile(r"\b(?:skip to|cookie(?:s)?|subscribe|sign in|menu|footer|breadcrumb)\b|跳到(?:主要)?内容|导航|页脚", re.I)
_SEO = re.compile(r"\b(?:read more|related articles|share this|all rights reserved)\b|相关阅读|上一篇|下一篇", re.I)
_QUESTION = re.compile(r"[?？]\s*$|^(?:什么|哪些|如何|为什么|是否|请)")
_PREDICATE = re.compile(r"(?:是|为|有|将|会|可|能|提高|降低|发布|表示|预计|显示|认为|转向|supports?|reported|will|is|are|was|were|has|have|grew|announced|[A-Za-z]+(?:ed|es))", re.I)


def classify_claim_text(value: str) -> ClaimTextQuality:
    text = str(value or "").strip()
    complete, _ = complete_sentence(text)
    navigation = bool(_NAVIGATION.search(text))
    seo = bool(_SEO.search(text))
    truncated = text.endswith(("…", "...", ",", "，", ";", "；", ":", "：", "-", "—"))
    # The predicate check is intentionally language-agnostic enough for
    # Chinese and English, while sentence integrity catches structural damage.
    predicate = bool(_PREDICATE.search(text))
    subject = len(re.sub(r"\s+", "", text)) >= 12 and not _QUESTION.search(text)
    factual = not _QUESTION.search(text) and len(text) >= 16
    return ClaimTextQuality(complete, subject, predicate, factual, truncated, navigation, seo)


def admit_claim(draft: ClaimDraft, evidence_by_id: dict[str, EvidenceRecord | dict[str, Any]]) -> ClaimAdmissionResult:
    reasons: list[str] = []
    text_quality = classify_claim_text(draft.statement)
    semantic_complete = text_quality.grammatical_complete
    question_relevant = bool(draft.question_id.strip() and draft.ask_id.strip())
    refs = [str(item) for item in draft.evidence_refs if str(item).strip()]
    resolved: list[EvidenceRecord] = []
    for ref in dict.fromkeys(refs):
        raw = evidence_by_id.get(ref)
        if raw is not None:
            resolved.append(raw if isinstance(raw, EvidenceRecord) else EvidenceRecord.from_dict(raw))
    evidence_bound = bool(refs and len(resolved) == len(set(refs)))
    # Source scoring gives ordinary fetched secondary material a deterministic
    # directness of 0.60.  It may support a normal worker claim, while the
    # stricter 0.80 threshold remains mandatory for salvage promotion.
    evidence_direct = bool(resolved) and max(float(item.directness_score) for item in resolved) >= 0.55
    source_eligible = bool(resolved) and any(
        item.source_type in {"primary", "authoritative_secondary", "secondary"}
        and float(item.authority_score) >= 0.55
        for item in resolved
    )
    publishable_text = text_quality.publishable
    if not semantic_complete:
        reasons.append("semantic_incomplete")
    if not question_relevant:
        reasons.append("missing_question_lineage")
    if not evidence_bound:
        reasons.append("unresolved_evidence_binding")
    if not evidence_direct:
        reasons.append("evidence_not_direct")
    if not source_eligible:
        reasons.append("source_below_claim_minimum")
    if text_quality.ui_navigation_detected:
        reasons.append("ui_navigation_text")
    if text_quality.seo_fragment_detected:
        reasons.append("seo_fragment")
    if text_quality.truncation_detected:
        reasons.append("truncated_text")
    if not publishable_text and not any(item in reasons for item in ("ui_navigation_text", "seo_fragment", "truncated_text")):
        reasons.append("unpublishable_text")
    source_score = max((float(item.authority_score) for item in resolved), default=0.0)
    directness = max((float(item.directness_score) for item in resolved), default=0.0)
    linguistic = 1.0 if publishable_text else 0.0
    score = round(
        (0.30 if semantic_complete else 0.0)
        + (0.25 if question_relevant else 0.0)
        + 0.20 * directness
        + 0.15 * source_score
        + 0.10 * linguistic,
        4,
    )
    threshold = 0.78 if draft.claim_type in {"forecast", "inference"} else 0.70
    admitted = not reasons and score >= threshold
    if not admitted and score < threshold:
        reasons.append("publishability_score_below_threshold")
    return ClaimAdmissionResult(admitted, reasons, semantic_complete, question_relevant, evidence_bound, evidence_direct, source_eligible, publishable_text, score)


__all__ = ["ClaimAdmissionResult", "ClaimDraft", "ClaimTextQuality", "admit_claim", "classify_claim_text"]

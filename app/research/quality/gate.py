"""Deterministic quality gate for reports, independent of provider self-reporting."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.research.evidence.quality import source_quality_metrics
from app.research.findings.integrity import complete_sentence


QualityVerdict = Literal["PASS", "REPAIRABLE", "FAIL"]
_RAW_ARTIFACT = re.compile(r"\bart-(?:web|file|pdf)-[\w-]+\b", re.I)
_CITATION = re.compile(r"\[\d+\]")
_FORECAST = ("未来", "将", "可能", "预计", "趋势", "forecast")
_MECHANISM = ("因为", "由于", "驱动", "机制", "因此", "意味着")
_MILESTONE = ("里程碑", "验证", "观察", "指标", "若", "如果")
_UNCERTAINTY = ("不确定", "风险", "取决于", "可能", "尚待", "限制")


@dataclass(frozen=True)
class QualityGateResult:
    verdict: QualityVerdict
    issues: tuple[str, ...]
    metrics: dict[str, Any]

    @property
    def repairable(self) -> bool:
        return self.verdict == "REPAIRABLE"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "repairable": self.repairable}


def _sentences(content: str) -> list[str]:
    return [item.strip(" -\t") for item in re.split(r"[。！？!?\n]+", content) if item.strip(" -\t")]


def _duplicate_ratio(sentences: list[str]) -> float:
    values = [re.sub(r"\s+|\[\d+\]", "", item).casefold() for item in sentences if len(item) >= 12]
    if not values:
        return 0.0
    duplicate = len(values) - len(set(values))
    return round(max(0.0, duplicate / len(values)), 4)


def evaluate_report_quality(
    *,
    content: str,
    brief: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    answer_contract: dict[str, Any] | None,
) -> QualityGateResult:
    text = str(content or "").strip()
    issues: list[str] = []
    sentences = _sentences(text)
    if not text:
        issues.append("no_content")
    if _RAW_ARTIFACT.search(text):
        issues.append("raw_artifact_citation")
    broken = [item for item in sentences if not complete_sentence(item)[0]]
    if broken:
        issues.append("broken_sentence")
    duplicate_ratio = _duplicate_ratio(sentences)
    if duplicate_ratio > 0.05:
        issues.append("duplicate_claims")
    citation_count = len(_CITATION.findall(text))
    if evidence_records and citation_count == 0:
        issues.append("citation_missing")
    bullet_count = sum(line.lstrip().startswith(("- ", "* ")) for line in text.splitlines())
    # A large list of bullet-sized snippets with no usable citation bindings
    # is an evidence dump even when it has a few headings around it.
    if bullet_count >= 8 and citation_count <= 1:
        issues.append("evidence_dump")
    questions = [str(item) for item in brief.get("key_questions") or [] if str(item).strip()]
    answers = list((answer_contract or {}).get("answers") or [])
    if not answers and isinstance((answer_contract or {}).get("final_answer"), dict):
        answers = list((answer_contract or {})["final_answer"].get("answers") or [])
    if questions and (
        len(answers) < len(questions)
        or any(not str(row.get("direct_answer") or "").strip() for row in answers[:len(questions)])
    ):
        issues.append("missing_question")
    quality = source_quality_metrics(evidence_records)
    if evidence_records and quality["high_authority_source_ratio"] < 0.6:
        issues.append("weak_sources")
    analytical = str(brief.get("user_intent") or "") in {
        "trend_forecast", "comparison", "recommendation", "explanation", "structured_report"
    }
    lower = text.casefold()
    has_forecast = analytical and any(token in lower for token in _FORECAST)
    if has_forecast and (
        not any(token in lower for token in _MECHANISM)
        or not any(token in lower for token in _MILESTONE)
        or not any(token in lower for token in _UNCERTAINTY)
    ):
        issues.append("forecast_missing_contract")
    strong_lines = [item for item in sentences if any(token in item for token in ("一定", "必然", "唯一", "超过"))]
    if any(not _CITATION.search(item) for item in strong_lines):
        issues.append("unsupported_strong_claim")
    if any(token in issues for token in ("no_content", "raw_artifact_citation", "missing_question")):
        verdict: QualityVerdict = "FAIL"
    elif issues:
        verdict = "REPAIRABLE"
    else:
        verdict = "PASS"
    return QualityGateResult(verdict, tuple(dict.fromkeys(issues)), {
        **quality,
        "broken_sentence_count": len(broken),
        "duplicate_claim_ratio": duplicate_ratio,
        "citation_count": citation_count,
        "unsupported_claim_count": int("unsupported_strong_claim" in issues),
        "forecast_uncertainty_coverage": not has_forecast or "forecast_missing_contract" not in issues,
    })


__all__ = ["QualityGateResult", "evaluate_report_quality"]

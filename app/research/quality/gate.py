"""Deterministic quality gate for reports, independent of provider self-reporting."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.research.evidence.quality import source_quality_metrics
from app.research.findings.integrity import complete_sentence


QualityVerdict = Literal["PASS", "REPAIRABLE", "FAIL"]
_RAW_ARTIFACT = re.compile(r"\bart-(?:web|file|pdf)-[\w-]+\b", re.I)
_RAW_SNIPPET = re.compile(
    r"(?:市场前景与发展趋势分析|市场规模与增长预测|根据多家权威机构|中国报告大厅网讯|搜索结果(?:显示|摘要)|原始摘录)",
    re.I,
)
_RAW_URL = re.compile(r"https?://\S+", re.I)
_INTERNAL_TITLE = re.compile(r"^#{1,6}\s*(?:q\d+\b|直接回答\s*$|关键判断\s*$)", re.I)
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


def _is_structural_sentence(value: str) -> bool:
    stripped = value.strip()
    return bool(
        re.fullmatch(r"\*\*[^*]+\*\*[：:]?", stripped)
        or stripped.startswith("#")
        or "为该判断提供已登记的直接或支持性证据" in stripped
        or "与正文 Claim–Evidence 映射对应" in stripped
    )


def _duplicate_ratio(sentences: list[str]) -> float:
    structural = (
        "机制", "为什么重要", "主要依据", "当前信号", "可观察里程碑", "不确定性", "已登记证据", "完整来源定位",
        "当任务从演示走向生产", "不同工具和 Agent 之间", "企业把 Agent 接入真实流程后", "任务周期变长后",
        "模型能力逐步商品化后", "多类参与者持续发布相关进展",
    )
    values = [
        re.sub(r"\s+|\[\d+\]", "", item).casefold()
        for item in sentences
        if len(item) >= 12 and not _is_structural_sentence(item) and not any(marker in item for marker in structural)
    ]
    if not values:
        return 0.0
    duplicate = len(values) - len(set(values))
    return round(max(0.0, duplicate / len(values)), 4)


def _summary_detail_overlap(content: str) -> float:
    """Return the highest normalized sentence similarity across summary/body."""
    match = re.search(r"^#\s*结论摘要\s*$([\s\S]*?)(?=^#\s+|\Z)", content, re.M)
    if not match:
        return 0.0
    summary = _sentences(match.group(1))
    detail = _sentences(content[match.end():])
    if not summary or not detail:
        return 0.0
    highest = 0.0
    for left in summary:
        normalized_left = re.sub(r"\s+|\[\d+\]|[*#]", "", left).casefold()
        for right in detail:
            normalized_right = re.sub(r"\s+|\[\d+\]|[*#]", "", right).casefold()
            if len(normalized_left) < 8 or len(normalized_right) < 8:
                continue
            highest = max(highest, SequenceMatcher(None, normalized_left, normalized_right).ratio())
    return round(highest, 4)


def evaluate_report_quality(
    *,
    content: str,
    brief: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    answer_contract: dict[str, Any] | None,
    insight_metrics: dict[str, Any] | None = None,
) -> QualityGateResult:
    text = str(content or "").strip()
    issues: list[str] = []
    sentences = _sentences(text)
    if not text:
        issues.append("no_content")
    if _RAW_ARTIFACT.search(text):
        issues.append("raw_artifact_citation")
    raw_snippet_count = len(_RAW_SNIPPET.findall(text))
    raw_snippet_count += len(_RAW_URL.findall(text))
    if raw_snippet_count:
        issues.append("raw_search_snippet")
    if any(_INTERNAL_TITLE.match(line.strip()) for line in text.splitlines()):
        issues.append("internal_question_title")
    broken = [
        item for item in sentences
        if not _is_structural_sentence(item) and not complete_sentence(item)[0]
    ]
    if broken:
        issues.append("broken_sentence")
    duplicate_ratio = _duplicate_ratio(sentences)
    if duplicate_ratio > 0.05:
        issues.append("duplicate_claims")
    summary_overlap = _summary_detail_overlap(text)
    if summary_overlap > 0.85:
        issues.append("summary_detail_overlap")
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
    raw_source_requirements = brief.get("source_requirements")
    source_requirements: dict[str, Any] = (
        raw_source_requirements if isinstance(raw_source_requirements, dict) else {}
    )
    primary_required = bool(source_requirements.get("primary_required"))
    # Source quality remains visible for every delivery.  It becomes a
    # release-blocking report defect only when the user or Brief explicitly
    # requires primary evidence; otherwise a grounded, independently sourced
    # answer must not be rejected solely because the ledger also retains weak
    # discovery artifacts from partial workers.
    if evidence_records and primary_required and quality["high_authority_source_ratio"] < 0.6:
        issues.append("weak_sources")
    analytical = str(brief.get("user_intent") or "") in {
        "trend_forecast", "comparison", "recommendation", "explanation", "structured_report"
    }
    lower = text.casefold()
    has_forecast = analytical and any(token in lower for token in _FORECAST)
    metric_values = dict(insight_metrics or {})
    forecast_contract_valid = (
        float(metric_values.get("forecast_milestone_coverage") or 0.0) >= 1.0
        and float(metric_values.get("forecast_uncertainty_coverage") or 0.0) >= 1.0
    )
    if has_forecast and not forecast_contract_valid and (
        not any(token in lower for token in _MECHANISM)
        or not any(token in lower for token in _MILESTONE)
        or not any(token in lower for token in _UNCERTAINTY)
    ):
        issues.append("forecast_missing_contract")
    strong_lines = [item for item in sentences if any(token in item for token in ("一定", "必然", "唯一", "超过"))]
    if any(not _CITATION.search(item) for item in strong_lines):
        issues.append("unsupported_strong_claim")
    if any(token in issues for token in ("no_content", "raw_artifact_citation", "raw_search_snippet", "missing_question")):
        verdict: QualityVerdict = "FAIL"
    elif issues:
        verdict = "REPAIRABLE"
    else:
        verdict = "PASS"
    return QualityGateResult(verdict, tuple(dict.fromkeys(issues)), {
        **quality,
        "broken_sentence_count": len(broken),
        "duplicate_claim_ratio": duplicate_ratio,
        "canonical_duplicate_claim_ratio": float(metric_values.get("duplicate_claim_ratio") or 0.0),
        "summary_detail_overlap": summary_overlap,
        "raw_snippet_count": raw_snippet_count,
        "claim_evidence_coverage": float(metric_values.get("claim_evidence_coverage") or 0.0),
        "source_upgrade_attempts": int(metric_values.get("source_upgrade_attempts") or 0),
        "source_upgrade_queued": int(metric_values.get("source_upgrade_queued") or 0),
        "signal_count": int(metric_values.get("signal_count") or 0),
        "mechanism_count": int(metric_values.get("mechanism_count") or 0),
        "insight_density": float(metric_values.get("insight_density") or 0.0),
        "citation_count": citation_count,
        "unsupported_claim_count": int("unsupported_strong_claim" in issues),
        "forecast_uncertainty_coverage": not has_forecast or "forecast_missing_contract" not in issues,
        "forecast_milestone_coverage": not has_forecast or "forecast_missing_contract" not in issues,
        "source_quality_warning": bool(evidence_records and quality["high_authority_source_ratio"] < 0.6),
        "primary_source_required": primary_required,
    })


__all__ = ["QualityGateResult", "evaluate_report_quality"]

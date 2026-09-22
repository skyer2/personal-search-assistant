"""Answerability, deterministic recovery, and final-answer completeness contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Literal

ClaimType = Literal["fact", "inference", "forecast", "attributed_opinion"]


@dataclass(frozen=True)
class QuestionAnswer:
    question_id: str
    direct_answer: str
    reasoning: list[str] = field(default_factory=list)
    finding_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 0.0
    limitations: list[str] = field(default_factory=list)
    claim_type: ClaimType = "fact"
    display_title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FinalAnswer:
    objective: str
    answers: list[QuestionAnswer]
    overall_summary: str
    synthesis_mode: Literal["primary", "compact_retry", "deterministic_recovery"]
    synthesis_degraded: bool
    unresolved_questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "answers": [item.to_dict() for item in self.answers],
            "overall_summary": self.overall_summary,
            "synthesis_mode": self.synthesis_mode,
            "synthesis_degraded": self.synthesis_degraded,
            "unresolved_questions": list(self.unresolved_questions),
        }


@dataclass(frozen=True)
class QuestionAnswerability:
    question_id: str
    answerable: bool
    supporting_findings: list[str] = field(default_factory=list)
    supporting_evidence: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnswerabilityResult:
    answerable: bool
    question_status: list[QuestionAnswerability]
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "answerable": self.answerable,
            "question_status": [item.to_dict() for item in self.question_status],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AnswerCompletenessResult:
    complete: bool
    answered_count: int
    total_questions: int
    direct_answer_count: int
    inference_count: int
    forecast_count: int
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_STOPWORDS = {"的", "和", "与", "及", "或", "在", "是", "有", "什么", "哪些", "如何"}
_TREND_WORDS = ("趋势", "未来", "发展", "怎么看", "判断", "为什么", "影响", "方向", "区别")
_REFUSAL_PREFIXES = ("已有以下信息", "目前可确认", "证据如下", "当前无法可靠确认")


def _tokens(value: str) -> set[str]:
    output: set[str] = set()
    for item in re.findall(r"[A-Za-z0-9]+|[\u3400-\u9fff]", str(value or "").lower()):
        if item not in _STOPWORDS:
            output.add(item)
    return output


def _refs(finding: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("evidence_ids", "artifact_ids", "source_ids", "sources"):
        raw = finding.get(key) or []
        raw = [raw] if isinstance(raw, str) else raw
        refs.extend(str(item).strip() for item in raw if str(item).strip())
    for key in ("evidence_id", "artifact_id", "source_id", "artifact_ref", "source"):
        value = str(finding.get(key) or "").strip()
        if value:
            refs.append(value)
    return list(dict.fromkeys(refs))


def _claim(finding: dict[str, Any]) -> str:
    claims = finding.get("claims")
    if isinstance(claims, (list, tuple)) and claims:
        return str(claims[0] or "").strip()
    return str(finding.get("claim") or finding.get("text") or finding.get("summary") or "").strip()


def _finding_id(finding: dict[str, Any], index: int) -> str:
    return str(
        finding.get("finding_id")
        or finding.get("claim_id")
        or finding.get("task_id")
        or f"finding-{index}"
    )


def assess_answerability(
    *,
    brief: Any,
    findings: list[dict[str, Any]],
    evidence_records: list[dict[str, Any]],
    coverage: dict[str, Any] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> AnswerabilityResult:
    """Assess whether each key question has enough material for an answer."""
    questions = list(getattr(brief, "key_questions", None) or [])
    if not questions:
        questions = [str(getattr(brief, "objective", "") or "")]
    coverage_sufficient = bool((coverage or {}).get("sufficient"))
    records: set[str] = set()
    for row in evidence_records:
        for key in ("evidence_id", "artifact_ref", "artifact_id", "source_id", "source"):
            value = str(row.get(key) or "").strip()
            if value:
                records.add(value)
    usable_conflict = any(
        str(row.get("status") or "") == "unresolved" and bool(row.get("blocking"))
        for row in conflicts or [] if isinstance(row, dict)
    )
    statuses: list[QuestionAnswerability] = []
    for index, question in enumerate(questions, 1):
        qid = f"q{index}"
        qtokens = _tokens(str(question))
        ranked: list[tuple[float, dict[str, Any]]] = []
        for finding in findings:
            claim = _claim(finding)
            refs = _refs(finding)
            if not claim or not refs:
                continue
            overlap = len(qtokens & _tokens(claim)) / max(1, len(qtokens))
            ranked.append((overlap, finding))
        ranked.sort(key=lambda row: row[0], reverse=True)
        selected = [row for score, row in ranked if score >= 0.12]
        if not selected and coverage_sufficient:
            selected = [row for _score, row in ranked[:3]]
        finding_refs = [_finding_id(row, i) for i, row in enumerate(selected)]
        evidence_refs = list(dict.fromkeys(ref for row in selected for ref in _refs(row) if ref in records))
        answerable = bool(selected and evidence_refs) and not usable_conflict
        missing: list[str] = []
        if not selected:
            missing.append("supporting_finding")
        if not evidence_refs:
            missing.append("supporting_evidence")
        if usable_conflict:
            missing.append("blocking_unresolved_conflict")
        statuses.append(QuestionAnswerability(qid, answerable, finding_refs, evidence_refs, missing))
    answerable = bool(statuses) and all(item.answerable for item in statuses)
    reason = "coverage_sufficient" if answerable and coverage_sufficient else "all_key_questions_supported" if answerable else "one_or_more_key_questions_unanswerable"
    return AnswerabilityResult(answerable, statuses, reason)


def compile_deterministic_answer(
    *,
    objective: str,
    brief: Any,
    findings: list[dict[str, Any]],
    answerability: AnswerabilityResult,
    synthesis_degraded: bool = True,
) -> FinalAnswer:
    """Compile a direct, evidence-bound answer without calling tools or an LLM."""
    questions = list(getattr(brief, "key_questions", None) or []) or [objective]
    by_id = {_finding_id(row, i): row for i, row in enumerate(findings) if isinstance(row, dict)}
    answers: list[QuestionAnswer] = []
    unresolved: list[str] = []
    for index, question in enumerate(questions, 1):
        qid = f"q{index}"
        status = next((item for item in answerability.question_status if item.question_id == qid), None)
        if status is None or not status.answerable:
            unresolved.append(str(question))
            answers.append(QuestionAnswer(qid, "当前证据不足，无法可靠回答这一问题。", limitations=["缺少可绑定的支持证据"], display_title=_display_title(str(question), index)))
            continue
        selected = [by_id[item] for item in status.supporting_findings if item in by_id]
        if not selected:
            selected = [row for row in findings if isinstance(row, dict) and _claim(row)][:3]
        claims = [_claim(row) for row in selected if _claim(row)]
        # A recovery is still an answer, not a replay of every worker finding.
        direct = claims[0][:420] if claims else ""
        if not direct:
            direct = "基于现有证据，可以形成方向性判断，但细节仍需继续核验。"
        trend = any(word in str(question) for word in _TREND_WORDS)
        explicit_types = [str(row.get("claim_type") or "") for row in selected]
        claim_type: ClaimType = "forecast" if trend and ("forecast" in explicit_types or "未来" in str(question)) else "inference" if trend else "fact"
        if trend and claim_type == "forecast":
            direct = f"基于当前证据，我判断：{direct}"
        reasoning = claims[1:3] if len(claims) > 1 else ["该判断仅覆盖本次已登记且可绑定的来源。"]
        confidence = min(1.0, max(0.35, sum(float(row.get("confidence") or 0.6) for row in selected[:3]) / max(1, len(selected[:3]))))
        answers.append(QuestionAnswer(qid, direct, reasoning, status.supporting_findings, status.supporting_evidence, confidence, claim_type=claim_type, display_title=_display_title(str(question), index)))
    summary = answers[0].direct_answer if answers else "当前没有可生成的回答。"
    return FinalAnswer(objective, answers, summary, "deterministic_recovery", synthesis_degraded, unresolved)


def assess_answer_completeness(final_answer: FinalAnswer, brief: Any) -> AnswerCompletenessResult:
    questions = list(getattr(brief, "key_questions", None) or []) or [final_answer.objective]
    issues: list[str] = []
    direct_count = 0
    inference_count = 0
    forecast_count = 0
    for index, question in enumerate(questions, 1):
        answer = next((item for item in final_answer.answers if item.question_id == f"q{index}"), None)
        if answer is None:
            issues.append(f"missing_question:q{index}")
            continue
        text = answer.direct_answer.strip()
        if len(text) < 8 or text.startswith(_REFUSAL_PREFIXES):
            issues.append(f"empty_direct_answer:q{index}")
        else:
            direct_count += 1
        if not answer.evidence_refs:
            issues.append(f"missing_evidence:q{index}")
        if answer.claim_type == "inference":
            inference_count += 1
        if answer.claim_type == "forecast":
            forecast_count += 1
        if any(word in str(question) for word in _TREND_WORDS) and answer.claim_type not in {"inference", "forecast"}:
            issues.append(f"missing_judgement:q{index}")
        if any(word in str(question) for word in _TREND_WORDS) and answer.claim_type in {"inference", "forecast"}:
            if len(answer.finding_refs) < 2 and answer.confidence < 0.85:
                issues.append(f"insufficient_judgement_support:q{index}")
    return AnswerCompletenessResult(not issues and len(final_answer.answers) == len(questions), direct_count, len(questions), direct_count, inference_count, forecast_count, tuple(issues))


def render_final_answer(answer: FinalAnswer, *, citation_numbers: dict[str, int] | None = None) -> str:
    citation_numbers = citation_numbers or {}
    lines = ["## 直接回答", "", answer.overall_summary, "", "## 关键判断", ""]
    for item in answer.answers:
        refs = "".join(f"[{citation_numbers[ref]}]" for ref in item.evidence_refs if ref in citation_numbers)
        lines.append(f"### {item.display_title or _display_title(item.question_id, 0)}")
        lines.append("")
        lines.append(f"{item.direct_answer}{(' ' + refs) if refs else ''}")
        if item.reasoning:
            lines.extend(["", "依据：", *[f"- {row}" for row in item.reasoning]])
        lines.append("")
    if answer.unresolved_questions:
        lines.extend(["## 尚未回答", "", *[f"- {row}" for row in answer.unresolved_questions], ""])
    lines.extend(["## 限制", "", "以上判断仅基于本次已登记证据；预测性内容明确标注为方向性判断。", ""])
    return "\n".join(lines).strip() + "\n"


def _display_title(question: str, index: int) -> str:
    value = str(question or "").strip()
    if "热点" in value or "当前" in value:
        return "2026 当前热点"
    if "未来" in value or "方向" in value or "趋势" in value:
        return "未来 1–2 年方向"
    if value.startswith("q") and value[1:].isdigit():
        return f"关键判断 {value[1:]}"
    return value[:60] or f"关键判断 {index or 1}"


__all__ = [
    "AnswerabilityResult", "AnswerCompletenessResult", "ClaimType", "FinalAnswer",
    "QuestionAnswer", "QuestionAnswerability", "assess_answerability",
    "assess_answer_completeness", "compile_deterministic_answer", "render_final_answer",
]

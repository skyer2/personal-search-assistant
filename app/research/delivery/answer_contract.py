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
    synthesis_mode: Literal[
        "primary", "compact_retry", "deterministic_recovery", "evidence_bound_recovery"
    ]
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
    status: Literal["UNANSWERABLE", "PARTIAL", "SUPPORTED", "STRONG"] = "UNANSWERABLE"
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
    validated_claims: list[dict[str, Any]] | None = None,
) -> AnswerabilityResult:
    """Assess each question by explicit claim lineage, never lexical overlap.

    Runtime callers pass ``validated_claims`` from the claim-admission
    boundary.  Historical persisted rows without a ``validated`` field are
    accepted only when they retain explicit question lineage and evidence;
    a row explicitly marked ``validated=False`` is never promoted.
    """
    questions = list(getattr(brief, "key_questions", None) or [])
    if not questions:
        questions = [str(getattr(brief, "objective", "") or "")]
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
    rows = validated_claims if validated_claims is not None else findings
    claim_rows = [
        row for row in rows or []
        if isinstance(row, dict)
        and bool(row.get("validated", True))
        and (
            str(row.get("question_id") or "").strip()
            or any(str(item).strip() for item in row.get("question_ids") or [])
        )
    ]
    statuses: list[QuestionAnswerability] = []
    for index, question in enumerate(questions, 1):
        qid = f"q{index}"
        selected: list[dict[str, Any]] = []
        for finding in claim_rows:
            question_ids = finding.get("question_ids") or []
            question_ids = [question_ids] if isinstance(question_ids, str) else question_ids
            lineage = {str(finding.get("question_id") or "").strip()}
            lineage.update(str(item).strip() for item in question_ids if str(item).strip())
            if qid not in lineage:
                continue
            claim = _claim(finding)
            refs = _refs(finding)
            if not claim or not refs:
                continue
            selected.append(finding)
        finding_refs = [_finding_id(row, i) for i, row in enumerate(selected)]
        evidence_refs = list(dict.fromkeys(ref for row in selected for ref in _refs(row) if ref in records))
        high_quality = sum(float(row.get("authority_score") or row.get("source_quality_score") or 0.0) >= 0.75 for row in selected)
        answerable = bool(selected and evidence_refs) and not usable_conflict
        missing: list[str] = []
        if not selected:
            missing.append("supporting_finding")
        if not evidence_refs:
            missing.append("supporting_evidence")
        if usable_conflict:
            missing.append("blocking_unresolved_conflict")
        status: Literal["UNANSWERABLE", "PARTIAL", "SUPPORTED", "STRONG"]
        if not selected or not evidence_refs:
            status = "UNANSWERABLE"
        elif usable_conflict or len(selected) < 2:
            status = "PARTIAL"
        elif high_quality >= 2 and len(set(evidence_refs)) >= 2:
            status = "STRONG"
        else:
            status = "SUPPORTED"
        statuses.append(QuestionAnswerability(qid, answerable, status, finding_refs, evidence_refs, missing))
    answerable = bool(statuses) and all(item.answerable for item in statuses)
    reason = "all_key_questions_explicitly_supported" if answerable else "one_or_more_key_questions_unanswerable"
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
    by_id = {
        _finding_id(row, i): row
        for i, row in enumerate(findings)
        if isinstance(row, dict)
        and bool(row.get("validated", True))
        and (
            str(row.get("question_id") or "").strip()
            or any(str(item).strip() for item in row.get("question_ids") or [])
        )
    }
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
        claims = [_claim(row) for row in selected if _claim(row)]
        # A recovery only organizes validated claims.  It never promotes a
        # snippet or fabricates a bridge from an unrelated question.
        direct = claims[0][:420] if claims else "当前证据不足，无法可靠回答这一问题。"
        explicit_types = [str(row.get("claim_type") or "") for row in selected]
        # A recovery must retain the worker-declared claim type.  The wording
        # of a future-oriented question is not evidence of a forecast.
        claim_type: ClaimType = (
            "forecast"
            if "forecast" in explicit_types
            else "inference"
            if "inference" in explicit_types
            else "fact"
        )
        reasoning = claims[1:3] if len(claims) > 1 else ["该判断仅覆盖本次已登记且可绑定的来源。"]
        confidence = min(1.0, max(0.35, sum(float(row.get("confidence") or 0.6) for row in selected[:3]) / max(1, len(selected[:3]))))
        limitations: list[str] = []
        # A deterministic recovery may make a bounded inference only from
        # multiple independently bound directional signals. It never upgrades
        # a present-tense fact into a forecast.
        trend_question = any(word in str(question) for word in _TREND_WORDS)
        directional_signal = any(
            any(token in claim for token in ("未来", "预计", "预测", "趋势", "方向", "路线图", "将"))
            for claim in claims
        )
        if (
            trend_question
            and claim_type == "fact"
            and directional_signal
            and len(set(status.supporting_evidence)) >= 2
            and len(selected) >= 2
        ):
            claim_type = "inference"
            direct = f"从本次可绑定来源的共同信号看，{direct}"
            limitations.append(
                "这是基于多来源信号的方向性归纳，因为共同驱动来自产品落地、采用和可靠性要求；可观察里程碑是后续产品落地、采用和可靠性指标。不确定性在于结果仍受技术、成本与监管变化影响。"
            )
        answers.append(QuestionAnswer(qid, direct, reasoning, status.supporting_findings, status.supporting_evidence, confidence, limitations, claim_type=claim_type, display_title=_display_title(str(question), index)))
    summary = answers[0].direct_answer if answers else "当前没有可生成的回答。"
    # This deterministic compiler is deliberately evidence-bound: it only
    # arranges admitted worker claims and never upgrades them into a forecast
    # or a primary-synthesis result.
    return FinalAnswer(objective, answers, summary, "evidence_bound_recovery", synthesis_degraded, unresolved)


def compile_evidence_bound_answer(
    *,
    objective: str,
    brief: Any,
    findings: list[dict[str, Any]],
    answerability: AnswerabilityResult,
    synthesis_degraded: bool = True,
) -> FinalAnswer:
    """Build a recovery answer solely from explicitly bound claim text.

    This compatibility entry point shares the admission guard with the v6
    deterministic compiler and records that the delivery was evidence-bound
    recovery rather than a primary synthesis attempt.
    """
    return compile_deterministic_answer(
        objective=objective,
        brief=brief,
        findings=findings,
        answerability=answerability,
        synthesis_degraded=synthesis_degraded,
    )


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
    current = [item for item in answer.answers if item.claim_type != "forecast"]
    future = [item for item in answer.answers if item.claim_type == "forecast"]
    lines = ["# 结论摘要", ""]
    if answer.synthesis_mode == "evidence_bound_recovery":
        lines.extend([
            "> 本结果为降级部分交付，只展示已有证据能够确认的内容。",
            "",
        ])
    lines.extend([f"- {answer.overall_summary}", "", "# 当前研究结论", ""])
    for item in current:
        refs = "".join(f"[{citation_numbers[ref]}]" for ref in item.evidence_refs[:3] if ref in citation_numbers)
        lines.append(f"## {item.display_title or _display_title(item.question_id, 0)}")
        lines.append("")
        lines.append(f"**结论**：{item.direct_answer}{(' ' + refs) if refs else ''}")
        if item.reasoning:
            lines.extend(["", "**依据与限制**：", *[f"- {row}" for row in item.reasoning]])
        lines.append("")
    lines.extend(["# 未来 1~2 年方向", ""])
    for item in future:
        refs = "".join(f"[{citation_numbers[ref]}]" for ref in item.evidence_refs[:3] if ref in citation_numbers)
        lines.extend([f"## {item.display_title or '方向性判断'}", "", f"**方向判断**：{item.direct_answer}{(' ' + refs) if refs else ''}", "", "**不确定性**：该判断仅基于本次已登记证据，需以后续可观察结果验证。", ""])
    if not future:
        lines.append("现有证据以当前状态为主；未来判断应以可观察里程碑和不确定性为边界。\n")
    if answer.unresolved_questions:
        lines.extend(["# 主要不确定性", "", *[f"- {row}" for row in answer.unresolved_questions], ""])
    lines.extend(["# 综合判断", "", "这些结论需要结合来源质量、证据独立性和后续变化持续复核，而不能替代新的事实核验。", ""])
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
    "assess_answer_completeness", "compile_deterministic_answer",
    "compile_evidence_bound_answer", "render_final_answer",
]

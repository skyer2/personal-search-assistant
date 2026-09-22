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
        "primary",
        "compact_retry",
        "deterministic_recovery",
        "evidence_bound_recovery",
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
    supporting_findings: list[str] = field(default_factory=list)
    supporting_evidence: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    ask_id: str = ""
    binding: str = "lineage"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AskAnswerability:
    ask_id: str
    answerable: bool
    question_ids: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnswerabilityResult:
    answerable: bool
    question_status: list[QuestionAnswerability]
    reason: str = ""
    ask_status: list[AskAnswerability] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answerable": self.answerable,
            "question_status": [item.to_dict() for item in self.question_status],
            "ask_status": [item.to_dict() for item in self.ask_status],
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


def _declared_lineage(finding: dict[str, Any]) -> set[str]:
    """Explicit question/criterion lineage a worker or planner bound to a finding."""
    values: set[str] = set()
    for key in ("question_ids", "question_id", "criterion_ids", "criterion_id", "supported_criteria", "ask_ids", "ask_id"):
        raw = finding.get(key) or []
        raw = [raw] if isinstance(raw, str) else raw
        values.update(str(item).strip() for item in raw if str(item).strip())
    return values


def _lineage_keys(question_id: str, question: str, ask_id: str) -> set[str]:
    keys = {question_id, question.strip(), ask_id}
    return {item for item in keys if item}


def assess_answerability(
    *,
    brief: Any,
    findings: list[dict[str, Any]],
    evidence_records: list[dict[str, Any]],
    coverage: dict[str, Any] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> AnswerabilityResult:
    """Assess answerability from explicit lineage, not keyword similarity.

    A finding counts for a question when it declares that question/criterion/ask
    id. Token overlap remains only as a last-resort bridge for payloads that
    carry no lineage at all, and is recorded as ``binding="token_overlap"``.
    """
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
    coverage_criteria = [
        row
        for row in (coverage or {}).get("criteria") or []
        if isinstance(row, dict)
    ]
    any_lineage = any(_declared_lineage(row) for row in findings if isinstance(row, dict))
    statuses: list[QuestionAnswerability] = []
    for index, question in enumerate(questions, 1):
        qid = f"q{index}"
        ask_id = ""
        resolver = getattr(brief, "ask_id_for_question_index", None)
        if callable(resolver):
            ask_id = str(resolver(index) or "")
        keys = _lineage_keys(qid, str(question), ask_id)
        if index <= len(coverage_criteria):
            criterion_id = str(
                coverage_criteria[index - 1].get("criterion_id") or ""
            )
            if criterion_id:
                keys.add(criterion_id)
        bound = [
            finding
            for finding in findings
            if isinstance(finding, dict)
            and _claim(finding)
            and _refs(finding)
            and (_declared_lineage(finding) & keys)
        ]
        binding = "lineage"
        selected = bound
        if not selected and not any_lineage:
            qtokens = _tokens(str(question))
            ranked = sorted(
                (
                    (len(qtokens & _tokens(_claim(finding))) / max(1, len(qtokens)), finding)
                    for finding in findings
                    if isinstance(finding, dict) and _claim(finding) and _refs(finding)
                ),
                key=lambda row: row[0],
                reverse=True,
            )
            selected = [row for score, row in ranked if score >= 0.12]
            if not selected and coverage_sufficient:
                selected = [row for _score, row in ranked[:3]]
            binding = "token_overlap"
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
        statuses.append(
            QuestionAnswerability(
                qid, answerable, finding_refs, evidence_refs, missing,
                ask_id=ask_id, binding=binding,
            )
        )
    answerable = bool(statuses) and all(item.answerable for item in statuses)
    reason = (
        "coverage_sufficient"
        if answerable and coverage_sufficient
        else "all_key_questions_supported"
        if answerable
        else "one_or_more_key_questions_unanswerable"
    )
    return AnswerabilityResult(answerable, statuses, reason, _ask_status(brief, statuses))


def _ask_status(brief: Any, statuses: list[QuestionAnswerability]) -> list[AskAnswerability]:
    """Roll question answerability up to the user's asks."""
    asks = list(getattr(brief, "user_asks", None) or ())
    if not asks:
        return []
    by_ask: dict[str, list[QuestionAnswerability]] = {}
    for item in statuses:
        if item.ask_id:
            by_ask.setdefault(item.ask_id, []).append(item)
    output: list[AskAnswerability] = []
    for ask in asks:
        ask_id = str(getattr(ask, "ask_id", "") or "")
        mapped = by_ask.get(ask_id) or []
        answerable = bool(mapped) and any(item.answerable for item in mapped)
        missing: list[str] = []
        if not mapped:
            missing.append("no_research_question")
        elif not answerable:
            missing.append("no_answerable_research_question")
        output.append(
            AskAnswerability(
                ask_id=ask_id,
                answerable=answerable,
                question_ids=[item.question_id for item in mapped],
                evidence_refs=list(
                    dict.fromkeys(ref for item in mapped for ref in item.supporting_evidence)
                ),
                missing_requirements=missing,
            )
        )
    return output


def compile_evidence_bound_answer(
    *,
    objective: str,
    brief: Any,
    findings: list[dict[str, Any]],
    answerability: AnswerabilityResult,
    synthesis_degraded: bool = True,
) -> FinalAnswer:
    """Recover an answer from validated claims only.

    This is EvidenceBoundRecovery: it may under-answer, but it must never invent
    a mechanism, a trend label or a forecast. Every sentence here is either a
    claim the workers produced or an explicit statement that evidence is missing.
    """
    questions = list(getattr(brief, "key_questions", None) or []) or [objective]
    by_id = {_finding_id(row, i): row for i, row in enumerate(findings) if isinstance(row, dict)}
    answers: list[QuestionAnswer] = []
    unresolved: list[str] = []
    for index, question in enumerate(questions, 1):
        qid = f"q{index}"
        status = next((item for item in answerability.question_status if item.question_id == qid), None)
        if status is None or not status.answerable:
            unresolved.append(str(question))
            answers.append(
                QuestionAnswer(
                    qid,
                    "当前证据不足，无法可靠回答这一问题。",
                    limitations=["缺少可绑定的支持证据"],
                    display_title=_display_title(str(question), index),
                )
            )
            continue
        selected = [by_id[item] for item in status.supporting_findings if item in by_id]
        claims = [_claim(row) for row in selected if _claim(row)]
        if not claims:
            # No verbatim claim bound to this question: do not fabricate one.
            unresolved.append(str(question))
            answers.append(
                QuestionAnswer(
                    qid,
                    "当前证据不足，无法可靠回答这一问题。",
                    finding_refs=status.supporting_findings,
                    evidence_refs=status.supporting_evidence,
                    limitations=["已登记证据未绑定到该问题的可用结论"],
                    display_title=_display_title(str(question), index),
                )
            )
            continue
        direct = "；".join(claims[:3])
        explicit_types = [str(row.get("claim_type") or "") for row in selected]
        # Claim type is read from the worker payload; recovery never upgrades a
        # fact into a forecast on its own.
        claim_type: ClaimType = (
            "forecast"
            if "forecast" in explicit_types
            else "inference"
            if "inference" in explicit_types
            else "fact"
        )
        reasoning = claims[1:4] if len(claims) > 1 else claims[:1]
        confidence = min(
            1.0,
            max(
                0.35,
                sum(float(row.get("confidence") or 0.6) for row in selected[:3])
                / max(1, len(selected[:3])),
            ),
        )
        answers.append(
            QuestionAnswer(
                qid,
                direct,
                reasoning,
                status.supporting_findings,
                status.supporting_evidence,
                confidence,
                claim_type=claim_type,
                display_title=_display_title(str(question), index),
            )
        )
    answered = [item for item in answers if item.evidence_refs and item.reasoning]
    summary = answered[0].direct_answer if answered else "当前证据不足，无法形成可靠综合判断。"
    return FinalAnswer(objective, answers, summary, "evidence_bound_recovery", synthesis_degraded, unresolved)


# Backwards-compatible alias; the behaviour is now evidence-bound recovery.
compile_deterministic_answer = compile_evidence_bound_answer


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
    lines = ["# 结论摘要", "", f"- {answer.overall_summary}", "", "# 当前研究结论", ""]
    for item in current:
        refs = "".join(f"[{citation_numbers[ref]}]" for ref in item.evidence_refs if ref in citation_numbers)
        lines.append(f"## {item.display_title or _display_title(item.question_id, 0)}")
        lines.append("")
        lines.append(f"**结论**：{item.direct_answer}{(' ' + refs) if refs else ''}")
        if item.reasoning:
            lines.extend(["", "**依据与限制**：", *[f"- {row}" for row in item.reasoning]])
        lines.append("")
    if future:
        lines.extend(["# 未来 1~2 年方向", ""])
        for item in future:
            refs = "".join(f"[{citation_numbers[ref]}]" for ref in item.evidence_refs if ref in citation_numbers)
            lines.extend([f"## {item.display_title or '方向性判断'}", "", f"**方向判断**：{item.direct_answer}{(' ' + refs) if refs else ''}", "", "**不确定性**：该判断仅基于本次已登记证据，需以后续可观察结果验证。", ""])
    if answer.unresolved_questions:
        lines.extend(["# 尚未回答", "", *[f"- {row}" for row in answer.unresolved_questions], ""])
    lines.extend(["# 限制", "", "以上内容仅来自本次已登记证据，未超出证据做推断。", ""])
    if answer.synthesis_mode in {"evidence_bound_recovery", "deterministic_recovery"}:
        # Recovery assembled existing claims; the model never wrote this report.
        lines.extend(
            [
                "本结果由运行时从已确认证据恢复生成，未经过完整模型综合，"
                "因此属于降级部分交付，不能视为完整成功。",
                "",
            ]
        )
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
    "AnswerabilityResult", "AnswerCompletenessResult", "AskAnswerability",
    "ClaimType", "FinalAnswer",
    "QuestionAnswer", "QuestionAnswerability", "assess_answerability",
    "assess_answer_completeness", "compile_deterministic_answer",
    "compile_evidence_bound_answer", "render_final_answer",
]

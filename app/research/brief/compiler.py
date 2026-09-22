"""Structured Research Brief compiler.

The LLM compiler is the production authority for open-ended semantics. The
deterministic compiler is only a conservative fallback when no model is
available; it never routes from TaskShape.
"""

from __future__ import annotations

import hashlib
import time
import re
import os
from dataclasses import replace
from typing import Any

from app.research.brief.models import (
    DeliverableRequirements,
    FreshnessRequirements,
    ResearchQuestion,
    SourceRequirements,
    StructuredResearchBrief,
)
from app.research.intent.user_ask import (
    UserAsk,
    UserAskContract,
    compile_user_ask_contract,
)
from app.config.timeouts import model_timeout_sec
from app.research.brief.validator import validate_structured_brief
from app.research.execution.structured_llm_gateway import (
    StructuredLLMGateway,
    emit_semantic_fallback,
)

BRIEF_AGENT_PROMPT = """你是研究任务的结构化 Brief 编译器。理解用户真正目标，不要按表面关键词分类。

用户输入：
{query}

上下文增量：
{conversation_delta}

只输出 JSON，字段如下：
{{"objective":"","user_intent":"atomic_fact|comparison|trend_forecast|recommendation|conflict_analysis|freshness_update|structured_report|explanation|research","explicit_subjects":[],"key_questions":[],"constraints":[],"source_requirements":{{"min_independent_sources":2,"primary_required":false,"preferred":["official","primary"],"forbidden":[]}},"freshness_requirements":{{"required":true,"time_horizon":"recent|point_in_time|historical|any"}},"deliverable":{{"format":"text|markdown|pdf","depth":"brief|standard|long"}},"success_criteria":[],"assumptions":[],"clarification_needed":false,"confidence":0.9}}

要求：区分 atomic fact、trend、forecast、recommendation、comparison、conflict；只保留必要问题；保留 freshness/source/output 显式约束；不确定时 clarification_needed=true。

key_questions 硬约束：
- 用户提出的每一个问题都必须有对应的 key_question，一个都不能丢。
- 必须保留用户原问题里的主体（例如 Agent、Cursor）和时间范围（例如 2026年9月、未来1-2年）。
- 可以把问题改写得更可检索，但不能换成另一个问题。
- 禁止输出与用户问题无关的通用模板问题，例如「当前主要关注点和工程路径是什么？」。
"""

_ATOMICS = ("什么时候", "哪一年", "哪年", "谁", "是什么", "多少", "发布时间", "when", "who", "what")
_OPEN_SIGNALS = (
    "未来", "趋势", "预测", "前景", "进展", "发展", "关注点",
    "值得", "推荐", "为什么", "冲突", "矛盾", "不一致", "最新", "当前", "截至",
    "报告", "深度", "全景", "有哪些", "对比", "比较",
)


def _stable_brief_id(objective: str, version: int) -> str:
    digest = hashlib.sha1(f"{version}|{objective}".encode("utf-8")).hexdigest()[:12]
    return f"brief_{digest}"


def _explicit_subjects(query: str) -> list[str]:
    comparison = re.search(r"(?:对比|比较)(.+?)(?:的|$)", query)
    if not comparison:
        return []
    fragments = re.split(r"[、,，;；/]|和|与|及", comparison.group(1))
    subjects = [item.strip() for item in fragments if item.strip()]
    return subjects if 2 <= len(subjects) <= 12 else []


def _intent(query: str) -> str:
    lowered = query.lower()
    if any(token in query for token in ("冲突", "矛盾", "不一致", "争议")):
        return "conflict_analysis"
    if any(token in lowered for token in ("对比", "比较", " vs ", "versus")):
        return "comparison"
    if any(token in query for token in ("报告", "深度报告", "调研报告")):
        return "structured_report"
    if any(token in query for token in ("未来", "趋势", "预测", "前景")):
        return "trend_forecast"
    if any(token in query for token in ("值得", "推荐", "应该选", "有哪些")):
        return "recommendation"
    if any(token in query for token in ("最新", "当前", "现在", "截至")):
        return "freshness_update"
    if any(token in query for token in ("为什么", "原因", "为何")):
        return "explanation"
    atomic = any(token in query or token in lowered for token in _ATOMICS)
    if atomic and len(query) <= 48 and not any(token in query or token in lowered for token in _OPEN_SIGNALS):
        return "atomic_fact"
    return "research"


def _research_question_text(ask: UserAsk) -> str:
    """Keep the user's own question text.

    Evidence requirements live in ``UserAsk.answer_requirements``; they must not
    be appended here, or every downstream matcher (coverage, answerability)
    would be diluted by boilerplate tokens.
    """
    return ask.text.strip().rstrip("？?。.！!")


def _questions_from_contract(contract: UserAskContract) -> list[ResearchQuestion]:
    questions: list[ResearchQuestion] = []
    for index, ask in enumerate(contract.asks, 1):
        text = _research_question_text(ask)
        if not text:
            continue
        questions.append(ResearchQuestion(f"q{len(questions) + 1}", ask.ask_id, text))
        _ = index
    return questions[:8]


def _freshness(intent: str, query: str) -> FreshnessRequirements:
    if intent in {"trend_forecast", "freshness_update"} or any(token in query for token in ("最新", "当前", "现在", "截至")):
        return FreshnessRequirements(required=True, time_horizon="recent")
    if intent == "atomic_fact":
        return FreshnessRequirements(required=False, time_horizon="point_in_time")
    return FreshnessRequirements(required=False, time_horizon="any")


def _source(intent: str) -> SourceRequirements:
    # A recommendation changes a user's decision.  Like an atomic fact, its
    # core claims need original or independently corroborated support; a
    # generic web result may remain a diagnostic signal but cannot close the
    # Completion Contract by itself.
    if intent in {"atomic_fact", "recommendation"}:
        return SourceRequirements(min_independent_sources=1, primary_required=True)
    return SourceRequirements(min_independent_sources=2, primary_required=False)


def _deliverable(query: str, intent: str) -> DeliverableRequirements:
    lowered = query.lower()
    if "pdf" in lowered:
        return DeliverableRequirements(format="pdf", depth="long")
    if intent in {"structured_report", "comparison", "trend_forecast"} or "报告" in query:
        return DeliverableRequirements(format="markdown", depth="long")
    if intent == "atomic_fact":
        return DeliverableRequirements(format="text", depth="brief")
    return DeliverableRequirements(format="text", depth="standard")


def compile_structured_brief(
    query: str,
    *,
    conversation_delta: str = "",
    existing_brief: dict[str, Any] | None = None,
) -> StructuredResearchBrief:
    """Query-preserving deterministic fallback.

    This path must never re-invent the user's question from keywords. It splits
    the original query into asks and keeps each ask verbatim inside its research
    question, so a Brief LLM failure degrades understanding, not intent.
    """
    objective = " ".join(part for part in (query, conversation_delta) if part).strip()
    intent = _intent(objective)
    contract = compile_user_ask_contract(query, conversation_delta=conversation_delta)
    subjects = _explicit_subjects(objective)
    if not subjects:
        derived = [ask.subject for ask in contract.asks if ask.subject.strip()]
        subjects = list(dict.fromkeys(derived))[:12]
    if not subjects and intent == "atomic_fact":
        subjects = [objective[:120]]
    questions = _questions_from_contract(contract)
    if not questions and objective:
        questions = [ResearchQuestion("q1", "A1", objective)]
    success = ["回答直接对齐用户目标。", "结论有一手来源或独立高质量来源支持。", "明确区分事实、推断和未确认内容。"]
    if intent == "comparison":
        success.append("显式主体均被覆盖，不引入未要求对象。")
    if any(ask.ask_type == "forecast" for ask in contract.asks):
        success.append("区分当前事实与未来预测。")
    version = 1
    assumptions: tuple[str, ...] = ()
    if existing_brief:
        previous = StructuredResearchBrief.from_dict(existing_brief)
        version = previous.version + 1
        assumptions = previous.assumptions
    brief = StructuredResearchBrief(
        brief_id=_stable_brief_id(objective, version), version=version, objective=objective,
        user_intent=intent, explicit_subjects=tuple(subjects),
        key_questions=tuple(item.text for item in questions),
        user_asks=tuple(contract.asks),
        research_questions=tuple(questions),
        constraints=("不要扩大到用户未要求的研究范围。",), source_requirements=_source(intent),
        freshness_requirements=_freshness(intent, objective), deliverable=_deliverable(objective, intent),
        success_criteria=tuple(success), assumptions=assumptions,
        clarification_needed=not bool(objective.strip()), compiler_source="deterministic_fallback",
        confidence=0.55, raw_query=query,
    )
    if validate_structured_brief(brief):
        return replace(brief, clarification_needed=True)
    return brief


def _ask_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", str(text or "").casefold())
    }


def _align_questions_to_asks(
    texts: list[str],
    asks: tuple[UserAsk, ...],
) -> tuple[ResearchQuestion, ...]:
    """Bind LLM-rewritten questions back onto the user's asks.

    Lineage is assigned by best token overlap, then every unmapped required ask
    gets its verbatim question appended. A rewritten Brief may add detail; it may
    not silently drop one of the user's asks.
    """
    if not asks:
        return tuple(
            ResearchQuestion(f"q{index}", "", text)
            for index, text in enumerate(texts, 1)
            if str(text).strip()
        )
    ask_tokens = {ask.ask_id: _ask_tokens(f"{ask.text} {ask.subject}") for ask in asks}
    questions: list[ResearchQuestion] = []
    for text in texts:
        cleaned = str(text or "").strip()
        if not cleaned:
            continue
        tokens = _ask_tokens(cleaned)
        best_id, best_score = asks[0].ask_id, -1.0
        for ask in asks:
            reference = ask_tokens.get(ask.ask_id) or set()
            if not reference:
                continue
            score = len(tokens & reference) / max(1, len(reference))
            if score > best_score:
                best_id, best_score = ask.ask_id, score
        questions.append(ResearchQuestion(f"q{len(questions) + 1}", best_id, cleaned))
    mapped = {item.ask_id for item in questions}
    for ask in asks:
        if not ask.required or ask.ask_id in mapped:
            continue
        text = _research_question_text(ask)
        if text:
            questions.append(ResearchQuestion(f"q{len(questions) + 1}", ask.ask_id, text))
    return tuple(questions[:8])


def _merge_llm_brief(fallback: StructuredResearchBrief, patch: dict[str, Any]) -> StructuredResearchBrief:
    source_raw = patch.get("source_requirements")
    freshness_raw = patch.get("freshness_requirements")
    deliverable_raw = patch.get("deliverable")
    source: dict[str, Any] = source_raw if isinstance(source_raw, dict) else {}
    freshness: dict[str, Any] = freshness_raw if isinstance(freshness_raw, dict) else {}
    deliverable: dict[str, Any] = deliverable_raw if isinstance(deliverable_raw, dict) else {}
    questions = _align_questions_to_asks(
        [str(item) for item in patch.get("key_questions") or fallback.key_questions],
        fallback.user_asks,
    )
    brief = StructuredResearchBrief(
        brief_id=fallback.brief_id, version=fallback.version,
        objective=str(patch.get("objective") or fallback.objective),
        user_intent=str(patch.get("user_intent") or fallback.user_intent),
        explicit_subjects=tuple(str(item) for item in patch.get("explicit_subjects") or fallback.explicit_subjects)[:12],
        key_questions=tuple(item.text for item in questions) or fallback.key_questions,
        user_asks=fallback.user_asks,
        research_questions=questions or fallback.research_questions,
        constraints=tuple(str(item) for item in patch.get("constraints") or fallback.constraints),
        source_requirements=SourceRequirements(
            min_independent_sources=max(1, int(source.get("min_independent_sources") or fallback.source_requirements.min_independent_sources)),
            # A model may strengthen a source requirement, never silently
            # weaken the deterministic safety floor selected from the user's
            # intent (for example a recommendation).
            primary_required=(
                fallback.source_requirements.primary_required
                or bool(source.get("primary_required", False))
            ),
            preferred=tuple(str(item) for item in source.get("preferred") or fallback.source_requirements.preferred),
            forbidden=tuple(str(item) for item in source.get("forbidden") or fallback.source_requirements.forbidden),
        ),
        freshness_requirements=FreshnessRequirements(
            required=bool(freshness.get("required", fallback.freshness_requirements.required)),
            time_horizon=str(freshness.get("time_horizon") or fallback.freshness_requirements.time_horizon),
        ),
        deliverable=DeliverableRequirements(
            format=str(deliverable.get("format") or fallback.deliverable.format),
            depth=str(deliverable.get("depth") or fallback.deliverable.depth),
        ),
        success_criteria=tuple(str(item) for item in patch.get("success_criteria") or fallback.success_criteria),
        assumptions=tuple(str(item) for item in patch.get("assumptions") or fallback.assumptions),
        clarification_needed=bool(patch.get("clarification_needed", fallback.clarification_needed)),
        compiler_source="structured_llm", confidence=max(0.0, min(1.0, float(patch.get("confidence") or 0.75))),
        raw_query=fallback.raw_query,
    )
    return fallback if validate_structured_brief(brief) else brief


async def compile_structured_brief_with_llm(
    query: str, *, agent: Any, budget_manager: Any, conversation_delta: str = "",
    existing_brief: dict[str, Any] | None = None, session_id: str = "",
    max_attempts: int = 2,
) -> StructuredResearchBrief:
    """Compile the Brief behind the Semantic Fidelity Gate.

    A Brief that lost one of the user's asks is rejected and retried; when the
    retry also fails, the query-preserving fallback is delivered instead.
    """
    from app.research.intent.fidelity import evaluate_semantic_fidelity

    fallback = compile_structured_brief(query, conversation_delta=conversation_delta, existing_brief=existing_brief)
    if agent is None:
        return fallback
    contract = UserAskContract(
        contract_id="", raw_query=query, asks=fallback.user_asks,
    )
    prompt = BRIEF_AGENT_PROMPT.format(query=query, conversation_delta=conversation_delta or "无")
    attempts = max(1, int(max_attempts))
    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            gateway = StructuredLLMGateway(budget_manager)
            with gateway.gateway.execution_scope(phase="brief"):
                structured = await gateway.ainvoke(
                    model=agent,
                    schema=StructuredResearchBrief,
                    prompt=prompt,
                    phase="brief",
                    # Planning is control-plane work. A slow provider falls
                    # back instead of consuming the research wall budget.
                    timeout_sec=min(
                        model_timeout_sec("LLM_BRIEF_TIMEOUT_SEC"),
                        max(
                            1.0,
                            float(
                                os.getenv("LLM_PLANNER_TIMEOUT_SEC", "30")
                                or 30
                            ),
                        ),
                    ),
                )
        except Exception as exc:
            emit_semantic_fallback(
                phase="brief",
                component="StructuredLLMGateway",
                fallback="deterministic",
                exc=exc,
                schema=StructuredResearchBrief,
                model=agent,
                started=started,
            )
            return fallback
        candidate = _merge_llm_brief(fallback, structured.to_dict())
        fidelity = evaluate_semantic_fidelity(contract, candidate)
        if fidelity.passed:
            return candidate
        _emit_fidelity_rejection(fidelity, attempt=attempt, attempts=attempts)
        if attempt >= attempts:
            return fallback
    return fallback


def _emit_fidelity_rejection(fidelity: Any, *, attempt: int, attempts: int) -> None:
    try:
        from app.observability import EventType, get_recorder

        recorder = get_recorder()
        if not recorder.is_active:
            return
        recorder.emit(
            EventType.SEMANTIC_FALLBACK,
            phase="brief",
            status="fallback",
            attributes={
                "phase": "brief",
                "component": "SemanticFidelityGate",
                "fallback": "query_preserving" if attempt >= attempts else "brief_retry",
                "error_category": "semantic_fidelity",
                "attempt": attempt,
                **fidelity.to_dict(),
            },
        )
    except Exception:
        return


__all__ = ["BRIEF_AGENT_PROMPT", "compile_structured_brief", "compile_structured_brief_with_llm"]

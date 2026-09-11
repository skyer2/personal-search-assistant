"""Structured Research Brief compiler.

The LLM compiler is the production authority for open-ended semantics. The
deterministic compiler is only a conservative fallback when no model is
available; it never routes from TaskShape.
"""

from __future__ import annotations

import hashlib
import time
import re
from dataclasses import replace
from typing import Any

from app.research.brief.models import (
    DeliverableRequirements,
    FreshnessRequirements,
    SourceRequirements,
    StructuredResearchBrief,
)
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


def _key_questions(query: str, intent: str) -> list[str]:
    if intent == "atomic_fact":
        return [query]
    if intent == "comparison":
        return [f"「{query}」中各主体的核心差异是什么？", "哪些差异有一手来源或高质量独立来源支持？", "在用户关心的场景下应如何选择？"]
    if intent == "trend_forecast":
        return ["当前主要关注点和工程路径是什么？", "未来一段时间可验证的进展有哪些？", "这些判断的主要不确定性和依据是什么？"]
    if intent == "recommendation":
        return ["哪些候选最相关，依据是什么？", "用户决策需要哪些关键事实？", "主要风险和限制是什么？"]
    if intent == "conflict_analysis":
        return ["各方结论分别是什么？", "口径、样本、时间或方法差异是否能解释冲突？", "当前可确认的结论是什么？"]
    if intent == "freshness_update":
        return ["最近发生了哪些重要进展？", "哪些来源能确认这些进展？"]
    if intent == "structured_report":
        return [query, "商业化或应用落地的关键证据是什么？", "主要风险、不确定性和时间路径是什么？"]
    if intent == "explanation":
        return [query, "支持该解释的关键证据是什么？"]
    return [query]


def _freshness(intent: str, query: str) -> FreshnessRequirements:
    if intent in {"trend_forecast", "freshness_update"} or any(token in query for token in ("最新", "当前", "现在", "截至")):
        return FreshnessRequirements(required=True, time_horizon="recent")
    if intent == "atomic_fact":
        return FreshnessRequirements(required=False, time_horizon="point_in_time")
    return FreshnessRequirements(required=False, time_horizon="any")


def _source(intent: str) -> SourceRequirements:
    if intent == "atomic_fact":
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
    objective = " ".join(part for part in (query, conversation_delta) if part).strip()
    intent = _intent(objective)
    subjects = _explicit_subjects(objective)
    if not subjects and intent == "atomic_fact":
        subjects = [objective[:120]]
    questions = _key_questions(objective, intent)[:8]
    success = ["回答直接对齐用户目标。", "结论有一手来源或独立高质量来源支持。", "明确区分事实、推断和未确认内容。"]
    if intent == "comparison":
        success.append("显式主体均被覆盖，不引入未要求对象。")
    if intent == "trend_forecast":
        success.append("区分当前事实与未来预测。")
    version = 1
    assumptions: tuple[str, ...] = ()
    if existing_brief:
        previous = StructuredResearchBrief.from_dict(existing_brief)
        version = previous.version + 1
        assumptions = previous.assumptions
    brief = StructuredResearchBrief(
        brief_id=_stable_brief_id(objective, version), version=version, objective=objective,
        user_intent=intent, explicit_subjects=tuple(subjects), key_questions=tuple(questions),
        constraints=("不要扩大到用户未要求的研究范围。",), source_requirements=_source(intent),
        freshness_requirements=_freshness(intent, objective), deliverable=_deliverable(objective, intent),
        success_criteria=tuple(success), assumptions=assumptions,
        clarification_needed=not bool(objective.strip()), compiler_source="deterministic_fallback",
        confidence=0.55, raw_query=query,
    )
    if validate_structured_brief(brief):
        return replace(brief, clarification_needed=True)
    return brief


def _merge_llm_brief(fallback: StructuredResearchBrief, patch: dict[str, Any]) -> StructuredResearchBrief:
    source = patch.get("source_requirements") if isinstance(patch.get("source_requirements"), dict) else {}
    freshness = patch.get("freshness_requirements") if isinstance(patch.get("freshness_requirements"), dict) else {}
    deliverable = patch.get("deliverable") if isinstance(patch.get("deliverable"), dict) else {}
    brief = StructuredResearchBrief(
        brief_id=fallback.brief_id, version=fallback.version,
        objective=str(patch.get("objective") or fallback.objective),
        user_intent=str(patch.get("user_intent") or fallback.user_intent),
        explicit_subjects=tuple(str(item) for item in patch.get("explicit_subjects") or fallback.explicit_subjects)[:12],
        key_questions=tuple(str(item) for item in patch.get("key_questions") or fallback.key_questions)[:8],
        constraints=tuple(str(item) for item in patch.get("constraints") or fallback.constraints),
        source_requirements=SourceRequirements(
            min_independent_sources=max(1, int(source.get("min_independent_sources") or fallback.source_requirements.min_independent_sources)),
            primary_required=bool(source.get("primary_required", fallback.source_requirements.primary_required)),
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
) -> StructuredResearchBrief:
    fallback = compile_structured_brief(query, conversation_delta=conversation_delta, existing_brief=existing_brief)
    if agent is None:
        return fallback
    prompt = BRIEF_AGENT_PROMPT.format(query=query, conversation_delta=conversation_delta or "无")
    started = time.perf_counter()
    try:
        gateway = StructuredLLMGateway(budget_manager)
        with gateway.gateway.execution_scope(phase="brief"):
            structured = await gateway.ainvoke(
                model=agent,
                schema=StructuredResearchBrief,
                prompt=prompt,
                phase="brief",
                timeout_sec=20,
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
    return _merge_llm_brief(fallback, structured.to_dict())


__all__ = ["BRIEF_AGENT_PROMPT", "compile_structured_brief", "compile_structured_brief_with_llm"]

"""
【Phase 14】工业级混合 Planner

规则候选 + 默认 LLM 结构化理解 → build_plan → Plan 校验。
LLM 失败回退规则；低置信度标记 needs_clarification。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.agent.harness.intent_slots import IntentSlots, build_clarification_question, detect_ambiguity_flags
from app.agent.harness.planner import (
    auto_resolve_clarification,
    understand_task as understand_task_rules,
)
from app.agent.harness.state import ExecutionPlan, TaskIntent

PLANNER_LLM_PROMPT = """你是个人搜索助手的意图理解助手。根据用户问题和规则引擎候选，输出结构化 JSON。

用户问题：
{query}

是否已有上传文件：{has_files}

规则引擎候选（JSON）：
{candidate_json}

要求：
1. 只输出一个 JSON 对象，不要 markdown 代码块
2. deliverable 只能是 text、md、pdf；默认 text（对话回答）
3. 只有用户明确要求生成报告/Markdown/PDF 时才用 md 或 pdf
4. 「列出 N 条 + 来源」仍用 text，在对话中附来源
5. slots 含 topic、item_count、require_citations、output_preference、time_range
6. confidence 0~1；reason 一句话

输出格式：
{{
  "needs_network": true,
  "needs_file_read": false,
  "deliverable": "text",
  "confidence": 0.88,
  "reason": "…",
  "slots": {{ "topic": "…", "require_citations": true, "output_preference": "chat" }}
}}
"""


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    text = (text or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def merge_intent_from_llm(
    rule_intent: TaskIntent,
    llm_patch: dict[str, Any],
    *,
    min_confidence: float = 0.5,
) -> TaskIntent:
    """LLM 高置信度时覆盖规则字段。"""
    confidence = float(llm_patch.get("confidence", 0) or 0)
    if confidence < min_confidence:
        rule_intent.planner_source = "rules"
        rule_intent.intent_confidence = rule_intent.rule_confidence
        return rule_intent

    deliverable = str(llm_patch.get("deliverable", rule_intent.deliverable))
    if deliverable not in {"text", "md", "pdf"}:
        deliverable = rule_intent.deliverable

    slots_data = llm_patch.get("slots") if isinstance(llm_patch.get("slots"), dict) else {}
    merged_slots = IntentSlots.from_dict({**rule_intent.slots.to_dict(), **slots_data})

    merged = TaskIntent(
        raw_query=rule_intent.raw_query,
        summary=f"搜索任务，交付物={deliverable}（planner={rule_intent.planner_source}+llm）",
        needs_network=bool(llm_patch.get("needs_network", rule_intent.needs_network)),
        needs_file_read=bool(llm_patch.get("needs_file_read", rule_intent.needs_file_read)),
        deliverable=deliverable,  # type: ignore[arg-type]
        keywords=rule_intent.keywords,
        planner_source="rules+llm",
        intent_confidence=confidence,
        rule_confidence=rule_intent.rule_confidence,
        planner_reason=str(llm_patch.get("reason", "")),
        slots=merged_slots,
    )
    if not merged.needs_network and not merged.needs_file_read:
        merged.needs_network = True

    merged.ambiguity_flags = detect_ambiguity_flags(
        rule_intent.raw_query,
        deliverable=merged.deliverable,
        slots=merged.slots,
        needs_network=merged.needs_network,
        needs_file_read=merged.needs_file_read,
    )
    merged.needs_clarification = bool(merged.ambiguity_flags) or merged.intent_confidence < 0.72
    merged.clarification_question = (
        build_clarification_question(merged.deliverable, merged.ambiguity_flags, merged.slots)
        if merged.needs_clarification
        else ""
    )
    merged.summary = (
        f"搜索任务，交付物={merged.deliverable}，置信度={merged.intent_confidence}（rules+llm）"
    )
    from app.research.planning.policy import apply_source_policy

    return apply_source_policy(merged)


async def understand_with_llm(
    rule_intent: TaskIntent,
    *,
    model: Any,
    session_id: str = "",
    has_uploaded_files: bool = False,
    min_confidence: float = 0.5,
) -> TaskIntent:
    """LLM 结构化理解；失败回退规则。"""
    if model is None:
        rule_intent.planner_source = "rules"
        rule_intent.intent_confidence = rule_intent.rule_confidence
        return rule_intent

    candidate = rule_intent.to_dict()
    candidate.pop("clarification_question", None)
    prompt = PLANNER_LLM_PROMPT.format(
        query=rule_intent.raw_query,
        has_files="是" if has_uploaded_files else "否",
        candidate_json=json.dumps(candidate, ensure_ascii=False),
    )
    try:
        from app.agent.harness.usage_tracker import tracked_ainvoke

        response = await tracked_ainvoke(
            model,
            prompt,
            session_id=session_id,
            phase="understand",
        )
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        patch = _extract_json(str(content))
        if not patch:
            rule_intent.planner_source = "rules"
            rule_intent.intent_confidence = rule_intent.rule_confidence
            return rule_intent
        return merge_intent_from_llm(rule_intent, patch, min_confidence=min_confidence)
    except Exception as exc:
        print(f"[PlannerLLM] understand failed, fallback rules: {exc}")
        rule_intent.planner_source = "rules"
        rule_intent.intent_confidence = rule_intent.rule_confidence
        return rule_intent


async def understand_intent(
    task_query: str,
    *,
    session_id: str = "",
    has_uploaded_files: bool = False,
    model: Any = None,
    llm_enabled: bool = True,
    llm_min_confidence: float = 0.5,
    clarification_auto_resolve: bool = False,
) -> TaskIntent:
    """【Phase 14】仅理解阶段：规则 + 默认 LLM → TaskIntent。"""
    rule_intent = understand_task_rules(task_query, has_uploaded_files)
    if llm_enabled:
        intent = await understand_with_llm(
            rule_intent,
            model=model,
            session_id=session_id,
            has_uploaded_files=has_uploaded_files,
            min_confidence=llm_min_confidence,
        )
    else:
        intent = rule_intent
        intent.planner_source = "rules"
        intent.intent_confidence = intent.rule_confidence

    if intent.needs_clarification and clarification_auto_resolve:
        intent = auto_resolve_clarification(intent)
    return intent


def build_plan_for_intent(intent: TaskIntent) -> tuple[ExecutionPlan, list[str]]:
    """Plan 阶段：Hybrid policy + 模板/动态 DAG + 校验。"""
    from app.research.planning.compose import compose_execution_plan_sync

    return compose_execution_plan_sync(intent)


async def plan_task(
    task_query: str,
    *,
    session_id: str = "",
    has_uploaded_files: bool = False,
    model: Any = None,
    llm_enabled: bool = True,
    llm_confirm_enabled: bool | None = None,
    llm_confirm_min_confidence: float = 0.5,
    clarification_auto_resolve: bool = False,
) -> tuple[TaskIntent, ExecutionPlan]:
    """规则 + 默认 LLM 理解 → 计划 + 校验。"""
    enabled = llm_confirm_enabled if llm_confirm_enabled is not None else llm_enabled
    intent = await understand_intent(
        task_query,
        session_id=session_id,
        has_uploaded_files=has_uploaded_files,
        model=model,
        llm_enabled=enabled,
        llm_min_confidence=llm_confirm_min_confidence,
        clarification_auto_resolve=clarification_auto_resolve,
    )
    plan, issues = build_plan_for_intent(intent)
    if issues:
        intent.planner_reason = (intent.planner_reason or "") + f" plan_issues={issues}"
    return intent, plan


# 向后兼容别名
confirm_intent_with_llm = understand_with_llm

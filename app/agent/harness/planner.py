"""任务理解与计划生成 — 个人搜索助手（默认 chat 交付，web + file）。"""

from __future__ import annotations

from app.agent.harness.intent_slots import (
    IntentSlots,
    apply_clarification_patch,
    build_clarification_question,
    compute_rule_confidence,
    detect_ambiguity_flags,
    extract_slots,
    infer_output_preference,
    resolve_deliverable_from_slots,
)
from app.agent.harness.state import ExecutionPlan, PlanStep, TaskIntent

NETWORK_KEYWORDS = ("搜索", "网络", "互联网", "公开", "新闻", "趋势", "资料", "检索", "查询", "查一下")
FILE_READ_KEYWORDS = ("上传", "附件", "读取文件", "文档内容")
PDF_KEYWORDS = ("pdf", "PDF")
MD_KEYWORDS = ("markdown", "Markdown", "MD", "md")
REPORT_KEYWORDS = ("报告", "研报", "白皮书")
REPORT_ACTION_KEYWORDS = ("生成", "整理", "撰写", "输出", "导出")


def _looks_like_report_request(query: str) -> bool:
    has_report = any(k in query for k in REPORT_KEYWORDS)
    has_action = any(k in query for k in REPORT_ACTION_KEYWORDS)
    return has_report and has_action


def _infer_deliverable(query: str, slots: IntentSlots) -> str:
    q = query.lower()
    q_raw = query
    if any(k in q for k in PDF_KEYWORDS) or "pdf" in q:
        return "pdf"
    if any(k in q_raw for k in MD_KEYWORDS):
        return "md"
    if _looks_like_report_request(q_raw):
        return "md"
    pref = resolve_deliverable_from_slots(slots, "text")
    if pref != "text":
        return pref
    return "text"


def understand_task(task_query: str, has_uploaded_files: bool = False) -> TaskIntent:
    q_raw = task_query.strip()
    slots = extract_slots(q_raw)
    needs_network = any(k in q_raw for k in NETWORK_KEYWORDS) or not has_uploaded_files
    needs_file_read = has_uploaded_files or any(k in q_raw for k in FILE_READ_KEYWORDS)
    deliverable = _infer_deliverable(q_raw, slots)
    slots.output_preference = infer_output_preference(
        q_raw,
        deliverable=deliverable,
        require_citations=slots.require_citations,
        item_count=slots.item_count,
    )
    if not needs_network and not needs_file_read:
        needs_network = True
    keywords = [k for k in NETWORK_KEYWORDS if k in q_raw]
    ambiguity_flags = detect_ambiguity_flags(
        q_raw,
        deliverable=deliverable,
        slots=slots,
        needs_network=needs_network,
        needs_file_read=needs_file_read,
    )
    rule_confidence = compute_rule_confidence(
        query=q_raw,
        needs_network=needs_network,
        needs_file_read=needs_file_read,
        deliverable=deliverable,
        slots=slots,
        ambiguity_flags=ambiguity_flags,
    )
    needs_clarification = bool(ambiguity_flags) or rule_confidence < 0.72
    clarification_question = (
        build_clarification_question(deliverable, ambiguity_flags, slots)
        if needs_clarification
        else ""
    )
    intent = TaskIntent(
        raw_query=task_query,
        summary=f"搜索任务，交付物={deliverable}，置信度={rule_confidence}",
        needs_network=needs_network,
        needs_file_read=needs_file_read,
        deliverable=deliverable,  # type: ignore[arg-type]
        keywords=keywords,
        planner_source="rules",
        intent_confidence=rule_confidence,
        rule_confidence=rule_confidence,
        slots=slots,
        ambiguity_flags=ambiguity_flags,
        needs_clarification=needs_clarification,
        clarification_question=clarification_question,
    )
    from app.research.planning.policy import apply_source_policy
    from app.agent.harness.research_brief import attach_brief

    intent = apply_source_policy(intent)
    return attach_brief(intent)


def build_plan(intent: TaskIntent) -> ExecutionPlan:
    from app.research.planning.policy import tools_for_sources

    steps: list[PlanStep] = []
    if intent.needs_file_read:
        steps.append(
            PlanStep(
                step_type="file_read",
                description="读取用户上传的附件内容",
                allowed_tools=tools_for_sources(["file"]),
            )
        )
    if intent.needs_network:
        steps.append(
            PlanStep(
                step_type="network_search",
                description="检索互联网公开资料",
                subagent="网络搜索助手",
                allowed_tools=tools_for_sources(["web"]),
            )
        )
    if intent.deliverable == "md":
        steps.append(
            PlanStep(
                step_type="generate_markdown",
                description="汇总信息并生成 Markdown 报告",
            )
        )
    elif intent.deliverable == "pdf":
        steps.extend(
            [
                PlanStep(
                    step_type="generate_markdown",
                    description="汇总信息并生成 Markdown 报告",
                ),
                PlanStep(
                    step_type="convert_pdf",
                    description="将 Markdown 转换为 PDF",
                ),
            ]
        )
    else:
        steps.append(
            PlanStep(
                step_type="summarize",
                description="汇总信息并输出对话式回答",
            )
        )
    summary = " → ".join(step.description for step in steps)
    return ExecutionPlan(steps=steps, summary=summary)


def validate_plan_against_intent(intent: TaskIntent, plan: ExecutionPlan) -> tuple[bool, list[str]]:
    issues: list[str] = []
    step_types = [s.step_type for s in plan.steps]
    if intent.needs_network and "network_search" not in step_types:
        issues.append("missing_network_search")
    if intent.needs_file_read and "file_read" not in step_types:
        issues.append("missing_file_read")
    if intent.deliverable == "md" and "generate_markdown" not in step_types:
        issues.append("missing_generate_markdown")
    if intent.deliverable == "pdf":
        if "generate_markdown" not in step_types:
            issues.append("missing_generate_markdown")
        if "convert_pdf" not in step_types:
            issues.append("missing_convert_pdf")
    if intent.deliverable == "text" and "summarize" not in step_types:
        issues.append("missing_summarize")
    if not plan.steps:
        issues.append("empty_plan")
    return len(issues) == 0, issues


def finalize_plan(plan: ExecutionPlan) -> ExecutionPlan:
    from app.agent.harness.orchestration import mark_parallel_retrieval_groups
    from app.research.runtime.scheduler import annotate_plan_tasks

    plan = annotate_plan_tasks(mark_parallel_retrieval_groups(plan))
    for step in plan.steps:
        step.metadata.setdefault("status", "pending")
    return plan


def detect_multi_intent(intent: TaskIntent) -> bool:
    count = sum([intent.needs_network, intent.needs_file_read])
    return count >= 2


def should_request_plan_review(intent: TaskIntent, *, min_confidence: float = 0.75) -> bool:
    if detect_multi_intent(intent):
        return True
    if intent.intent_confidence < min_confidence:
        return True
    if intent.ambiguity_flags and not intent.clarification_resolved:
        return True
    if "deliverable_ambiguous" in intent.ambiguity_flags:
        return True
    return False


def apply_intent_clarification(intent: TaskIntent, edited_action: dict) -> TaskIntent:
    patched = apply_clarification_patch(intent.to_dict(), edited_action)
    updated = TaskIntent.from_dict(patched)
    updated.raw_query = intent.raw_query
    if not updated.summary:
        updated.summary = f"搜索任务，交付物={updated.deliverable}（已澄清）"
    updated.intent_confidence = max(updated.intent_confidence, updated.rule_confidence)
    return updated


def auto_resolve_clarification(intent: TaskIntent) -> TaskIntent:
    if not intent.needs_clarification:
        return intent
    resolved = TaskIntent.from_dict(intent.to_dict())
    if "deliverable_ambiguous" in resolved.ambiguity_flags:
        resolved.deliverable = "text"
        resolved.slots.output_preference = "chat"
    resolved.needs_clarification = False
    resolved.clarification_resolved = True
    resolved.clarification_question = ""
    resolved.planner_reason = (resolved.planner_reason or "") + " [auto_resolve]"
    resolved.summary = f"搜索任务，交付物={resolved.deliverable}（auto_resolve）"
    return resolved


def apply_plan_edits(plan: ExecutionPlan, steps_payload: list[dict]) -> ExecutionPlan:
    new_steps: list[PlanStep] = []
    for item in steps_payload:
        if not isinstance(item, dict):
            continue
        step_type = str(item.get("step_type", "")).strip()
        if not step_type:
            continue
        new_steps.append(
            PlanStep(
                step_type=step_type,
                description=str(item.get("description", step_type)),
                subagent=item.get("subagent"),
                metadata={"hitl_edited": True},
            )
        )
    if not new_steps:
        return plan
    summary = " → ".join(step.description for step in new_steps)
    return finalize_plan(ExecutionPlan(steps=new_steps, summary=summary))


def dynamic_replan(
    plan: ExecutionPlan,
    insert_after_index: int,
    reason: str,
    extra_steps: list[PlanStep] | None = None,
) -> ExecutionPlan:
    from app.research.planning.policy import tools_for_sources

    steps = list(plan.steps)
    inserts: list[PlanStep] = list(extra_steps or [])
    if not inserts:
        if reason in {"search_empty", "wrong_subagent", "citation_coverage_low"}:
            inserts.append(
                PlanStep(
                    step_type="network_search",
                    description="【动态重规划】补充公开资料交叉验证",
                    subagent="网络搜索助手",
                    allowed_tools=tools_for_sources(["web"]),
                    metadata={"replan_reason": reason},
                )
            )
        elif reason == "user_replan":
            inserts.append(
                PlanStep(
                    step_type="summarize",
                    description="【动态重规划】按用户编辑重新汇总",
                    metadata={"replan_reason": reason},
                )
            )
    pos = min(max(insert_after_index + 1, 0), len(steps))
    for offset, step in enumerate(inserts):
        steps.insert(pos + offset, step)
    summary = " → ".join(step.description for step in steps)
    return finalize_plan(ExecutionPlan(steps=steps, summary=summary))


def plan_to_editable_dict(plan: ExecutionPlan) -> list[dict]:
    return [
        {
            "step_type": step.step_type,
            "description": step.description,
            "subagent": step.subagent,
        }
        for step in plan.steps
    ]

"""受约束 Lead Planner：只输出研究目标 DAG，没有 runtime 权力。"""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep, TaskIntent
from app.research.planning.policy import (
    SourcePolicy,
    extract_compare_entities,
    intent_allowed_sources,
    tools_for_sources,
)

LEAD_PLANNER_PROMPT = """你是 Lead Research Planner，不是运行时。
你只输出研究目标 DAG，禁止调用工具、禁止调度工人、禁止决定何时停止、禁止抬高预算。

硬约束（必须遵守，高于你的判断）：
{policy_json}

用户问题：
{query}

完整 Research Brief（含实体/维度/深度/新鲜度/成功标准/effort 软配额摘要）：
{brief}

规则：
1. 按「要研究什么问题」拆任务，优先覆盖 Brief.entities 与 Brief.dimensions；不要按数据源拆（禁止 task=只搜网页）。
2. 每个 task 必须标注 coverage_keys（对应 Brief.dimensions 或 supporting_context）。
3. 覆盖 Brief.dimensions 的任务 priority=P0、required=true；仅作背景/历史辅助的任务 priority=P1、required=false。
4. 禁止把 Brief 里的核心维度标成 optional。
5. 每个 task 的 allowed_sources 只能从 policy.allowed_sources 选取。
6. 禁止把 policy.forbidden_sources 写进任何 task。
7. 比较类问题按实体或评价维度拆；横向比较任务 depends_on 各实体任务。
8. 不要生成写报告 / PDF / summarize 任务，系统会追加。
9. task 数量 2~{max_tasks}（已是 Hard Ceiling clamp 后的上限）。
10. 每个 P0 维度默认 evidence_target.independent_sources=3、max_sources=6，不要为堆来源而拆额外任务。
11. 只输出一个 JSON 对象。

格式：
{{
  "research_brief": "一句话研究说明书",
  "tasks": [
    {{
      "task_id": "t_capability",
      "objective": "…",
      "depends_on": [],
      "allowed_sources": ["web"],
      "coverage_keys": ["技术能力现状与边界"],
      "priority": "P0",
      "required": true,
      "evidence_target": {{"independent_sources": 3, "max_sources": 6, "prefer_primary": true}},
      "effort": "medium"
    }}
  ]
}}
"""


def _extract_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if raw.startswith("{") and raw.endswith("}"):
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _sanitize_sources(requested: list[Any], policy: SourcePolicy) -> list[str]:
    allowed = set(policy.allowed_sources)
    cleaned: list[str] = []
    for item in requested or []:
        name = str(item).strip().lower()
        alias = {"network": "web", "internet": "web", "sql": "db", "database": "db", "rag": "kb"}.get(name, name)
        if alias in allowed and alias not in cleaned:
            cleaned.append(alias)
    return cleaned or [s for s in intent_like_default(policy)]


def intent_like_default(policy: SourcePolicy) -> list[str]:
    preferred = [s for s in ("web", "file") if s in policy.allowed_sources]
    return preferred[:3] or list(policy.allowed_sources)


def research_step_from_task(
    *,
    task_id: str,
    objective: str,
    depends_on: list[str],
    sources: list[str],
    coverage_keys: list[str] | None = None,
    required: bool | None = None,
    priority: str | int | None = None,
    evidence_target: dict | None = None,
    extra_metadata: dict | None = None,
) -> PlanStep:
    meta: dict = {"allowed_sources": list(sources), "kind": "research_task"}
    if coverage_keys:
        meta["coverage_keys"] = [str(x) for x in coverage_keys if str(x).strip()]
    if required is not None:
        meta["required"] = bool(required)
        meta["optional"] = not bool(required)
    if priority is not None:
        meta["priority"] = priority
    if evidence_target:
        meta["evidence_target"] = dict(evidence_target)
    if extra_metadata:
        meta.update(extra_metadata)
    return PlanStep(
        step_type="research",
        description=objective,
        subagent="研究工人",
        task_id=task_id,
        depends_on=list(depends_on),
        allowed_tools=tools_for_sources(sources),
        objective=objective,
        metadata=meta,
    )


def append_synthesis(intent: TaskIntent, steps: list[PlanStep]) -> list[PlanStep]:
    research_ids = [s.task_id for s in steps if s.task_id]
    if intent.deliverable == "md":
        steps.append(
            PlanStep(
                step_type="generate_markdown",
                description="只消费已有证据，写成 Markdown 报告",
                task_id="t_synthesize_md",
                depends_on=list(research_ids),
            )
        )
    elif intent.deliverable == "pdf":
        steps.append(
            PlanStep(
                step_type="generate_markdown",
                description="只消费已有证据，写成 Markdown 报告",
                task_id="t_synthesize_md",
                depends_on=list(research_ids),
            )
        )
        steps.append(
            PlanStep(
                step_type="convert_pdf",
                description="将 Markdown 转换为 PDF",
                task_id="t_synthesize_pdf",
                depends_on=["t_synthesize_md"],
            )
        )
    else:
        steps.append(
            PlanStep(
                step_type="summarize",
                description="只消费已有证据，输出最终回答",
                task_id="t_synthesize_text",
                depends_on=list(research_ids),
            )
        )
    return steps


def heuristic_dynamic_plan(intent: TaskIntent, policy: SourcePolicy) -> ExecutionPlan:
    """无 LLM 时的确定性拆解：按 Brief 实体/维度作为 P0 研究目标。"""
    from app.agent.harness.research_brief import brief_of
    from app.research.planning.priority import stamp_semantic_priority

    sources = intent_allowed_sources(intent)
    sources = [s for s in sources if policy.allows(s)] or list(policy.allowed_sources)
    brief = brief_of(intent, query=intent.raw_query)
    entities = [e for e in (brief.entities or []) if e][:5]
    if len(entities) < 2:
        entities = extract_compare_entities(intent.raw_query)
    dimensions = [d for d in (brief.dimensions or []) if d and d != "关键事实"]
    dim_hint = "、".join(dimensions[:4]) if dimensions else "与用户问题相关的事实、进展与证据"
    primary_hint = "；优先官方/一手来源" if brief.prefer_primary else ""
    evidence_target = {
        "independent_sources": 3,
        "max_sources": 6,
        "prefer_primary": bool(brief.prefer_primary),
    }
    steps: list[PlanStep] = []
    if len(entities) >= 2:
        entity_ids: list[str] = []
        for index, name in enumerate(entities[:5], start=1):
            tid = f"t_entity_{index}"
            entity_ids.append(tid)
            steps.append(
                research_step_from_task(
                    task_id=tid,
                    objective=f"{name}：{dim_hint}{primary_hint}",
                    depends_on=[],
                    sources=sources,
                    coverage_keys=list(dimensions[:4]) or [name],
                    required=True,
                    priority="P0",
                    evidence_target=evidence_target,
                )
            )
        steps.append(
            research_step_from_task(
                task_id="t_compare",
                objective=f"基于各实体证据做横向比较（{dim_hint}），不引入新的未授权来源",
                depends_on=list(entity_ids),
                sources=sources,
                coverage_keys=["横向比较"] if "横向比较" in dimensions else list(dimensions[:2]),
                required=True,
                priority="P0",
                evidence_target=evidence_target,
            )
        )
        brief_text = brief.objective or f"比较 {' / '.join(entities[:5])}"
    elif dimensions:
        for index, dim in enumerate(dimensions[:6], start=1):
            steps.append(
                research_step_from_task(
                    task_id=f"t_dim_{index}",
                    objective=f"{dim}{primary_hint}",
                    depends_on=[],
                    sources=sources,
                    coverage_keys=[dim],
                    required=True,
                    priority="P0",
                    evidence_target=evidence_target,
                )
            )
        brief_text = brief.objective or intent.raw_query
    else:
        steps.append(
            research_step_from_task(
                task_id="t_core",
                objective=(brief.objective or intent.raw_query) + primary_hint,
                depends_on=[],
                sources=sources,
                coverage_keys=[],
                required=True,
                priority="P0",
                evidence_target=evidence_target,
            )
        )
        brief_text = brief.objective or intent.summary or intent.raw_query
    append_synthesis(intent, steps)
    plan = ExecutionPlan(
        steps=steps,
        summary=" → ".join(s.description for s in steps),
        planning_mode="dynamic",
        research_brief=brief_text,
    )
    return stamp_semantic_priority(plan, intent=intent, dimensions=dimensions)


def plan_from_lead_payload(
    payload: dict[str, Any],
    intent: TaskIntent,
    policy: SourcePolicy,
    *,
    max_tasks: int = 6,
) -> ExecutionPlan | None:
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return None
    seen: set[str] = set()
    staged: list[dict[str, Any]] = []
    for index, raw in enumerate(tasks[:max_tasks], start=1):
        if not isinstance(raw, dict):
            continue
        objective = str(raw.get("objective") or raw.get("description") or "").strip()
        if not objective:
            continue
        tid = str(raw.get("task_id") or f"t_dyn_{index}").strip() or f"t_dyn_{index}"
        if tid in seen:
            tid = f"{tid}_{index}"
        seen.add(tid)
        sources = _sanitize_sources(list(raw.get("allowed_sources") or []), policy)
        staged.append(
            {
                "tid": tid,
                "objective": objective,
                "depends": [str(x) for x in (raw.get("depends_on") or [])],
                "sources": sources,
                "effort": str(raw.get("effort") or "").strip().lower(),
                "coverage_keys": [str(x) for x in (raw.get("coverage_keys") or []) if str(x).strip()],
                "required": raw.get("required"),
                "priority": raw.get("priority"),
                "evidence_target": raw.get("evidence_target")
                if isinstance(raw.get("evidence_target"), dict)
                else None,
            }
        )
    steps: list[PlanStep] = []
    for item in staged:
        depends = [d for d in item["depends"] if d in seen and d != item["tid"]]
        required = item["required"]
        if required is not None:
            required = bool(required)
        step = research_step_from_task(
            task_id=item["tid"],
            objective=item["objective"],
            depends_on=depends,
            sources=item["sources"],
            coverage_keys=item["coverage_keys"] or None,
            required=required,
            priority=item["priority"],
            evidence_target=item["evidence_target"],
        )
        if item["effort"] in {"low", "medium", "high"}:
            meta = dict(getattr(step, "metadata", None) or {})
            meta["effort"] = item["effort"]
            step.metadata = meta
        steps.append(step)
    if not steps:
        return None
    append_synthesis(intent, steps)
    from app.research.planning.priority import stamp_semantic_priority

    plan = ExecutionPlan(
        steps=steps,
        summary=" → ".join(s.description for s in steps),
        planning_mode="dynamic",
        research_brief=str(payload.get("research_brief") or intent.summary or ""),
    )
    return stamp_semantic_priority(plan, intent=intent)


async def lead_plan_with_llm(
    intent: TaskIntent,
    policy: SourcePolicy,
    *,
    model: Any,
    session_id: str = "",
    max_tasks: int = 6,
    effort: Any | None = None,
) -> ExecutionPlan | None:
    if model is None:
        return None
    from app.research.planning.effort import brief_payload_for_lead_planner

    prompt = LEAD_PLANNER_PROMPT.format(
        policy_json=json.dumps(policy.to_dict(), ensure_ascii=False),
        query=intent.raw_query,
        brief=json.dumps(
            brief_payload_for_lead_planner(intent, effort=effort),
            ensure_ascii=False,
        ),
        max_tasks=max_tasks,
    )
    try:
        from app.agent.harness.usage_tracker import tracked_ainvoke

        response = await tracked_ainvoke(
            model,
            prompt,
            session_id=session_id,
            phase="plan",
        )
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        payload = _extract_json(str(content))
        if not payload:
            return None
        return plan_from_lead_payload(payload, intent, policy, max_tasks=max_tasks)
    except Exception as exc:
        print(f"[LeadPlanner] llm failed, fallback heuristic: {exc}")
        return None

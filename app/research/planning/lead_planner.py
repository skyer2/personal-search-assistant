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
2. 每个 task 的 allowed_sources 只能从 policy.allowed_sources 选取。
3. 禁止把 policy.forbidden_sources 写进任何 task。
4. 比较类问题按实体或评价维度拆；横向比较任务 depends_on 各实体任务。
5. 不要生成写报告 / PDF / summarize 任务，系统会追加。
6. task 数量 2~{max_tasks}（已是 Hard Ceiling clamp 后的上限）。
7. allowed_sources 只能是 web 和/或 file。
8. 可在 task 上标注 effort: low|medium|high 作为提示；禁止输出 exact_search_calls 等假精确次数。
9. 只输出一个 JSON 对象。

格式：
{{
  "research_brief": "一句话研究说明书",
  "tasks": [
    {{
      "task_id": "t_tesla",
      "objective": "Tesla 2026 商业化进度：产能、客户、收入线索",
      "depends_on": [],
      "allowed_sources": ["web"],
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
) -> PlanStep:
    return PlanStep(
        step_type="research",
        description=objective,
        subagent="研究工人",
        task_id=task_id,
        depends_on=list(depends_on),
        allowed_tools=tools_for_sources(sources),
        objective=objective,
        metadata={"allowed_sources": list(sources), "kind": "research_task"},
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
    """无 LLM 时的确定性拆解：按 Brief 实体/维度或比较实体作为研究目标。"""
    from app.agent.harness.research_brief import brief_of

    sources = intent_allowed_sources(intent)
    sources = [s for s in sources if policy.allows(s)] or list(policy.allowed_sources)
    brief = brief_of(intent, query=intent.raw_query)
    entities = [e for e in (brief.entities or []) if e][:5]
    if len(entities) < 2:
        entities = extract_compare_entities(intent.raw_query)
    dimensions = [d for d in (brief.dimensions or []) if d and d != "关键事实"]
    dim_hint = "、".join(dimensions[:4]) if dimensions else "与用户问题相关的事实、进展与证据"
    primary_hint = "；优先官方/一手来源" if brief.prefer_primary else ""
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
                )
            )
        steps.append(
            research_step_from_task(
                task_id="t_compare",
                objective=f"基于各实体证据做横向比较（{dim_hint}），不引入新的未授权来源",
                depends_on=list(entity_ids),
                sources=sources,
            )
        )
        brief_text = brief.objective or f"比较 {' / '.join(entities[:5])}"
    else:
        steps.append(
            research_step_from_task(
                task_id="t_core",
                objective=(brief.objective or intent.raw_query) + primary_hint,
                depends_on=[],
                sources=sources,
            )
        )
        brief_text = brief.objective or intent.summary or intent.raw_query
    append_synthesis(intent, steps)
    return ExecutionPlan(
        steps=steps,
        summary=" → ".join(s.description for s in steps),
        planning_mode="dynamic",
        research_brief=brief_text,
    )


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
    staged: list[tuple[str, str, list[str], list[str], str]] = []
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
        effort_hint = str(raw.get("effort") or "").strip().lower()
        staged.append(
            (tid, objective, [str(x) for x in (raw.get("depends_on") or [])], sources, effort_hint)
        )
    steps: list[PlanStep] = []
    for tid, objective, raw_deps, sources, effort_hint in staged:
        depends = [d for d in raw_deps if d in seen and d != tid]
        step = research_step_from_task(
            task_id=tid,
            objective=objective,
            depends_on=depends,
            sources=sources,
        )
        if effort_hint in {"low", "medium", "high"}:
            meta = dict(getattr(step, "metadata", None) or {})
            meta["effort"] = effort_hint
            step.metadata = meta
        steps.append(step)
    if not steps:
        return None
    append_synthesis(intent, steps)
    return ExecutionPlan(
        steps=steps,
        summary=" → ".join(s.description for s in steps),
        planning_mode="dynamic",
        research_brief=str(payload.get("research_brief") or intent.summary or ""),
    )


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

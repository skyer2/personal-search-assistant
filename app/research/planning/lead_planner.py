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
1. 按「要研究什么问题」拆任务，优先覆盖 Brief.subjects（canonical subject_id）与 Brief.dimensions；不要按数据源拆（禁止 task=只搜网页）。
2. 每个 task 必须标注 coverage_keys（对应 Brief.dimensions 或 supporting_context）。
3. 覆盖 Brief.dimensions 的任务 priority=P0、required=true；仅作背景/历史辅助的任务 priority=P1、required=false。
4. 禁止把 Brief 里的核心维度标成 optional。
5. 每个 task 的 allowed_sources 只能从 policy.allowed_sources 选取。
6. 禁止把 policy.forbidden_sources 写进任何 task。
7. 比较类问题按实体或评价维度拆；横向比较任务 depends_on 各实体任务。
8. 不要生成写报告 / PDF / summarize 任务，系统会追加。
9. task 数量 2~{max_tasks}（已是 Hard Ceiling clamp 后的上限）。
10. 每个 P0 维度默认 evidence_target.independent_sources=3、max_sources=6，不要为堆来源而拆额外任务。
11. 任务粒度硬约束：单个 task 最多 2 个独立实体、4 个核心维度、8 个信息单元；超过必须拆成多个 task。
12. 开放式市场/赛道问题先拆 Landscape Discovery（候选发现），不要一开始就对全部候选做完整深度调研；后续 Progress/Replan 再决定深挖对象。
13. 只输出一个 JSON 对象。

格式：
{{
  "research_brief": "一句话研究说明书",
  "tasks": [
    {{
      "task_id": "t_capability",
      "task_kind": "deep_dive",
      "subject_id": "deepseek",
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
    task_kind: str = "deep_dive",
    subject_id: str = "",
) -> PlanStep:
    meta: dict = {
        "allowed_sources": list(sources),
        "kind": "research_task",
        "task_kind": task_kind,
    }
    if subject_id:
        meta["subject_id"] = subject_id
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


def _dedupe_research_steps(
    steps: list[PlanStep], brief: Any
) -> list[PlanStep]:
    """Fan out by canonical subject, not lexical aliases."""
    subjects = [
        {
            "id": str(getattr(subject, "subject_id", "") or subject.canonical),
            "canonical": str(subject.canonical),
            "aliases": [str(x) for x in (subject.aliases or [])],
        }
        for subject in (getattr(brief, "subjects", None) or [])
    ]
    brief_kind = str(getattr(brief, "task_kind", "") or "")
    output: list[PlanStep] = []
    owners: dict[tuple[str, str, tuple[str, ...], tuple[int, int] | None], str] = {}
    replacements: dict[str, str] = {}

    for step in steps:
        metadata = dict(step.metadata or {})
        if str(metadata.get("task_kind") or "deep_dive") == "comparison":
            if step.task_id:
                replacements[step.task_id] = ""
            continue
        if brief_kind == "landscape_discovery":
            metadata["task_kind"] = "discovery"
        else:
            metadata.setdefault("task_kind", "deep_dive")

        objective = f"{step.objective or ''} {step.description or ''}"
        subject_id = str(metadata.get("subject_id") or "")
        if not subject_id:
            matched = next(
                (
                    item["id"]
                    for item in subjects
                    if item["canonical"].lower() in objective.lower()
                    or any(alias.lower() in objective.lower() for alias in item["aliases"])
                ),
                None,
            )
            subject_id = matched or (subjects[0]["id"] if len(subjects) == 1 else "general")
        metadata["subject_id"] = subject_id
        step.metadata = metadata

        raw_range = metadata.get("target_item_range")
        item_range = (
            (int(raw_range[0]), int(raw_range[1]))
            if isinstance(raw_range, list)
            and len(raw_range) == 2
            and all(isinstance(x, int) for x in raw_range)
            else None
        )
        coverage = tuple(str(x) for x in (metadata.get("coverage_keys") or []))
        key = (subject_id, str(metadata["task_kind"]), coverage, item_range)
        owner = owners.get(key)
        if owner is not None:
            if step.task_id:
                replacements[step.task_id] = owner
            continue
        if step.task_id:
            owners[key] = step.task_id
        output.append(step)

    for step in output:
        rewritten = []
        for dependency in step.depends_on or []:
            replacement = replacements.get(dependency, dependency)
            if replacement:
                rewritten.append(replacement)
        step.depends_on = rewritten
    return output


def heuristic_dynamic_plan(intent: TaskIntent, policy: SourcePolicy) -> ExecutionPlan:
    """无 LLM 时的确定性拆解：按 Brief 实体/维度作为 P0 研究目标。"""
    from app.agent.harness.research_brief import brief_of
    from app.research.planning.granularity import normalize_plan_granularity
    from app.research.planning.priority import stamp_semantic_priority

    sources = intent_allowed_sources(intent)
    sources = [s for s in sources if policy.allows(s)] or list(policy.allowed_sources)
    brief = brief_of(intent, query=intent.raw_query)
    subjects = list(brief.subjects or [])
    entities = [subject.canonical for subject in subjects][:5]
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
    if brief.task_kind == "landscape_discovery":
        subject = subjects[0] if subjects else None
        discovery_dimensions = ["候选池"]
        steps.append(
            research_step_from_task(
                task_id="t_landscape",
                objective=(
                    f"Landscape Discovery：发现 6-8 家高潜力候选，"
                    f"覆盖 {'、'.join(discovery_dimensions)}{primary_hint}"
                ),
                depends_on=[],
                sources=sources,
                coverage_keys=discovery_dimensions,
                required=True,
                priority="P0",
                evidence_target=evidence_target,
                task_kind="discovery",
                subject_id=str(getattr(subject, "subject_id", "") or "general"),
                extra_metadata={
                    "target_items": 8,
                    "produces_artifact": "candidate_set",
                },
            )
        )
        brief_text = brief.objective or intent.raw_query
    elif len(entities) >= 2:
        entity_ids: list[str] = []
        for index, (name, subject) in enumerate(
            zip(entities[:5], subjects[:5]), start=1
        ):
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
                    task_kind="deep_dive",
                    subject_id=str(getattr(subject, "subject_id", "") or name),
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
                    task_kind="deep_dive",
                    subject_id=str(getattr(subjects[0], "subject_id", "") or "general")
                    if subjects else "general",
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
                    task_kind="deep_dive",
                    subject_id=str(getattr(subjects[0], "subject_id", "") or "general")
                    if subjects else "general",
                )
        )
        brief_text = brief.objective or intent.summary or intent.raw_query
    steps = normalize_plan_granularity(steps, brief, max_research_tasks=6)
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
                "task_kind": str(raw.get("task_kind") or "").strip().lower(),
                "subject_id": str(raw.get("subject_id") or "").strip(),
                "target_items": raw.get("target_items"),
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
            task_kind=(
                "discovery"
                if item["task_kind"] in {"discovery", "landscape_discovery"}
                else "deep_dive"
            ),
            subject_id=item["subject_id"],
            extra_metadata=(
                {"target_items": int(item["target_items"])}
                if item["target_items"] is not None
                else None
            ),
        )
        if item["effort"] in {"low", "medium", "high"}:
            meta = dict(getattr(step, "metadata", None) or {})
            meta["effort"] = item["effort"]
            step.metadata = meta
        steps.append(step)
    if not steps:
        return None
    from app.agent.harness.research_brief import brief_of
    from app.research.planning.granularity import normalize_plan_granularity

    brief = brief_of(intent, query=intent.raw_query)
    steps = normalize_plan_granularity(
        steps,
        brief,
        max_research_tasks=max_tasks,
    )
    steps = _dedupe_research_steps(steps, brief)
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

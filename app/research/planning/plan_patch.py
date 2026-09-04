"""PlanPatch：Evaluator 发现缺口后，由代码决定是否接受补丁。"""

from __future__ import annotations

from typing import Any

from app.agent.harness.planner import finalize_plan
from app.agent.harness.state import ExecutionPlan, PlanStep, TaskIntent
from app.research.planning.lead_planner import research_step_from_task
from app.research.planning.policy import SourcePolicy, parse_source_policy
from app.research.planning.validator import RESEARCH_TYPES, validate_hybrid_plan


def apply_plan_patch(
    plan: ExecutionPlan,
    patch: dict[str, Any],
    intent: TaskIntent,
    *,
    policy: SourcePolicy | None = None,
    max_new_tasks: int = 2,
    max_plan_steps: int = 12,
) -> tuple[ExecutionPlan, list[str]]:
    """校验并追加 research tasks。拒绝越权来源 / 环 / 超预算。"""
    policy = policy or parse_source_policy(intent.raw_query)
    additions = list(patch.get("add_tasks") or [])[: max(0, max_new_tasks)]
    if not additions:
        return plan, ["empty_patch"]
    existing = {step.task_id for step in plan.steps if step.task_id}
    new_steps = list(plan.steps)
    inserted = 0
    synth_index = next(
        (i for i, s in enumerate(new_steps) if s.step_type in {"generate_markdown", "summarize"}),
        len(new_steps),
    )
    for raw in additions:
        if not isinstance(raw, dict):
            continue
        objective = str(raw.get("objective") or "").strip()
        if not objective:
            continue
        tid = str(raw.get("task_id") or f"t_patch_{inserted + 1}")
        if tid in existing:
            tid = f"{tid}_v{plan.plan_version + 1}"
        sources = [
            s
            for s in (raw.get("allowed_sources") or [])
            if str(s) in policy.allowed_sources
        ] or [s for s in ("web", "file") if s in policy.allowed_sources]
        depends = [str(x) for x in (raw.get("depends_on") or []) if str(x) in existing]
        step = research_step_from_task(
            task_id=tid,
            objective=objective,
            depends_on=depends,
            sources=sources,
        )
        raw_meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        if raw_meta:
            merged = dict(getattr(step, "metadata", None) or {})
            merged.update(raw_meta)
            step.metadata = merged
        new_steps.insert(synth_index + inserted, step)
        existing.add(tid)
        inserted += 1
        synth_index += 1
    if inserted == 0:
        return plan, ["empty_patch"]
    for step in new_steps:
        if step.step_type in {"generate_markdown", "summarize"}:
            step.depends_on = [
                s.task_id for s in new_steps if s.task_id and s.step_type in RESEARCH_TYPES
            ]
    candidate = ExecutionPlan(
        steps=new_steps,
        summary=" → ".join(s.description for s in new_steps),
        plan_version=int(plan.plan_version or 1) + 1,
        planning_mode=plan.planning_mode or "dynamic",
        research_brief=plan.research_brief,
    )
    candidate = finalize_plan(candidate)
    issues = validate_hybrid_plan(
        intent,
        candidate,
        policy=policy,
        max_plan_steps=max_plan_steps,
    )
    if issues:
        return plan, issues
    return candidate, []


def build_progress_patch(
    plan: ExecutionPlan,
    intent: TaskIntent,
    *,
    assessment: dict[str, Any] | None = None,
    worker_results: list[Any] | None = None,
    max_new_tasks: int = 2,
) -> dict[str, Any]:
    """把 ProgressAssessment 收成受约束的 PlanPatch proposal（仍须 apply_plan_patch）。

    每个新任务绑定 resolves_gap_ids；target_gap_ids 仅为实际被选中的 gap。
    expected_disagreement 不进入补丁。
    """
    from app.research.planning.progress import ProgressAssessment

    parsed = ProgressAssessment.from_dict(assessment or {})
    policy = parse_source_policy(intent.raw_query)
    default_sources = [s for s in ("web", "file") if s in policy.allowed_sources]
    if not default_sources:
        return {"add_tasks": [], "reason": "no_allowed_source", "target_gap_ids": []}

    # gap_id → description 映射（仅 actionable）
    gap_by_desc: dict[str, str] = {}
    for item in parsed.gaps or []:
        if not isinstance(item, dict):
            continue
        if item.get("actionable") is False or str(item.get("type") or "") == "expected_disagreement":
            continue
        gid = str(item.get("gap_id") or "")
        desc = str(item.get("description") or "")
        if gid and desc:
            gap_by_desc[desc] = gid

    proposals: list[dict[str, Any]] = []

    def _resolve_gap_id(signal: str, reason: str) -> str:
        text = str(signal or "")
        if text in gap_by_desc:
            return gap_by_desc[text]
        # 模糊：description 包含 signal
        for desc, gid in gap_by_desc.items():
            if text and (text in desc or desc in text):
                return gid
        from app.observability.semantic import stable_gap_id

        return stable_gap_id(reason, text or reason)

    def _append(objective: str, reason: str, signal: str) -> None:
        if len(proposals) >= max(0, max_new_tasks):
            return
        text = str(objective or "").strip()
        if not text:
            return
        gap_id = _resolve_gap_id(signal, reason)
        # 避免同一 gap 被多个 task 重复绑定
        if any(gap_id in (p.get("metadata") or {}).get("resolves_gap_ids", []) for p in proposals):
            return
        proposals.append(
            {
                "task_id": f"t_gap_{len(proposals) + 1}",
                "objective": text[:200],
                "depends_on": [],
                "allowed_sources": list(default_sources),
                "reason": reason,
                "metadata": {
                    "resolves_gap_ids": [gap_id],
                    "patch_reason": reason,
                },
            }
        )

    # 只对 actionable 信号建任务；conflicts 用 unresolved 列表
    for item in parsed.coverage_gaps:
        _append(_objective_from_gap(item, plan), "coverage", item)
    for item in parsed.unresolved_conflicts:
        _append(f"交叉验证冲突：{item}", "unresolved_conflict", item)
    for item in parsed.stale_evidence:
        _append(f"补充最新年份证据：{item}", "stale", item)
    for item in parsed.missing_dimensions:
        _append(f"补充维度：{item}", "missing_dimension", item)

    if proposals:
        target_gap_ids = []
        for prop in proposals:
            for gid in (prop.get("metadata") or {}).get("resolves_gap_ids") or []:
                if gid and gid not in target_gap_ids:
                    target_gap_ids.append(str(gid))
        return {
            "reason": parsed.reason or "semantic_gap",
            "add_tasks": proposals,
            "target_gap_ids": target_gap_ids,
            "triggered_by": parsed.progress_id or "",
            "patch_id": f"patch_{(parsed.progress_id or 'x')[-8:]}",
        }

    fallback = build_gap_patch(plan, intent, worker_results=worker_results)
    if isinstance(fallback, dict):
        # fallback 也只绑定实际 add_tasks 对应的 gap
        ids: list[str] = []
        for raw in list(fallback.get("add_tasks") or []):
            if not isinstance(raw, dict):
                continue
            meta = dict(raw.get("metadata") or {})
            if not meta.get("resolves_gap_ids") and parsed.open_gap_ids:
                gid = parsed.open_gap_ids[len(ids)] if len(ids) < len(parsed.open_gap_ids) else ""
                if gid:
                    meta["resolves_gap_ids"] = [gid]
                    raw["metadata"] = meta
            for gid in meta.get("resolves_gap_ids") or []:
                if gid and gid not in ids:
                    ids.append(str(gid))
        fallback["target_gap_ids"] = ids
        fallback.setdefault("triggered_by", parsed.progress_id or "")
    return fallback


def _objective_from_gap(item: str, plan: ExecutionPlan) -> str:
    text = str(item or "")
    parts = text.split(":", 2)
    if len(parts) >= 3:
        tid, rest = parts[1], parts[2]
        for step in plan.steps:
            if step.task_id == tid or str(step.resolved_task_id(0)) == tid:
                return f"补充证据：{step.objective or step.description or rest}"
        return f"补充证据：{rest}"
    return f"补充证据：{text}" if text else ""


def build_gap_patch(
    plan: ExecutionPlan,
    intent: TaskIntent,
    *,
    worker_results: list[Any] | None = None,
) -> dict[str, Any]:
    """无 LLM 时的缺口补丁：失败或空证据的研究任务补一刀，且遵守来源策略。"""
    policy = parse_source_policy(intent.raw_query)
    results = list(worker_results or [])
    empty_ids = {
        str(row.get("task_id"))
        for row in results
        if isinstance(row, dict) and (not row.get("ok") or not str(row.get("summary") or "").strip())
    }
    failed = [
        step
        for step in plan.steps
        if step.step_type in RESEARCH_TYPES
        and (
            str(step.metadata.get("status") or "") == "failed"
            or step.task_id in empty_ids
        )
    ]
    if not failed:
        failed = [
            step
            for step in plan.steps
            if step.step_type in RESEARCH_TYPES
            and str(step.metadata.get("status") or "") != "done"
        ]
    target = failed[0] if failed else None
    if target is None:
        return {"add_tasks": [], "reason": "no_gap"}
    sources = [
        s
        for s in (target.metadata or {}).get("allowed_sources") or []
        if s in policy.allowed_sources
    ] or [s for s in ("web", "file") if s in policy.allowed_sources]
    if not sources:
        return {"add_tasks": [], "reason": "no_allowed_source"}
    return {
        "reason": f"gap:{target.task_id}",
        "add_tasks": [
            {
                "task_id": f"{target.task_id}_gap",
                "objective": f"补充证据：{target.objective or target.description}",
                "depends_on": [],
                "allowed_sources": sources,
            }
        ],
    }

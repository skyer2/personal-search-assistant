"""PlanPatch：Evaluator 发现缺口后，由代码决定是否接受补丁。"""

from __future__ import annotations

import re
from typing import Any

from app.agent.harness.planner import finalize_plan
from app.agent.harness.state import ExecutionPlan, PlanStep, TaskIntent
from app.research.planning.lead_planner import research_step_from_task
from app.research.planning.policy import SourcePolicy, parse_source_policy
from app.research.planning.priority import stamp_semantic_priority
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
        raw_meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        coverage_keys = [str(x) for x in (raw_meta.get("coverage_keys") or []) if str(x).strip()]
        step = research_step_from_task(
            task_id=tid,
            objective=objective,
            depends_on=depends,
            sources=sources,
            coverage_keys=coverage_keys,
            required=bool(raw_meta.get("required", True)),
            priority=int(raw_meta.get("priority", 0) or 0),
            task_kind=str(raw_meta.get("task_kind") or "deep_dive"),
            subject_id=str(raw_meta.get("subject_id") or ""),
            extra_metadata={
                key: value
                for key, value in raw_meta.items()
                if key not in {"coverage_keys", "required", "priority", "task_kind", "subject_id"}
            },
        )
        new_steps.insert(synth_index + inserted, step)
        existing.add(tid)
        inserted += 1
        synth_index += 1
    if inserted == 0:
        return plan, ["empty_patch"]
    candidate = ExecutionPlan(
        steps=new_steps,
        summary=" → ".join(s.description for s in new_steps),
        plan_version=int(plan.plan_version or 1) + 1,
        planning_mode=plan.planning_mode or "dynamic",
        research_brief=plan.research_brief,
    )
    candidate = finalize_plan(candidate)
    candidate = stamp_semantic_priority(candidate, intent=intent)
    required_ids = [
        step.task_id
        for step in candidate.steps
        if step.task_id
        and step.step_type in RESEARCH_TYPES
        and not bool((step.metadata or {}).get("optional"))
    ]
    for step in candidate.steps:
        if step.step_type in {"generate_markdown", "summarize"}:
            step.depends_on = list(required_ids)
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
    candidate_set: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 ProgressAssessment 收成受约束的 PlanPatch proposal（仍须 apply_plan_patch）。

    每个新任务绑定 resolves_gap_ids；target_gap_ids 仅为实际被选中的 gap。
    expected_disagreement 不进入补丁。
    """
    from app.research.planning.progress import ProgressAssessment

    parsed = ProgressAssessment.from_dict(assessment or {})
    brief_kind = _brief_task_kind(intent)
    if brief_kind == "landscape_discovery":
        return _build_landscape_gap_patch(
            intent,
            parsed=parsed,
            candidate_set=candidate_set,
            max_new_tasks=max_new_tasks,
        )
    policy = parse_source_policy(intent.raw_query)
    default_sources = [s for s in ("web", "file") if s in policy.allowed_sources]
    if not default_sources:
        return {"add_tasks": [], "reason": "no_allowed_source", "target_gap_ids": []}

    # gap_id → description 映射（仅 actionable）
    gap_by_desc: dict[str, str] = {}
    for item in parsed.gaps or []:
        if not isinstance(item, dict):
            continue
        if item.get("blocking", item.get("actionable", True)) is False:
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
                    "required": True,
                    "optional": False,
                    "priority": 0,
                    "coverage_keys": [signal] if signal else [],
                },
            }
        )

    # 只对 actionable 信号建任务；conflicts 用 unresolved 列表
    candidates: list[tuple[str, str, str]] = []
    for gap in parsed.gaps:
        if not isinstance(gap, dict) or gap.get("blocking", gap.get("actionable", True)) is False:
            continue
        desc = str(gap.get("description") or "").strip()
        if desc:
            candidates.append((_objective_from_gap(desc, plan), "coverage", desc))
    if not candidates:
        candidates.extend((_objective_from_gap(item, plan), "coverage", item) for item in parsed.coverage_gaps)
    seen_clusters: set[str] = set()
    for objective, reason, signal in candidates:
        cluster = _gap_cluster(signal)
        if cluster in seen_clusters:
            continue
        seen_clusters.add(cluster)
        _append(objective, reason, signal)
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


def _brief_task_kind(intent: TaskIntent) -> str:
    brief = getattr(intent, "brief", None)
    if isinstance(brief, dict):
        return str(brief.get("task_kind") or "")
    return str(getattr(brief, "task_kind", "") or "")


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", str(value or "").strip()).strip("_")
    return cleaned[:48].lower() or "candidate"


def _candidate_names(candidate_set: dict[str, Any] | None) -> list[str]:
    if not isinstance(candidate_set, dict) or not candidate_set.get("available"):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for raw in candidate_set.get("items") or []:
        name = re.sub(r"^[\s\d\.\-、·]+", "", str(raw or "").strip())
        name = re.split(r"[:：]", name, maxsplit=1)[0].strip()
        if not 2 <= len(name) <= 48:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= 8:
            break
    return names


def _gap_dimension(text: str) -> str:
    value = str(text or "")
    rules = (
        ("招聘与人才机会", ("招聘", "薪酬", "人才", "岗位")),
        ("团队背景", ("团队", "创始", "组织")),
        ("商业化进展", ("商业化", "营收", "订单", "客户")),
        ("融资与估值", ("融资", "估值", "投资")),
        ("加入风险", ("风险", "监管", "竞争")),
        ("技术路线与产品", ("技术", "产品", "模型")),
    )
    for dimension, markers in rules:
        if any(marker in value for marker in markers):
            return dimension
    return "关键事实"


def _build_landscape_gap_patch(
    intent: TaskIntent,
    *,
    parsed: Any,
    candidate_set: dict[str, Any] | None,
    max_new_tasks: int,
) -> dict[str, Any]:
    policy = parse_source_policy(intent.raw_query)
    allowed_sources = [
        source
        for source in ("web", "file")
        if source in policy.allowed_sources
    ]
    candidates = _candidate_names(candidate_set)
    gaps = [
        item
        for item in (parsed.gaps or [])
        if isinstance(item, dict)
        and item.get("blocking", item.get("actionable", True)) is not False
        and str(item.get("gap_id") or "").strip()
    ]
    if not candidates or not gaps:
        return {
            "reason": "candidate_set_unavailable" if not candidates else "no_actionable_gap",
            "add_tasks": [],
            "target_gap_ids": [],
            "triggered_by": parsed.progress_id or "",
        }

    dimensions: list[str] = []
    gap_ids: list[str] = []
    for gap in gaps:
        dimension = _gap_dimension(str(gap.get("description") or ""))
        if dimension not in dimensions:
            dimensions.append(dimension)
        gap_id = str(gap.get("gap_id") or "")
        if gap_id and gap_id not in gap_ids:
            gap_ids.append(gap_id)

    tasks: list[dict[str, Any]] = []
    for candidate in candidates[: max(0, max_new_tasks)]:
        selected_dimensions = dimensions[:4]
        subject_id = _slug(candidate)
        objective = (
            f"{candidate}：补充{'、'.join(selected_dimensions)}证据；"
            "仅针对该候选补齐缺口，不重新扫描整个赛道。"
        )
        tasks.append(
            {
                "task_id": f"t_gap_{subject_id}",
                "objective": objective[:200],
                "depends_on": [],
            "allowed_sources": list(allowed_sources),
                "reason": "candidate_gap_fill",
                "metadata": {
                    "task_kind": "gap_fill",
                    "subject_id": subject_id,
                    "entities": [candidate],
                    "coverage_keys": selected_dimensions,
                    "resolves_gap_ids": gap_ids,
                    "requires_artifacts": ["candidate_set"],
                    "patch_reason": "candidate_gap_fill",
                    "required": True,
                    "optional": False,
                    "priority": 0,
                },
            }
        )
    return {
        "reason": "candidate_gap_fill",
        "add_tasks": tasks,
        "target_gap_ids": gap_ids,
        "triggered_by": parsed.progress_id or "",
        "patch_id": f"patch_gap_{_slug(intent.raw_query)[:16]}",
    }


def _objective_from_gap(item: str, plan: ExecutionPlan) -> str:
    text = str(item or "")
    parts = text.split(":", 2)
    if len(parts) >= 3:
        tid, rest = parts[1], parts[2]
        for step in plan.steps:
            if step.task_id == tid or str(step.resolved_task_id(0)) == tid:
                return f"针对性核验：{rest}（仅补此缺口；参考原任务：{step.objective or step.description}）"
        return f"针对性核验：{rest}"
    return f"针对性核验：{text}" if text else ""


def _gap_cluster(text: str) -> str:
    value = str(text or "").lower()
    groups = {
        "benchmark_authority": ("benchmark", "swe-bench", "livecode", "基准", "脚手架", "官方来源"),
        "adoption": ("mau", "采用", "用户", "usage", "cursor", "claude code"),
        "productivity": ("metr", "dora", "productivity", "生产率", "效率", "相关系数"),
        "security": ("security", "安全", "漏洞", "audit", "审计"),
    }
    for name, tokens in groups.items():
        if any(token in value for token in tokens):
            return name
    return re.sub(r"\s+", " ", value)[:100]


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
                "metadata": {
                    "required": True,
                    "optional": False,
                    "priority": 0,
                    "resolves_gap_ids": [f"gap:{target.task_id}"],
                },
            }
        ],
    }

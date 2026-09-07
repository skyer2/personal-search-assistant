"""Constrained research-task patches derived from semantic gap assessments."""

from __future__ import annotations

import re
from typing import Any

from app.agent.harness.state import ExecutionPlan, TaskIntent
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
    """Validate and append research tasks; fixed pipeline steps are never planned."""
    resolved_policy = policy or parse_source_policy(intent.raw_query)
    additions = [item for item in list(patch.get("add_tasks") or []) if isinstance(item, dict)][
        : max(0, max_new_tasks)
    ]
    if not additions:
        return plan, ["empty_patch"]

    existing = {step.task_id for step in plan.steps if step.task_id}
    new_steps = list(plan.steps)
    inserted = 0
    for raw in additions:
        objective = str(raw.get("objective") or "").strip()
        if not objective:
            continue
        task_id = str(raw.get("task_id") or f"t_patch_{inserted + 1}")
        if task_id in existing:
            task_id = f"{task_id}_v{plan.plan_version + 1}"
        sources = [
            source
            for source in (raw.get("allowed_sources") or [])
            if str(source) in resolved_policy.allowed_sources
        ] or [
            source for source in ("web", "file") if source in resolved_policy.allowed_sources
        ]
        raw_metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        step = research_step_from_task(
            task_id=task_id,
            objective=objective,
            depends_on=[str(item) for item in raw.get("depends_on") or [] if str(item) in existing],
            sources=sources,
            coverage_keys=[str(item) for item in raw_metadata.get("coverage_keys") or []],
            required=bool(raw_metadata.get("required", True)),
            priority=int(raw_metadata.get("priority", 0) or 0),
            task_kind=str(raw_metadata.get("task_kind") or "deep_dive"),
            subject_id=str(raw_metadata.get("subject_id") or ""),
            extra_metadata={
                key: value
                for key, value in raw_metadata.items()
                if key
                not in {"coverage_keys", "required", "priority", "task_kind", "subject_id"}
            },
        )
        new_steps.append(step)
        existing.add(task_id)
        inserted += 1
    if inserted == 0:
        return plan, ["empty_patch"]

    candidate = ExecutionPlan(
        steps=new_steps,
        summary=" → ".join(step.description for step in new_steps),
        plan_version=int(plan.plan_version or 1) + 1,
        planning_mode=plan.planning_mode or "dynamic",
        research_brief=plan.research_brief,
    )
    candidate = stamp_semantic_priority(candidate, intent=intent)
    issues = validate_hybrid_plan(
        intent,
        candidate,
        policy=resolved_policy,
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
    """Convert a pure semantic gap assessment into a bounded patch proposal."""
    value = dict(assessment or {})
    if str(value.get("status") or value.get("verdict") or "") != "gap":
        return {"add_tasks": [], "reason": "progress_not_gap", "target_gap_ids": []}

    policy = parse_source_policy(intent.raw_query)
    allowed_sources = [
        source for source in ("web", "file") if source in policy.allowed_sources
    ]
    if not allowed_sources:
        return {"add_tasks": [], "reason": "no_allowed_source", "target_gap_ids": []}

    coverage_gaps = [str(item) for item in value.get("coverage_gaps") or [] if str(item).strip()]
    missing_dimensions = [
        str(item) for item in value.get("missing_dimensions") or [] if str(item).strip()
    ]
    conflicts = [
        str(item)
        for item in value.get("unresolved_conflicts") or value.get("conflicts") or []
        if str(item).strip()
    ]
    stale = [str(item) for item in value.get("stale_evidence") or [] if str(item).strip()]
    signals = _deduplicate([*coverage_gaps, *missing_dimensions])

    candidates = _candidate_names(candidate_set)
    if candidates and _brief_task_kind(intent) == "landscape_discovery":
        dimensions = [_gap_dimension(item) for item in signals] or ["关键事实"]
        tasks = [
            {
                "task_id": f"t_gap_{_slug(candidate)}",
                "objective": (
                    f"{candidate}：补充{'、'.join(dimensions[:4])}证据；"
                    "仅针对该候选补齐缺口，不重新扫描整个赛道。"
                )[:200],
                "depends_on": [],
                "allowed_sources": list(allowed_sources),
                "metadata": {
                    "task_kind": "gap_fill",
                    "subject_id": _slug(candidate),
                    "entities": [candidate],
                    "coverage_keys": dimensions[:4],
                    "resolves_gap_ids": signals,
                    "requires_artifacts": ["candidate_set"],
                    "required": True,
                    "optional": False,
                    "priority": 0,
                },
            }
            for candidate in candidates[: max(0, max_new_tasks)]
        ]
        return {
            "reason": "candidate_gap_fill",
            "add_tasks": tasks,
            "target_gap_ids": signals,
        }

    proposals: list[dict[str, Any]] = []
    for signal in signals:
        if len(proposals) >= max(0, max_new_tasks):
            break
        proposals.append(
            _proposal(
                _objective_from_gap(signal, plan),
                "coverage" if signal in coverage_gaps else "missing_dimension",
                signal,
                allowed_sources,
            )
        )
    for signal in conflicts:
        if len(proposals) >= max(0, max_new_tasks):
            break
        proposals.append(
            _proposal(f"交叉验证冲突：{signal}", "unresolved_conflict", signal, allowed_sources)
        )
    for signal in stale:
        if len(proposals) >= max(0, max_new_tasks):
            break
        proposals.append(
            _proposal(f"补充最新年份证据：{signal}", "stale_evidence", signal, allowed_sources)
        )
    if proposals:
        return {
            "reason": str(value.get("reason_codes") and value["reason_codes"][0] or "semantic_gap"),
            "add_tasks": proposals,
            "target_gap_ids": [item for item in signals if item],
        }
    return build_gap_patch(plan, intent, worker_results=worker_results)


def build_gap_patch(
    plan: ExecutionPlan,
    intent: TaskIntent,
    *,
    worker_results: list[Any] | None = None,
) -> dict[str, Any]:
    """Fallback patch for failed or evidence-empty research tasks."""
    policy = parse_source_policy(intent.raw_query)
    empty_ids = {
        str(row.get("task_id"))
        for row in list(worker_results or [])
        if isinstance(row, dict) and (not row.get("ok") or not str(row.get("summary") or "").strip())
    }
    target = next(
        (
            step
            for step in plan.steps
            if step.step_type in RESEARCH_TYPES
            and (str(step.metadata.get("status") or "") == "failed" or step.task_id in empty_ids)
        ),
        None,
    )
    if target is None:
        return {"add_tasks": [], "reason": "no_gap", "target_gap_ids": []}
    sources = [
        source
        for source in (target.metadata or {}).get("allowed_sources") or []
        if source in policy.allowed_sources
    ] or [source for source in ("web", "file") if source in policy.allowed_sources]
    if not sources:
        return {"add_tasks": [], "reason": "no_allowed_source", "target_gap_ids": []}
    gap_id = f"gap:{target.task_id}"
    return {
        "reason": gap_id,
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
                    "resolves_gap_ids": [gap_id],
                },
            }
        ],
        "target_gap_ids": [gap_id],
    }


def _proposal(
    objective: str,
    reason: str,
    signal: str,
    allowed_sources: list[str],
) -> dict[str, Any]:
    from app.observability.semantic import stable_gap_id

    gap_id = stable_gap_id(reason, signal or reason)
    return {
        "task_id": f"t_gap_{_slug(signal or reason)}",
        "objective": objective[:200],
        "depends_on": [],
        "allowed_sources": list(allowed_sources),
        "metadata": {
            "resolves_gap_ids": [gap_id],
            "patch_reason": reason,
            "required": True,
            "optional": False,
            "priority": 0,
            "coverage_keys": [signal] if signal else [],
        },
    }


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


def _objective_from_gap(item: str, plan: ExecutionPlan) -> str:
    text = str(item or "")
    parts = text.split(":", 2)
    if len(parts) >= 3:
        task_id, rest = parts[1], parts[2]
        for step in plan.steps:
            if step.task_id == task_id or str(step.resolved_task_id(0)) == task_id:
                return f"针对性核验：{rest}（参考原任务：{step.objective or step.description}）"
        return f"针对性核验：{rest}"
    return f"针对性核验：{text}" if text else ""


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))

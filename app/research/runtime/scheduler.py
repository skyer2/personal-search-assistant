"""
DAG Scheduler：宏观控制代码化。

READY = PENDING 且 depends_on 均 DONE。
并行单元：research task（含旧数据源步）。

Evidence-driven early stop：
- research 任务打 required/optional 优先级
- Synthesis 只依赖 required 检索任务
- enough / force_synthesis 时 skip 剩余 optional
"""

from __future__ import annotations

from typing import Any, Iterable

from app.agent.harness.orchestration import RETRIEVAL_STEP_TYPES, SYNTHESIS_STEP_TYPES
from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.planning.priority import stamp_semantic_priority

TERMINAL_STATUS = frozenset({"done", "failed", "skipped"})


def _stamp_research_priority(plan: ExecutionPlan, intent: Any | None = None) -> None:
    """Planner/Brief 语义优先级；禁止按列表位置把后半任务打成 optional。"""
    stamp_semantic_priority(plan, intent=intent)


def required_retrieval_ids(plan: ExecutionPlan) -> list[str]:
    ids: list[str] = []
    for index, step in enumerate(plan.steps):
        if step.step_type not in RETRIEVAL_STEP_TYPES:
            continue
        meta = step.metadata if isinstance(step.metadata, dict) else {}
        if meta.get("optional"):
            continue
        ids.append(step.resolved_task_id(index))
    if not ids:
        # fallback：全部 retrieval
        ids = [
            step.resolved_task_id(i)
            for i, step in enumerate(plan.steps)
            if step.step_type in RETRIEVAL_STEP_TYPES
        ]
    return ids


def annotate_plan_tasks(plan: ExecutionPlan, intent: Any | None = None) -> ExecutionPlan:
    """为每步补 task_id / depends_on。检索步默认无依赖；合成步依赖 required 检索。"""
    _stamp_research_priority(plan, intent=intent)
    retrieval_ids: list[str] = []
    markdown_id = ""
    for index, step in enumerate(plan.steps):
        if not step.task_id:
            step.task_id = f"t{index}:{step.step_type}"
        if step.depends_on:
            if step.step_type in RETRIEVAL_STEP_TYPES:
                retrieval_ids.append(step.task_id)
            if step.step_type == "generate_markdown":
                markdown_id = step.task_id
            continue
        if step.step_type in RETRIEVAL_STEP_TYPES:
            if not step.depends_on:
                step.depends_on = []
            retrieval_ids.append(step.task_id)
        elif step.step_type in {"generate_markdown", "summarize"}:
            # Evidence-driven：只等 required，不等 optional straggler
            step.depends_on = required_retrieval_ids(plan) or list(retrieval_ids)
            markdown_id = step.task_id
        elif step.step_type == "convert_pdf":
            step.depends_on = [markdown_id] if markdown_id else required_retrieval_ids(plan)
        else:
            step.depends_on = required_retrieval_ids(plan) or list(retrieval_ids)
    if plan.plan_version < 1:
        plan.plan_version = 1
    return plan


def task_status_map(plan: ExecutionPlan) -> dict[str, str]:
    status: dict[str, str] = {}
    for index, step in enumerate(plan.steps):
        tid = step.resolved_task_id(index)
        status[tid] = str(step.metadata.get("status") or "pending")
    return status


def _deps_satisfied(
    step: PlanStep,
    status: dict[str, str],
    *,
    allow_failed_deps: bool = False,
) -> bool:
    for dep in step.depends_on or []:
        current = status.get(dep)
        if current == "done":
            continue
        if allow_failed_deps and current in {"failed", "skipped"}:
            continue
        if current == "skipped":
            # skipped optional deps 不挡合成
            continue
        return False
    return True


def ready_steps(
    plan: ExecutionPlan,
    status: dict[str, str] | None = None,
    *,
    include_types: Iterable[str] | None = None,
    allow_failed_deps: bool = False,
    include_optional: bool = True,
) -> list[tuple[int, PlanStep]]:
    status = status or task_status_map(plan)
    allowed = set(include_types) if include_types is not None else None
    ready: list[tuple[int, PlanStep]] = []
    for index, step in enumerate(plan.steps):
        tid = step.resolved_task_id(index)
        current = status.get(tid, "pending")
        if current not in {"pending", "running"}:
            continue
        if current == "running":
            continue
        if allowed is not None and step.step_type not in allowed:
            continue
        meta = step.metadata if isinstance(step.metadata, dict) else {}
        if not include_optional and meta.get("optional"):
            continue
        if _deps_satisfied(step, status, allow_failed_deps=allow_failed_deps):
            ready.append((index, step))
    return ready


def ready_research_steps(
    plan: ExecutionPlan,
    status: dict[str, str] | None = None,
    *,
    include_optional: bool = True,
) -> list[tuple[int, PlanStep]]:
    """可 fan-out 的研究任务：旧数据源步 + research objective 步。"""
    return ready_steps(
        plan,
        status,
        include_types=RETRIEVAL_STEP_TYPES,
        include_optional=include_optional,
    )


def ready_retrieval_steps(
    plan: ExecutionPlan,
    status: dict[str, str] | None = None,
) -> list[tuple[int, PlanStep]]:
    return ready_research_steps(plan, status)


def all_retrieval_done(plan: ExecutionPlan, status: dict[str, str] | None = None) -> bool:
    status = status or task_status_map(plan)
    retrieval = [
        step.resolved_task_id(i)
        for i, step in enumerate(plan.steps)
        if step.step_type in RETRIEVAL_STEP_TYPES
    ]
    if not retrieval:
        return True
    return all(status.get(tid) in TERMINAL_STATUS for tid in retrieval)


def required_retrieval_done(plan: ExecutionPlan, status: dict[str, str] | None = None) -> bool:
    status = status or task_status_map(plan)
    required = required_retrieval_ids(plan)
    if not required:
        return all_retrieval_done(plan, status)
    return all(status.get(tid) in TERMINAL_STATUS for tid in required)


def skip_optional_pending(
    plan: ExecutionPlan,
    status: dict[str, str] | None = None,
    *,
    reason: str = "early_stop_enough",
) -> dict[str, str]:
    """把尚未开始的 optional research 标为 skipped；返回更新后的 status map。"""
    status = dict(status or task_status_map(plan))
    for index, step in enumerate(plan.steps):
        if step.step_type not in RETRIEVAL_STEP_TYPES:
            continue
        meta = step.metadata if isinstance(step.metadata, dict) else {}
        if not meta.get("optional"):
            continue
        tid = step.resolved_task_id(index)
        if status.get(tid, "pending") != "pending":
            continue
        status[tid] = "skipped"
        meta["status"] = "skipped"
        meta["skip_reason"] = reason
        step.metadata = meta
    return status


def skip_pending_research(
    plan: ExecutionPlan,
    status: dict[str, str] | None = None,
    *,
    reason: str = "force_synthesis",
    include_required: bool = False,
    include_running: bool = False,
) -> dict[str, str]:
    """Skip pending optional (and optionally remaining required) so synthesis can start."""
    status = skip_optional_pending(plan, status, reason=reason)
    if not include_required:
        return status
    for index, step in enumerate(plan.steps):
        if step.step_type not in RETRIEVAL_STEP_TYPES:
            continue
        tid = step.resolved_task_id(index)
        current = status.get(tid, "pending")
        if current != "pending" and not (include_running and current == "running"):
            continue
        meta = step.metadata if isinstance(step.metadata, dict) else {}
        status[tid] = "skipped"
        meta["status"] = "skipped"
        meta["skip_reason"] = reason
        if current == "running":
            meta["cancelled_by_deadline"] = True
        step.metadata = meta
    return status


def select_dispatch_wave(
    plan: ExecutionPlan,
    status: dict[str, str] | None = None,
    *,
    include_optional: bool = False,
    max_parallel: int = 3,
) -> list[tuple[int, PlanStep]]:
    """Required-first staged dispatch: one Pregel wave ≤ max_parallel, never dump all tasks."""
    ready = ready_research_steps(plan, status, include_optional=include_optional)
    cap = max(1, int(max_parallel or 1))
    return ready[:cap]


def next_synthesis_step(
    plan: ExecutionPlan,
    status: dict[str, str] | None = None,
    *,
    allow_failed_deps: bool = False,
) -> tuple[int, PlanStep] | None:
    ready = ready_steps(
        plan,
        status,
        include_types=SYNTHESIS_STEP_TYPES,
        allow_failed_deps=allow_failed_deps,
    )
    return ready[0] if ready else None


def dispatch_sends(
    plan: ExecutionPlan,
    status: dict[str, str] | None = None,
    *,
    include_optional: bool = False,
    max_parallel: int = 3,
) -> list[dict[str, Any]]:
    """纯数据描述的 fan-out 清单；graph.py 再转成 Send。默认只要 P0。"""
    payloads: list[dict[str, Any]] = []
    for index, step in select_dispatch_wave(
        plan, status, include_optional=include_optional, max_parallel=max_parallel
    ):
        payloads.append(
            {
                "task_id": step.resolved_task_id(index),
                "step_index": index,
                "step_type": step.step_type,
                "description": step.description,
                "subagent": step.subagent or "",
            }
        )
    return payloads

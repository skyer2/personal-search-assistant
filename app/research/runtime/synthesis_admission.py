"""Synthesis admission policy shared by graph routing and runner nodes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.domain.contracts import task_status_projection, tasks_from_status
from app.research.runtime.scheduler import (
    RETRIEVAL_STEP_TYPES,
    SYNTHESIS_STEP_TYPES,
    TERMINAL_STATUS,
    next_synthesis_step,
    skip_optional_pending,
    skip_pending_research,
)


@dataclass(frozen=True)
class SynthesisAdmission:
    allowed: bool
    mode: str
    reason: str
    trusted_evidence_count: int
    worker_terminal_count: int
    required_research_pending: tuple[str, ...]
    skip_required: bool


def _research_steps(plan: ExecutionPlan) -> list[tuple[int, PlanStep]]:
    return [
        (index, step)
        for index, step in enumerate(plan.steps)
        if step.step_type in RETRIEVAL_STEP_TYPES
    ]


def _required_pending(plan: ExecutionPlan, status: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        step.resolved_task_id(index)
        for index, step in _research_steps(plan)
        if not bool((step.metadata or {}).get("optional"))
        and status.get(step.resolved_task_id(index), "pending")
        in {"pending", "running"}
    )


def trusted_evidence_count(state: dict[str, Any]) -> int:
    refs: set[str] = set()
    for row in list(state.get("worker_results") or []):
        if not isinstance(row, dict):
            continue
        raw_payload: Any = row.get("payload")
        payload: dict[str, Any] = (
            dict(raw_payload) if isinstance(raw_payload, dict) else {}
        )
        refs.update(
            str(item)
            for item in list(payload.get("evidence_ids") or [])
            + list(payload.get("sources") or [])
            if str(item).strip()
        )
    refs.update(
        str(item)
        for item in list(state.get("evidence_refs") or [])
        if str(item).strip() and not str(item).startswith("step:")
    )
    return len(refs)


def evaluate_synthesis_admission(
    state: dict[str, Any],
    plan: ExecutionPlan,
    status: dict[str, str],
    *,
    forced: bool = False,
    deadline: bool = False,
    trusted_evidence_count_override: int | None = None,
) -> SynthesisAdmission:
    raw_assessment: Any = state.get("progress_assessment")
    assessment: dict[str, Any] = (
        dict(raw_assessment) if isinstance(raw_assessment, dict) else {}
    )
    progress_reason = str(assessment.get("reason") or "")
    emergency_reason = ""
    if forced or progress_reason == "force_synthesis_budget":
        emergency_reason = "force_synthesis_budget"
    elif deadline:
        emergency_reason = "deadline_exceeded"
    elif state.get("abort_reason"):
        emergency_reason = str(state.get("abort_reason") or "aborted")
    elif progress_reason == "graph_no_progress":
        emergency_reason = "graph_no_progress"
    elif bool(state.get("replan_exhausted")):
        emergency_reason = "replan_exhausted"

    evidence_count = (
        int(trusted_evidence_count_override)
        if trusted_evidence_count_override is not None
        else trusted_evidence_count(state)
    )
    worker_terminal_count = sum(
        1
        for index, step in _research_steps(plan)
        if status.get(step.resolved_task_id(index), "pending") in TERMINAL_STATUS
    )
    required_pending = _required_pending(plan, status)
    blocking_gaps = [
        item
        for item in list(assessment.get("gaps") or [])
        if isinstance(item, dict)
        and item.get("blocking", item.get("actionable", True)) is not False
        and item.get("actionable", True) is not False
        and str(item.get("severity") or "high")
        in {"high", "medium", "blocking", "important"}
    ]

    if emergency_reason:
        if evidence_count == 0:
            return SynthesisAdmission(
                allowed=True,
                mode="no_evidence_partial",
                reason=f"no_trusted_evidence:{emergency_reason}",
                trusted_evidence_count=0,
                worker_terminal_count=worker_terminal_count,
                required_research_pending=required_pending,
                skip_required=True,
            )
        return SynthesisAdmission(
            allowed=True,
            mode="emergency",
            reason=emergency_reason,
            trusted_evidence_count=evidence_count,
            worker_terminal_count=worker_terminal_count,
            required_research_pending=required_pending,
            skip_required=True,
        )

    if required_pending:
        return SynthesisAdmission(
            allowed=False,
            mode="normal",
            reason="required_research_pending",
            trusted_evidence_count=evidence_count,
            worker_terminal_count=worker_terminal_count,
            required_research_pending=required_pending,
            skip_required=False,
        )
    if evidence_count == 0:
        return SynthesisAdmission(
            allowed=False,
            mode="normal",
            reason="no_trusted_evidence",
            trusted_evidence_count=0,
            worker_terminal_count=worker_terminal_count,
            required_research_pending=required_pending,
            skip_required=False,
        )
    if blocking_gaps:
        return SynthesisAdmission(
            allowed=False,
            mode="normal",
            reason="blocking_coverage_gap",
            trusted_evidence_count=evidence_count,
            worker_terminal_count=worker_terminal_count,
            required_research_pending=required_pending,
            skip_required=False,
        )
    return SynthesisAdmission(
        allowed=True,
        mode="normal",
        reason="coverage_and_evidence_satisfied",
        trusted_evidence_count=evidence_count,
        worker_terminal_count=worker_terminal_count,
        required_research_pending=required_pending,
        skip_required=False,
    )


def _mark_pending_synthesis_skipped(
    plan: ExecutionPlan,
    status: dict[str, str],
    *,
    reason: str,
) -> dict[str, str]:
    status = dict(status)
    for index, step in enumerate(plan.steps):
        if step.step_type not in SYNTHESIS_STEP_TYPES:
            continue
        task_id = step.resolved_task_id(index)
        if status.get(task_id, "pending") not in {"pending", "running"}:
            continue
        status[task_id] = "skipped"
    return status


def prepare_synthesis_update(
    state: dict[str, Any],
    plan: ExecutionPlan,
    *,
    forced: bool = False,
    deadline: bool = False,
    trusted_evidence_count_override: int | None = None,
) -> dict[str, Any]:
    admission = evaluate_synthesis_admission(
        state,
        plan,
        task_status_projection(state.get("tasks")) or dict(state.get("task_status") or {}),
        forced=forced,
        deadline=deadline,
        trusted_evidence_count_override=trusted_evidence_count_override,
    )
    if not admission.allowed:
        raise ValueError(
            f"synthesis admission rejected: {admission.reason}"
        )

    runtime_status = task_status_projection(state.get("tasks")) or dict(
        state.get("task_status") or {}
    )
    status = skip_optional_pending(
        plan,
        runtime_status,
        reason=(
            "emergency_synthesis"
            if admission.mode != "normal"
            else "early_stop_enough"
        ),
    )
    if next_synthesis_step(plan, status, allow_failed_deps=True) is None:
        if not admission.skip_required:
            raise ValueError(
                "normal synthesis cannot skip required research"
            )
        status = skip_pending_research(
            plan,
            status,
            reason="emergency_synthesis",
            include_required=True,
            include_running=True,
        )
    if next_synthesis_step(plan, status, allow_failed_deps=True) is None:
        raise ValueError("prepare_synthesis cannot make synthesis runnable")

    common = {
        "plan": plan.to_dict(),
        "tasks": tasks_from_status(status),
        "task_status": status,
        "synthesis_admission": True,
        "synthesis_mode": admission.mode,
        "synthesis_admission_reason": admission.reason,
        "trusted_evidence_count": admission.trusted_evidence_count,
        "progress": "ready_for_synthesis",
    }
    if admission.mode == "no_evidence_partial":
        status = _mark_pending_synthesis_skipped(
            plan,
            status,
            reason="no_trusted_evidence",
        )
        from app.agent.harness.partial_report import (
            render_no_evidence_partial_report,
        )

        raw_assessment: Any = state.get("progress_assessment")
        assessment: dict[str, Any] = (
            dict(raw_assessment) if isinstance(raw_assessment, dict) else {}
        )
        common.update(
            {
                "plan": plan.to_dict(),
                "tasks": tasks_from_status(status),
                "task_status": status,
                "status": "partial",
                "final_content": render_no_evidence_partial_report(
                    objective=str(
                        state.get("resolved_query") or state.get("task_query") or ""
                    ),
                    missing_dimensions=[
                        str(item)
                        for item in list(assessment.get("missing_dimensions") or [])
                        + list(assessment.get("coverage_gaps") or [])
                        if str(item).strip()
                    ][:12],
                ),
                "replan_exhausted": True,
                "progress": "no_evidence_partial",
            }
        )
        return common
    if admission.mode == "emergency":
        common["replan_exhausted"] = True
    return common

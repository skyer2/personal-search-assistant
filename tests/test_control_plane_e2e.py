"""Deterministic end-to-end gates for the converged control plane."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.control.transitions import terminal_update
from app.research.domain.contracts import (
    OutcomeStatus,
    TaskStatus,
    WorkflowPhase,
    initialize_tasks,
    merge_task_state,
    task_status_projection,
)
from app.research.runtime.graph import compile_research_graph
from app.research.runtime.state import empty_research_state


ROOT = Path(__file__).resolve().parents[1]


def _base_plan() -> ExecutionPlan:
    return ExecutionPlan(
        summary="deterministic landscape plan",
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_landscape",
                description="landscape",
            ),
            PlanStep(
                step_type="summarize",
                task_id="t_summary",
                description="summary",
                depends_on=["t_landscape"],
            ),
        ],
    )


class DeterministicRuntime:
    def __init__(self, scenario: str):
        self.scenario = scenario
        self.plan = _base_plan()
        self.tasks: dict[str, dict[str, Any]] = initialize_tasks(self.plan)
        self.replan_calls = 0

    async def node_vanilla_agent(self, state: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def node_intent(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "intent": {"raw_query": state["task_query"]},
            "brief": {"objective": "landscape"},
            "needs_clarification": False,
            "phase": WorkflowPhase.UNDERSTAND.value,
        }

    async def node_clarify(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"needs_clarification": False, "phase": WorkflowPhase.CLARIFY.value}

    async def node_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        self.tasks = initialize_tasks(self.plan)
        return {
            "plan": self.plan.to_dict(),
            "plan_version": self.plan.plan_version,
            "tasks": self.tasks,
            "phase": WorkflowPhase.PLAN.value,
            "outcome": OutcomeStatus.RUNNING.value,
        }

    async def node_plan_validate(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "needs_plan_review": False,
            "phase": WorkflowPhase.PLAN_VALIDATED.value,
        }

    async def node_dispatch(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"progress": "dispatch", "phase": WorkflowPhase.DISPATCH.value}

    async def node_research_worker(self, state: dict[str, Any]) -> dict[str, Any]:
        task_id = str(state["task_id"])
        status = TaskStatus.PARTIAL if self.scenario == "worker_partial" else TaskStatus.DONE
        self.tasks = merge_task_state(self.tasks, task_id, status)
        return {
            "worker_results": [
                {
                    "task_id": task_id,
                    "ok": True,
                    "status": status.value,
                    "summary": "deterministic evidence",
                    "payload": {"evidence_ids": [f"evidence:{task_id}"]},
                }
            ],
            "tasks": self.tasks,
            "evidence_refs": [f"evidence:{task_id}"],
            "phase": WorkflowPhase.EXECUTE.value,
        }

    async def node_progress(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.scenario in {"gap_replan", "quality_exhausted"} and self.replan_calls == 0:
            assessment = {"verdict": "gap", "reason": "semantic_gap"}
        elif self.scenario == "replan_rejected" and self.replan_calls == 0:
            assessment = {"verdict": "gap", "reason": "semantic_gap"}
        else:
            assessment = {"verdict": "enough", "reason": "enough_evidence"}
            if self.scenario == "research_token_cap":
                assessment = {
                    "verdict": "enough",
                    "reason": "force_synthesis_budget",
                    "budget_degrade_reason": "research_token_cap",
                }
        update: dict[str, Any] = {
            "progress_assessment": assessment,
            "synthesis_admission": True,
            "phase": WorkflowPhase.PROGRESS.value,
        }
        if self.scenario == "research_token_cap":
            update["replan_exhausted"] = True
        return update

    async def node_replan(self, state: dict[str, Any]) -> dict[str, Any]:
        self.replan_calls += 1
        attempts = int(state.get("replan_attempts") or 0) + 1
        if self.scenario == "replan_rejected":
            return {
                "replan_attempts": attempts,
                "replan_exhausted": True,
                "progress": "enough",
                "progress_assessment": {
                    "verdict": "enough",
                    "reason": "replan_exhausted",
                },
                "rejected_patch_hashes": ["patch:deterministic"],
                "phase": WorkflowPhase.REPLAN.value,
            }
        if self.scenario in {"gap_replan", "quality_exhausted"}:
            self.plan.steps.insert(
                1,
                PlanStep(
                    step_type="research",
                    task_id="t_deep_dive",
                    description="deep dive",
                ),
            )
            self.tasks = initialize_tasks(self.plan)
            for task_id, task_state in dict(state.get("tasks") or {}).items():
                if task_id in self.tasks:
                    self.tasks[task_id] = task_state
            return {
                "plan": self.plan.to_dict(),
                "plan_version": self.plan.plan_version,
                "tasks": self.tasks,
                "replan_attempts": attempts,
                "replan_applied_count": 1,
                "replan_count": 1,
                "replan_exhausted": self.scenario == "quality_exhausted",
                "progress": "run",
                "progress_assessment": {"verdict": "run", "reason": "semantic_gap"},
                "phase": WorkflowPhase.REPLAN.value,
            }
        return {
            "replan_attempts": attempts,
            "replan_exhausted": True,
            "progress": "enough",
            "progress_assessment": {"verdict": "enough", "reason": "replan_exhausted"},
            "phase": WorkflowPhase.REPLAN.value,
        }

    async def node_prepare_synthesis(self, state: dict[str, Any]) -> dict[str, Any]:
        for index, step in enumerate(self.plan.steps):
            task_id = step.resolved_task_id(index)
            if step.step_type == "research":
                if self.tasks.get(task_id, {}).get("status") == TaskStatus.PENDING.value:
                    self.tasks = merge_task_state(
                        self.tasks,
                        task_id,
                        TaskStatus.SKIPPED,
                        skip_reason="force_synthesis",
                    )
            else:
                self.tasks = merge_task_state(self.tasks, task_id, TaskStatus.PENDING)
        return {
            "plan": self.plan.to_dict(),
            "tasks": self.tasks,
            "synthesis_admission": True,
            "synthesis_mode": "emergency"
            if self.scenario in {"replan_rejected", "research_token_cap"}
            else "normal",
            "progress": "ready_for_synthesis",
            "phase": WorkflowPhase.PREPARE_SYNTHESIS.value,
        }

    async def node_synthesize(self, state: dict[str, Any]) -> dict[str, Any]:
        self.tasks = merge_task_state(self.tasks, "t_summary", TaskStatus.DONE)
        token_cap = self.scenario == "research_token_cap"
        update: dict[str, Any] = {
            "tasks": self.tasks,
            "status": "partial" if token_cap else "synthesized",
            "outcome": OutcomeStatus.PARTIAL.value
            if token_cap
            else OutcomeStatus.RUNNING.value,
            "final_content": "deterministic answer",
            "trusted_evidence_count": 1,
            "progress": "synthesized",
            "phase": WorkflowPhase.SYNTHESIS.value,
        }
        if token_cap:
            update["termination"] = {
                "outcome": OutcomeStatus.PARTIAL.value,
                "reason": "research_token_cap",
                "stage": "synthesis",
                "detected_stage": "synthesis",
                "origin_stage": "research",
                "cause_event_id": "budget_event",
                "research_completed": True,
                "synthesis_attempted": True,
                "quality_attempted": False,
            }
        return update

    async def node_quality_gate(self, state: dict[str, Any]) -> dict[str, Any]:
        passed = self.scenario not in {"replan_rejected", "quality_exhausted"}
        return {
            "quality_passed": passed,
            "quality_reason": "" if passed else "quality_failed",
            "quality_repair_action": "partial",
            "quality_attempts": 1,
            "phase": WorkflowPhase.QUALITY.value,
        }

    async def node_repair_synthesis(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"phase": WorkflowPhase.REPAIR_SYNTHESIS.value}

    async def node_finalize(self, state: dict[str, Any]) -> dict[str, Any]:
        success = self.scenario in {"normal", "gap_replan", "worker_partial"}
        outcome = OutcomeStatus.SUCCESS if success else OutcomeStatus.PARTIAL
        reason = "completed" if success else (
            "replan_exhausted"
            if self.scenario == "replan_rejected"
            else "quality_failed"
            if self.scenario == "quality_exhausted"
            else "research_token_cap"
        )
        terminal = terminal_update(
            {**state, "phase": WorkflowPhase.FINALIZE.value},
            outcome=outcome,
            reason=reason,
            stage="finalize",
            detected_stage="finalize",
            research_completed=True,
            synthesis_attempted=True,
            quality_attempted=True,
        )
        return {
            "status": outcome.value,
            "outcome": outcome.value,
            "final_content": "deterministic answer",
            "progress": "done",
            **terminal,
        }

    async def node_abort(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"status": "aborted", "progress": "abort"}


async def _run(scenario: str) -> tuple[DeterministicRuntime, dict[str, Any]]:
    runtime = DeterministicRuntime(scenario)
    graph = compile_research_graph(runtime=runtime, profile="agent")
    state = empty_research_state(
        run_id=f"run-{scenario}",
        session_id=f"session-{scenario}",
        task_query="国内 AI 初创公司值得加入吗",
        max_replan_count=1,
    )
    result = await graph.ainvoke(state, config={"recursion_limit": 20})
    return runtime, result


async def test_e2e_normal_landscape_success() -> None:
    runtime, result = await _run("normal")
    assert result["status"] == OutcomeStatus.SUCCESS.value
    assert result["termination"]["reason"] == "completed"
    assert runtime.replan_calls == 0
    assert task_status_projection(result["tasks"]) == {
        "t_landscape": "done",
        "t_summary": "done",
    }


async def test_e2e_semantic_gap_replan_then_success() -> None:
    runtime, result = await _run("gap_replan")
    assert result["status"] == OutcomeStatus.SUCCESS.value
    assert result["termination"]["reason"] == "completed"
    assert runtime.replan_calls == 1
    assert result["replan_applied_count"] == 1
    assert task_status_projection(result["tasks"])["t_deep_dive"] == "done"


async def test_e2e_replan_rejected_partial_finite() -> None:
    runtime, result = await _run("replan_rejected")
    assert result["status"] == OutcomeStatus.PARTIAL.value
    assert result["termination"]["reason"] == "replan_exhausted"
    assert result["replan_attempts"] == 1
    assert result["rejected_patch_hashes"] == ["patch:deterministic"]
    assert runtime.replan_calls == 1


async def test_e2e_worker_partial_with_salvage_continues() -> None:
    runtime, result = await _run("worker_partial")
    assert result["status"] == OutcomeStatus.SUCCESS.value
    assert result["termination"]["reason"] == "completed"
    assert task_status_projection(result["tasks"])["t_landscape"] == "partial"
    assert result["tasks"]["t_summary"]["status"] == "done"


async def test_e2e_research_token_cap_preserves_first_cause() -> None:
    runtime, result = await _run("research_token_cap")
    assert result["status"] == OutcomeStatus.PARTIAL.value
    assert result["termination"]["reason"] == "research_token_cap"
    assert result["termination"]["cause_event_id"] == "budget_event"
    assert runtime.replan_calls == 0


async def test_e2e_quality_fail_after_replan_exhausted_never_replans() -> None:
    runtime, result = await _run("quality_exhausted")
    assert result["status"] == OutcomeStatus.PARTIAL.value
    assert result["termination"]["reason"] == "quality_failed"
    assert result["replan_attempts"] == 1
    assert result["replan_exhausted"] is True
    assert runtime.replan_calls == 1


def test_control_plane_runtime_invariants() -> None:
    runner = (ROOT / "app/research/runtime/runner.py").read_text(encoding="utf-8")
    state = (ROOT / "app/research/runtime/state.py").read_text(encoding="utf-8")
    assert '"recursion_limit": 20' in runner
    assert "WorkflowPhase.TERMINATED.value" not in runner
    assert "task_status:" not in state

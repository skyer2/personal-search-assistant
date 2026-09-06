"""Regression tests for research graph convergence invariants."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.research_brief import compile_research_brief
from app.agent.harness.state import ExecutionPlan, PlanStep
from app.research.planning.candidate import annotate_candidate_dependencies
from app.research.planning.validator import validate_artifact_dependencies
from app.research.runtime.state import empty_research_state


def _plan_with_optional_and_synthesis() -> ExecutionPlan:
    return ExecutionPlan(
        summary="early stop plan",
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_optional",
                description="optional research",
                metadata={"optional": True, "status": "pending"},
            ),
            PlanStep(
                step_type="summarize",
                task_id="t_summary",
                description="summary",
                depends_on=["t_optional"],
            ),
        ],
    )


def _state(plan: ExecutionPlan, **overrides: Any) -> dict[str, Any]:
    state = empty_research_state(
        run_id="run-convergence",
        session_id="session-convergence",
        task_query="国内 AI 初创公司值得加入吗",
    )
    state.update(
        {
            "plan": plan.to_dict(),
            "plan_version": plan.plan_version,
            "task_status": {
                step.resolved_task_id(index): str(
                    step.metadata.get("status") or "pending"
                )
                for index, step in enumerate(plan.steps)
            },
        }
    )
    state.update(overrides)
    return state


class _ConvergenceRuntime:
    async def node_vanilla_agent(self, state: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def node_intent(self, state: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def node_clarify(self, state: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def node_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def node_plan_validate(self, state: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def node_dispatch(self, state: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def node_research_worker(self, state: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("early-stop graph must not dispatch research workers")

    async def node_progress(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"progress_assessment": {"verdict": "enough", "reason": "test"}}

    async def node_prepare_synthesis(self, state: dict[str, Any]) -> dict[str, Any]:
        plan = ExecutionPlan.from_dict(state["plan"])
        status = dict(state["task_status"])
        for index, step in enumerate(plan.steps):
            if step.step_type == "research" and step.metadata.get("optional"):
                task_id = step.resolved_task_id(index)
                status[task_id] = "skipped"
                metadata = dict(step.metadata)
                metadata["status"] = "skipped"
                metadata["skip_reason"] = "early_stop_enough"
                step.metadata = metadata
        return {
            "plan": plan.to_dict(),
            "task_status": status,
            "synthesis_admission": True,
            "replan_exhausted": True,
            "progress": "ready_for_synthesis",
        }

    async def node_synthesize(self, state: dict[str, Any]) -> dict[str, Any]:
        status = dict(state["task_status"])
        status["t_summary"] = "done"
        return {
            "status": "synthesized",
            "progress": "synthesized",
            "task_status": status,
        }

    async def node_replan(self, state: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def node_quality_gate(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"quality_passed": True, "progress": "quality"}

    async def node_finalize(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "completed",
            "final_content": "partial answer",
            "progress": "done",
        }

    async def node_abort(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"status": "aborted", "progress": "abort"}


async def test_early_stop_graph_converges_through_prepare_synthesis() -> None:
    from app.research.runtime.graph import compile_research_graph

    plan = _plan_with_optional_and_synthesis()
    graph = compile_research_graph(runtime=_ConvergenceRuntime(), profile="agent")
    result = await graph.ainvoke(
        _state(
            plan,
            progress_assessment={"verdict": "enough", "reason": "test"},
            evidence_refs=["external:preexisting"],
        ),
        config={"recursion_limit": 30},
    )

    assert result["status"] == "completed"
    assert result["progress"] == "done"
    assert result["quality_passed"] is True
    assert result["task_status"]["t_optional"] == "skipped"
    assert result["task_status"]["t_summary"] == "done"
    assert result["synthesis_admission"] is True


def test_routers_do_not_mutate_pending_optional_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.research.runtime import scheduler
    from app.research.runtime.graph import route_dispatch, route_progress

    def fail_if_called(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise AssertionError("router must not call skip_optional_pending")

    monkeypatch.setattr(scheduler, "skip_optional_pending", fail_if_called)
    state = _state(
        _plan_with_optional_and_synthesis(),
        progress_assessment={"verdict": "enough", "reason": "test"},
    )
    state["task_status"]["t_summary"] = "skipped"

    assert route_dispatch(state) == "progress"
    assert route_progress(state) == "quality_gate"


def test_prepare_synthesis_persists_optional_skip_and_admission() -> None:
    from app.research.runtime.graph import prepare_synthesis_node

    plan = _plan_with_optional_and_synthesis()
    update = prepare_synthesis_node(
        _state(plan, evidence_refs=["external:preexisting"])
    )

    assert update["task_status"]["t_optional"] == "skipped"
    assert update["tasks"]["t_optional"]["status"] == "skipped"
    assert update["synthesis_admission"] is True
    assert "replan_exhausted" not in update
    restored = ExecutionPlan.from_dict(update["plan"])
    assert restored.steps[0].metadata["status"] == "pending"


def test_synthesis_routed_but_not_runnable_fails_closed() -> None:
    from app.research.runtime.graph import GraphInvariantViolation, synthesize_node

    plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_required",
                description="required research",
            ),
            PlanStep(
                step_type="summarize",
                task_id="t_summary",
                description="summary",
                depends_on=["t_required"],
            ),
        ]
    )

    with pytest.raises(GraphInvariantViolation):
        synthesize_node(_state(plan))


def test_admitted_synthesis_allows_failed_research_dependency() -> None:
    from app.research.runtime.graph import synthesize_node

    plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_failed",
                description="required research",
                metadata={"status": "failed"},
            ),
            PlanStep(
                step_type="summarize",
                task_id="t_summary",
                description="summary",
                depends_on=["t_failed"],
            ),
        ]
    )
    state = _state(plan, synthesis_admission=True, replan_exhausted=True)
    update = synthesize_node(state)

    assert update["status"] == "synthesized"
    assert update["task_status"]["t_summary"] == "done"


@pytest.mark.parametrize("status", ["synthesized", "partial", "completed"])
def test_terminal_research_status_never_redispatches(status: str) -> None:
    from app.research.runtime.graph import route_dispatch

    state = _state(_plan_with_optional_and_synthesis(), status=status)

    assert route_dispatch(state) == "quality_gate"


def test_required_artifact_consumer_promotes_optional_producer() -> None:
    discovery = PlanStep(
        step_type="research",
        task_id="t_landscape",
        description="发现候选公司",
        metadata={"optional": True},
    )
    deep_dive = PlanStep(
        step_type="research",
        task_id="t_deep_dive",
        description="深挖候选公司",
        depends_on=["t_landscape"],
    )
    plan = annotate_candidate_dependencies(
        ExecutionPlan(steps=[discovery, deep_dive], summary="artifact plan")
    )

    assert plan.steps[0].metadata["required"] is True
    assert plan.steps[0].metadata["optional"] is False
    assert plan.steps[0].metadata["priority"] == 0
    assert plan.steps[0].metadata["promoted_for_artifact"] == "candidate_set"
    assert validate_artifact_dependencies(plan) == []


def test_missing_artifact_producer_is_rejected() -> None:
    plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                task_id="t_deep_dive",
                description="deep dive",
                metadata={"requires_artifacts": ["candidate_set"]},
            )
        ]
    )

    assert validate_artifact_dependencies(plan) == [
        "missing_artifact_producer:candidate_set:t_deep_dive"
    ]


def test_career_recommendation_fallback_has_quality_floor() -> None:
    query = "你觉的当下国内AI初创有潜力值得加入的公司有哪些？为什么？"
    brief = compile_research_brief(task_query=query)

    assert brief.entities != [query]
    assert brief.entities == ["国内 AI 初创公司"]
    assert brief.freshness == "recent"
    for dimension in (
        "技术实力",
        "团队背景",
        "融资与估值",
        "商业化进展",
        "赛道前景",
        "招聘与人才机会",
        "加入风险",
    ):
        assert dimension in brief.dimensions


def test_stagnant_cycle_guard_forces_synthesis_admission() -> None:
    from app.research.runtime.graph import progress_node

    plan = _plan_with_optional_and_synthesis()
    state = _state(plan)
    updates = []
    for _ in range(3):
        update = progress_node(state)
        updates.append(update)
        state.update(update)

    assert [update["stagnant_cycles"] for update in updates] == [0, 1, 2]
    assert updates[2]["progress_assessment"]["verdict"] == "enough"
    assert updates[2]["progress_assessment"]["reason"] == "graph_no_progress"
    assert updates[2]["replan_exhausted"] is True
    assert updates[2]["synthesis_admission"] is True

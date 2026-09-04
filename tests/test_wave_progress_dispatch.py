"""Wave-based research dispatch: P0 first, Worker → Progress, semantic priority."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.planner import understand_task
from app.agent.harness.state import ExecutionPlan, PlanStep, TaskIntent
from app.agent.harness.research_brief import ResearchBrief
from app.observability.semantic import compute_replan_gap_closure
from app.research.planning.lead_planner import plan_from_lead_payload
from app.research.planning.policy import parse_source_policy
from app.research.planning.priority import stamp_semantic_priority
from app.research.planning.progress import assess_progress
from app.research.runtime.graph import route_dispatch, route_progress
from app.research.runtime.scheduler import (
    annotate_plan_tasks,
    required_retrieval_ids,
    select_dispatch_wave,
    task_status_map,
)
from app.research.runtime.state import empty_research_state


def _ai_coding_plan() -> ExecutionPlan:
    intent = TaskIntent(
        raw_query="AI coding 能不能彻底解决编程？",
        summary="deep research",
        needs_network=True,
        deliverable="md",
        brief=ResearchBrief(
            objective="AI coding 能否彻底解决编程",
            dimensions=[
                "技术能力现状与边界",
                "根本性挑战与局限",
                "发展趋势与未来展望",
                "人类开发者角色与人机协作",
            ],
        ),
    )
    payload = {
        "research_brief": intent.brief.objective,
        "tasks": [
            {
                "task_id": "t_capability",
                "objective": "技术能力现状与边界",
                "allowed_sources": ["web"],
                "coverage_keys": ["技术能力现状与边界"],
                "priority": "P0",
                "required": True,
            },
            {
                "task_id": "t_fundamental",
                "objective": "根本性挑战与局限",
                "allowed_sources": ["web"],
                "coverage_keys": ["根本性挑战与局限"],
                "priority": "P0",
                "required": True,
            },
            {
                "task_id": "t_automation_history",
                "objective": "自动化历史辅助论证",
                "allowed_sources": ["web"],
                "coverage_keys": ["supporting_context"],
                "priority": "P1",
                "required": False,
            },
            {
                "task_id": "t_trends",
                "objective": "发展趋势与未来展望",
                "allowed_sources": ["web"],
                "coverage_keys": ["发展趋势与未来展望"],
                "priority": "P0",
                "required": True,
            },
            {
                "task_id": "t_human_role",
                "objective": "人类开发者角色与人机协作",
                "allowed_sources": ["web"],
                "coverage_keys": ["人类开发者角色与人机协作"],
                "priority": "P0",
                "required": True,
            },
        ],
    }
    policy = parse_source_policy(intent.raw_query)
    plan = plan_from_lead_payload(payload, intent, policy, max_tasks=6)
    assert plan is not None
    return annotate_plan_tasks(plan, intent=intent)


def test_planner_core_dimensions_are_required_not_position_heuristic():
    plan = _ai_coding_plan()
    by_id = {s.task_id: s for s in plan.steps if s.step_type == "research"}
    assert by_id["t_capability"].metadata["required"] is True
    assert by_id["t_fundamental"].metadata["required"] is True
    assert by_id["t_trends"].metadata["required"] is True
    assert by_id["t_human_role"].metadata["required"] is True
    assert by_id["t_automation_history"].metadata["optional"] is True
    required = required_retrieval_ids(plan)
    assert "t_human_role" in required
    assert "t_automation_history" not in required
    print("[OK] semantic required vs optional")


def test_stamp_does_not_use_first_half_heuristic():
    steps = [
        PlanStep(step_type="research", description=f"t{i}", task_id=f"t{i}", objective=f"dim {i}")
        for i in range(5)
    ]
    steps.append(PlanStep(step_type="summarize", description="s", task_id="ts"))
    plan = ExecutionPlan(steps=steps, planning_mode="dynamic")
    stamp_semantic_priority(plan)  # no dimensions → all required
    research = [s for s in plan.steps if s.step_type == "research"]
    assert all(s.metadata.get("required") for s in research)
    print("[OK] no first-half optional fallback")


def test_first_dispatch_is_p0_wave_capped():
    plan = _ai_coding_plan()
    state = empty_research_state(run_id="r1", session_id="s1", task_query="q")
    state["plan"] = plan.to_dict()
    state["task_status"] = task_status_map(plan)
    state["budget"]["max_parallel_workers"] = 3
    routed = route_dispatch(state)
    assert isinstance(routed, list)
    assert 1 <= len(routed) <= 3
    ids = [item.arg["task_id"] for item in routed]
    assert "t_automation_history" not in ids
    assert all(tid.startswith("t_") for tid in ids)
    print("[OK] first wave P0 only, capped", ids)


def test_progress_ignores_pending_optional():
    plan = _ai_coding_plan()
    for step in plan.steps:
        if step.step_type == "research" and step.metadata.get("required"):
            step.metadata["status"] = "done"
        elif step.step_type == "research":
            step.metadata["status"] = "pending"
    rows = []
    for step in plan.steps:
        if step.step_type != "research" or step.metadata.get("optional"):
            continue
        rows.append(
            {
                "task_id": step.task_id,
                "ok": True,
                "summary": f"{step.objective} 2026 已有一手来源与量产证据",
                "payload": {
                    "facts": [f"{step.objective} 2026 进展明确，客户订单充足"],
                    "sources": [f"https://example.com/{step.task_id}"],
                    "confidence": 0.9,
                },
            }
        )
    assessment = assess_progress(
        plan,
        task_status=task_status_map(plan),
        worker_results=rows,
        query="AI coding",
        current_year=2026,
    )
    assert assessment.verdict in {"enough", "gap"}
    assert assessment.verdict != "run", assessment.reason
    print("[OK] progress evaluates without draining optional", assessment.verdict)


def test_route_progress_enough_goes_synthesize_not_optional_dispatch():
    plan = _ai_coding_plan()
    for step in plan.steps:
        if step.step_type == "research" and step.metadata.get("required"):
            step.metadata["status"] = "done"
    state = empty_research_state(run_id="r2", session_id="s2", task_query="q")
    state["plan"] = plan.to_dict()
    state["task_status"] = task_status_map(plan)
    state["progress_assessment"] = {"verdict": "enough", "reason": "coverage_ok"}
    assert route_progress(state) in {"synthesize", "quality_gate"}
    print("[OK] enough → synthesize")


def test_select_wave_never_dumps_all_five():
    plan = _ai_coding_plan()
    wave = select_dispatch_wave(plan, max_parallel=3, include_optional=False)
    assert len(wave) == 3
    ids = [s.task_id for _, s in wave]
    assert "t_automation_history" not in ids
    print("[OK] wave size", ids)


def test_replan_metrics_na_when_not_attempted():
    events = [
        {"type": "worker.completed", "task_id": "t1", "status": "ok"},
    ]
    closure = compute_replan_gap_closure(events)
    assert closure["replan_useful"] is None
    assert closure["gap_closure_rate"] is None
    assert closure["replan_attempted"] is False
    assert closure["progress_attempted"] is False
    print("[OK] replan N/A")


def test_graph_worker_edges_to_progress():
    src = Path("/workspace/app/research/runtime/graph.py").read_text()
    assert 'builder.add_edge("research_worker", "progress")' in src
    assert 'builder.add_edge("research_worker", "dispatch")' not in src
    print("[OK] worker → progress edge")


if __name__ == "__main__":
    test_planner_core_dimensions_are_required_not_position_heuristic()
    test_stamp_does_not_use_first_half_heuristic()
    test_first_dispatch_is_p0_wave_capped()
    test_progress_ignores_pending_optional()
    test_route_progress_enough_goes_synthesize_not_optional_dispatch()
    test_select_wave_never_dumps_all_five()
    test_replan_metrics_na_when_not_attempted()
    test_graph_worker_edges_to_progress()
    print("all wave-progress tests passed")

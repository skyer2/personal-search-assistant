"""隔离 Worker 执行：Send fan-out 时不应持有共享 lock 包住整步。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep, StepResult
from app.agent.harness.run_budget import RunBudgetManager
from app.research.runtime.isolation import (
    IsolatedWorkerOutcome,
    apply_isolated_outcome,
    snapshot_worker_loop_state,
    worker_row,
)


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        summary="parallel",
        steps=[
            PlanStep(step_type="research", description="A", task_id="t_a", metadata={"status": "pending"}),
            PlanStep(step_type="research", description="B", task_id="t_b", metadata={"status": "pending"}),
        ],
        plan_version=1,
        planning_mode="dynamic",
    )


async def test_isolated_workers_overlap_and_merge():
    parent = LoopState(session_id="par")
    parent.plan = _plan()
    started: dict[str, float] = {}
    ended: dict[str, float] = {}
    lock = asyncio.Lock()

    async def run_one(index: int) -> IsolatedWorkerOutcome:
        async with lock:
            child = snapshot_worker_loop_state(parent)
            child.step_index = index
        tid = parent.plan.steps[index].task_id
        started[tid] = time.monotonic()
        await asyncio.sleep(0.25)
        ended[tid] = time.monotonic()
        result = StepResult(
            step_type="research",
            content=f"{tid} done",
            metadata={
                "worker_payload": {
                    "ok": True,
                    "summary": f"{tid} 2026 收入 1亿美元",
                    "facts": [f"{tid} revenue"],
                    "sources": [f"https://{tid}.example"],
                    "confidence": 0.9,
                }
            },
        )
        child.step_results.append(result)
        return IsolatedWorkerOutcome(
            step_index=index,
            task_id=tid,
            ok=True,
            result=result,
            child_state=child,
        )

    wall0 = time.monotonic()
    outcomes = await asyncio.gather(run_one(0), run_one(1))
    elapsed = time.monotonic() - wall0
    assert elapsed < 0.45, f"workers were serialized: elapsed={elapsed:.3f}s"
    assert started["t_a"] < ended["t_b"] and started["t_b"] < ended["t_a"]

    for outcome in sorted(outcomes, key=lambda item: item.step_index):
        apply_isolated_outcome(parent, outcome)
    assert [s.metadata["status"] for s in parent.plan.steps] == ["done", "done"]
    assert len(parent.step_results) == 2
    row = worker_row("t_a", parent.plan.steps[0], True, outcomes[0].result)
    assert row["payload"]["facts"]
    print(f"[OK] isolated overlap elapsed={elapsed:.3f}s facts={row['payload']['facts']}")


def test_snapshot_does_not_alias_parent_plan():
    parent = LoopState(session_id="alias")
    parent.plan = _plan()
    child = snapshot_worker_loop_state(parent)
    child.plan.steps[0].metadata["status"] = "running"
    assert parent.plan.steps[0].metadata.get("status") == "pending"
    print("[OK] child snapshot is isolated")


def test_snapshot_filters_runtime_lock_handles():
    parent = LoopState(session_id="runtime-handle")
    parent.plan = _plan()
    parent.metadata["_run_budget_manager"] = RunBudgetManager()
    parent.metadata["_parallel_child"] = True
    parent.metadata["budget_snapshot"] = {"used_tokens": 1}

    child = snapshot_worker_loop_state(parent)

    assert "_run_budget_manager" not in child.metadata
    assert child.metadata["_parallel_child"] is True
    assert child.metadata["budget_snapshot"] == {"used_tokens": 1}
    assert isinstance(parent.metadata["_run_budget_manager"], RunBudgetManager)
    print("[OK] runtime handles never enter worker snapshots")


if __name__ == "__main__":
    asyncio.run(test_isolated_workers_overlap_and_merge())
    test_snapshot_does_not_alias_parent_plan()
    test_snapshot_filters_runtime_lock_handles()
    print("\n=== Parallelism tests passed ===")

"""
【Phase 6】Trajectory Diff Eval — 轨迹提取与对比

对比实际执行路径与 golden task 期望轨迹（step 序列 + 助手调用）。
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any


def extract_trajectory_from_trace(trace: list[Any]) -> list[str]:
    """从 Harness PhaseEvent 列表提取 execute 步类型序列。"""
    trajectory: list[str] = []
    for event in trace:
        phase = getattr(event, "phase", None) or (event.get("phase") if isinstance(event, dict) else None)
        status = getattr(event, "status", None) or (event.get("status") if isinstance(event, dict) else None)
        data = getattr(event, "data", None) or (event.get("data", {}) if isinstance(event, dict) else {})
        if phase == "execute" and status == "done":
            step_type = data.get("step_type")
            if step_type:
                trajectory.append(str(step_type))
        if phase == "recover" and status == "done":
            trajectory.append("recover")
    return trajectory


def extract_trajectory_dry(planned_steps: list[str], planned_agents: list[str] | None = None) -> list[str]:
    """dry-run 模式：用 planner 产出的 step 序列作为期望轨迹。"""
    traj = list(planned_steps)
    if planned_agents:
        for agent in planned_agents:
            if agent and f"agent:{agent}" not in traj:
                traj.append(f"agent:{agent}")
    return traj


def compare_trajectories(
    actual: list[str],
    expected: list[str],
) -> dict[str, Any]:
    """计算轨迹相似度与差异集合。"""
    if not expected and not actual:
        return {
            "similarity": 1.0,
            "missing_steps": [],
            "extra_steps": [],
            "order_preserved": True,
        }

    matcher = SequenceMatcher(None, actual, expected)
    similarity = round(matcher.ratio(), 3)

    expected_core = [s for s in expected if not s.startswith("agent:")]
    actual_core = [s for s in actual if not s.startswith("agent:")]
    missing = [s for s in expected_core if s not in actual_core]
    extra = [s for s in actual_core if s not in expected_core]

    order_preserved = actual_core == expected_core

    return {
        "similarity": similarity,
        "missing_steps": missing,
        "extra_steps": extra,
        "order_preserved": order_preserved,
        "actual": actual,
        "expected": expected,
    }


def trajectory_passes(
    comparison: dict[str, Any],
    min_similarity: float = 0.6,
    require_order: bool = False,
) -> bool:
    if comparison.get("similarity", 0) < min_similarity:
        return False
    if require_order and not comparison.get("order_preserved"):
        return False
    if comparison.get("missing_steps"):
        return False
    return True

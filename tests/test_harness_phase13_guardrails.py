"""Phase 13: Harness 运行时护栏测试（无需 LLM）。"""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.guardrails import (
    AbortReason,
    GuardrailAction,
    can_replan,
    evaluate_run_guardrails,
)
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep
from app.config.loader import reload_harness_config


def _cfg(**overrides):
    cfg = reload_harness_config()
    ns = SimpleNamespace(**{**cfg.__dict__, **overrides})
    return ns


def test_tool_call_budget():
    state = LoopState(session_id="s1")
    state.tool_calls_count = 20
    decision = evaluate_run_guardrails(
        state,
        _cfg(max_tool_calls=20, max_total_tokens=100000, max_run_sec=600),
        elapsed_sec=1.0,
        estimated_tokens=10,
    )
    assert decision.action == GuardrailAction.DEGRADE
    assert decision.abort is False
    assert decision.reason == AbortReason.BUDGET_TOOL_CALLS
    print("[OK] tool call budget degrades to synthesis")


def test_deadline():
    state = LoopState(session_id="s1")
    decision = evaluate_run_guardrails(
        state,
        _cfg(max_run_sec=10, max_tool_calls=20, max_total_tokens=100000),
        elapsed_sec=10.0,
        estimated_tokens=10,
    )
    assert decision.action == GuardrailAction.DEGRADE
    assert decision.abort is False
    assert decision.reason == AbortReason.DEADLINE
    print("[OK] deadline degrades to synthesis")


def test_max_replan_blocks_can_replan():
    state = LoopState(session_id="s1", replan_count=3, max_retries=2)
    state.plan = ExecutionPlan(steps=[PlanStep(step_type="network_search", description="s")])
    cfg = _cfg(max_replan_count=3, hitl_allow_replan=True, max_plan_steps=12)
    assert can_replan(state, cfg) is False
    print("[OK] max replan")


def test_max_plan_steps():
    state = LoopState(session_id="s1")
    state.plan = ExecutionPlan(
        steps=[PlanStep(step_type="summarize", description=f"s{i}") for i in range(13)]
    )
    decision = evaluate_run_guardrails(
        state,
        _cfg(max_plan_steps=12, max_tool_calls=20, max_total_tokens=100000, max_run_sec=600),
        elapsed_sec=1.0,
        estimated_tokens=1,
    )
    assert decision.action == GuardrailAction.DEGRADE
    assert decision.abort is False
    assert decision.reason == AbortReason.MAX_PLAN_STEPS
    print("[OK] max plan steps degrades to synthesis")


def test_config_phase13():
    cfg = reload_harness_config()
    assert cfg.max_run_sec == 900
    assert cfg.max_replan_count >= 1
    assert cfg.max_plan_steps >= 8
    assert cfg.max_step_tool_calls == 16
    print("[OK] config phase13")


if __name__ == "__main__":
    test_tool_call_budget()
    test_deadline()
    test_max_replan_blocks_can_replan()
    test_max_plan_steps()
    test_config_phase13()
    print("\n=== Phase 13 harness guardrail tests passed ===")

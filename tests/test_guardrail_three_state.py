"""三态资源决策：资源耗尽 → DEGRADE（合成交付），而非 ABORT。"""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.guardrails import (
    AbortReason,
    GuardrailAction,
    evaluate_run_guardrails,
)
from app.agent.harness.run_budget import RunBudgetManager
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep
from app.config.loader import reload_harness_config


def _cfg(**overrides):
    cfg = reload_harness_config()
    return SimpleNamespace(**{**cfg.__dict__, **overrides})


def test_resource_exhaustion_degrades_not_aborts():
    state = LoopState(session_id="s3")
    state.tool_calls_count = 40
    decision = evaluate_run_guardrails(
        state,
        _cfg(max_tool_calls=40, max_total_tokens=100000, max_run_sec=600),
        elapsed_sec=1.0,
        estimated_tokens=10,
    )
    assert decision.action == GuardrailAction.DEGRADE
    assert decision.abort is False
    assert decision.degrade is True
    assert decision.reason == AbortReason.BUDGET_TOOL_CALLS
    assert decision.to_dict()["action"] == "degrade"
    print("[OK] tool exhaustion degrades to synthesis")


def test_deadline_degrades_not_aborts():
    state = LoopState(session_id="s3")
    decision = evaluate_run_guardrails(
        state,
        _cfg(max_run_sec=10, max_tool_calls=40, max_total_tokens=100000),
        elapsed_sec=10.0,
        estimated_tokens=10,
    )
    assert decision.action == GuardrailAction.DEGRADE
    assert decision.abort is False
    print("[OK] deadline degrades to synthesis")


def test_tool_limit_sets_force_synthesis_in_budget_manager():
    mgr = RunBudgetManager(token_limit=1000, tool_call_limit=3, llm_call_limit=10)
    mgr.note_tool_call()
    mgr.note_tool_call()
    assert mgr.force_synthesis() is False
    mgr.note_tool_call()
    assert mgr.force_synthesis() is True
    allowed, reason = mgr.research_allowed()
    assert allowed is False
    assert reason == "budget_tool_calls"
    print("[OK] tool limit forces synthesis in budget manager")


def test_max_replan_degrades():
    state = LoopState(session_id="s3", replan_count=2)
    state.plan = ExecutionPlan(steps=[PlanStep(step_type="network_search", description="s")])
    decision = evaluate_run_guardrails(
        state,
        _cfg(max_replan_count=2, max_tool_calls=40, max_total_tokens=100000, max_run_sec=600),
        elapsed_sec=1.0,
        estimated_tokens=10,
    )
    assert decision.action == GuardrailAction.DEGRADE
    assert decision.reason == AbortReason.MAX_REPLAN
    print("[OK] max replan degrades to synthesis")


if __name__ == "__main__":
    test_resource_exhaustion_degrades_not_aborts()
    test_deadline_degrades_not_aborts()
    test_tool_limit_sets_force_synthesis_in_budget_manager()
    test_max_replan_degrades()
    print("\n=== three-state guardrail tests passed ===")

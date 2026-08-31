"""
Harness Phase 1 单元测试（个人搜索助手，无需 LLM API）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.harness.compressor import ContextCompressor
from app.agent.harness.planner import build_plan, understand_task
from app.agent.harness.recovery import RecoveryManager
from app.agent.harness.state import LoopState, Phase
from app.agent.harness.validator import ResultValidator


def test_imports():
    from app.agent.harness import AgentHarness, HarnessResult, Phase as P
    from app.agent.main_agent import harness, run_deep_agent

    assert AgentHarness is not None
    assert harness is not None
    assert callable(run_deep_agent)
    assert P.UNDERSTAND.value == "understand"
    print("[OK] imports")


def test_understand_and_plan():
    intent = understand_task("搜索2026年AI趋势，生成PDF报告")
    assert intent.needs_network
    assert intent.deliverable == "pdf"

    plan = build_plan(intent)
    step_types = [s.step_type for s in plan.steps]
    assert "network_search" in step_types
    assert "generate_markdown" in step_types
    assert "convert_pdf" in step_types
    print(f"[OK] plan: {plan.summary}")


def test_chat_default_deliverable():
    intent = understand_task("列出 5 条 AI 趋势，附来源链接")
    assert intent.deliverable == "text"
    plan = build_plan(intent)
    assert plan.steps[-1].step_type == "summarize"
    print("[OK] chat default deliverable")


def test_validator_step_and_finalize():
    validator = ResultValidator()
    session_dir = Path(__file__).resolve().parents[1] / "output" / "test_harness_val"
    session_dir.mkdir(parents=True, exist_ok=True)

    state = LoopState(session_id="test")
    state.intent = understand_task("搜索AI新闻")
    state.final_content = "这是关于AI趋势的详细研究报告，包含多个来源。"
    state.assistants_called = ["网络搜索助手"]

    outcome = validator.validate_finalize(state, session_dir)
    assert outcome.passed, f"expected pass, got {outcome.reason}"
    print("[OK] validator finalize pass case")

    from app.agent.harness.state import PlanStep, StepResult

    step = PlanStep(step_type="network_search", description="搜索", subagent="网络搜索助手")
    result = StepResult(step_type="network_search", content="x" * 200)
    state.assistants_called = ["网络搜索助手"]
    step_outcome = validator.validate_step(step, result, session_dir, state)
    assert step_outcome.passed
    print("[OK] validator step pass case")


def test_recovery_hint():
    recovery = RecoveryManager()
    state = LoopState(session_id="test")
    state.plan = build_plan(understand_task("搜索 AI 新闻"))
    hint = recovery.build_recovery_hint("search_too_short", state)
    assert "搜索" in hint
    print(f"[OK] recovery hint: {hint[:80]}...")


def test_compressor():
    compressor = ContextCompressor()
    short = "短文本"
    assert compressor.compress_sync(short) == short
    long = "x" * 3000
    compressed = compressor.compress_sync(long, "network_search")
    assert len(compressed) < len(long)
    print("[OK] compressor")


def test_context_builder_tools():
    from app.agent.harness.context_builder import ContextBuilder

    builder = ContextBuilder()
    tool_ctx = builder.build_tool_context("network_search")
    assert "internet_search" in tool_ctx
    print("[OK] context builder tools")


if __name__ == "__main__":
    test_imports()
    test_understand_and_plan()
    test_chat_default_deliverable()
    test_validator_step_and_finalize()
    test_recovery_hint()
    test_compressor()
    test_context_builder_tools()
    print("\n=== Phase 1 tests passed ===")

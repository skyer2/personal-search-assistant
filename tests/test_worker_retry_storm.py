"""研究步空转修复：白名单、抽取、no_error、步内预算（无需 LLM）。"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.artifacts import ArtifactStore, reset_artifact_store, set_artifact_store
from app.agent.harness.loop import AgentHarness
from app.agent.harness.orchestration import (
    build_strict_json_retry_instruction,
    check_unauthorized_tools,
    extract_last_assistant_text,
    salvage_payload_from_artifacts,
    parse_worker_payload,
)
from app.agent.harness.state import PlanStep, StepResult
from app.agent.harness.step_budget import (
    STOP_JSON_MESSAGE,
    consume_retrieval_or_block,
    retrieval_budget,
)
from app.agent.harness.validator import ResultValidator
from app.research.planning.policy import SOURCE_TOOLS, tools_for_sources
from app.research.planning.validator import _covers_source
from app.agent.harness.state import ExecutionPlan
from app.config.loader import reload_harness_config


def test_web_allowlist_includes_context_tools():
    tools = tools_for_sources(["web"])
    assert "internet_search" in tools
    assert "fetch_url" in tools
    assert "read_artifact" in tools
    assert "read_evidence" in tools
    assert set(SOURCE_TOOLS["web"]) == {"internet_search", "fetch_url"}
    print("[OK] web allowlist includes JIT context tools")


def test_covers_source_ignores_context_tools():
    plan = ExecutionPlan(
        steps=[
            PlanStep(
                step_type="research",
                description="web only",
                allowed_tools=tools_for_sources(["web"]),
            )
        ]
    )
    assert _covers_source(plan, "web") is True
    assert _covers_source(plan, "file") is False
    print("[OK] context tools do not cover file source")


def test_context_tools_never_unauthorized():
    step = PlanStep(
        step_type="research",
        description="搜",
        allowed_tools=["internet_search", "fetch_url"],
    )
    ok, bad = check_unauthorized_tools(
        step, ["internet_search", "read_artifact", "read_evidence"], enforce=True
    )
    assert ok is True and not bad
    ok2, bad2 = check_unauthorized_tools(step, ["generate_markdown"], enforce=True)
    assert ok2 is False and "generate_markdown" in bad2
    print("[OK] context tools always allowed")


def test_extract_skips_tool_message():
    tool_msg = SimpleNamespace(type="tool", content="Tavily: Agent 失败模式...")
    ai_json = SimpleNamespace(
        type="ai",
        content=json.dumps(
            {"ok": True, "summary": "三种架构", "facts": ["ReAct"], "sources": ["https://e.com"]},
            ensure_ascii=False,
        ),
        tool_calls=None,
    )
    text = extract_last_assistant_text([ai_json, tool_msg])
    assert "三种架构" in text
    assert "Tavily" not in text
    print("[OK] extract last AIMessage not ToolMessage")


def test_salvage_from_artifacts():
    store = ArtifactStore()
    set_artifact_store(store)
    try:
        store.put(
            "ReAct 是一种循环架构",
            kind="web",
            locator="https://example.com/react",
            title="ReAct",
            step_index=0,
            step_type="research",
        )
        empty = parse_worker_payload("Tavily snippet 没有 JSON", step_type="research")
        assert not empty.facts
        salvaged = salvage_payload_from_artifacts(empty, step_index=0)
        assert salvaged.ok is True
        assert salvaged.facts
        assert any("example.com" in s for s in salvaged.sources)
        print("[OK] salvage payload from artifacts")
    finally:
        reset_artifact_store()


def test_no_error_ignores_research_prose():
    validator = ResultValidator()
    session_dir = ROOT / "output" / "test_retry_storm"
    session_dir.mkdir(parents=True, exist_ok=True)
    from app.agent.harness.planner import understand_task
    from app.agent.harness.state import LoopState

    state = LoopState(session_id="t")
    state.intent = understand_task("搜索 AI 新闻")
    state.assistants_called = ["网络搜索助手"]
    step = PlanStep(step_type="network_search", description="搜索", subagent="网络搜索助手")
    result = StepResult(
        step_type="network_search",
        content="x" * 80,
        compressed_content="本文讨论 Agent 的失败模式、错误处理与异常恢复策略。" + "详" * 120,
    )
    outcome = validator.validate_step(step, result, session_dir, state)
    assert outcome.passed, outcome.reason

    timeout = StepResult(
        step_type="network_search",
        content="步骤执行超时",
        compressed_content="步骤执行超时",
        metadata={"step_timeout": True, "step_assistants_called": ["网络搜索助手"]},
    )
    timed = validator.validate_step(step, timeout, session_dir, state)
    assert not timed.passed and timed.reason == "step_timeout"
    print("[OK] no_error does not false-positive on 失败模式")


def test_json_retry_forbids_research():
    step = PlanStep(step_type="research", description="d", subagent="研究工人")
    hint = build_strict_json_retry_instruction(step)
    assert "禁止" in hint
    assert "internet_search" in hint
    print("[OK] json retry forbids re-search")


def test_step_retrieval_budget_blocks():
    with retrieval_budget(0):
        msg = consume_retrieval_or_block("internet_search")
        assert msg == STOP_JSON_MESSAGE
    with retrieval_budget(1):
        assert consume_retrieval_or_block("internet_search") is None
        assert consume_retrieval_or_block("fetch_url") == STOP_JSON_MESSAGE
    assert consume_retrieval_or_block("internet_search") is None
    print("[OK] step retrieval budget")


def test_worker_summary_skips_llm_compress():
    result = StepResult(
        step_type="research",
        content="{" + "x" * 500 + "}",
        metadata={
            "structured_ok": True,
            "worker_payload": {
                "ok": True,
                "summary": "对比了 ReAct、Plan-Execute、Multi-Agent 三种架构的调度方式。",
                "facts": ["ReAct 循环调用工具", "Plan-Execute 先规划再执行"],
                "sources": ["https://example.com/a", "https://example.com/b"],
            },
        },
    )
    text = AgentHarness._compressed_from_worker_payload(result)
    assert "ReAct" in text
    assert len(text) >= 80
    print("[OK] worker summary used as compress")


def test_config_step_tool_budget():
    cfg = reload_harness_config()
    assert cfg.max_step_tool_calls == 8
    assert cfg.max_tool_calls >= 40
    print("[OK] max_step_tool_calls loaded")


if __name__ == "__main__":
    test_web_allowlist_includes_context_tools()
    test_covers_source_ignores_context_tools()
    test_context_tools_never_unauthorized()
    test_extract_skips_tool_message()
    test_salvage_from_artifacts()
    test_no_error_ignores_research_prose()
    test_json_retry_forbids_research()
    test_step_retrieval_budget_blocks()
    test_worker_summary_skips_llm_compress()
    test_config_step_tool_budget()
    print("\n=== retry storm tests passed ===")

"""Phase 8: 混合 Planner + 结构化整合 + 证据 digest 测试（无需 LLM）。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.context_builder import ContextBuilder
from app.agent.harness.orchestration import (
    aggregate_evidence_digest,
    format_evidence_digest_for_prompt,
    parse_worker_payload,
    validate_structured_worker_payload,
)
from app.agent.harness.planner import _looks_like_report_request, understand_task
from app.agent.harness.planner_llm import merge_intent
from app.agent.harness.state import LoopState, PlanStep, StepResult, TaskIntent
from app.config.loader import reload_harness_config


def test_deliverable_rules_improved():
    intent1 = understand_task("帮我查一下机器人行业")
    assert intent1.deliverable == "text"

    intent2 = understand_task("整理一份机器人行业研究报告")
    assert intent2.deliverable == "md"

    intent3 = understand_task("生成 Markdown 文件")
    assert intent3.deliverable == "md"

    assert _looks_like_report_request("整理机器人行业研究报告")
    assert not _looks_like_report_request("生成一下答案")
    print("[OK] deliverable rules phase8")


def test_merge_intent_llm_patch():
    rule = understand_task("查数据库库存")
    patch = {
        "needs_network": True,
        "needs_database": True,
        "needs_knowledge_base": False,
        "needs_file_read": False,
        "deliverable": "md",
        "confidence": 0.9,
        "reason": "需要补充公开资料并写报告",
    }
    merged = merge_intent(rule, patch, min_confidence=0.5)
    assert merged.needs_network is True
    assert merged.deliverable == "md"
    assert merged.planner_source == "rules+llm"
    print("[OK] merge_intent")


def test_structured_validation():
    step = PlanStep(step_type="network_search", description="s", subagent="网络搜索助手")
    prose = parse_worker_payload("这是一段散文，没有 JSON", step_type="network_search")
    ok, reason = validate_structured_worker_payload(prose, step, require_json=True)
    assert not ok and reason == "invalid_structured_output"

    structured = parse_worker_payload(
        json.dumps(
            {
                "ok": True,
                "summary": "行业增速15%",
                "facts": ["2025年增速约15%"],
                "sources": ["https://example.com"],
                "worker": "网络搜索助手",
                "step_type": "network_search",
            }
        ),
        step_type="network_search",
    )
    ok2, _ = validate_structured_worker_payload(structured, step, require_json=True)
    assert ok2
    print("[OK] structured validation")


def test_evidence_digest_for_synthesis():
    results = [
        StepResult(
            step_type="network_search",
            content="raw",
            metadata={
                "worker_payload": {
                    "summary": "公开资料结论",
                    "facts": ["机器人市场扩大"],
                    "sources": ["https://a.com"],
                    "confidence": 0.9,
                }
            },
        ),
        StepResult(
            step_type="database_query",
            content="raw",
            metadata={
                "worker_payload": {
                    "summary": "库存数据",
                    "facts": ["库存1000台"],
                    "sources": ["sales_table"],
                    "confidence": 0.95,
                }
            },
        ),
    ]
    digest = aggregate_evidence_digest(results)
    assert len(digest["all_facts"]) == 2
    text = format_evidence_digest_for_prompt(digest)
    assert "机器人市场扩大" in text
    assert "sales_table" in text

    builder = ContextBuilder()
    state = LoopState(session_id="t")
    state.step_results = results
    ctx = builder.build_prior_results_context(
        state,
        current_step_type="generate_markdown",
        use_evidence_digest=True,
    )
    assert "证据_digest" in ctx
    assert "库存1000台" in ctx
    print("[OK] evidence digest for synthesis")


def test_phase8_config():
    reload_harness_config()
    from app.config.loader import get_harness_config

    cfg = get_harness_config()
    assert cfg.structured_output_retry is True
    assert cfg.synthesis_use_evidence_digest is True
    assert cfg.planner_llm_confirm_enabled is True
    print("[OK] phase8 config defaults")


if __name__ == "__main__":
    test_deliverable_rules_improved()
    test_merge_intent_llm_patch()
    test_structured_validation()
    test_evidence_digest_for_synthesis()
    test_phase8_config()
    print("\n=== Phase 8 tests passed ===")

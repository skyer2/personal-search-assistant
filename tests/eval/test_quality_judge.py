"""Quality Judge：LLM JSON path + reference grader. Never llm_stub."""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.eval.graders.llm_judge import (
    grade_against_reference,
    judge_answer_quality,
    parse_judge_json,
)


def test_parse_judge_json_and_pass_label():
    parsed = parse_judge_json(
        '{"correctness": 0.9, "completeness": 0.8, "grounding": 0.7, "unsupported_claims": [], "critical_error": false}'
    )
    assert parsed.judge_source == "llm"
    assert parsed.pass_label == "pass"
    failed = parse_judge_json('prefix {"correctness": 0.1, "critical_error": true} trailing')
    assert failed.pass_label == "fail"
    print("[OK] parse_judge_json")


def test_disabled_judge_is_not_a_score():
    result = asyncio.run(judge_answer_quality(question="q", answer="a", enabled=False))
    assert result.judge_source == "disabled"
    assert result.correctness is None
    print("[OK] disabled judge")


def test_reference_grader_pass_and_fail():
    passed = grade_against_reference(
        question="Unitree 收入？",
        answer="来源冲突：A 称 10 亿美元，B 称 50 亿美元，不能直接下结论。",
        evidence="来源 A：10 亿美元。来源 B：50 亿美元。",
        reference="来源冲突，不能直接采信单一数字。",
        must_include=["冲突"],
    )
    assert passed.judge_source == "reference_grader"
    assert passed.pass_label == "pass"
    failed = grade_against_reference(
        question="Unitree 收入？",
        answer="确定是 50 亿美元，没有争议。",
        evidence="来源 A：10 亿美元。来源 B：50 亿美元。",
        reference="来源冲突，不能直接采信单一数字。",
        must_include=["冲突"],
    )
    assert failed.pass_label == "fail"
    print("[OK] reference grader")


def test_llm_judge_uses_injected_completer():
    async def fake_complete(_prompt: str) -> str:
        return '{"correctness": 0.85, "completeness": 0.7, "grounding": 0.9, "unsupported_claims": [], "critical_error": false, "rationale": "ok"}'

    result = asyncio.run(
        judge_answer_quality(
            question="q",
            answer="a",
            enabled=True,
            complete=fake_complete,
        )
    )
    assert result.judge_source == "llm"
    assert result.correctness == 0.85
    assert result.pass_label == "pass"
    print("[OK] injected llm judge")


def test_llm_failure_falls_back_to_reference():
    async def boom(_prompt: str) -> str:
        raise RuntimeError("no model")

    result = asyncio.run(
        judge_answer_quality(
            question="q",
            answer="纠错与逻辑比特扩展",
            reference="纠错与逻辑比特扩展，尚未通用容错。",
            must_include=["纠错"],
            enabled=True,
            complete=boom,
        )
    )
    assert result.judge_source == "reference_grader"
    assert result.pass_label == "pass"
    print("[OK] fallback reference grader")


def test_no_stub_source():
    async def boom(_prompt: str) -> str:
        raise RuntimeError("x")

    result = asyncio.run(judge_answer_quality(question="q", answer="a", enabled=True, complete=boom))
    assert result.judge_source == "unavailable"
    print("[OK] no stub")


if __name__ == "__main__":
    test_parse_judge_json_and_pass_label()
    test_disabled_judge_is_not_a_score()
    test_reference_grader_pass_and_fail()
    test_llm_judge_uses_injected_completer()
    test_llm_failure_falls_back_to_reference()
    test_no_stub_source()
    print("=== quality judge tests passed ===")

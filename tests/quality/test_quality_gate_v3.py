from __future__ import annotations

from pathlib import Path

import pytest

from app.research.quality.gate import evaluate_report_quality


ROOT = Path(__file__).parent / "bad_outputs"


def _evidence(*, source_type: str = "primary") -> list[dict[str, object]]:
    return [{
        "evidence_id": "E1",
        "source_id": "official.example.com",
        "locator": "https://official.example.com/news",
        "source_type": source_type,
        "source_tier": "PRIMARY" if source_type == "primary" else "SECONDARY",
        "authority_score": 0.9 if source_type == "primary" else 0.25,
    }]


@pytest.mark.parametrize("fixture", sorted(ROOT.glob("*.md")), ids=lambda path: path.stem)
def test_curated_bad_outputs_never_pass(fixture: Path) -> None:
    evidence = _evidence(source_type="community" if fixture.stem == "weak_sources" else "primary")
    result = evaluate_report_quality(
        content=fixture.read_text(encoding="utf-8"),
        brief={"key_questions": ["当前热点是什么？", "未来方向是什么？"], "user_intent": "trend_forecast"},
        evidence_records=evidence,
        answer_contract={"answers": [{"question_id": "q1"}, {"question_id": "q2"}]},
    )
    assert result.verdict != "PASS"


def test_weak_discovery_sources_are_a_warning_unless_primary_sources_were_required() -> None:
    content = "# 结论摘要\n\nAgent 已形成可核验的变化信号。[1]"
    evidence = _evidence(source_type="secondary")

    ordinary = evaluate_report_quality(
        content=content,
        brief={"key_questions": ["当前热点是什么？"], "source_requirements": {"primary_required": False}},
        evidence_records=evidence,
        answer_contract={"answers": [{"question_id": "q1", "direct_answer": "Agent 已形成可核验的变化信号。"}]},
    )
    primary = evaluate_report_quality(
        content=content,
        brief={"key_questions": ["当前热点是什么？"], "source_requirements": {"primary_required": True}},
        evidence_records=evidence,
        answer_contract={"answers": [{"question_id": "q1", "direct_answer": "Agent 已形成可核验的变化信号。"}]},
    )

    assert "weak_sources" not in ordinary.issues
    assert ordinary.metrics["source_quality_warning"] is True
    assert "weak_sources" in primary.issues


def test_report_labels_and_reference_binding_explanations_are_not_broken_or_duplicate_claims() -> None:
    content = (
        "# 结论摘要\n\n- Agent 已形成可核验的变化信号。[1]\n\n"
        "# 当前热点\n\n**核心判断**：Agent 已形成可核验的变化信号。[1]\n\n"
        "**主要依据**：\n- [1] example.com：为该判断提供已登记的直接或支持性证据。\n\n"
        "# 参考来源\n\n- [1] example.com：与正文 Claim–Evidence 映射对应。"
    )
    result = evaluate_report_quality(
        content=content,
        brief={"key_questions": ["当前热点是什么？"], "source_requirements": {"primary_required": False}},
        evidence_records=_evidence(source_type="secondary"),
        answer_contract={"answers": [{"question_id": "q1", "direct_answer": "Agent 已形成可核验的变化信号。"}]},
    )

    assert "broken_sentence" not in result.issues
    assert "duplicate_claims" not in result.issues


def test_good_report_with_semantic_forecast_contract_passes() -> None:
    result = evaluate_report_quality(
        content=(
            "# 结论摘要\nAgent runtime 是当前重点。[1]\n\n"
            "# 未来 1~2 年方向\n未来两年可能成为部署核心，因为工具协议与评测需求正在驱动落地。[1]"
            "可观察里程碑是企业将其纳入生产工作流；不确定性在于成本和可靠性。"
        ),
        brief={"key_questions": ["未来方向是什么？"], "user_intent": "trend_forecast"},
        evidence_records=_evidence(),
        answer_contract={"answers": [{"question_id": "q1", "direct_answer": "未来方向是 runtime 化"}]},
    )
    assert result.verdict == "PASS"

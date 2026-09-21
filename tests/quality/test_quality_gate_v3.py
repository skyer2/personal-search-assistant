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


def test_good_report_with_semantic_forecast_contract_passes() -> None:
    result = evaluate_report_quality(
        content=(
            "## 直接回答\nAgent runtime 是当前重点。[1]\n\n"
            "## 未来方向\n未来两年可能成为部署核心，因为工具协议与评测需求正在驱动落地。[1]"
            "可观察里程碑是企业将其纳入生产工作流；不确定性在于成本和可靠性。"
        ),
        brief={"key_questions": ["未来方向是什么？"], "user_intent": "trend_forecast"},
        evidence_records=_evidence(),
        answer_contract={"answers": [{"question_id": "q1", "direct_answer": "未来方向是 runtime 化"}]},
    )
    assert result.verdict == "PASS"

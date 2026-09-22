"""v5 delivery contract: findings are normalized before report writing."""

from __future__ import annotations

from types import SimpleNamespace

from app.research.delivery.insight_synthesis import build_insight_synthesis, render_deterministic_insight_report
from app.research.execution.synthesis_executor import SynthesisExecutor, SynthesisRequest
from app.research.quality.gate import evaluate_report_quality
from app.research.runtime.worker import ResearchContext


def _records(count: int = 20) -> list[dict[str, object]]:
    return [
        {
            "evidence_id": f"E{index}",
            "locator": f"https://official{index}.example.com/report",
            "title": f"Official research update {index}",
            "published_at": "2026-09-01",
            "source_type": "primary" if index % 3 == 0 else "authoritative_secondary",
            "authority_score": 0.9 if index % 3 == 0 else 0.8,
            "directness_score": 0.9,
            "freshness_score": 0.9,
            "independence_score": 1.0,
        }
        for index in range(1, count + 1)
    ]


def _findings(count: int = 20) -> list[dict[str, object]]:
    topics = ["企业 ROI 与工作流", "MCP 互操作协议", "可靠性评测与治理", "长期运行时与 memory", "Agent 产品能力"]
    return [
        {
            "finding_id": f"F{index}",
            "claim": f"{topics[(index - 1) % len(topics)]} 的第 {index} 条已验证变化。",
            "evidence_ids": [f"E{index}"],
            "supported_criteria": [f"q{(index - 1) % 3 + 1}"],
            "confidence": 0.8,
            "claim_type": "forecast" if index in {4, 9} else "fact",
        }
        for index in range(1, count + 1)
    ]


def test_twenty_findings_become_bounded_signals_and_writer_never_sees_snippets() -> None:
    synthesis = build_insight_synthesis(
        findings=_findings(), evidence_records=_records(), citation_numbers={f"E{i}": i for i in range(1, 21)},
    )
    assert 3 <= len(synthesis.signals) <= 6
    assert synthesis.metrics["claims_before_dedup"] == 20
    assert synthesis.metrics["duplicate_claim_ratio"] <= 0.05
    assert all(card.mechanism and card.support_refs for card in synthesis.insight_cards)

    request = SynthesisRequest(
        mode="normal",
        findings=[{"claim": "RAW_SNIPPET_SHOULD_NOT_CROSS_WRITER_BOUNDARY", "evidence_ids": ["E1"]}],
        evidence_digests=[],
        **synthesis.writer_payload(),
    )
    prompt = SynthesisExecutor(SimpleNamespace(), SimpleNamespace())._prompt(
        request, ResearchContext(run_id="r", query="研究 Agent", session_id="s")
    )
    assert "RAW_SNIPPET_SHOULD_NOT_CROSS_WRITER_BOUNDARY" not in prompt
    assert "结构化洞察卡" in prompt
    assert "证据摘录" not in prompt


def test_binding_renders_top_three_but_retains_the_full_binding() -> None:
    finding = [{"finding_id": "F1", "claim": "企业采用方向正在收敛。", "evidence_ids": ["E1", "E2", "E3", "E4", "E5"], "confidence": 0.9}]
    synthesis = build_insight_synthesis(
        findings=finding, evidence_records=_records(5), citation_numbers={f"E{i}": i for i in range(1, 6)},
    )
    binding = synthesis.bindings[0]
    assert len(binding.primary_evidence_refs) + len(binding.supporting_evidence_refs) == 5
    assert len(binding.display_evidence) <= 4


def test_deterministic_report_has_resolvable_source_markers_without_raw_urls() -> None:
    synthesis = build_insight_synthesis(
        findings=_findings(5), evidence_records=_records(5),
        citation_numbers={f"E{i}": i for i in range(1, 6)},
    )

    report = render_deterministic_insight_report(synthesis=synthesis, objective="研究 Agent 热点")

    assert "# 参考来源" in report
    assert "[1]" in report
    assert "https://" not in report


def test_article_shaped_salvage_claim_is_replaced_by_an_honest_signal() -> None:
    synthesis = build_insight_synthesis(
        findings=[{
            "finding_id": "F1", "claim": "该图片使用了AI生成技术 文|记者 Agent 正在成为热点。",
            "evidence_ids": ["E1"], "confidence": 0.8,
        }],
        evidence_records=_records(1), citation_numbers={"E1": 1},
    )

    report = render_deterministic_insight_report(synthesis=synthesis, objective="研究 Agent 热点")

    assert "该图片" not in report
    assert "文|" not in report


def test_secondary_excerpt_is_not_promoted_to_a_substantive_report_claim() -> None:
    synthesis = build_insight_synthesis(
        findings=[{
            "finding_id": "F1", "claim": "某转载站称 Agent 将彻底改变所有企业工作流。",
            "evidence_ids": ["E1"], "confidence": 0.8,
        }],
        evidence_records=[{
            "evidence_id": "E1", "locator": "https://secondary.example/report",
            "source_type": "secondary", "authority_score": 0.55,
        }], citation_numbers={"E1": 1},
    )

    report = render_deterministic_insight_report(synthesis=synthesis, objective="研究 Agent 热点")

    assert "彻底改变所有企业" not in report
    assert "值得持续跟踪的变化信号" in report
    report = render_deterministic_insight_report(synthesis=synthesis, objective="研究 Agent")
    assert "# 结论摘要" in report
    assert "# 综合判断" in report
    assert "https://" not in report


def test_secondary_named_original_source_is_queued_or_reconciled_without_new_worker() -> None:
    synthesis = build_insight_synthesis(
        findings=[{"finding_id": "F1", "claim": "Gartner predicts enterprise Agent adoption will rise.", "evidence_ids": ["E1"], "confidence": 0.7}],
        evidence_records=[{"evidence_id": "E1", "locator": "https://secondary.example/report", "source_type": "secondary"}],
    )
    assert len(synthesis.source_upgrades) == 1
    upgrade = synthesis.source_upgrades[0]
    assert upgrade.old_source_type == "secondary"
    assert upgrade.search_hints[0].startswith("site:gartner.com")
    assert upgrade.failure_reason == "queued_for_existing_primary_source_lane"


def test_quality_gate_rejects_raw_snippet_and_repairs_summary_duplication() -> None:
    evidence = [{"evidence_id": "E1", "source_type": "primary", "source_tier": "PRIMARY", "authority_score": 0.9}]
    raw = evaluate_report_quality(
        content="# 结论摘要\n\nAI Agent市场前景与发展趋势分析 根据多家权威机构。\n",
        brief={"key_questions": ["当前热点是什么？"]}, evidence_records=evidence,
        answer_contract={"answers": [{"question_id": "q1", "direct_answer": "有结论"}]},
    )
    assert raw.verdict == "FAIL"
    raw_url = evaluate_report_quality(
        content="# 结论摘要\n\n结论来自 https://example.com/raw-search-result。\n",
        brief={"key_questions": ["当前热点是什么？"]}, evidence_records=evidence,
        answer_contract={"answers": [{"question_id": "q1", "direct_answer": "有结论"}]},
    )
    assert raw_url.verdict == "FAIL"
    duplicate = evaluate_report_quality(
        content="# 结论摘要\n\n企业正在从 PoC 转向业务结果。[1]\n\n# 当前热点\n\n企业正在从 PoC 转向业务结果。[1]",
        brief={"key_questions": ["当前热点是什么？"]}, evidence_records=evidence,
        answer_contract={"answers": [{"question_id": "q1", "direct_answer": "有结论"}]},
    )
    assert duplicate.verdict == "REPAIRABLE"
    assert "summary_detail_overlap" in duplicate.issues


def test_forecast_cards_always_include_milestone_and_uncertainty() -> None:
    synthesis = build_insight_synthesis(findings=_findings(), evidence_records=_records())
    assert synthesis.forecast_cards
    assert all(card.mechanism and card.observable_milestone and card.uncertainty for card in synthesis.forecast_cards)

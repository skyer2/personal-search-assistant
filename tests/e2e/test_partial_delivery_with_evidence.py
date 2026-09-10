from __future__ import annotations

from app.research.runtime.graph import finalize_node, quality_gate_node, synthesize_node
from app.research.runtime.state import empty_research_state


QUERY = "你觉得当下国内 AI 初创有潜力值得加入的公司有哪些？为什么？"


def test_partial_delivery_keeps_usable_evidence_and_discloses_limits():
    state = empty_research_state(run_id="e2e-partial", session_id="s", task_query=QUERY)
    state.update(
        {
            "phase": "coverage_judge",
            "control_decision": {"action": "deliver_partial"},
            "coverage_judgement": {
                "sufficient": False,
                "status": "gap",
                "missing": ["技术路线与产品"],
            },
            "evidence_assessment": {"status": "partial", "evidence_count": 1},
            "evidence_records": [
                {
                    "evidence_id": "ev_partial",
                    "source_id": "example.com",
                    "locator": "https://example.com/ai-startup",
                    "authority_score": 0.8,
                }
            ],
            "claims": [
                {
                    "claim_id": "claim_partial",
                    "text": "月之暗面具有可验证的 AI 初创证据。",
                    "evidence_ids": ["ev_partial"],
                    "confidence": 0.82,
                }
            ],
            "findings": [
                {
                    "finding_id": "finding_partial",
                    "task_id": "t_discovery",
                    "summary": "月之暗面具有可验证的 AI 初创证据。",
                    "claims": ["月之暗面具有可验证的 AI 初创证据。"],
                    "evidence_ids": ["ev_partial"],
                }
            ],
            "worker_results": [
                {
                    "task_id": "t_discovery",
                    "ok": False,
                    "status": "partial",
                    "summary": "worker_timeout; recovered evidence",
                }
            ],
        }
    )

    state.update(synthesize_node(state))
    content = state["final_content"]
    assert "月之暗面具有可验证的 AI 初创证据。" in content
    assert "https://example.com/ai-startup" in content
    assert "技术路线与产品" in content
    assert "ev_partial" not in content
    assert "finding_partial" not in content
    assert "部分交付" in content
    assert "不能视为完整成功" in content

    state.update(quality_gate_node(state))
    assert "coverage_gap" in state["quality_assessment"]["issues"]
    state.update(finalize_node(state))
    assert state["final_content"].strip()
    assert state["termination"]["outcome"] == "partial"
    assert state["termination"]["research_completed"] is False
    assert state["termination"]["synthesis_attempted"] is True
    assert state["termination"]["quality_attempted"] is True

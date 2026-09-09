from __future__ import annotations

from app.research.runtime.graph import finalize_node, quality_gate_node, synthesize_node
from app.research.runtime.state import empty_research_state
from app.research.spec.compiler import compile_research_spec


QUERY = "你觉得当下国内 AI 初创有潜力值得加入的公司有哪些？为什么？"


def test_partial_delivery_keeps_usable_evidence_and_discloses_limits():
    spec = compile_research_spec(QUERY)
    state = empty_research_state(run_id="e2e-partial", session_id="s", task_query=QUERY)
    state.update(
        {
            "phase": "assess",
            "research_spec": spec.to_dict(),
            "control_decision": {"action": "deliver_partial"},
            "coverage_state": {
                "coverage_ratio": 0.4,
                "missing_ids": ["coverage_technology"],
                "partial_ids": ["coverage_funding"],
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
    assert "ev_partial" in content
    assert "Uncovered coverage unit: coverage_technology" in content
    assert "部分交付" in content
    assert "不能视为完整成功" in content

    state.update(quality_gate_node(state))
    assert "coverage_gate_failed" in state["quality_assessment"]["issues"]
    state.update(finalize_node(state))
    assert state["final_content"].strip()
    assert state["termination"]["outcome"] == "partial"
    assert state["termination"]["research_completed"] is False
    assert state["termination"]["synthesis_attempted"] is True
    assert state["termination"]["quality_attempted"] is True

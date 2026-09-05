"""Evidence-driven marginal gain stop：连续零增益即使预算充足也停止研究。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.research.planning.marginal_gain import (
    MarginalGainState,
    evaluate_marginal_gain,
    record_wave_gain,
)


def _row(task_id, *, evidence=None, sources=None, facts=None, ok=True):
    return {
        "task_id": task_id,
        "ok": ok,
        "payload": {
            "evidence_ids": list(evidence or []),
            "sources": list(sources or []),
            "facts": list(facts or []),
        },
    }


def test_zero_gain_window_stops():
    state = MarginalGainState()
    g1 = record_wave_gain(state, [_row("t1", evidence=["e1"], sources=["s1"], facts=["f1"])])
    assert g1.total_gain == 3
    assert evaluate_marginal_gain(state).stop is False

    # 单波零增益还不够（window=2）
    g2 = record_wave_gain(state, [_row("t2", evidence=["e1"], sources=["s1"], facts=["f1"])])
    assert g2.total_gain == 0
    # 窗口内连续两波零增益 → 停止研究
    record_wave_gain(state, [_row("t3", evidence=["e1"], sources=["s1"], facts=["f1"])])
    decision = evaluate_marginal_gain(state)
    assert decision.stop is True
    assert decision.reason == "marginal_gain_low"
    assert decision.metrics["window_gain"] == 0
    print("[OK] zero marginal gain stops research")


def test_new_evidence_prevents_stop():
    state = MarginalGainState()
    record_wave_gain(state, [_row("t1", evidence=["e1"])])
    record_wave_gain(state, [_row("t2", evidence=["e2"], facts=["f2"])])
    decision = evaluate_marginal_gain(state)
    assert decision.stop is False
    print("[OK] new evidence keeps researching")


def test_failed_waves_do_not_trigger_stop():
    state = MarginalGainState()
    record_wave_gain(state, [_row("t1", ok=False)])
    record_wave_gain(state, [_row("t2", ok=False)])
    decision = evaluate_marginal_gain(state)
    assert decision.stop is False
    print("[OK] failed waves do not trigger marginal-gain stop")


def test_state_roundtrip():
    state = MarginalGainState()
    record_wave_gain(state, [_row("t1", evidence=["e1"], sources=["s1"], facts=["f1"])])
    raw = state.to_dict()
    restored = MarginalGainState.from_dict(raw)
    g = record_wave_gain(restored, [_row("t1", evidence=["e1"], sources=["s1"], facts=["f1"])])
    assert g.total_gain == 0
    assert len(restored.wave_gains) == 2
    print("[OK] marginal gain state roundtrip dedupes")


if __name__ == "__main__":
    test_zero_gain_window_stops()
    test_new_evidence_prevents_stop()
    test_failed_waves_do_not_trigger_stop()
    test_state_roundtrip()
    print("\n=== marginal gain tests passed ===")

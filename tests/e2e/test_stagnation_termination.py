from app.research.control.policy import decide_control
from tests.e2e.test_replan_hard_ceiling import _state


def test_stagnation_terminates_recovery_and_delivers_partial():
    state = _state()
    state["semantic_stall"] = 2
    state["marginal_gain"] = {"stalled": True, "semantic_gain": 0.0}
    decision = decide_control(state)
    assert decision["action"] == "deliver_partial"
    assert "semantic_gain_low" in decision["reason_codes"]

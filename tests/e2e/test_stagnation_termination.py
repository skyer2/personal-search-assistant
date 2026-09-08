from tests.e2e.test_replan_hard_ceiling import _state
from app.research.control.policy import decide_control


def test_stagnation_terminates_recovery_and_delivers_partial():
    state = _state()
    state["stalled_cycles"] = 2
    decision = decide_control(state)
    assert decision["action"] == "deliver_partial"
    assert "recovery_stalled" in decision["reason_codes"]

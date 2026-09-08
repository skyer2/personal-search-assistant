from tests.e2e.test_replan_hard_ceiling import _state
from app.research.control.policy import decide_control


def test_recovery_generation_ceiling_stops_recovery():
    state = _state()
    state["replan_budget"] = {"attempted": 0, "applied": 0, "max_attempts": 2}
    state["plan"]["steps"][0]["metadata"]["generation"] = 2
    decision = decide_control(state)
    assert decision["action"] == "deliver_partial"
    assert "recovery_exhausted" in decision["reason_codes"]

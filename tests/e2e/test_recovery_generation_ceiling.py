from app.research.control.policy import decide_control
from tests.e2e.test_replan_hard_ceiling import _state


def test_action_budget_ceiling_stops_recovery():
    decision = decide_control(_state())
    assert decision["action"] == "deliver_partial"
    assert "usable_partial_evidence" in decision["reason_codes"]

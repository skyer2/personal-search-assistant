"""Eval dry-run smoke：新 hierarchy，不再依赖旧 DB/RAGFlow tasks。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.eval.metrics import build_report
from tests.eval.run_eval import run_dry_eval
from tests.eval.runners.scenario import run_scenario_dry_eval


def test_dry_eval_runs():
    results = run_dry_eval()
    report = build_report(results)
    assert report.total >= 60
    assert report.task_success_rate == 1.0
    assert report.plan_validation_pass_rate == 1.0
    payload = report.to_dict()
    assert "gate_pass_rate" in payload
    assert payload["regression_summary"]["component"] == {"total": 20, "passed": 20}
    assert payload["regression_summary"]["capability"] == {"total": 20, "passed": 20}
    assert payload["regression_summary"]["scenario"] == {"total": 20, "passed": 20}
    assert payload["regression_summary"]["by_component"]["planner"] == {"total": 5, "passed": 5}
    assert payload["regression_summary"]["by_component"]["progress"] == {"total": 6, "passed": 6}
    assert payload["regression_summary"]["by_component"]["replan"] == {"total": 5, "passed": 5}
    assert payload["regression_summary"]["by_component"]["evidence"] == {"total": 4, "passed": 4}
    assert set(payload["regression_summary"]["by_capability"]) == {
        "single_fact", "filtered_aggregation", "comparison", "explicit_multi_hop",
        "implicit_multi_hop", "long_report", "freshness", "ambiguity", "conflict",
        "false_premise", "vertical_domain", "long_tail", "multilingual",
        "strict_citation", "multi_turn_correction",
    }
    assert all(value == {"total": value["total"], "passed": value["passed"]} for value in payload["regression_summary"]["by_capability"].values())
    print(f"[OK] dry eval TSR/Gate={report.task_success_rate:.1%} n={report.total}")


def test_scenarios_are_failure_oriented():
    rows = run_scenario_dry_eval()
    assert len(rows) == 20
    assert all(item.metadata.get("failure_mode") for item in rows)
    print("[OK] 20 failure-oriented scenarios")


if __name__ == "__main__":
    test_dry_eval_runs()
    test_scenarios_are_failure_oriented()

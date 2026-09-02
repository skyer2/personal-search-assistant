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
    assert report.total >= 20
    assert report.task_success_rate == 1.0
    assert report.plan_validation_pass_rate == 1.0
    payload = report.to_dict()
    assert "gate_pass_rate" in payload
    print(f"[OK] dry eval TSR/Gate={report.task_success_rate:.1%} n={report.total}")


def test_scenarios_are_failure_oriented():
    rows = run_scenario_dry_eval()
    assert len(rows) == 20
    assert all(item.metadata.get("failure_mode") for item in rows)
    print("[OK] 20 failure-oriented scenarios")


if __name__ == "__main__":
    test_dry_eval_runs()
    test_scenarios_are_failure_oriented()

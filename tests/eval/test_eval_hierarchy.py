"""Harness eval hierarchy：L1 component + L2 scenario dry-run。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.eval.metrics import build_report
from tests.eval.run_eval import run_dry_eval
from tests.eval.runners.component import run_component_eval
from tests.eval.runners.scenario import run_scenario_dry_eval


def test_component_eval_all_pass():
    results = run_component_eval()
    failed = [row.task_id for row in results if not row.success]
    assert not failed, f"component failed: {failed}"
    assert len(results) >= 18
    print(f"[OK] component {len(results)} cases")


def test_scenario_dry_eval_all_pass():
    results = run_scenario_dry_eval()
    failed = [row.task_id for row in results if not row.success]
    assert not failed, f"scenario failed: {failed}"
    assert len(results) == 20
    print(f"[OK] scenario dry-run {len(results)} cases")


def test_constraint_grader_if_then():
    from tests.eval.graders.trajectory import grade_constraints

    miss = grade_constraints(
        ["progress.evaluated"],
        {"if": {"progress.verdict": "gap"}, "then": {"required": ["replan.applied"]}},
        attributes={"progress.verdict": "gap"},
    )
    assert miss["ok"] is False
    hit = grade_constraints(
        ["progress.evaluated", "replan.applied"],
        {"if": {"progress.verdict": "gap"}, "then": {"required": ["replan.applied"]}, "limits": {"replan_count": 2}},
        counts={"replan_count": 1},
        attributes={"progress.verdict": "gap"},
    )
    assert hit["ok"] is True
    print("[OK] constraint if-then")


def test_dry_eval_combined_baseline_shape():
    results = run_dry_eval()
    report = build_report(results)
    payload = report.to_dict()
    assert report.total == len(results)
    assert report.task_success_rate == 1.0
    assert payload["gate_pass_rate"] == 1.0
    assert payload["plan_validation_pass_rate"] == 1.0
    print(f"[OK] combined dry-run {report.passed}/{report.total}")


if __name__ == "__main__":
    test_component_eval_all_pass()
    test_scenario_dry_eval_all_pass()
    test_constraint_grader_if_then()
    test_dry_eval_combined_baseline_shape()
    print("\n=== eval hierarchy tests passed ===")

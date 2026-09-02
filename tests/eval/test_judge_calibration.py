"""Human gold vs automatic judge. Agreement is reported, not assumed to be 100%."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.eval.graders.calibration import CALIBRATION_PATH, calibrate_judge_sync
from tests.eval.runners.component import load_jsonl


def test_calibration_dataset_has_human_labels():
    rows = load_jsonl(CALIBRATION_PATH)
    assert len(rows) >= 12
    assert all((row.get("human") or {}).get("pass_label") in {"pass", "fail"} for row in rows)
    print(f"[OK] calibration set n={len(rows)}")


def test_reference_grader_calibration_runs():
    payload = calibrate_judge_sync(use_llm=False)
    assert payload["n"] >= 12
    assert payload["mode"] == "reference_grader"
    assert 0.0 <= payload["agreement"] <= 1.0
    assert payload["agreement"] >= 0.6
    disagreed = [row for row in payload["details"] if not row["agree"]]
    assert any(row["case_id"] == "cal_13_paraphrase_trap" for row in disagreed)
    print(f"[OK] calibration agreement={payload['agreement']} kappa={payload['kappa']} disagreed={len(disagreed)}")


if __name__ == "__main__":
    test_calibration_dataset_has_human_labels()
    test_reference_grader_calibration_runs()
    print("=== judge calibration tests passed ===")

"""Reliability aggregations: pass@1 / pass@k / pass^k."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.eval.metrics import TaskEvalResult
from tests.eval.reliability import reliability_report


def _row(task_id: str, success: bool, latency_ms: int = 0, tokens: int = 0) -> TaskEvalResult:
    return TaskEvalResult(
        task_id=task_id,
        query=task_id,
        mode="live",
        success=success,
        latency_ms=latency_ms,
        tokens=tokens,
    )


def test_pass_at_k_and_pass_hat_k():
    results = [
        _row("a", True, 100, 10),
        _row("a", False, 200, 20),
        _row("a", True, 150, 15),
        _row("b", False, 80, 8),
        _row("b", False, 90, 9),
        _row("b", False, 70, 7),
    ]
    report = reliability_report(results, k=3)
    assert report["n_cases"] == 2
    assert report["repeat"] == 3
    # a: 2/3 success => pass@1=2/3; pass@3=1; pass^3=0
    # b: 0/3 => all zeros
    assert report["pass_at_1"] == round(((2 / 3) + 0) / 2, 3)
    assert report["pass_at_k"] == 0.5
    assert report["pass_hat_k"] == 0.0
    assert report["latency_mean_ms"] > 0
    print("[OK] pass@k / pass^k")


def test_all_success_is_pass_hat_k():
    results = [_row("a", True), _row("a", True), _row("b", True), _row("b", True)]
    report = reliability_report(results, k=2)
    assert report["pass_at_1"] == 1.0
    assert report["pass_at_k"] == 1.0
    assert report["pass_hat_k"] == 1.0
    print("[OK] consistent success")


if __name__ == "__main__":
    test_pass_at_k_and_pass_hat_k()
    test_all_success_is_pass_hat_k()
    print("=== reliability tests passed ===")

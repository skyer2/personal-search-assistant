"""Phase 9: 可观测性聚合 + Eval Judge 测试（无需 LLM API）。"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.observability import RunObservabilitySnapshot, build_observability_snapshot
from app.agent.harness.state import LoopState
from app.api.observability_metrics import aggregate_metrics, render_prometheus_text
from app.config.loader import reload_harness_config
from tests.eval.judge import heuristic_report_judge


def test_observability_snapshot():
    state = LoopState(session_id="s1")
    state.obs_structured_checks = 3
    state.obs_structured_passes = 2
    state.obs_structured_retries = 1
    state.obs_parallel_batch_count = 1
    state.obs_parallel_steps_executed = 2
    state.obs_estimated_tokens_saved = 120
    snap = build_observability_snapshot(state)
    assert snap.structured_output_compliance_rate == round(2 / 3, 3)
    assert snap.parallel_batch_count == 1
    assert snap.estimated_tokens_saved == 120
    print("[OK] observability snapshot")


def test_jsonl_metrics_aggregation():
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        path = log_dir / "session_a" / "run_a.jsonl"
        path.parent.mkdir(parents=True)
        record = {
            "trace_id": "t1",
            "session_id": "session_a",
            "phase": "run",
            "status": "success",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": 5000,
            "tool_calls": 4,
            "extra": {
                "event": "run_summary",
                "metadata": {
                    "tool_calls_count": 4,
                    "step_success_rate": 1.0,
                    "avg_compression_ratio": 0.35,
                    "citation_coverage_rate": 0.8,
                    "observability": {
                        "structured_output_checks": 2,
                        "structured_output_passes": 2,
                        "parallel_batch_count": 1,
                        "parallel_steps_executed": 2,
                        "estimated_tokens_saved": 200,
                    },
                },
            },
        }
        path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        metrics = aggregate_metrics(log_dir, window_hours=24)
        assert metrics.runs_total == 1
        assert metrics.task_success_rate == 1.0
        assert metrics.structured_output_compliance_rate == 1.0
        assert metrics.parallel_batch_total == 1
        prom = render_prometheus_text(metrics)
        assert "harness_runs_total 1" in prom
        print("[OK] jsonl metrics aggregation")


def test_heuristic_judge():
    good = """# 行业报告\n\n市场规模增长明显 [1]。详见 https://example.com\n\n## 参考文献\n[1] example"""
    bad = "太短"
    r1 = heuristic_report_judge(good, min_score=0.6)
    r2 = heuristic_report_judge(bad, min_score=0.6)
    assert r1.passed and r1.score >= 0.6
    assert not r2.passed
    print("[OK] heuristic judge")


def test_config_phase9():
    cfg = reload_harness_config()
    assert cfg.metrics_enabled is True
    assert cfg.prometheus_enabled is True
    assert cfg.eval_heuristic_judge_enabled is True
    print("[OK] config phase9")


if __name__ == "__main__":
    test_observability_snapshot()
    test_jsonl_metrics_aggregation()
    test_heuristic_judge()
    test_config_phase9()
    print("\n=== Phase 9 observability tests passed ===")

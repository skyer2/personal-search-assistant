"""Phase 4: harness.yml / JSONL logger / health endpoint tests."""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api.health import collect_health
from app.config.loader import load_harness_config, reload_harness_config


def test_harness_config_loads():
    reload_harness_config()
    config = load_harness_config()
    assert config.max_retries >= 1
    assert config.jsonl_log_dir == "logs/traces"
    assert config.compression_max_chars >= 200
    print(f"[OK] harness config version={config.version}")


def test_health_endpoint_shape():
    payload = asyncio.run(collect_health())
    assert "status" in payload
    assert "dependencies" in payload
    assert "llm" in payload["dependencies"]
    assert "version" in payload
    print(f"[OK] health status={payload['status']}")


def test_eval_comparison_markdown():
    from tests.eval.run_eval import write_comparison_markdown

    report = {
        "generated_at": "2026-07-15T12:00:00",
        "mode": "dry-run",
        "total": 10,
        "passed": 10,
        "task_success_rate": 1.0,
        "tool_selection_accuracy": 1.0,
        "step_success_rate": 1.0,
        "recovery_rate": 0.0,
        "avg_tool_calls": 0.0,
        "avg_latency_ms": 0.0,
        "avg_compression_ratio": 1.0,
        "results": [],
    }
    comparison = {
        "baseline_generated_at": "2026-07-15T00:00:00",
        "baseline_mode": "dry-run",
        "deltas": {"task_success_rate": 0.0},
        "regressions": [],
        "blocked_merge": False,
    }
    out = write_comparison_markdown(report, comparison, ROOT / "tests" / "eval" / "results")
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "Gate" in text
    print(f"[OK] comparison markdown: {out.name}")


if __name__ == "__main__":
    test_harness_config_loads()
    test_health_endpoint_shape()
    test_eval_comparison_markdown()
    print("\n=== All Phase 4 tests passed ===")

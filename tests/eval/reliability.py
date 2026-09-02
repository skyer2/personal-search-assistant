"""Reliability: repeated runs of the same case.

LLM agents are stochastic. One success is not reliability.
For production agents prefer pass^k (all k runs succeed) over pass@k
(at least one of k succeeds).
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

from tests.eval.metrics import TaskEvalResult


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((item - mean) ** 2 for item in values) / (len(values) - 1)
    return math.sqrt(variance)


def reliability_report(results: Iterable[TaskEvalResult], *, k: int | None = None) -> dict[str, Any]:
    grouped: dict[str, list[TaskEvalResult]] = defaultdict(list)
    for row in results:
        grouped[row.task_id].append(row)
    if not grouped:
        return {
            "n_cases": 0,
            "repeat": 0,
            "pass_at_1": 0.0,
            "pass_at_k": 0.0,
            "pass_hat_k": 0.0,
        }

    repeats = [len(items) for items in grouped.values()]
    repeat = k or max(repeats)
    case_pass_at_1: list[float] = []
    case_pass_at_k: list[float] = []
    case_pass_hat_k: list[float] = []
    latencies: list[float] = []
    tokens: list[float] = []

    for items in grouped.values():
        sample = items[:repeat]
        successes = [1.0 if item.success else 0.0 for item in sample]
        case_pass_at_1.append(_mean(successes))
        case_pass_at_k.append(1.0 if any(item.success for item in sample) else 0.0)
        case_pass_hat_k.append(1.0 if sample and all(item.success for item in sample) else 0.0)
        latencies.extend(float(item.latency_ms) for item in sample if item.latency_ms > 0)
        tokens.extend(float(item.tokens) for item in sample if item.tokens > 0)

    return {
        "n_cases": len(grouped),
        "repeat": repeat,
        "pass_at_1": round(_mean(case_pass_at_1), 3),
        "pass_at_k": round(_mean(case_pass_at_k), 3),
        "pass_hat_k": round(_mean(case_pass_hat_k), 3),
        "latency_mean_ms": round(_mean(latencies), 1),
        "latency_std_ms": round(_std(latencies), 1),
        "tokens_mean": round(_mean(tokens), 1),
        "tokens_std": round(_std(tokens), 1),
        "note": "pass@k = at least one of k succeeds; pass^k = all k succeed. Prefer pass^k for production agents.",
    }

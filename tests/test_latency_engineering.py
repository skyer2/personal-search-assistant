"""Latency engineering: batch retrieval, absolute deadline, early stop."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.run_budget import RunBudgetManager, get_or_create_run_budget
from app.agent.harness.state import ExecutionPlan, LoopState, PlanStep
from app.agent.harness.step_budget import retrieval_budget, consume_n_retrieval_or_block
from app.research.runtime.scheduler import (
    annotate_plan_tasks,
    ready_research_steps,
    required_retrieval_ids,
    skip_optional_pending,
)
from app.tools.batch_retrieval import run_batch_fetch, run_batch_search
from app.tools.retrieval_cache import clear_retrieval_cache, cached_call, search_cache_key


def test_batch_search_runs_in_parallel_and_respects_budget():
    clear_retrieval_cache()
    calls: list[str] = []

    def _fake_search(query, topic="general", max_results=5, include_raw_content=False):
        calls.append(query)
        return {"ok": True, "query": query, "results": [{"url": f"https://ex/{query}"}]}

    with patch("app.tools.batch_retrieval.search_internet", side_effect=_fake_search):
        with retrieval_budget(10):
            out = run_batch_search(["q1", "q2", "q3"], max_results=3)
    assert out["ok"] is True
    assert out["query_count"] == 3
    assert set(calls) == {"q1", "q2", "q3"}

    with retrieval_budget(1):
        blocked = run_batch_search(["a", "b"])
    assert blocked["ok"] is False
    assert blocked["error"] == "step_retrieval_budget"
    print("[OK] batch_search parallel + budget")


def test_batch_fetch_parallel():
    clear_retrieval_cache()

    def _fake_fetch(url, max_chars=8000, timeout=None, fetcher=None, use_cache=True):
        return {"ok": True, "url": url, "title": url, "snippet": "x", "artifact_id": "a1"}

    with patch("app.tools.batch_retrieval.fetch_url_content", side_effect=_fake_fetch):
        with retrieval_budget(10):
            out = run_batch_fetch(["https://a.example", "https://b.example"])
    assert out["ok"] is True
    assert out["url_count"] == 2
    print("[OK] batch_fetch parallel")


def test_retrieval_cache_single_flight():
    clear_retrieval_cache()
    counter = {"n": 0}

    def producer():
        counter["n"] += 1
        time.sleep(0.05)
        return {"v": counter["n"]}

    key = search_cache_key("hello world", "general", 5)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(cached_call, kind="search", cache_key=key, producer=producer) for _ in range(4)]
        vals = [f.result() for f in futs]
    assert counter["n"] == 1
    assert all(v == vals[0] for v in vals)
    print("[OK] search cache single-flight")


def test_absolute_deadline_uses_run_started_origin():
    started = time.perf_counter() - 100.0  # already 100s into the run
    mgr = RunBudgetManager(
        deadline_sec=600,
        started_at=started,
        synthesis_reserve_sec=75,
    )
    # Remaining should be ~500s, not ~600s
    rem = mgr.remaining_run_sec()
    assert 480 <= rem <= 520, rem
    research_rem = mgr.remaining_for_research_sec()
    assert research_rem < rem
    assert abs(research_rem - (rem - 75)) < 1.0

    # Near end: force synthesis time reserve
    mgr2 = RunBudgetManager(
        deadline_sec=600,
        started_at=time.perf_counter() - 560,
        synthesis_reserve_sec=75,
    )
    allowed, why = mgr2.research_allowed()
    assert allowed is False
    assert why in {"synthesis_time_reserve", "deadline_exceeded", "synthesis_reserve_protect"}
    print("[OK] absolute deadline + synthesis time reserve")


def test_get_or_create_run_budget_binds_run_started():
    state = LoopState(session_id="lat1")
    started = time.perf_counter() - 50
    state.metadata["run_started_monotonic"] = started
    mgr = get_or_create_run_budget(state, None, run_started=started)
    assert mgr.remaining_run_sec() < 560  # not a fresh 600 clock
    mgr2 = get_or_create_run_budget(state, None)
    assert mgr2 is mgr
    print("[OK] run budget origin sticky")


def test_optional_tasks_and_early_skip():
    plan = ExecutionPlan(
        steps=[
            PlanStep(step_type="research", description="r1", task_id="t1", objective="技术能力现状与边界"),
            PlanStep(step_type="research", description="r2", task_id="t2", objective="根本性挑战与局限"),
            PlanStep(step_type="research", description="r3", task_id="t3", objective="发展趋势与未来展望"),
            PlanStep(
                step_type="research",
                description="r4",
                task_id="t4",
                objective="自动化历史辅助",
                metadata={
                    "coverage_keys": ["supporting_context"],
                    "required": False,
                    "optional": True,
                    "priority": 1,
                },
            ),
            PlanStep(step_type="summarize", description="s", task_id="ts", objective="synth"),
        ]
    )
    annotate_plan_tasks(plan)
    required = required_retrieval_ids(plan)
    assert "t4" not in required
    optional = [
        s for s in plan.steps if s.step_type == "research" and s.metadata.get("optional")
    ]
    assert [s.task_id for s in optional] == ["t4"]

    synth = next(s for s in plan.steps if s.step_type == "summarize")
    for oid in [s.task_id for s in optional]:
        assert oid not in (synth.depends_on or [])

    for s in plan.steps:
        if s.step_type == "research" and s.metadata.get("required"):
            s.metadata["status"] = "done"
    status = skip_optional_pending(plan, reason="early_stop_enough")
    for s in optional:
        assert status[s.task_id] == "skipped"
    ready = ready_research_steps(plan, status, include_optional=True)
    assert ready == []
    print("[OK] optional early skip + synth deps")


def test_consume_n_retrieval_budget():
    with retrieval_budget(3):
        assert consume_n_retrieval_or_block(2) is None
        assert consume_n_retrieval_or_block(2) is not None  # only 1 left
    print("[OK] consume_n retrieval budget")


def test_worker_timeout_cap_formula():
    """Document expected outer timeout: not step*(retries+1)."""
    step_timeout = 90
    max_retries = 2
    remaining = 500.0
    retry_slack = 1.0 + min(0.5, 0.25 * max_retries)
    worker_timeout = min(float(step_timeout) * retry_slack, max(5.0, remaining))
    assert worker_timeout <= 135.0  # 90 * 1.5
    assert worker_timeout < step_timeout * (max_retries + 1)
    print("[OK] worker timeout capped", worker_timeout)


if __name__ == "__main__":
    test_batch_search_runs_in_parallel_and_respects_budget()
    test_batch_fetch_parallel()
    test_retrieval_cache_single_flight()
    test_absolute_deadline_uses_run_started_origin()
    test_get_or_create_run_budget_binds_run_started()
    test_optional_tasks_and_early_skip()
    test_consume_n_retrieval_budget()
    test_worker_timeout_cap_formula()
    print("all latency tests passed")

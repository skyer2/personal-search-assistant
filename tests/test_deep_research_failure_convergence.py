from __future__ import annotations

from typing import Any
from unittest.mock import patch

from app.agent.harness.run_budget import RunBudgetManager, create_run_budget_manager
from app.agent.harness.step_budget import worker_retrieval_budget
from app.config.loader import reload_harness_config
from app.research.control.runtime_policy import decide_control
from app.research.control.terminal_policy import terminal_update
from app.research.routing.mode_router import (
    budget_for_mode,
    canonicalize_mode,
    graph_branch_for_mode,
    run_budget_overrides_for_mode,
)
from app.research.runtime.ingestion import ingest_new_worker_results
from app.research.runtime.state import empty_research_state
from app.tools.batch_retrieval import run_batch_search


def _fake_search(query: str, **_kwargs: Any) -> dict[str, Any]:
    if query in {"q1", "q2"}:
        return {
            "ok": True,
            "query": query,
            "results": [{"url": f"https://example.com/{query}", "title": query}],
        }
    return {"ok": True, "query": query, "results": []}


def test_batch_search_requires_at_least_one_valid_http_result() -> None:
    with (
        patch("app.tools.batch_retrieval.search_internet", return_value={"results": []}),
        worker_retrieval_budget(search_queries=1, fetch_sources=1, tool_invocations=1),
    ):
        empty = run_batch_search(["empty"])

    assert empty["ok"] is False
    assert empty["ok_count"] == 0
    assert empty["results"][0]["ok"] is False
    assert empty["results"][0]["error"] == "search_empty"


def test_batch_search_counts_only_queries_with_valid_results() -> None:
    with (
        patch("app.tools.batch_retrieval.search_internet", side_effect=_fake_search),
        worker_retrieval_budget(search_queries=5, fetch_sources=5, tool_invocations=5),
    ):
        mixed = run_batch_search(["q1", "q2", "q3", "q4", "q5"])

    assert mixed["ok"] is True
    assert mixed["query_count"] == 5
    assert mixed["ok_count"] == 2
    assert [row["ok"] for row in mixed["results"]] == [True, True, False, False, False]


def test_batch_search_provider_exception_is_not_success() -> None:
    with (
        patch(
            "app.tools.batch_retrieval.search_internet",
            side_effect=RuntimeError("provider unavailable"),
        ),
        worker_retrieval_budget(search_queries=1, fetch_sources=1, tool_invocations=1),
    ):
        failed = run_batch_search(["provider failure"])

    assert failed["ok"] is False
    assert failed["ok_count"] == 0
    assert "provider unavailable" in failed["results"][0]["error"]


def test_worker_leases_reserve_only_in_flight_tokens() -> None:
    manager = RunBudgetManager(
        token_limit=500_000,
        llm_call_limit=240,
        tool_call_limit=600,
        deadline_sec=3_600,
        max_parallel_workers=2,
    )
    first, first_reason = manager.reserve_worker_lease(
        "task_a", parallel_workers=2, token_ceiling=80_000, max_llm_calls=16
    )
    second, second_reason = manager.reserve_worker_lease(
        "task_b", parallel_workers=2, token_ceiling=80_000, max_llm_calls=16
    )
    assert first and second
    assert not first_reason and not second_reason
    assert manager.snapshot().reserved_tokens == 0

    first_call, first_call_reason = manager.reserve_llm_call(
        estimated_tokens=8_000,
        worker_task_id="task_a",
        phase="worker",
    )
    second_call, second_call_reason = manager.reserve_llm_call(
        estimated_tokens=8_000,
        worker_task_id="task_b",
        phase="worker",
    )
    assert first_call and second_call
    assert not first_call_reason and not second_call_reason
    assert manager.snapshot().reserved_tokens == 16_000

    manager.commit_llm_usage(first_call, 6_000)
    manager.commit_llm_usage(second_call, 6_000)
    assert manager.snapshot().reserved_tokens == 0
    assert manager.snapshot().used_tokens == 12_000

    denied, denied_reason = manager.reserve_llm_call(
        estimated_tokens=75_000,
        worker_task_id="task_a",
        phase="worker",
    )
    assert denied == ""
    assert denied_reason == "worker_token_cap"


def test_deep_debug_is_explicit_profile_without_changing_production_defaults() -> None:
    config = reload_harness_config()

    assert canonicalize_mode("deep_debug") == "deep_debug"
    assert graph_branch_for_mode("deep_debug") == "intent"
    assert config.max_total_tokens == 500_000
    assert config.max_run_sec == 1_800
    assert config.max_replan_count == 3

    budget = budget_for_mode(
        "deep_debug", getattr(config, "personal_search", None) or {}
    )
    assert budget["max_tool_calls"] == 600
    assert budget["max_replan_count"] == 6
    assert budget["max_parallel_workers"] == 2
    assert budget["max_total_tokens"] == 1_200_000
    assert budget["max_run_sec"] == 3_600
    assert budget["max_llm_calls"] == 240
    assert budget["max_llm_calls_per_worker"] == 32

    overrides = run_budget_overrides_for_mode(
        "deep_debug", getattr(config, "personal_search", None) or {}
    )
    manager = create_run_budget_manager(config, run_budget=overrides)
    assert manager.token_limit == 1_200_000
    assert manager.tool_call_limit == 600
    assert manager.deadline_sec == 3_600
    assert manager.llm_call_limit == 240
    assert manager.max_llm_calls_per_worker == 32
    assert manager.max_parallel_workers == 2


def test_one_failed_worker_does_not_discard_usable_worker_evidence() -> None:
    state = empty_research_state(
        run_id="mixed-worker-run",
        session_id="mixed-worker-run",
        task_query="Evaluate Company A.",
    )
    state["dispatch_wave_id"] = 1
    state["worker_results"] = [
        {
            "task_id": "task_failed",
            "dispatch_wave_id": 1,
            "ok": False,
            "status": "failed",
            "summary": "search_empty",
            "fail_reason": "search_empty",
        },
        {
            "task_id": "task_success",
            "dispatch_wave_id": 1,
            "ok": True,
            "status": "done",
            "summary": "Company A has funding evidence.",
            "payload": {
                "summary": "Company A has funding evidence.",
                "facts": ["Company A has funding evidence."],
                "sources": ["https://example.com/company-a"],
                "evidence_ids": ["E1"],
                "findings": [
                    {
                        "claim": "Company A has funding evidence.",
                        "evidence_ids": ["E1"],
                        "confidence": 0.9,
                    }
                ],
            },
        },
    ]

    update = ingest_new_worker_results(state)
    assert update["evidence_records"]
    assert update["claims"]
    assert update["findings"]

    state.update(update)
    state["budget"]["exhausted"] = True
    assert decide_control(state).action == "deliver_partial"


def test_no_usable_evidence_is_the_only_evidence_failure_terminal() -> None:
    state = empty_research_state(
        run_id="no-evidence-run",
        session_id="no-evidence-run",
        task_query="Evaluate Company A.",
    )
    state["quality_assessment"] = {"verdict": "fail"}
    update = terminal_update(state, reason="", stage="finalize")
    assert update["termination"]["reason"] == "NO_USABLE_EVIDENCE"
    assert update["termination"]["outcome"] == "failed"

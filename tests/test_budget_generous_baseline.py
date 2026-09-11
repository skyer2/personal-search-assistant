"""Contract tests for the generous deep-research budget baseline."""

from __future__ import annotations

import pytest

from app.agent.harness.run_budget import RunBudgetManager
from app.agent.harness.step_budget import (
    consume_fetch_sources_or_block,
    consume_search_queries_or_block,
    worker_retrieval_budget,
)
from app.config.loader import reload_harness_config
from app.research.runtime.graph import _plan_from_tasks
from app.research.runtime.task_budget import (
    task_budget_metadata,
    task_budget_profile,
)
from app.research.supervisor.models import ResearchTaskRequest


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("small", (40_000, 10, 6, 10, 10, 2_500)),
        ("medium", (80_000, 16, 10, 16, 16, 3_500)),
        ("large", (120_000, 24, 16, 24, 24, 5_000)),
    ],
)
def test_task_budget_profiles_use_generous_baseline(
    monkeypatch: pytest.MonkeyPatch,
    effort: str,
    expected: tuple[int, int, int, int, int, int],
) -> None:
    prefix = f"HARNESS_TASK_{effort.upper()}_"
    for suffix in (
        "TOKEN_CEILING",
        "MAX_LLM_CALLS",
        "MAX_SEARCH_QUERIES",
        "MAX_FETCH_SOURCES",
        "MAX_TOOL_INVOCATIONS",
        "MAX_OUTPUT_TOKENS_PER_CALL",
    ):
        monkeypatch.delenv(prefix + suffix, raising=False)

    profile = task_budget_profile(effort)
    values = (
        profile.token_ceiling,
        profile.max_llm_calls,
        profile.max_search_queries,
        profile.max_fetch_sources,
        profile.max_tool_invocations,
        profile.max_output_tokens_per_call,
    )
    assert values == expected


def test_run_budget_uses_hard_safety_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HARNESS_MAX_LLM_CALLS",
        "HARNESS_MAX_LLM_CALLS_PER_WORKER",
        "HARNESS_MAX_RUN_SEC",
        "HARNESS_WORKER_IDLE_TIMEOUT_SEC",
    ):
        monkeypatch.delenv(name, raising=False)

    config = reload_harness_config()
    assert config.max_total_tokens == 500_000
    assert config.max_tool_calls == 300
    assert config.max_step_tool_calls == 40
    assert config.max_llm_calls_per_run == 120
    assert config.max_llm_calls_per_worker == 24
    assert config.max_run_sec == 1_800
    assert config.worker_idle_timeout_sec == 120


def test_medium_profile_reaches_worker_lease_without_legacy_clamp() -> None:
    request = ResearchTaskRequest(
        objective="Evaluate embodied-AI company evidence",
        estimated_effort="medium",
    )
    profile = task_budget_profile(request.estimated_effort)
    plan = _plan_from_tasks([request], plan_version=1, planning_mode="test")
    metadata = dict(plan.steps[0].metadata)
    assert metadata["token_ceiling"] == profile.token_ceiling
    assert task_budget_metadata(profile) == {
        "max_llm_calls": 16,
        "max_search_queries": 10,
        "max_fetch_sources": 16,
        "max_tool_invocations": 16,
        "max_output_tokens_per_call": 3_500,
    }

    manager = RunBudgetManager(
        token_limit=500_000,
        llm_call_limit=120,
        tool_call_limit=300,
        deadline_sec=1_800,
        synthesis_reserve_sec=180,
        max_parallel_workers=3,
        max_llm_calls_per_worker=24,
    )
    leases = []
    for task_id in ("task-a", "task-b", "task-c"):
        lease_id, reason = manager.reserve_worker_lease(
            task_id,
            parallel_workers=3,
            max_llm_calls=metadata["max_llm_calls"],
            token_ceiling=metadata["token_ceiling"],
            max_output_tokens_per_call=metadata["max_output_tokens_per_call"],
        )
        assert lease_id
        assert not reason
        leases.append(lease_id)

    for task_id in ("task-a", "task-b", "task-c"):
        snapshot = manager.worker_lease_snapshot(task_id=task_id)
        assert snapshot["token_limit"] == 80_000
        assert snapshot["llm_calls_limit"] == 16


def test_medium_worker_can_search_fetch_and_supplement_search() -> None:
    with worker_retrieval_budget(
        search_queries=10,
        fetch_sources=16,
        tool_invocations=16,
    ) as budget:
        assert consume_search_queries_or_block(4) is None
        assert consume_fetch_sources_or_block(8) is None
        assert consume_search_queries_or_block(4) is None
        assert budget.snapshot() == {
            "search_queries_used": 8,
            "search_queries_limit": 10,
            "fetch_sources_used": 8,
            "fetch_sources_limit": 16,
            "tool_invocations_used": 3,
            "tool_invocations_limit": 16,
        }

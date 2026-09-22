"""Hard budget objects must agree with the routed research profile."""

from __future__ import annotations

import time
from types import SimpleNamespace

from app.agent.harness.budget_events import emit_budget_denied
from app.agent.harness.run_budget import RunBudgetManager
from app.agent.harness.state import LoopState
from app.config.loader import reload_harness_config
from app.research.runtime.runner import ResearchGraphRunner


def _runner() -> ResearchGraphRunner:
    return ResearchGraphRunner(SimpleNamespace(harness_config=reload_harness_config()))


def _context(mode: str, manager: RunBudgetManager | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        task_query="Research current agent trends",
        search_mode=mode,
        run_started=time.perf_counter(),
        state=LoopState(session_id=f"budget-{mode}"),
        budget_manager=manager,
    )


def test_deep_debug_route_builds_actual_hard_budget_before_session() -> None:
    ctx = _context("deep_debug")
    profile, _ = _runner()._bootstrap_run(ctx)

    assert profile == "deep_debug"
    assert ctx.budget_manager.token_limit == 1_200_000
    assert ctx.budget_manager.deadline_sec == 3_600
    assert ctx.budget_manager.llm_call_limit == 240
    assert ctx.budget_manager.max_parallel_workers == 2
    assert ctx.state.metadata["run_budget"]["max_total_tokens"] == ctx.budget_manager.token_limit


def test_default_agent_budget_is_not_contaminated_by_deep_debug() -> None:
    runner = _runner()
    runner._bootstrap_run(_context("deep_debug"))
    ctx = _context("agent")
    profile, _ = runner._bootstrap_run(ctx)

    assert profile == "agent"
    assert ctx.budget_manager.token_limit == 500_000
    assert ctx.budget_manager.deadline_sec == 1_800
    assert ctx.budget_manager.llm_call_limit != 240
    assert ctx.budget_manager.max_parallel_workers == 3


def test_unused_default_manager_is_rebuilt_for_deep_debug() -> None:
    old = RunBudgetManager(token_limit=500_000, deadline_sec=1_800)
    ctx = _context("deep_debug", old)
    _runner()._bootstrap_run(ctx)

    assert ctx.budget_manager is not old
    assert ctx.budget_manager.token_limit == 1_200_000


def test_used_manager_cannot_mutate_profile() -> None:
    old = RunBudgetManager(token_limit=500_000, deadline_sec=1_800)
    old.commit_tokens(1)
    ctx = _context("deep_debug", old)
    ctx.state.metadata["run_budget"] = {"profile": "agent"}
    profile, _ = _runner()._bootstrap_run(ctx)

    assert profile == "agent"
    assert ctx.budget_manager is old
    assert ctx.state.metadata["run_budget"]["max_total_tokens"] == 500_000
    assert "mutation blocked" in ctx.state.metadata["budget_profile_warning"]


def test_denial_without_counters_uses_the_rejecting_phase_counter(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(
        "app.observability.get_recorder",
        lambda: SimpleNamespace(is_active=True, emit=lambda *args, **kwargs: events.append(kwargs)),
    )
    manager = RunBudgetManager(token_limit=1_000)
    manager.commit_tokens(200)

    emit_budget_denied(
        scope="research_phase",
        resource="token",
        reason="research_phase_token_cap",
        budget_manager=manager,
    )

    attrs = events[0]["attributes"]
    assert attrs["used"] == 200
    assert attrs["reserved"] == 0
    # v4 protects 15% for the single targeted repair wave; initial research
    # is therefore capped at 60% rather than borrowing the repair share.
    assert attrs["limit"] == 600

"""Regression tests for rejected-replan and landscape gap convergence."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.harness.deliverables import ensure_requested_deliverables
from app.agent.harness.planner import understand_task
from app.agent.harness.state import LoopState, StepResult, TaskIntent
from app.agent.harness.artifacts import ArtifactStore, reset_artifact_store, set_artifact_store
from app.research.planning.lead_planner import heuristic_dynamic_plan
from app.research.planning.plan_patch import apply_plan_patch, build_progress_patch
from app.research.planning.policy import parse_source_policy
from app.research.planning.candidate import build_candidate_set
from app.research.runtime.graph import (
    compile_research_graph,
    control_plane_fingerprint,
    route_after_quality,
)
from app.research.runtime.state import empty_research_state
from app.research.runtime.project import findings_from_worker_row
from app.research.runtime.fast_synthesis import (
    build_minimal_evidence_pack,
    render_fast_partial_report,
)
from app.research.runtime.synthesis_admission import trusted_evidence_count
from app.research.runtime.worker import (
    LangChainWorkerRuntime,
    ResearchContext,
    ResearchTask,
    WorkerResult,
)
from app.research.runtime.untrusted import structured_evidence_from_artifact
from app.agent.harness.run_budget import BudgetReservationError, RunBudgetManager
from app.agent.harness.tool_contract import apply_tool_output_contract
from app.agent.harness.usage_tracker import (
    UsageTrackingCallback,
    bind_budget_manager,
    bind_worker_budget_scope,
    set_current_llm_reservation,
    wrap_model_with_budget,
)


EXAMPLE_QUERY = "你觉的当下国内AI初创有潜力值得加入的公司有哪些？为什么？输出结果为pdf"


def test_rejected_replan_cannot_loop_from_quality() -> None:
    assert (
        route_after_quality(
            {
                "quality_passed": False,
                "quality_repair_action": "replan",
                "quality_attempts": 1,
                "budget": {"max_replan_count": 2},
                "replan_count": 0,
                "replan_attempts": 1,
                "replan_applied_count": 0,
                "replan_exhausted": True,
            }
        )
        == "finalize"
    )
    assert (
        route_after_quality(
            {
                "quality_passed": False,
                "quality_repair_action": "replan",
                "quality_attempts": 1,
                "budget": {"max_replan_count": 1},
                "replan_count": 0,
                "replan_attempts": 1,
                "replan_applied_count": 0,
            }
        )
        == "finalize"
    )


def test_control_fingerprint_ignores_transient_quality_attempts() -> None:
    base = {
        "plan_version": 1,
        "task_status": {"t_landscape": "done"},
        "progress_assessment": {"open_gap_ids": ["gap_hiring"]},
        "replan_exhausted": False,
        "replan_attempts": 1,
        "quality_reason": "no_content",
        "quality_repair_action": "replan",
        "status": "synthesized",
    }
    assert control_plane_fingerprint(base) == control_plane_fingerprint(
        {**base, "quality_attempts": 9}
    )
    changed = {**base, "task_status": {"t_landscape": "done", "t_gap_deepseek": "done"}}
    assert control_plane_fingerprint(base) != control_plane_fingerprint(changed)


def test_landscape_gap_patch_is_candidate_scoped_gap_fill() -> None:
    intent = understand_task(EXAMPLE_QUERY)
    assert intent.deliverable == "pdf"
    assert intent.brief.task_kind == "landscape_discovery"
    plan = heuristic_dynamic_plan(intent, parse_source_policy(EXAMPLE_QUERY))
    assessment = {
        "verdict": "gap",
        "reason": "semantic_gap",
        "progress_id": "progress_test",
        "gaps": [
            {"gap_id": "gap_hiring", "description": "招聘与人才机会不足", "actionable": True},
            {"gap_id": "gap_risk", "description": "加入风险不足", "actionable": True},
        ],
        "open_gap_ids": ["gap_hiring", "gap_risk"],
    }
    candidate_set = {
        "available": True,
        "status": "complete",
        "items": ["DeepSeek", "Moonshot", "MiniMax"],
    }
    patch = build_progress_patch(
        plan,
        intent,
        assessment=assessment,
        max_new_tasks=2,
        candidate_set=candidate_set,
    )
    tasks = patch["add_tasks"]
    assert patch["reason"] == "candidate_gap_fill"
    assert [task["metadata"]["subject_id"] for task in tasks] == ["deepseek", "moonshot"]
    for task in tasks:
        metadata = task["metadata"]
        assert metadata["task_kind"] == "gap_fill"
        assert metadata["entities"]
        assert metadata["coverage_keys"]
        assert metadata["resolves_gap_ids"]
        assert metadata["requires_artifacts"] == ["candidate_set"]

    updated, issues = apply_plan_patch(
        plan,
        patch,
        intent,
        max_new_tasks=2,
    )
    assert issues == []
    assert any(step.task_id == "t_gap_deepseek" for step in updated.steps)


def test_broad_category_gap_fill_is_rejected() -> None:
    intent = understand_task(EXAMPLE_QUERY)
    plan = heuristic_dynamic_plan(intent, parse_source_policy(EXAMPLE_QUERY))
    patch = {
        "add_tasks": [
            {
                "task_id": "t_gap_broad",
                "objective": "补充国内 AI 初创公司招聘信息",
                "allowed_sources": ["web"],
                "metadata": {
                    "task_kind": "gap_fill",
                    "subject_id": "china_ai_startups",
                    "entities": ["国内 AI 初创公司"],
                    "coverage_keys": ["招聘与人才机会"],
                    "resolves_gap_ids": ["gap_hiring"],
                    "requires_artifacts": ["candidate_set"],
                },
            }
        ],
        "target_gap_ids": ["gap_hiring"],
    }
    _, issues = apply_plan_patch(plan, patch, intent, max_new_tasks=1)
    assert "category_gap_too_broad:t_gap_broad" in issues


def test_runner_counts_rejected_replan_attempt(monkeypatch) -> None:
    import app.research.runtime.runner as runner_module

    intent = understand_task(EXAMPLE_QUERY)
    plan = heuristic_dynamic_plan(intent, parse_source_policy(EXAMPLE_QUERY))
    state = LoopState(session_id="replan-reject")
    state.intent = intent
    state.plan = plan
    state.metadata["run_budget"] = {"max_replan_count": 1}

    session = runner_module.RunSession.__new__(runner_module.RunSession)
    session.state = state
    session.budget_manager = SimpleNamespace(remaining_for_research_sec=lambda: 1000)
    monkeypatch.setattr(runner_module, "get_session", lambda _run_id: session)

    config = SimpleNamespace(
        hitl_allow_replan=True,
        max_replan_count=1,
        max_plan_steps=12,
        planner_max_plan_patch_tasks=2,
        max_step_tool_calls=8,
        step_timeout_sec=10,
        replan_context_overhead_sec=1,
        replan_checkpoint_overhead_sec=1,
    )
    runner = runner_module.ResearchGraphRunner.__new__(runner_module.ResearchGraphRunner)
    runner.harness = SimpleNamespace(harness_config=config)

    update = asyncio.run(
        runner.node_replan(
            {
                "run_id": "run-replan-reject",
                "budget": {"max_replan_count": 1},
                "replan_count": 0,
                "replan_attempts": 0,
                "replan_applied_count": 0,
                "worker_results": [],
                "candidate_set": {},
                "progress_assessment": {
                    "verdict": "gap",
                    "reason": "semantic_gap",
                    "gaps": [{"gap_id": "gap_hiring", "description": "招聘不足"}],
                },
            }
        )
    )
    assert update["replan_attempts"] == 1
    assert update["replan_applied_count"] == 0
    assert update["replan_count"] == 0
    assert update["replan_exhausted"] is True


def test_runner_applies_candidate_gap_patch_once(monkeypatch) -> None:
    import app.research.runtime.runner as runner_module

    intent = understand_task(EXAMPLE_QUERY)
    plan = heuristic_dynamic_plan(intent, parse_source_policy(EXAMPLE_QUERY))
    state = LoopState(session_id="replan-apply")
    state.intent = intent
    state.plan = plan
    state.metadata["run_budget"] = {"max_replan_count": 1}

    session = runner_module.RunSession.__new__(runner_module.RunSession)
    session.state = state
    session.budget_manager = SimpleNamespace(remaining_for_research_sec=lambda: 1000)
    monkeypatch.setattr(runner_module, "get_session", lambda _run_id: session)

    config = SimpleNamespace(
        hitl_allow_replan=True,
        max_replan_count=1,
        max_plan_steps=12,
        planner_max_plan_patch_tasks=2,
        max_step_tool_calls=8,
        step_timeout_sec=10,
        replan_context_overhead_sec=1,
        replan_checkpoint_overhead_sec=1,
    )
    harness = SimpleNamespace(
        harness_config=config,
        _report_phase=lambda *args, **kwargs: None,
    )
    runner = runner_module.ResearchGraphRunner.__new__(runner_module.ResearchGraphRunner)
    runner.harness = harness

    update = asyncio.run(
        runner.node_replan(
            {
                "run_id": "run-replan-apply",
                "budget": {"max_replan_count": 1},
                "replan_count": 0,
                "replan_attempts": 0,
                "replan_applied_count": 0,
                "worker_results": [],
                "candidate_set": {
                    "available": True,
                    "status": "complete",
                    "items": ["DeepSeek", "Moonshot"],
                },
                "progress_assessment": {
                    "verdict": "gap",
                    "reason": "semantic_gap",
                    "progress_id": "progress_apply",
                    "gaps": [
                        {
                            "gap_id": "gap_hiring",
                            "description": "招聘与人才机会不足",
                            "actionable": True,
                        }
                    ],
                    "open_gap_ids": ["gap_hiring"],
                },
            }
        )
    )
    assert update["plan_version"] == 2
    assert update["replan_attempts"] == 1
    assert update["replan_applied_count"] == 1
    assert update["replan_count"] == 1
    assert update["replan_exhausted"] is True
    assert "t_gap_deepseek" in update["task_status"]


class FakeBudgetedModel:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, input, config=None, **kwargs):
        self.calls += 1
        return "ok"


def test_budget_model_wrapper_blocks_before_provider_call() -> None:
    provider = FakeBudgetedModel()
    model = wrap_model_with_budget(provider)
    manager = RunBudgetManager(token_limit=10, llm_call_limit=2)
    with bind_budget_manager(manager):
        with pytest.raises(BudgetReservationError):
            asyncio.run(model.ainvoke("hello"))
    assert provider.calls == 0
    assert manager.snapshot().llm_calls == 0
    assert manager.snapshot().reserved_llm_calls == 0


def test_budget_model_wrapper_commits_when_callback_is_absent() -> None:
    provider = FakeBudgetedModel()
    model = wrap_model_with_budget(provider)
    manager = RunBudgetManager(token_limit=10000, llm_call_limit=2)
    with bind_budget_manager(manager):
        result = asyncio.run(model.ainvoke("hello"))
    assert result == "ok"
    assert provider.calls == 1
    assert manager.snapshot().llm_calls == 1
    assert manager.snapshot().used_tokens > 0
    assert manager.snapshot().reserved_llm_calls == 0


def test_budget_model_wrapper_does_not_touch_lazy_model_attributes() -> None:
    class LazyConfigurableModel:
        def invoke(self, value):
            return value

        def __getattr__(self, name):
            if name == "_harness_budget_aware":
                raise AssertionError("budget wrapper touched lazy model marker")
            raise AttributeError(name)

    model = LazyConfigurableModel()
    wrapped = wrap_model_with_budget(model)
    assert wrapped.invoke("hello") == "hello"


def test_usage_callback_reuses_model_wrapper_reservation() -> None:
    manager = RunBudgetManager(token_limit=1000, llm_call_limit=2)
    callback = UsageTrackingCallback(session_id="budget-callback", phase="execute")
    with bind_budget_manager(manager):
        reservation_id, _reason = manager.reserve_llm_call(
            estimated_tokens=100,
            phase="execute",
        )
        token = set_current_llm_reservation(reservation_id)
        try:
            callback.on_llm_start(None, ["hello"], run_id="run-1")
        finally:
            set_current_llm_reservation("")
        assert manager.snapshot().reserved_llm_calls == 1
        callback.on_llm_end(
            SimpleNamespace(
                llm_output={
                    "model_name": "fake",
                    "token_usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
                generations=[],
            ),
            run_id="run-1",
        )
    assert manager.snapshot().llm_calls == 1
    assert manager.snapshot().used_tokens == 15
    assert manager.snapshot().reserved_llm_calls == 0


def test_budget_blocked_worker_salvages_artifact_evidence() -> None:
    intent = understand_task(EXAMPLE_QUERY)
    plan = heuristic_dynamic_plan(intent, parse_source_policy(EXAMPLE_QUERY))
    state = LoopState(session_id="budget-salvage")
    state.intent = intent
    state.plan = plan
    session = SimpleNamespace(
        state=state,
        worker_sem=asyncio.Semaphore(1),
        lock=asyncio.Lock(),
        budget_manager=SimpleNamespace(
            sync_from_usage=lambda **kwargs: None,
            reserve_worker_lease=lambda task_id, parallel_workers: ("lease", ""),
            remaining_for_research_sec=lambda: 100,
        ),
        _resolve_max_workers=lambda: 1,
        ctx=SimpleNamespace(
            task_query=EXAMPLE_QUERY,
            relative_session_dir=Path("."),
            uploaded_prompt=None,
            session_dir=Path("."),
            idempotency=None,
        ),
        session_id="budget-salvage",
        run_id="run-budget-salvage",
    )

    async def run_single_step(*args, **kwargs):
        raise BudgetReservationError("research_token_cap")

    harness = SimpleNamespace(
        harness_config=SimpleNamespace(
            step_timeout_sec=10,
            worker_idle_timeout_sec=1,
            max_retries=0,
        ),
        _run_single_step=run_single_step,
    )
    store = ArtifactStore()
    set_artifact_store(store)
    try:
        with bind_worker_budget_scope("t_landscape"):
            apply_tool_output_contract(
                {
                    "results": [
                        {
                            "title": "DeepSeek company profile",
                            "url": "https://example.com/deepseek",
                            "snippet": "DeepSeek: 国内高潜力 AI 初创公司，聚焦开源大模型。",
                        }
                    ]
                },
                tool_name="internet_search",
                step_type="network_search",
            )
        result = asyncio.run(
            LangChainWorkerRuntime(harness, session).execute(
                ResearchTask(
                    task_id="t_landscape",
                    objective="发现国内 AI 初创公司候选池",
                    step_type="research",
                    step_index=0,
                    plan_version=1,
                ),
                ResearchContext(run_id="run-budget-salvage", query=EXAMPLE_QUERY),
            )
        )
    finally:
        reset_artifact_store()

    assert result.status == "blocked"
    assert result.fail_reason == "research_token_cap"
    assert result.evidence_refs
    assert "https://example.com/deepseek" in result.sources
    assert any("DeepSeek" in str(item.get("summary") or "") for item in result.findings)
    assert state.metadata["force_synthesis"] is True
    assert state.metadata["budget_degrade_reason"] == "research_token_cap"


def test_budget_blocked_worker_evidence_reaches_candidate_and_synthesis(monkeypatch) -> None:
    import app.research.runtime.runner as runner_module
    import app.research.runtime.worker as worker_module

    intent = understand_task(EXAMPLE_QUERY)
    plan = heuristic_dynamic_plan(intent, parse_source_policy(EXAMPLE_QUERY))
    state = LoopState(session_id="budget-projection")
    state.intent = intent
    state.plan = plan
    session = SimpleNamespace(
        state=state,
        ctx=SimpleNamespace(
            task_query=EXAMPLE_QUERY,
            user_id="me",
            tenant_id="local",
            project_id="Inbox",
        ),
        session_id="budget-projection",
        run_id="run-budget-projection",
    )
    monkeypatch.setattr(runner_module, "get_session", lambda _run_id: session)

    class BlockedRuntime:
        def __init__(self, harness, current_session):
            self.harness = harness
            self.session = current_session

        async def execute(self, task, context):
            return WorkerResult(
                ok=False,
                task_id=task.task_id,
                status="blocked",
                summary="budget_blocked:research_token_cap",
                findings=[
                    {
                        "task_id": task.task_id,
                        "summary": "DeepSeek 是国内高潜力 AI 初创公司。",
                        "facts": ["DeepSeek"],
                        "sources": ["https://example.com/deepseek"],
                        "evidence_ids": ["art-web-1"],
                    }
                ],
                evidence_refs=["art-web-1"],
                sources=["https://example.com/deepseek"],
                fail_reason="research_token_cap",
            )

    monkeypatch.setattr(worker_module, "LangChainWorkerRuntime", BlockedRuntime)
    runner = runner_module.ResearchGraphRunner.__new__(runner_module.ResearchGraphRunner)
    runner.harness = SimpleNamespace(
        harness_config=SimpleNamespace(hitl_enabled=False, worker_executor_v2=False),
    )
    update = asyncio.run(
        runner.node_research_worker(
            {
                "run_id": "run-budget-projection",
                "step_index": 0,
                "task_id": "t_landscape",
                "step_type": "research",
                "plan_version": 1,
                "budget": {},
            }
        )
    )
    row = update["worker_results"][0]
    payload = row["payload"]
    assert row["status"] == "blocked"
    assert payload["partial_evidence_count"] == 1
    assert payload["evidence_ids"] == ["art-web-1"]
    assert findings_from_worker_row(row)
    assert trusted_evidence_count(update) >= 2

    task_status = {
        step.resolved_task_id(index): "failed"
        for index, step in enumerate(plan.steps)
    }
    candidate_set = build_candidate_set(
        plan,
        worker_rows=[row],
        task_status=task_status,
        query=EXAMPLE_QUERY,
        brief=intent.brief.to_dict(),
    )
    assert candidate_set["available"] is True
    assert "DeepSeek" in candidate_set["items"]


def test_salvaged_tool_json_is_readable_and_report_includes_sources() -> None:
    raw_content = (
        '{"results":[{"title":"MiniMax IPO","url":"https://example.com/minimax",'
        '"content":"MiniMax 正在推进港股 IPO。"},'
        '{"title":"智谱AI 融资","url":"https://example.com/zhipu",'
        '"content":"智谱AI 继续获得机构投资。"}]}'
    )
    evidence = structured_evidence_from_artifact(
        SimpleNamespace(
            artifact_id="art-web-1",
            kind="web",
            locator="https://example.com/minimax",
            title="search results",
            summary='{"results":[{"title":"trunc',
            content=raw_content,
        )
    )
    assert "MiniMax IPO" in evidence["excerpt"]
    assert "智谱AI 融资" in evidence["excerpt"]
    assert "MiniMax IPO" in evidence["facts"]

    pack = build_minimal_evidence_pack(
        state=LoopState(session_id="salvage-report"),
        graph_state={
            "findings": [
                {
                    "task_id": "t_landscape",
                    "summary": "MiniMax 与智谱AI 是可评估候选。",
                }
            ],
            "worker_results": [
                {
                    "task_id": "t_landscape",
                    "payload": {"sources": ["https://example.com/minimax"]},
                }
            ],
        },
    )
    report = render_fast_partial_report(pack, reason="research_token_cap")
    assert "https://example.com/minimax" in report
    assert "MiniMax 与智谱AI 是可评估候选。" in report


class ConvergenceRuntime:
    def __init__(self, deliverable_dir: Path) -> None:
        self.deliverable_dir = deliverable_dir
        self.node_calls = 0
        self.progress_calls = 0
        self.replan_calls = 0
        self.pdf_path: Path | None = None

    def _call(self, name: str) -> None:
        self.node_calls += 1
        if name == "progress":
            self.progress_calls += 1
        if name == "replan":
            self.replan_calls += 1

    async def node_vanilla_agent(self, state):
        self._call("vanilla")
        return {"status": "completed", "final_content": "unused"}

    async def node_intent(self, state):
        self._call("intent")
        intent = understand_task(EXAMPLE_QUERY)
        payload = intent.to_dict()
        payload["brief"] = intent.brief.to_dict()
        budget = dict(state["budget"])
        budget["max_replan_count"] = 1
        return {
            "intent": payload,
            "brief": intent.brief.to_dict(),
            "needs_clarification": False,
            "budget": budget,
            "progress": "intent",
        }

    async def node_clarify(self, state):
        self._call("clarify")
        return {"needs_clarification": False}

    async def node_plan(self, state):
        self._call("plan")
        intent = TaskIntent.from_dict(state["intent"])
        plan = heuristic_dynamic_plan(intent, parse_source_policy(EXAMPLE_QUERY))
        status = {
            step.resolved_task_id(index): "pending"
            for index, step in enumerate(plan.steps)
        }
        return {
            "plan": plan.to_dict(),
            "plan_version": plan.plan_version,
            "task_status": status,
            "needs_plan_review": False,
            "progress": "planned",
        }

    async def node_plan_validate(self, state):
        self._call("plan_validate")
        return {"needs_plan_review": False, "progress": "plan_validated"}

    async def node_dispatch(self, state):
        self._call("dispatch")
        return {"progress": "dispatch"}

    async def node_research_worker(self, state):
        self._call("worker")
        task_id = state["task_id"]
        return {
            "worker_results": [
                {
                    "task_id": task_id,
                    "ok": True,
                    "payload": {
                        "summary": "Discovery found DeepSeek, Moonshot and MiniMax.",
                        "facts": ["DeepSeek", "Moonshot", "MiniMax"],
                        "sources": ["https://example.com/startups"],
                        "evidence_ids": ["ev_landscape"],
                    },
                }
            ],
            "task_status": {task_id: "done"},
            "evidence_refs": ["ev_landscape"],
        }

    async def node_progress(self, state):
        self._call("progress")
        assessment = {
            "verdict": "gap",
            "reason": "semantic_gap",
            "progress_id": "progress_e2e",
            "gaps": [
                {"gap_id": "gap_hiring", "description": "招聘与人才机会不足", "actionable": True},
                {"gap_id": "gap_risk", "description": "加入风险不足", "actionable": True},
            ],
            "open_gap_ids": ["gap_hiring", "gap_risk"],
        }
        return {
            "progress_assessment": assessment,
            "candidate_set": {
                "available": True,
                "status": "complete",
                "items": ["DeepSeek", "Moonshot", "MiniMax"],
            },
            "progress": "progress_eval",
        }

    async def node_replan(self, state):
        self._call("replan")
        return {
            "replan_attempts": 1,
            "replan_applied_count": 0,
            "replan_count": 0,
            "replan_exhausted": True,
            "progress": "enough",
            "progress_assessment": {
                **state["progress_assessment"],
                "verdict": "enough",
                "reason": "replan_rejected",
            },
        }

    async def node_prepare_synthesis(self, state):
        self._call("prepare_synthesis")
        return {
            "synthesis_admission": True,
            "synthesis_mode": "emergency",
            "synthesis_admission_reason": "replan_exhausted",
            "progress": "ready_for_synthesis",
        }

    async def node_synthesize(self, state):
        self._call("synthesize")
        return {
            "task_status": {"t_synthesize_md": "done", "t_synthesize_pdf": "done"},
            "status": "synthesized",
            "final_content": "# 国内 AI 初创公司\n\nDeepSeek、Moonshot 与 MiniMax 均为高潜力候选。",
            "progress": "synthesized",
        }

    async def node_quality_gate(self, state):
        self._call("quality")
        return {
            "quality_passed": False,
            "quality_reason": "no_content",
            "quality_repairable": False,
            "quality_repair_action": "replan",
            "quality_attempts": 1,
            "replan_exhausted": True,
            "progress": "quality",
        }

    async def node_repair_synthesis(self, state):
        self._call("repair")
        return {"progress": "repair_synthesis"}

    async def node_finalize(self, state):
        self._call("finalize")
        report = str(state.get("final_content") or "")
        loop_state = LoopState(session_id="replan-e2e")
        loop_state.intent = TaskIntent.from_dict(state["intent"])
        loop_state.final_content = report
        loop_state.step_results = [
            StepResult(step_type="generate_markdown", content=report),
            StepResult(step_type="convert_pdf", content="pdf"),
        ]
        written = ensure_requested_deliverables(self.deliverable_dir, loop_state)
        self.pdf_path = written["pdf"]
        return {
            "status": "partial",
            "final_content": report,
            "artifacts": [str(self.pdf_path)] if self.pdf_path else [],
            "progress": "done",
        }

    async def node_abort(self, state):
        self._call("abort")
        return {"status": "aborted", "abort_reason": "aborted"}


def test_example_pdf_query_converges_after_rejected_replan(tmp_path: Path) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    runtime = ConvergenceRuntime(tmp_path)
    graph = compile_research_graph(
        checkpointer=InMemorySaver(),
        runtime=runtime,
        profile="agent",
    )
    result = asyncio.run(
        graph.ainvoke(
            empty_research_state(
                run_id="run-replan-e2e",
                session_id="session-replan-e2e",
                task_query=EXAMPLE_QUERY,
                max_replan_count=1,
            ),
            config={
                "configurable": {"thread_id": "session-replan-e2e"},
                "recursion_limit": 20,
            },
        )
    )
    assert runtime.replan_calls == 1
    assert runtime.progress_calls == 2
    assert runtime.node_calls < 20
    assert result["status"] == "partial"
    assert runtime.pdf_path is not None and runtime.pdf_path.exists()
    assert runtime.pdf_path.read_bytes()[:4] == b"%PDF"
    assert "DeepSeek" in result["final_content"]

"""Release invariants for the research runtime stabilization SDD."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage
from app.agent.harness.artifacts import ArtifactStore, reset_artifact_store, set_artifact_store
from app.agent.harness.run_budget import RunBudgetManager
from app.agent.harness.state import LoopState, PlanStep
from app.research.brief.compiler import compile_structured_brief
from app.research.control.terminal_policy import terminal_update
from app.research.coverage.judge import CoverageJudge, judge_coverage
from app.research.execution.worker_executor import WorkerExecutorV2
from app.research.runtime import runner as runner_module
from app.research.runtime.admission import admit_dispatch
from app.research.runtime.ingestion import ingest_new_worker_results
from app.research.runtime.state import empty_research_state
from app.research.runtime.task_identity import semantic_fingerprint
from app.research.runtime.worker import ResearchContext, ResearchTask
from app.research.routing.mode_router import budget_for_mode
from app.research.supervisor.models import ResearchTaskRequest


class FakeConfig:
    max_parallel_workers = 3
    synthesis_step_timeout_sec = 10


class FakeHarness:
    harness_config = FakeConfig()


class RaisingAgent:
    async def ainvoke(self, *args: Any, **kwargs: Any):
        raise RuntimeError("provider unavailable")


class TimeoutThenAnswer:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, *args: Any, **kwargs: Any):
        self.calls += 1
        if self.calls == 1:
            raise asyncio.TimeoutError()
        return AIMessage(
            content="Company A has recent funding evidence.",
            usage_metadata={"input_tokens": 320, "output_tokens": 35, "total_tokens": 355},
        )


def _budget_manager() -> RunBudgetManager:
    return RunBudgetManager(token_limit=100_000, llm_call_limit=100)


def _run_session(manager: RunBudgetManager, harness: FakeHarness | None = None):
    loop = LoopState(session_id="runtime-stabilization", run_id="runtime-stabilization")
    ctx = SimpleNamespace(
        state=loop,
        session_id=loop.session_id,
        run_id=loop.run_id,
        lock=SimpleNamespace(),
        budget_manager=manager,
        citation_manager=None,
        user_id="me",
        tenant_id="local",
        project_id="Inbox",
        run_started=0.0,
    )
    return runner_module.RunSession(harness or FakeHarness(), ctx)


def _synthesis_state() -> dict[str, Any]:
    brief = compile_structured_brief("Evaluate Company A for an AI startup role.")
    state = empty_research_state(
        run_id="runtime-stabilization",
        session_id="runtime-stabilization",
        task_query="Evaluate Company A for an AI startup role.",
    )
    state.update(
        {
            "phase": "coverage_judge",
            "brief": brief.to_dict(),
            "coverage_judgement": {
                "sufficient": False,
                "status": "gap",
                "missing": ["candidate-level evidence"],
            },
            "control_decision": {"action": "deliver_partial"},
            "evidence_records": [
                {
                    "evidence_id": "evidence_company_a",
                    "source_id": "example.com",
                    "source_kind": "web",
                    "locator": "https://example.com/company-a",
                    "authority_score": 0.8,
                    "excerpt_ref": "Company A has recent funding evidence.",
                }
            ],
            "claims": [
                {
                    "claim_id": "claim_company_a",
                    "text": "Company A has recent funding evidence.",
                    "evidence_ids": ["evidence_company_a"],
                }
            ],
            "findings": [
                {
                    "finding_id": "finding_company_a",
                    "task_id": "task_000_company_a",
                    "summary": "Company A has recent funding evidence.",
                    "claims": ["Company A has recent funding evidence."],
                    "evidence_ids": ["evidence_company_a"],
                }
            ],
        }
    )
    return state


def test_worker_budget_stop_preserves_recovered_evidence():
    store = ArtifactStore()
    set_artifact_store(store)
    try:
        store.put(
            "Company A raised a new funding round.",
            kind="web",
            locator="https://example.com/company-a",
            title="Company A profile",
            metadata={"run_id": "runtime-stabilization", "task_id": "task_budget"},
            step_index=2,
            step_type="research",
        )
        result = WorkerExecutorV2(None, None)._salvage_or_fail(
            ResearchTask(
                task_id="task_budget",
                objective="Collect Company A funding evidence",
                step_type="research",
                step_index=2,
            ),
            ResearchContext(
                run_id="runtime-stabilization",
                query="Collect Company A funding evidence",
            ),
            PlanStep(
                step_type="research",
                task_id="task_budget",
                description="Collect Company A funding evidence",
                objective="Collect Company A funding evidence",
            ),
            2,
            0.0,
            cause="budget_blocked:research_phase_token_cap",
            fail_reason="research_phase_token_cap",
            status="blocked",
            ok=False,
        )
        assert result.ok is False
        assert result.status == "partial"
        assert result.fail_reason == "research_phase_token_cap"
        assert result.evidence_refs
        assert result.findings
    finally:
        reset_artifact_store()


async def test_coverage_llm_failure_stays_gap_without_delta():
    brief = compile_structured_brief("Evaluate Company A for an AI startup role.")
    previous = judge_coverage(brief, [])
    assert previous.sufficient is False

    judgement = await CoverageJudge(RaisingAgent(), _budget_manager()).evaluate(
        brief,
        [],
        previous=previous,
    )
    assert judgement.sufficient is False
    assert judgement.status == "gap"
    assert judgement.source == "deterministic_fail_closed"


def test_duplicate_supervisor_task_is_rejected():
    request = ResearchTaskRequest(
        objective="Collect Company A funding evidence",
        target_gaps=("company-level funding",),
    )
    fingerprint = semantic_fingerprint(
        objective=request.objective,
        target_gaps=request.target_gaps,
        target_criteria=request.target_criteria,
    )
    admission = admit_dispatch(
        [request],
        wave_id=2,
        budget_manager=_budget_manager(),
        state={
            "budget": {"max_parallel_workers": 2},
            "task_fingerprints": {fingerprint: "task_001_previous"},
        },
    )
    assert admission.approved == ()
    assert admission.approved_count == 0
    assert admission.denied_reason == {"request_0": "duplicate_semantic_task"}


def test_dispatch_admission_counts_reserved_llm_calls():
    manager = SimpleNamespace(
        max_parallel_workers=2,
        remaining_for_research_tokens=lambda: 100_000,
        research_allowed=lambda: (True, ""),
        remaining_for_research_sec=lambda: 1_000.0,
        snapshot=lambda: SimpleNamespace(
            llm_calls=9,
            reserved_llm_calls=1,
            llm_call_limit=10,
        ),
    )
    admission = admit_dispatch(
        [ResearchTaskRequest(objective="Collect new Company B evidence")],
        wave_id=1,
        budget_manager=manager,
        state={"budget": {"max_parallel_workers": 2}},
    )
    assert admission.approved == ()
    assert admission.denied_reason == {"request_0": "run_llm_call_cap"}


async def test_low_synthesis_budget_skips_llm_and_renders_partial(monkeypatch):
    manager = _budget_manager()
    manager.commit_tokens(95_001)
    harness = FakeHarness()
    harness.synthesis_model = RaisingAgent()
    session = _run_session(manager, harness)
    monkeypatch.setattr(runner_module, "get_session", lambda _run_id: session)

    update = await runner_module.ResearchGraphRunner(harness).node_synthesize(_synthesis_state())
    assert manager.remaining_for_synthesis_tokens() < 1_000
    assert update["final_content"].strip()
    assert update["synthesis_failed"] is True
    assert session.state.metadata["synthesis_budget_low"] is True
    assert session.state.metadata["synthesis_fail_reason"] == "synthesis_budget_low"
    assert "Company A has recent funding evidence." in update["final_content"]
    assert "evidence_company_a" not in update["final_content"]


async def test_synthesis_failure_with_evidence_returns_user_readable_partial(monkeypatch):
    manager = _budget_manager()
    harness = FakeHarness()
    harness.synthesis_model = RaisingAgent()
    session = _run_session(manager, harness)
    monkeypatch.setattr(runner_module, "get_session", lambda _run_id: session)

    update = await runner_module.ResearchGraphRunner(harness).node_synthesize(_synthesis_state())
    assert update["final_content"].strip()
    assert update["synthesis_failed"] is True
    assert session.state.metadata["fallback_used"] == "deterministic_partial"
    assert session.state.metadata["synthesis_fail_reason"] == "provider_unavailable"
    assert "Company A has recent funding evidence." in update["final_content"]
    assert "evidence_company_a" not in update["final_content"]


async def test_quality_gate_evaluates_final_delivery_not_worker_history(monkeypatch):
    harness = FakeHarness()
    session = _run_session(_budget_manager(), harness)
    monkeypatch.setattr(runner_module, "get_session", lambda _run_id: session)
    state = _synthesis_state()
    state.update(
        {
            "phase": "synthesis",
            "final_content": "Company A has recent funding evidence.",
            "coverage_judgement": {"sufficient": True, "status": "sufficient"},
            "synthesis_failed": True,
            "synthesis_attempts": 2,
            "synthesis_degraded": True,
            "worker_results": [
                {"task_id": "task_partial", "status": "partial", "fail_reason": "worker_timeout"}
            ],
        }
    )

    update = await runner_module.ResearchGraphRunner(harness).node_quality_gate(state)

    assert update["quality_assessment"]["verdict"] == "pass"
    assert update["quality_assessment"]["issues"] == []
    assert update["quality_assessment"]["grounding"] is True


async def test_primary_timeout_compact_retry_is_visible_and_grounded(monkeypatch):
    harness = FakeHarness()
    harness.synthesis_model = TimeoutThenAnswer()
    session = _run_session(_budget_manager(), harness)
    monkeypatch.setattr(runner_module, "get_session", lambda _run_id: session)
    state = _synthesis_state()
    state["coverage_judgement"] = {"sufficient": True, "status": "sufficient"}
    state["control_decision"] = {"action": "synthesize"}

    update = await runner_module.ResearchGraphRunner(harness).node_synthesize(state)
    metadata = session.state.metadata
    assert harness.synthesis_model.calls == 2
    assert update["synthesis_failed"] is False
    assert update["synthesis_degraded"] is True
    assert metadata["synthesis_degraded"] is True
    assert metadata["synthesis_retry_count"] == 1
    assert metadata["successful_attempt"] == 2
    assert metadata["first_attempt_reason"] == "synthesis_timeout"
    assert metadata["normal_pack_tokens"] == 8_000
    assert metadata["successful_pack_tokens"] == 4_000
    assert [row["attempt"] for row in metadata["synthesis_attempt_metrics"]] == [1, 2]
    assert metadata["synthesis_attempt_metrics"][0]["fail_reason"] == "synthesis_timeout"
    assert metadata["synthesis_attempt_metrics"][1]["actual_output_tokens"] == 35

    quality_state = {**state, **update}
    quality = await runner_module.ResearchGraphRunner(harness).node_quality_gate(quality_state)
    assert quality["quality_assessment"]["verdict"] == "pass"
    assert quality["quality_assessment"]["grounding"] is True


def test_deep_debug_primary_synthesis_timeout_honors_explicit_env(monkeypatch):
    session = _run_session(_budget_manager())
    session.state.metadata["run_budget"] = {"synthesis_step_timeout_sec": 300}
    monkeypatch.delenv("HARNESS_SYNTHESIS_STEP_TIMEOUT_SEC", raising=False)
    assert session.synthesis_timeout_sec() == 180
    monkeypatch.setenv("HARNESS_SYNTHESIS_STEP_TIMEOUT_SEC", "120")
    assert session.synthesis_timeout_sec() == 120
    monkeypatch.setenv("HARNESS_SYNTHESIS_STEP_TIMEOUT_SEC", "invalid")
    assert session.synthesis_timeout_sec() == 180
    assert budget_for_mode("deep_debug", {"experiment": {"deep_debug": {}}})[
        "synthesis_step_timeout_sec"
    ] == 180


def test_no_evidence_terminates_as_failure_with_exact_reason():
    update = terminal_update(
        {"quality_assessment": {"verdict": "fail"}, "evidence_records": []},
        reason="",
        stage="finalize",
    )
    assert update["termination"]["runtime_status"] == "finished"
    assert update["termination"]["outcome"] == "failed"
    assert update["termination"]["reason"] == "NO_USABLE_EVIDENCE"


def test_worker_result_replay_is_idempotent():
    state = empty_research_state(
        run_id="runtime-stabilization",
        session_id="runtime-stabilization",
        task_query="Evaluate Company A for an AI startup role.",
    )
    state["dispatch_wave_id"] = 0
    state["worker_results"] = [
        {
            "task_id": "task_000_company_a",
            "dispatch_wave_id": 0,
            "status": "partial",
            "summary": "Recovered Company A evidence",
            "payload": {
                "summary": "Recovered Company A evidence",
                "facts": ["Tesla revenue 10亿美元"],
                "sources": ["https://example.com/company-a"],
                "search_queries": ["Company A funding"],
            },
        }
    ]
    first = ingest_new_worker_results(state)
    assert first
    assert len(first["evidence_records"]) == 1
    assert len(first["claims"]) == 1
    assert len(first["findings"]) == 1

    state["processed_worker_result_ids"] = list(first["processed_worker_result_ids"])
    state["evidence_records"] = list(first["evidence_records"])
    state["evidence_refs"] = list(first["evidence_refs"])
    state["claims"] = list(first["claims"])
    state["claim_conflicts"] = list(first["claim_conflicts"])
    state["claim_resolutions"] = list(first["claim_resolutions"])
    state["findings"] = list(first["findings"])
    state["search_query_fingerprints"] = list(first["search_query_fingerprints"])
    state["research_value_signal"] = first["research_value_signal"]

    assert ingest_new_worker_results(state) == {}


def test_worker_leases_do_not_reserve_unspent_token_ceilings():
    manager = RunBudgetManager(
        token_limit=100_000,
        llm_call_limit=100,
        max_parallel_workers=4,
    )
    first, first_reason = manager.reserve_worker_lease("task_a", parallel_workers=2)
    second, second_reason = manager.reserve_worker_lease("task_b", parallel_workers=2)
    assert first and second
    assert not first_reason and not second_reason
    fair_share = manager.phase_plan.research_cap_tokens(100_000) // 2
    assert manager._worker_leases[first].token_ceiling == fair_share
    assert manager._worker_leases[second].token_ceiling == fair_share
    assert manager.snapshot().active_worker_leases == 2
    third, third_reason = manager.reserve_worker_lease("task_c", parallel_workers=2)
    assert third
    assert not third_reason
    assert manager.snapshot().reserved_tokens == 0

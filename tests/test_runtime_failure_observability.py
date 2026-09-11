"""Runtime failure contracts: budgets, telemetry, and user-facing recovery."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel

import app.observability as observability
from app.agent.harness.state import StepResult
from app.agent.harness.step_budget import (
    consume_search_queries_or_block,
    worker_retrieval_budget,
)
from app.agent.harness.tool_contract import wrap_tool_with_contract
from app.observability.recorder import AgentTelemetry
from app.research.delivery.partial_renderer import render_partial_delivery
from app.research.delivery.synthesis_context import EvidenceDigest
from app.research.execution.structured_llm_gateway import emit_semantic_fallback
from app.research.execution.worker_executor import WorkerExecutorV2
from app.research.runtime.ingestion import ingest_new_worker_results
from app.research.runtime.worker import ResearchContext, ResearchTask
from app.tools.batch_retrieval import run_batch_fetch, run_batch_search


def _events(
    recorder: AgentTelemetry,
    session_id: str,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    return [
        event.to_dict()
        for event in recorder.journal.events_for_run(session_id, run_id or session_id)
    ]


def test_batch_search_then_batch_fetch_share_worker_scope_but_not_resource_budgets():
    def fake_search(query: str, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "query": query, "results": []}

    def fake_fetch(url: str, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "url": url, "title": url, "artifact_id": f"art-{url}"}

    with (
        patch("app.tools.batch_retrieval.search_internet", side_effect=fake_search),
        patch("app.tools.batch_retrieval.fetch_url_content", side_effect=fake_fetch),
        worker_retrieval_budget(
            search_queries=4,
            fetch_sources=8,
            tool_invocations=6,
        ) as budget,
    ):
        search = run_batch_search(["q1", "q2", "q3", "q4"])
        fetch = run_batch_fetch(["https://a.example", "https://b.example", "https://c.example"])

    assert search["ok"] is True
    assert fetch["ok"] is True
    assert budget.search_queries_used == 4
    assert budget.fetch_sources_used == 3
    assert budget.tool_invocations_used == 2


def test_budget_denial_event_contains_scope_resource_and_snapshot(monkeypatch):
    recorder = AgentTelemetry()
    recorder._ws_enabled = False
    monkeypatch.setattr(observability, "get_recorder", lambda: recorder)
    recorder.start_run(session_id="budget-denial", run_id="budget-denial")
    try:
        with worker_retrieval_budget(
            search_queries=1,
            fetch_sources=1,
            tool_invocations=1,
        ):
            consume_search_queries_or_block(2)
    finally:
        recorder.finish_run(status="failed", duration_ms=1)

    events = _events(recorder, "budget-denial")
    denials = [event for event in events if event["type"] == "budget.denied"]
    assert len(denials) == 1
    attributes = denials[0]["attributes"]
    assert attributes["scope"] == "worker"
    assert attributes["resource"] == "search_query"
    assert attributes["reason"] == "search_query_cap"
    assert attributes["used"] == 0
    assert attributes["limit"] == 1


def test_tool_wrapper_preserves_raw_payload_and_emits_private_telemetry(monkeypatch):
    class BatchFetchArgs(BaseModel):
        query: str
        urls: list[str]

    class FakeTool:
        args_schema = BatchFetchArgs

        def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "query": payload["query"],
                "results": [{"url": payload["urls"][0], "content": "body"}],
            }

    raw_payload = {
        "query": "SECRET_QUERY",
        "urls": ["https://secret.example", "https://other.example"],
    }
    recorder = AgentTelemetry()
    recorder._ws_enabled = False
    monkeypatch.setattr(observability, "get_recorder", lambda: recorder)
    recorder.start_run(session_id="tool-contract", run_id="tool-contract")
    try:
        wrapped = wrap_tool_with_contract(
            FakeTool(),
            tool_name="batch_fetch",
            apply_output_contract=False,
        )
        output = wrapped.invoke(raw_payload)
    finally:
        recorder.finish_run(status="success", duration_ms=1)

    assert output == {
        "query": "SECRET_QUERY",
        "results": [{"url": "https://secret.example", "content": "body"}],
    }
    events = _events(recorder, "tool-contract")
    assert [event["type"] for event in events if event["type"].startswith("tool.")] == [
        "tool.started",
        "tool.completed",
    ]
    started = next(event for event in events if event["type"] == "tool.started")
    assert started["attributes"]["args_meta"] == {
        "arg_names": ["query", "urls"],
        "item_count": 2,
    }
    assert "SECRET_QUERY" not in json.dumps(events, ensure_ascii=False)


def test_semantic_fallback_event_keeps_error_diagnosis(monkeypatch):
    class Schema:
        pass

    recorder = AgentTelemetry()
    recorder._ws_enabled = False
    monkeypatch.setattr(observability, "get_recorder", lambda: recorder)
    recorder.start_run(session_id="semantic-fallback", run_id="semantic-fallback")
    try:
        emit_semantic_fallback(
            phase="supervisor",
            component="supervisor",
            fallback="deterministic",
            exc=ValueError("unsupported structured output"),
            schema=Schema,
            model=SimpleNamespace(model_name="test-model"),
            started=__import__("time").perf_counter(),
        )
    finally:
        recorder.finish_run(status="partial", duration_ms=1)

    events = _events(recorder, "semantic-fallback")
    fallbacks = [event for event in events if event["type"] == "semantic.fallback"]
    assert len(fallbacks) == 1
    attributes = fallbacks[0]["attributes"]
    assert attributes["error_type"] == "ValueError"
    assert attributes["error_message"] == "unsupported structured output"
    assert attributes["error_category"] == "structured_output"
    assert attributes["model"] == "test-model"


def test_ingestion_prefers_recovered_findings_over_runtime_failure_summary():
    state = {
        "dispatch_wave_id": 0,
        "plan": {
            "steps": [
                {
                    "task_id": "task-recovered",
                    "metadata": {"target_criteria": ["回答直接对齐用户目标。"]},
                }
            ]
        },
        "worker_results": [
            {
                "task_id": "task-recovered",
                "attempt": 1,
                "dispatch_wave_id": 0,
                "summary": "worker_timeout; recovered evidence from existing artifacts",
                "payload": {
                    "summary": "worker_timeout; recovered evidence from existing artifacts",
                    "sources": ["https://example.com/source"],
                    "evidence_ids": ["evidence-recovered"],
                    "facts": ["Recovered evidence supports this fact."],
                    "findings": [
                        {
                            "task_id": "task-recovered",
                            "summary": "Recovered evidence supports this fact.",
                            "evidence_ids": ["evidence-recovered"],
                        }
                    ],
                },
            }
        ],
    }

    update = ingest_new_worker_results(state)

    assert update["findings"]
    claims = [str(finding.get("claim") or finding.get("summary")) for finding in update["findings"]]
    assert claims == ["Recovered evidence supports this fact."]
    assert update["findings"][0]["supported_criteria"] == ["回答直接对齐用户目标。"]
    assert update["findings"][0]["claims"] == ["Recovered evidence supports this fact."]
    assert all("worker_timeout" not in claim for claim in claims)


def test_partial_renderer_filters_embedded_runtime_codes():
    content = render_partial_delivery(
        objective="评估公司",
        findings=[
            {"claim": "worker_timeout; recovered evidence", "evidence_ids": ["ev-1"]},
            {"claim": "Company A has verifiable evidence.", "evidence_ids": ["ev-1"]},
        ],
        evidence_digests=[
            EvidenceDigest("ev-1", "Source", "https://example.com", "verified excerpt")
        ],
        worker_summaries=[],
        semantic_gaps=[],
        limitations=[],
        unresolved_conflicts=[],
        worker_failure_reasons=["worker_timeout"],
        synthesis_failure_reason="synthesis_timeout",
    )
    assert "Company A has verifiable evidence." in content
    assert "worker_timeout" not in content
    assert "synthesis_timeout" not in content
    assert "降级部分交付" in content


def test_worker_success_event_carries_budget_snapshot(monkeypatch):
    class FakeRecorder:
        is_active = True

        def __init__(self):
            self.events: list[tuple[str, dict[str, Any]]] = []

        def start_span(self, *args: Any, **kwargs: Any) -> str:
            return "span"

        def emit(self, event_type: str, **kwargs: Any):
            self.events.append((str(event_type), kwargs))

        def end_span(self, *args: Any, **kwargs: Any) -> None:
            return None

    class FakeBudgetManager:
        def reserve_worker_lease(self, *args: Any, **kwargs: Any):
            return "lease", ""

        def release_worker_lease(self, *args: Any, **kwargs: Any) -> None:
            return None

        def worker_lease_snapshot(self, lease_id: str):
            assert lease_id == "lease"
            return {
                "llm_calls_used": 1,
                "llm_calls_limit": 3,
                "tokens_used": 120,
                "token_limit": 12_000,
            }

    class FakeHarness:
        harness_config = SimpleNamespace(
            step_timeout_sec=10,
            max_step_tool_calls=8,
            max_tool_calls=20,
        )

        def _enrich_worker_result(self, _step: Any, result: Any, _state: Any):
            return result

    recorder = FakeRecorder()
    monkeypatch.setattr(observability, "get_recorder", lambda: recorder)
    step = SimpleNamespace(
        step_type="network_search",
        description="search",
        objective="search",
        subagent="",
        allowed_tools=[],
        metadata={
            "simple_fact_fast_path": True,
            "max_search_queries": 4,
            "max_fetch_sources": 8,
            "max_tool_invocations": 6,
        },
    )
    state = SimpleNamespace(plan=SimpleNamespace(steps=[step]), tool_calls_count=0)
    session = SimpleNamespace(
        state=state,
        budget_manager=FakeBudgetManager(),
        ctx=SimpleNamespace(),
        _resolve_max_workers=lambda: 1,
    )

    async def invoke(*args: Any, **kwargs: Any) -> StepResult:
        tool_usage = kwargs["tool_usage"]
        tool_usage["tool_calls"] = 1
        tool_usage["tools_invoked"] = ["internet_search"]
        tool_usage["budget"] = {
            "search_queries_used": 2,
            "search_queries_limit": 4,
            "fetch_sources_used": 0,
            "fetch_sources_limit": 8,
            "tool_invocations_used": 1,
            "tool_invocations_limit": 6,
        }
        return StepResult(
            step_type="network_search",
            content=json.dumps({"ok": True, "summary": "found", "evidence_ids": ["ev-1"]}),
            metadata={},
        )

    monkeypatch.setattr(WorkerExecutorV2, "_invoke_simple_fact", invoke)
    task = ResearchTask(
        task_id="task-budget",
        objective="query",
        step_type="network_search",
        step_index=0,
    )
    context = ResearchContext(run_id="run", query="query", session_id="session")
    result = asyncio.run(WorkerExecutorV2(FakeHarness(), session).execute(task, context))

    assert result.ok is True
    completed = next(event for event in recorder.events if event[0] == "worker.completed")
    budget = completed[1]["attributes"]["budget"]
    assert budget == {
        "llm_calls_used": 1,
        "llm_calls_limit": 3,
        "tokens_used": 120,
        "token_limit": 12_000,
        "search_queries_used": 2,
        "search_queries_limit": 4,
        "fetch_sources_used": 0,
        "fetch_sources_limit": 8,
        "tool_invocations_used": 1,
        "tool_invocations_limit": 6,
    }

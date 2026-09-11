"""Production-fidelity E2E for synthesis-provider failures.

These tests replace only the provider implementation. Graph, planner mode,
replan budget, token budget, phase shares, worker/synthesis timeout semantics,
and parallelism remain production-equivalent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from app.agent.harness.loop import AgentHarness
from app.agent.harness.run_budget import BudgetReservationError
from app.agent.harness.tool_contract import apply_tool_output_contract
from app.config.loader import get_harness_config, reload_harness_config
from app.observability import get_recorder
from app.observability.journal import summarize_trace
from app.research.execution import worker_executor as worker_executor_module
from tests.e2e.deterministic_landscape import CapturingToolGateway


QUERY = "你觉的当下国内AI初创有潜力值得加入的公司有哪些？为什么？输出结果为pdf"
CLOCK_JUMPS_REQUESTED = 0


def deterministic_search(**kwargs: Any) -> dict[str, Any]:
    query = str(kwargs.get("query") or "AI startup landscape")
    return {
        "query": query,
        "results": [
            {
                "title": "AI startup landscape",
                "url": "https://example.com/ai-startup-landscape",
                "content": "Candidate AI startups have recent funding and commercial signals.",
                "raw_content": "Candidate AI startups have recent funding and commercial signals.",
            }
        ],
    }


class ProductionFaultProvider:
    def __init__(
        self,
        *,
        synthesis_failure: str = "",
        worker_failure: str = "research_token_cap",
        context_retry_success: bool = True,
        worker_evidence: bool = True,
    ):
        self.synthesis_failure = synthesis_failure
        self.worker_failure = worker_failure
        self.context_retry_success = context_retry_success
        self.worker_evidence = worker_evidence
        self.synthesis_calls = 0

    async def astream(self, payload: dict[str, Any], config: dict[str, Any] | None = None):
        messages = list(payload.get("messages") or [])
        last_message = messages[-1]
        prompt = (
            str(last_message.get("content") or "")
            if isinstance(last_message, dict)
            else str(getattr(last_message, "content", "") or "")
        )
        if prompt.startswith("任务：") and "合成模式" in prompt:
            yield {"synthesis": {"messages": [await self._synthesis_message()]}}
            return

        gateway = CapturingToolGateway.current
        if gateway is None:
            yield {"planner": {"messages": [AIMessage(content="")]}}
            return
        if not self.worker_evidence:
            yield {
                "worker": {
                    "messages": [
                        AIMessage(
                            content=json.dumps(
                                {"ok": True, "summary": "No usable evidence.", "facts": [], "sources": []},
                                ensure_ascii=False,
                            )
                        )
                    ]
                }
            }
            return
        raw = gateway.call(deterministic_search, query="国内 AI 初创公司 全景 融资")
        contracted = apply_tool_output_contract(
            raw,
            tool_name="internet_search",
            step_type="network_search",
        )
        card = json.loads(contracted)["results"][0]
        yield {
            "worker": {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "internet_search",
                                "args": {"query": "国内 AI 初创公司 全景 融资"},
                                "id": "call-production-fidelity",
                            }
                        ],
                    )
                ]
            }
        }
        await asyncio.sleep(0.01)
        if self.worker_failure:
            raise BudgetReservationError(self.worker_failure)
        payload = {
            "ok": not bool(self.worker_failure),
            "summary": "Collected one landscape source.",
            "facts": ["Candidate AI startups have recent funding signals."],
            "sources": [card["url"]] if self.worker_evidence else [],
            "evidence_ids": [card["artifact_id"]] if self.worker_evidence else [],
            "artifact_ids": [card["artifact_id"]] if self.worker_evidence else [],
            "findings": (
                [
                    {
                        "finding_id": "finding_landscape",
                        "claim": "Candidate AI startups have recent funding signals.",
                        "evidence_ids": [card["artifact_id"]],
                    }
                ]
                if self.worker_evidence
                else []
            ),
        }
        if self.worker_failure:
            payload["error_code"] = self.worker_failure
        yield {"worker": {"messages": [AIMessage(content=json.dumps(payload, ensure_ascii=False))]}}

    async def ainvoke(self, payload: Any, config: dict[str, Any] | None = None):
        if isinstance(payload, list):
            prompt = str(getattr(payload[-1], "content", "") or "") if payload else ""
        else:
            messages = list(payload.get("messages") or [])
            last_message = messages[-1] if messages else None
            prompt = (
                str(last_message.get("content") or "")
                if isinstance(last_message, dict)
                else str(getattr(last_message, "content", "") or "")
            )
        if prompt.startswith("任务：") and "合成模式" in prompt:
            return await self._synthesis_message()
        raise RuntimeError("raw synthesis model must only receive synthesis prompts")

    async def _synthesis_message(self):
        self.synthesis_calls += 1
        failure = self.synthesis_failure
        if failure == "rate_limit":
            raise RuntimeError("429 rate limit")
        if failure == "timeout":
            global CLOCK_JUMPS_REQUESTED
            CLOCK_JUMPS_REQUESTED += 1
            await asyncio.Event().wait()
        if failure == "empty":
            return AIMessage(content="")
        if failure == "context":
            if self.synthesis_calls == 1:
                raise RuntimeError("context_length_exceeded")
            if not self.context_retry_success:
                raise RuntimeError("context_length_exceeded")
        if failure == "unavailable":
            raise RuntimeError("provider unavailable")
        if failure == "auth":
            raise RuntimeError("invalid auth")
        if failure == "budget":
            raise RuntimeError("run_token_cap")
        return AIMessage(
            content=(
                "# 国内 AI 初创公司部分评估\n\n"
                "- 已恢复的证据显示若干候选公司具有近期融资与商业化信号。[1]\n"
            )
        )


class VirtualTimeoutLoop(asyncio.SelectorEventLoop):
    """Replace only the clock; timeout duration and graph config stay production."""

    def __init__(self):
        super().__init__()
        self._virtual_time = super().time()

    def time(self) -> float:
        global CLOCK_JUMPS_REQUESTED
        real_time = super().time()
        if CLOCK_JUMPS_REQUESTED:
            CLOCK_JUMPS_REQUESTED -= 1
            self._virtual_time = max(self._virtual_time, real_time) + 1000.0
        else:
            self._virtual_time = max(self._virtual_time, real_time)
        return self._virtual_time


def _assert_production_config() -> None:
    config = get_harness_config()
    assert config.planner_llm_enabled is True
    assert config.max_replan_count == 3
    assert config.direct_worker_invoke is True
    assert config.max_total_tokens == 300000
    assert config.synthesis_step_timeout_sec == 60
    assert config.synthesis_retry_timeout_sec == 30


def _run(
    tmp_path: Path,
    session_id: str,
    provider: ProductionFaultProvider,
):
    config = get_harness_config()
    harness = AgentHarness(
        agent=provider,
        project_root=tmp_path,
        harness_config=config,
        synthesis_model=provider,
        workers={"research": provider, "network_search": provider, "web": provider},
    )
    return asyncio.run(harness.run(QUERY, session_id, mode="agent"))


def _run_with_virtual_timeout_clock(
    tmp_path: Path,
    session_id: str,
    provider: ProductionFaultProvider,
):
    global CLOCK_JUMPS_REQUESTED
    CLOCK_JUMPS_REQUESTED = 0
    config = get_harness_config()
    harness = AgentHarness(
        agent=provider,
        project_root=tmp_path,
        harness_config=config,
        synthesis_model=provider,
        workers={"research": provider, "network_search": provider, "web": provider},
    )
    loop = VirtualTimeoutLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(harness.run(QUERY, session_id, mode="agent"))
    finally:
        loop.close()
        asyncio.set_event_loop(None)
        CLOCK_JUMPS_REQUESTED = 0


def _trace(session_id: str, result: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_id = str(result.metadata.get("run_id") or "")
    events = [
        event.to_dict()
        for event in get_recorder().journal.events_for_run(session_id, run_id)
    ]
    return events, summarize_trace(events)


def _assert_common_invariants(result: Any, summary: dict[str, Any]) -> None:
    assert result.content.strip()
    assert result.metadata["termination"]["outcome"] in {"partial", "success"}
    integrity = summary["trace_integrity"]
    assert integrity["passed"] is True, integrity
    assert integrity["counts"]["worker_started"] == integrity["counts"]["worker_done"]
    assert integrity["span_tree"]["root_count"] >= 1
    assert integrity["span_tree"]["span_count"] >= 1
    assert integrity["lineage_edges"] > 0


def test_l3_research_cap_to_synthesis_success(tmp_path: Path, monkeypatch):
    reload_harness_config()
    _assert_production_config()
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    provider = ProductionFaultProvider(synthesis_failure="")
    result = _run(tmp_path, "l3-synthesis-success", provider)
    events, summary = _trace("l3-synthesis-success", result)
    _assert_common_invariants(result, summary)
    assert provider.synthesis_calls == 1
    assert any(event["type"] == "synthesis.completed" for event in events)


def test_l3_rate_limit_falls_back_to_partial_delivery(tmp_path: Path, monkeypatch):
    reload_harness_config()
    _assert_production_config()
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    provider = ProductionFaultProvider(synthesis_failure="rate_limit")
    result = _run(tmp_path, "l3-synthesis-rate-limit", provider)
    events, summary = _trace("l3-synthesis-rate-limit", result)
    _assert_common_invariants(result, summary)
    assert provider.synthesis_calls == 2
    failures = [event for event in events if event["type"] == "synthesis.failed"]
    assert len(failures) == 2
    assert all(event["attributes"]["fail_reason"] == "provider_rate_limit" for event in failures)
    assert result.metadata["fallback_used"] == "deterministic_partial"
    assert result.content.strip()


def test_l3_empty_content_falls_back_to_partial_delivery(tmp_path: Path, monkeypatch):
    reload_harness_config()
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    provider = ProductionFaultProvider(synthesis_failure="empty")
    result = _run(tmp_path, "l3-synthesis-empty", provider)
    events, summary = _trace("l3-synthesis-empty", result)
    _assert_common_invariants(result, summary)
    assert provider.synthesis_calls == 1
    assert all(
        event["attributes"]["fail_reason"] == "empty_content"
        for event in events
        if event["type"] == "synthesis.failed"
    )
    assert result.metadata["fallback_used"] == "deterministic_partial"


def test_l3_context_error_compacts_and_retries(tmp_path: Path, monkeypatch):
    reload_harness_config()
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    provider = ProductionFaultProvider(synthesis_failure="context", context_retry_success=True)
    result = _run(tmp_path, "l3-synthesis-context-retry", provider)
    events, summary = _trace("l3-synthesis-context-retry", result)
    _assert_common_invariants(result, summary)
    assert provider.synthesis_calls == 2
    failures = [event for event in events if event["type"] == "synthesis.failed"]
    assert len(failures) == 1
    assert failures[0]["attributes"]["fail_reason"] == "context_length_exceeded"
    assert failures[0]["attributes"]["fallback_action"] == "compact_retry"
    assert any(event["type"] == "synthesis.completed" for event in events)


def test_l3_context_error_falls_back_after_compact_failure(tmp_path: Path, monkeypatch):
    reload_harness_config()
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    provider = ProductionFaultProvider(synthesis_failure="context", context_retry_success=False)
    result = _run(tmp_path, "l3-synthesis-context-fallback", provider)
    events, summary = _trace("l3-synthesis-context-fallback", result)
    _assert_common_invariants(result, summary)
    assert provider.synthesis_calls == 2
    assert result.metadata["fallback_used"] == "deterministic_partial"


def test_l3_provider_unavailable_falls_back_to_partial_delivery(tmp_path: Path, monkeypatch):
    reload_harness_config()
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    provider = ProductionFaultProvider(synthesis_failure="unavailable")
    result = _run(tmp_path, "l3-synthesis-unavailable", provider)
    events, summary = _trace("l3-synthesis-unavailable", result)
    _assert_common_invariants(result, summary)
    assert provider.synthesis_calls == 2
    assert all(
        event["attributes"]["fail_reason"] == "provider_unavailable"
        for event in events
        if event["type"] == "synthesis.failed"
    )


def test_l3_provider_auth_falls_back_after_one_attempt(tmp_path: Path, monkeypatch):
    reload_harness_config()
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    provider = ProductionFaultProvider(synthesis_failure="auth")
    result = _run(tmp_path, "l3-synthesis-auth", provider)
    events, summary = _trace("l3-synthesis-auth", result)
    _assert_common_invariants(result, summary)
    assert provider.synthesis_calls == 1
    assert result.metadata["synthesis_fail_reason"] == "provider_auth"
    assert result.metadata["fallback_used"] == "deterministic_partial"


def test_l3_budget_exhausted_with_evidence_falls_back_to_partial_delivery(
    tmp_path: Path, monkeypatch
):
    reload_harness_config()
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    provider = ProductionFaultProvider(synthesis_failure="budget")
    result = _run(tmp_path, "l3-synthesis-budget", provider)
    events, summary = _trace("l3-synthesis-budget", result)
    _assert_common_invariants(result, summary)
    assert provider.synthesis_calls == 1
    assert result.metadata["synthesis_fail_reason"] == "run_token_cap"
    assert result.metadata["fallback_used"] == "deterministic_partial"


def test_l3_no_evidence_and_synthesis_failure_is_explicit_failed(
    tmp_path: Path, monkeypatch
):
    reload_harness_config()
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    provider = ProductionFaultProvider(
        synthesis_failure="rate_limit",
        worker_failure="",
        worker_evidence=False,
    )
    result = _run(tmp_path, "l3-synthesis-no-evidence", provider)
    events, summary = _trace("l3-synthesis-no-evidence", result)
    assert result.metadata["termination"]["outcome"] == "failed"
    assert result.metadata["fallback_used"] == ""
    assert "部分研究结果" not in result.content
    assert summary["trace_integrity"]["counts"]["worker_started"] == summary["trace_integrity"]["counts"]["worker_done"]
    assert not any(str(event["type"]).startswith("synthesis.") for event in events)


def test_l3_release_blocker_research_cap_synthesis_timeout_yields_partial(
    tmp_path: Path, monkeypatch
):
    reload_harness_config()
    _assert_production_config()
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    provider = ProductionFaultProvider(synthesis_failure="timeout")
    result = _run_with_virtual_timeout_clock(
        tmp_path,
        "l3-release-blocker-timeout",
        provider,
    )
    events, summary = _trace("l3-release-blocker-timeout", result)

    assert result.metadata["termination"]["outcome"] == "partial"
    assert result.content.strip()
    assert result.metadata["synthesis_attempts"] == 1
    assert result.metadata["synthesis_fail_reason"] == "synthesis_timeout"
    assert result.metadata["fallback_used"] == "deterministic_partial"
    assert result.content.strip()
    assert result.metadata["supervisor_iterations"] <= get_harness_config().max_replan_count
    assert not any("GraphRecursion" in str(event.get("error") or "") for event in events)

    integrity = summary["trace_integrity"]
    assert integrity["passed"] is True, integrity
    assert integrity["counts"]["worker_started"] == integrity["counts"]["worker_done"]
    assert integrity["span_tree"]["root_count"] >= 1
    assert integrity["lineage_edges"] > 0
    failures = [event for event in events if event["type"] == "synthesis.failed"]
    assert len(failures) == 1
    assert [event["attributes"]["fallback_action"] for event in failures] == [
        "deterministic_partial",
    ]
    worker_failures = [event for event in events if event["type"] == "worker.failed"]
    assert worker_failures
    assert all(
        event["attributes"].get("fail_reason") == "research_token_cap"
        for event in worker_failures
    )

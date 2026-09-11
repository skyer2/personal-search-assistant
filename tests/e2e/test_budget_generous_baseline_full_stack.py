"""Production-topology E2E for the generous per-worker budget baseline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from app.agent.harness.loop import AgentHarness
from app.config.loader import get_harness_config
from app.observability import get_recorder
from app.observability.journal import summarize_trace
from app.research.execution import worker_executor as worker_executor_module
from app.tools.batch_retrieval import run_batch_fetch, run_batch_search
from tests.e2e.deterministic_landscape import CapturingToolGateway


GOLDEN_QUERIES = (
    "2026年9月 Agent 最新的热点是什么？你觉得 Agent 未来1-2年的发展方向是什么？",
    "当下国内具身智能有哪些有潜力、值得加入的公司？为什么？",
)


def _fake_search(query: str, **_kwargs: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "query": query,
        "results": [
            {
                "title": f"Evidence for {query}",
                "url": f"https://example.com/{abs(hash(query))}",
                "content": "Deterministic evidence for the generous baseline query.",
                "raw_content": "Deterministic evidence for the generous baseline query.",
            }
        ],
    }


def _fake_fetch(url: str, **_kwargs: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "url": url,
        "title": "Fetched source",
        "content": "Fetched deterministic source content.",
        "artifact_id": f"art-{abs(hash(url))}",
    }


class GenerousBaselineAgent:
    async def ainvoke(self, payload: Any, config: dict[str, Any] | None = None):
        if isinstance(payload, list):
            prompt = str(getattr(payload[-1], "content", "") or "")
            if prompt.startswith("任务：") and "合成模式" in prompt:
                return AIMessage(
                    content=(
                        "# 研究结果\n\n"
                        "- 基于搜索、抓取与补充搜索证据，形成可追溯结论。[1]\n"
                    )
                )
        raise RuntimeError("raw synthesis model must only receive synthesis prompts")

    async def astream(self, payload: dict[str, Any], config: dict[str, Any] | None = None):
        messages = list(payload.get("messages") or [])
        last_message = messages[-1]
        prompt = (
            str(last_message.get("content") or "")
            if isinstance(last_message, dict)
            else str(getattr(last_message, "content", "") or "")
        )
        if prompt.startswith("任务：") and "合成模式" in prompt:
            yield {
                "synthesis": {
                    "messages": [
                        AIMessage(
                            content=(
                                "# 研究结果\n\n"
                                "- 基于搜索、抓取与补充搜索证据，形成可追溯结论。[1]\n"
                            )
                        )
                    ]
                }
            }
            return

        gateway = CapturingToolGateway.current
        if gateway is None:
            raise RuntimeError("worker tool gateway is not active")

        first_search = run_batch_search(
            ["agent trend 2026", "agent direction", "embodied ai company", "company funding"],
            max_results=1,
        )
        urls = [
            str(row.get("url") or "")
            for result in first_search.get("results") or []
            for row in result.get("results") or []
        ][:3]
        fetched = run_batch_fetch(urls)
        supplement = run_batch_search(["latest agent evidence", "company team evidence"], max_results=1)
        artifact_ids = [
            str(row.get("artifact_id") or "")
            for result in fetched.get("results") or []
            for row in result.get("results") or []
        ][:3]
        source_urls = [url for url in urls if url]

        assert first_search.get("ok") is True
        assert fetched.get("ok") is True
        assert supplement.get("ok") is True
        yield {
            "worker": {
                "messages": [
                    AIMessage(
                        content=json.dumps(
                            {
                                "ok": True,
                                "summary": "Completed search, fetch, and supplementary search.",
                                "facts": [
                                    "The query has deterministic search and fetched evidence.",
                                    "Supplementary search confirmed the initial evidence direction.",
                                ],
                                "sources": source_urls,
                                "evidence_ids": artifact_ids,
                                "artifact_ids": artifact_ids,
                            },
                            ensure_ascii=False,
                        )
                    )
                ]
            }
        }


def test_golden_queries_complete_workers_without_budget_caps(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(worker_executor_module, "ToolGateway", CapturingToolGateway)
    monkeypatch.setattr("app.tools.batch_retrieval.search_internet", _fake_search)
    monkeypatch.setattr("app.tools.batch_retrieval.fetch_url_content", _fake_fetch)

    config = get_harness_config()
    config.planner_llm_enabled = False
    config.max_replan_count = 0
    config.direct_worker_invoke = True
    agent = GenerousBaselineAgent()

    for index, query in enumerate(GOLDEN_QUERIES, start=1):
        harness = AgentHarness(
            agent=agent,
            project_root=tmp_path,
            harness_config=config,
            workers={"research": agent, "network_search": agent, "web": agent},
        )
        session_id = f"budget-golden-e2e-{index}"
        result = asyncio.run(harness.run(query, session_id, mode="agent"))
        run_id = str(result.metadata.get("run_id") or "")
        events = [
            event.to_dict()
            for event in get_recorder().journal.events_for_run(session_id, run_id)
        ]
        summary = summarize_trace(events)

        assert result.status in {"success", "partial"}
        assert result.content.strip()
        assert summary["trace_integrity"]["passed"] is True

        completed = [event for event in events if event["type"] == "worker.completed"]
        failed = [event for event in events if event["type"] == "worker.failed"]
        assert completed
        assert not failed
        assert not [
            event
            for event in events
            if event["type"] == "budget.denied"
            and str((event.get("attributes") or {}).get("reason") or "").endswith("_cap")
        ]

        for event in completed:
            budget = dict((event.get("attributes") or {}).get("budget") or {})
            assert budget["llm_calls_limit"] == 16
            assert budget["token_limit"] == 80_000
            assert budget["search_queries_used"] == 6
            assert budget["search_queries_limit"] == 10
            assert budget["fetch_sources_used"] == 3
            assert budget["fetch_sources_limit"] == 16
            assert budget["tool_invocations_used"] == 3
            assert budget["tool_invocations_limit"] == 16

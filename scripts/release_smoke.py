"""Release smoke gate for the research harness.

The gate has two layers:
1. L3 production-fidelity fault-injection tests (separate pytest process).
2. Deterministic release query suite with only provider implementations replaced.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from langchain_core.messages import AIMessage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.harness.loop import AgentHarness
from app.agent.harness.run_budget import BudgetReservationError
from app.agent.harness.tool_contract import apply_tool_output_contract
from app.agent.harness.token_counter import estimate_tokens
from app.config.loader import get_harness_config, reload_harness_config
from app.observability import get_recorder
from app.observability.journal import summarize_trace
from app.research.execution import worker_executor as worker_executor_module
from app.research.execution.tool_gateway import ToolGateway
import app.tools.tavily_tool as search_tool_module


RELEASE_QUERIES = [
    "你觉的当下国内AI初创有潜力值得加入的公司有哪些？为什么？",
    "比较 LangGraph、Temporal 和 Agent Harness 在 durable workflow 和 failure recovery 上的差异",
    "目前 agent 和多 agent 在什么场景真正落地效果好？哪些还不行？",
    "Transformer是哪一年提出的？",
]


class SmokeToolGateway(ToolGateway):
    current: SmokeToolGateway | None = None

    @contextmanager
    def execution_scope(
        self,
        *,
        worker_task_id: str = "",
        step_index: int = -1,
        run_id: str = "",
        session_id: str = "",
    ) -> Iterator[None]:
        previous = SmokeToolGateway.current
        SmokeToolGateway.current = self
        try:
            with super().execution_scope(
                worker_task_id=worker_task_id,
                step_index=step_index,
                run_id=run_id,
                session_id=session_id,
            ):
                yield
        finally:
            SmokeToolGateway.current = previous


class SmokeSearchProvider:
    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "")
        if "transformer" in query.lower():
            return {
                "query": query,
                "results": [
                    {
                        "title": "Attention Is All You Need",
                        "url": "https://arxiv.org/abs/1706.03762",
                        "content": "The Transformer was proposed in 2017.",
                        "raw_content": "The Transformer was proposed in 2017.",
                    }
                ],
            }
        return {
            "query": query,
            "results": [
                {
                    "title": "Deterministic research evidence",
                    "url": "https://example.com/research-evidence",
                    "content": "Deterministic evidence for the release smoke query.",
                    "raw_content": "Deterministic evidence for the release smoke query.",
                }
            ],
        }


class SmokeLLMProvider:
    def __init__(self):
        self.calls = 0
        self.estimated_tokens = 0

    async def astream(self, payload: dict[str, Any], config: dict[str, Any] | None = None):
        messages = list(payload.get("messages") or [])
        last_message = messages[-1]
        prompt = (
            str(last_message.get("content") or "")
            if isinstance(last_message, dict)
        else str(getattr(last_message, "content", "") or "")
        )
        self.calls += 1
        self.estimated_tokens += estimate_tokens(prompt)
        if prompt.startswith("任务：") and "合成模式" in prompt:
            yield {
                "synthesis": {
                    "messages": [
                        AIMessage(
                            content=(
                                "# 研究结果\n\n"
                                "- 基于已恢复证据，本次得到可追溯的部分结论。[1]\n"
                                "- 由于 research_token_cap，本结果属于部分交付。\n"
                            )
                        )
                    ]
                }
            }
            return

        gateway = SmokeToolGateway.current
        if gateway is None:
            yield {"planner": {"messages": [AIMessage(content="")]}}
            return
        raw = gateway.call(
            SmokeSearchProvider().invoke,
            {"query": prompt[:300], "max_results": 2, "include_raw_content": True},
        )
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
                                "args": {"query": prompt[:300]},
                                "id": f"release-smoke-{card['artifact_id']}",
                            }
                        ],
                    )
                ]
            }
        }
        await asyncio.sleep(0.01)
        raise BudgetReservationError("research_token_cap")


def assert_production_config() -> None:
    config = get_harness_config()
    expected = {
        "planner_llm_enabled": True,
        "max_replan_count": 3,
        "direct_worker_invoke": True,
        "max_total_tokens": 300000,
        "synthesis_step_timeout_sec": 240,
    }
    for key, value in expected.items():
        actual = getattr(config, key)
        if actual != value:
            raise RuntimeError(f"production config mismatch: {key}={actual!r}, expected={value!r}")


def run_l3_gate(output_dir: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/e2e/test_production_fidelity_synthesis.py",
        "-p",
        "no:cacheprovider",
        f"--basetemp={output_dir / 'pytest-l3'}",
    ]
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    print("[L3 Production-Fidelity Fault-Injection]")
    print(completed.stdout.strip())
    if completed.stderr.strip():
        print(completed.stderr.strip())
    if completed.returncode != 0:
        raise RuntimeError(f"L3 gate failed with exit code {completed.returncode}")


def _usage_value(usage: dict[str, Any], key: str) -> int:
    total = usage.get("total") if isinstance(usage.get("total"), dict) else {}
    return int(usage.get(key) or total.get(key) or 0)


def run_query(
    *,
    query: str,
    session_id: str,
    project_root: Path,
) -> tuple[dict[str, Any], Any]:
    started = time.perf_counter()
    provider = SmokeLLMProvider()
    harness = AgentHarness(
        agent=provider,
        project_root=project_root,
        harness_config=get_harness_config(),
        workers={"research": provider, "network_search": provider, "web": provider},
    )
    result = asyncio.run(harness.run(query, session_id, mode="agent"))
    duration_ms = int((time.perf_counter() - started) * 1000)
    run_id = str(result.metadata.get("run_id") or "")
    events = [
        event.to_dict()
        for event in get_recorder().journal.events_for_run(session_id, run_id)
    ]
    summary = summarize_trace(events)
    integrity = summary["trace_integrity"]
    usage = dict(result.metadata.get("usage") or {})
    row = {
        "query": query,
        "status": result.status,
        "outcome": result.metadata.get("termination", {}).get("outcome", ""),
        "duration_ms": duration_ms,
        "llm_calls": max(provider.calls, _usage_value(usage, "calls")),
        "tokens": max(provider.estimated_tokens, _usage_value(usage, "total_tokens")),
        "replans": int(result.metadata.get("replan_count") or 0),
        "workers": len(summary.get("workers") or []),
        "worker_started": int(integrity["counts"].get("worker_started") or 0),
        "worker_done": int(integrity["counts"].get("worker_done") or 0),
        "evidence": len(summary.get("evidence") or []),
        "synthesis_attempts": int(result.metadata.get("synthesis_attempts") or 0),
        "synthesis_failure": result.metadata.get("synthesis_fail_reason") or "",
        "fallback_used": result.metadata.get("fallback_used") or "",
        "trace_integrity": "PASS" if integrity.get("passed") else "FAIL",
        "span_roots": int(integrity.get("span_tree", {}).get("root_count") or 0),
        "lineage_edges": int(integrity.get("lineage_edges") or 0),
    }
    return row, result


def print_rows(rows: list[dict[str, Any]]) -> None:
    fields = [
        "query",
        "status",
        "outcome",
        "duration_ms",
        "llm_calls",
        "tokens",
        "replans",
        "workers",
        "worker_started",
        "worker_done",
        "evidence",
        "synthesis_attempts",
        "synthesis_failure",
        "fallback_used",
        "trace_integrity",
        "span_roots",
        "lineage_edges",
    ]
    print("\n[Release Query Report]")
    print(" | ".join(field.upper() for field in fields))
    for row in rows:
        print(" | ".join(str(row.get(field, "")) for field in fields))


def print_q1_report(rows: list[dict[str, Any]]) -> None:
    q1 = RELEASE_QUERIES[0]
    selected = [row for row in rows if row["query"] == q1]
    print("\n[Release Blocker Query Aggregate]")
    print(f"query: {q1}")
    print(f"runs: {len(selected)}")
    print(f"success: {sum(row['status'] == 'success' for row in selected)}")
    print(f"partial: {sum(row['status'] == 'partial' for row in selected)}")
    print(f"failed: {sum(row['status'] == 'failed' for row in selected)}")
    print(f"empty_output: {sum(not str(row.get('content', '')).strip() for row in selected)}")
    print(f"avg_tokens: {sum(row['tokens'] for row in selected) // max(1, len(selected))}")
    print(f"avg_duration_ms: {sum(row['duration_ms'] for row in selected) // max(1, len(selected))}")
    print(f"trace_failures: {sum(row['trace_integrity'] != 'PASS' for row in selected)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--q1-runs", type=int, default=3)
    parser.add_argument("--skip-l3", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "release_smoke")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    reload_harness_config()
    assert_production_config()
    if not args.skip_l3:
        run_l3_gate(args.output)

    rows_with_content: list[dict[str, Any]] = []
    original_tool_gateway = worker_executor_module.ToolGateway
    original_search_provider = search_tool_module.internet_search
    worker_executor_module.ToolGateway = SmokeToolGateway
    search_tool_module.internet_search = SmokeSearchProvider()
    try:
        query_plan = [(RELEASE_QUERIES[0], f"release-smoke-q1-{index}") for index in range(max(1, args.q1_runs))]
        query_plan.extend(
            (query, f"release-smoke-q{index}")
            for index, query in enumerate(RELEASE_QUERIES[1:], start=2)
        )
        for query, session_id in query_plan:
            row, result = run_query(
                query=query,
                session_id=session_id,
                project_root=args.output,
            )
            row["content"] = result.content
            rows_with_content.append(row)
    finally:
        worker_executor_module.ToolGateway = original_tool_gateway
        search_tool_module.internet_search = original_search_provider

    rows = [{key: value for key, value in row.items() if key != "content"} for row in rows_with_content]
    print_rows(rows)
    print_q1_report(rows_with_content)

    failures: list[str] = []
    for row in rows:
        if row["trace_integrity"] != "PASS":
            failures.append(f"trace integrity failed: {row['query']}")
        if row["worker_started"] != row["worker_done"]:
            failures.append(f"worker lifecycle mismatch: {row['query']}")
        if row["span_roots"] < 1:
            failures.append(f"missing root span: {row['query']}")
        if not str(next(item["content"] for item in rows_with_content if item["query"] == row["query"])).strip():
            failures.append(f"empty output: {row['query']}")
        if row["replans"] > get_harness_config().max_replan_count:
            failures.append(f"replan ceiling exceeded: {row['query']}")

    q1_rows = [row for row in rows_with_content if row["query"] == RELEASE_QUERIES[0]]
    if any(row["outcome"] not in {"partial", "success"} for row in q1_rows):
        failures.append("Q1 did not produce a partial/success outcome")
    q4 = next((row for row in rows_with_content if row["query"] == RELEASE_QUERIES[3]), None)
    if q4 is None or "2017" not in q4["content"]:
        failures.append("Q4 simple fact fast path did not answer 2017")

    report_path = args.output / "report.json"
    report_path.write_text(
        json.dumps({"rows": rows, "failures": failures}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if failures:
        print("\n[Release Smoke] FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("\n[Release Smoke] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

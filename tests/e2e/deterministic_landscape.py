"""Deterministic landscape worker/synthesis agents for full-stack E2E tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.messages import AIMessage

from app.agent.harness.tool_contract import apply_tool_output_contract
from app.research.execution.tool_gateway import ToolGateway


class CapturingToolGateway(ToolGateway):
    current: CapturingToolGateway | None = None

    def __init__(self, remaining_calls: int | None):
        super().__init__(remaining_calls)
        CapturingToolGateway.current = self


def deterministic_search(**kwargs: Any) -> dict[str, Any]:
    query = str(kwargs.get("query") or "AI startup landscape")
    return {
        "query": query,
        "results": [
            {
                "title": "AI startup landscape",
                "url": "https://example.com/ai-startup-landscape",
                "content": "Candidate AI startups and their recent funding milestones.",
                "raw_content": "Candidate AI startups and their recent funding milestones.",
            }
        ],
        "candidates": [
            {
                "name": "月之暗面",
                "aliases": ["Moonshot AI"],
                "confidence": 0.9,
                "selection_reason": "AI startup with recent funding and product signals",
            },
            {
                "name": "智谱AI",
                "aliases": ["Zhipu AI"],
                "confidence": 0.88,
                "selection_reason": "AI startup with commercialization signal",
            },
            {
                "name": "DeepSeek",
                "aliases": ["深度求索"],
                "confidence": 0.92,
                "selection_reason": "AI startup with technology and market signal",
            },
        ],
    }


class DeterministicAgent:
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
                                "# 国内 AI 初创公司部分评估\n\n"
                                "- 已恢复的检索证据显示若干候选公司具有近期融资与商业化信号。\n"
                                "- Worker 持续超时，Replan 已达到有界恢复上限，本次为降级部分交付。\n"
                            )
                        )
                    ]
                }
            }
            return

        gateway = CapturingToolGateway.current
        if gateway is None:
            raise RuntimeError("worker tool gateway is not active")
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
                                "id": "call-landscape",
                            }
                        ],
                    )
                ]
            }
        }
        await asyncio.sleep(0.2)
        yield {
            "worker": {
                "messages": [
                    AIMessage(
                        content=json.dumps(
                        {
                            "ok": True,
                            "summary": "Collected one landscape source.",
                            "facts": ["Candidate AI startups have recent funding signals."],
                            "sources": [card["url"]],
                            "evidence_ids": [card["artifact_id"]],
                            "artifact_ids": [card["artifact_id"]],
                            "candidates": [
                                {
                                    "name": "月之暗面",
                                    "aliases": ["Moonshot AI"],
                                    "confidence": 0.9,
                                    "evidence_ids": [card["artifact_id"]],
                                    "selection_reason": "AI startup with recent funding and product signals",
                                },
                                {
                                    "name": "智谱AI",
                                    "aliases": ["Zhipu AI"],
                                    "confidence": 0.88,
                                    "evidence_ids": [card["artifact_id"]],
                                    "selection_reason": "AI startup with commercialization signal",
                                },
                                {
                                    "name": "DeepSeek",
                                    "aliases": ["深度求索"],
                                    "confidence": 0.92,
                                    "evidence_ids": [card["artifact_id"]],
                                    "selection_reason": "AI startup with technology and market signal",
                                },
                            ],
                        },
                            ensure_ascii=False,
                        )
                    )
                ]
            }
        }

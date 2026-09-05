"""
【Phase 13】Harness 运行时护栏

企业生产：显式 Kill Switch — 工具次数 / token / 墙钟时限 / 重规划次数 / 计划步数。
默认 fail-closed：任一超限立即 ABORT，保留已完成步结果做 partial 交付。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agent.harness.state import LoopState
    from app.config.loader import HarnessConfig


class AbortReason:
    BUDGET_TOOL_CALLS = "budget_tool_calls"
    BUDGET_TOKENS = "budget_tokens"
    BUDGET_RETRIEVAL_UNITS = "budget_retrieval_units"
    DEADLINE = "deadline_exceeded"
    MAX_REPLAN = "max_replan"
    MAX_PLAN_STEPS = "max_plan_steps"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class GuardrailDecision:
    abort: bool
    reason: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "abort": self.abort,
            "reason": self.reason,
            "message": self.message,
        }


def evaluate_run_guardrails(
    state: "LoopState",
    config: "HarnessConfig",
    *,
    elapsed_sec: float,
    estimated_tokens: int,
) -> GuardrailDecision:
    """每步执行前评估护栏。先命中先返回。

    Adaptive research leases are deliberately ignored here. Only ``hard.*``
    ceilings may abort the run.
    """
    run_budget = {}
    meta = getattr(state, "metadata", None) or {}
    if isinstance(meta, dict):
        raw = meta.get("run_budget")
        if isinstance(raw, dict):
            run_budget = raw

    def _cap(key: str, attr: str, default: int = 0) -> int:
        if key in run_budget and run_budget[key] is not None:
            return int(run_budget[key])
        return int(getattr(config, attr, default) or default)

    max_tools = _cap("max_agent_actions", "max_tool_calls", 0)
    if max_tools > 0 and state.tool_calls_count >= max_tools:
        return GuardrailDecision(
            abort=True,
            reason=AbortReason.BUDGET_TOOL_CALLS,
            message=f"工具调用达到上限 {max_tools}",
        )

    hard_retrieval = _cap("hard_retrieval_units", "max_tool_calls", 0)
    retrieval_used = int(meta.get("retrieval_units_used") or 0)
    if hard_retrieval > 0 and retrieval_used >= hard_retrieval:
        return GuardrailDecision(
            abort=True,
            reason=AbortReason.BUDGET_RETRIEVAL_UNITS,
            message=f"检索资源达到硬上限 {hard_retrieval} units",
        )

    max_tokens = _cap("max_total_tokens", "max_total_tokens", 0)
    if max_tokens > 0 and estimated_tokens >= max_tokens:
        return GuardrailDecision(
            abort=True,
            reason=AbortReason.BUDGET_TOKENS,
            message=f"token 达到上限 {max_tokens}（含真实 LLM usage）",
        )

    max_llm = int(run_budget.get("max_llm_calls") or getattr(config, "max_llm_calls_per_run", 0) or 0)
    llm_used = 0
    if isinstance(meta, dict):
        llm_used = int(meta.get("llm_calls_used") or 0)
    if max_llm > 0 and llm_used >= max_llm:
        return GuardrailDecision(
            abort=True,
            reason=AbortReason.BUDGET_TOKENS,
            message=f"LLM 调用达到上限 {max_llm}",
        )

    max_run_sec = _cap("max_run_sec", "max_run_sec", 0)
    if max_run_sec > 0 and elapsed_sec >= max_run_sec:
        return GuardrailDecision(
            abort=True,
            reason=AbortReason.DEADLINE,
            message=f"任务墙钟时限 {max_run_sec}s 已到",
        )

    max_replan = _cap("max_replan_count", "max_replan_count", 0)
    if max_replan > 0 and state.replan_count >= max_replan:
        return GuardrailDecision(
            abort=True,
            reason=AbortReason.MAX_REPLAN,
            message=f"动态重规划达到上限 {max_replan}",
        )

    max_steps = _cap("max_plan_steps", "max_plan_steps", 0)
    plan_len = len(state.plan.steps) if state.plan else 0
    if max_steps > 0 and plan_len > max_steps:
        return GuardrailDecision(
            abort=True,
            reason=AbortReason.MAX_PLAN_STEPS,
            message=f"计划步数 {plan_len} 超过上限 {max_steps}",
        )

    return GuardrailDecision(abort=False)


def can_replan(state: "LoopState", config: "HarnessConfig") -> bool:
    """是否允许再插入动态重规划步。

    Retry（format/transient）与 Replan（semantic）预算独立：
    不再用 state.retry_count 消耗 semantic replan 权力。
    """
    if not getattr(config, "hitl_allow_replan", True):
        return False
    meta = getattr(state, "metadata", None) or {}
    if isinstance(meta, dict) and meta.get("force_synthesis"):
        return False
    run_budget = {}
    if isinstance(meta, dict) and isinstance(meta.get("run_budget"), dict):
        run_budget = meta["run_budget"]
    max_replan = int(
        run_budget.get("max_replan_count", getattr(config, "max_replan_count", 0) or 0)
    )
    if max_replan > 0 and state.replan_count >= max_replan:
        return False
    max_steps = int(
        run_budget.get("max_plan_steps", getattr(config, "max_plan_steps", 0) or 0)
    )
    plan_len = len(state.plan.steps) if state.plan else 0
    if max_steps > 0 and plan_len >= max_steps:
        return False
    return True

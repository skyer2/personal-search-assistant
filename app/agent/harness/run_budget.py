"""Run-level budget manager: hard ceiling that research cannot steal from synthesis.

Invocation-aware checks use UsageTracker real tokens when available.
Thread-safe for parallel workers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PhaseBudgetPlan:
    """Soft phase shares under the hard token ceiling (must sum ≤ 1.0)."""

    understand_plan: float = 0.05
    research: float = 0.55
    replan: float = 0.10
    synthesis: float = 0.25
    quality: float = 0.05

    def synthesis_reserve_tokens(self, total: int) -> int:
        return max(0, int(total * self.synthesis))

    def research_cap_tokens(self, total: int) -> int:
        # Research + replan share; synthesis+quality reserved
        return max(0, int(total * (self.research + self.replan)))


@dataclass
class RunBudgetSnapshot:
    token_limit: int
    used_tokens: int
    llm_calls: int
    llm_call_limit: int
    tool_calls: int
    tool_call_limit: int
    research_cap_tokens: int
    synthesis_reserve_tokens: int
    force_synthesis: bool
    deadline_sec: float
    elapsed_sec: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_limit": self.token_limit,
            "used_tokens": self.used_tokens,
            "llm_calls": self.llm_calls,
            "llm_call_limit": self.llm_call_limit,
            "tool_calls": self.tool_calls,
            "tool_call_limit": self.tool_call_limit,
            "research_cap_tokens": self.research_cap_tokens,
            "synthesis_reserve_tokens": self.synthesis_reserve_tokens,
            "force_synthesis": self.force_synthesis,
            "deadline_sec": self.deadline_sec,
            "elapsed_sec": self.elapsed_sec,
            "remaining_tokens": max(0, self.token_limit - self.used_tokens),
            "remaining_for_research": max(0, self.research_cap_tokens - self.used_tokens),
        }


class RunBudgetManager:
    """Atomic run budget. Research stops when research_cap hit; synthesis reserve protected."""

    def __init__(
        self,
        *,
        token_limit: int = 100_000,
        llm_call_limit: int = 30,
        tool_call_limit: int = 40,
        deadline_sec: float = 600.0,
        phase_plan: PhaseBudgetPlan | None = None,
        max_llm_calls_per_worker: int = 8,
    ) -> None:
        self.token_limit = max(0, int(token_limit or 0))
        self.llm_call_limit = max(0, int(llm_call_limit or 0))
        self.tool_call_limit = max(0, int(tool_call_limit or 0))
        self.deadline_sec = max(0.0, float(deadline_sec or 0))
        self.phase_plan = phase_plan or PhaseBudgetPlan()
        self.max_llm_calls_per_worker = max(1, int(max_llm_calls_per_worker or 8))
        self._lock = threading.RLock()
        self._started = time.perf_counter()
        self._used_tokens = 0
        self._llm_calls = 0
        self._tool_calls = 0
        self._force_synthesis = False

    @classmethod
    def from_config(cls, config: Any | None, *, run_budget: dict[str, Any] | None = None) -> "RunBudgetManager":
        rb = dict(run_budget or {})
        token_limit = int(
            rb.get("max_total_tokens")
            or getattr(config, "max_total_tokens", 100_000)
            or 100_000
        )
        tool_limit = int(
            rb.get("max_tool_calls")
            or getattr(config, "max_tool_calls", 40)
            or 40
        )
        deadline = float(
            rb.get("max_run_sec")
            or getattr(config, "max_run_sec", 600)
            or 600
        )
        llm_limit = int(
            rb.get("max_llm_calls")
            or getattr(config, "max_llm_calls_per_run", 30)
            or 30
        )
        per_worker = int(getattr(config, "max_llm_calls_per_worker", 8) or 8)
        return cls(
            token_limit=token_limit,
            llm_call_limit=llm_limit,
            tool_call_limit=tool_limit,
            deadline_sec=deadline,
            max_llm_calls_per_worker=per_worker,
        )

    def sync_from_usage(self, *, session_id: str = "", tool_calls: int = 0) -> None:
        """Pull real LLM usage into the manager."""
        used = 0
        calls = 0
        try:
            from app.agent.harness.usage_tracker import get_usage_tracker

            if session_id:
                summary = get_usage_tracker().session_summary(session_id)
                total = summary.get("total") or {}
                used = int(total.get("total_tokens") or 0)
                calls = int(total.get("calls") or 0)
        except Exception:
            pass
        with self._lock:
            self._used_tokens = max(self._used_tokens, used)
            self._llm_calls = max(self._llm_calls, calls)
            self._tool_calls = max(self._tool_calls, int(tool_calls or 0))
            self._maybe_force_synthesis_locked()

    def commit_tokens(self, tokens: int) -> None:
        with self._lock:
            self._used_tokens += max(0, int(tokens or 0))
            self._maybe_force_synthesis_locked()

    def note_llm_call(self) -> None:
        with self._lock:
            self._llm_calls += 1
            self._maybe_force_synthesis_locked()

    def note_tool_call(self) -> None:
        with self._lock:
            self._tool_calls += 1

    def _maybe_force_synthesis_locked(self) -> None:
        research_cap = self.phase_plan.research_cap_tokens(self.token_limit)
        if self.token_limit > 0 and self._used_tokens >= research_cap:
            self._force_synthesis = True
        if self.llm_call_limit > 0 and self._llm_calls >= self.llm_call_limit:
            self._force_synthesis = True

    def force_synthesis(self) -> bool:
        with self._lock:
            return self._force_synthesis

    def mark_force_synthesis(self) -> None:
        with self._lock:
            self._force_synthesis = True

    def remaining_run_sec(self) -> float:
        if self.deadline_sec <= 0:
            return 1e9
        return max(0.0, self.deadline_sec - (time.perf_counter() - self._started))

    def snapshot(self) -> RunBudgetSnapshot:
        with self._lock:
            return RunBudgetSnapshot(
                token_limit=self.token_limit,
                used_tokens=self._used_tokens,
                llm_calls=self._llm_calls,
                llm_call_limit=self.llm_call_limit,
                tool_calls=self._tool_calls,
                tool_call_limit=self.tool_call_limit,
                research_cap_tokens=self.phase_plan.research_cap_tokens(self.token_limit),
                synthesis_reserve_tokens=self.phase_plan.synthesis_reserve_tokens(self.token_limit),
                force_synthesis=self._force_synthesis,
                deadline_sec=self.deadline_sec,
                elapsed_sec=time.perf_counter() - self._started,
            )

    def research_allowed(self) -> tuple[bool, str]:
        snap = self.snapshot()
        if snap.force_synthesis:
            return False, "synthesis_reserve_protect"
        if snap.token_limit > 0 and snap.used_tokens >= snap.token_limit:
            return False, "budget_tokens"
        if snap.token_limit > 0 and snap.used_tokens >= snap.research_cap_tokens:
            return False, "research_token_cap"
        if snap.llm_call_limit > 0 and snap.llm_calls >= snap.llm_call_limit:
            return False, "budget_llm_calls"
        if snap.tool_call_limit > 0 and snap.tool_calls >= snap.tool_call_limit:
            return False, "budget_tool_calls"
        if self.deadline_sec > 0 and snap.elapsed_sec >= self.deadline_sec:
            return False, "deadline_exceeded"
        return True, ""


def get_or_create_run_budget(state: Any, config: Any | None) -> RunBudgetManager:
    meta = getattr(state, "metadata", None)
    if not isinstance(meta, dict):
        meta = {}
        try:
            state.metadata = meta
        except Exception:
            pass
    existing = meta.get("_run_budget_manager")
    if isinstance(existing, RunBudgetManager):
        return existing
    run_budget = meta.get("run_budget") if isinstance(meta.get("run_budget"), dict) else {}
    mgr = RunBudgetManager.from_config(config, run_budget=run_budget)
    meta["_run_budget_manager"] = mgr
    # Persist phase caps into run_budget for observability
    snap = mgr.snapshot()
    budget = dict(run_budget)
    budget.setdefault("max_total_tokens", snap.token_limit)
    budget["research_cap_tokens"] = snap.research_cap_tokens
    budget["synthesis_reserve_tokens"] = snap.synthesis_reserve_tokens
    budget["max_llm_calls"] = snap.llm_call_limit
    meta["run_budget"] = budget
    return mgr

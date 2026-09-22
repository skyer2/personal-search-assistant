"""Absolute run deadline, phase budgets, and atomic resource reservations."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class PhaseBudgetPlan:
    """Soft phase shares under the hard token ceiling (must sum ≤ 1.0)."""

    understand_plan: float = 0.05
    research: float = 0.55
    supervisor: float = 0.10
    synthesis: float = 0.25
    quality: float = 0.05

    def synthesis_reserve_tokens(self, total: int) -> int:
        return max(0, int(total * self.synthesis))

    def research_cap_tokens(self, total: int) -> int:
        return max(0, int(total * (self.research + self.supervisor)))

    def quality_reserve_tokens(self, total: int) -> int:
        return max(0, int(total * self.quality))


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
    remaining_run_sec: float = 0.0
    remaining_research_sec: float = 0.0
    synthesis_reserve_sec: float = 0.0
    reserved_tokens: int = 0
    reserved_llm_calls: int = 0
    active_worker_leases: int = 0
    remaining_for_research_tokens: int = 0
    remaining_for_synthesis_tokens: int = 0
    remaining_for_quality_tokens: int = 0
    repair_reserve_tokens: int = 0
    remaining_for_repair_tokens: int = 0

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
            "remaining_run_sec": self.remaining_run_sec,
            "remaining_research_sec": self.remaining_research_sec,
            "synthesis_reserve_sec": self.synthesis_reserve_sec,
            "remaining_tokens": max(0, self.token_limit - self.used_tokens),
            "remaining_for_research": max(0, self.research_cap_tokens - self.used_tokens),
            "reserved_tokens": self.reserved_tokens,
            "reserved_llm_calls": self.reserved_llm_calls,
            "active_worker_leases": self.active_worker_leases,
            "remaining_for_research_tokens": self.remaining_for_research_tokens,
            "remaining_for_synthesis_tokens": self.remaining_for_synthesis_tokens,
            "remaining_for_quality_tokens": self.remaining_for_quality_tokens,
            "repair_reserve_tokens": self.repair_reserve_tokens,
            "remaining_for_repair_tokens": self.remaining_for_repair_tokens,
        }


@dataclass(frozen=True)
class _WorkerLease:
    lease_id: str
    task_id: str
    token_ceiling: int
    max_llm_calls: int
    max_output_tokens_per_call: int = 4_096
    used_tokens: int = 0
    in_flight_tokens: int = 0
    llm_calls: int = 0
    stage: str = "research"


@dataclass(frozen=True)
class _LLMReservation:
    reservation_id: str
    estimated_tokens: int
    worker_task_id: str
    phase: str


class BudgetReservationError(RuntimeError):
    """Raised when an LLM request cannot be authorized before it is sent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RunBudgetManager:
    """Atomic run budget with absolute wall-clock deadline and reservations."""

    _RESEARCH_LLM_PHASES = frozenset(
        {"execute", "worker", "research", "recover", "validate"}
    )

    def __init__(
        self,
        *,
        token_limit: int = 100_000,
        llm_call_limit: int = 30,
        tool_call_limit: int = 40,
        deadline_sec: float = 600.0,
        phase_plan: PhaseBudgetPlan | None = None,
        max_llm_calls_per_worker: int = 8,
        started_at: float | None = None,
        synthesis_reserve_sec: float = 210.0,
        max_parallel_workers: int = 3,
        stage_reserves: dict[str, float] | None = None,
    ) -> None:
        self.token_limit = max(0, int(token_limit or 0))
        self.llm_call_limit = max(0, int(llm_call_limit or 0))
        self.tool_call_limit = max(0, int(tool_call_limit or 0))
        self.deadline_sec = max(0.0, float(deadline_sec or 0))
        self.phase_plan = phase_plan or PhaseBudgetPlan()
        self.max_llm_calls_per_worker = max(1, int(max_llm_calls_per_worker or 8))
        self.synthesis_reserve_sec = max(0.0, float(synthesis_reserve_sec or 0))
        self.max_parallel_workers = max(1, int(max_parallel_workers or 3))
        self.stage_reserves = {
            "research": 0.60,
            "repair": 0.15,
            "report": 0.15,
            "verification": 0.10,
            **(stage_reserves or {}),
        }
        self._lock = threading.RLock()
        self._started = float(started_at) if started_at is not None else time.perf_counter()
        self.deadline_at = self._started + self.deadline_sec if self.deadline_sec > 0 else None
        self._used_tokens = 0
        self._llm_calls = 0
        self._tool_calls = 0
        self._force_synthesis = False
        self._reserved_nonworker_tokens = 0
        self._reserved_llm_calls = 0
        self._worker_leases: dict[str, _WorkerLease] = {}
        self._llm_reservations: dict[str, _LLMReservation] = {}

    @classmethod
    def from_config(
        cls,
        config: Any | None,
        *,
        run_budget: dict[str, Any] | None = None,
        started_at: float | None = None,
    ) -> "RunBudgetManager":
        rb = dict(run_budget or {})
        return cls(
            token_limit=int(rb.get("max_total_tokens") or getattr(config, "max_total_tokens", 100_000) or 100_000),
            llm_call_limit=int(rb.get("max_llm_calls") or getattr(config, "max_llm_calls_per_run", 30) or 30),
            tool_call_limit=int(rb.get("max_tool_calls") or getattr(config, "max_tool_calls", 40) or 40),
            deadline_sec=float(rb.get("max_run_sec") or getattr(config, "max_run_sec", 600) or 600),
            max_llm_calls_per_worker=int(
                rb.get("max_llm_calls_per_worker")
                or getattr(config, "max_llm_calls_per_worker", 8)
                or 8
            ),
            started_at=started_at,
            synthesis_reserve_sec=float(rb.get("synthesis_reserve_sec") or getattr(config, "synthesis_reserve_sec", 210) or 75),
            max_parallel_workers=int(rb.get("max_parallel_workers") or getattr(config, "max_parallel_workers", 3) or 3),
            stage_reserves=rb.get("stage_reserves") if isinstance(rb.get("stage_reserves"), dict) else None,
        )

    def stage_reserve_tokens(self, stage: str) -> int:
        """Deterministic token reserve for a named phase."""
        share = float(self.stage_reserves.get(str(stage), 0.0) or 0.0)
        return max(0, int(self.token_limit * max(0.0, min(1.0, share))))

    def _research_stage_cap_tokens(self, stage: str) -> int:
        """Return the non-borrowable ceiling for a research stage.

        Initial research may consume only its 60% share.  The one targeted
        repair wave is allowed to consume the separately protected 15% after
        that, while report and verification shares remain unavailable.
        """
        initial = self.stage_reserve_tokens("research")
        if str(stage).lower() == "repair":
            return min(self.token_limit, initial + self.stage_reserve_tokens("repair"))
        return initial

    def reserve(self, stage: str) -> dict[str, Any]:
        """Return a diagnostic reservation without spending capacity."""
        reserved = self.stage_reserve_tokens(stage)
        with self._lock:
            cap = self._research_stage_cap_tokens(stage) if str(stage) in {"research", "repair"} else reserved
            available = max(0, cap - self._used_tokens - self._effective_reserved_tokens_locked())
        return {"stage": str(stage), "reserved_tokens": reserved, "available_tokens": available, "stage_cap_tokens": cap}

    def sync_from_usage(self, *, session_id: str = "", tool_calls: int = 0) -> None:
        """Reconcile real LLM usage; this is not an authorization path."""
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

    def cap_tool_calls(self, limit: int) -> None:
        with self._lock:
            new_limit = max(0, int(limit or 0))
            if new_limit <= 0:
                return
            if self.tool_call_limit <= 0 or new_limit < self.tool_call_limit:
                self.tool_call_limit = new_limit
                self._maybe_force_synthesis_locked()

    def _emit_denied(
        self,
        *,
        scope: str,
        resource: str,
        reason: str,
        task_id: str = "",
        used: int = 0,
        reserved: int = 0,
        limit: int = 0,
    ) -> None:
        from app.agent.harness.budget_events import emit_budget_denied

        emit_budget_denied(
            scope=scope,
            resource=resource,
            reason=reason,
            task_id=task_id,
            used=used,
            reserved=reserved,
            limit=limit,
            budget_manager=self,
        )

    def commit_tokens(self, tokens: int) -> None:
        with self._lock:
            self._used_tokens += max(0, int(tokens or 0))
            self._maybe_force_synthesis_locked()

    def note_llm_call(self) -> None:
        with self._lock:
            self._llm_calls += 1
            self._maybe_force_synthesis_locked()

    def note_tool_call(self) -> bool:
        return self.reserve_tool_calls(1)[0]

    def _active_worker_reserved_tokens_locked(self) -> int:
        return sum(max(0, lease.in_flight_tokens) for lease in self._worker_leases.values())

    def _effective_reserved_tokens_locked(self) -> int:
        return self._reserved_nonworker_tokens + self._active_worker_reserved_tokens_locked()

    def reserve_worker_lease(
        self,
        task_id: str,
        *,
        max_llm_calls: int | None = None,
        token_ceiling: int | None = None,
        parallel_workers: int | None = None,
        max_output_tokens_per_call: int | None = None,
        stage: str = "research",
    ) -> tuple[str, str]:
        """Atomically reserve worker research capacity."""
        normalized_stage = "repair" if str(stage).lower() == "repair" else "research"
        research_cap = self._research_stage_cap_tokens(normalized_stage)
        workers = max(1, int(parallel_workers or self.max_parallel_workers))
        fair_share = max(1, research_cap // workers) if research_cap > 0 else 0
        requested = max(0, int(token_ceiling if token_ceiling is not None else fair_share))

        with self._lock:
            reason = self._research_block_reason_locked(stage=normalized_stage)
            if reason:
                resource = (
                    "token" if reason.endswith("token_cap")
                    else "llm_call" if reason.endswith("llm_call_cap")
                    else "tool_call" if reason == "tool_call_cap"
                    else "time"
                )
                self._emit_denied(
                    scope="run" if reason.startswith("run_") or reason in {"tool_call_cap", "deadline_exceeded"} else "research_phase",
                    resource=resource,
                    reason=reason,
                    task_id=str(task_id or ""),
                )
                return "", reason
            active = self._active_worker_reserved_tokens_locked()
            if research_cap > 0:
                remaining = research_cap - self._used_tokens - active
                if remaining <= 0:
                    self._emit_denied(
                        scope="research_phase",
                        resource="token",
                        reason="research_phase_token_cap",
                        task_id=str(task_id or ""),
                        used=self._used_tokens,
                        limit=research_cap,
                    )
                    return "", "research_phase_token_cap"
                requested = min(requested, remaining)
            if self.token_limit > 0:
                remaining_hard = (
                    self.token_limit
                    - self._used_tokens
                    - self._reserved_nonworker_tokens
                    - active
                )
                if remaining_hard <= 0:
                    self._emit_denied(
                        scope="run",
                        resource="token",
                        reason="run_token_cap",
                        task_id=str(task_id or ""),
                        used=self._used_tokens,
                        limit=self.token_limit,
                    )
                    return "", "run_token_cap"
                requested = min(requested, remaining_hard)

            lease_id = f"lease_{uuid.uuid4().hex[:16]}"
            self._worker_leases[lease_id] = _WorkerLease(
                lease_id=lease_id,
                task_id=str(task_id or ""),
                token_ceiling=max(0, requested),
                max_llm_calls=max(1, int(max_llm_calls or self.max_llm_calls_per_worker)),
                max_output_tokens_per_call=max(
                    1,
                    int(max_output_tokens_per_call or 4_096),
                ),
                stage=normalized_stage,
            )
            return lease_id, ""

    def release_worker_lease(self, lease_id: str) -> None:
        if not lease_id:
            return
        with self._lock:
            lease = self._worker_leases.pop(lease_id, None)
            if lease is None:
                return
            for reservation in list(self._llm_reservations.values()):
                if reservation.worker_task_id == lease.task_id:
                    self._release_llm_reservation_locked(reservation.reservation_id)
            self._maybe_force_synthesis_locked()

    def worker_output_limit(self, task_id: str) -> int:
        with self._lock:
            lease = next(
                (
                    item
                    for item in self._worker_leases.values()
                    if item.task_id == str(task_id or "")
                ),
                None,
            )
            return lease.max_output_tokens_per_call if lease else 4_096

    def worker_lease_snapshot(
        self,
        task_id: str = "",
        *,
        lease_id: str = "",
    ) -> dict[str, int]:
        with self._lock:
            lease = None
            if lease_id:
                lease = self._worker_leases.get(lease_id)
            if lease is None and task_id:
                lease = next(
                    (
                        item
                        for item in self._worker_leases.values()
                        if item.task_id == str(task_id or "")
                    ),
                    None,
                )
            if lease is None:
                return {}
            return {
                "llm_calls_used": lease.llm_calls,
                "llm_calls_limit": lease.max_llm_calls,
                "tokens_used": lease.used_tokens,
                "token_limit": lease.token_ceiling,
                "in_flight_tokens": lease.in_flight_tokens,
            }

    def reserve_llm_call(
        self,
        *,
        estimated_tokens: int,
        worker_task_id: str = "",
        phase: str = "",
    ) -> tuple[str, str]:
        """Atomically authorize one LLM request before it is sent."""
        total = max(1, int(estimated_tokens or 1))
        normalized_phase = str(phase or "").lower()
        with self._lock:
            if (
                self.llm_call_limit > 0
                and self._llm_calls + self._reserved_llm_calls >= self.llm_call_limit
            ):
                self._emit_denied(
                    scope="run",
                    resource="llm_call",
                    reason="run_llm_call_cap",
                    task_id=worker_task_id,
                    used=self._llm_calls,
                    reserved=self._reserved_llm_calls,
                    limit=self.llm_call_limit,
                )
                return "", "run_llm_call_cap"

            lease = None
            if worker_task_id:
                lease = next(
                    (item for item in self._worker_leases.values() if item.task_id == worker_task_id),
                    None,
                )
                if lease is None:
                    self._emit_denied(
                        scope="worker",
                        resource="llm_call",
                        reason="worker_lease_missing",
                        task_id=worker_task_id,
                    )
                    return "", "worker_lease_missing"
                if lease.llm_calls >= lease.max_llm_calls:
                    self._emit_denied(
                        scope="worker",
                        resource="llm_call",
                        reason="worker_llm_call_cap",
                        task_id=worker_task_id,
                        used=lease.llm_calls,
                        limit=lease.max_llm_calls,
                    )
                    return "", "worker_llm_call_cap"
                if (
                    lease.token_ceiling > 0
                    and lease.used_tokens + lease.in_flight_tokens + total > lease.token_ceiling
                ):
                    self._emit_denied(
                        scope="worker",
                        resource="token",
                        reason="worker_token_cap",
                        task_id=worker_task_id,
                        used=lease.used_tokens,
                        reserved=lease.in_flight_tokens,
                        limit=lease.token_ceiling,
                    )
                    return "", "worker_token_cap"
                effective_after = (
                    self._used_tokens + self._effective_reserved_tokens_locked() + total
                )
            else:
                effective_after = (
                    self._used_tokens + self._effective_reserved_tokens_locked() + total
                )

            if self.token_limit > 0 and effective_after > self.token_limit:
                self._emit_denied(
                    scope="run",
                    resource="token",
                    reason="run_token_cap",
                    task_id=worker_task_id,
                    used=self._used_tokens,
                    reserved=self._effective_reserved_tokens_locked(),
                    limit=self.token_limit,
                )
                return "", "run_token_cap"
            research_stage = lease.stage if lease is not None else "research"
            if (
                normalized_phase in self._RESEARCH_LLM_PHASES
                and self.token_limit > 0
                and effective_after > self._research_stage_cap_tokens(research_stage)
            ):
                self._emit_denied(
                    scope="research_phase",
                    resource="token",
                    reason="research_phase_token_cap",
                    task_id=worker_task_id,
                    used=self._used_tokens,
                    reserved=self._effective_reserved_tokens_locked(),
                    limit=self._research_stage_cap_tokens(research_stage),
                )
                return "", "research_phase_token_cap"

            reservation_id = f"llmres_{uuid.uuid4().hex[:16]}"
            self._llm_reservations[reservation_id] = _LLMReservation(
                reservation_id=reservation_id,
                estimated_tokens=total,
                worker_task_id=str(worker_task_id or ""),
                phase=normalized_phase,
            )
            self._reserved_llm_calls += 1
            if lease is not None:
                self._worker_leases[lease.lease_id] = _WorkerLease(
                    lease_id=lease.lease_id,
                    task_id=lease.task_id,
                    token_ceiling=lease.token_ceiling,
                    max_llm_calls=lease.max_llm_calls,
                    max_output_tokens_per_call=lease.max_output_tokens_per_call,
                    used_tokens=lease.used_tokens,
                    in_flight_tokens=lease.in_flight_tokens + total,
                    llm_calls=lease.llm_calls + 1,
                    stage=lease.stage,
                )
            else:
                self._reserved_nonworker_tokens += total
            return reservation_id, ""

    def commit_llm_usage(self, reservation_id: str, actual_tokens: int) -> None:
        """Convert a reservation to actual usage and refund over-reservation."""
        with self._lock:
            reservation = self._llm_reservations.pop(reservation_id, None)
            if reservation is None:
                return
            self._reserved_llm_calls = max(0, self._reserved_llm_calls - 1)
            actual = max(1, int(actual_tokens or reservation.estimated_tokens))
            if reservation.worker_task_id:
                lease = next(
                    (item for item in self._worker_leases.values() if item.task_id == reservation.worker_task_id),
                    None,
                )
                if lease is not None:
                    self._worker_leases[lease.lease_id] = _WorkerLease(
                        lease_id=lease.lease_id,
                        task_id=lease.task_id,
                        token_ceiling=lease.token_ceiling,
                        max_llm_calls=lease.max_llm_calls,
                        max_output_tokens_per_call=lease.max_output_tokens_per_call,
                        used_tokens=lease.used_tokens + actual,
                        in_flight_tokens=max(0, lease.in_flight_tokens - reservation.estimated_tokens),
                        llm_calls=lease.llm_calls,
                        stage=lease.stage,
                    )
            else:
                self._reserved_nonworker_tokens = max(
                    0, self._reserved_nonworker_tokens - reservation.estimated_tokens
                )
            self._used_tokens += actual
            self._llm_calls += 1
            self._maybe_force_synthesis_locked()

    def release_llm_reservation(self, reservation_id: str) -> None:
        with self._lock:
            self._release_llm_reservation_locked(reservation_id)

    def _release_llm_reservation_locked(self, reservation_id: str) -> None:
        reservation = self._llm_reservations.pop(reservation_id, None)
        if reservation is None:
            return
        self._reserved_llm_calls = max(0, self._reserved_llm_calls - 1)
        if reservation.worker_task_id:
            lease = next(
                (item for item in self._worker_leases.values() if item.task_id == reservation.worker_task_id),
                None,
            )
            if lease is not None:
                self._worker_leases[lease.lease_id] = _WorkerLease(
                    lease_id=lease.lease_id,
                    task_id=lease.task_id,
                    token_ceiling=lease.token_ceiling,
                    max_llm_calls=lease.max_llm_calls,
                    max_output_tokens_per_call=lease.max_output_tokens_per_call,
                    used_tokens=lease.used_tokens,
                    in_flight_tokens=max(0, lease.in_flight_tokens - reservation.estimated_tokens),
                    llm_calls=lease.llm_calls,
                    stage=lease.stage,
                )
        else:
            self._reserved_nonworker_tokens = max(
                0, self._reserved_nonworker_tokens - reservation.estimated_tokens
            )
        # An errored provider request still consumed one call slot.
        self._llm_calls += 1

    def reserve_tool_calls(self, count: int = 1) -> tuple[bool, str]:
        calls = max(1, int(count or 1))
        with self._lock:
            if self.tool_call_limit > 0 and self._tool_calls + calls > self.tool_call_limit:
                self._emit_denied(
                    scope="run",
                    resource="tool_call",
                    reason="tool_call_cap",
                    used=self._tool_calls,
                    reserved=calls,
                    limit=self.tool_call_limit,
                )
                return False, "tool_call_cap"
            self._tool_calls += calls
            self._maybe_force_synthesis_locked()
            return True, ""

    def _maybe_force_synthesis_locked(self) -> None:
        research_cap = self._research_stage_cap_tokens("research")
        if self.token_limit > 0 and self._used_tokens >= research_cap:
            self._force_synthesis = True
        if self.llm_call_limit > 0 and self._llm_calls >= self.llm_call_limit:
            self._force_synthesis = True
        if self.tool_call_limit > 0 and self._tool_calls >= self.tool_call_limit:
            self._force_synthesis = True
        if self.remaining_for_research_sec() <= 0 and self.deadline_sec > 0:
            self._force_synthesis = True

    def force_synthesis(self) -> bool:
        with self._lock:
            self._maybe_force_synthesis_locked()
            return self._force_synthesis

    def mark_force_synthesis(self) -> None:
        with self._lock:
            self._force_synthesis = True

    def remaining_run_sec(self) -> float:
        if self.deadline_at is None:
            return 1e9
        return max(0.0, self.deadline_at - time.perf_counter())

    def remaining_for_research_sec(self) -> float:
        """Research may not consume the synthesis time reserve."""
        return max(0.0, self.remaining_run_sec() - self.synthesis_reserve_sec)

    def remaining_for_research_tokens(self) -> int:
        """Tokens research may still reserve; synthesis and quality are protected."""
        with self._lock:
            return max(
                0,
                self._research_stage_cap_tokens("research")
                - self._used_tokens
                - self._effective_reserved_tokens_locked(),
            )

    def remaining_for_supervisor_tokens(self) -> int:
        """Supervisor shares the research admission ceiling, not the synthesis reserve."""
        return self.remaining_for_research_tokens()

    def remaining_for_repair_tokens(self) -> int:
        """Capacity still protected for the single targeted repair wave."""
        with self._lock:
            return max(
                0,
                self._research_stage_cap_tokens("repair")
                - self._used_tokens
                - self._effective_reserved_tokens_locked(),
            )

    def repair_allowed(self) -> tuple[bool, str]:
        with self._lock:
            reason = self._research_block_reason_locked(stage="repair")
        return not reason, reason

    def remaining_for_synthesis_tokens(self) -> int:
        """Tokens available after protecting only the quality reserve."""
        with self._lock:
            return max(
                0,
                self.token_limit
                - self.phase_plan.quality_reserve_tokens(self.token_limit)
                - self._used_tokens
                - self._effective_reserved_tokens_locked(),
            )

    def remaining_for_quality_tokens(self) -> int:
        with self._lock:
            return max(
                0,
                self.token_limit
                - self._used_tokens
                - self._effective_reserved_tokens_locked(),
            )

    def elapsed_sec(self) -> float:
        return max(0.0, time.perf_counter() - self._started)

    def snapshot(self) -> RunBudgetSnapshot:
        with self._lock:
            remaining = self.remaining_run_sec()
            return RunBudgetSnapshot(
                token_limit=self.token_limit,
                used_tokens=self._used_tokens,
                llm_calls=self._llm_calls,
                llm_call_limit=self.llm_call_limit,
                tool_calls=self._tool_calls,
                tool_call_limit=self.tool_call_limit,
                research_cap_tokens=self._research_stage_cap_tokens("research"),
                synthesis_reserve_tokens=self.phase_plan.synthesis_reserve_tokens(self.token_limit),
                force_synthesis=self._force_synthesis,
                deadline_sec=self.deadline_sec,
                elapsed_sec=self.elapsed_sec(),
                remaining_run_sec=remaining,
                remaining_research_sec=max(0.0, remaining - self.synthesis_reserve_sec),
                synthesis_reserve_sec=self.synthesis_reserve_sec,
                reserved_tokens=self._effective_reserved_tokens_locked(),
                reserved_llm_calls=self._reserved_llm_calls,
                active_worker_leases=len(self._worker_leases),
                remaining_for_research_tokens=self.remaining_for_research_tokens(),
                remaining_for_synthesis_tokens=self.remaining_for_synthesis_tokens(),
                remaining_for_quality_tokens=self.remaining_for_quality_tokens(),
                repair_reserve_tokens=self.stage_reserve_tokens("repair"),
                remaining_for_repair_tokens=self.remaining_for_repair_tokens(),
            )

    def _research_block_reason_locked(self, *, stage: str = "research") -> str:
        snap = self.snapshot()
        effective_used = snap.used_tokens + snap.reserved_tokens
        if snap.token_limit > 0 and effective_used >= snap.token_limit:
            return "run_token_cap"
        research_cap = self._research_stage_cap_tokens(stage)
        if snap.token_limit > 0 and effective_used >= research_cap:
            return "research_phase_token_cap"
        if (
            snap.llm_call_limit > 0
            and snap.llm_calls + snap.reserved_llm_calls >= snap.llm_call_limit
        ):
            return "run_llm_call_cap"
        if snap.tool_call_limit > 0 and snap.tool_calls >= snap.tool_call_limit:
            return "tool_call_cap"
        if self.deadline_sec > 0 and snap.elapsed_sec >= self.deadline_sec:
            return "deadline_exceeded"
        if self.deadline_sec > 0 and snap.remaining_research_sec <= 0:
            return "synthesis_time_reserve"
        return ""

    def research_allowed(self) -> tuple[bool, str]:
        with self._lock:
            reason = self._research_block_reason_locked(stage="research")
        return not reason, reason

    def exhaustion_reason(self) -> str:
        """Return the exact committed exhaustion reason, if any."""
        snap = self.snapshot()
        if snap.token_limit > 0 and snap.used_tokens >= snap.token_limit:
            return "run_token_cap"
        if snap.token_limit > 0 and snap.used_tokens >= self._research_stage_cap_tokens("research"):
            return "research_phase_token_cap"
        if snap.llm_call_limit > 0 and snap.llm_calls >= snap.llm_call_limit:
            return "run_llm_call_cap"
        if snap.tool_call_limit > 0 and snap.tool_calls >= snap.tool_call_limit:
            return "tool_call_cap"
        if self.deadline_sec > 0 and snap.remaining_run_sec <= 0:
            return "deadline_exceeded"
        if self.deadline_sec > 0 and snap.remaining_research_sec <= 0:
            return "synthesis_time_reserve"
        return ""


def create_run_budget_manager(
    config: Any | None,
    *,
    run_budget: dict[str, Any] | None = None,
    run_started: float | None = None,
) -> RunBudgetManager:
    """Create a process-local manager; callers must keep it out of state."""
    return RunBudgetManager.from_config(
        config, run_budget=run_budget, started_at=run_started
    )

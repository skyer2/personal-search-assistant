"""In-memory append-only journal + replay helpers."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from app.observability.events import AgentEvent


class RunJournal:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, list[AgentEvent]] = defaultdict(list)

    def append(self, event: AgentEvent) -> None:
        with self._lock:
            self._by_session[event.session_id].append(event)

    def replay(self, session_id: str) -> list[AgentEvent]:
        with self._lock:
            return list(self._by_session.get(session_id, []))

    def events_for_run(self, session_id: str, run_id: str) -> list[AgentEvent]:
        return [event for event in self.replay(session_id) if event.run_id == run_id]

    def clear(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._by_session.clear()
            else:
                self._by_session.pop(session_id, None)


_TREE_OMIT_TYPES = {"llm_usage"}


def build_span_tree(events: list[dict[str, Any]]) -> dict[str, Any]:
    """把扁平 event 列表收成 span 因果树，供 TraceViewer 使用。

    JSONL 仍保留全部事件；因果树省略 llm_usage / gen_ai.chat，避免 400+ 叶子把 understand/plan/worker 淹掉。
    """
    omitted = 0
    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        event_type = str(event.get("type") or event.get("event") or "")
        if event_type in _TREE_OMIT_TYPES:
            omitted += 1
            continue
        span_id = str(event.get("span_id") or event.get("event_id") or f"anon-{len(nodes)}")
        if span_id not in nodes:
            nodes[span_id] = {
                "span_id": span_id,
                "parent_span_id": event.get("parent_span_id"),
                "name": event.get("type") or event.get("event") or event.get("phase") or "event",
                "phase": event.get("phase"),
                "status": event.get("status"),
                "duration_ms": event.get("duration_ms"),
                "task_id": event.get("task_id"),
                "plan_version": event.get("plan_version"),
                "attempt": event.get("attempt"),
                "timestamp": event.get("timestamp"),
                "events": [],
                "children": [],
            }
            order.append(span_id)
        node = nodes[span_id]
        node["name"] = _preferred_span_name(node, event)
        if event.get("duration_ms") is not None:
            node["duration_ms"] = event.get("duration_ms")
        if event.get("status"):
            node["status"] = event.get("status")
        if event.get("parent_span_id") and not node.get("parent_span_id"):
            node["parent_span_id"] = event.get("parent_span_id")
        if event.get("task_id") and not node.get("task_id"):
            node["task_id"] = event.get("task_id")
        node["events"].append(
            {
                "type": event.get("type") or event.get("event"),
                "status": event.get("status"),
                "timestamp": event.get("timestamp"),
                "duration_ms": event.get("duration_ms"),
            }
        )

    roots: list[dict[str, Any]] = []
    for span_id in order:
        node = nodes[span_id]
        parent = node.get("parent_span_id")
        if parent and parent in nodes and parent != span_id:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)
    return {"roots": roots, "span_count": len(nodes), "event_count": len(events), "omitted_count": omitted}


_SPAN_NAME_PRIORITY = (
    "research.run",
    "worker.execute",
    "worker.started",
    "worker.completed",
    "plan.created",
    "replan.applied",
    "progress.evaluated",
    "tool.started",
    "gen_ai.chat",
    "quality.evaluated",
    "eval.scored",
)


def _preferred_span_name(node: dict[str, Any], event: dict[str, Any]) -> str:
    current = str(node.get("name") or "")
    incoming = str(event.get("type") or event.get("event") or event.get("phase") or "")
    if current in _SPAN_NAME_PRIORITY and incoming not in _SPAN_NAME_PRIORITY:
        return current
    if incoming in _SPAN_NAME_PRIORITY:
        return incoming
    return current or incoming or "event"


_WORKER_TERMINAL = {"worker.completed", "worker.failed"}


def _as_int(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _worker_attempt_key(event: dict[str, Any], attrs: dict[str, Any]) -> tuple[str, int]:
    task_id = str(event.get("task_id") or attrs.get("task_id") or "")
    attempt = event.get("attempt")
    if attempt is None:
        attempt = attrs.get("attempt")
    return (task_id, _as_int(attempt, 1))


def _coalesce_worker_row(
    rows: dict[tuple[str, int], dict[str, Any]],
    event: dict[str, Any],
    attrs: dict[str, Any],
    event_type: str,
) -> None:
    """同一 task_id+attempt 的 started/completed 合成一行，objective 从 started 继承。"""
    key = _worker_attempt_key(event, attrs)
    incoming = {
        "type": event_type,
        "task_id": event.get("task_id") or attrs.get("task_id") or None,
        "status": event.get("status"),
        "duration_ms": event.get("duration_ms"),
        "attempt": key[1],
        "plan_version": event.get("plan_version") if event.get("plan_version") is not None else attrs.get("plan_version"),
        "objective": attrs.get("objective"),
        "fail_reason": attrs.get("fail_reason") or event.get("fail_reason"),
        "evidence_ids": attrs.get("evidence_ids") or [],
        "step_type": attrs.get("step_type"),
        "timestamp": event.get("timestamp"),
    }
    existing = rows.get(key)
    if existing is None:
        rows[key] = incoming
        return
    terminal = existing.get("type") in _WORKER_TERMINAL
    if event_type in _WORKER_TERMINAL or not terminal:
        existing["type"] = event_type
    for field in ("status", "duration_ms", "plan_version", "timestamp"):
        if incoming.get(field) not in (None, ""):
            existing[field] = incoming[field]
    if incoming.get("objective"):
        existing["objective"] = incoming["objective"]
    elif not existing.get("objective"):
        existing["objective"] = incoming.get("objective")
    if incoming.get("fail_reason"):
        existing["fail_reason"] = incoming["fail_reason"]
    if incoming.get("evidence_ids"):
        existing["evidence_ids"] = incoming["evidence_ids"]
    if incoming.get("step_type"):
        existing["step_type"] = incoming["step_type"]


def summarize_trace(events: list[dict[str, Any]]) -> dict[str, Any]:
    """把 journal 收成 Agent-native 视图：identity / worker / progress / replan / evidence / eval / usage。"""
    identity: dict[str, Any] = {}
    workers_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    progress: list[dict[str, Any]] = []
    replans: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    evals: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": 0.0,
        "calls": 0,
    }
    for event in events:
        event_type = str(event.get("type") or event.get("event") or "")
        attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
        if not identity.get("session_id"):
            identity = {
                "session_id": event.get("session_id"),
                "run_id": event.get("run_id"),
                "trace_id": event.get("trace_id"),
                "git_sha": attrs.get("git_sha") or event.get("git_sha"),
                "config_hash": attrs.get("config_hash") or event.get("config_hash"),
                "variant": attrs.get("variant") or event.get("variant"),
            }
        if attrs.get("failure.stage") or attrs.get("failure.type") or str(event_type).endswith(".failed"):
            failures.append(
                {
                    "stage": attrs.get("failure.stage"),
                    "type": attrs.get("failure.type"),
                    "reason": attrs.get("fail_reason") or attrs.get("error") or event.get("status"),
                    "task_id": event.get("task_id"),
                    "event": event_type,
                    "timestamp": event.get("timestamp"),
                }
            )
        if event_type.startswith("worker."):
            _coalesce_worker_row(workers_by_key, event, attrs, event_type)
        elif event_type == "progress.evaluated":
            progress.append(
                {
                    "type": event_type,
                    "verdict": attrs.get("verdict") or event.get("status"),
                    "reason": attrs.get("reason"),
                    "gaps": attrs.get("gaps") or [],
                    "conflict_count": attrs.get("conflict_count"),
                    "missing_dimensions": attrs.get("missing_dimensions") or [],
                    "plan_version": event.get("plan_version"),
                    "status": event.get("status"),
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event_type.startswith("replan."):
            replans.append(
                {
                    "type": event_type,
                    "from_plan_version": attrs.get("from_plan_version"),
                    "to_plan_version": attrs.get("to_plan_version") or event.get("plan_version"),
                    "reason": attrs.get("reason"),
                    "gaps": attrs.get("gaps") or [],
                    "added_tasks": attrs.get("added_tasks") or [],
                    "removed_tasks": attrs.get("removed_tasks") or [],
                    "remaining_budget": attrs.get("remaining_budget") or {},
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event_type == "evidence.registered":
            evidence.append(
                {
                    "evidence_id": attrs.get("evidence_id") or attrs.get("source_id"),
                    "artifact_id": attrs.get("artifact_id"),
                    "task_id": event.get("task_id"),
                    "locator": attrs.get("locator"),
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event_type in {"eval.scored", "quality.evaluated"}:
            evals.append(
                {
                    "case_id": attrs.get("case_id"),
                    "variant": attrs.get("variant"),
                    "accuracy": attrs.get("accuracy"),
                    "citation_score": attrs.get("citation_score") or attrs.get("citation_coverage_rate"),
                    "replan_count": attrs.get("replan_count"),
                    "latency_ms": attrs.get("latency_ms") or event.get("duration_ms"),
                    "passed": attrs.get("passed"),
                    "severity": attrs.get("severity"),
                    "status": event.get("status"),
                    "type": event_type,
                    "tool_calls": attrs.get("tool_calls") or attrs.get("tool_calls_count"),
                    "tokens": attrs.get("total_tokens") or attrs.get("tokens"),
                    "progress": attrs.get("progress") or attrs.get("verdict"),
                    "replan": attrs.get("replan"),
                    "evidence": attrs.get("evidence") or attrs.get("evidence_ids") or [],
                }
            )
        elif event_type in {"gen_ai.chat", "llm_usage"}:
            usage["calls"] += 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens"):
                usage[key] += int(attrs.get(key) or event.get(key) or 0)
            usage["cost_usd"] += float(attrs.get("cost_usd") or event.get("cost_usd") or 0.0)
    workers = list(workers_by_key.values())
    failure_counts: dict[str, int] = {}
    for row in failures:
        stage = str(row.get("stage") or "runtime")
        failure_counts[stage] = failure_counts.get(stage, 0) + 1
    eval_matrix = _eval_variant_matrix(evals)
    return {
        "identity": identity,
        "workers": workers,
        "progress": progress,
        "replans": replans,
        "evidence": evidence,
        "evals": evals,
        "eval_matrix": eval_matrix,
        "failures": failures,
        "failure_counts": failure_counts,
        "usage": usage,
        "event_count": len(events),
        "worker_count": len(workers),
        "progress_count": len(progress),
        "replan_count": sum(1 for row in replans if row.get("type") == "replan.applied"),
    }


def _eval_variant_matrix(evals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Side-by-side eval.scored rows grouped by case_id, keyed by variant."""
    scored = [row for row in evals if row.get("type") == "eval.scored" and row.get("case_id")]
    if not scored:
        return []
    by_case: dict[str, dict[str, Any]] = {}
    variants: list[str] = []
    for row in scored:
        case_id = str(row.get("case_id"))
        variant = str(row.get("variant") or "default")
        if variant not in variants:
            variants.append(variant)
        bucket = by_case.setdefault(case_id, {"case_id": case_id, "variants": {}})
        bucket["variants"][variant] = {
            "accuracy": row.get("accuracy"),
            "citation": row.get("citation_score"),
            "latency_ms": row.get("latency_ms"),
            "replan_count": row.get("replan_count"),
            "tool_calls": row.get("tool_calls"),
            "tokens": row.get("tokens"),
            "passed": row.get("passed"),
        }
    return [{"variants": variants, "cases": list(by_case.values())}]

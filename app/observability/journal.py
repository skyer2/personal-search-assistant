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


_TREE_OMIT_TYPES = {"llm_usage", "gen_ai.chat"}


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
        # parent_span_id is immutable once set at Span creation — do NOT overwrite from later events
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

    # Detect cycles and orphans before building parent→child links
    cycle_count = 0
    orphan_count = 0
    roots: list[dict[str, Any]] = []
    for span_id in order:
        node = nodes[span_id]
        parent = node.get("parent_span_id")
        if parent == span_id:
            cycle_count += 1
            node["parent_span_id"] = None
            parent = None
        if parent and parent in nodes:
            # Walk ancestors to detect cycles
            visited: set[str] = {span_id}
            current = parent
            is_cycle = False
            while current and current in nodes:
                if current in visited:
                    is_cycle = True
                    break
                visited.add(current)
                current = nodes[current].get("parent_span_id")
            if is_cycle:
                cycle_count += 1
                node["parent_span_id"] = None
                roots.append(node)
            else:
                nodes[parent]["children"].append(node)
        elif parent and parent not in nodes:
            orphan_count += 1
            roots.append(node)
        else:
            roots.append(node)
    return {
        "roots": roots,
        "span_count": len(nodes),
        "event_count": len(events),
        "omitted_count": omitted,
        "root_count": len(roots),
        "orphan_count": orphan_count,
        "cycle_count": cycle_count,
        "valid": len(roots) >= 1 and cycle_count == 0,
    }


_SPAN_NAME_PRIORITY = (
    "research.run",
    "task.understand",
    "plan.create",
    "worker.execute",
    "worker.started",
    "worker.completed",
    "brief.compiled",
    "plan.created",
    "synthesis.generate",
    "synthesis.completed",
    "replan.applied",
    "progress.evaluated",
    "retrieval.search",
    "tool.started",
    "gen_ai.chat",
    "quality.evaluated",
    "eval.scored",
    "recovery.decided",
    "context.built",
    "checkpoint.saved",
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
        "finding_ids": attrs.get("finding_ids") or [],
        "gaps": attrs.get("gaps") or [],
        "conflicts": attrs.get("conflicts") or [],
        "confidence": attrs.get("confidence"),
        "tool_calls": attrs.get("tool_calls"),
        "brief_id": attrs.get("brief_id"),
        "plan_id": attrs.get("plan_id"),
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
    for field in ("status", "duration_ms", "plan_version", "timestamp", "confidence", "tool_calls", "brief_id", "plan_id"):
        if incoming.get(field) not in (None, ""):
            existing[field] = incoming[field]
    if incoming.get("objective"):
        existing["objective"] = incoming["objective"]
    elif not existing.get("objective"):
        existing["objective"] = incoming.get("objective")
    if incoming.get("fail_reason"):
        existing["fail_reason"] = incoming["fail_reason"]
    for list_field in ("evidence_ids", "finding_ids", "gaps", "conflicts"):
        if incoming.get(list_field):
            existing[list_field] = incoming[list_field]
    if incoming.get("step_type"):
        existing["step_type"] = incoming["step_type"]


def summarize_trace(events: list[dict[str, Any]]) -> dict[str, Any]:
    """把 journal 收成 Agent-native 视图：identity / brief / plan / worker / lineage / eval。"""
    from app.observability.semantic import (
        build_lineage_edges,
        compute_replan_gap_closure,
        earliest_failure_origin,
    )

    identity: dict[str, Any] = {}
    brief: dict[str, Any] | None = None
    plans: list[dict[str, Any]] = []
    workers_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    progress: list[dict[str, Any]] = []
    replans: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    synthesis: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
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
            # warning / soft miss 不计入 failure attribution
            if str(event.get("status") or "").lower() == "warning":
                pass
            elif str(attrs.get("severity") or "").lower() == "warning":
                pass
            else:
                failures.append(
                    {
                        "stage": attrs.get("failure.stage"),
                        "origin_stage": attrs.get("failure.origin_stage") or attrs.get("failure.stage"),
                        "detected_stage": attrs.get("failure.detected_stage"),
                        "type": attrs.get("failure.type"),
                        "reason": attrs.get("fail_reason") or attrs.get("error") or event.get("status"),
                        "cause_artifact_id": attrs.get("failure.cause_artifact_id"),
                        "task_id": event.get("task_id"),
                        "event": event_type,
                        "timestamp": event.get("timestamp"),
                    }
                )
        if event_type == "brief.compiled":
            brief = {
                "brief_id": attrs.get("brief_id"),
                "brief_version": attrs.get("brief_version") or 1,
                "objective": attrs.get("objective"),
                "entities": attrs.get("entities") or [],
                "dimensions": attrs.get("dimensions") or [],
                "depth": attrs.get("depth"),
                "freshness": attrs.get("freshness"),
                "deliverable": attrs.get("deliverable"),
                "prefer_primary": attrs.get("prefer_primary"),
                "planner_source": attrs.get("planner_source"),
                "intent_confidence": attrs.get("intent_confidence"),
                "brief_ref": attrs.get("brief_ref"),
                "brief_hash": attrs.get("brief_hash"),
                "span_id": event.get("span_id"),
                "timestamp": event.get("timestamp"),
            }
        elif event_type.startswith("worker."):
            _coalesce_worker_row(workers_by_key, event, attrs, event_type)
        elif event_type == "plan.created":
            plans.append(
                {
                    "plan_id": attrs.get("plan_id"),
                    "plan_version": event.get("plan_version") or attrs.get("plan_version"),
                    "brief_id": attrs.get("brief_id"),
                    "task_count": attrs.get("task_count"),
                    "task_ids": attrs.get("task_ids") or [],
                    "planning_mode": attrs.get("planning_mode"),
                    "parallel_groups": attrs.get("parallel_groups"),
                    "brief_coverage": attrs.get("brief_coverage") or {},
                    "plan_ref": attrs.get("plan_ref"),
                    "plan_hash": attrs.get("plan_hash"),
                    "span_id": event.get("span_id"),
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event_type == "progress.evaluated":
            progress.append(
                {
                    "type": event_type,
                    "progress_id": attrs.get("progress_id"),
                    "verdict": attrs.get("verdict") or event.get("status"),
                    "reason": attrs.get("reason"),
                    "gaps": attrs.get("gaps") or [],
                    "open_gap_ids": attrs.get("open_gap_ids") or [],
                    "resolved_gap_ids": attrs.get("resolved_gap_ids") or [],
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
                    "patch_id": attrs.get("patch_id"),
                    "triggered_by": attrs.get("triggered_by"),
                    "target_gap_ids": attrs.get("target_gap_ids") or [],
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
                    "finding_id": attrs.get("finding_id"),
                    "claim_id": attrs.get("claim_id"),
                    "source_id": attrs.get("source_id"),
                    "source_kind": attrs.get("source_kind"),
                    "support_type": attrs.get("support_type"),
                    "source_quality": attrs.get("source_quality"),
                    "freshness": attrs.get("freshness"),
                    "task_id": event.get("task_id"),
                    "locator": attrs.get("locator"),
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event_type.startswith("synthesis."):
            synthesis.append(
                {
                    "type": event_type,
                    "answer_id": attrs.get("answer_id"),
                    "brief_id": attrs.get("brief_id"),
                    "plan_id": attrs.get("plan_id"),
                    "evidence_ids": attrs.get("evidence_ids") or [],
                    "claim_ids": attrs.get("claim_ids") or [],
                    "citation_ids": attrs.get("citation_ids") or [],
                    "answer_ref": attrs.get("answer_ref"),
                    "answer_hash": attrs.get("answer_hash"),
                    "word_count": attrs.get("word_count"),
                    "status": event.get("status"),
                    "span_id": event.get("span_id"),
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event_type.startswith("recovery."):
            recoveries.append(
                {
                    "type": event_type,
                    "decision": attrs.get("decision") or attrs.get("action"),
                    "failure_type": attrs.get("failure_type") or attrs.get("failure.type"),
                    "attempt": event.get("attempt") or attrs.get("attempt"),
                    "remaining_budget": attrs.get("remaining_budget") or {},
                    "status": event.get("status"),
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
                    "target_span_id": attrs.get("target_span_id"),
                    "target_artifact_id": attrs.get("target_artifact_id"),
                    "target_type": attrs.get("target_type"),
                    "grader": attrs.get("grader"),
                    "grader_version": attrs.get("grader_version"),
                    "metric": attrs.get("metric"),
                    "score": attrs.get("score"),
                    "label": attrs.get("label"),
                }
            )
        elif event_type in {"gen_ai.chat", "llm_usage"}:
            usage["calls"] += 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens"):
                usage[key] += int(attrs.get(key) or event.get(key) or 0)
            usage["cost_usd"] += float(attrs.get("cost_usd") or event.get("cost_usd") or 0.0)

        # enrich worker rows with lineage fields from completed attrs
        if event_type in _WORKER_TERMINAL:
            key = _worker_attempt_key(event, attrs)
            row = workers_by_key.get(key)
            if row is not None:
                for field in (
                    "finding_ids",
                    "evidence_ids",
                    "gaps",
                    "conflicts",
                    "confidence",
                    "tool_calls",
                    "search_calls",
                    "tokens",
                    "brief_id",
                    "plan_id",
                ):
                    if attrs.get(field) not in (None, "", []):
                        row[field] = attrs.get(field)

    workers = list(workers_by_key.values())
    failure_counts: dict[str, int] = {}
    for row in failures:
        stage = str(row.get("origin_stage") or row.get("stage") or "runtime")
        failure_counts[stage] = failure_counts.get(stage, 0) + 1
    eval_matrix = _eval_variant_matrix(evals)
    gap_closure = compute_replan_gap_closure(events)
    failure_origin = earliest_failure_origin(events)
    lineage = build_lineage_edges(events)
    quality = {
        "gap_closure": gap_closure,
        "failure_origin": failure_origin,
        "brief_coverage": (plans[-1].get("brief_coverage") if plans else None),
    }
    return {
        "identity": identity,
        "brief": brief,
        "plans": plans,
        "workers": workers,
        "progress": progress,
        "replans": replans,
        "evidence": evidence,
        "synthesis": synthesis,
        "recoveries": recoveries,
        "quality": quality,
        "lineage": lineage,
        "evals": evals,
        "eval_matrix": eval_matrix,
        "failures": failures,
        "failure_counts": failure_counts,
        "failure_origin": failure_origin,
        "usage": usage,
        "event_count": len(events),
        "worker_count": len(workers),
        "progress_count": len(progress),
        "replan_count": sum(1 for row in replans if row.get("type") == "replan.applied"),
        "gap_closure_rate": gap_closure.get("gap_closure_rate"),
        "replan_useful": gap_closure.get("replan_useful"),
        "replan_attempted": bool(gap_closure.get("replan_attempted")),
        "progress_attempted": bool(gap_closure.get("progress_attempted")),
        "trace_integrity": _check_integrity(events, identity),
    }


def _check_integrity(events: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.observability.integrity import check_trace_integrity

        run_status = ""
        for event in reversed(events):
            event_type = str(event.get("type") or event.get("event") or "")
            if event_type in {"run.completed", "run.failed"}:
                run_status = str(event.get("status") or "completed" if event_type == "run.completed" else "failed")
                break
        return check_trace_integrity(events, run_status=run_status)
    except Exception:
        return {"passed": None, "issues": ["integrity_check_failed"]}


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

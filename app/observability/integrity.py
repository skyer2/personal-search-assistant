"""Trace Integrity Checker: validates that a completed run has all expected events."""

from __future__ import annotations

from typing import Any


def check_trace_integrity(
    events: list[dict[str, Any]],
    *,
    run_status: str = "",
    include_tree: bool = True,
) -> dict[str, Any]:
    """Post-run check: are Brief/Plan/Progress/Synthesis/Quality present as expected?

    Returns a dict with passed/issues suitable for the Overview tab.
    """
    issues: list[str] = []
    counts: dict[str, int] = {}
    seq_values: list[int] = []
    is_terminal = run_status in {"success", "completed", "partial", "failed", "interrupted", "ok", "done"}
    is_agent_mode = False

    for event in events:
        event_type = str(event.get("type") or event.get("event") or "")
        if event_type:
            counts[event_type] = counts.get(event_type, 0) + 1
        seq = event.get("seq")
        if seq is not None:
            try:
                seq_values.append(int(seq))
            except (TypeError, ValueError):
                pass
        attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
        if attrs.get("search_mode") == "agent" or event_type == "brief.compiled":
            is_agent_mode = True

    brief_count = counts.get("brief.compiled", 0)
    plan_count = counts.get("plan.created", 0)
    worker_started = counts.get("worker.started", 0)
    worker_done = counts.get("worker.completed", 0) + counts.get("worker.failed", 0)
    evidence_count = counts.get("evidence.registered", 0)
    progress_count = counts.get("progress.evaluated", 0)
    synthesis_count = counts.get("synthesis.completed", 0) + counts.get("synthesis.failed", 0)
    quality_count = counts.get("quality.evaluated", 0)
    run_started = counts.get("run.started", 0)
    run_completed = counts.get("run.completed", 0) + counts.get("run.failed", 0)

    if is_agent_mode:
        if brief_count == 0:
            issues.append("missing_brief_event")
        if plan_count == 0:
            issues.append("missing_plan_event")

    if is_terminal and run_started == 0:
        issues.append("missing_run_started_event")

    if is_terminal and is_agent_mode:
        if worker_done == 0:
            issues.append("missing_worker_terminal_event")
        if progress_count == 0:
            issues.append("missing_progress_event")
        termination_count = counts.get("termination.reason", 0) + counts.get("run.failed", 0)
        if synthesis_count == 0 and termination_count == 0:
            issues.append("missing_synthesis_or_termination_event")
        if quality_count == 0:
            issues.append("missing_quality_event")

    if worker_started > 0 and worker_done < worker_started:
        issues.append(f"worker_mismatch:started={worker_started},done={worker_done}")
    if is_agent_mode and evidence_count > 0 and worker_done == 0:
        issues.append("evidence_without_worker_terminal")
    if run_status == "partial":
        terminal = next((e for e in reversed(events) if str(e.get("type") or e.get("event")) in {"run.completed", "run.failed", "run_summary"}), {})
        attrs = terminal.get("attributes") if isinstance(terminal.get("attributes"), dict) else {}
        metadata = attrs.get("metadata") if isinstance(attrs.get("metadata"), dict) else terminal.get("metadata") or {}
        if not (metadata.get("termination") or metadata.get("abort_reason")):
            issues.append("partial_without_termination_reason")

    # Seq uniqueness
    if seq_values:
        unique = set(seq_values)
        if len(unique) < len(seq_values):
            issues.append(f"seq_duplicates:{len(seq_values) - len(unique)}")
        sorted_seqs = sorted(seq_values)
        if sorted_seqs != seq_values and len(seq_values) > 2:
            non_monotonic = sum(1 for i in range(1, len(seq_values)) if seq_values[i] < seq_values[i - 1])
            if non_monotonic > 0:
                issues.append(f"seq_non_monotonic:{non_monotonic}")

    # Span tree health (summarized from build_span_tree output)
    from app.observability.journal import build_span_tree

    tree = build_span_tree(events) if include_tree else {"span_count": 0, "root_count": 0, "cycle_count": 0, "valid": None}
    if tree.get("span_count", 0) > 0:
        if tree.get("root_count", 0) == 0:
            issues.append("span_tree_no_root")
        if tree.get("cycle_count", 0) > 0:
            issues.append(f"span_tree_cycles:{tree['cycle_count']}")

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "counts": {
            "brief": brief_count,
            "plan": plan_count,
            "worker_started": worker_started,
            "worker_done": worker_done,
            "progress": progress_count,
            "synthesis": synthesis_count,
            "quality": quality_count,
            "run_started": run_started,
            "run_completed": run_completed,
        },
        "is_agent_mode": is_agent_mode,
        "span_tree": {
            "span_count": tree.get("span_count", 0),
            "root_count": tree.get("root_count", 0),
            "cycle_count": tree.get("cycle_count", 0),
            "valid": tree.get("valid", False),
        },
    }

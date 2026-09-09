"""Trace Integrity Checker: validates that a completed run has all expected events."""

from __future__ import annotations

from typing import Any

_STAGE_ALIAS = {
    "understand": "understand",
    "brief": "understand",
    "plan": "planning",
    "planning": "planning",
    "execute": "worker",
    "worker": "worker",
    "compress": "worker",
    "progress": "progress",
    "supervisor": "planning",
    "coverage": "progress",
    "synthesis": "synthesis",
    "finalize": "synthesis",
    "validate": "quality",
    "quality": "quality",
    "runtime": "runtime",
}

_STAGE_ORDER = {
    "understand": 1,
    "planning": 2,
    "worker": 3,
    "progress": 4,
    "synthesis": 5,
    "quality": 6,
    "runtime": 7,
}


def _stage_required(stage: str, failure_origin_stage: str) -> bool:
    origin = _STAGE_ALIAS.get(failure_origin_stage.lower(), failure_origin_stage.lower())
    stage = _STAGE_ALIAS.get(stage.lower(), stage.lower())
    if not origin or origin not in _STAGE_ORDER or stage not in _STAGE_ORDER:
        return True
    return _STAGE_ORDER[stage] < _STAGE_ORDER[origin]


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
    is_simple_fact_fast_path = False
    failure_origin_stage = ""
    worker_evidence_ids: set[str] = set()
    synthesis_evidence_ids: set[str] = set()
    evidence_statuses: list[str] = []
    synthesis_failed_events: list[dict[str, Any]] = []
    worker_terminal_without_task_id = 0
    worker_attempt_keys: list[tuple[str, int, int]] = []
    progress_waves: list[int] = []

    for event in events:
        event_type = str(event.get("type") or event.get("event") or "")
        if event_type:
            counts[event_type] = counts.get(event_type, 0) + 1
        raw_seq = event.get("seq")
        if raw_seq is not None:
            try:
                seq_values.append(int(raw_seq))
            except (TypeError, ValueError):
                pass
        raw_attrs = event.get("attributes")
        attrs = raw_attrs if isinstance(raw_attrs, dict) else {}
        if attrs.get("search_mode") == "agent" or event_type in {"brief.compiled", "spec.compiled"}:
            is_agent_mode = True
        if (
            attrs.get("task_shape") == "simple_fact"
            and attrs.get("execution_path") == "fast_path"
        ):
            is_simple_fact_fast_path = True
        if event_type in {"worker.completed", "worker.failed"}:
            if not str(event.get("task_id") or attrs.get("task_id") or "").strip():
                worker_terminal_without_task_id += 1
            worker_evidence_ids.update(str(item) for item in attrs.get("evidence_ids") or [] if str(item).strip())
        if event_type == "evidence.assessed":
            evidence_statuses.append(str(attrs.get("status") or ""))
        if event_type == "worker.started":
            try:
                started_task_id = str(event.get("task_id") or attrs.get("task_id") or "")
                worker_attempt_keys.append(
                    (
                        started_task_id,
                        int(event.get("plan_version") or attrs.get("plan_version") or 0),
                        int(event.get("attempt") or attrs.get("attempt") or 0),
                    )
                )
            except (TypeError, ValueError):
                pass
        if event_type == "progress.assessed":
            try:
                wave_id = int(attrs.get("dispatch_wave_id") or 0)
                if wave_id > 0:
                    progress_waves.append(wave_id)
            except (TypeError, ValueError):
                pass
            try:
                gap_count = int(attrs.get("unresolved_gap_count") or 0)
            except (TypeError, ValueError):
                gap_count = 0
        if event_type == "synthesis.completed":
            synthesis_evidence_ids.update(str(item) for item in attrs.get("evidence_ids") or [] if str(item).strip())
        if event_type == "synthesis.failed":
            synthesis_failed_events.append(event)
            synthesis_evidence_ids.update(str(item) for item in attrs.get("evidence_ids") or [] if str(item).strip())
        if event_type in {"run.failed", "run_summary", "run.terminated"}:
            failure_origin_stage = str(
                attrs.get("failure.origin_stage")
                or ((attrs.get("metadata") or {}).get("failure.origin_stage") if isinstance(attrs.get("metadata"), dict) else "")
                or failure_origin_stage
            )

    terminal_event: dict[str, Any] = next(
        (
            event
            for event in reversed(events)
            if str(event.get("type") or event.get("event")) in {"run.completed", "run.failed", "run_summary"}
        ),
        {},
    )
    raw_terminal_attrs = terminal_event.get("attributes")
    terminal_attrs = raw_terminal_attrs if isinstance(raw_terminal_attrs, dict) else {}
    terminal_metadata_value = terminal_attrs.get("metadata")
    raw_terminal_metadata = terminal_event.get("metadata")
    terminal_metadata = (
        terminal_metadata_value
        if isinstance(terminal_metadata_value, dict)
        else raw_terminal_metadata
        if isinstance(raw_terminal_metadata, dict)
        else {}
    )
    raw_termination = terminal_metadata.get("termination") or terminal_attrs.get("termination")
    termination = raw_termination if isinstance(raw_termination, dict) else {}
    termination_reason = str(
        termination.get("reason")
        or terminal_metadata.get("abort_reason")
        or terminal_attrs.get("abort_reason")
        or ""
    )
    termination_stage = str(termination.get("stage") or "")
    if not failure_origin_stage and termination_stage:
        failure_origin_stage = termination_stage

    brief_count = int(counts.get("brief.compiled", 0)) + int(counts.get("spec.compiled", 0))
    plan_count = int(counts.get("plan.created", 0))
    worker_started = int(counts.get("worker.started", 0))
    worker_done = int(counts.get("worker.completed", 0)) + int(
        counts.get("worker.failed", 0)
    )
    evidence_count = int(counts.get("evidence.registered", 0))
    progress_count = int(counts.get("progress.assessed", 0))
    synthesis_count = int(counts.get("synthesis.completed", 0)) + int(
        counts.get("synthesis.failed", 0)
    )
    quality_count = int(counts.get("quality.assessed", 0))
    control_count = int(counts.get("control.decided", 0))
    run_started = int(counts.get("run.started", 0))
    run_completed = int(counts.get("run.completed", 0)) + int(
        counts.get("run.failed", 0)
    )
    run_terminated = int(counts.get("run.terminated", 0))
    synthesis_started = int(counts.get("synthesis.started", 0))

    duplicate_worker_keys = {
        key for key in worker_attempt_keys if worker_attempt_keys.count(key) > 1
    }
    if duplicate_worker_keys:
        issues.append(f"duplicate_worker_execution:{len(duplicate_worker_keys)}")
    duplicate_progress_waves = {wave for wave in progress_waves if progress_waves.count(wave) > 1}
    if duplicate_progress_waves:
        issues.append(f"duplicate_progress_for_wave:{len(duplicate_progress_waves)}")
    if any(
        "graphrecursion" in " ".join(
            [
                str(event.get("status") or ""),
                str(event.get("error") or ""),
                str((event.get("attributes") or {}).get("error") or ""),
                str((event.get("attributes") or {}).get("reason") or ""),
            ]
        ).lower()
        or "recursion_limit" in " ".join(
            [
                str(event.get("status") or ""),
                str(event.get("error") or ""),
                str((event.get("attributes") or {}).get("error") or ""),
                str((event.get("attributes") or {}).get("reason") or ""),
            ]
        ).lower()
        for event in events
    ):
        issues.append("graph_recursion_termination")

    if is_agent_mode and not is_simple_fact_fast_path:
        if brief_count == 0 and _stage_required("brief", failure_origin_stage):
            issues.append("missing_spec_or_brief_event")
        if plan_count == 0 and _stage_required("plan", failure_origin_stage):
            issues.append("missing_plan_event")

    if is_terminal and run_started == 0:
        issues.append("missing_run_started_event")

    if is_terminal and is_agent_mode:
        if worker_done == 0 and _stage_required("worker", failure_origin_stage):
            issues.append("missing_worker_terminal_event")
        if (
            progress_count == 0
            and not is_simple_fact_fast_path
            and worker_started > 0
            and _stage_required("progress", failure_origin_stage)
        ):
            issues.append("missing_progress_event")
        termination_count = (
            counts.get("termination.reason", 0)
            + counts.get("run.failed", 0)
            + (1 if termination else 0)
        )
        if (
            synthesis_count == 0
            and not is_simple_fact_fast_path
            and termination_count == 0
            and _stage_required("synthesis", failure_origin_stage)
        ):
            issues.append("missing_synthesis_or_termination_event")
        quality_attempted = (
            termination.get("quality_attempted") is True
            or terminal_metadata.get("quality_attempted") is True
        )
        quality_required = not is_simple_fact_fast_path and (
            run_status in {"success", "completed", "ok", "done"}
            or quality_attempted
            or termination_stage in {"quality", "finalize"}
            or _STAGE_ALIAS.get(failure_origin_stage.lower(), failure_origin_stage.lower()) == "quality"
        )
        if quality_count == 0 and quality_required and (
            quality_attempted or _stage_required("quality", failure_origin_stage)
        ):
            issues.append("missing_quality_event")

    control_required = bool(
        progress_count
        or worker_done
        or synthesis_count
        or run_status in {"success", "completed", "partial", "ok", "done"}
    )
    if is_agent_mode and not is_simple_fact_fast_path and control_required and control_count == 0:
        issues.append("missing_control_decision_event")
    if worker_terminal_without_task_id:
        issues.append(f"worker_terminal_without_task_id:{worker_terminal_without_task_id}")
    if synthesis_count > 0 and run_terminated == 0:
        issues.append("missing_run_terminated_event")

    if worker_done > 0 and worker_started != worker_done:
        issues.append(f"worker_lifecycle_mismatch:started={worker_started},done={worker_done}")
    if is_agent_mode and evidence_count > 0 and worker_done == 0:
        issues.append("evidence_without_worker_terminal")
    if run_status == "partial":
        if not (
            termination
            or termination_reason
            or terminal_metadata.get("abort_reason")
            or terminal_attrs.get("abort_reason")
        ):
            issues.append("partial_without_termination_reason")

    for event in events:
        if str(event.get("type") or event.get("event")) != "progress.assessed":
            continue
        attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
        status = str(attrs.get("status") or event.get("status") or "")
        if status != "gap":
            continue
        actionable = any(
            attrs.get(key)
            for key in (
                "coverage_gaps",
                "missing_dimensions",
                "unresolved_conflicts",
                "low_confidence_claims",
                "stale_evidence",
                "unmet_success_criteria",
                "reason_codes",
            )
        )
        if not actionable:
            issues.append("non_actionable_gap")
            break

    if worker_evidence_ids and synthesis_evidence_ids and not (worker_evidence_ids & synthesis_evidence_ids):
        issues.append("artifact_evidence_disconnect")
    if worker_evidence_ids and synthesis_count == 0 and run_status in {"success", "partial", "completed"}:
        issues.append("artifact_evidence_without_synthesis")
    if synthesis_started > 0:
        if synthesis_count == 0:
            issues.append("missing_synthesis_terminal_event")
        root_span_ids = {
            str(event.get("span_id"))
            for event in events
            if str(event.get("type") or event.get("event")) == "run.started"
        }
        synthesis_span_ids = {
            str(event.get("span_id"))
            for event in events
            if str(event.get("type") or event.get("event")).startswith("synthesis.")
        }
        if not synthesis_span_ids or synthesis_span_ids.issubset(root_span_ids):
            issues.append("missing_synthesis_span")
    if synthesis_failed_events:
        if any(not str((item.get("attributes") or {}).get("fail_reason") or "").strip() for item in synthesis_failed_events):
            issues.append("synthesis_failure_reason_missing")

    terminated_event = next(
        (
            event
            for event in reversed(events)
            if str(event.get("type") or event.get("event")) == "run.terminated"
        ),
        {},
    )
    terminal_run_event = next(
        (
            event
            for event in reversed(events)
            if str(event.get("type") or event.get("event")) in {"run.completed", "run.failed"}
        ),
        {},
    )
    if terminated_event and terminal_run_event:
        terminated_attrs = (
            terminated_event.get("attributes")
            if isinstance(terminated_event.get("attributes"), dict)
            else {}
        )
        outcome = str(terminated_event.get("status") or terminated_attrs.get("termination_status") or "")
        run_terminal_status = str(terminal_run_event.get("status") or "")
        allowed = {
            "success": {"success", "ok", "completed"},
            "partial": {"partial"},
            "failed": {"failed", "error"},
            "cancelled": {"cancelled", "interrupted"},
        }.get(outcome)
        if allowed and run_terminal_status and run_terminal_status not in allowed:
            issues.append(f"terminal_outcome_mismatch:{outcome}!={run_terminal_status}")

    final_content_chars: int | None = None
    for event in (terminated_event, terminal_run_event):
        attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
        if attrs.get("final_content_chars") is not None:
            try:
                final_content_chars = int(attrs.get("final_content_chars"))
            except (TypeError, ValueError):
                final_content_chars = None
            break
    usable_evidence = bool(worker_evidence_ids) or any(
        status in {"partial", "sufficient"} for status in evidence_statuses
    )
    if usable_evidence and final_content_chars == 0:
        issues.append("usable_evidence_with_empty_final_content")

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
    from app.observability.semantic import build_lineage_edges

    lineage = build_lineage_edges(events) if include_tree else []
    lineage_edges = len(lineage)
    evidence_lineage_edges = sum(
        1
        for edge in lineage
        if str(edge.get("from_type")) == "evidence" or str(edge.get("to_type")) == "evidence"
    )
    span_count = int(tree.get("span_count") or 0)
    root_count = int(tree.get("root_count") or 0)
    cycle_count = int(tree.get("cycle_count") or 0)
    if span_count > 0:
        if root_count == 0:
            issues.append("span_tree_no_root")
        if cycle_count > 0:
            issues.append(f"span_tree_cycles:{cycle_count}")
    if is_agent_mode and is_terminal and not is_simple_fact_fast_path:
        if span_count < 1:
            issues.append("missing_root_span")
        elif root_count < 1:
            issues.append("missing_root_span")
    if worker_evidence_ids and synthesis_started > 0 and evidence_lineage_edges == 0:
        issues.append("missing_evidence_lineage")

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "failure_origin_stage": failure_origin_stage,
        "counts": {
            "brief": brief_count,
            "plan": plan_count,
            "worker_started": worker_started,
            "worker_done": worker_done,
            "progress": progress_count,
            "synthesis": synthesis_count,
            "synthesis_started": synthesis_started,
            "quality": quality_count,
            "control": control_count,
            "run_started": run_started,
            "run_completed": run_completed,
            "run_terminated": run_terminated,
        },
        "is_agent_mode": is_agent_mode,
        "span_tree": {
            "span_count": tree.get("span_count", 0),
            "root_count": tree.get("root_count", 0),
            "cycle_count": tree.get("cycle_count", 0),
            "valid": tree.get("valid", False),
        },
        "lineage_edges": lineage_edges,
    }

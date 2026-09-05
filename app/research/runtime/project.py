"""Graph → Loop 单向投影。

ResearchState 是唯一 workflow truth。LoopState 只是进程内 handles：
领域函数（_phase_*、工人）仍吃 LoopState，但不得反向成为 resume 来源。
"""

from __future__ import annotations

from typing import Any

from app.agent.harness.state import ExecutionPlan, LoopState, TaskIntent


def apply_graph_to_loop(loop: LoopState, gstate: dict[str, Any]) -> LoopState:
    """把 Graph 上的 workflow 字段投影到 LoopState，供领域服务读取。"""
    intent = gstate.get("intent")
    if isinstance(intent, dict) and intent:
        loop.intent = TaskIntent.from_dict(intent)
    plan = gstate.get("plan")
    if isinstance(plan, dict) and plan:
        loop.plan = ExecutionPlan.from_dict(plan)
        status = dict(gstate.get("task_status") or {})
        for i, step in enumerate(loop.plan.steps):
            tid = step.resolved_task_id(i)
            if tid in status:
                step.metadata["status"] = status[tid]
    if "replan_count" in gstate:
        loop.replan_count = int(gstate.get("replan_count") or 0)
    budget = gstate.get("budget")
    if isinstance(budget, dict) and budget:
        run_budget = dict(loop.metadata.get("run_budget") or {})
        for key in ("max_parallel_workers", "max_replan_count"):
            if budget.get(key) is None:
                continue
            value = max(0, int(budget[key]))
            existing = run_budget.get(key)
            if existing is not None:
                value = min(value, int(existing))
            run_budget[key] = value
        loop.metadata["run_budget"] = run_budget
    final = gstate.get("final_content")
    if isinstance(final, str) and final:
        loop.final_content = final
    abort = gstate.get("abort_reason")
    if abort:
        loop.abort_reason = str(abort)
    brief = gstate.get("brief")
    if isinstance(brief, dict) and brief:
        loop.research_brief_obj = brief
        if loop.intent is not None and (loop.intent.brief is None or loop.intent.brief.is_empty()):
            from app.agent.harness.research_brief import ResearchBrief

            loop.intent.brief = ResearchBrief.from_dict(brief)
    loop.metadata["workflow_authority"] = "research_state"
    findings = gstate.get("findings")
    if isinstance(findings, list):
        loop.metadata["partial_findings"] = [
            dict(item) for item in findings if isinstance(item, dict)
        ][:24]
    if gstate.get("progress"):
        loop.metadata["graph_progress"] = gstate.get("progress")
    if gstate.get("search_mode"):
        loop.metadata["search_mode"] = gstate.get("search_mode")
    return loop


def brief_from_intent(intent: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(intent, dict):
        return {}
    brief = intent.get("brief")
    return dict(brief) if isinstance(brief, dict) else {}


def findings_from_worker_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw_payload = row.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    findings: list[dict[str, Any]] = []
    raw = payload.get("findings") if isinstance(payload, dict) else None
    if isinstance(raw, list):
        for item in raw[:12]:
            if isinstance(item, dict):
                findings.append(dict(item))
            elif item:
                findings.append(
                    {
                        "task_id": str(row.get("task_id") or ""),
                        "summary": str(item)[:400],
                    }
                )
    if not findings and row.get("ok") and (payload.get("summary") or row.get("summary")):
        findings.append(
            {
                "task_id": str(row.get("task_id") or ""),
                "summary": str(payload.get("summary") or row.get("summary") or "")[:400],
                "facts": list(payload.get("facts") or [])[:8],
                "sources": list(payload.get("sources") or [])[:8],
            }
        )
    return findings

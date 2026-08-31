"""研究进度评估：enough / gap / abort / run。

语义缺口先用可测启发式（coverage / conflict / stale / missing dimension）。
是否允许补任务由 Harness（max_replan / PlanPatch validator）决定，LLM 没有 runtime 权力。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.agent.harness.state import ExecutionPlan, LoopState
from app.research.planning.validator import RESEARCH_TYPES, SYNTHESIS_TYPES
from app.research.runtime.scheduler import ready_research_steps

Progress = Literal["enough", "gap", "abort", "run"]

_HEDGE = re.compile(
    r"可能|不确定|未能确认|暂无|未找到|没有找到|不清楚|insufficient|unclear|maybe|not found",
    re.I,
)
_EMPTY_HINT = re.compile(
    r"未披露|暂无数据|没有收入|无相关|no relevant|empty result",
    re.I,
)
_METRIC = re.compile(
    r"(?P<label>收入|营收|订单|销量|估值|利润|revenue|arr|gmv)"
    r"(?:\s*[:：为是约]){0,4}\s*"
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>亿美元|亿元|亿|万美元|万|%|美元|元|usd)?",
    re.I,
)
_YEAR = re.compile(r"\b(20\d{2})\b")
_COMMERCIAL_DIM = ("收入", "营收", "订单", "量产", "商业化", "客户", "revenue", "order", "production")
_DIM_ALIASES = {
    "商业化": ("商业化", "量产", "交付", "营收", "订单", "收入", "客户", "revenue", "order", "production"),
    "横向比较": ("比较", "对比", "vs", "差异", "横向"),
    "技术路线": ("技术", "方案", "架构"),
    "竞争格局": ("竞争", "对手", "格局"),
    "风险": ("风险", "监管", "合规"),
    "监管": ("监管", "合规", "牌照"),
    "市场规模": ("市场规模", "市场空间", "cagr"),
}


@dataclass
class ProgressAssessment:
    verdict: Progress = "enough"
    coverage_gaps: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    low_confidence_claims: list[str] = field(default_factory=list)
    stale_evidence: list[str] = field(default_factory=list)
    missing_dimensions: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ProgressAssessment":
        data = dict(raw or {})
        verdict = str(data.get("verdict") or "enough")
        if verdict not in {"enough", "gap", "abort", "run"}:
            verdict = "enough"
        return cls(
            verdict=verdict,  # type: ignore[arg-type]
            coverage_gaps=[str(x) for x in (data.get("coverage_gaps") or []) if x],
            conflicts=[str(x) for x in (data.get("conflicts") or []) if x],
            low_confidence_claims=[str(x) for x in (data.get("low_confidence_claims") or []) if x],
            stale_evidence=[str(x) for x in (data.get("stale_evidence") or []) if x],
            missing_dimensions=[str(x) for x in (data.get("missing_dimensions") or []) if x],
            reason=str(data.get("reason") or ""),
        )


def latest_worker_results(rows: list[Any] | None) -> list[dict[str, Any]]:
    """同一 task_id 只保留最后一次结果。"""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("task_id") or "").strip()
        if not tid:
            continue
        latest[tid] = row
    return list(latest.values())


def assess_progress(
    plan: ExecutionPlan | None,
    *,
    task_status: dict[str, str] | None = None,
    state: LoopState | None = None,
    worker_results: list[Any] | None = None,
    query: str = "",
    aborted: bool = False,
    current_year: int | None = None,
    enabled: bool = True,
    intent: Any = None,
) -> ProgressAssessment:
    if aborted or (state is not None and state.abort_reason):
        return ProgressAssessment(verdict="abort", reason="aborted")
    if plan is None or not plan.steps:
        return ProgressAssessment(verdict="abort", reason="empty_plan")

    status = dict(task_status or {})
    if state is not None:
        for index, step in enumerate(plan.steps):
            tid = step.resolved_task_id(index)
            status.setdefault(tid, str(step.metadata.get("status") or "pending"))

    ready = ready_research_steps(plan, status)
    if ready:
        return ProgressAssessment(
            verdict="run",
            reason=f"ready_research:{len(ready)}",
        )

    pending_research = [
        step
        for index, step in enumerate(plan.steps)
        if step.step_type in RESEARCH_TYPES
        and status.get(step.resolved_task_id(index), "pending") in {"pending", "running"}
    ]
    if pending_research:
        # 无 READY 仍 pending：通常是上游失败挡住依赖，按缺口处理以免 dispatch 空转。
        return ProgressAssessment(
            verdict="gap",
            coverage_gaps=[
                f"blocked:{step.task_id}:{step.objective or step.description}"
                for step in pending_research
            ][:8],
            reason="blocked_pending_research",
        )

    failed_research = [
        step
        for index, step in enumerate(plan.steps)
        if step.step_type in RESEARCH_TYPES
        and status.get(step.resolved_task_id(index), "pending") == "failed"
    ]
    assessment = ProgressAssessment(verdict="enough", reason="coverage_ok")
    for step in failed_research:
        assessment.coverage_gaps.append(
            f"failed:{step.task_id}:{step.objective or step.description}"
        )

    rows = latest_worker_results(worker_results)
    if not rows and state is not None:
        rows = _rows_from_loop_state(state)

    _fill_worker_signals(assessment, plan, rows, query=query or getattr(plan, "research_brief", "") or "")

    resolved_intent = intent
    if resolved_intent is None and state is not None:
        resolved_intent = getattr(state, "intent", None)
    elif isinstance(resolved_intent, dict):
        from app.agent.harness.state import TaskIntent

        resolved_intent = TaskIntent.from_dict(resolved_intent)

    mode = str(plan.planning_mode or "")
    year = int(current_year or datetime.now().year)
    if enabled:
        _fill_stale_and_dimensions(
            assessment,
            plan,
            rows,
            query=query,
            current_year=year,
            intent=resolved_intent,
        )
        if mode != "dynamic":
            # DIRECT/TEMPLATE：没有显式失败/gaps/冲突时不因启发式维度再搜。
            if not assessment.coverage_gaps and not assessment.conflicts:
                assessment.stale_evidence = []
                assessment.missing_dimensions = []
                assessment.low_confidence_claims = []

    if not enabled:
        assessment.verdict = "enough" if not failed_research else "gap"
        assessment.reason = "progress_eval_disabled"
        return assessment

    if assessment.coverage_gaps or assessment.conflicts:
        assessment.verdict = "gap"
    elif mode == "dynamic" and (
        assessment.missing_dimensions or assessment.stale_evidence or _all_low_confidence(rows)
    ):
        assessment.verdict = "gap"
    else:
        assessment.verdict = "enough"

    pending_synth = [
        step
        for index, step in enumerate(plan.steps)
        if step.step_type in SYNTHESIS_TYPES
        and status.get(step.resolved_task_id(index), "pending") in {"pending", "running"}
    ]
    if assessment.verdict == "enough" and pending_synth:
        assessment.reason = "ready_for_synthesis"
    elif assessment.verdict == "gap":
        assessment.reason = assessment.reason if assessment.reason != "coverage_ok" else "semantic_gap"
    return assessment


def evaluate_progress(
    plan: ExecutionPlan | None,
    *,
    task_status: dict[str, str] | None = None,
    state: LoopState | None = None,
    worker_results: list[Any] | None = None,
    query: str = "",
    aborted: bool = False,
    enabled: bool = True,
    intent: Any = None,
) -> Progress:
    return assess_progress(
        plan,
        task_status=task_status,
        state=state,
        worker_results=worker_results,
        query=query,
        aborted=aborted,
        enabled=enabled,
        intent=intent,
    ).verdict


def _rows_from_loop_state(state: LoopState) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in state.step_results:
        step_type = str(getattr(item, "step_type", "") or "")
        if step_type not in RESEARCH_TYPES:
            continue
        meta = getattr(item, "metadata", None) or {}
        payload = meta.get("worker_payload") if isinstance(meta, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        content = str(getattr(item, "content", "") or "")
        rows.append(
            {
                "task_id": str(meta.get("task_id") or meta.get("step_index") or ""),
                "ok": bool(payload.get("ok", True)),
                "summary": str(payload.get("summary") or content or "")[:400],
                "step_type": step_type,
                "payload": payload,
            }
        )
    return rows


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else {}


def _text_of(row: dict[str, Any]) -> str:
    payload = _payload(row)
    parts = [
        str(row.get("summary") or ""),
        str(payload.get("summary") or ""),
        " ".join(str(x) for x in (payload.get("facts") or [])),
        " ".join(str(x) for x in (payload.get("gaps") or [])),
    ]
    return " ".join(parts)


def _fill_worker_signals(
    assessment: ProgressAssessment,
    plan: ExecutionPlan,
    rows: list[dict[str, Any]],
    *,
    query: str,
) -> None:
    by_id = {str(row.get("task_id") or ""): row for row in rows}
    for index, step in enumerate(plan.steps):
        if step.step_type not in RESEARCH_TYPES:
            continue
        tid = step.resolved_task_id(index)
        row = by_id.get(tid)
        if row is None:
            continue
        payload = _payload(row)
        summary = str(row.get("summary") or payload.get("summary") or "").strip()
        facts = [str(x) for x in (payload.get("facts") or []) if str(x).strip()]
        sources = [str(x) for x in (payload.get("sources") or []) if str(x).strip()]
        if not row.get("ok", True) or not summary:
            assessment.coverage_gaps.append(f"empty:{tid}:{step.objective or step.description}")
        for gap in payload.get("gaps") or []:
            assessment.coverage_gaps.append(f"reported:{tid}:{gap}")
        for conflict in payload.get("conflicts") or []:
            assessment.conflicts.append(str(conflict))
        try:
            confidence = float(payload.get("confidence") if payload.get("confidence") is not None else 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        text = _text_of(row)
        if confidence < 0.5 or (_HEDGE.search(text) and not facts):
            assessment.low_confidence_claims.append(f"{tid}:{summary[:80] or 'low_confidence'}")
        if _EMPTY_HINT.search(text) and not facts:
            assessment.coverage_gaps.append(f"thin:{tid}:{step.objective or step.description}")
        if not sources and not facts and summary:
            assessment.low_confidence_claims.append(f"{tid}:no_sources")

    for row in rows:
        tid = str(row.get("task_id") or "")
        blob = _text_of(row)
        grouped: dict[str, list[float]] = {}
        for match in _METRIC.finditer(blob):
            label = str(match.group("label") or "").lower()
            try:
                value = float(match.group("num"))
            except (TypeError, ValueError):
                continue
            if value >= 1900 and value <= 2100 and label not in {"revenue", "arr", "gmv"}:
                # 年份误匹配，跳过
                continue
            unit = str(match.group("unit") or "").lower()
            grouped.setdefault(f"{label}:{unit}", []).append(value)
        for key, values in grouped.items():
            if len(values) < 2:
                continue
            base = max(abs(values[0]), 1e-6)
            if max(values) - min(values) > max(0.15 * base, 0.01):
                shown = ", ".join(f"{val:g}" for val in values)
                assessment.conflicts.append(f"{tid}:{key}: {shown}")


def _all_low_confidence(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    flagged = 0
    for row in rows:
        payload = _payload(row)
        try:
            confidence = float(payload.get("confidence") if payload.get("confidence") is not None else 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        if confidence < 0.5 or _HEDGE.search(_text_of(row)):
            flagged += 1
    return flagged == len(rows) and flagged > 0


def _fill_stale_and_dimensions(
    assessment: ProgressAssessment,
    plan: ExecutionPlan,
    rows: list[dict[str, Any]],
    *,
    query: str,
    current_year: int,
    intent: Any = None,
) -> None:
    from app.agent.harness.research_brief import PRIMARY_SOURCE_HINTS, brief_of

    brief = brief_of(
        intent,
        query=query or "",
        plan_brief=str(getattr(plan, "research_brief", "") or ""),
    )
    blob_query = f"{query} {plan.research_brief or ''} {brief.time_range}"
    query_years = [int(y) for y in _YEAR.findall(blob_query)]
    target_year = max(query_years) if query_years else current_year
    combined = " ".join(_text_of(row) for row in rows)
    mentioned = [int(y) for y in _YEAR.findall(combined)]
    freshness = str(brief.freshness or "any")
    needs_fresh = freshness == "recent" or bool(brief.time_range) or bool(query_years)
    if needs_fresh and mentioned and max(mentioned) <= target_year - 2:
        assessment.stale_evidence.append(
            f"latest_year={max(mentioned)}; required>={target_year}"
        )

    dims = [d for d in (brief.dimensions or []) if d and d != "关键事实"]
    if not dims:
        compare = any(token in blob_query for token in ("比较", "对比", " vs ", "VS"))
        commercial = any(token in blob_query for token in ("商业化", "营收", "收入", "量产"))
        if compare and commercial:
            dims = list(_COMMERCIAL_DIM)
        else:
            dims = []
    by_id = {str(row.get("task_id") or ""): row for row in rows}
    if dims:
        for index, step in enumerate(plan.steps):
            if step.step_type not in RESEARCH_TYPES:
                continue
            if step.depends_on:
                continue
            tid = step.resolved_task_id(index)
            row = by_id.get(tid)
            text = _text_of(row) if row else ""
            hay = text.lower()
            covered = False
            for dim in dims:
                aliases = _DIM_ALIASES.get(dim, (dim,))
                if any(alias.lower() in hay for alias in aliases):
                    covered = True
                    break
            if not covered:
                assessment.missing_dimensions.append(
                    f"{tid}:{(step.objective or step.description)[:80]}"
                )

    if brief.prefer_primary:
        urls: list[str] = []
        for row in rows:
            payload = _payload(row)
            urls.extend(str(x) for x in (payload.get("sources") or []) if x)
            urls.extend(str(x) for x in (row.get("sources") or []) if x)
        blob = " ".join(urls).lower() + " " + combined.lower()
        if urls and not any(hint.lower() in blob for hint in PRIMARY_SOURCE_HINTS):
            assessment.coverage_gaps.append("missing_primary_source")

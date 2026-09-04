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
    unresolved_conflicts: list[str] = field(default_factory=list)
    expected_disagreements: list[str] = field(default_factory=list)
    low_confidence_claims: list[str] = field(default_factory=list)
    stale_evidence: list[str] = field(default_factory=list)
    missing_dimensions: list[str] = field(default_factory=list)
    unmet_success_criteria: list[str] = field(default_factory=list)
    unmet_constraints: list[str] = field(default_factory=list)
    reason: str = ""
    gaps: list[dict[str, Any]] = field(default_factory=list)
    open_gap_ids: list[str] = field(default_factory=list)
    resolved_gap_ids: list[str] = field(default_factory=list)
    progress_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ProgressAssessment":
        data = dict(raw or {})
        verdict = str(data.get("verdict") or "enough")
        if verdict not in {"enough", "gap", "abort", "run"}:
            verdict = "enough"
        gaps_raw = data.get("gaps") or []
        gaps = [dict(x) for x in gaps_raw if isinstance(x, dict)]
        unresolved = [str(x) for x in (data.get("unresolved_conflicts") or []) if x]
        expected = [str(x) for x in (data.get("expected_disagreements") or []) if x]
        # 兼容旧 payload：conflicts 未拆分时全部当 unresolved（保守）
        conflicts = [str(x) for x in (data.get("conflicts") or []) if x]
        if conflicts and not unresolved and not expected:
            unresolved = list(conflicts)
        return cls(
            verdict=verdict,  # type: ignore[arg-type]
            coverage_gaps=[str(x) for x in (data.get("coverage_gaps") or []) if x],
            conflicts=conflicts or (unresolved + expected),
            unresolved_conflicts=unresolved,
            expected_disagreements=expected,
            low_confidence_claims=[str(x) for x in (data.get("low_confidence_claims") or []) if x],
            stale_evidence=[str(x) for x in (data.get("stale_evidence") or []) if x],
            missing_dimensions=[str(x) for x in (data.get("missing_dimensions") or []) if x],
            unmet_success_criteria=[str(x) for x in (data.get("unmet_success_criteria") or []) if x],
            unmet_constraints=[str(x) for x in (data.get("unmet_constraints") or []) if x],
            reason=str(data.get("reason") or ""),
            gaps=gaps,
            open_gap_ids=[str(x) for x in (data.get("open_gap_ids") or []) if x],
            resolved_gap_ids=[str(x) for x in (data.get("resolved_gap_ids") or []) if x],
            progress_id=str(data.get("progress_id") or ""),
        )

    def materialize_gaps(self, *, previous_gap_ids: list[str] | None = None) -> "ProgressAssessment":
        from app.observability.events import new_id
        from app.observability.semantic import materialize_gap_items, stable_gap_id

        if not self.gaps:
            self.gaps = materialize_gap_items(
                coverage_gaps=self.coverage_gaps
                + [f"criteria:{c}" for c in self.unmet_success_criteria]
                + [f"constraint:{c}" for c in self.unmet_constraints],
                missing_dimensions=self.missing_dimensions,
                conflicts=self.unresolved_conflicts,
                expected_disagreements=self.expected_disagreements,
                stale_evidence=self.stale_evidence,
            )
        for item in self.gaps:
            if not item.get("gap_id"):
                item["gap_id"] = stable_gap_id(
                    str(item.get("type") or "coverage"),
                    str(item.get("description") or item.get("dimension") or "gap"),
                )
        # Advisory limitations and expected uncertainty are disclosed by synthesis;
        # only blocking/important gaps may trigger another research wave.
        actionable_types = {
            "coverage",
            "missing_dimension",
            "unresolved_conflict",
            "conflict",
            "stale",
            "criteria",
            "constraint",
        }
        self.open_gap_ids = [
            str(item.get("gap_id") or "")
            for item in self.gaps
            if item.get("gap_id")
            and str(item.get("type") or "") in actionable_types
            and item.get("blocking", item.get("actionable", True)) is not False
            and str(item.get("severity") or "high") in {"high", "medium", "blocking", "important"}
        ]
        prev = {str(x) for x in (previous_gap_ids or []) if x}
        curr = set(self.open_gap_ids)
        self.resolved_gap_ids = sorted(prev - curr)
        if not self.progress_id:
            self.progress_id = f"progress_{new_id(8)}"
        return self


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


def _merge_claim_reconciliation(
    assessment: ProgressAssessment,
    reconciliation: Any | None,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> None:
    """把跨 Worker Claim Reconciliation 结果并入 Progress。

    Global claim-level 冲突优先于单 Worker heuristic。
    """
    result = reconciliation
    if result is None and rows is not None:
        try:
            from app.research.claims import reconcile_worker_results

            result = reconcile_worker_results(rows)
        except Exception:
            return
    if result is None:
        return
    if hasattr(result, "to_dict"):
        data = result.to_dict()
    elif isinstance(result, dict):
        data = result
    else:
        return

    for label in data.get("unresolved_labels") or []:
        text = str(label)
        if text and text not in assessment.unresolved_conflicts:
            assessment.unresolved_conflicts.append(text)
        if text and text not in assessment.conflicts:
            assessment.conflicts.append(text)
    for label in data.get("disclosed_labels") or []:
        text = str(label)
        if text and text not in assessment.expected_disagreements:
            assessment.expected_disagreements.append(text)
        if text and text not in assessment.conflicts:
            assessment.conflicts.append(text)
    # 权威消解成功的冲突：从 unresolved 中剔除同文案（若先前 heuristic 误判）
    resolved_set = {str(x) for x in (data.get("resolved_labels") or []) if x}
    if resolved_set:
        assessment.unresolved_conflicts = [
            x for x in assessment.unresolved_conflicts if x not in resolved_set
        ]


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
    previous_gap_ids: list[str] | None = None,
    reconciliation: Any | None = None,
) -> ProgressAssessment:
    def _finalize(assessment: ProgressAssessment) -> ProgressAssessment:
        return assessment.materialize_gaps(previous_gap_ids=previous_gap_ids)

    if aborted or (state is not None and state.abort_reason):
        return _finalize(ProgressAssessment(verdict="abort", reason="aborted"))
    if plan is None or not plan.steps:
        return _finalize(ProgressAssessment(verdict="abort", reason="empty_plan"))

    status = dict(task_status or {})
    if state is not None:
        for index, step in enumerate(plan.steps):
            tid = step.resolved_task_id(index)
            status.setdefault(tid, str(step.metadata.get("status") or "pending"))

    ready = ready_research_steps(plan, status, include_optional=False)
    if ready:
        return _finalize(
            ProgressAssessment(
                verdict="run",
                reason=f"ready_research:{len(ready)}",
            )
        )

    pending_research = [
        step
        for index, step in enumerate(plan.steps)
        if step.step_type in RESEARCH_TYPES
        and not (step.metadata or {}).get("optional")
        and status.get(step.resolved_task_id(index), "pending") in {"pending", "running"}
    ]
    if pending_research:
        # 无 READY 仍 pending：通常是上游失败挡住依赖，按缺口处理以免 dispatch 空转。
        return _finalize(
            ProgressAssessment(
                verdict="gap",
                coverage_gaps=[
                    f"blocked:{step.task_id}:{step.objective or step.description}"
                    for step in pending_research
                ][:8],
                reason="blocked_pending_research",
            )
        )

    failed_research = [
        step
        for index, step in enumerate(plan.steps)
        if step.step_type in RESEARCH_TYPES
        and not (step.metadata or {}).get("optional")
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
    _merge_claim_reconciliation(assessment, reconciliation, rows=rows)

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
        _fill_brief_contract(
            assessment,
            plan,
            rows,
            intent=resolved_intent,
            query=query,
        )
        if mode != "dynamic":
            # DIRECT/TEMPLATE：没有显式失败/gaps/冲突时不因启发式维度再搜。
            if not assessment.coverage_gaps and not assessment.unresolved_conflicts:
                assessment.stale_evidence = []
                assessment.missing_dimensions = []
                assessment.low_confidence_claims = []
                assessment.unmet_success_criteria = []
                assessment.unmet_constraints = []
                # expected_disagreements 保留，供 Synthesis 解释；不因它们清掉 conflicts 列表

    if not enabled:
        assessment.verdict = "enough" if not failed_research else "gap"
        assessment.reason = "progress_eval_disabled"
        return _finalize(assessment)

    if assessment.coverage_gaps or assessment.unresolved_conflicts:
        assessment.verdict = "gap"
    elif mode == "dynamic" and (
        assessment.missing_dimensions
        or assessment.stale_evidence
        or assessment.unmet_success_criteria
        or assessment.unmet_constraints
        or _all_low_confidence(rows)
    ):
        assessment.verdict = "gap"
    else:
        assessment.verdict = "enough"
        if assessment.expected_disagreements and assessment.reason == "coverage_ok":
            assessment.reason = "enough_with_expected_disagreement"

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
    return _finalize(assessment)


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


_BENCHMARK_HINT = re.compile(
    r"benchmark|swe-?bench|arena|terminal.?bench|pass@|accuracy|score|得分|准确率|脚手架|scaffold|划分|口径|版本",
    re.I,
)
_SCOPE_HINT = re.compile(
    r"\b(20\d{2}|q[1-4]|verified|pro|lite|full|v\d)\b|不同|分别|对照|对比基准",
    re.I,
)


def _classify_conflict(
    text: str,
    *,
    sources: list[str],
    confidence: float,
    facts: list[str],
) -> str:
    """返回 expected_disagreement | unresolved_conflict。

    不同 benchmark / 时间 / 方法口径 → expected（交 Synthesis 解释，不 Replan）。
    同 scope 不可解释且证据薄 → unresolved（可 Replan）。
    """
    blob = str(text or "")
    if _BENCHMARK_HINT.search(blob) or _SCOPE_HINT.search(blob):
        return "expected_disagreement"
    if len(sources) >= 2 and confidence >= 0.55 and facts:
        return "expected_disagreement"
    if "并列" in blob or "口径" in blob or "方法" in blob:
        return "expected_disagreement"
    if confidence < 0.5 or not sources or not facts:
        return "unresolved_conflict"
    # 默认：多数字差异视为预期分歧，避免搜得越多 conflict 越多的死循环
    return "expected_disagreement"


def _fill_worker_signals(
    assessment: ProgressAssessment,
    plan: ExecutionPlan,
    rows: list[dict[str, Any]],
    *,
    query: str,
) -> None:
    _ = query
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
            # Legacy string gaps are limitations, not proof that a core brief
            # dimension is unanswered. Workers must explicitly promote blockers.
            if isinstance(gap, dict):
                description = str(gap.get("description") or gap.get("claim") or "").strip()
                severity = str(gap.get("severity") or "advisory").lower()
                blocking = bool(gap.get("blocking")) or severity in {"blocking", "important", "high", "medium"}
                if description:
                    assessment.gaps.append({
                        **gap,
                        "type": str(gap.get("type") or "evidence_gap"),
                        "dimension": str(gap.get("dimension") or step.objective or step.description),
                        "description": description,
                        "severity": severity,
                        "blocking": blocking,
                        "actionable": blocking,
                    })
                    if blocking:
                        assessment.coverage_gaps.append(f"reported:{tid}:{description}")
            elif str(gap).strip():
                assessment.gaps.append({
                    "type": "advisory",
                    "dimension": str(step.objective or step.description),
                    "description": str(gap).strip(),
                    "severity": "advisory",
                    "blocking": False,
                    "actionable": False,
                })
        try:
            confidence = float(payload.get("confidence") if payload.get("confidence") is not None else 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        for conflict in payload.get("conflicts") or []:
            label = f"{tid}:{conflict}"
            assessment.conflicts.append(label)
            kind = _classify_conflict(
                str(conflict),
                sources=sources,
                confidence=confidence,
                facts=facts,
            )
            if kind == "unresolved_conflict":
                assessment.unresolved_conflicts.append(label)
            else:
                assessment.expected_disagreements.append(label)
        text = _text_of(row)
        if confidence < 0.5 or (_HEDGE.search(text) and not facts):
            assessment.low_confidence_claims.append(f"{tid}:{summary[:80] or 'low_confidence'}")
        if _EMPTY_HINT.search(text) and not facts:
            assessment.coverage_gaps.append(f"thin:{tid}:{step.objective or step.description}")
        if not sources and not facts and summary:
            assessment.low_confidence_claims.append(f"{tid}:no_sources")

    for row in rows:
        tid = str(row.get("task_id") or "")
        payload = _payload(row)
        facts = [str(x) for x in (payload.get("facts") or []) if str(x).strip()]
        sources = [str(x) for x in (payload.get("sources") or []) if str(x).strip()]
        try:
            confidence = float(payload.get("confidence") if payload.get("confidence") is not None else 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        blob = _text_of(row)
        grouped: dict[str, list[float]] = {}
        for match in _METRIC.finditer(blob):
            label = str(match.group("label") or "").lower()
            try:
                value = float(match.group("num"))
            except (TypeError, ValueError):
                continue
            if value >= 1900 and value <= 2100 and label not in {"revenue", "arr", "gmv"}:
                continue
            unit = str(match.group("unit") or "").lower()
            grouped.setdefault(f"{label}:{unit}", []).append(value)
        for key, values in grouped.items():
            if len(values) < 2:
                continue
            base = max(abs(values[0]), 1e-6)
            if max(values) - min(values) > max(0.15 * base, 0.01):
                shown = ", ".join(f"{val:g}" for val in values)
                label = f"{tid}:{key}: {shown}"
                assessment.conflicts.append(label)
                # 同文档内多数字：默认视为不同口径/时间序列，不触发 Replan
                kind = _classify_conflict(
                    f"{key} {blob[:200]}",
                    sources=sources,
                    confidence=confidence,
                    facts=facts,
                )
                if kind == "unresolved_conflict":
                    assessment.unresolved_conflicts.append(label)
                else:
                    assessment.expected_disagreements.append(label)


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
        facts_blob: list[str] = []
        for row in rows:
            payload = _payload(row)
            urls.extend(str(x) for x in (payload.get("sources") or []) if x)
            urls.extend(str(x) for x in (row.get("sources") or []) if x)
            facts_blob.extend(str(x) for x in (payload.get("facts") or []) if x)
        # 只看来源 URL + facts，避免 objective/summary 里「优先官方」措辞假阳性
        blob = (" ".join(urls) + " " + " ".join(facts_blob)).lower()
        if urls and not any(hint.lower() in blob for hint in PRIMARY_SOURCE_HINTS):
            assessment.coverage_gaps.append("missing_primary_source")


def _fill_brief_contract(
    assessment: ProgressAssessment,
    plan: ExecutionPlan,
    rows: list[dict[str, Any]],
    *,
    intent: Any = None,
    query: str = "",
) -> None:
    """对照 Research Brief 的 success_criteria / constraints（语义合同，非 Intent）。"""
    from app.agent.harness.research_brief import PRIMARY_SOURCE_HINTS, brief_of

    brief = brief_of(
        intent,
        query=query or "",
        plan_brief=str(getattr(plan, "research_brief", "") or ""),
    )
    combined = " ".join(_text_of(row) for row in rows)
    combined_l = combined.lower()

    urls: list[str] = []
    facts: list[str] = []
    for row in rows:
        payload = _payload(row)
        urls.extend(str(x) for x in (payload.get("sources") or []) if x)
        facts.extend(str(x) for x in (payload.get("facts") or []) if x)
    source_blob = (" ".join(urls) + " " + " ".join(facts)).lower()

    for criterion in list(brief.success_criteria or [])[:8]:
        c = str(criterion)
        cl = c.lower()
        # 系统默认成功标准：证据可追溯
        if "evidence" in cl or "artifact" in cl or "可追溯" in c:
            if rows and not urls and not facts:
                assessment.unmet_success_criteria.append(c[:120])
            continue
        if "冲突" in c or "并列" in c:
            # 冲突处理是写作约束，不据此判 GAP
            continue
        if "交付" in c or "工作目录" in c:
            continue
        if "官方" in c or "一手" in c or "primary" in cl:
            if urls and not any(h.lower() in source_blob for h in PRIMARY_SOURCE_HINTS):
                assessment.unmet_success_criteria.append(c[:120])
            continue
        if "时间" in c or "年份" in c or "fresh" in cl:
            # stale_evidence 已覆盖
            continue
        # 其余：从标准里抽关键词，证据文本需命中至少一半
        tokens = [
            t
            for t in re.split(r"[\s,，、/：:]+", c)
            if len(t) >= 2 and t not in {"必须", "尽量", "关键", "结论", "覆盖", "用户", "关心"}
        ][:6]
        if len(tokens) >= 2:
            hits = sum(1 for t in tokens if t.lower() in combined_l)
            if hits < max(1, len(tokens) // 2) and rows:
                assessment.unmet_success_criteria.append(c[:120])

    for constraint in list(brief.constraints or [])[:8]:
        c = str(constraint)
        if c.startswith("禁止来源:"):
            forbidden = [x.strip() for x in c.split(":", 1)[-1].split(",") if x.strip()]
            hit = [f for f in forbidden if f.lower() in source_blob]
            if hit:
                assessment.unmet_constraints.append(f"violated_forbid:{','.join(hit[:4])}")
        elif "优先官方" in c or "一手" in c:
            if urls and not any(h.lower() in source_blob for h in PRIMARY_SOURCE_HINTS):
                assessment.unmet_constraints.append(c[:120])
        elif "引用" in c or "[n]" in c:
            if rows and not urls and facts:
                assessment.unmet_constraints.append(c[:120])

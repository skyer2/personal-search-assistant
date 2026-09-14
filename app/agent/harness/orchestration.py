"""
多 Agent 编排增强（Phase 7）

【修改点】工业级协作原语：
- 检索步并行分组（无依赖 fan-out + join）
- 工人结构化回传解析
- 步级 checkpoint 持久化
- 幂等键防重复执行
- 子 Agent 绑定校验辅助
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.agent.harness.state import ExecutionPlan, PlanStep, StepResult, StepStatus

# 可并行 fan-out 的检索类步骤（写文件 / 汇总不在此列）
RETRIEVAL_STEP_TYPES = frozenset({"network_search", "file_read", "research"})

SUBAGENT_STEP_TYPES = frozenset({"network_search", "research"})

FORBIDDEN_TOOLS_BY_STEP: dict[str, frozenset[str]] = {
    "network_search": frozenset({"generate_markdown", "convert_md_to_pdf"}),
    "research": frozenset({"generate_markdown", "convert_md_to_pdf"}),
    "generate_markdown": frozenset({"internet_search"}),
}

# 缺 JSON / 越权后的内层重试：禁止再搜，只补 JSON。
JSON_ONLY_FAIL_REASONS = frozenset(
    {
        "invalid_structured_output",
        "invalid_structured_worker_result",
        "empty_worker_result",
        "unauthorized_tool",
        "worker_failed",
    }
)


@dataclass
class GapSignal:
    """A machine-readable research gap carried unchanged through the pipeline."""

    type: str = "evidence_gap"
    dimension: str = ""
    description: str = ""
    severity: str = "advisory"
    blocking: bool = False


def _normalize_gap(value: Any) -> GapSignal:
    if isinstance(value, GapSignal):
        return value
    if isinstance(value, dict):
        return GapSignal(
            type=str(value.get("type") or "evidence_gap"),
            dimension=str(value.get("dimension") or ""),
            description=str(value.get("description") or value.get("message") or ""),
            severity=str(value.get("severity") or "advisory"),
            blocking=bool(value.get("blocking", False)),
        )
    return GapSignal(description=str(value or ""))


@dataclass
class WorkerResultPayload:
    """工人回传的结构化载荷（监督者消费）。"""

    ok: bool = True
    summary: str = ""
    facts: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    confidence: float = 1.0
    error_code: str = ""
    worker: str = ""
    step_type: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[GapSignal] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    suggested_followups: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    stop_reason: str = ""

    def to_context_snippet(self, max_chars: int = 600) -> str:
        parts = [self.summary or ""]
        if self.findings:
            claims = []
            for item in self.findings[:5]:
                claims.append(str(item.get("claim") or ""))
            parts.append("主张: " + "; ".join(c for c in claims if c))
        elif self.facts:
            parts.append("要点: " + "; ".join(self.facts[:5]))
        if self.evidence_ids:
            parts.append("evidence: " + ", ".join(self.evidence_ids[:8]))
        if self.sources:
            parts.append("来源: " + ", ".join(self.sources[:5]))
        if self.gaps:
            parts.append(
                "缺口: "
                + "; ".join(g.description for g in self.gaps[:3] if g.description)
            )
        if self.conflicts:
            parts.append("冲突: " + "; ".join(self.conflicts[:3]))
        if not self.ok and self.error_code:
            parts.append(f"[{self.error_code}]")
        text = " | ".join(p for p in parts if p.strip())
        return text[:max_chars]


@dataclass
class StepExecutionDelta:
    """单步执行对共享状态的增量（并行 join 用）。"""

    step_index: int
    step_result: StepResult
    assistants_called: list[str] = field(default_factory=list)
    tool_calls: int = 0
    unauthorized_tools: list[str] = field(default_factory=list)


def step_idempotency_key(session_id: str, step_index: int, step_type: str) -> str:
    return f"{session_id}:{step_index}:{step_type}"


def task_query_fingerprint(task_query: str) -> str:
    normalized = re.sub(r"\s+", " ", task_query.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def mark_parallel_retrieval_groups(plan: ExecutionPlan) -> ExecutionPlan:
    """为连续检索步标记 parallel_group，供 StateGraph dispatch。"""
    group_id = 0
    i = 0
    steps = plan.steps
    while i < len(steps):
        if steps[i].step_type not in RETRIEVAL_STEP_TYPES:
            i += 1
            continue
        j = i
        while j < len(steps) and steps[j].step_type in RETRIEVAL_STEP_TYPES:
            j += 1
        if j - i >= 2:
            for k in range(i, j):
                steps[k].metadata["parallel_group"] = group_id
                steps[k].metadata["parallel_size"] = j - i
            group_id += 1
        i = j
    return plan

def parse_worker_payload(
    raw_content: str,
    *,
    step_type: str = "",
    subagent: str = "",
) -> WorkerResultPayload:
    """解析工人回传：优先 JSON，否则包装为 summary。"""
    text = (raw_content or "").strip()
    if not text:
        return WorkerResultPayload(
            ok=False,
            summary="",
            error_code="empty_worker_result",
            worker=subagent,
            step_type=step_type,
        )

    json_blob = _extract_json_object(text)
    if json_blob is not None:
        facts = [str(f) for f in json_blob.get("facts", []) if f][:10]
        findings = _normalize_findings(json_blob.get("findings"))
        artifact_ids = [
            str(item) for item in json_blob.get("artifact_ids") or [] if str(item).strip()
        ]
        for finding in findings:
            artifact_ids.extend(
                str(item)
                for item in finding.get("artifact_ids") or []
                if str(item).strip()
            )
        artifact_ids = list(dict.fromkeys(artifact_ids))[:20]
        return WorkerResultPayload(
            ok=bool(json_blob.get("ok", True)),
            summary=str(json_blob.get("summary", text))[:4000],
            facts=facts,
            sources=[str(s) for s in json_blob.get("sources", []) if s][:10],
            confidence=float(json_blob.get("confidence", 1.0) or 1.0),
            error_code=str(json_blob.get("error_code", "")),
            worker=str(json_blob.get("worker", subagent)),
            step_type=str(json_blob.get("step_type", step_type)),
            findings=findings,
            gaps=[_normalize_gap(x) for x in (json_blob.get("gaps") or []) if x][:8],
            conflicts=[
                _conflict_text(x) for x in (json_blob.get("conflicts") or []) if x
            ][:8],
            suggested_followups=[
                str(x) for x in (json_blob.get("suggested_followups") or []) if x
            ][:6],
            evidence_ids=[str(x) for x in (json_blob.get("evidence_ids") or []) if x][
                :20
            ],
            artifact_ids=artifact_ids,
            stop_reason=str(json_blob.get("stop_reason", "")),
        )

    return WorkerResultPayload(
        ok=True,
        summary=text[:4000],
        worker=subagent,
        step_type=step_type,
    )


def attach_structured_payload(
    result: StepResult, payload: WorkerResultPayload
) -> StepResult:
    """将结构化载荷写入 StepResult.metadata。"""
    result.metadata["worker_payload"] = asdict(payload)
    result.metadata["structured_ok"] = payload.ok
    result.metadata["structured_json"] = (
        _extract_json_object((result.content or "").strip()) is not None
    )
    if payload.error_code:
        result.metadata["error_code"] = payload.error_code
    return result


def validate_structured_worker_payload(
    payload: WorkerResultPayload,
    step: PlanStep,
    *,
    require_json: bool = True,
) -> tuple[bool, str]:
    """Validate that research workers return evidence-backed findings."""
    if step.step_type not in SUBAGENT_STEP_TYPES:
        return True, ""
    if not payload.summary.strip():
        return False, "empty_worker_result"
    _ = require_json
    if not payload.findings:
        return False, payload.error_code or "invalid_structured_worker_result"
    for finding in payload.findings:
        if not str(finding.get("claim") or "").strip():
            return False, "invalid_structured_worker_result"
        if not any(
            str(item).strip()
            for item in [
                *(finding.get("evidence_ids") or []),
                *(finding.get("artifact_ids") or []),
            ]
        ):
            return False, "invalid_structured_worker_result"
    return True, ""


def build_strict_json_retry_instruction(step: PlanStep) -> str:
    """Finalization-only retry contract: no retrieval, findings only."""
    worker = step.subagent or step.step_type
    return f"""
    【Finalization-only 重试 — 禁止再检索，只输出 JSON】
    上次回传缺少最终 AI JSON 或缺少 evidence-backed findings。禁止调用 internet_search / fetch_url / batch_search / batch_fetch，不要重新搜索或抓页。
    已抓取的网页如需核对，只用 read_artifact / read_evidence。
    evidence_ids / artifact_ids 必须逐字复制工具返回的真实 ID；禁止自造 E1、E2、source1 等编号。
    每个 finding 必须有 claim，并至少绑定一个真实 evidence_ids 或 artifact_ids；没有可绑定证据时不要输出 supported finding。
    请仅输出 JSON，不要任何解释文字：
    {{"ok":true,"summary":"...","findings":[{{"claim":"...","evidence_ids":["<exact runtime evidence id>"],"artifact_ids":["<exact runtime artifact id>"],"confidence":0.9}}],"gaps":[],"conflicts":[],"stop_reason":"local_evidence_sufficient","error_code":"","worker":"{worker}","step_type":"{step.step_type}"}}
    """


def extract_final_ai_content(messages: list[Any] | None) -> str:
    """Return only the last non-empty assistant message without tool calls."""
    for message in reversed(messages or []):
        if not is_assistant_message(message):
            continue
        if getattr(message, "tool_calls", None):
            continue
        content = message_text(message).strip()
        if content:
            return content
    return ""


def worker_payload_from_dict(
    raw: dict[str, Any] | None,
    *,
    step_type: str = "",
    subagent: str = "",
) -> WorkerResultPayload:
    """Rebuild a typed worker payload without losing evidence/artifact fields."""
    row = raw or {}
    findings = _normalize_findings(row.get("findings"))
    artifact_ids = [
        str(item) for item in row.get("artifact_ids") or [] if str(item).strip()
    ]
    for finding in findings:
        artifact_ids.extend(
            str(item)
            for item in finding.get("artifact_ids") or []
            if str(item).strip()
        )
    artifact_ids = list(dict.fromkeys(artifact_ids))[:20]
    return WorkerResultPayload(
        ok=bool(row.get("ok", True)),
        summary=str(row.get("summary") or ""),
        facts=[str(item) for item in row.get("facts") or [] if str(item).strip()],
        sources=[str(item) for item in row.get("sources") or [] if str(item).strip()],
        confidence=float(row.get("confidence", 1.0) or 1.0),
        error_code=str(row.get("error_code") or ""),
        worker=str(row.get("worker") or subagent),
        step_type=str(row.get("step_type") or step_type),
        findings=findings,
        gaps=[_normalize_gap(item) for item in row.get("gaps") or [] if item is not None][:8],
        conflicts=[_conflict_text(item) for item in row.get("conflicts") or [] if item is not None][:8],
        suggested_followups=[str(item) for item in row.get("suggested_followups") or [] if str(item).strip()][:6],
        evidence_ids=[str(item) for item in row.get("evidence_ids") or [] if str(item).strip()][:20],
        artifact_ids=artifact_ids,
        stop_reason=str(row.get("stop_reason") or ""),
    )


SYNTHESIS_STEP_TYPES = frozenset({"generate_markdown", "summarize", "convert_pdf"})


def aggregate_evidence_digest(step_results: list[StepResult]) -> dict[str, Any]:
    """【Phase 8】汇总多工人 facts/sources，供写报告步骤使用。"""
    facts_by_step: list[dict[str, Any]] = []
    all_facts: list[str] = []
    all_sources: list[str] = []
    seen_facts: set[str] = set()
    seen_sources: set[str] = set()

    for idx, result in enumerate(step_results, 1):
        payload = (result.metadata or {}).get("worker_payload") or {}
        if not isinstance(payload, dict):
            continue
        step_facts = [str(f) for f in payload.get("facts", []) if f]
        step_sources = [str(s) for s in payload.get("sources", []) if s]
        step_findings = [item for item in (payload.get("findings") or []) if item]
        step_eids = [str(x) for x in (payload.get("evidence_ids") or []) if x]
        summary = str(payload.get("summary", ""))
        facts_by_step.append(
            {
                "step_index": idx,
                "step_type": result.step_type,
                "summary": summary[:800],
                "facts": step_facts[:10],
                "sources": step_sources[:10],
                "findings": step_findings[:10],
                "evidence_ids": step_eids[:12],
                "confidence": payload.get("confidence", 1.0),
            }
        )
        for fact in step_facts:
            key = fact.strip().lower()
            if key and key not in seen_facts:
                seen_facts.add(key)
                all_facts.append(fact)
        for source in step_sources:
            key = source.strip().lower()
            if key and key not in seen_sources:
                seen_sources.add(key)
                all_sources.append(source)

    return {
        "facts_by_step": facts_by_step,
        "all_facts": all_facts[:40],
        "all_sources": all_sources[:30],
        "step_count": len(facts_by_step),
    }


def format_evidence_digest_for_prompt(
    digest: dict[str, Any],
    *,
    max_steps: int = 12,
) -> str:
    """格式化为写报告/汇总步骤的上下文块。超长时按步截断，引导 JIT 回读。"""
    if not digest.get("facts_by_step"):
        return ""
    blocks = list(digest["facts_by_step"] or [])
    truncated = 0
    if max_steps > 0 and len(blocks) > max_steps:
        truncated = len(blocks) - max_steps
        blocks = blocks[-max_steps:]
    lines = ["    【多源证据_digest — 写报告必须引用 evidence_id / [n]】"]
    if truncated:
        lines.append(
            f"    （仅最近 {max_steps} 步证据卡，省略 {truncated} 步；其余 read_evidence）"
        )
    for block in blocks:
        lines.append(
            f"  步骤{block['step_index']} [{block['step_type']}] "
            f"confidence={block.get('confidence', 1.0)}"
        )
        if block.get("summary"):
            lines.append(f"    摘要: {block['summary'][:400]}")
        for item in block.get("findings") or []:
            if isinstance(item, dict):
                claim = item.get("claim") or ""
                eids = ",".join(str(x) for x in (item.get("evidence_ids") or [])[:4])
                lines.append(f"    - 主张: {claim} evidence=[{eids or '-'}]")
        for fact in block.get("facts") or []:
            lines.append(f"    - 事实: {fact}")
        for src in block.get("sources") or []:
            lines.append(f"    - 来源: {src}")
        for eid in block.get("evidence_ids") or []:
            lines.append(f"    - evidence_id: {eid}")
    if digest.get("all_facts"):
        lines.append("    【合并事实清单】")
        for fact in digest["all_facts"][:25]:
            lines.append(f"    * {fact}")
    return "\n".join(lines)


def check_subagent_binding(
    step: PlanStep,
    assistants_called: list[str],
    *,
    enforce: bool,
) -> tuple[bool, str]:
    """校验计划指定的子 Agent 是否被调用。"""
    if not enforce or not step.subagent:
        return True, ""
    if step.subagent in assistants_called:
        return True, ""
    return False, "wrong_subagent"


def check_unauthorized_tools(
    step: PlanStep,
    tools_invoked: list[str],
    *,
    enforce: bool,
) -> tuple[bool, list[str]]:
    """校验本步是否调用了禁止工具（计划绑定）。

    JIT 回读工具始终允许：研究工人提示词会要求 read_artifact / read_evidence，
    计划白名单漏了它们时不应整步判越权重搜。
    """
    if not enforce:
        return True, []
    from app.agent.harness.worker_profiles import CONTEXT_TOOLS

    always_allowed = set(CONTEXT_TOOLS)
    if step.allowed_tools:
        allowed = set(step.allowed_tools) | always_allowed
        bad = [t for t in tools_invoked if t not in allowed]
        return len(bad) == 0, bad
    forbidden = FORBIDDEN_TOOLS_BY_STEP.get(step.step_type, frozenset())
    bad = [t for t in tools_invoked if t in forbidden and t not in always_allowed]
    return len(bad) == 0, bad


def build_worker_output_instruction(step: PlanStep) -> str:
    """【修改点】要求子 Agent 回传结构化 JSON（监督者解析）。"""
    if step.step_type not in SUBAGENT_STEP_TYPES:
        return ""
    return f"""
    【工人结构化回传 — 必须遵守】
    最终回复必须是纯 JSON，不要 markdown 代码块：
    {{
      "ok": true,
      "summary": "本步结论摘要",
      "findings": [
        {{"claim": "可核对的主张", "evidence_ids": ["<exact runtime evidence id>"], "artifact_ids": ["<exact runtime artifact id>"], "confidence": 0.8}}
      ],
      "gaps": ["尚未覆盖的问题"],
      "conflicts": ["来源冲突描述"],
      "stop_reason": "local_evidence_sufficient"
    }}
    你的最终交付物是 findings，不是搜索记录。每个 finding 的 claim 必须可验证，且至少绑定一个工具返回的真实 evidence_ids 或 artifact_ids；不要把网页全文贴回 JSON。
    evidence_ids / artifact_ids 必须逐字复制工具返回值；禁止自造 E1、E2、source1 等编号。没有可绑定证据时不得输出 supported finding。
    若失败：ok=false，并填写 error_code（如 search_empty / sql_empty / timeout）。
    """


class IdempotencyRegistry:
    """已完成步骤登记，避免 resume 重复调用外部工具。"""

    def __init__(self) -> None:
        self._completed: dict[str, StepResult] = {}

    def register(self, key: str, result: StepResult) -> None:
        self._completed[key] = result

    def get(self, key: str) -> Optional[StepResult]:
        return self._completed.get(key)

    def keys(self) -> list[str]:
        return list(self._completed.keys())


def _normalize_findings(raw: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for i, item in enumerate(raw[:20]):
            if isinstance(item, str) and item.strip():
                items.append(
                    {"claim_id": f"C{i+1}", "claim": item.strip(), "evidence_ids": []}
                )
            elif isinstance(item, dict) and (item.get("claim") or item.get("text")):
                claim = str(item.get("claim") or item.get("text") or "").strip()
                evidence_ids = [
                    str(value)
                    for value in [
                        *(item.get("evidence_ids") or []),
                        item.get("evidence_id") or "",
                    ]
                    if str(value).strip()
                ]
                artifact_ids = [
                    str(value)
                    for value in [
                        *(item.get("artifact_ids") or []),
                        item.get("artifact_id") or "",
                    ]
                    if str(value).strip()
                ]
                items.append(
                    {
                        "claim_id": str(item.get("claim_id") or f"C{i+1}"),
                        "claim": claim,
                        "evidence_ids": evidence_ids,
                        "artifact_ids": artifact_ids,
                        "confidence": _safe_confidence(item.get("confidence")),
                        "source_quality": str(item.get("source_quality") or "unknown"),
                    }
                )
    return items


def _safe_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value or 1.0)))
    except (TypeError, ValueError):
        return 0.65


def _conflict_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("claim") or item)
    return str(item)


def apply_step_status(plan: ExecutionPlan, completed_count: int) -> None:
    for idx, step in enumerate(plan.steps):
        if idx < completed_count:
            step.metadata["status"] = StepStatus.DONE.value
        else:
            step.metadata.setdefault("status", StepStatus.PENDING.value)


def message_text(msg: Any) -> str:
    """把 LangChain / dict 消息的 content 收成纯文本。"""
    content = (
        msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    )
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def _message_role(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role") or msg.get("type") or "").lower()
    type_attr = str(getattr(msg, "type", "") or "").lower()
    role_attr = str(getattr(msg, "role", "") or "").lower()
    class_name = type(msg).__name__.lower()
    return type_attr or role_attr or class_name


def is_tool_message(msg: Any) -> bool:
    role = _message_role(msg)
    class_name = type(msg).__name__.lower()
    return role in {"tool", "toolmessage"} or "toolmessage" in class_name


def is_assistant_message(msg: Any) -> bool:
    role = _message_role(msg)
    class_name = type(msg).__name__.lower()
    return (
        role in {"ai", "assistant", "aimessage", "aimessagechunk"}
        or "aimessage" in class_name
    )


def extract_last_assistant_text(messages: list[Any] | None) -> str:
    """倒着找最后一条有正文的助手消息，跳过 ToolMessage / 纯 tool_calls。"""
    for msg in reversed(messages or []):
        if is_tool_message(msg):
            continue
        text = message_text(msg).strip()
        if not text:
            continue
        if is_assistant_message(msg):
            return text
        role = _message_role(msg)
        if role in {"human", "user", "system", "humanmessage", "systemmessage"}:
            continue
        if text.startswith("{") or _extract_json_object(text) is not None:
            return text
    return ""


def salvage_payload_from_artifacts(
    payload: WorkerResultPayload,
    *,
    step: PlanStep | None = None,
    step_index: int = -1,
) -> WorkerResultPayload:
    """JSON 解析失败时，用本步已抓原文卡片拼一份可用载荷，避免整步重搜。"""
    if payload.facts or payload.sources or payload.findings:
        return payload
    try:
        from app.agent.harness.artifacts import get_artifact_store

        store = get_artifact_store()
    except Exception:
        return payload
    items = store.iter_artifacts()
    if not items:
        return payload
    if step_index >= 0:
        scoped = [
            a for a in items if int(getattr(a, "step_index", -1) or -1) == step_index
        ]
        if scoped:
            items = scoped
    items = items[-12:]
    sources: list[str] = []
    facts: list[str] = []
    ids: list[str] = []
    for art in items:
        ids.append(art.artifact_id)
        loc = str(getattr(art, "locator", "") or "")
        if loc.startswith("http"):
            sources.append(loc)
        title = str(getattr(art, "title", "") or art.artifact_id)
        snippet = str(getattr(art, "summary", "") or getattr(art, "content", "") or "")[
            :240
        ]
        if snippet:
            facts.append(f"{title}: {snippet}")
        elif loc and loc not in sources:
            sources.append(loc)
    if not facts and not sources:
        return payload
    payload.ok = True
    payload.error_code = ""
    payload.facts = facts[:10]
    payload.sources = list(dict.fromkeys(sources))[:10]
    payload.artifact_ids = list(dict.fromkeys(list(payload.artifact_ids or []) + ids))[
        :20
    ]
    if not payload.summary.strip():
        payload.summary = f"已根据 {len(ids)} 份已存原文整理要点，未重新检索。"
    if step is not None:
        payload.worker = payload.worker or (step.subagent or "")
        payload.step_type = payload.step_type or step.step_type
    return payload


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None

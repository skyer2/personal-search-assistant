"""
Harness 状态定义

定义显式 Agent Loop 的阶段枚举、计划结构和运行状态，
供 loop / validator / recovery 等模块共享。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from app.agent.harness.intent_slots import IntentSlots
from app.agent.harness.research_brief import ResearchBrief


class Phase(str, Enum):
    UNDERSTAND = "understand"
    PLAN = "plan"
    REPLAN = "replan"  # 【Phase 6】动态重规划
    BUILD_CONTEXT = "build_context"
    EXECUTE = "execute"
    PARALLEL_EXECUTE = "parallel_execute"  # 【Phase 7】检索步 fan-out
    COMPRESS = "compress"
    VALIDATE = "validate"
    RECOVER = "recover"
    FINALIZE = "finalize"
    ABORT = "abort"


class StepStatus(str, Enum):
    """【Phase 7】计划步状态（权威计划对象）。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TaskIntent:
    """任务理解阶段的结构化输出。"""

    raw_query: str
    summary: str
    needs_network: bool = False
    needs_file_read: bool = False
    deliverable: Literal["text", "md", "pdf"] = "text"
    keywords: list[str] = field(default_factory=list)
    # 【Phase 8】混合 Planner 元数据
    planner_source: Literal["rules", "llm", "rules+llm"] = "rules"
    intent_confidence: float = 1.0
    planner_reason: str = ""
    # 【Phase 14】结构化槽位 + 澄清
    slots: IntentSlots = field(default_factory=IntentSlots)
    rule_confidence: float = 1.0
    ambiguity_flags: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str = ""
    clarification_resolved: bool = False
    forbidden_sources: list[str] = field(default_factory=list)
    required_sources: list[str] = field(default_factory=list)
    planning_mode: str = ""
    brief: ResearchBrief = field(default_factory=ResearchBrief)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_query": self.raw_query,
            "summary": self.summary,
            "needs_network": self.needs_network,
            "needs_file_read": self.needs_file_read,
            "deliverable": self.deliverable,
            "keywords": list(self.keywords),
            "planner_source": self.planner_source,
            "intent_confidence": self.intent_confidence,
            "planner_reason": self.planner_reason,
            "slots": self.slots.to_dict(),
            "rule_confidence": self.rule_confidence,
            "ambiguity_flags": list(self.ambiguity_flags),
            "needs_clarification": self.needs_clarification,
            "clarification_question": self.clarification_question,
            "clarification_resolved": self.clarification_resolved,
            "forbidden_sources": list(self.forbidden_sources),
            "required_sources": list(self.required_sources),
            "planning_mode": self.planning_mode,
            "brief": self.brief.to_dict() if self.brief else {},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskIntent":
        deliverable = str(data.get("deliverable", "text"))
        if deliverable not in {"text", "md", "pdf"}:
            deliverable = "text"
        obj = cls(
            raw_query=str(data.get("raw_query", "")),
            summary=str(data.get("summary", "")),
            needs_network=bool(data.get("needs_network", False)),
            needs_file_read=bool(data.get("needs_file_read", False)),
            deliverable=deliverable,  # type: ignore[arg-type]
            keywords=[str(k) for k in (data.get("keywords") or [])],
            planner_source=data.get("planner_source", "rules"),  # type: ignore[arg-type]
            intent_confidence=float(data.get("intent_confidence", 1.0) or 1.0),
            planner_reason=str(data.get("planner_reason", "")),
            slots=IntentSlots.from_dict(data.get("slots")),
            rule_confidence=float(data.get("rule_confidence", 1.0) or 1.0),
            ambiguity_flags=[str(f) for f in (data.get("ambiguity_flags") or [])],
            needs_clarification=bool(data.get("needs_clarification", False)),
            clarification_question=str(data.get("clarification_question", "")),
            clarification_resolved=bool(data.get("clarification_resolved", False)),
            forbidden_sources=[str(x) for x in (data.get("forbidden_sources") or [])],
            required_sources=[str(x) for x in (data.get("required_sources") or [])],
            planning_mode=str(data.get("planning_mode") or ""),
            brief=ResearchBrief.from_dict(data.get("brief")),
        )
        if obj.brief.is_empty() and obj.raw_query:
            from app.agent.harness.research_brief import compile_research_brief

            obj.brief = compile_research_brief(task_query=obj.raw_query, intent=obj)
        return obj


@dataclass
class PlanStep:
    """执行计划中的单步。"""

    step_type: str
    description: str
    subagent: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)  # 【Phase 6】HITL edit / replan 元数据
    task_id: str = ""
    depends_on: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    objective: str = ""

    def resolved_task_id(self, index: int) -> str:
        return self.task_id or f"t{index}:{self.step_type}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_type": self.step_type,
            "description": self.description,
            "subagent": self.subagent,
            "metadata": dict(self.metadata or {}),
            "task_id": self.task_id,
            "depends_on": list(self.depends_on or []),
            "allowed_tools": list(self.allowed_tools or []),
            "objective": self.objective,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PlanStep":
        row = data or {}
        return cls(
            step_type=str(row.get("step_type", "")),
            description=str(row.get("description", "")),
            subagent=row.get("subagent"),
            metadata=dict(row.get("metadata") or {}),
            task_id=str(row.get("task_id") or ""),
            depends_on=[str(x) for x in (row.get("depends_on") or [])],
            allowed_tools=[str(x) for x in (row.get("allowed_tools") or [])],
            objective=str(row.get("objective") or ""),
        )


@dataclass
class ExecutionPlan:
    """计划生成阶段的结构化输出。"""

    steps: list[PlanStep] = field(default_factory=list)
    summary: str = ""
    plan_version: int = 1
    planning_mode: str = "template"
    research_brief: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "steps": [step.to_dict() for step in self.steps],
            "plan_version": int(self.plan_version or 1),
            "planning_mode": self.planning_mode or "template",
            "research_brief": self.research_brief or "",
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExecutionPlan":
        row = data or {}
        return cls(
            summary=str(row.get("summary", "")),
            steps=[PlanStep.from_dict(item) for item in (row.get("steps") or [])],
            plan_version=int(row.get("plan_version") or 1),
            planning_mode=str(row.get("planning_mode") or "template"),
            research_brief=str(row.get("research_brief") or ""),
        )


@dataclass
class StepResult:
    """单步执行结果。"""

    step_type: str
    content: str
    compressed_content: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhaseEvent:
    """阶段事件，写入 trace 供评测使用。"""

    phase: str
    status: str
    duration_ms: Optional[int] = None
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ValidationOutcome:
    passed: bool
    reason: str = ""
    severity: Literal["error", "warning"] = "error"


@dataclass
class LoopState:
    """Harness 运行时的完整状态。"""

    session_id: str
    phase: Phase = Phase.UNDERSTAND
    intent: Optional[TaskIntent] = None
    plan: Optional[ExecutionPlan] = None
    step_index: int = 0
    step_results: list[StepResult] = field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 2
    trace: list[PhaseEvent] = field(default_factory=list)
    final_content: str = ""
    assistants_called: list[str] = field(default_factory=list)
    recovery_hints: list[str] = field(default_factory=list)
    memory_facts: list[str] = field(default_factory=list)
    memory_records: list[Any] = field(default_factory=list)
    memory_recalled: bool = False
    memory_user_id: str = ""
    memory_tenant_id: str = "default"
    memory_wrap_untrusted: bool = False
    # 【Phase 18】请求级身份与项目来源台账
    memory_project_id: str = "default"
    memory_identity_ephemeral: bool = False
    memory_source_ledger: list[Any] = field(default_factory=list)
    tool_calls_count: int = 0
    step_validation_results: list[dict[str, Any]] = field(default_factory=list)
    compression_ratios: list[float] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    # 【Phase 6】Citation-First + Dynamic Re-plan
    replan_count: int = 0
    evidence_source_count: int = 0
    citation_coverage_rate: float = 0.0
    hallucination_rate: float = 0.0
    # 【Phase 7】多 Agent 编排
    completed_step_keys: list[str] = field(default_factory=list)
    task_fingerprint: str = ""
    resumed_from_checkpoint: bool = False
    # 【Phase 9】运行时观测计数（写入 run_summary / Langfuse / metrics API）
    obs_structured_checks: int = 0
    obs_structured_passes: int = 0
    obs_structured_retries: int = 0
    obs_parallel_batch_count: int = 0
    obs_parallel_steps_executed: int = 0
    obs_orchestration_violations: int = 0
    obs_binding_violations: int = 0
    obs_unauthorized_tool_hits: int = 0
    obs_estimated_tokens_saved: int = 0
    obs_step_message_tokens_peak: int = 0
    obs_context_budget_trims: int = 0
    working_notes: str = ""
    evidence_lookup_block: str = ""
    evidence_lookup: list[Any] = field(default_factory=list)
    research_brief_obj: Any = None
    obs_entity_retention_rates: list[float] = field(default_factory=list)
    obs_retention_patches: int = 0
    obs_fresh_threads: int = 0
    obs_tool_results_cleared: int = 0
    obs_evidence_retrieved_count: int = 0
    obs_evidence_used_count: int = 0
    obs_artifacts_stored: int = 0
    obs_cache_read_tokens: int = 0
    obs_context_efficiency: float = 0.0
    graph_thread_ids: list[str] = field(default_factory=list)
    numeric_citation_coverage: float = 0.0
    obs_memory_recalled_count: int = 0
    obs_memory_saved_count: int = 0
    obs_memory_recall_at_k: float = 0.0
    obs_memory_embedding_used: bool = False
    obs_memory_trust_filtered: int = 0
    obs_memory_sources_recorded: int = 0
    # 【Phase 13】运行时护栏中止
    abort_reason: str = ""
    abort_message: str = ""


@dataclass
class HarnessResult:
    """Harness 执行完成后的返回结构。"""

    session_id: str
    status: Literal["success", "partial", "failed", "cancelled"]
    content: str
    trace: list[PhaseEvent]
    artifacts: list[str] = field(default_factory=list)
    retry_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

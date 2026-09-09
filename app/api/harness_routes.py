"""Harness 能力清单：实验档 agent / direct，不是搜索产品。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.config.loader import get_harness_config
from app.research.planning.effort import HardCeiling

router = APIRouter(prefix="/api/harness", tags=["harness"])


@router.get("/capabilities")
def harness_capabilities() -> dict[str, Any]:
    config = get_harness_config()
    personal = getattr(config, "personal_search", {}) or {}
    hard = HardCeiling.from_config(config)
    return {
        "version": config.version,
        "product": "research-agent-harness",
        "not_a_search_engine": True,
        "control_model": {
            "slogan": "Brief and Supervisor own research semantics; RuntimePolicy owns execution safety",
            "brief": "user intent, constraints, deliverable, and success criteria",
            "supervisor": "research strategy and next research action",
            "worker": "isolated task execution",
            "coverage": "Brief-aligned sufficient / gap judgement over evidence-backed findings",
            "runtime_policy": "budget, retry, safety, and terminal semantics",
        },
        "experiment_modes": ["agent", "direct"],
        "default_mode": "agent",
        "environment_tools": ["internet_search", "fetch_url", "read_file_content"],
        "enabled_sources": personal.get("enabled_sources", {"web": True, "file": True}),
        "identity": {"tenant_id": "local", "user_id": "me"},
        "loop": [
            "simple_fact: brief fast path → researcher → answer",
            "agent: brief → supervisor → researchers → findings → coverage → synthesis",
            "direct (baseline only): single worker + search tool",
        ],
        "control_plane": {
            "domain": "app.research",
            "runtime": "langgraph",
            "worker_runtime": "WorkerExecutorV2",
            "synthesis_runtime": "SynthesisExecutor",
            "simple_fact_execution_path": "fast_path",
            "leaf": "langchain.create_agent",
            "progress_eval_enabled": bool(getattr(config, "progress_eval_enabled", True)),
        },
        "hard_ceiling": hard.to_dict(),
        "guardrails": {
            "max_tool_calls": config.max_tool_calls,
            "max_total_tokens": config.max_total_tokens,
            "max_run_sec": config.max_run_sec,
            "max_supervisor_iterations": config.max_replan_count,
            "max_plan_steps": config.max_plan_steps,
            "max_step_tool_calls": config.max_step_tool_calls,
            "max_parallel_workers": config.max_parallel_workers,
            "note": "Hard safety ceiling. Adaptive Effort can only clamp below these values.",
        },
        "adaptive_effort": {
            "enabled": bool(getattr(config, "effort_adaptive_enabled", True)),
            "module": "app.research.planning.effort",
            "estimator": "deterministic ComplexityEstimator from Research Brief IR",
            "clamp": "min(effort_request, hard_ceiling)",
            "gap_grant": "bounded next-iteration reserve; depletes remaining_*; never raises session ceiling",
            "parallelism": "run_budget.max_parallel_workers clamps Worker semaphore",
        },
        "task_understanding": {
            "ir": "StructuredResearchBrief",
            "not_intent": True,
            "not_plan": True,
            "fields": [
                "objective",
                "explicit_subjects",
                "key_questions",
                "constraints",
                "success_criteria",
                "source_requirements",
                "freshness_requirements",
                "deliverable",
            ],
        },
        "memory_enabled": bool(getattr(config, "memory_enabled", False)),
        "developer_mode": {
            "eval": True,
            "trace": True,
            "metrics": config.metrics_enabled,
        },
    }

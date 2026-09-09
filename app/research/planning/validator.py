"""Execution-plan projection validation: DAG, budget, and source policy."""

from __future__ import annotations

from typing import Any

from app.agent.harness.state import ExecutionPlan, PlanStep, TaskIntent
from app.research.planning.granularity import analyze_task_granularity
from app.research.planning.priority import match_coverage_keys
from app.research.planning.policy import SOURCE_TOOLS, SourcePolicy, parse_source_policy

RESEARCH_TYPES = frozenset({"research", "network_search", "file_read"})


def _covers_source(plan: ExecutionPlan, source: str) -> bool:
    need_tools = set(SOURCE_TOOLS.get(source, ()))
    for step in plan.steps:
        if step.step_type == {
            "web": "network_search",
            "file": "file_read",
        }.get(source):
            return True
        allowed = set(step.allowed_tools or [])
        if allowed & need_tools:
            return True
    return False


def _has_cycle(steps: list[PlanStep]) -> bool:
    ids = {step.task_id for step in steps if step.task_id}
    visiting: set[str] = set()
    seen: set[str] = set()
    deps = {step.task_id: list(step.depends_on or []) for step in steps if step.task_id}

    def visit(nid: str) -> bool:
        if nid in seen:
            return False
        if nid in visiting:
            return True
        visiting.add(nid)
        for dep in deps.get(nid, []):
            if dep in ids and visit(dep):
                return True
        visiting.remove(nid)
        seen.add(nid)
        return False

    return any(visit(tid) for tid in ids)


def validate_artifact_dependencies(plan: ExecutionPlan) -> list[str]:
    """Validate artifact producer reachability for required consumers."""
    issues: list[str] = []
    producers: dict[str, list[PlanStep]] = {}
    for step in plan.steps:
        meta = step.metadata if isinstance(step.metadata, dict) else {}
        artifact = str(meta.get("produces_artifact") or "")
        if artifact:
            producers.setdefault(artifact, []).append(step)

    for step in plan.steps:
        meta = step.metadata if isinstance(step.metadata, dict) else {}
        if bool(meta.get("optional")):
            continue
        for artifact in meta.get("requires_artifacts") or []:
            artifact_name = str(artifact)
            artifact_producers = producers.get(artifact_name, [])
            if not artifact_producers:
                issues.append(f"missing_artifact_producer:{artifact_name}:{step.task_id}")
                continue
            if all(
                bool((producer.metadata or {}).get("optional"))
                for producer in artifact_producers
            ):
                issues.append(
                    f"required_task_depends_on_optional_artifact:{artifact_name}:{step.task_id}"
                )
    return issues


def validate_required_research_contract(
    intent: TaskIntent,
    plan: ExecutionPlan,
) -> list[str]:
    """Evidence research needs required workers, not optional enrichment only."""
    research_steps = [
        step for step in plan.steps if step.step_type in RESEARCH_TYPES
    ]
    evidence_research = bool(
        intent.needs_network
        or intent.needs_file_read
        or research_steps
    )
    if not evidence_research:
        return []

    required_research = [
        step
        for step in research_steps
        if not bool((step.metadata or {}).get("optional"))
    ]
    issues: list[str] = []
    if not required_research:
        issues.append("no_required_research_task")
        return issues

    brief = getattr(intent, "brief", None)
    if _brief_task_kind(brief) == "landscape_discovery" and any(
        str((step.metadata or {}).get("task_kind") or "")
        in {"discovery", "landscape_discovery"}
        for step in required_research
    ):
        return issues
    dimensions = [
        str(item)
        for item in (getattr(brief, "dimensions", None) or [])
        if str(item).strip()
    ]
    for dimension in dimensions:
        required_covered = any(
            match_coverage_keys(
                " ".join(
                    [
                        str(step.objective or ""),
                        str(step.description or ""),
                        " ".join(
                            str(item)
                            for item in (step.metadata or {}).get("coverage_keys")
                            or []
                        ),
                    ]
                ),
                [dimension],
            )
            for step in required_research
        )
        optional_covered = any(
            match_coverage_keys(
                " ".join(
                    [
                        str(step.objective or ""),
                        str(step.description or ""),
                        " ".join(
                            str(item)
                            for item in (step.metadata or {}).get("coverage_keys")
                            or []
                        ),
                    ]
                ),
                [dimension],
            )
            for step in research_steps
            if bool((step.metadata or {}).get("optional"))
        )
        if optional_covered and not required_covered:
            issues.append(f"core_dimension_only_optional:{dimension}")
    return issues


def _brief_task_kind(brief: Any) -> str:
    value = getattr(brief, "task_kind", "")
    if isinstance(brief, dict):
        value = brief.get("task_kind")
    return str(value or "")


def _step_subject_id(step: PlanStep, brief: Any) -> str:
    explicit = str((step.metadata or {}).get("subject_id") or "").strip()
    if explicit:
        return explicit
    subjects = getattr(brief, "subjects", None)
    if isinstance(brief, dict):
        subjects = brief.get("subjects")
    objective = f"{step.objective or ''} {step.description or ''}".lower()
    for subject in subjects or []:
        if isinstance(subject, dict):
            canonical = str(subject.get("canonical") or "").lower()
            aliases = [str(x).lower() for x in (subject.get("aliases") or [])]
        else:
            canonical = str(getattr(subject, "canonical", "") or "").lower()
            aliases = [str(x).lower() for x in (getattr(subject, "aliases", None) or [])]
        if canonical and canonical in objective or any(alias and alias in objective for alias in aliases):
            subject_id = (
                subject.get("subject_id")
                if isinstance(subject, dict)
                else getattr(subject, "subject_id", "")
            )
            return str(subject_id or canonical)
    return "general"


def validate_hybrid_plan(
    intent: TaskIntent,
    plan: ExecutionPlan,
    *,
    policy: SourcePolicy | None = None,
    max_plan_steps: int = 12,
    max_research_tasks: int = 6,
) -> list[str]:
    issues: list[str] = []
    policy = policy or parse_source_policy(intent.raw_query)
    if not plan.steps:
        return ["empty_plan"]
    if any(step.step_type not in RESEARCH_TYPES for step in plan.steps):
        return ["non_research_step_in_plan"]

    if len(plan.steps) > max_plan_steps:
        issues.append("too_many_steps")

    research_count = sum(1 for s in plan.steps if s.step_type in RESEARCH_TYPES)
    if research_count > max_research_tasks:
        issues.append("too_many_research_tasks")

    ids = [s.task_id for s in plan.steps if s.task_id]
    if len(ids) != len(set(ids)):
        issues.append("duplicate_task_id")
    id_set = set(ids)

    for step in plan.steps:
        for dep in step.depends_on or []:
            if dep and dep not in id_set:
                issues.append("missing_dependency")

    if _has_cycle(plan.steps):
        issues.append("cycle_in_dependencies")

    issues.extend(validate_artifact_dependencies(plan))
    issues.extend(validate_required_research_contract(intent, plan))

    brief = getattr(intent, "brief", None)
    brief_kind = _brief_task_kind(brief)
    brief_subject_ids = {
        str(
            (subject.get("subject_id") if isinstance(subject, dict) else getattr(subject, "subject_id", ""))
            or ""
        )
        for subject in (
            brief.get("subjects", [])
            if isinstance(brief, dict)
            else getattr(brief, "subjects", None) or []
        )
    }
    seen_research: set[tuple[str, str, tuple[str, ...]]] = set()
    for step in plan.steps:
        if step.step_type not in RESEARCH_TYPES:
            continue
        metadata = step.metadata or {}
        task_kind = str(
            metadata.get("task_kind")
            or ("discovery" if step.step_type == "network_search" else "deep_dive")
        )
        subject_id = _step_subject_id(step, brief)
        if brief_kind == "landscape_discovery" and task_kind not in {
            "discovery",
            "landscape_discovery",
            "gap_fill",
        }:
            issues.append(f"category_requires_discovery:{step.task_id}")
        if task_kind == "gap_fill":
            if brief_kind != "landscape_discovery":
                issues.append(f"invalid_gap_fill_context:{step.task_id}")
            if not (metadata.get("resolves_gap_ids") or []):
                issues.append(f"invalid_gap_fill_missing_gap:{step.task_id}")
            if not (metadata.get("coverage_keys") or []):
                issues.append(f"invalid_gap_fill_missing_coverage:{step.task_id}")
            if "candidate_set" not in (metadata.get("requires_artifacts") or []):
                issues.append(f"invalid_gap_fill_missing_candidate_set:{step.task_id}")
            if not (metadata.get("entities") or []):
                issues.append(f"invalid_gap_fill_missing_entity:{step.task_id}")
            if subject_id in brief_subject_ids | {"general", ""}:
                issues.append(f"category_gap_too_broad:{step.task_id}")
        if (
            task_kind == "comparison"
            or step.task_id == "t_compare"
            or (
                "横向比较" in f"{step.objective or ''} {step.description or ''}"
                and len(step.depends_on or []) >= 2
            )
        ):
            issues.append(f"comparison_is_synthesis:{step.task_id}")
        coverage = tuple(str(x) for x in (metadata.get("coverage_keys") or []))
        research_key = (subject_id, task_kind, coverage)
        if research_key in seen_research:
            issues.append(f"duplicate_subject_task:{step.task_id}:{subject_id}")
        seen_research.add(research_key)
        complexity = analyze_task_granularity(step, brief)
        if complexity.oversized:
            issues.append(
                "task_too_large:"
                f"{step.task_id}:entities={complexity.entity_count},"
                f"dimensions={complexity.dimension_count},cells={complexity.estimated_cells}"
            )

    if intent.needs_network and "web" not in policy.forbidden_sources:
        if not _covers_source(plan, "web"):
            issues.append("missing_network_search")
    if intent.needs_file_read and "file" not in policy.forbidden_sources:
        if not _covers_source(plan, "file"):
            issues.append("missing_file_read")

    return issues


def validate_execution_plan(
    plan: Any,
    *,
    spec: Any | None = None,
    coverage_contract: Any | None = None,
    candidate_set: Any | None = None,
    max_tasks: int = 12,
) -> list[str]:
    """Structural and semantic validation for the canonical spec-driven plan."""
    issues: list[str] = []
    steps = list(getattr(plan, "steps", None) or [])
    if not steps:
        return ["empty_plan"]
    if any(step.step_type not in RESEARCH_TYPES for step in steps):
        issues.append("non_research_step")
    ids = [step.task_id for step in steps]
    if any(not task_id for task_id in ids):
        issues.append("missing_task_id")
    if len(ids) != len(set(ids)):
        issues.append("duplicate_task_id")
    id_set = set(ids)
    for step in steps:
        for dependency in step.depends_on or []:
            if dependency not in id_set:
                issues.append(f"missing_dependency:{step.task_id}")
    if _has_cycle(steps):
        issues.append("cycle_in_dependencies")
    if len(steps) > max_tasks:
        issues.append("too_many_tasks")
    if any("web" not in (step.metadata or {}).get("allowed_sources", ["web"]) for step in steps):
        issues.append("forbidden_source")

    units = list(getattr(coverage_contract, "units", None) or [])
    required_coverage_ids = {
        str(getattr(unit, "coverage_id", "") or (unit.get("coverage_id") if isinstance(unit, dict) else ""))
        for unit in units
    }
    owned_coverage_ids = {
        str(item)
        for step in steps
        for item in (step.metadata or {}).get("coverage_ids", [])
    }
    shape = str(getattr(spec, "task_shape", "") or (spec.get("task_shape") if isinstance(spec, dict) else ""))
    if shape not in {"BREADTH_HEAVY", "DYNAMIC_DISCOVERY"}:
        missing = required_coverage_ids - owned_coverage_ids
        if missing:
            issues.append(f"required_coverage_without_owner:{len(missing)}")
    else:
        discovery = [
            step
            for step in steps
            if str((step.metadata or {}).get("task_kind") or "") == "discovery"
        ]
        expanded = bool(candidate_set.get("expanded")) if isinstance(candidate_set, dict) else bool(getattr(candidate_set, "expanded", False))
        if not discovery and not expanded:
            issues.append("landscape_missing_discovery_contract")
        if discovery and not any((step.metadata or {}).get("produces_artifact") == "candidate_set" for step in discovery):
            issues.append("discovery_missing_candidate_artifact")

    for step in steps:
        metadata = step.metadata or {}
        if str(metadata.get("task_kind") or "") == "gap_fill":
            if not metadata.get("resolves_gap_ids"):
                issues.append(f"gap_fill_missing_gap_binding:{step.task_id}")
            if not metadata.get("coverage_ids") and not metadata.get("coverage_keys"):
                issues.append(f"gap_fill_missing_coverage:{step.task_id}")
        if str(metadata.get("task_kind") or "") == "deep_dive" and shape in {"BREADTH_HEAVY", "DYNAMIC_DISCOVERY"}:
            if not metadata.get("subject_id", "").startswith("candidate:"):
                issues.append(f"landscape_deep_dive_missing_candidate:{step.task_id}")
    return issues


__all__ = [
    "validate_artifact_dependencies",
    "validate_execution_plan",
]

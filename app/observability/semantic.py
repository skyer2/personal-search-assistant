"""Semantic lineage helpers: stable gap ids, coverage, earliest failure origin."""

from __future__ import annotations

import hashlib
from typing import Any


def stable_gap_id(gap_type: str, key: str) -> str:
    digest = hashlib.sha1(f"{str(gap_type)}:{str(key).strip().lower()}".encode("utf-8")).hexdigest()[:10]
    return f"gap_{digest}"


def materialize_gap_items(
    *,
    coverage_gaps: list[str] | None = None,
    missing_dimensions: list[str] | None = None,
    conflicts: list[str] | None = None,
    stale_evidence: list[str] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(gap_type: str, key: str, *, dimension: str = "", severity: str = "high") -> None:
        text = str(key or "").strip()
        if not text:
            return
        gap_id = stable_gap_id(gap_type, text)
        if gap_id in seen:
            return
        seen.add(gap_id)
        items.append(
            {
                "gap_id": gap_id,
                "type": gap_type,
                "dimension": dimension or text,
                "severity": severity,
                "description": text[:240],
            }
        )

    for item in coverage_gaps or []:
        _add("coverage", str(item), severity="high")
    for item in missing_dimensions or []:
        _add("missing_dimension", str(item), dimension=str(item), severity="high")
    for item in conflicts or []:
        _add("conflict", str(item), severity="high")
    for item in stale_evidence or []:
        _add("stale", str(item), severity="medium")
    return items


def plan_brief_coverage(brief: dict[str, Any] | None, plan: Any) -> dict[str, Any]:
    dimensions = [str(x) for x in ((brief or {}).get("dimensions") or []) if str(x).strip()]
    entities = [str(x) for x in ((brief or {}).get("entities") or []) if str(x).strip()]
    steps = list(getattr(plan, "steps", None) or [])
    blob = " ".join(
        f"{getattr(step, 'objective', '')} {getattr(step, 'description', '')} {getattr(step, 'task_id', '')}"
        for step in steps
    ).lower()
    dim_hits = {dim: (dim.lower() in blob) for dim in dimensions}
    entity_hits = {ent: (ent.lower() in blob) for ent in entities}
    missing = [dim for dim, hit in dim_hits.items() if not hit]
    return {
        "dimensions": dim_hits,
        "entities": entity_hits,
        "missing_dimensions": missing,
        "coverage_rate": (sum(1 for hit in dim_hits.values() if hit) / len(dim_hits)) if dim_hits else None,
    }


def earliest_failure_origin(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer earliest evaluated failing semantic stage over late operational errors."""
    stage_rank = {
        "understand": 10,
        "planning": 20,
        "plan": 20,
        "worker": 30,
        "retrieval": 35,
        "evidence": 40,
        "progress": 50,
        "replan": 60,
        "synthesis": 70,
        "quality": 80,
        "runtime": 90,
        "tool": 35,
    }
    candidates: list[dict[str, Any]] = []
    for event in events:
        attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
        origin = attrs.get("failure.origin_stage") or attrs.get("failure.stage")
        if not origin and str(event.get("type") or "").endswith(".failed"):
            origin = attrs.get("failure.stage") or "runtime"
        if not origin:
            # soft quality miss: eval.scored / quality.evaluated failed
            event_type = str(event.get("type") or event.get("event") or "")
            if event_type in {"eval.scored", "quality.evaluated"} and (
                attrs.get("passed") is False or event.get("status") in {"fail", "failed"}
            ):
                origin = str(attrs.get("target_type") or attrs.get("failure.origin_stage") or "quality")
            elif event_type == "progress.evaluated" and str(attrs.get("verdict") or "") == "gap":
                # gap itself is not a failure unless never closed — skip here
                continue
            else:
                continue
        candidates.append(
            {
                "origin_stage": str(origin),
                "detected_stage": str(attrs.get("failure.detected_stage") or event.get("phase") or origin),
                "type": attrs.get("failure.type") or attrs.get("metric") or event.get("type"),
                "cause_event_id": attrs.get("failure.cause_event_id") or event.get("event_id"),
                "cause_artifact_id": attrs.get("failure.cause_artifact_id")
                or attrs.get("target_artifact_id")
                or attrs.get("brief_id")
                or attrs.get("plan_id"),
                "reason": attrs.get("fail_reason") or attrs.get("reason") or attrs.get("error"),
                "timestamp": event.get("timestamp"),
                "rank": stage_rank.get(str(origin).lower(), 100),
                "seq": int(event.get("seq") or 0),
            }
        )
    if not candidates:
        return None
    candidates.sort(key=lambda row: (row["rank"], row["seq"]))
    best = candidates[0]
    return {k: v for k, v in best.items() if k not in {"rank"}}


def compute_replan_gap_closure(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Replan useful iff targeted gaps later appear in resolved_gap_ids."""
    targeted: list[str] = []
    resolved: set[str] = set()
    applied = 0
    for event in events:
        event_type = str(event.get("type") or event.get("event") or "")
        attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
        if event_type == "replan.applied":
            applied += 1
            for gap_id in attrs.get("target_gap_ids") or []:
                if gap_id:
                    targeted.append(str(gap_id))
        if event_type == "progress.evaluated":
            for gap_id in attrs.get("resolved_gap_ids") or []:
                if gap_id:
                    resolved.add(str(gap_id))
    unique_targets = list(dict.fromkeys(targeted))
    closed = [gap_id for gap_id in unique_targets if gap_id in resolved]
    waste = [gap_id for gap_id in unique_targets if gap_id not in resolved]
    closure_rate = (len(closed) / len(unique_targets)) if unique_targets else None
    return {
        "replan_applied": applied,
        "target_gap_ids": unique_targets,
        "resolved_gap_ids": sorted(resolved),
        "closed_gap_ids": closed,
        "waste_gap_ids": waste,
        "gap_closure_rate": closure_rate,
        "replan_useful": bool(closed) if unique_targets else False,
        "needless_replan": bool(applied and not unique_targets),
    }


def build_lineage_edges(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for event in events:
        attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
        inputs = list(event.get("input_refs") or attrs.get("input_refs") or [])
        outputs = list(event.get("output_refs") or attrs.get("output_refs") or [])
        if not inputs and not outputs:
            # synthesize from common id fields
            in_ids = []
            out_ids = []
            for key in ("brief_id", "plan_id", "progress_id"):
                if attrs.get(key):
                    in_ids.append({"type": key.replace("_id", ""), "id": attrs.get(key)})
            for key in ("brief_id", "plan_id", "answer_id", "patch_id", "evidence_id", "finding_id"):
                if attrs.get(key) and key.endswith("_id"):
                    out_ids.append({"type": key.replace("_id", ""), "id": attrs.get(key)})
            inputs = in_ids
            outputs = out_ids
        if not outputs:
            continue
        for out_ref in outputs:
            out = out_ref if isinstance(out_ref, dict) else {"id": str(out_ref)}
            parents = inputs or [{"type": "event", "id": event.get("event_id")}]
            for in_ref in parents:
                src = in_ref if isinstance(in_ref, dict) else {"id": str(in_ref)}
                edges.append(
                    {
                        "from_type": src.get("type"),
                        "from_id": src.get("id"),
                        "to_type": out.get("type"),
                        "to_id": out.get("id"),
                        "via_event": event.get("type") or event.get("event"),
                        "span_id": event.get("span_id"),
                    }
                )
    return edges

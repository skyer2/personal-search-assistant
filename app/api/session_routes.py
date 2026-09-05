"""Durable session / run projection APIs.

Frontend state is a projection. These endpoints restore UX after refresh / reconnect.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.observability.replay import (
    load_events,
    load_lineage_projection,
    load_summary_projection,
    load_trace_payload,
    load_tree_projection,
    load_wire_events,
)
from app.run_store import get_run_store
from app.run_store.files import list_output_files, list_run_output_files, resolve_output_file

router = APIRouter()

_APP_ROOT = Path(__file__).resolve().parents[1]
_OUTPUT_DIR = _APP_ROOT / "output"
_UPDATED_DIR = _APP_ROOT / "updated"
_TRACES_DIR = _APP_ROOT / "logs" / "traces"


@router.get("/api/sessions")
async def list_sessions(
    tenant_id: str | None = None,
    include_archived: bool = False,
):
    """Session 列表（租户隔离；归档 Session 默认不参与）。"""
    store = get_run_store()
    sessions = store.list_sessions(
        tenant_id=_tenant_of(tenant_id),
        include_archived=include_archived,
    )
    return {
        "tenant_id": _tenant_of(tenant_id),
        "sessions": [item.to_dict() for item in sessions],
    }


@router.post("/api/sessions/{session_id}/archive")
async def archive_session(session_id: str, tenant_id: str | None = None):
    store = get_run_store()
    updated = store.set_session_archived(
        session_id,
        archived=True,
        tenant_id=_tenant_of(tenant_id),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="session_not_found_or_forbidden")
    return {"session_id": session_id, "archived": True}


@router.post("/api/sessions/{session_id}/unarchive")
async def unarchive_session(session_id: str, tenant_id: str | None = None):
    store = get_run_store()
    updated = store.set_session_archived(
        session_id,
        archived=False,
        tenant_id=_tenant_of(tenant_id),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="session_not_found_or_forbidden")
    return {"session_id": session_id, "archived": False}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, tenant_id: str | None = None):
    """删除 Session：tombstone + 级联清理 runs / uploads / 工作区 / trace。"""
    store = get_run_store()
    result = store.delete_session(
        session_id,
        tenant_id=_tenant_of(tenant_id),
        output_root=_OUTPUT_DIR,
        updated_root=_UPDATED_DIR,
        traces_root=_TRACES_DIR,
    )
    if result.get("reason") == "forbidden":
        raise HTTPException(status_code=403, detail="forbidden")
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail="session_not_found")
    return result


@router.delete("/api/runs/{run_id}")
async def delete_run(run_id: str, tenant_id: str | None = None):
    """删除单次 Run 及其 Run-owned 数据。"""
    store = get_run_store()
    result = store.delete_run(
        run_id,
        tenant_id=_tenant_of(tenant_id),
        output_root=_OUTPUT_DIR,
    )
    if result.get("reason") == "forbidden":
        raise HTTPException(status_code=403, detail="forbidden")
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail="run_not_found")
    return result


@router.post("/api/admin/retention/apply")
async def apply_retention_policy(
    intermediate_days: int = 7,
    trace_days: int = 90,
):
    from app.run_store.retention import RetentionPolicy, apply_retention

    result = apply_retention(
        get_run_store(),
        policy=RetentionPolicy(
            intermediate_retention_days=max(0, intermediate_days),
            trace_retention_days=max(0, trace_days),
        ),
        output_root=_OUTPUT_DIR,
    )
    return result


def _tenant_of(requested: str | None) -> str:
    # 单机版默认 local；企业部署替换为认证中间件注入的 tenant
    return (requested or "local").strip() or "local"


@router.get("/api/sessions/{session_id}/bootstrap")
async def bootstrap_session(session_id: str):
    store = get_run_store()
    current = store.latest_run(session_id)
    events = []
    if current:
        events = load_wire_events(
            session_id,
            run_id=current.run_id,
            after_seq=0,
            limit=120,
            replay=True,
        )
    payload = store.bootstrap(session_id, output_root=_OUTPUT_DIR, events=events)
    return payload.to_dict()


@router.get("/api/sessions/{session_id}/uploads")
async def list_session_uploads(session_id: str):
    store = get_run_store()
    if store.get_session(session_id) is None and not store.list_uploads(session_id):
        return {"session_id": session_id, "files": []}
    return {
        "session_id": session_id,
        "files": [item.to_dict() for item in store.list_uploads(session_id)],
    }


@router.get("/api/sessions/{session_id}/artifacts")
async def list_session_artifacts(session_id: str):
    return {
        "session_id": session_id,
        "files": list_output_files(_OUTPUT_DIR, session_id),
    }


@router.get("/api/sessions/{session_id}/download")
async def download_session_artifact(session_id: str, name: str, download: bool = False):
    try:
        target = resolve_output_file(_OUTPUT_DIR, session_id, name)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    is_pdf = target.suffix.lower() == ".pdf"
    return FileResponse(
        target,
        filename=target.name,
        media_type="application/pdf" if is_pdf else None,
        content_disposition_type="attachment" if download or not is_pdf else "inline",
    )


@router.get("/api/sessions/{session_id}/traces")
async def list_session_traces(session_id: str):
    store = get_run_store()
    runs = store.list_runs(session_id)
    return {
        "session_id": session_id,
        "traces": [
            {
                "run_id": run.run_id,
                "session_id": run.session_id,
                "status": run.status,
                "query": run.query,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "ended_at": run.ended_at,
                "last_event_seq": run.last_event_seq,
            }
            for run in runs
        ],
        "current_run_id": runs[-1].run_id if runs else None,
    }


@router.get("/api/runs/{run_id}/trace")
async def get_run_trace(run_id: str):
    run = get_run_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    payload = load_trace_payload(run.session_id, run_id=run_id)
    payload["status"] = run.status
    payload["query"] = run.query
    return payload


@router.get("/api/runs/{run_id}/summary")
async def get_run_trace_summary(run_id: str):
    """Lightweight first paint: no raw events, tree, or lineage edge payload."""
    run = get_run_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    payload = load_summary_projection(run.session_id, run_id=run_id)
    summary = dict(payload.get("summary") or {})
    records = [
        event.to_jsonl_record() for event in load_events(run.session_id, run_id=run_id)
    ]
    span_count = len(
        {str(row.get("span_id") or row.get("event_id")) for row in records}
    )
    lineage_count = sum(
        len(row.get("input_refs") or []) * len(row.get("output_refs") or [])
        for row in records
    )
    summary["counts"] = {
        "events": int(payload.get("total") or 0),
        "lineage": lineage_count,
        "evidence": len(summary.get("evidence") or []),
        "spans": span_count,
    }
    summary["status"] = run.status
    summary["started_at"] = run.started_at
    summary["ended_at"] = run.ended_at
    return {
        "run_id": run_id,
        "session_id": run.session_id,
        "summary": summary,
        "total": payload.get("total", 0),
    }


@router.get("/api/runs/{run_id}/lineage")
async def get_run_lineage(
    run_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    run = get_run_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    rows = load_lineage_projection(run.session_id, run_id=run_id)
    return {
        "run_id": run_id,
        "items": rows[offset : offset + limit],
        "offset": offset,
        "limit": limit,
        "total": len(rows),
    }


@router.get("/api/runs/{run_id}/tree")
async def get_run_tree(run_id: str):
    run = get_run_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    tree = load_tree_projection(run.session_id, run_id=run_id)
    return {"run_id": run_id, "tree": tree, "total": tree.get("event_count", 0)}


@router.get("/api/runs/{run_id}/evidence")
async def get_run_evidence(
    run_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    import json

    run = get_run_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    path = _OUTPUT_DIR / f"session_{run.session_id}" / "runs" / run_id / "evidence.json"
    if not path.exists():
        return {
            "run_id": run_id,
            "session_id": run.session_id,
            "sources": [],
            "total": 0,
            "message": "本 run 暂无证据链",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="evidence_unreadable") from exc
    rows = list(payload.get("sources") or [])
    return {
        "run_id": run_id,
        "session_id": run.session_id,
        "sources": rows[offset : offset + limit],
        "total": len(rows),
        "generated_at": payload.get("generated_at"),
    }


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    run = get_run_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return run.to_dict()


@router.get("/api/runs/{run_id}/events")
async def list_run_events(
    run_id: str,
    after_seq: int = Query(default=0, ge=0),
    before_seq: int | None = Query(default=None, ge=0),
    limit: int = Query(default=120, ge=1, le=2000),
):
    run = get_run_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    events = load_wire_events(
        run.session_id,
        run_id=run_id,
        after_seq=after_seq,
        before_seq=before_seq,
        limit=limit,
        replay=True,
    )
    return {
        "run_id": run_id,
        "session_id": run.session_id,
        "events": events,
        "last_event_seq": run.last_event_seq,
        "count": len(events),
    }


@router.get("/api/runs/{run_id}/artifacts")
async def list_run_artifacts(run_id: str):
    run = get_run_store().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return {
        "run_id": run_id,
        "session_id": run.session_id,
        # Run endpoint 必须 Run Scope：禁止借 session_id 列出整段 Session 历史
        "files": list_run_output_files(_OUTPUT_DIR, run.session_id, run_id),
    }

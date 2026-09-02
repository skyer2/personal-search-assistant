"""Durable session / run projection APIs.

Frontend state is a projection. These endpoints restore UX after refresh / reconnect.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.observability.replay import load_trace_payload, load_wire_events
from app.run_store import get_run_store
from app.run_store.files import list_output_files, resolve_output_file

router = APIRouter()

_APP_ROOT = Path(__file__).resolve().parents[1]
_OUTPUT_DIR = _APP_ROOT / "output"


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
        "files": list_output_files(_OUTPUT_DIR, run.session_id),
    }

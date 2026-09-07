"""Long-term memory management APIs.

Conversation history and long-term memory are separate stores. These endpoints
expose only memory records and explicit forget operations.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.agent.memory import get_memory_store

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _identity_defaults(tenant_id: str | None, user_id: str | None, project_id: str | None):
    return {
        "tenant_id": (tenant_id or "local").strip() or "local",
        "user_id": (user_id or "me").strip() or "me",
        "project_id": (project_id or "").strip(),
    }


@router.get("/records")
async def list_memory_records(
    tenant_id: str | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
    include_deleted: bool = False,
):
    identity = _identity_defaults(tenant_id, user_id, project_id)
    store = get_memory_store()
    records = store.list_records(
        identity["user_id"],
        tenant_id=identity["tenant_id"],
        include_deleted=include_deleted,
        project_id=identity["project_id"],
    )
    return {
        **identity,
        "total": len(records),
        "records": [record.to_dict() for record in records],
    }


@router.delete("/records/{record_id}")
async def forget_memory_record(
    record_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
):
    identity = _identity_defaults(tenant_id, user_id, None)
    deleted = await get_memory_store().delete(
        record_id,
        identity["user_id"],
        tenant_id=identity["tenant_id"],
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="memory_record_not_found")
    return {"deleted": True, "record_id": record_id}


@router.post("/forget-session/{session_id}")
async def forget_session_memory(
    session_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
):
    identity = _identity_defaults(tenant_id, user_id, None)
    deleted = await get_memory_store().forget_session(
        session_id,
        user_id=identity["user_id"],
        tenant_id=identity["tenant_id"],
    )
    return {"deleted": deleted, "session_id": session_id}


@router.post("/forget-user")
async def forget_user_memory(
    tenant_id: str | None = None,
    user_id: str | None = None,
):
    identity = _identity_defaults(tenant_id, user_id, None)
    deleted = await get_memory_store().forget_user(
        identity["user_id"],
        tenant_id=identity["tenant_id"],
    )
    return {"deleted": deleted, "user_id": identity["user_id"]}

"""Agent Worker maintenance API: submit, observe, resume events, cancel, retry."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.identity import resolve_uid
from app.runtime.maintenance import (
    cancel_operation,
    environment_health,
    event_payload,
    event_stream,
    events_after,
    list_operations,
    operation_detail,
    operation_payload,
    reconcile_operation,
    retry_operation,
    submit_operation,
)

router = APIRouter()


class MaintenanceSubmit(BaseModel):
    action: Literal["check", "deploy", "update", "restart", "disable", "restore"]


def maintenance_actor(
    x_verified_uid: str | None = Header(default=None, alias="X-Verified-UID"),
    x_owner_id: str | None = Header(default=None, alias="X-Owner-Id"),
    nexus_uid: str | None = Cookie(default=None, alias="nexus_uid"),
) -> str:
    """Resolve the acting UID; the request body never decides who acted."""

    return resolve_uid(x_verified_uid, x_owner_id, nexus_uid)


@router.post("/api/agent-workers/{environment_id}/maintenance", status_code=202)
async def submit_maintenance(
    environment_id: int,
    payload: MaintenanceSubmit,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(maintenance_actor),
):
    operation = await submit_operation(
        session,
        environment_id,
        payload.action,
        requested_by=actor,
    )
    return operation_payload(operation)


@router.get("/api/agent-workers/{environment_id}/maintenance")
async def maintenance_history(
    environment_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    before_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    return {
        "items": await list_operations(
            session, environment_id, limit=limit, before_id=before_id
        )
    }


@router.get("/api/maintenance/{operation_id}")
async def maintenance_detail(
    operation_id: str, session: AsyncSession = Depends(get_session)
):
    detail = await operation_detail(session, operation_id)
    if detail is None:
        raise HTTPException(404, "维护操作不存在")
    return detail


@router.get("/api/maintenance/{operation_id}/events/page")
async def maintenance_event_page(
    operation_id: str,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    detail = await operation_detail(session, operation_id)
    if detail is None:
        raise HTTPException(404, "维护操作不存在")
    events = await events_after(session, operation_id, after_id, limit=limit)
    items = [event_payload(item) for item in events]
    return {
        "items": items,
        "next_cursor": items[-1]["id"] if len(items) == limit else None,
    }


@router.get("/api/maintenance/{operation_id}/events")
async def maintenance_events(
    operation_id: str,
    after_id: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    session: AsyncSession = Depends(get_session),
):
    detail = await operation_detail(session, operation_id)
    if detail is None:
        raise HTTPException(404, "维护操作不存在")
    cursor = after_id
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError:
            pass

    async def stream():
        from app.database import SessionLocal
        from app.models import WorkerMaintenanceOperation

        async for item in event_stream(operation_id, cursor):
            if item.get("type") == "keepalive":
                yield ": keepalive\n\n"
                continue
            encoded = json.dumps(item, ensure_ascii=False)
            yield f"id: {item['id']}\nevent: {item.get('type') or 'message'}\ndata: {encoded}\n\n"
        async with SessionLocal() as db:
            operation = await db.get(WorkerMaintenanceOperation, operation_id)
            state = operation.state if operation is not None else "UNKNOWN"
        yield (
            "event: end\n"
            f"data: {json.dumps({'operation_id': operation_id, 'state': state}, ensure_ascii=False)}\n\n"
        )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/api/maintenance/{operation_id}/cancel")
async def cancel_maintenance(
    operation_id: str,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(maintenance_actor),
):
    operation = await cancel_operation(session, operation_id, actor=actor)
    return operation_payload(operation)


@router.post("/api/maintenance/{operation_id}/retry")
async def retry_maintenance(
    operation_id: str,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(maintenance_actor),
):
    operation = await retry_operation(session, operation_id, actor=actor)
    return operation_payload(operation)


@router.post("/api/maintenance/{operation_id}/reconcile")
async def reconcile_maintenance(
    operation_id: str,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(maintenance_actor),
):
    """Read-only check for an INTERRUPTED operation before releasing its lock."""

    operation = await reconcile_operation(session, operation_id, actor=actor)
    return operation_payload(operation)


@router.get("/api/agent-workers/{environment_id}/health")
async def worker_health(environment_id: int, session: AsyncSession = Depends(get_session)):
    health = await environment_health(session, environment_id)
    if health is None:
        raise HTTPException(404, "Worker 环境不存在")
    return health

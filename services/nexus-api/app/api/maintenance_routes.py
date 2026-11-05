"""Agent Worker maintenance API: submit, observe, resume events, cancel, retry."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.domain import MaintenanceAction, MaintenanceState
from app.identity import request_uid
from app.models import WorkerMaintenanceOperation
from app.runtime.maintenance import (
    cancel_operation,
    environment_health,
    event_payload,
    event_stream,
    events_after,
    list_operations,
    operation_detail,
    operation_payload,
    retry_operation,
    submit_operation,
)

router = APIRouter()


class MaintenanceSubmit(BaseModel):
    action: Literal["check", "deploy", "update", "restart"]


@router.post("/api/workers/{agent_worker_id}/maintenance", status_code=202)
async def submit_maintenance(
    agent_worker_id: str,
    payload: MaintenanceSubmit,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(request_uid),
):
    operation = await submit_operation(
        session,
        agent_worker_id,
        payload.action,
        requested_by=actor,
    )
    return operation_payload(operation)


@router.get("/api/workers/{agent_worker_id}/maintenance")
async def maintenance_history(
    agent_worker_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    action: MaintenanceAction | None = None,
    state: MaintenanceState | None = None,
    session: AsyncSession = Depends(get_session),
):
    return await list_operations(
        session, agent_worker_id, limit=limit, offset=offset, action=action, state=state
    )


@router.get("/api/maintenance/{operation_id}")
async def maintenance_detail(operation_id: str, session: AsyncSession = Depends(get_session)):
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
    if await session.get(WorkerMaintenanceOperation, operation_id) is None:
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
    if await session.get(WorkerMaintenanceOperation, operation_id) is None:
        raise HTTPException(404, "维护操作不存在")
    await session.commit()
    cursor = after_id
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError:
            pass

    async def stream():
        from app.database import SessionLocal

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
    actor: str = Depends(request_uid),
):
    operation = await cancel_operation(session, operation_id, actor=actor)
    return operation_payload(operation)


@router.post("/api/maintenance/{operation_id}/retry")
async def retry_maintenance(
    operation_id: str,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(request_uid),
):
    operation = await retry_operation(session, operation_id, actor=actor)
    return operation_payload(operation)


@router.get("/api/workers/{agent_worker_id}/health")
async def worker_health(agent_worker_id: str, session: AsyncSession = Depends(get_session)):
    health = await environment_health(session, agent_worker_id)
    if health is None:
        raise HTTPException(404, "Worker 环境不存在")
    return health


@router.post("/api/maintenance/{operation_id}/confirm-stopped")
async def confirm_stopped(
    operation_id: str, session=Depends(get_session), actor: str = Depends(request_uid)
):
    from app.runtime.maintenance import confirm_maintenance_stopped

    return operation_payload(await confirm_maintenance_stopped(session, operation_id, actor))

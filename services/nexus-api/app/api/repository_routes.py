"""Agent Worker 复制。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Task
from app.runtime.repository import (
    WorkerCopyError,
    copy_worker,
)

router = APIRouter(prefix="/api")


class WorkerCopy(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    port: int | None = Field(default=None, ge=1, le=65535)


@router.post("/agent-workers/{employee_id}/copy", status_code=201)
async def copy_employee(
    employee_id: str,
    payload: WorkerCopy,
    session: AsyncSession = Depends(get_session),
):
    from app.api.routes import employee_read, require_employee

    source = await require_employee(employee_id, session)
    task_count = await session.scalar(
        select(func.count()).select_from(Task).where(Task.employee_id == source.id)
    )
    try:
        await copy_worker(session, source, new_id=payload.id, name=payload.name, port=payload.port)
    except WorkerCopyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    copied = await employee_read(await require_employee(payload.id, session), session)
    copied_tasks = await session.scalar(
        select(func.count()).select_from(Task).where(Task.employee_id == payload.id)
    )
    return {
        "employee": copied,
        "source_task_count": task_count or 0,
        "copied_task_count": copied_tasks or 0,
    }

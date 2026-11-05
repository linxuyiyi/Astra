"""Task、分类与审计 API。Worker 配置只有 /api/workers 一个入口。"""

import asyncio
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal, get_session
from app.models import (
    AgentWorker,
    Event,
    ExperienceEvidence,
    ExperienceReviewJob,
    KnowledgeFeedback,
    KnowledgeItem,
    KnowledgeUsage,
    Task,
    TaskCategory,
    TaskWakeOutbox,
)
from app.runtime.worker_connection import worker_connections
from app.schemas import (
    EventRead,
    TaskCategoryCreate,
    TaskCategoryPatch,
    TaskCategoryRead,
    TaskCreate,
    TaskRead,
    TaskUpdate,
)
from app.services import TaskService, dashboard_metrics

router = APIRouter(prefix="/api")


async def require_task(task_id, session):
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/system/health")
async def health():
    return {"status": "ok", "version": "1.5.0"}


@router.get("/metrics")
async def metrics(session=Depends(get_session)):
    return await dashboard_metrics(session)


@router.get("/task-categories", response_model=list[TaskCategoryRead])
async def list_task_categories(
    include_disabled: bool = False,
    session: AsyncSession = Depends(get_session),
):
    query = select(TaskCategory)
    if not include_disabled:
        query = query.where(TaskCategory.enabled.is_(True))
    return (
        (await session.execute(query.order_by(TaskCategory.sort_order, TaskCategory.created_at)))
        .scalars()
        .all()
    )


@router.post(
    "/task-categories",
    response_model=TaskCategoryRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_task_category(
    payload: TaskCategoryCreate,
    session: AsyncSession = Depends(get_session),
):
    category = TaskCategory(id=f"CAT-{uuid4().hex[:8].upper()}", **payload.model_dump())
    session.add(category)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="任务分类名称已存在") from exc
    await session.refresh(category)
    return category


@router.patch("/task-categories/{category_id}", response_model=TaskCategoryRead)
async def patch_task_category(
    category_id: str,
    payload: TaskCategoryPatch,
    session: AsyncSession = Depends(get_session),
):
    category = await session.get(TaskCategory, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="任务分类不存在")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(category, key, value)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="任务分类名称已存在") from exc
    await session.refresh(category)
    return category


async def validate_category_ids(category_ids: list[str], session: AsyncSession) -> None:
    if not category_ids:
        raise HTTPException(status_code=422, detail="至少选择一个任务分类")
    existing = set(
        (
            await session.execute(
                select(TaskCategory.id).where(
                    TaskCategory.id.in_(category_ids), TaskCategory.enabled.is_(True)
                )
            )
        ).scalars()
    )
    missing = set(category_ids) - existing
    if missing:
        raise HTTPException(status_code=422, detail=f"任务分类不存在或已停用：{', '.join(sorted(missing))}")


async def validate_task_worker(session, worker_id, task_type, category_id):
    from app.models import AgentWorker

    if not worker_id:
        return
    worker = await session.get(AgentWorker, worker_id)
    if (
        not worker
        or not worker.enabled
        or task_type not in worker.task_types
        or category_id not in worker.category_ids
    ):
        raise HTTPException(422, "指定 Worker 不存在、停用或不支持任务类型/分类")


@router.get("/tasks", response_model=list[TaskRead])
async def list_tasks(
    response: Response,
    status: str | None = None,
    agent_worker_id: str | None = None,
    origin_employee_id: str | None = None,
    source: str | None = None,
    q: str | None = None,
    task_type: str | None = None,
    category_id: str | None = None,
    external_task_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session=Depends(get_session),
):
    query = select(Task)
    for key, value in {
        "status": status,
        "agent_worker_id": agent_worker_id,
        "origin_employee_id": origin_employee_id,
        "source": source,
        "task_type": task_type,
        "category_id": category_id,
        "external_task_id": external_task_id,
    }.items():
        if value:
            query = query.where(getattr(Task, key) == value)
    if q:
        query = query.where(Task.title.ilike(f"%{q}%") | Task.id.ilike(f"%{q}%"))
    response.headers["X-Total-Count"] = str(
        await session.scalar(select(func.count()).select_from(query.subquery()))
    )
    rows = await session.execute(
        query.add_columns(AgentWorker.name)
        .outerjoin(AgentWorker, AgentWorker.id == Task.agent_worker_id)
        .order_by(Task.created_at.desc(), Task.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [task_read(task, worker_name) for task, worker_name in rows]


def task_read(task, worker_name):
    return TaskRead.model_validate(task).model_copy(update={"worker_name": worker_name})


@router.post("/tasks", response_model=TaskRead, status_code=201)
async def create_task(payload: TaskCreate, session=Depends(get_session)):
    category = await session.get(TaskCategory, payload.category_id)
    if not category or not category.enabled:
        raise HTTPException(422, "请选择已启用的任务分类")
    await validate_task_worker(session, payload.agent_worker_id, payload.task_type.value, payload.category_id)
    return await TaskService(session).create(payload)


@router.get("/tasks/{task_id}", response_model=TaskRead)
async def get_task(task_id: str, session=Depends(get_session)):
    task = await require_task(task_id, session)
    worker = await session.get(AgentWorker, task.agent_worker_id) if task.agent_worker_id else None
    if worker is None:
        return task_read(task, None)
    online = await worker_connections([worker])
    return task_read(task, worker.name).model_copy(update={
        "worker_online": online[worker.id],
        "worker_last_heartbeat_at": worker.last_heartbeat_at,
    })


@router.patch("/tasks/{task_id}", response_model=TaskRead)
async def patch_task(task_id: str, payload: TaskUpdate, session=Depends(get_session)):
    task = await require_task(task_id, session)
    await validate_task_worker(session, payload.agent_worker_id, task.task_type, task.category_id)
    try:
        return await TaskService(session).update(task, payload)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/tasks/{task_id}/confirm", response_model=TaskRead)
async def confirm_task_completion(task_id: str, session=Depends(get_session)):
    from app.api.workspace_routes import error_response
    from app.runtime.lifecycle import RuntimeService

    await require_task(task_id, session)
    try:
        return await RuntimeService(session).confirm_completion(task_id)
    except Exception as exc:
        raise error_response(exc) from exc


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, session=Depends(get_session)):
    from app.runtime.experience_review.coordinator import ACTIVE_STATUSES, lock_review_source_task

    await lock_review_source_task(session, task_id)
    task = await require_task(task_id, session)
    if task.status not in {"PENDING", "SUCCEEDED", "CANCELED"} or task.pending_message_id:
        raise HTTPException(409, "任务尚未释放，不能删除")
    active_review = await session.scalar(
        select(ExperienceReviewJob.id)
        .where(
            ExperienceReviewJob.task_id_snapshot == task_id,
            ExperienceReviewJob.status.in_(ACTIVE_STATUSES),
        )
        .limit(1)
    )
    if active_review:
        raise HTTPException(409, "任务仍有未完成的经验复盘，请在复盘结束后删除")
    # 数字图书馆引用属于历史审计信息，任务删除后置空引用而不是级联删除知识条目。
    await session.execute(
        update(KnowledgeItem)
        .where(KnowledgeItem.source_task_id == task_id)
        .values(source_task_id=None, source_session_id=None)
    )
    await session.execute(
        update(KnowledgeUsage).where(KnowledgeUsage.task_id == task_id).values(task_id=None, session_id=None)
    )
    await session.execute(
        update(KnowledgeFeedback).where(KnowledgeFeedback.task_id == task_id).values(task_id=None)
    )
    await session.execute(
        update(ExperienceEvidence)
        .where(ExperienceEvidence.task_id == task_id)
        .values(task_id=None, source_session_id=None)
    )
    await session.execute(delete(TaskWakeOutbox).where(TaskWakeOutbox.task_id == task_id))
    await session.delete(task)
    await session.commit()
    return {"deleted": task_id}


@router.get("/tasks/{task_id}/events", response_model=list[EventRead])
async def task_events(
    task_id: str,
    limit: int = Query(100, ge=1, le=200),
    after_id: int | None = Query(None, ge=0),
    before_id: int | None = Query(None, ge=1),
    session: AsyncSession = Depends(get_session),
):
    await require_task(task_id, session)
    query = select(Event).where(Event.task_id == task_id)
    if after_id is not None:
        query = query.where(Event.id > after_id).order_by(Event.id)
    else:
        if before_id is not None:
            query = query.where(Event.id < before_id)
        query = query.order_by(Event.id.desc())
    return (await session.scalars(query.limit(limit))).all()


@router.get("/events", response_model=list[EventRead])
async def list_events(limit: int = Query(50, ge=1, le=500), session: AsyncSession = Depends(get_session)):
    return (
        (await session.execute(select(Event).order_by(Event.created_at.desc()).limit(limit))).scalars().all()
    )


async def _stream_event_batch(task_id: str | None, last_id: int | None) -> tuple[int, list[Event]]:
    from anyio import CancelScope

    # Never retain a transaction or connection while waiting for the next poll.
    with CancelScope(shield=True):
        async with SessionLocal() as stream_session:
            condition = Event.task_id == task_id if task_id else True
            if last_id is None:
                latest = await stream_session.scalar(select(func.max(Event.id)).where(condition)) or 0
                return latest, []
            rows = (
                await stream_session.scalars(
                    select(Event)
                    .where(
                        condition,
                        Event.id > last_id,
                    )
                    .order_by(Event.id)
                )
            ).all()
            return (rows[-1].id if rows else last_id), list(rows)


@router.get("/events/stream")
async def event_stream() -> StreamingResponse:
    async def generate():
        yield "event: connected\ndata: {}\n\n"
        last_id, _ = await _stream_event_batch(None, None)
        keepalive_ticks = 0
        while True:
            await asyncio.sleep(1)
            last_id, rows = await _stream_event_batch(None, last_id)
            for event in rows:
                payload = {
                    "id": event.id,
                    "type": event.event_type,
                    "task_id": event.task_id,
                    "employee_id": event.employee_id,
                    "phase": event.phase,
                    "message": event.message,
                    "created_at": event.created_at.isoformat(),
                }
                yield f"event: update\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            keepalive_ticks = 0 if rows else keepalive_ticks + 1
            if keepalive_ticks >= 15:
                keepalive_ticks = 0
                yield ": keepalive\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

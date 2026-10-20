import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.library_routes import library_principal
from app.assistant.review import retry_review, review_detail
from app.config.settings import get_settings
from app.dashboard import (
    dashboard_activity,
    dashboard_attention,
    dashboard_employees,
    dashboard_overview,
    dashboard_tasks,
)
from app.database import SessionLocal, get_session
from app.domain import EmployeeStatus, EnvironmentStatus, RuntimeSessionStatus, TaskStatus
from app.event_bus import event_bus
from app.library_service import KnowledgeService, LibraryPrincipal
from app.models import (
    AgentWorker,
    CodeRepository,
    Event,
    ExperienceEvidence,
    KnowledgeFeedback,
    KnowledgeItem,
    KnowledgeUsage,
    OpenCodeExecutionEnvironment,
    RuntimeSession,
    Task,
    TaskCategory,
    TaskReview,
    TaskSummary,
    TaskWakeOutbox,
    WorkerInstance,
    WorkerMaintenanceEvent,
    WorkerMaintenanceOperation,
)
from app.runtime.capability import binding_entries, stamp_desired
from app.runtime.presentation import project_channel_health
from app.runtime.repository import related_tasks
from app.schemas import (
    EmployeeCreate,
    EmployeePatch,
    EmployeeRead,
    EventRead,
    ExecutionEnvironmentRead,
    ExecutionEnvironmentWrite,
    RelatedTaskCreate,
    TaskCategoryCreate,
    TaskCategoryPatch,
    TaskCategoryRead,
    TaskCreate,
    TaskRead,
    TaskUpdate,
)
from app.services import (
    EmployeeService,
    TaskService,
    dashboard_metrics,
)
from app.usage import (
    aggregate_usage_by_day,
    apply_day_tokens,
    count_completed_tasks,
    day_bounds,
    employee_usage,
    load_sessions,
    session_local_date,
    summarize_sessions,
    task_usage,
)

router = APIRouter(prefix="/api")


async def require_task(task_id: str, session: AsyncSession) -> Task:
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


async def require_employee(employee_id: str, session: AsyncSession) -> AgentWorker:
    employee = await session.scalar(
        select(AgentWorker)
        .where(AgentWorker.id == employee_id)
        .options(
            selectinload(AgentWorker.execution_environment).selectinload(
                OpenCodeExecutionEnvironment.repository
            )
        )
    )
    if not employee:
        raise HTTPException(status_code=404, detail="Agent Worker 不存在")
    return employee


async def employee_read(employee: AgentWorker, session: AsyncSession) -> EmployeeRead:
    start, end = day_bounds()
    sessions = await load_sessions(session, employee_id=employee.id, start=start, end=end, overlap=True)
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    day_sessions = [item for item in sessions if session_local_date(item) == today]
    usage = await summarize_sessions(session, day_sessions, window=(start, end))
    apply_day_tokens(usage, aggregate_usage_by_day(sessions).get(today.isoformat()))
    completed = await count_completed_tasks(session, employee_id=employee.id, start=start, end=end)
    base = EmployeeRead.model_validate(employee)
    environment = employee.execution_environment
    worker_status = None
    repository_id = environment.repository_id if environment is not None else None
    if environment is not None:
        instance = await session.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == environment.id)
        )
        if instance is not None:
            worker_status = instance.status
    rows = (
        await session.execute(
            select(Task.status, Task.runtime_connection).where(
                Task.employee_id == employee.id,
                Task.status.in_(["RUNNING", "WAITING"]),
            )
        )
    ).all()
    health = project_channel_health(
        mode=environment.mode if environment is not None else None,
        worker_channel=environment.worker_channel if environment is not None else None,
        environment_status=environment.status if environment is not None else None,
        worker_status=worker_status,
        repository_id=str(repository_id) if repository_id else None,
        employee_status=employee.status,
        task_statuses=[row.status for row in rows],
        runtime_connections=[row.runtime_connection for row in rows],
    )
    return EmployeeRead.model_validate(
        {
            **base.model_dump(),
            "today_usage": usage,
            "today_completed": completed,
            "channel_health": health,
        }
    )


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(select(1))
    return {"status": "ok", "time": datetime.now(UTC).isoformat()}


@router.get("/system/metrics")
async def metrics(session: AsyncSession = Depends(get_session)) -> dict:
    return await dashboard_metrics(session)


@router.get("/dashboard/overview")
async def home_dashboard_overview(session: AsyncSession = Depends(get_session)) -> dict:
    return await dashboard_overview(session)


@router.get("/dashboard/agent-workers")
async def home_dashboard_employees(session: AsyncSession = Depends(get_session)) -> dict:
    return await dashboard_employees(session)


@router.get("/dashboard/attention")
async def home_dashboard_attention(session: AsyncSession = Depends(get_session)) -> dict:
    return await dashboard_attention(session)


@router.get("/dashboard/tasks")
async def home_dashboard_tasks(session: AsyncSession = Depends(get_session)) -> dict:
    return await dashboard_tasks(session)


@router.get("/dashboard/activity")
async def home_dashboard_activity(session: AsyncSession = Depends(get_session)) -> dict:
    return await dashboard_activity(session)


@router.get("/dashboard/knowledge")
async def home_dashboard_knowledge(
    session: AsyncSession = Depends(get_session),
    principal: LibraryPrincipal = Depends(library_principal),
) -> dict:
    return await KnowledgeService(session).dashboard_summary(principal)


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


@router.get("/agent-workers", response_model=list[EmployeeRead])
async def list_employees(session: AsyncSession = Depends(get_session)):
    employees = (
        (
            await session.execute(
                select(AgentWorker)
                .options(
                    selectinload(AgentWorker.execution_environment).selectinload(
                        OpenCodeExecutionEnvironment.repository
                    )
                )
                .order_by(AgentWorker.id)
            )
        )
        .scalars()
        .all()
    )
    return [await employee_read(employee, session) for employee in employees]


@router.post("/agent-workers", response_model=EmployeeRead, status_code=status.HTTP_201_CREATED)
async def create_employee(payload: EmployeeCreate, session: AsyncSession = Depends(get_session)):
    await validate_category_ids(payload.category_ids, session)
    environment_payload = payload.execution_environment.model_dump()
    repository_id = environment_payload.get("repository_id")
    if repository_id and await session.get(CodeRepository, repository_id) is None:
        raise HTTPException(status_code=422, detail="代码仓库不存在")
    skill_refs = environment_payload.pop("skill_refs", None)
    mcp_refs = environment_payload.pop("mcp_refs", None)
    employee_payload = payload.model_dump(exclude={"execution_environment"})
    employee_payload["task_types"] = [item.value for item in payload.task_types]
    employee = AgentWorker(
        **employee_payload,
        status=EmployeeStatus.IDLE,
        online_since=datetime.now(UTC),
        last_heartbeat=datetime.now(UTC),
    )
    session.add(employee)
    try:
        await session.flush()
        environment = OpenCodeExecutionEnvironment(
            employee_id=employee.id,
            **environment_payload,
            status=EnvironmentStatus.UNKNOWN,
        )
        if skill_refs is not None:
            environment.skill_refs = skill_refs
        if mcp_refs is not None:
            environment.mcp_refs = mcp_refs
        entries = binding_entries(environment.skill_refs, environment.mcp_refs)
        if entries:
            environment.status_detail = stamp_desired({}, entries, checked_at=datetime.now(UTC).isoformat())
        session.add(environment)
        session.add(
            Event(
                employee_id=employee.id,
                event_type="EMPLOYEE_CREATED",
                phase="business",
                message=f"新增 Agent Worker：{employee.name}",
                detail={"role": employee.role},
            )
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="员工 ID 已存在") from exc
    return await employee_read(await require_employee(employee.id, session), session)


@router.get("/agent-workers/{employee_id}", response_model=EmployeeRead)
async def get_employee(employee_id: str, session: AsyncSession = Depends(get_session)):
    return await employee_read(await require_employee(employee_id, session), session)


@router.patch("/agent-workers/{employee_id}", response_model=EmployeeRead)
async def patch_employee(
    employee_id: str, payload: EmployeePatch, session: AsyncSession = Depends(get_session)
):
    employee = await require_employee(employee_id, session)
    updates = payload.model_dump(exclude_unset=True)
    if payload.category_ids is not None:
        await validate_category_ids(payload.category_ids, session)
    if payload.task_types is not None:
        updates["task_types"] = [item.value for item in payload.task_types]
    for key, value in updates.items():
        setattr(employee, key, value)
    await session.commit()
    return await employee_read(await require_employee(employee.id, session), session)


@router.put("/agent-workers/{employee_id}/environment", response_model=ExecutionEnvironmentRead)
async def update_employee_environment(
    employee_id: str,
    payload: ExecutionEnvironmentWrite,
    session: AsyncSession = Depends(get_session),
):
    employee = await require_employee(employee_id, session)
    environment = employee.execution_environment
    from app.runtime.remote_provisioning import guard_environment_write

    await guard_environment_write(session, environment)
    if environment is None:
        environment = OpenCodeExecutionEnvironment(employee_id=employee.id)
        session.add(environment)
    updates = payload.model_dump()
    provided = payload.model_fields_set
    skill_refs = updates.pop("skill_refs", None)
    mcp_refs = updates.pop("mcp_refs", None)
    if not updates.get("ssh_password"):
        updates.pop("ssh_password", None)
    if "repository_id" not in provided:
        updates.pop("repository_id", None)
    elif updates.get("repository_id") and await session.get(
        CodeRepository, updates["repository_id"]
    ) is None:
        raise HTTPException(status_code=422, detail="代码仓库不存在")
    if "branch" not in provided:
        updates.pop("branch", None)
    previous = environment.status_detail if isinstance(environment.status_detail, dict) else {}
    bindings_changed = skill_refs is not None or mcp_refs is not None
    for key, value in updates.items():
        setattr(environment, key, value)
    if skill_refs is not None:
        environment.skill_refs = skill_refs
    if mcp_refs is not None:
        environment.mcp_refs = mcp_refs
    environment.status = EnvironmentStatus.UNKNOWN
    detail = {
        "message": "配置已更新，等待测试连接",
        "applies_to": "future_tasks" if employee.current_task_count else "all_new_runs",
    }
    entries = binding_entries(environment.skill_refs, environment.mcp_refs)
    if bindings_changed or entries or (isinstance(previous.get("capability"), dict)):
        detail = stamp_desired(detail, entries, previous=previous, checked_at=datetime.now(UTC).isoformat())
    environment.status_detail = detail
    await session.commit()
    await session.refresh(environment)
    if environment.repository_id:
        environment.repository = await session.get(CodeRepository, environment.repository_id)
    session.add(
        Event(
            employee_id=employee.id,
            event_type="ENVIRONMENT_UPDATED",
            phase="environment",
            message="Agent 工作环境已更新",
            detail={
                "mode": environment.mode,
                "name": environment.name,
                "applies_to_future_tasks": bool(employee.current_task_count),
            },
        )
    )
    await session.commit()
    return environment


@router.get("/agent-workers/{employee_id}/usage")
async def get_employee_usage(
    employee_id: str,
    days: int = Query(default=7, ge=1, le=30),
    session: AsyncSession = Depends(get_session),
):
    await require_employee(employee_id, session)
    return await employee_usage(session, employee_id, days)


@router.post("/agent-workers/{employee_id}/pause", response_model=EmployeeRead)
async def pause_employee(employee_id: str, session: AsyncSession = Depends(get_session)):
    employee = await require_employee(employee_id, session)
    if employee.current_task_count:
        raise HTTPException(status_code=409, detail="员工有当前任务，请先在任务详情中暂停或处理该任务")
    await EmployeeService(session).set_status(employee, EmployeeStatus.PAUSED)
    return employee


@router.post("/agent-workers/{employee_id}/resume", response_model=EmployeeRead)
async def resume_employee(employee_id: str, session: AsyncSession = Depends(get_session)):
    employee = await require_employee(employee_id, session)
    target = EmployeeStatus.WORKING if employee.current_task_count else EmployeeStatus.IDLE
    await EmployeeService(session).set_status(employee, target)
    return employee


@router.delete("/agent-workers/{employee_id}")
async def delete_employee(employee_id: str, session: AsyncSession = Depends(get_session)):
    employee = await require_employee(employee_id, session)
    from app.runtime.remote_provisioning import guard_environment_write

    await guard_environment_write(session, employee.execution_environment)
    if employee.current_task_count:
        raise HTTPException(status_code=409, detail="员工正在执行任务，不能删除")
    task_count = await session.scalar(
        select(func.count()).select_from(Task).where(Task.employee_id == employee_id)
    )
    if task_count:
        raise HTTPException(status_code=409, detail="该员工已有任务记录，不能删除；可先停用员工")
    # 数字图书馆引用属于历史审计信息，删除员工后置空引用而不是级联删除知识条目。
    await session.execute(
        update(KnowledgeItem)
        .where(KnowledgeItem.source_employee_id == employee_id)
        .values(source_employee_id=None)
    )
    await session.execute(
        update(KnowledgeUsage).where(KnowledgeUsage.employee_id == employee_id).values(employee_id=None)
    )
    await session.execute(
        update(KnowledgeFeedback).where(KnowledgeFeedback.employee_id == employee_id).values(employee_id=None)
    )
    await session.execute(delete(Event).where(Event.employee_id == employee_id))
    environment = employee.execution_environment
    if environment is not None:
        operation_ids = (
            await session.scalars(
                select(WorkerMaintenanceOperation.id).where(
                    WorkerMaintenanceOperation.environment_id == environment.id
                )
            )
        ).all()
        if operation_ids:
            await session.execute(
                delete(WorkerMaintenanceEvent).where(
                    WorkerMaintenanceEvent.operation_id.in_(operation_ids)
                )
            )
            await session.execute(
                delete(WorkerMaintenanceOperation).where(
                    WorkerMaintenanceOperation.environment_id == environment.id
                )
            )
    session.add(
        Event(
            event_type="EMPLOYEE_DELETED",
            phase="business",
            message=f"删除 Agent Worker：{employee.name}",
            detail={"role": employee.role},
        )
    )
    await session.delete(employee)
    await session.commit()
    return {"deleted": employee_id}


@router.get("/tasks", response_model=list[TaskRead])
async def list_tasks(
    q: str | None = None,
    source: str | None = None,
    task_type: str | None = None,
    category_id: str | None = None,
    task_status: str | None = Query(default=None, alias="status"),
    priority: str | None = None,
    employee_id: str | None = None,
    worker_id: str | None = None,
    responsible_employee_id: str | None = None,
    source_session_id: str | None = None,
    source_conversation_id: str | None = None,
    source_channel_id: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    query = select(Task)
    if q:
        query = query.where(or_(Task.id.ilike(f"%{q}%"), Task.title.ilike(f"%{q}%")))
    if source:
        query = query.where(Task.source == source)
    if task_type:
        query = query.where(Task.task_type == task_type)
    if category_id:
        query = query.where(Task.category_id == category_id)
    if task_status:
        query = query.where(Task.status == task_status)
    if priority:
        query = query.where(Task.priority == priority)
    if worker_id:
        query = query.where(Task.employee_id == worker_id)
    elif employee_id:
        query = query.where(Task.employee_id == employee_id)
    if responsible_employee_id:
        query = query.where(Task.responsible_employee_id == responsible_employee_id)
    if source_session_id:
        query = query.where(Task.source_session_id == source_session_id)
    if source_conversation_id:
        query = query.where(Task.source_conversation_id == source_conversation_id)
    if source_channel_id:
        query = query.where(Task.source_channel_id == source_channel_id)
    if created_from:
        query = query.where(Task.created_at >= created_from)
    if created_to:
        query = query.where(Task.created_at <= created_to)
    tasks = (await session.execute(query.order_by(desc(Task.created_at)).limit(limit))).scalars().all()
    await _attach_session_facts(session, tasks)
    return tasks


@router.post("/tasks/related", status_code=status.HTTP_201_CREATED)
async def create_related_tasks(payload: RelatedTaskCreate, session: AsyncSession = Depends(get_session)):
    category = await session.get(TaskCategory, payload.category_id)
    if not category or not category.enabled:
        raise HTTPException(status_code=422, detail="请选择已启用的任务分类")
    created: list[Task] = []
    created_ids: list[str] = []
    for index, part in enumerate(payload.parts):
        if await session.get(CodeRepository, part.repository_id) is None:
            raise HTTPException(status_code=422, detail="仓库不存在")
        if part.depends_on is not None and part.depends_on >= index:
            raise HTTPException(status_code=422, detail="依赖必须指向更早的任务")
        task = await TaskService(session).create(
            TaskCreate(
                task_type=payload.task_type,
                category_id=payload.category_id,
                title=part.title or payload.title,
                description=payload.description,
                acceptance_criteria=payload.acceptance_criteria,
                repository_id=part.repository_id,
            ),
            commit=False,
        )
        if part.depends_on is not None:
            task.related_task_id = created_ids[part.depends_on]
        created.append(task)
        created_ids.append(task.id)
    await session.commit()
    for task in created:
        await session.refresh(task)
        await event_bus.publish({"type": "TASK_CREATED", "task_id": task.id})
    return [
        {
            "id": task.id,
            "title": task.title,
            "repository_id": task.repository_id,
            "related_task_id": task.related_task_id,
            "status": task.status,
        }
        for task in created
    ]


@router.get("/tasks/{task_id}/related")
async def get_related_tasks(task_id: str, session: AsyncSession = Depends(get_session)):
    await require_task(task_id, session)
    rows = await related_tasks(session, task_id)
    return [
        {
            "id": task.id,
            "title": task.title,
            "repository_id": task.repository_id,
            "related_task_id": task.related_task_id,
            "status": task.status,
            "base_commit": task.base_commit,
        }
        for task in rows
    ]


@router.post("/tasks", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskCreate, session: AsyncSession = Depends(get_session)):
    category = await session.get(TaskCategory, payload.category_id)
    if not category or not category.enabled:
        raise HTTPException(status_code=422, detail="请选择已启用的任务分类")
    if payload.repository_id and await session.get(CodeRepository, payload.repository_id) is None:
        raise HTTPException(status_code=422, detail="仓库不存在")
    if payload.employee_id:
        employee = await require_employee(payload.employee_id, session)
        if not employee.enabled:
            raise HTTPException(status_code=409, detail="指定员工未启用")
        if payload.task_type.value not in employee.task_types:
            raise HTTPException(status_code=409, detail="指定员工不支持该任务类型")
        if payload.category_id not in employee.category_ids:
            raise HTTPException(status_code=409, detail="指定员工不支持该任务分类")
    try:
        return await TaskService(session).create(payload)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="来源任务已导入") from exc


@router.get("/tasks/{task_id}", response_model=TaskRead)
async def get_task(task_id: str, session: AsyncSession = Depends(get_session)):
    task = await require_task(task_id, session)
    await _attach_session_facts(session, [task])
    return task


async def _attach_session_facts(session: AsyncSession, tasks: list[Task]) -> None:
    ids = [task.current_session_id for task in tasks if task.current_session_id]
    bindings = {}
    if ids:
        rows = (await session.scalars(select(RuntimeSession).where(RuntimeSession.id.in_(ids)))).all()
        bindings = {row.id: row for row in rows}
    for task in tasks:
        binding = bindings.get(task.current_session_id or "")
        task.session_status = binding.status if binding else None
        task.session_terminal_reason = binding.terminal_reason if binding else None


@router.patch("/tasks/{task_id}", response_model=TaskRead)
async def patch_task(task_id: str, payload: TaskUpdate, session: AsyncSession = Depends(get_session)):
    task = await require_task(task_id, session)
    if "employee_id" in payload.model_fields_set and payload.employee_id:
        employee = await require_employee(payload.employee_id, session)
        if not employee.enabled:
            raise HTTPException(status_code=409, detail="指定员工未启用")
        if task.task_type not in employee.task_types:
            raise HTTPException(status_code=409, detail="指定员工不支持该任务类型")
        if (task.category_id or "") not in employee.category_ids:
            raise HTTPException(status_code=409, detail="指定员工不支持该任务分类")
    try:
        return await TaskService(session).update(task, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/review")
async def get_task_review(task_id: str, session: AsyncSession = Depends(get_session)):
    return await review_detail(session, await require_task(task_id, session))


@router.post("/tasks/{task_id}/review/retry")
async def retry_task_review(task_id: str, session: AsyncSession = Depends(get_session)):
    await require_task(task_id, session)
    try:
        return await retry_review(session, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


DELETABLE_TASK_STATUSES = frozenset(
    {TaskStatus.PENDING, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED}
)


@router.post("/tasks/{task_id}/confirm", response_model=TaskRead)
async def confirm_task_completion(task_id: str, session: AsyncSession = Depends(get_session)):
    from app.runtime.lifecycle import RuntimeService

    await require_task(task_id, session)
    try:
        return await RuntimeService(session).confirm_completion(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, session: AsyncSession = Depends(get_session)):
    task = await session.scalar(
        select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True)
    )
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if TaskStatus(task.status) not in DELETABLE_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="任务正在执行或等待人工，不能删除")
    if task.pending_message_id:
        raise HTTPException(status_code=409, detail="任务仍有待确认提交，必须先停止并收敛 Session")
    if task.current_session_id:
        runtime_session = await session.get(RuntimeSession, task.current_session_id)
        if runtime_session and runtime_session.status in {
            RuntimeSessionStatus.RUNNING.value,
            RuntimeSessionStatus.WAITING.value,
            RuntimeSessionStatus.UNKNOWN.value,
        }:
            raise HTTPException(status_code=409, detail="当前 Session 尚未终止，不能删除任务")
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
    await session.execute(delete(TaskReview).where(TaskReview.task_id == task_id))
    await session.execute(delete(TaskSummary).where(TaskSummary.task_id == task_id))
    await session.execute(delete(RuntimeSession).where(RuntimeSession.task_id == task_id))
    await session.delete(task)
    await session.commit()
    return {"deleted": task_id}


@router.get("/tasks/{task_id}/usage")
async def get_task_usage(task_id: str, session: AsyncSession = Depends(get_session)):
    await require_task(task_id, session)
    return await task_usage(session, task_id)


@router.get("/tasks/{task_id}/events", response_model=list[EventRead])
async def task_events(task_id: str, session: AsyncSession = Depends(get_session)):
    await require_task(task_id, session)
    return (
        (await session.execute(select(Event).where(Event.task_id == task_id).order_by(Event.created_at)))
        .scalars()
        .all()
    )


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

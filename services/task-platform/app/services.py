from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.domain import PRIORITY_WEIGHT, EmployeeStatus, TaskStatus
from app.event_bus import event_bus
from app.models import AgentWorker, Event, Task
from app.runtime.wake_outbox import record_wake
from app.schemas import TaskCreate, TaskUpdate
from app.state_machine import TaskStateMachine
from app.usage import (
    count_completed_tasks,
    load_sessions,
    summarize_sessions,
    today_token_bucket,
)

STATUS_EVENT = {status: f"TASK_{status.value}" for status in TaskStatus}


def new_task_id() -> str:
    now = datetime.now(UTC)
    return f"T-{now:%Y%m%d}-{uuid4().hex[:6].upper()}"


class TaskService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, payload: TaskCreate, *, commit: bool = True) -> Task:
        if payload.idempotency_key:
            existing = await self.session.scalar(
                select(Task).where(Task.idempotency_key == payload.idempotency_key)
            )
            if existing:
                return existing
        criteria = [
            {
                "id": criterion.id or f"AC-{index:02d}",
                "content": criterion.content,
            }
            for index, criterion in enumerate(payload.acceptance_criteria, start=1)
        ]
        task = Task(
            id=new_task_id(),
            source=payload.source,
            external_task_id=payload.external_task_id,
            task_type=payload.task_type.value,
            category_id=payload.category_id,
            title=payload.title,
            description=payload.description,
            acceptance_criteria=criteria,
            attachments=payload.attachments,
            priority=payload.priority.value,
            priority_weight=PRIORITY_WEIGHT[payload.priority],
            completion_policy=payload.completion_policy.value,
            employee_id=payload.employee_id,
            responsible_employee_id=payload.responsible_employee_id,
            source_session_id=payload.source_session_id,
            source_conversation_id=payload.source_conversation_id,
            source_channel_id=payload.source_channel_id,
            idempotency_key=payload.idempotency_key,
            repository_id=payload.repository_id,
            status=TaskStatus.PENDING,
            current_progress="等待领取",
            last_activity_at=datetime.now(UTC),
        )
        self.session.add(task)
        await self._record(
            task,
            "TASK_CREATED",
            "work",
            "任务已创建",
            {
                "source": task.source,
                "external_task_id": task.external_task_id,
                "employee_id": task.employee_id,
            },
        )
        await self._record(task, "TASK_PENDING", "work", "任务已进入待领取队列")
        try:
            if commit:
                await self.session.commit()
            else:
                await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            if payload.idempotency_key:
                existing = await self.session.scalar(
                    select(Task).where(Task.idempotency_key == payload.idempotency_key)
                )
                if existing:
                    return existing
            raise
        if not commit:
            return task
        await self.session.refresh(task)
        await event_bus.publish({"type": "TASK_CREATED", "task_id": task.id})
        return task

    async def update(self, task: Task, payload: TaskUpdate) -> Task:
        if TaskStatus(task.status) in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED}:
            raise ValueError("已结束的任务不能修改")
        fields = payload.model_fields_set
        if "title" in fields and payload.title is not None:
            task.title = payload.title
        if "description" in fields and payload.description is not None:
            task.description = payload.description
        if "priority" in fields and payload.priority is not None:
            task.priority = payload.priority.value
            task.priority_weight = PRIORITY_WEIGHT[payload.priority]
        if "acceptance_criteria" in fields and payload.acceptance_criteria is not None:
            task.acceptance_criteria = [
                {"id": criterion.id or f"AC-{index:02d}", "content": criterion.content}
                for index, criterion in enumerate(payload.acceptance_criteria, start=1)
            ]
        if "employee_id" in fields:
            if TaskStatus(task.status) != TaskStatus.PENDING:
                raise ValueError("只有待领取任务可以调整 Worker")
            task.employee_id = payload.employee_id or None
        if "responsible_employee_id" in fields:
            task.responsible_employee_id = payload.responsible_employee_id or None
        await self.add_event(task, "TASK_UPDATED", "work", "任务信息已更新", {"fields": sorted(fields)})
        await self.session.refresh(task)
        return task

    async def transition(
        self,
        task: Task,
        target: TaskStatus,
        *,
        current_progress: str | None = None,
        message: str | None = None,
        detail: dict | None = None,
        commit: bool = True,
    ) -> Task:
        previous = TaskStatus(task.status)
        TaskStateMachine.validate(previous, target)
        return await self._apply_transition(
            task,
            previous,
            target,
            current_progress=current_progress,
            message=message,
            detail=detail,
            commit=commit,
        )

    async def _apply_transition(
        self,
        task: Task,
        previous: TaskStatus,
        target: TaskStatus,
        *,
        current_progress: str | None,
        message: str | None,
        detail: dict | None,
        commit: bool = True,
    ) -> Task:
        now = datetime.now(UTC)
        task.status = target.value
        if current_progress is not None:
            task.current_progress = current_progress
        elif target is TaskStatus.RUNNING:
            task.current_progress = "执行中"
        if target is TaskStatus.RUNNING and task.started_at is None:
            task.claimed_at = task.claimed_at or now
            task.started_at = now
        if target is TaskStatus.RUNNING:
            task.inactivity_alert = False
            task.inactivity_alert_since = None
            task.finished_at = None
        if target in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED}:
            task.finished_at = now
        event_type = STATUS_EVENT[target]
        event = await self._record(
            task,
            event_type,
            "work",
            message or f"任务状态从 {previous.value} 变更为 {target.value}",
            {"from": previous.value, "to": target.value, **(detail or {})},
        )
        await record_wake(self.session, task, event, previous, target)
        if commit:
            await self.session.commit()
            await self.session.refresh(task)
            await event_bus.publish({"type": event_type, "task_id": task.id, "status": target.value})
        return task

    async def add_event(
        self,
        task: Task,
        event_type: str,
        phase: str,
        message: str,
        detail: dict | None = None,
        *,
        commit: bool = True,
    ) -> Event:
        event = await self._record(task, event_type, phase, message, detail)
        if commit:
            await self.session.commit()
            await self.session.refresh(event)
            await event_bus.publish({"type": event_type, "task_id": task.id, "event_id": event.id})
        return event

    async def _record(
        self,
        task: Task,
        event_type: str,
        phase: str,
        message: str,
        detail: dict | None = None,
    ) -> Event:
        event = Event(
            task_id=task.id,
            employee_id=task.employee_id,
            event_type=event_type,
            phase=phase,
            message=message,
            detail=detail or {},
        )
        task.last_activity_at = event.created_at or datetime.now(UTC)
        task.inactivity_alert = False
        task.inactivity_alert_since = None
        self.session.add(event)
        await self.session.flush()
        return event


class EmployeeService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def set_status(self, employee: AgentWorker, status: EmployeeStatus) -> None:
        employee.status = status.value
        if status is not EmployeeStatus.OFFLINE:
            now = datetime.now(UTC)
            employee.last_heartbeat = now
            employee.online_since = employee.online_since or now
        await self.session.commit()
        await event_bus.publish(
            {"type": "EMPLOYEE_STATUS_CHANGED", "employee_id": employee.id, "status": status.value}
        )

    async def heartbeat(self, employee: AgentWorker) -> None:
        now = datetime.now(UTC)
        employee.last_heartbeat = now
        employee.online_since = employee.online_since or now
        if employee.status == EmployeeStatus.OFFLINE:
            employee.status = EmployeeStatus.IDLE
        await self.session.commit()


async def dashboard_metrics(session: AsyncSession) -> dict:
    task_counts = dict(
        (await session.execute(select(Task.status, func.count(Task.id)).group_by(Task.status))).all()
    )
    employee_counts = dict(
        (
            await session.execute(
                select(AgentWorker.status, func.count(AgentWorker.id)).group_by(
                    AgentWorker.status
                )
            )
        ).all()
    )
    timezone = ZoneInfo(get_settings().timezone)
    today = datetime.now(timezone).date()
    day_start = datetime.combine(today, time.min, tzinfo=timezone).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    # Business completion is the immutable Task.completed_at, never a successful
    # RuntimeSession count. Session terminal counts stay separate.
    completed_today = await count_completed_tasks(session, start=day_start, end=day_end)
    usage_sessions = await load_sessions(session, start=day_start, end=day_end)
    usage = await summarize_sessions(session, usage_sessions)
    today_bucket = await today_token_bucket(session)
    today_tokens = today_bucket.get("total_tokens") if today_bucket else None
    today_tool_calls = today_bucket.get("tool_call_count") if today_bucket else None
    return {
        "online_employees": sum(
            count
            for status, count in employee_counts.items()
            if status not in {EmployeeStatus.OFFLINE, EmployeeStatus.ERROR}
        ),
        "running_tasks": sum(task_counts.get(status, 0) for status in [TaskStatus.RUNNING]),
        "idle_employees": employee_counts.get(EmployeeStatus.IDLE, 0),
        "error_employees": employee_counts.get(EmployeeStatus.ERROR, 0),
        "pending_tasks": task_counts.get(TaskStatus.PENDING, 0),
        "waiting_tasks": task_counts.get(TaskStatus.WAITING, 0),
        "failed_tasks": task_counts.get(TaskStatus.FAILED, 0),
        "completed_today": completed_today,
        "completed_sessions_today": usage["completed_sessions"],
        "failed_today": usage["failed_sessions"],
        "failed_sessions_today": usage["failed_sessions"],
        "interrupted_sessions_today": usage["interrupted_sessions"],
        "today_tokens": today_tokens,
        "today_tool_calls": today_tool_calls,
        "task_status_distribution": task_counts,
        "employee_status_distribution": employee_counts,
    }

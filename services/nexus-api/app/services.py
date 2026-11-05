from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from app.domain import TaskStatus
from app.event_bus import event_bus
from app.models import AgentWorker, Event, Task
from app.runtime.presentation import TASK_STATUS
from app.runtime.wake_outbox import record_wake
from app.state_machine import TaskStateMachine


def new_task_id():
    return f"T-{datetime.now(UTC):%Y%m%d}-{uuid4().hex[:8].upper()}"


class TaskService:
    def __init__(self, session):
        self.session = session

    async def create(self, payload, *, commit=True):
        data = payload.model_dump(mode="json")
        data["acceptance_criteria"] = [
            {"id": item["id"] or f"AC-{i:02d}", "content": item["content"]}
            for i, item in enumerate(data["acceptance_criteria"], 1)
        ]
        task = Task(id=new_task_id(), **data, status="PENDING", current_progress="等待领取")
        self.session.add(task)
        await self.add_event(task, "TASK_CREATED", "work", "任务已创建", commit=False)
        if task.source == "group_chat" and task.origin_employee_id:
            from nexus_api.db_models import discussions

            discussion = (
                await self.session.execute(
                    select(discussions.c.id, discussions.c.initiator_id).where(
                        discussions.c.channel_id == task.external_task_id,
                        discussions.c.active_employee_id == task.origin_employee_id,
                        discussions.c.state == "running",
                    )
                )
            ).first()
            if discussion:
                await self.add_event(
                    task,
                    "group_task_requested",
                    "work",
                    "群聊执行任务来源",
                    {"initiator_id": discussion.initiator_id, "discussion_id": discussion.id},
                    commit=False,
                )
        if commit:
            await self.session.commit()
            await self.session.refresh(task)
        return task

    async def update(self, task, payload):
        if task.status in {"SUCCEEDED", "CANCELED"}:
            raise ValueError("已结束的任务不能修改")
        data = payload.model_dump(exclude_unset=True, mode="json")
        if "agent_worker_id" in data and (task.status != "PENDING" or task.claimed_at):
            raise ValueError("只有待领取任务可以调整 Worker")
        for key, value in data.items():
            setattr(task, key, value)
        await self.add_event(task, "TASK_UPDATED", "work", "任务信息已更新", {"fields": list(data)})
        return task

    async def transition(
        self, task, target, *, current_progress=None, message=None, detail=None, commit=True
    ):
        previous, target = TaskStatus(task.status), TaskStatus(target)
        TaskStateMachine.validate(previous, target)
        task.status = target.value
        now = datetime.now(UTC)
        if target == TaskStatus.RUNNING:
            task.claimed_at = task.claimed_at or now
            task.started_at = task.started_at or now
            task.finished_at = None
        elif target in {TaskStatus.SUCCEEDED, TaskStatus.CANCELED}:
            task.finished_at = now
        if current_progress is not None:
            task.current_progress = current_progress
        elif previous != target:
            task.current_progress = (
                "执行中" if target == TaskStatus.RUNNING else TASK_STATUS[target.value]["label"]
            )
        if previous != target:
            event = await self.add_event(
                task,
                f"TASK_{target.value}",
                "work",
                message
                or (
                    f"任务状态从{TASK_STATUS[previous.value]['label']}"
                    f"变更为{TASK_STATUS[target.value]['label']}"
                ),
                {"from": previous.value, "to": target.value, **(detail or {})},
                commit=False,
            )
            await record_wake(self.session, task, event, previous, target)
        if commit:
            await self.session.commit()
            await event_bus.publish(
                {"type": f"TASK_{target.value}", "task_id": task.id, "status": target.value}
            )
        return task

    async def add_event(self, task, event_type, phase, message, detail=None, *, commit=True):
        event = Event(
            task_id=task.id,
            employee_id=task.agent_worker_id,
            event_type=event_type,
            phase=phase,
            message=message,
            detail=detail or {},
        )
        task.last_activity_at = datetime.now(UTC)
        self.session.add(event)
        await self.session.flush()
        if commit:
            await self.session.commit()
            await event_bus.publish({"type": event_type, "task_id": task.id, "event_id": event.id})
        return event


async def dashboard_metrics(session):
    tasks = dict((await session.execute(select(Task.status, func.count()).group_by(Task.status))).all())
    workers = dict(
        (await session.execute(select(AgentWorker.status, func.count()).group_by(AgentWorker.status))).all()
    )
    return {
        "task_status_distribution": tasks,
        "employee_status_distribution": workers,
        "running_tasks": tasks.get("RUNNING", 0),
        "pending_tasks": tasks.get("PENDING", 0),
        "waiting_tasks": tasks.get("WAITING", 0),
        "idle_employees": workers.get("IDLE", 0),
        "online_employees": workers.get("IDLE", 0) + workers.get("WORKING", 0),
    }

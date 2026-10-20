"""把需要唤醒责任员工的任务事件写入同一事务的 outbox。"""

from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import TaskStatus
from app.models import Event, Task, TaskWakeOutbox

_TERMINAL_OR_WAITING = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.WAITING,
    TaskStatus.CANCELED,
}


def should_wake(previous: TaskStatus, target: TaskStatus) -> bool:
    if target in _TERMINAL_OR_WAITING:
        return True
    return target is TaskStatus.RUNNING and previous is TaskStatus.FAILED


def wake_hint(task: Task, event: Event, target: TaskStatus) -> str:
    reason = (task.human_required_reason or "").strip()
    reason_text = f"原因：{reason}。" if target is TaskStatus.WAITING and reason else ""
    return (
        f"任务 {task.id} 状态变为 {target.value}（事件 {event.id}）。{reason_text}"
        "请先调用 nexus_task_get 查询最新状态，再决定是否处理。"
        "不要根据这条提示直接改任务状态。"
    )


async def record_wake(
    session: AsyncSession,
    task: Task,
    event: Event,
    previous: TaskStatus,
    target: TaskStatus,
) -> TaskWakeOutbox | None:
    if not should_wake(previous, target):
        return None
    employee_id = (task.responsible_employee_id or "").strip()
    if not employee_id:
        return None
    now = datetime.now(UTC)
    await session.execute(
        update(TaskWakeOutbox)
        .where(TaskWakeOutbox.task_id == task.id, TaskWakeOutbox.status == "pending")
        .values(status="superseded", updated_at=now)
    )
    row = TaskWakeOutbox(
        event_id=event.id,
        task_id=task.id,
        responsible_employee_id=employee_id,
        source_session_id=task.source_session_id,
        status="pending",
        hint=wake_hint(task, event, target),
        attempts=0,
        next_retry_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row

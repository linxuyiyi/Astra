"""把需要唤醒责任员工的任务事件写入同一事务的 outbox。"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import TaskStatus
from app.models import Event, Task, TaskWakeOutbox
from app.runtime.presentation import TASK_STATUS

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
        f"任务 {task.id} 状态变为{TASK_STATUS[target.value]['label']}（事件 {event.id}）。{reason_text}"
        "请先调用 nexus_task_get 查询最新状态，再决定是否处理。"
        "若为等待状态，调用 nexus_task_assistance 读取任务执行端的问题、权限或回合输出；"
        "已有信息能回答就直接回复任务执行端，缺信息或新授权才在原对话问人，不要求人进入 AgentWork。"
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


async def record_assistance_requests(session: AsyncSession, task: Task, request_ids: list[str]) -> None:
    """记录已通知的请求标识；连续等待期间的新问题也必须唤醒，不保存问题内容。"""
    if not request_ids or not task.responsible_employee_id:
        return
    detail = {"session_id": task.current_session_id, "request_ids": sorted(request_ids)}
    previous = await session.scalar(
        select(Event).where(
            Event.task_id == task.id, Event.event_type == "WORKER_ASSISTANCE_REQUESTED",
        ).order_by(Event.id.desc()).limit(1)
    )
    if previous and previous.detail == detail:
        return
    event = Event(
        task_id=task.id, employee_id=task.employee_id, event_type="WORKER_ASSISTANCE_REQUESTED",
        phase="work", message="任务执行端有新的求助请求，请责任员工先处理", detail=detail,
    )
    session.add(event)
    await session.flush()
    if task.status == TaskStatus.WAITING.value:
        await record_wake(session, task, event, TaskStatus.WAITING, TaskStatus.WAITING)

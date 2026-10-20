"""One occupancy predicate for a claimed or bound task.

Unclaimed PENDING does not occupy an employee. A claimed or bound task occupies
until it is SUCCEEDED, or it is CANCELED and a human release still matches the
current Session and pending_attempt. RUNNING and WAITING stay occupied even
when a stop confirmation is present. current_task_count is written only here.
"""

from __future__ import annotations

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import EmployeeStatus, TaskStatus
from app.models import AgentWorker, RuntimeSession, Task


def clear_execution_stop(task: Task) -> None:
    task.execution_stop_confirmed_at = None
    task.execution_stop_session_id = None
    task.execution_stop_attempt = None
    task.execution_stop_operator = None
    task.execution_stop_reason = None


def release_matches(task: Task) -> bool:
    if task.execution_stop_confirmed_at is None:
        return False
    if task.execution_stop_attempt != task.pending_attempt:
        return False
    return task.execution_stop_session_id == task.current_session_id


def task_occupies(task: Task) -> bool:
    if not task.employee_id:
        return False
    unclaimed_pending = (
        task.status == TaskStatus.PENDING
        and task.claimed_at is None
        and task.current_session_id is None
        and task.pending_message_id is None
    )
    if unclaimed_pending:
        return False
    if task.pending_message_id:
        return True
    if task.status == TaskStatus.SUCCEEDED:
        return False
    if task.status == TaskStatus.CANCELED and release_matches(task):
        return False
    return True


def release_match_clause():
    return and_(
        Task.pending_message_id.is_(None),
        Task.status == TaskStatus.CANCELED,
        Task.execution_stop_confirmed_at.is_not(None),
        Task.execution_stop_attempt == Task.pending_attempt,
        or_(
            and_(
                Task.current_session_id.is_(None),
                Task.execution_stop_session_id.is_(None),
            ),
            Task.execution_stop_session_id == Task.current_session_id,
        ),
    )


def occupying_clause(employee_id):
    unclaimed_pending = and_(
        Task.status == TaskStatus.PENDING,
        Task.claimed_at.is_(None),
        Task.current_session_id.is_(None),
        Task.pending_message_id.is_(None),
    )
    succeeded = and_(
        Task.pending_message_id.is_(None),
        Task.status == TaskStatus.SUCCEEDED,
    )
    return and_(
        Task.employee_id == employee_id,
        ~unclaimed_pending,
        ~succeeded,
        ~release_match_clause(),
    )


def occupying_exists(employee_id):
    return exists(select(Task.id).where(occupying_clause(employee_id)))


def blocking_session_exists(employee_id):
    """Sessions still running, waiting, or unknown without a matching human release."""

    released_task = exists(
        select(Task.id).where(Task.id == RuntimeSession.task_id, release_match_clause())
    )
    return exists(
        select(RuntimeSession.id).where(
            RuntimeSession.employee_id == employee_id,
            RuntimeSession.status.in_(["RUNNING", "WAITING", "UNKNOWN"]),
            ~released_task,
        )
    )


async def sync_employee_occupancy(
    session: AsyncSession,
    employee_id: str | None,
    *,
    preferred: Task | None = None,
) -> AgentWorker | None:
    """Write current_task_count from the occupancy predicate."""

    if not employee_id:
        return None
    employee = await session.get(AgentWorker, employee_id)
    if employee is None:
        return None
    rows = (
        await session.scalars(select(Task).where(occupying_clause(employee_id)))
    ).all()
    chosen = None
    if preferred is not None and any(row.id == preferred.id for row in rows):
        chosen = preferred
    elif rows:
        chosen = rows[0]
    employee.current_task_count = int(chosen is not None)
    if employee.status in {EmployeeStatus.PAUSED, EmployeeStatus.ERROR}:
        return employee
    if chosen is None:
        if employee.status in {EmployeeStatus.WORKING, EmployeeStatus.WAIT_HUMAN}:
            employee.status = EmployeeStatus.IDLE
        return employee
    employee.status = (
        EmployeeStatus.WAIT_HUMAN
        if chosen.status == TaskStatus.WAITING
        else EmployeeStatus.WORKING
    )
    return employee

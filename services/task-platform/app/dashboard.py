from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.domain import EmployeeStatus, RuntimeSessionStatus, TaskStatus
from app.models import AgentWorker, Event, RuntimeSession, Task
from app.runtime.presentation import EMPLOYEE_LABELS, TASK_STATUS
from app.services import dashboard_metrics
from app.usage import day_bounds

ATTENTION_STATUSES = {key for key, value in TASK_STATUS.items() if value["group"] == "attention"}
OFFICE_TASK_STATUSES = {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.FAILED}
ACTIVITY_EVENT_TYPES = {
    "TASK_CREATED",
    "TASK_RUNNING",
    "TASK_SUCCEEDED",
    "TASK_FAILED",
    "TASK_WAITING",
    "EMPLOYEE_CREATED",
}
OFFICE_TASK_PRIORITY = {
    TaskStatus.WAITING: 0, TaskStatus.RUNNING: 1, TaskStatus.PENDING: 2, TaskStatus.FAILED: 3,
}
KNOWN_TEAMS = (
    ("automation", "自动化工程", 10, re.compile(r"自动化|集成|接口"), {"automation", "integration"}),
    ("testing-platform", "测试平台", 20, re.compile(r"测试平台|平台开发|平台测试"), {"testing-platform"}),
    (
        "defense-operation",
        "防线运营",
        30,
        re.compile(r"防线|质量运营|质量保障|安全运营"),
        {"defense-operation", "incident"},
    ),
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _local_date(value: datetime) -> str:
    timezone = ZoneInfo(get_settings().timezone)
    return _aware(value).astimezone(timezone).date().isoformat()


def _team_identity(employee: AgentWorker) -> tuple[str, str, int]:
    role = (employee.role or "").strip()
    category_ids = set(employee.category_ids or [])
    for team_id, name, order, pattern, categories in KNOWN_TEAMS:
        if pattern.search(role) or category_ids.intersection(categories):
            return team_id, name, order
    name = re.sub(r"(?:数字)?员工$|工程师$|专家$|专员$|开发$|测试$", "", role).strip() or "综合协作"
    digest = hashlib.sha1(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"team-{digest}", name, 100


def _current_session_condition():
    # Only the task's current, unsuperseded binding can describe its office desk.
    return and_(
        RuntimeSession.id == Task.current_session_id,
        RuntimeSession.task_id == Task.id,
        RuntimeSession.employee_id == Task.employee_id,
        RuntimeSession.superseded_by.is_(None),
    )


async def _current_employee_tasks(session: AsyncSession) -> dict[str, tuple[Task, RuntimeSession | None]]:
    rows = (
        await session.execute(
            select(Task, RuntimeSession)
            .outerjoin(RuntimeSession, _current_session_condition())
            .where(Task.employee_id.is_not(None), Task.status.in_(OFFICE_TASK_STATUSES))
            .order_by(Task.updated_at.desc(), Task.id)
        )
    ).all()
    current: dict[str, tuple[Task, RuntimeSession | None]] = {}
    for task, binding in rows:
        previous = current.get(task.employee_id)
        bound_active = binding is not None and task.status in {TaskStatus.RUNNING, TaskStatus.WAITING}
        rank = (not bound_active, OFFICE_TASK_PRIORITY[task.status])
        previous_bound_active = previous is not None and previous[1] is not None and previous[0].status in {
            TaskStatus.RUNNING, TaskStatus.WAITING,
        }
        if previous is None or rank < (not previous_bound_active, OFFICE_TASK_PRIORITY[previous[0].status]):
            current[task.employee_id] = (task, binding)
    return current


def _employee_state(
    employee: AgentWorker, task: Task | None, binding: RuntimeSession | None,
) -> tuple[str, str]:
    if not employee.enabled:
        return "idle", "已停用"
    if employee.status == EmployeeStatus.OFFLINE:
        return "idle", employee.runtime_status_label
    if employee.status == EmployeeStatus.PAUSED:
        return "waiting", employee.runtime_status_label
    if employee.status == EmployeeStatus.ERROR:
        return "error", employee.runtime_status_label
    if task is None:
        if employee.status == EmployeeStatus.WAIT_HUMAN:
            return "waiting", employee.runtime_status_label
        return "idle", EMPLOYEE_LABELS[EmployeeStatus.IDLE]
    if task.status == TaskStatus.FAILED:
        return "error", TASK_STATUS[TaskStatus.FAILED]["label"]
    if task.runtime_connection == "UNAVAILABLE" or (
        binding and binding.status == RuntimeSessionStatus.UNKNOWN
    ):
        return "waiting", "连接异常/状态待确认"
    if binding and binding.status == RuntimeSessionStatus.SUCCEEDED and task.status != TaskStatus.SUCCEEDED:
        return "waiting", "执行结束，待确认"
    if binding and binding.status in {
        RuntimeSessionStatus.FAILED,
        RuntimeSessionStatus.INTERRUPTED,
    }:
        return "waiting", "执行异常，待恢复"
    if task.status == TaskStatus.WAITING or (binding and binding.status == RuntimeSessionStatus.WAITING):
        reason = (task.human_required_reason or "").strip()
        return "waiting", reason or TASK_STATUS[TaskStatus.WAITING]["label"]
    if task.status == TaskStatus.PENDING:
        return "idle", task.runtime_status_label
    if binding and binding.status == RuntimeSessionStatus.RUNNING and task.runtime_connection == "CONNECTED":
        return "working", task.runtime_status_label
    return "idle", "状态待确认"


def _office_task(task: Task, binding: RuntimeSession | None) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "current_session_id": task.current_session_id,
        "session_status": binding.status if binding else None,
        "runtime_status_label": task.runtime_status_label,
        "runtime_detail": task.runtime_detail,
        "human_required_reason": task.human_required_reason,
        "runtime_connection": task.runtime_connection,
    }


def _attention_condition():
    return or_(
        Task.status.in_(ATTENTION_STATUSES),
        Task.inactivity_alert.is_(True),
    )


async def dashboard_overview(session: AsyncSession) -> dict:
    metrics = await dashboard_metrics(session)
    employees = (await session.scalars(select(AgentWorker))).all()
    current_tasks = await _current_employee_tasks(session)
    team_count = len({_team_identity(employee)[0] for employee in employees})
    attention_total = await session.scalar(select(func.count(Task.id)).where(_attention_condition()))
    return {
        "employee_total": len(employees),
        "team_count": team_count,
        "working_employees": sum(
            _employee_state(employee, *current_tasks.get(employee.id, (None, None)))[0] == "working"
            for employee in employees
        ),
        "running_tasks": metrics["running_tasks"],
        "waiting_tasks": metrics["waiting_tasks"],
        "failed_tasks": metrics["failed_tasks"],
        "online_employees": metrics["online_employees"],
        "completed_today": metrics["completed_today"],
        "attention_total": attention_total or 0,
    }


async def dashboard_employees(session: AsyncSession) -> dict:
    employees = (
        await session.scalars(
            select(AgentWorker).order_by(AgentWorker.created_at, AgentWorker.id)
        )
    ).all()
    current_tasks = await _current_employee_tasks(session)

    teams: dict[str, dict] = {}
    for employee in employees:
        team_id, team_name, order = _team_identity(employee)
        team = teams.setdefault(
            team_id,
            {"id": team_id, "name": team_name, "order": order, "employee_count": 0, "employees": []},
        )
        task, binding = current_tasks.get(employee.id, (None, None))
        state, state_label = _employee_state(employee, task, binding)
        team["employees"].append(
            {
                "id": employee.id,
                "name": employee.name,
                "role": employee.role,
                "character_id": employee.character_id,
                "status": employee.status,
                "enabled": employee.enabled,
                "runtime_state": employee.runtime_state,
                "runtime_status_label": employee.runtime_status_label,
                "runtime_detail": employee.runtime_detail,
                "failure": employee.failure,
                "state": state,
                "state_label": state_label,
                "current_task": _office_task(task, binding) if task else None,
            }
        )
        team["employee_count"] += 1
    return {"teams": sorted(teams.values(), key=lambda team: (team["order"], team["name"]))}


def _attention_status(task: Task) -> tuple[str, str]:
    return task.status, task.runtime_status_label


async def dashboard_attention(session: AsyncSession) -> dict:
    rows = (
        await session.execute(
            select(Task, AgentWorker.name, RuntimeSession)
            .outerjoin(AgentWorker, Task.employee_id == AgentWorker.id)
            .outerjoin(RuntimeSession, _current_session_condition())
            .where(_attention_condition())
            .order_by(Task.updated_at.desc())
            .limit(5)
        )
    ).all()
    items = []
    for task, employee_name, binding in rows:
        status, status_label = _attention_status(task)
        occurred_at = task.inactivity_alert_since or task.finished_at or task.updated_at
        items.append(
            {
                "task_id": task.id,
                "task_title": task.title,
                "employee_id": task.employee_id,
                "employee_name": employee_name or "未分配",
                "status": status,
                "status_label": status_label,
                "current_session_id": task.current_session_id,
                "session_status": binding.status if binding else None,
                "runtime_detail": task.runtime_detail,
                "human_required_reason": task.human_required_reason,
                "runtime_connection": task.runtime_connection,
                "occurred_at": occurred_at,
            }
        )
    return {"items": items}


async def dashboard_tasks(session: AsyncSession) -> dict:
    metrics = await dashboard_metrics(session)
    start, end = day_bounds(7)
    rows = (
        await session.execute(
            select(Task.created_at).where(
                or_(Task.created_at >= start, Task.finished_at >= start)
            )
        )
    ).all()
    completed_at_values = (
        await session.execute(
            select(Task.completed_at).where(
                Task.completed_at.is_not(None), Task.completed_at >= start
            )
        )
    ).scalars().all()
    timezone = ZoneInfo(get_settings().timezone)
    first_day = start.astimezone(timezone).date()
    today = (end - timedelta(microseconds=1)).astimezone(timezone).date()
    trend = []
    points: dict[str, dict[str, int | str]] = {}
    current = first_day
    while current <= today:
        key = current.isoformat()
        points[key] = {"date": key, "total": 0, "completed": 0}
        trend.append(points[key])
        current += timedelta(days=1)
    for (created_at,) in rows:
        created_key = _local_date(created_at)
        if created_key in points:
            points[created_key]["total"] += 1
    for completed_at in completed_at_values:
        # Business completion follows the immutable completed_at day.
        completed_key = _local_date(completed_at)
        if completed_key in points:
            points[completed_key]["completed"] += 1
    recent_rows = (
        await session.execute(
            select(Task, AgentWorker.name)
            .outerjoin(AgentWorker, Task.employee_id == AgentWorker.id)
            .order_by(Task.updated_at.desc())
            .limit(6)
        )
    ).all()
    return {
        "running": metrics["running_tasks"],
        "waiting": metrics["waiting_tasks"],
        "completed_today": metrics["completed_today"],
        "failed_today": metrics["failed_today"],
        "trend": trend,
        "recent": [
            {
                "id": task.id,
                "title": task.title,
                "status": task.status,
                "runtime_state": task.runtime_state,
                "runtime_status_label": task.runtime_status_label,
                "runtime_detail": task.runtime_detail,
                "failure": task.failure,
                "employee_id": task.employee_id,
                "employee_name": employee_name or "未分配",
                "updated_at": task.updated_at,
            }
            for task, employee_name in recent_rows
        ],
    }


def _activity_copy(
    event: Event,
    task_title: str | None,
    task_source: str | None,
    employee_name: str | None,
) -> tuple[str, str]:
    title = task_title or "任务"
    if event.event_type == "EMPLOYEE_CREATED":
        return "employee_created", f"新增 Agent Worker：{employee_name or 'Agent Worker'}"
    if event.event_type == "TASK_CREATED":
        if task_source == "nr_test_system":
            return "schedule_triggered", f"定时任务触发：{title}"
        return "task_created", f"创建任务：{title}"
    if event.event_type == "TASK_RUNNING":
        return "task_started", f"开始执行：{title}"
    if event.event_type == "TASK_SUCCEEDED":
        return "task_completed", f"完成任务：{title}"
    if event.event_type in {"TASK_FAILED"}:
        return "task_failed", f"任务失败：{title}"
    return "waiting_human", f"等待人工确认：{title}"


async def dashboard_activity(session: AsyncSession) -> dict:
    rows = (
        await session.execute(
            select(Event, AgentWorker.name, Task.title, Task.source)
            .outerjoin(AgentWorker, Event.employee_id == AgentWorker.id)
            .outerjoin(Task, Event.task_id == Task.id)
            .where(
                Event.event_type.in_(ACTIVITY_EVENT_TYPES),
                # 已删除目标的历史事件不能挤占首页，也不能提供失效的详情入口。
                or_(
                    and_(Event.task_id.is_not(None), Task.id.is_not(None)),
                    and_(Event.task_id.is_(None), AgentWorker.id.is_not(None)),
                ),
            )
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(12)
        )
    ).all()
    items = []
    for event, employee_name, task_title, task_source in rows:
        kind, description = _activity_copy(event, task_title, task_source, employee_name)
        target_type = "task" if event.task_id else "employee" if event.employee_id else None
        target_id = event.task_id or event.employee_id
        items.append(
            {
                "id": event.id,
                "employee_id": event.employee_id,
                "employee_name": employee_name or "系统",
                "kind": kind,
                "description": description,
                "created_at": event.created_at,
                "target_type": target_type,
                "target_id": target_id,
            }
        )
    return {"items": items}

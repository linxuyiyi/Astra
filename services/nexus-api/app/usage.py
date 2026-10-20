from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.domain import RuntimeSessionStatus, TaskStatus
from app.models import RuntimeSession, Task

# "任务成功率" 的已完成口径与 Experience 自动验证同源：Task.completed_at 不为空
# 表示业务签收完成。completed_at 只在首次成功时写入，restart 不会改写历史统计。
COMPLETED_TASK_STATUS = TaskStatus.SUCCEEDED

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
    "llm_call_count",
    "tool_call_count",
)


def day_bounds(days: int = 1) -> tuple[datetime, datetime]:
    timezone = ZoneInfo(get_settings().timezone)
    today = datetime.now(timezone).date()
    start_date = today - timedelta(days=max(1, days) - 1)
    start = datetime.combine(start_date, time.min, tzinfo=timezone).astimezone(UTC)
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=timezone).astimezone(UTC)
    return start, end


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _attributed_column():
    # Terminal Sessions belong to finished_at; live Sessions still use started_at.
    return func.coalesce(
        RuntimeSession.finished_at,
        RuntimeSession.started_at,
        RuntimeSession.created_at,
    )


def session_attributed_at(runtime_session: RuntimeSession) -> datetime:
    return _aware(
        runtime_session.finished_at
        or runtime_session.started_at
        or runtime_session.created_at
    )


def session_local_date(runtime_session: RuntimeSession) -> date:
    timezone = ZoneInfo(get_settings().timezone)
    return session_attributed_at(runtime_session).astimezone(timezone).date()


async def load_sessions(
    session: AsyncSession,
    *,
    employee_id: str | None = None,
    task_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    overlap: bool = False,
) -> list[RuntimeSession]:
    """Load Sessions in scope.

    With ``overlap`` a Session is included when its lifetime intersects the
    window, so per-message daily buckets that fall inside the window are not
    dropped because the Session itself was attributed to another day.
    """

    query = select(RuntimeSession)
    if employee_id:
        query = query.where(RuntimeSession.employee_id == employee_id)
    if task_id:
        query = query.where(RuntimeSession.task_id == task_id)
    attributed = _attributed_column()
    if overlap and start and end:
        query = query.where(
            attributed < end,
            or_(
                RuntimeSession.finished_at.is_(None),
                RuntimeSession.finished_at >= start,
            ),
        )
    else:
        if start:
            query = query.where(attributed >= start)
        if end:
            query = query.where(attributed < end)
    return (await session.execute(query.order_by(attributed))).scalars().all()


def _sum_field(sessions: list[RuntimeSession], field: str) -> int | None:
    values = [getattr(item, field) for item in sessions if getattr(item, field) is not None]
    return sum(values) if values else None


def _breakdown_totals(entries: list[dict]) -> dict[str, int | None]:
    totals: dict[str, int | None] = {field: None for field in TOKEN_FIELDS}
    for field in TOKEN_FIELDS:
        values = [
            entry.get(field) for entry in entries if entry.get(field) is not None
        ]
        if values:
            totals[field] = sum(values)
    return totals


def aggregate_usage_by_day(sessions: list[RuntimeSession]) -> dict[str, dict[str, int | None]]:
    """Token/tool totals per real message day across the given Sessions."""

    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in sessions:
        for entry in item.usage_by_day or []:
            day = entry.get("date")
            if day:
                grouped[str(day)].append(entry)
    return {day: _breakdown_totals(entries) for day, entries in grouped.items()}


def apply_day_tokens(point: dict, bucket: dict[str, int | None] | None) -> None:
    """Replace Session-day token values with real message-day values."""

    for field in TOKEN_FIELDS:
        point[field] = bucket.get(field) if bucket else None


async def count_completed_tasks(
    session: AsyncSession,
    *,
    employee_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Business-accepted Task count, attributed to the immutable completed_at."""

    query = select(func.count(func.distinct(Task.id))).where(Task.completed_at.is_not(None))
    if employee_id:
        query = query.where(Task.employee_id == employee_id)
    if start:
        query = query.where(Task.completed_at >= start)
    if end:
        query = query.where(Task.completed_at < end)
    return int(await session.scalar(query) or 0)


async def _task_occupancy_seconds(
    session: AsyncSession,
    task_ids: set[str],
    now: datetime,
    window: tuple[datetime, datetime] | None,
) -> int:
    """Wall clock from Task.started_at to Task.finished_at.

    A RuntimeSession row can be shorter than the task: the session is created
    after the worker is already RUNNING, and finished_at may follow the native
    message clock. Work duration follows the task interval, clipped to the
    reporting window when one is given.
    """

    if not task_ids:
        return 0
    tasks = (await session.scalars(select(Task).where(Task.id.in_(task_ids)))).all()
    total = 0
    for task in tasks:
        if not task.started_at:
            continue
        start = _aware(task.started_at)
        end = _aware(task.finished_at) if task.finished_at else now
        if window is not None:
            window_start, window_end = window
            start = max(start, _aware(window_start))
            end = min(end, _aware(window_end))
        total += max(0, int((end - start).total_seconds()))
    return total


async def summarize_sessions(
    session: AsyncSession,
    sessions: list[RuntimeSession],
    *,
    window: tuple[datetime, datetime] | None = None,
) -> dict:
    summary = {field: _sum_field(sessions, field) for field in TOKEN_FIELDS}

    duration_seconds = 0
    now = datetime.now(UTC)
    for item in sessions:
        if not item.started_at:
            continue
        end = _aware(item.finished_at) if item.finished_at else now
        duration_seconds += max(0, int((end - _aware(item.started_at)).total_seconds()))
    task_ids = {item.task_id for item in sessions if item.task_id}
    duration_seconds = max(
        duration_seconds,
        await _task_occupancy_seconds(session, task_ids, now, window),
    )

    tool_success = 0
    tool_failure = 0
    for item in sessions:
        for tool in item.tool_usage or []:
            tool_success += int(tool.get("success_count", 0))
            tool_failure += int(tool.get("failure_count", 0))

    # Unknown execution time stays unknown; it must never be folded into idle.
    execution_values = [item.execution_seconds for item in sessions]
    if sessions and any(value is None for value in execution_values):
        execution_seconds: int | None = None
    else:
        execution_seconds = sum(value or 0 for value in execution_values)
    idle_seconds = (
        None if execution_seconds is None else max(0, duration_seconds - execution_seconds)
    )

    task_ids = {item.task_id for item in sessions}
    completed_tasks = 0
    waiting_seconds = 0
    if task_ids:
        completed_tasks = int(
            await session.scalar(
                select(func.count(func.distinct(Task.id))).where(
                    Task.id.in_(task_ids), Task.completed_at.is_not(None)
                )
            )
            or 0
        )
        waiting_rows = (
            await session.execute(
                select(Task.submitted_at, Task.confirmed_at).where(
                    Task.id.in_(task_ids), Task.submitted_at.is_not(None)
                )
            )
        ).all()
        for submitted_at, confirmed_at in waiting_rows:
            end = _aware(confirmed_at) if confirmed_at else now
            waiting_seconds += max(0, int((end - _aware(submitted_at)).total_seconds()))

    session_statuses = [item.status for item in sessions]
    return {
        **summary,
        "tool_success_count": tool_success,
        "tool_failure_count": tool_failure,
        "duration_seconds": duration_seconds,
        "execution_seconds": execution_seconds,
        "idle_seconds": idle_seconds,
        "waiting_seconds": waiting_seconds,
        "completed_tasks": completed_tasks,
        "task_count": len(task_ids),
        "session_count": len(sessions),
        "native_session_count": sum(item.native_session_count or 1 for item in sessions),
        "completed_sessions": session_statuses.count(RuntimeSessionStatus.SUCCEEDED.value),
        "failed_sessions": session_statuses.count(RuntimeSessionStatus.FAILED.value),
        "interrupted_sessions": session_statuses.count(RuntimeSessionStatus.INTERRUPTED.value),
    }


def model_breakdown(sessions: list[RuntimeSession]) -> list[dict]:
    """Per-model consumption from the real message model, not the Session label."""

    grouped: dict[str, dict[str, int | str]] = {}
    for item in sessions:
        used: set[str] = set()
        for entry in item.model_usage or []:
            name = str(entry.get("model_name") or "未知")
            used.add(name)
            bucket = grouped.setdefault(
                name,
                {
                    "model_name": name,
                    "session_count": 0,
                    "total_tokens": 0,
                    "llm_call_count": 0,
                    "tool_call_count": 0,
                },
            )
            bucket["total_tokens"] += int(entry.get("total_tokens") or 0)
            bucket["llm_call_count"] += int(entry.get("llm_call_count") or 0)
            bucket["tool_call_count"] += int(entry.get("tool_call_count") or 0)
        for name in used:
            grouped[name]["session_count"] += 1
    return sorted(grouped.values(), key=lambda item: item["total_tokens"], reverse=True)


async def tool_breakdown(session: AsyncSession, sessions: list[RuntimeSession]) -> list[dict]:
    grouped: dict[str, dict[str, int]] = defaultdict(
        lambda: {"call_count": 0, "success_count": 0, "failure_count": 0, "duration_ms": 0}
    )
    for item in sessions:
        for tool in item.tool_usage or []:
            name = str(tool.get("tool_name") or "其他")
            entry = grouped[name]
            entry["call_count"] += int(tool.get("call_count", 0))
            entry["success_count"] += int(tool.get("success_count", 0))
            entry["failure_count"] += int(tool.get("failure_count", 0))
            entry["duration_ms"] += int(tool.get("duration_ms", 0))
    return [
        {"tool_name": name, **values}
        for name, values in sorted(grouped.items(), key=lambda item: item[1]["call_count"], reverse=True)
    ]


async def employee_usage(session: AsyncSession, employee_id: str, days: int) -> dict:
    start, end = day_bounds(days)
    # Overlap so cross-midnight Sessions contribute their real message-day tokens.
    sessions = await load_sessions(
        session, employee_id=employee_id, start=start, end=end, overlap=True
    )
    summary = await summarize_sessions(session, sessions, window=(start, end))
    tools = await tool_breakdown(session, sessions)
    timezone = ZoneInfo(get_settings().timezone)
    by_day = aggregate_usage_by_day(sessions)
    trend: list[dict] = []
    for offset in range(days):
        day_start = start + timedelta(days=offset)
        day_end = day_start + timedelta(days=1)
        day = day_start.astimezone(timezone).date()
        day_sessions = [item for item in sessions if session_local_date(item) == day]
        point = await summarize_sessions(session, day_sessions, window=(day_start, day_end))
        apply_day_tokens(point, by_day.get(day.isoformat()))
        # Business completion belongs to the immutable completed_at day.
        point["completed_tasks"] = await count_completed_tasks(
            session, employee_id=employee_id, start=day_start, end=day_start + timedelta(days=1)
        )
        trend.append({"date": day.isoformat(), **point})
    return {
        "period_days": days,
        "summary": summary,
        "trend": trend,
        "tools": tools,
        "models": model_breakdown(sessions),
        "token_by_day": [
            {"date": day, **values} for day, values in sorted(by_day.items())
        ],
    }


async def today_token_bucket(session: AsyncSession, *, employee_id: str | None = None) -> dict | None:
    """Real message-day token totals for the current local day."""

    timezone = ZoneInfo(get_settings().timezone)
    today = datetime.now(timezone).date()
    start, end = day_bounds()
    sessions = await load_sessions(
        session, employee_id=employee_id, start=start, end=end, overlap=True
    )
    by_day = aggregate_usage_by_day(sessions)
    return by_day.get(today.isoformat())


async def task_usage(session: AsyncSession, task_id: str) -> dict:
    sessions = await load_sessions(session, task_id=task_id)
    by_day = aggregate_usage_by_day(sessions)
    return {
        "summary": await summarize_sessions(session, sessions),
        "tools": await tool_breakdown(session, sessions),
        "models": model_breakdown(sessions),
        "token_by_day": [
            {"date": day, **values} for day, values in sorted(by_day.items())
        ],
        "sessions": len(sessions),
        "root_session_count": len(sessions),
        "native_session_count": sum(item.native_session_count or 1 for item in sessions),
    }
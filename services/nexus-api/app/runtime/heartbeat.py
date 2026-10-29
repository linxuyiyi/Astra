from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.domain import EmployeeStatus
from app.models import AgentWorker
from app.runtime.occupancy import blocking_session_exists, occupying_exists


async def heartbeat_active_employees(session: AsyncSession) -> list[str]:
    from app.runtime.lifecycle import RuntimeService
    return await RuntimeService(session).refresh_employee_heartbeats()


async def mark_stale_employees_offline(session: AsyncSession, settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    threshold = datetime.now(UTC) - timedelta(seconds=settings.heartbeat_timeout)
    employees = (
        (
            await session.execute(
                select(AgentWorker).where(
                    AgentWorker.last_heartbeat.is_not(None),
                    AgentWorker.last_heartbeat < threshold,
                    ~occupying_exists(AgentWorker.id),
                    ~blocking_session_exists(AgentWorker.id),
                    AgentWorker.status.not_in(
                        [EmployeeStatus.OFFLINE, EmployeeStatus.PAUSED, EmployeeStatus.ERROR]
                    ),
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for employee in employees:
        employee.status = EmployeeStatus.OFFLINE
    if employees:
        await session.commit()
    return [employee.id for employee in employees]

from typing import Any

from sqlalchemy import func, select

from app.models import DigitalEmployee, Task


async def nexus_operation_overview(params: dict[str, Any], user_id: str, db_session) -> dict[str, Any]:
    # Compute actual stats
    task_result = await db_session.execute(select(Task.status, func.count()).group_by(Task.status))
    task_stats = {row[0]: row[1] for row in task_result.all()}

    emp_result = await db_session.execute(
        select(DigitalEmployee.status, func.count()).group_by(DigitalEmployee.status)
    )
    emp_stats = {row[0]: row[1] for row in emp_result.all()}

    return {
        "tasks": task_stats,
        "employees": emp_stats
    }

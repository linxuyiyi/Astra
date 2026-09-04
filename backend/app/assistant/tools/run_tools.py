from typing import Any

from sqlalchemy import select

from app.models import Run


async def nexus_run_search(params: dict[str, Any], user_id: str, db_session) -> dict[str, Any]:
    query = select(Run)
    if params.get("task_id"):
        query = query.where(Run.task_id == params["task_id"])

    result = await db_session.execute(query.limit(10))
    runs = result.scalars().all()

    return {
        "items": [
            {"id": r.id, "task_id": r.task_id, "status": r.status}
            for r in runs
        ],
        "total": len(runs)
    }

async def nexus_run_failure_summary(params: dict[str, Any], user_id: str, db_session) -> dict[str, Any]:
    run_id = params.get("run_id")
    result = await db_session.execute(select(Run).where(Run.id == run_id))
    run = result.scalar_one_or_none()

    if not run:
        return {"error": "Run not found"}

    return {
        "run_id": run.id,
        "status": run.status,
        "error": run.error_summary or "Unknown error"
    }

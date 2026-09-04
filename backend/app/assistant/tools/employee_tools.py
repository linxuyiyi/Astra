from typing import Any

from sqlalchemy import select

from app.models import DigitalEmployee


async def nexus_employee_search(params: dict[str, Any], user_id: str, db_session) -> dict[str, Any]:
    query = select(DigitalEmployee)
    if params.get("keyword"):
        query = query.where(DigitalEmployee.name.contains(params["keyword"]))

    result = await db_session.execute(query)
    employees = result.scalars().all()

    return {
        "items": [
            {"id": e.id, "name": e.name, "status": e.status}
            for e in employees
        ],
        "total": len(employees)
    }

async def nexus_employee_recommend(params: dict[str, Any], user_id: str, db_session) -> dict[str, Any]:
    # Basic recommend randomly matching type
    result = await db_session.execute(select(DigitalEmployee).where(DigitalEmployee.enabled).limit(3))
    employees = result.scalars().all()
    return {
        "recommendations": [
            {"employee_id": e.id, "name": e.name, "reason": "Available"}
            for e in employees
        ]
    }

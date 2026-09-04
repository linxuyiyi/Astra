import uuid
from typing import Any

from sqlalchemy import select

from app.models import Task


async def nexus_task_search(params: dict[str, Any], user_id: str, db_session) -> dict[str, Any]:
    query = select(Task).order_by(Task.created_at.desc()).limit(params.get("limit", 20))
    if params.get("keyword"):
        query = query.where(Task.title.contains(params["keyword"]))
    if params.get("task_id"):
        query = query.where(Task.id == params["task_id"])

    result = await db_session.execute(query)
    tasks = result.scalars().all()

    return {
        "items": [
            {"id": t.id, "title": t.title, "status": getattr(t.status, "value", t.status),
             "employee_id": t.employee_id}
            for t in tasks
        ],
        "total": len(tasks)
    }

async def nexus_task_get(params: dict[str, Any], user_id: str, db_session) -> dict[str, Any]:
    task_id = params.get("task_id")
    result = await db_session.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()

    if not task:
        return {"error": "Task not found"}

    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status.value if hasattr(task.status, 'value') else task.status,
        "employee_id": task.employee_id
    }

async def nexus_task_prepare_create(params: dict[str, Any], user_id: str, db_session) -> dict[str, Any]:
    from app.assistant.models import AssistantOperation

    op_id = f"OP-{uuid.uuid4().hex}"

    # Properly construct TaskCreate kwargs based on Astra's real requirements
    task_payload = {
        "source": "manual",
        "task_type": params.get("task_type", "ISSUE"),
        "category_id": params.get("category_id") or "default",
        "title": params.get("title") or "New Task",
        "description": params.get("description") or "Created by Assistant",
        "acceptance_criteria": [{"content": c} for c in (params.get("acceptance_criteria") or ["Done"])],
        "employee_id": params.get("employee_id"),
        "priority": params.get("priority", "MEDIUM")
    }

    op = AssistantOperation(
        id=uuid.uuid4().hex,
        operation_id=op_id,
        conversation_id=params.get("conversation_id") or None,
        user_id=user_id,
        operation_type="task_create",
        risk_level="L1",
        status="WAITING_CONFIRM",
        request_json=task_payload,
        preview_json=task_payload
    )
    db_session.add(op)
    await db_session.commit()

    return {
        "operation_id": op_id,
        "requires_confirmation": True,
        "preview": op.preview_json
    }

async def nexus_task_cancel(params: dict[str, Any], user_id: str, db_session) -> dict[str, Any]:
    from app.assistant.models import AssistantOperation

    op_id = f"OP-{uuid.uuid4().hex}"
    op = AssistantOperation(
        id=uuid.uuid4().hex,
        operation_id=op_id,
        conversation_id=params.get("conversation_id", ""),
        user_id=user_id,
        operation_type="task_cancel",
        risk_level="L2",
        status="WAITING_CONFIRM",
        request_json={"task_id": params.get("task_id")},
        preview_json={"task_id": params.get("task_id")}
    )
    db_session.add(op)
    await db_session.commit()

    return {
        "operation_id": op_id,
        "requires_confirmation": True,
        "preview": op.preview_json
    }

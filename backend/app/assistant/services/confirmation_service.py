
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.models import AssistantOperation
from app.models import utcnow
from app.schemas import TaskCreate
from app.services import TaskService


class ConfirmationService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.task_service = TaskService(db)

    async def get_operation(self, operation_id: str) -> AssistantOperation | None:
        result = await self.db.execute(
            select(AssistantOperation).where(AssistantOperation.operation_id == operation_id)
        )
        return result.scalar_one_or_none()

    async def confirm_operation(self, operation_id: str, user_id: str) -> AssistantOperation:
        op = await self.get_operation(operation_id)
        if not op:
            raise ValueError("Operation not found")

        # Security checks
        if op.user_id != user_id:
            raise ValueError("Unauthorized")
        if op.status != "PREPARED" and op.status != "WAITING_CONFIRM":
            raise ValueError(f"Operation cannot be confirmed in status {op.status}")

        op.status = "CONFIRMED"
        op.confirmed_at = utcnow()
        await self.db.commit()
        await self.db.refresh(op)

        # Actually execute the operation based on type
        op.status = "EXECUTING"
        await self.db.commit()

        try:
            if op.operation_type == "task_create":
                task_payload = TaskCreate(**op.request_json)
                task = await self.task_service.create(task_payload)
                op.result_json = {"task_id": task.id, "status": task.status}
            elif op.operation_type == "task_cancel":
                task_id = op.request_json.get("task_id")
                from app.api.routes import (
                    _abort_opencode_session,
                    _cancel_active_runs,
                    require_task,
                    transition_task,
                )
                from app.domain import TaskStatus
                task = await require_task(task_id, self.db)
                runtime_aborted = await _abort_opencode_session(task, self.db)
                await transition_task(
                    task,
                    TaskStatus.CANCELLED,
                    "任务已由平台助手取消",
                    self.db,
                    None,
                    "任务已取消",
                )
                await _cancel_active_runs(task.id, self.db)
                if runtime_aborted:
                    await self.task_service.add_event(
                        task,
                        "OPENCODE_ABORTED",
                        "runtime",
                        "OpenCode Session 已中止",
                        {"session_id": task.opencode_session_id, "reason": "cancel"},
                    )
                op.result_json = {"status": "CANCELLED"}
            else:
                op.result_json = {"message": "Executed"}

            op.status = "SUCCESS"
            op.executed_at = utcnow()
        except Exception as e:
            op.status = "FAILED"
            op.result_json = {"error": str(e)}

        await self.db.commit()
        await self.db.refresh(op)
        return op

    async def cancel_operation(self, operation_id: str, user_id: str) -> AssistantOperation:
        op = await self.get_operation(operation_id)
        if not op:
            raise ValueError("Operation not found")

        if op.user_id != user_id:
            raise ValueError("Unauthorized")

        op.status = "CANCELLED"
        await self.db.commit()
        await self.db.refresh(op)
        return op

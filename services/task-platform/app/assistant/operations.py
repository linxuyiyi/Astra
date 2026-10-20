"""助手操作的 prepare → confirm 流程。

第一步 prepare 只做校验、补默认值、生成预览并落库；真正的业务执行只在
用户点击确认后发生（REST /operations/{id}/confirm），不会把“用户确认”
重新发给模型再让模型决定要不要调用工具。operation_id 是幂等键。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.models import AssistantOperation
from app.config.settings import Settings, get_settings
from app.domain import Priority, TaskStatus, TaskType
from app.models import AgentWorker, Task, TaskCategory
from app.schemas import AcceptanceCriterion, TaskCreate
from app.services import TaskService

RISK_LABEL = {"L1": "低", "L2": "中", "L3": "高"}


class OperationError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def new_operation_id() -> str:
    return f"OP-{uuid4().hex[:12].upper()}"


def task_type_label(value: str) -> str:
    return {"ISSUE": "问题", "REQUIREMENT": "需求"}.get(value, value)


class OperationService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    async def get(self, operation_id: str) -> AssistantOperation | None:
        return await self.session.scalar(
            select(AssistantOperation).where(AssistantOperation.operation_id == operation_id)
        )

    async def _create_operation(
        self,
        *,
        operation_type: str,
        risk_level: str,
        user_id: str,
        request: dict[str, Any],
        preview: dict[str, Any],
        conversation_id: str | None,
        message_id: str | None,
    ) -> AssistantOperation:
        operation = AssistantOperation(
            operation_id=new_operation_id(),
            conversation_id=conversation_id,
            message_id=message_id,
            user_id=user_id,
            operation_type=operation_type,
            risk_level=risk_level,
            status="WAITING_CONFIRM",
            request_json=request,
            preview_json=preview,
            expires_at=datetime.now(UTC) + timedelta(minutes=self.settings.assistant_operation_ttl_minutes),
        )
        self.session.add(operation)
        await self.session.commit()
        await self.session.refresh(operation)
        return operation

    async def link_conversation(
        self, operation_id: str, conversation_id: str, message_id: str | None = None
    ) -> None:
        """Orchestrator 在 Tool 事件流中把操作关联回真实会话。"""
        operation = await self.get(operation_id)
        if not operation:
            return
        # 归属只能由接收到真实 OpenCode Session 事件的编排器确定，不能接受
        # 模型在 Tool 参数中伪造的 conversation_id。
        operation.conversation_id = conversation_id
        if message_id:
            operation.message_id = message_id
        await self.session.commit()

    async def prepare_task_create(
        self,
        *,
        user_id: str,
        task_type: str,
        employee_id: str | None,
        title: str | None = None,
        description: str | None = None,
        category_id: str | None = None,
        acceptance_criteria: list[str] | None = None,
        priority: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
    ) -> AssistantOperation:
        try:
            task_type_enum = TaskType(str(task_type or "").upper())
        except ValueError as exc:
            raise OperationError(f"不支持的任务类型：{task_type}", status_code=400) from exc

        employee: AgentWorker | None = None
        if employee_id:
            employee = await self.session.get(AgentWorker, employee_id)
            if not employee:
                raise OperationError(f"Agent Worker 不存在：{employee_id}", status_code=404)
            if not employee.enabled:
                raise OperationError(f"Agent Worker {employee.name} 已停用")
            if task_type_enum.value not in employee.task_types:
                raise OperationError(
                    f"Agent Worker {employee.name} 不支持任务类型 {task_type_label(task_type_enum.value)}"
                )

        category: TaskCategory | None = None
        if category_id:
            category = await self.session.get(TaskCategory, category_id)
            if not category or not category.enabled:
                raise OperationError(f"任务分类不可用：{category_id}")
        elif employee and employee.category_ids:
            category = await self.session.get(TaskCategory, employee.category_ids[0])
        if category is None:
            category = await self.session.scalar(
                select(TaskCategory).where(TaskCategory.enabled.is_(True)).limit(1)
            )
        if category is None:
            raise OperationError("平台尚未配置任务分类，无法创建任务")
        if employee and employee.category_ids and category.id not in employee.category_ids:
            raise OperationError(f"Agent Worker {employee.name} 不支持任务分类 {category.name}")

        if priority:
            try:
                priority_enum = Priority(str(priority).upper())
            except ValueError as exc:
                raise OperationError(f"不支持的优先级：{priority}", status_code=400) from exc
        else:
            priority_enum = Priority.MEDIUM

        description_text = str(description or title or "").strip()
        final_title = str(title or description_text or f"{task_type_label(task_type_enum.value)}任务").strip()
        final_title = final_title[:240]
        criteria = [item.strip() for item in (acceptance_criteria or []) if item and item.strip()]
        if not criteria:
            criteria = ["由平台助手创建：完成任务目标并提交结果说明"]

        request = {
            "task_type": task_type_enum.value,
            "employee_id": employee.id if employee else None,
            "category_id": category.id,
            "title": final_title,
            "description": description_text or final_title,
            "acceptance_criteria": criteria,
            "priority": priority_enum.value,
        }
        preview = {
            "task_name": final_title,
            "task_type": task_type_enum.value,
            "task_type_label": task_type_label(task_type_enum.value),
            "employee": employee.name if employee else "自动分配",
            "employee_id": employee.id if employee else None,
            "category": category.name,
            "priority": priority_enum.value,
            "acceptance_criteria": criteria,
            "inputs": request,
        }
        return await self._create_operation(
            operation_type="task_create",
            risk_level="L2",
            user_id=user_id,
            request=request,
            preview=preview,
            conversation_id=conversation_id,
            message_id=message_id,
        )

    async def prepare_task_action(
        self,
        *,
        user_id: str,
        action: str,
        task_id: str,
        reason: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
    ) -> AssistantOperation:
        task = await self.session.get(Task, task_id)
        if not task:
            raise OperationError(f"任务不存在：{task_id}", status_code=404)
        action = str(action or "").lower()
        if action not in {"cancel", "retry"}:
            raise OperationError(f"不支持的操作：{action}", status_code=400)

        status = TaskStatus(task.status)
        if action == "cancel":
            if status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED}:
                raise OperationError(f"任务已处于终态 {status.value}，无需取消")
            risk = "L3" if status is TaskStatus.RUNNING else "L2"
            operation_type = "task_cancel"
            note = reason or "平台助手取消任务"
        elif action == "retry":
            if status not in {TaskStatus.FAILED}:
                raise OperationError(f"只有失败任务可以重新执行，当前状态 {status.value}")
            risk = "L2"
            operation_type = "task_retry"
            note = reason or "平台助手重新执行任务（新 OpenCode Session）"

        request = {"task_id": task.id, "action": action, "reason": note}
        preview = {
            "task_id": task.id,
            "task_name": task.title,
            "current_status": status.value,
            "employee": task.employee_id or "未分配",
            "reason": note,
            "inputs": request,
        }
        return await self._create_operation(
            operation_type=operation_type,
            risk_level=risk,
            user_id=user_id,
            request=request,
            preview=preview,
            conversation_id=conversation_id,
            message_id=message_id,
        )

    async def prepare_task_reassign(
        self,
        *,
        user_id: str,
        task_id: str,
        employee_id: str,
        reason: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
    ) -> AssistantOperation:
        task = await self.session.get(Task, task_id)
        if not task:
            raise OperationError(f"任务不存在：{task_id}", status_code=404)
        status = TaskStatus(task.status)
        if status not in {TaskStatus.PENDING}:
            raise OperationError(
                f"只有待领取或已挂起的任务可以重新分配，当前状态 {status.value}"
            )
        employee = await self._require_eligible_employee(task, employee_id)
        if task.employee_id == employee.id:
            raise OperationError(f"任务已分配给 {employee.name}，无需重复分配")

        note = reason or "平台助手重新分配任务"
        request = {
            "task_id": task.id,
            "action": "reassign",
            "employee_id": employee.id,
            "previous_employee_id": task.employee_id,
            "reason": note,
        }
        preview = {
            "task_id": task.id,
            "task_name": task.title,
            "current_status": status.value,
            "previous_employee_id": task.employee_id,
            "new_employee": employee.name,
            "new_employee_id": employee.id,
            "reason": note,
            "inputs": request,
        }
        return await self._create_operation(
            operation_type="task_reassign",
            risk_level="L2",
            user_id=user_id,
            request=request,
            preview=preview,
            conversation_id=conversation_id,
            message_id=message_id,
        )

    async def _require_eligible_employee(
        self, task: Task, employee_id: str
    ) -> AgentWorker:
        employee = await self.session.get(AgentWorker, employee_id)
        if not employee:
            raise OperationError(f"Agent Worker 不存在：{employee_id}", status_code=404)
        if not employee.enabled:
            raise OperationError(f"Agent Worker {employee.name} 已停用")
        if task.task_type not in employee.task_types:
            raise OperationError(
                f"Agent Worker {employee.name} 不支持任务类型 {task_type_label(task.task_type)}"
            )
        if task.category_id and employee.category_ids and task.category_id not in employee.category_ids:
            raise OperationError(f"Agent Worker {employee.name} 不支持该任务分类")
        return employee

    async def confirm(self, operation_id: str, user_id: str) -> dict[str, Any]:
        operation = await self.get(operation_id)
        if not operation:
            raise OperationError("操作不存在", status_code=404)
        if operation.user_id != user_id:
            raise OperationError("无权确认其他用户发起的操作", status_code=403)
        if operation.status == "SUCCESS":
            return operation.result_json
        if operation.status != "WAITING_CONFIRM":
            raise OperationError(f"操作当前状态为 {operation.status}，无法确认")
        if operation.expires_at and operation.expires_at < datetime.now(UTC):
            operation.status = "EXPIRED"
            await self.session.commit()
            raise OperationError("操作已过期，请重新发起")
        operation.confirmed_at = datetime.now(UTC)
        operation.status = "EXECUTING"
        await self.session.commit()
        try:
            result = await self._execute(operation)
        except Exception as exc:  # 业务异常统一转为操作失败，不让栈溢出到前端
            detail = getattr(exc, "detail", None) or str(exc)
            # 业务方法可能在失败前留下未提交状态；先回滚，再把 Operation
            # 从 EXECUTING 收敛到 FAILED，避免确认卡永久卡死。
            await self.session.rollback()
            operation = await self.get(operation_id)
            assert operation is not None
            operation.status = "FAILED"
            operation.result_json = {"error": str(detail)}
            operation.executed_at = datetime.now(UTC)
            await self.session.commit()
            status_code = exc.status_code if isinstance(exc, OperationError) else 409
            raise OperationError(f"操作执行失败：{detail}", status_code=status_code) from exc
        operation = await self.get(operation_id)
        assert operation is not None
        operation.status = "SUCCESS"
        operation.result_json = result
        operation.executed_at = datetime.now(UTC)
        await self.session.commit()
        return result

    async def cancel(self, operation_id: str, user_id: str) -> AssistantOperation:
        operation = await self.get(operation_id)
        if not operation:
            raise OperationError("操作不存在", status_code=404)
        if operation.user_id != user_id:
            raise OperationError("无权取消其他用户发起的操作", status_code=403)
        if operation.status in {"WAITING_CONFIRM", "PREPARED"}:
            operation.status = "CANCELLED"
            await self.session.commit()
        elif operation.status != "CANCELLED":
            raise OperationError(f"操作当前状态为 {operation.status}，无法取消")
        return operation

    async def _execute(self, operation: AssistantOperation) -> dict[str, Any]:
        # 延迟导入避免模块循环：app.api.routes 依赖大量运行时模块。
        from app.runtime.lifecycle import RuntimeService

        request = operation.request_json or {}
        if operation.operation_type == "task_create":
            payload = TaskCreate(
                source="assistant",
                task_type=TaskType(request["task_type"]),
                category_id=request["category_id"],
                title=request["title"],
                description=request.get("description") or request["title"],
                acceptance_criteria=[
                    AcceptanceCriterion(content=item)
                    for item in request.get("acceptance_criteria") or ["完成任务目标"]
                ],
                employee_id=request.get("employee_id"),
                priority=Priority(request.get("priority") or "MEDIUM"),
            )
            task = await TaskService(self.session).create(payload)
            return {
                "task_id": task.id,
                "status": task.status,
                "title": task.title,
                "employee_id": task.employee_id,
            }

        task_id = str(request.get("task_id") or "")
        task = await self.session.get(Task, task_id)
        if not task:
            raise OperationError(f"任务不存在：{task_id}", status_code=404)
        if operation.operation_type == "task_cancel":
            result = await RuntimeService(self.session).stop(task_id)
        elif operation.operation_type == "task_retry":
            result = await RuntimeService(self.session).start(task_id, restart=True)
        elif operation.operation_type == "task_reassign":
            status = TaskStatus(task.status)
            if status not in {TaskStatus.PENDING}:
                raise OperationError(
                    f"任务状态已变为 {status.value}，请重新发起重分配"
                )
            employee = await self._require_eligible_employee(
                task, str(request.get("employee_id") or "")
            )
            if task.current_session_id or task.pending_message_id:
                raise OperationError("已绑定 Session 的任务不能重新分配员工")
            previous_employee_id = task.employee_id
            if previous_employee_id == employee.id:
                raise OperationError(f"任务已分配给 {employee.name}")
            task.employee_id = employee.id
            task.claimed_at = None
            task.current_session_id = None
            task.execution_environment_snapshot = None
            await TaskService(self.session).add_event(
                task,
                "TASK_REASSIGNED",
                "work",
                f"任务已重新分配给 {employee.name}",
                {
                    "from_employee_id": previous_employee_id,
                    "to_employee_id": employee.id,
                    "reason": request.get("reason"),
                    "by": operation.user_id,
                    "operation_id": operation.operation_id,
                },
            )
            result = task
        else:
            raise OperationError(f"不支持的操作类型：{operation.operation_type}")
        return {
            "task_id": result.id,
            "status": result.status,
            "title": result.title,
            "employee_id": result.employee_id,
        }

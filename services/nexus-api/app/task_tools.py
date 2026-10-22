"""Task MCP 调用的任务操作。状态变化只走 TaskService 和 RuntimeService。"""

from __future__ import annotations

import hashlib
from contextvars import ContextVar
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import Priority, TaskStatus, TaskType
from app.executors.errors import OpenCodeExecutionTimeout, OpenCodeServerUnavailable
from app.models import AgentWorker, Event, Task, TaskCategory
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.schemas import AcceptanceCriterion, TaskCreate
from app.services import TaskService
from app.state_machine import InvalidTransition

nexus_employee_id: ContextVar[str] = ContextVar("nexus_employee_id", default="")
nexus_session_id: ContextVar[str] = ContextVar("nexus_session_id", default="")
nexus_conversation_id: ContextVar[str] = ContextVar("nexus_conversation_id", default="")
nexus_discussion_id: ContextVar[str] = ContextVar("nexus_discussion_id", default="")
nexus_actor_id: ContextVar[str] = ContextVar("nexus_actor_id", default="")
nexus_channel_id: ContextVar[str] = ContextVar("nexus_channel_id", default="")

_CONCLUDED = {TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value, TaskStatus.CANCELED.value}


def _text(value: str | None) -> str:
    return (value or "").strip()


_CONFLICT_MARKERS = ("端口冲突", "另一员工", "归属冲突", "workspace 与员工", "启动 workspace")
_AUTH_TYPES = {"AuthenticationException", "BadAuthenticationType", "AuthenticationFailed"}
_AUTH_MARKERS = ("authentication failed", "authentication exception", "publickey", "password=", "passwd=")
_UNREACHABLE_TYPES = {"ConnectError", "ConnectTimeout", "NetworkError", "ReadError", "WriteError"}


def classify_runtime_error(exc: Exception) -> dict[str, str]:
    """把执行环境异常收成固定原因。不回传原始文本，避免带上主机、口令或路径。"""

    if isinstance(exc, (TimeoutError, OpenCodeExecutionTimeout)):
        return {
            "failure_code": "EXECUTION_TIMEOUT",
            "error": "执行超时",
            "hint": "请稍后重试；若反复超时，检查 Worker 的 Serve 是否卡住",
        }
    text = f"{type(exc).__name__} {exc}".lower()
    if any(marker in text for marker in _CONFLICT_MARKERS) or "另一员工或工作空间" in str(exc):
        return {
            "failure_code": "WORKSPACE_CONFLICT",
            "error": "Workspace 或端口归属冲突",
            "hint": "检查该员工的端口是否被其他员工或工作区占用",
        }
    if type(exc).__name__ in _AUTH_TYPES or any(marker in text for marker in _AUTH_MARKERS):
        return {
            "failure_code": "SSH_AUTH_FAILED",
            "error": "SSH 认证失败",
            "hint": "检查远程账号配置，不要在对话里重复发送密码",
        }
    if isinstance(exc, PermissionError):
        return {
            "failure_code": "PERMISSION_DENIED",
            "error": "没有执行该操作的权限",
            "hint": "确认当前责任员工是否仍拥有该任务",
        }
    if (
        isinstance(exc, (OpenCodeServerUnavailable, ConnectionError, OSError))
        or type(exc).__name__ in _UNREACHABLE_TYPES
        or any(marker in text for marker in ("unreachable", "connection refused", "connect error", "不可达"))
    ):
        return {
            "failure_code": "ENVIRONMENT_UNREACHABLE",
            "error": "执行环境不可达",
            "hint": "检查 Worker 网络和 Serve 是否在运行",
        }
    return {
        "failure_code": "ENVIRONMENT_UNAVAILABLE",
        "error": "执行环境暂时不可用",
        "hint": "稍后重试；若仍失败，查看该 Worker 的运行环境",
    }


class TaskTools:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        *,
        task_type: str,
        category_id: str,
        title: str,
        description: str,
        acceptance_criteria: list[str] | None = None,
        priority: str = "MEDIUM",
        worker_id: str | None = None,
        responsible_employee_id: str | None = None,
        source_session_id: str | None = None,
        source_conversation_id: str | None = None,
        source_channel_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if nexus_channel_id.get() and any(
            (
                source_channel_id and source_channel_id != nexus_channel_id.get(),
                source_session_id and source_session_id != nexus_session_id.get(),
                source_conversation_id,
            )
        ):
            return {"ok": False, "error": "不能覆盖服务端群调用来源"}
        category = await self.session.get(TaskCategory, category_id)
        if not category or not category.enabled:
            return {"ok": False, "error": "请选择已启用的任务分类"}
        try:
            kind = TaskType(task_type)
            level = Priority(priority)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        actor = _text(nexus_employee_id.get())
        if not actor:
            return {"ok": False, "error": "未识别当前责任员工"}
        requested_owner = _text(responsible_employee_id)
        if requested_owner and requested_owner != actor:
            return {"ok": False, "error": "不能代替其他责任员工创建任务"}
        employee = actor
        session_id = _text(source_session_id) or _text(nexus_session_id.get())
        conversation_id = _text(source_conversation_id) or _text(nexus_conversation_id.get())
        channel_id = _text(source_channel_id) or _text(nexus_channel_id.get())
        key = _text(idempotency_key) or _derived_key(session_id, kind.value, category_id, title, description)
        assignee = _text(worker_id) or None
        if assignee:
            refusal = await self._worker_refusal(assignee, kind.value, category_id)
            if refusal:
                return {"ok": False, "error": refusal}
        try:
            payload = TaskCreate(
                source="nexus-employee" if employee else "manual",
                task_type=kind,
                category_id=category_id,
                title=title.strip(),
                description=description.strip(),
                acceptance_criteria=[AcceptanceCriterion(content=item) for item in acceptance_criteria or []],
                employee_id=assignee,
                responsible_employee_id=employee or None,
                source_session_id=session_id or None,
                source_conversation_id=conversation_id or None,
                source_channel_id=channel_id or None,
                idempotency_key=key,
                priority=level,
            )
        except ValidationError as exc:
            return {"ok": False, "error": str(exc)}
        task = await TaskService(self.session).create(payload)
        if nexus_actor_id.get():
            self.session.add(
                Event(
                    task_id=task.id,
                    event_type="group_task_requested",
                    phase="audit",
                    message="用户通过数字员工发起任务",
                    detail={
                        "initiator_id": nexus_actor_id.get(),
                        "employee_id": actor,
                        "source_channel_id": channel_id,
                        "source_session_id": session_id,
                    },
                )
            )
            await self.session.commit()
        return self._ok(task)

    async def get(self, task_id: str) -> dict[str, Any]:
        task = await self._owned(task_id)
        if isinstance(task, dict):
            return task
        return self._ok(task)

    async def list(
        self,
        *,
        responsible_employee_id: str | None = None,
        worker_id: str | None = None,
        source_channel_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        actor = _text(nexus_employee_id.get())
        if not actor:
            return {"ok": False, "error": "未识别当前责任员工", "tasks": []}
        requested = _text(responsible_employee_id)
        if requested and requested != actor:
            return {"ok": False, "error": "不能查询其他责任员工的任务", "tasks": []}
        query = select(Task).where(Task.responsible_employee_id == actor)
        if _text(worker_id):
            query = query.where(Task.employee_id == _text(worker_id))
        if nexus_channel_id.get():
            query = query.where(Task.source_channel_id == nexus_channel_id.get())
        elif nexus_conversation_id.get():
            query = query.where(Task.source_conversation_id == nexus_conversation_id.get())
        elif _text(source_channel_id):
            query = query.where(Task.source_channel_id == _text(source_channel_id))
        if status:
            query = query.where(Task.status == status)
        rows = (
            await self.session.scalars(query.order_by(Task.created_at.desc()).limit(max(1, min(limit, 50))))
        ).all()
        return {"ok": True, "tasks": [self._brief(item) for item in rows]}

    async def update(
        self,
        task_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        acceptance_criteria: list[str] | None = None,
    ) -> dict[str, Any]:
        task = await self._owned(task_id)
        if isinstance(task, dict):
            return task
        if task.status in _CONCLUDED:
            return self._refuse(task, "已结束的任务不能修改")
        if title:
            task.title = title.strip()
        if description:
            task.description = description.strip()
        if acceptance_criteria is not None:
            task.acceptance_criteria = [
                {"id": f"AC-{index:02d}", "content": item}
                for index, item in enumerate(acceptance_criteria, start=1)
            ]
        await self.session.commit()
        await self.session.refresh(task)
        return self._ok(task)

    async def assign(self, task_id: str, worker_id: str) -> dict[str, Any]:
        task = await self._owned(task_id)
        if isinstance(task, dict):
            return task
        if task.status != TaskStatus.PENDING.value:
            return self._refuse(task, "只有待领取任务可以指定 Worker")
        refusal = await self._worker_refusal(worker_id, task.task_type, task.category_id or "")
        if refusal:
            return self._refuse(task, refusal)
        task.employee_id = worker_id
        await TaskService(self.session).add_event(
            task, "TASK_ASSIGNED", "work", "已指定 Worker", {"worker_id": worker_id}
        )
        await self.session.refresh(task)
        return self._ok(task)

    async def start(self, task_id: str) -> dict[str, Any]:
        task = await self._owned(task_id)
        if isinstance(task, dict):
            return task
        if task.status != TaskStatus.PENDING.value:
            return self._refuse(task, "只有待领取任务可以开始")
        return await self._runtime(task, lambda: RuntimeService(self.session).start(task.id))

    async def pause(self, task_id: str) -> dict[str, Any]:
        task = await self._owned(task_id)
        if isinstance(task, dict):
            return task
        if task.status != TaskStatus.RUNNING.value:
            return self._refuse(task, "只有执行中的任务可以暂停")
        return await self._runtime(
            task, lambda: RuntimeService(self.session).stop(task.id, cancel_task=False)
        )

    async def resume(self, task_id: str) -> dict[str, Any]:
        task = await self._owned(task_id)
        if isinstance(task, dict):
            return task
        if task.status != TaskStatus.WAITING.value:
            return self._refuse(task, "只有等待中的任务可以恢复")
        restart = bool(task.current_session_id)
        return await self._runtime(task, lambda: RuntimeService(self.session).start(task.id, restart=restart))

    async def terminate(self, task_id: str) -> dict[str, Any]:
        task = await self._owned(task_id)
        if isinstance(task, dict):
            return task
        if task.status in _CONCLUDED:
            return self._refuse(task, "任务已结束")
        return await self._runtime(task, lambda: RuntimeService(self.session).stop(task.id, cancel_task=True))

    async def retry(self, task_id: str) -> dict[str, Any]:
        task = await self._owned(task_id)
        if isinstance(task, dict):
            return task
        if task.status not in {TaskStatus.FAILED.value, TaskStatus.CANCELED.value}:
            return self._refuse(task, "只有失败或已终止的任务可以重试")
        return await self._runtime(task, lambda: RuntimeService(self.session).start(task.id, restart=True))

    async def logs(self, task_id: str) -> dict[str, Any]:
        task = await self._owned(task_id)
        if isinstance(task, dict):
            return task
        rows = (
            await self.session.scalars(
                select(Event).where(Event.task_id == task.id).order_by(Event.id.desc()).limit(50)
            )
        ).all()
        return {
            "ok": True,
            "task_id": task.id,
            "status": task.status,
            "events": [
                {"id": item.id, "event_type": item.event_type, "message": item.message, "detail": item.detail}
                for item in rows
            ],
        }

    async def result(self, task_id: str) -> dict[str, Any]:
        task = await self._owned(task_id)
        if isinstance(task, dict):
            return task
        return {
            "ok": True,
            "task_id": task.id,
            "status": task.status,
            "current_progress": task.current_progress,
            "failure_reason": task.failure_reason,
            "workspace": task.workspace,
            "current_session_id": task.current_session_id,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        }

    async def intervene(self, task_id: str, reason: str) -> dict[str, Any]:
        task = await self._owned(task_id)
        if isinstance(task, dict):
            return task
        if task.status in _CONCLUDED:
            return self._refuse(task, "已结束的任务不能请求人工处理")
        note = {"reason": reason.strip(), "source": "nexus-task"}
        if task.status == TaskStatus.RUNNING.value:
            outcome = await self._runtime(
                task, lambda: RuntimeService(self.session).stop(task.id, cancel_task=False)
            )
            if not outcome["ok"]:
                return outcome
            task = await self.session.get(Task, task_id)
        elif task.status == TaskStatus.PENDING.value:
            await TaskService(self.session).transition(task, TaskStatus.WAITING, message="等待人工处理")
            task = await self.session.get(Task, task_id)
        task.human_intervention = note
        await self.session.commit()
        await self.session.refresh(task)
        return self._ok(task)

    async def confirm(self, task_id: str) -> dict[str, Any]:
        task = await self._owned(task_id)
        if isinstance(task, dict):
            return task
        if task.status == TaskStatus.SUCCEEDED.value:
            return self._ok(task)
        if task.status in {TaskStatus.CANCELED.value, TaskStatus.FAILED.value, TaskStatus.PENDING.value}:
            return self._refuse(task, "当前状态不能确认完成")
        return await self._runtime(task, lambda: RuntimeService(self.session).confirm_completion(task.id))

    async def _runtime(self, task: Task, call) -> dict[str, Any]:
        task_id = task.id
        try:
            updated = await call()
        except (RuntimeStateError, InvalidTransition, ValueError) as exc:
            return await self._runtime_failure(task_id, str(exc))
        except Exception as exc:
            failure = classify_runtime_error(exc)
            return await self._runtime_failure(
                task_id,
                failure["error"],
                failure_code=failure["failure_code"],
                hint=failure["hint"],
            )
        return self._ok(updated)

    async def _runtime_failure(
        self,
        task_id: str,
        reason: str,
        *,
        failure_code: str | None = None,
        hint: str | None = None,
    ) -> dict[str, Any]:
        await self.session.rollback()
        current = await self.session.get(Task, task_id)
        if current is None:
            body = {"ok": False, "error": reason, "task_id": task_id}
            if failure_code:
                body["failure_code"] = failure_code
            if hint:
                body["hint"] = hint
            return body
        return self._refuse(current, reason, failure_code=failure_code, hint=hint)

    async def _owned(self, task_id: str) -> Task | dict[str, Any]:
        actor = _text(nexus_employee_id.get())
        if not actor:
            return {"ok": False, "error": "未识别当前责任员工"}
        task = await self.session.get(Task, task_id)
        if task is None or (task.responsible_employee_id or "") != actor:
            return {"ok": False, "error": "任务不存在"}
        if nexus_channel_id.get() and task.source_channel_id != nexus_channel_id.get():
            return {"ok": False, "error": "任务不存在"}
        if nexus_conversation_id.get() and task.source_conversation_id != nexus_conversation_id.get():
            return {"ok": False, "error": "任务不存在"}
        return task

    async def _worker_refusal(self, worker_id: str, task_type: str, category_id: str) -> str | None:
        worker = await self.session.get(AgentWorker, worker_id)
        if not worker or not worker.enabled:
            return "指定 Worker 不存在或未启用"
        if task_type not in (worker.task_types or []):
            return "指定 Worker 不支持该任务类型"
        if category_id and category_id not in (worker.category_ids or []):
            return "指定 Worker 不支持该任务分类"
        return None

    def _ok(self, task: Task) -> dict[str, Any]:
        body = self._brief(task)
        body["ok"] = True
        body["description"] = task.description
        body["acceptance_criteria"] = task.acceptance_criteria
        return body

    def _refuse(
        self,
        task: Task,
        error: str,
        *,
        failure_code: str | None = None,
        hint: str | None = None,
    ) -> dict[str, Any]:
        body = {"ok": False, "error": error, "task_id": task.id, "status": task.status}
        if failure_code:
            body["failure_code"] = failure_code
        if hint:
            body["hint"] = hint
        return body

    def _brief(self, task: Task) -> dict[str, Any]:
        return {
            "task_id": task.id,
            "title": task.title,
            "status": task.status,
            "task_type": task.task_type,
            "category_id": task.category_id,
            "worker_id": task.employee_id,
            "responsible_employee_id": task.responsible_employee_id,
            "source_session_id": task.source_session_id,
            "source_channel_id": task.source_channel_id,
            "current_progress": task.current_progress,
        }


def _derived_key(
    session_id: str,
    task_type: str,
    category_id: str,
    title: str,
    description: str,
) -> str | None:
    if not session_id:
        return None
    raw = f"{session_id}\n{task_type}\n{category_id}\n{title.strip()}\n{description.strip()}"
    return f"{session_id}:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"

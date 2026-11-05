"""Task MCP 调用的任务操作。状态变化只走 TaskService 和 RuntimeService。"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import Priority, TaskStatus, TaskType
from app.executors.errors import OpenCodeExecutionTimeout, OpenCodeServerUnavailable
from app.models import (
    AgentWorker,
    Event,
    ExperienceReviewJob,
    KnowledgeItem,
    Task,
    TaskCategory,
)
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.schemas import AcceptanceCriterion, TaskCreate
from app.services import TaskService
from app.services import acceptance_criteria as normalize_criteria
from app.state_machine import InvalidTransition
from nexus_api.db_models import employees

_CONCLUDED = {TaskStatus.SUCCEEDED.value, TaskStatus.CANCELED.value}


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
        agent_worker_id: str | None = None,
        origin_employee_id: str | None = None,
        source: str = "opencode",
        external_task_id: str | None = None,
    ) -> dict[str, Any]:
        category = await self.session.get(TaskCategory, category_id)
        if not category or not category.enabled:
            return {"ok": False, "error": "请选择已启用的任务分类"}
        try:
            kind = TaskType(task_type)
            level = Priority(priority)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        employee = _text(origin_employee_id)
        exists = await self.session.scalar(select(employees.c.id).where(employees.c.id == employee))
        if not exists:
            return {"ok": False, "error": "员工不存在"}
        assignee = _text(agent_worker_id) or None
        if assignee:
            refusal = await self._worker_refusal(assignee, kind.value, category_id)
            if refusal:
                return {"ok": False, "error": refusal}
        try:
            payload = TaskCreate(
                source=source,
                external_task_id=external_task_id,
                task_type=kind,
                category_id=category_id,
                title=title.strip(),
                description=description.strip(),
                acceptance_criteria=[AcceptanceCriterion(content=item) for item in acceptance_criteria or []],
                agent_worker_id=assignee,
                origin_employee_id=employee or None,
                priority=level,
            )
        except ValidationError as exc:
            return {"ok": False, "error": str(exc)}
        task = await TaskService(self.session).create(payload)
        return self._ok(task)

    async def categories(self) -> dict[str, Any]:
        rows = (
            await self.session.scalars(
                select(TaskCategory)
                .where(TaskCategory.enabled.is_(True))
                .order_by(TaskCategory.sort_order, TaskCategory.id)
                .limit(200)
            )
        ).all()
        return {
            "ok": True,
            "categories": [
                {"id": row.id, "name": row.name, "description": row.description, "enabled": row.enabled}
                for row in rows
            ],
        }

    async def get(self, task_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        return self._ok(task)

    async def list(
        self,
        *,
        origin_employee_id: str | None = None,
        agent_worker_id: str | None = None,
        external_task_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        query = select(Task)
        if _text(origin_employee_id):
            query = query.where(Task.origin_employee_id == _text(origin_employee_id))
        if _text(agent_worker_id):
            query = query.where(Task.agent_worker_id == _text(agent_worker_id))
        if _text(external_task_id):
            query = query.where(Task.external_task_id == _text(external_task_id))
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
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        if task.status in _CONCLUDED:
            return self._refuse(task, "已结束的任务不能修改")
        if title:
            task.title = title.strip()
        if description:
            task.description = description.strip()
        if acceptance_criteria is not None:
            by_content = {}
            for item in task.acceptance_criteria:
                by_content.setdefault(item["content"], []).append(item.get("id"))
            items = []
            for content in acceptance_criteria:
                retained = by_content.get(content)
                items.append({"id": retained.pop(0) if retained else None, "content": content})
            task.acceptance_criteria = normalize_criteria(items)
        await self.session.commit()
        await self.session.refresh(task)
        return self._ok(task)

    async def assign(self, task_id: str, agent_worker_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        if task.status != TaskStatus.PENDING.value:
            return self._refuse(task, "只有待领取任务可以指定 Worker")
        refusal = await self._worker_refusal(agent_worker_id, task.task_type, task.category_id or "")
        if refusal:
            return self._refuse(task, refusal)
        task.agent_worker_id = agent_worker_id
        await TaskService(self.session).add_event(
            task, "TASK_ASSIGNED", "work", "已指定 Worker", {"agent_worker_id": agent_worker_id}
        )
        await self.session.refresh(task)
        return self._ok(task)

    async def start(self, task_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        if task.status != TaskStatus.PENDING.value:
            return self._refuse(task, "只有待领取任务可以开始")
        return await self._runtime(task, lambda: RuntimeService(self.session).start(task.id))

    async def pause(self, task_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        if task.status != TaskStatus.RUNNING.value:
            return self._refuse(task, "只有执行中的任务可以暂停")
        return await self._runtime(
            task, lambda: RuntimeService(self.session).stop(task.id, cancel_task=False)
        )

    async def resume(self, task_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        return await self._runtime(task, lambda: RuntimeService(self.session).resume(task.id))

    async def terminate(self, task_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        if task.status in _CONCLUDED:
            return self._refuse(task, "任务已结束")
        return await self._runtime(task, lambda: RuntimeService(self.session).stop(task.id, cancel_task=True))

    async def retry(self, task_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        return await self._runtime(task, lambda: RuntimeService(self.session).restart(task.id))

    async def logs(self, task_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
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
            "runtime_status_label": task.runtime_status_label,
            "events": [
                {"id": item.id, "event_type": item.event_type, "message": item.message, "detail": item.detail}
                for item in rows
            ],
        }

    async def result(self, task_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        return {
            "ok": True,
            "task_id": task.id,
            "status": task.status,
            "runtime_status_label": task.runtime_status_label,
            "current_progress": task.current_progress,
            "workspace": task.workspace,
            "current_session_id": task.current_session_id,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        }

    async def summary(self, task_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        job = await self.session.scalar(
            select(ExperienceReviewJob)
            .where(ExperienceReviewJob.task_id == task.id)
            .order_by(ExperienceReviewJob.confirmed_at.desc(), ExperienceReviewJob.id.desc())
            .limit(1)
        )
        candidates = []
        if job and job.candidate_ids:
            knowledge = (
                await self.session.scalars(
                    select(KnowledgeItem)
                    .where(KnowledgeItem.id.in_(job.candidate_ids), KnowledgeItem.source_task_id == task.id)
                    .limit(20)
                )
            ).all()
            candidates = [
                {
                    "id": item.id,
                    "title": item.title,
                    "status": item.status,
                    "verification_status": item.verification_status,
                    "summary": item.summary[:4000],
                    "url": f"/library?knowledge={item.id}",
                }
                for item in knowledge
            ]
        return {
            "ok": True,
            "task_id": task.id,
            "status": task.status,
            "runtime_status_label": task.runtime_status_label,
            "current_progress": task.current_progress,
            "task_url": f"/tasks/{task.id}",
            "review": None
            if job is None
            else {
                "id": job.id,
                "status": job.status,
                "phase": job.phase,
                "zero_reason": job.zero_reason,
                "candidate_count": len(candidates),
            },
            "candidates": candidates,
            "hint": "尚未生成复盘作业；任务验收确认后由后台处理"
            if job is None
            else "复盘与候选审核是独立状态；候选未发布前不能当作正式经验",
        }

    async def intervene(self, task_id: str, reason: str) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        if task.status in _CONCLUDED:
            return self._refuse(task, "已结束的任务不能请求人工处理")
        if task.status == TaskStatus.RUNNING.value:
            outcome = await self._runtime(
                task, lambda: RuntimeService(self.session).stop(task.id, cancel_task=False)
            )
            if not outcome["ok"]:
                return outcome
            task = await self.session.get(Task, task_id)
        elif task.status == TaskStatus.PENDING.value:
            return self._refuse(task, "任务尚未开始")
        task.current_progress = reason.strip()
        task.waiting_reason = "intervention"
        await self.session.commit()
        await self.session.refresh(task)
        return self._ok(task)

    def _assistance(self):
        from app.runtime.assistance import WorkerAssistance

        return WorkerAssistance(RuntimeService(self.session))

    async def assistance(self, task_id: str, cursor: int = 0) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        try:
            return await self._assistance().read(task_id, cursor=cursor)
        except (RuntimeStateError, ValueError) as exc:
            return await self._runtime_failure(task_id, str(exc))
        except Exception as exc:
            return await self._runtime_failure(task_id, **self._assistance_error(exc))

    async def reply(
        self,
        task_id: str,
        expected_session_id: str,
        request_id: str,
        kind: str,
        answers: list[list[str]] | None = None,
        decision: str | None = None,
    ) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        if not expected_session_id or not request_id:
            return self._refuse(task, "必须提供查询得到的执行会话和请求标识")
        try:
            return await self._assistance().reply(
                task_id,
                expected_session_id=expected_session_id,
                request_id=request_id,
                kind=kind,
                answers=answers,
                decision=decision,
            )
        except (RuntimeStateError, ValueError) as exc:
            return await self._runtime_failure(task_id, str(exc))
        except Exception as exc:
            return await self._runtime_failure(task_id, **self._assistance_error(exc))

    async def input(
        self,
        task_id: str,
        expected_session_id: str,
        expected_submission_id: str,
        text: str,
    ) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
        if not expected_session_id or not expected_submission_id:
            return self._refuse(task, "必须提供查询得到的执行会话和回合标识")
        return await self._runtime(
            task,
            lambda: self._assistance().input(
                task_id,
                expected_session_id=expected_session_id,
                expected_submission_id=expected_submission_id,
                text=text,
            ),
        )

    @staticmethod
    def _assistance_error(exc):
        failure = classify_runtime_error(exc)
        return {"reason": failure["error"], "failure_code": failure["failure_code"], "hint": failure["hint"]}

    async def confirm(self, task_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
        if isinstance(task, dict):
            return task
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
        # Mutations return the Task outcome; query get/assistance for Session facts.
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

    async def _task(self, task_id: str) -> Task | dict[str, Any]:
        task = await self.session.get(Task, task_id)
        if task is None:
            return {"ok": False, "error": "任务不存在"}
        return task

    async def _worker_refusal(self, agent_worker_id: str, task_type: str, category_id: str) -> str | None:
        worker = await self.session.get(AgentWorker, agent_worker_id)
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
        body = {
            "ok": False,
            "error": error,
            "task_id": task.id,
            "status": task.status,
            "runtime_status_label": task.runtime_status_label,
        }
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
            "runtime_status_label": task.runtime_status_label,
            "task_type": task.task_type,
            "category_id": task.category_id,
            "agent_worker_id": task.agent_worker_id,
            "origin_employee_id": task.origin_employee_id,
            "source": task.source,
            "external_task_id": task.external_task_id,
            "current_progress": task.current_progress,
            "current_session_id": task.current_session_id,
            "waiting_reason": task.waiting_reason,
        }

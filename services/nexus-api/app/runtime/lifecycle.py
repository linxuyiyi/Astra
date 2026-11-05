"""Task 的业务生命周期。Session、消息及执行状态由 OpenCode 原生接口提供。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.config.settings import get_settings
from app.executors.errors import OpenCodeExecutionError, OpenCodeSessionNotFound
from app.executors.opencode_prompt import (
    bind_task_prompt_context,
    build_task_prompt,
    resolve_task_prompt_context,
)
from app.models import AgentWorker, Task, TaskCategory, WorkerMaintenanceOperation
from app.runtime.occupancy import occupying_clause, sync_worker_occupancy
from app.runtime.opencode_model import gateway_model
from app.runtime.session_connection import connect, prepare_workspace, resolve_environment, session_tree
from app.runtime.wake_outbox import record_assistance_requests
from app.runtime.workspace_files import materialize_task_attachments
from app.services import TaskService


class RuntimeStateError(ValueError):
    pass


def message_id():
    # OpenCode identifiers are lexically ordered by creation time.
    return f"msg_{int(datetime.now(UTC).timestamp() * 1000):013x}{uuid4().hex[:12]}"


class RuntimeService:
    def __init__(self, session):
        self.session = session

    async def lock_task(self, task_id):
        task = await self.session.scalar(
            select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True)
        )
        if task is None:
            raise RuntimeStateError("任务不存在")
        return task

    async def project(self, task, status, *, detail=None):
        await TaskService(self.session).transition(task, status, current_progress=detail, commit=False)
        await sync_worker_occupancy(self.session, task.agent_worker_id)
        await self.session.commit()
        return task

    async def _worker(self, task):
        worker = await self.session.scalar(
            select(AgentWorker)
            .where(AgentWorker.id == task.agent_worker_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            not worker
            or (not worker.enabled and task.status == "PENDING")
            or worker.status in {"OFFLINE", "PAUSED"}
        ):
            raise RuntimeStateError("Worker 未启用、离线或已暂停")
        occupied = await self.session.scalar(
            select(Task.id).where(occupying_clause(worker.id), Task.id != task.id).limit(1).with_for_update()
        )
        if occupied:
            raise RuntimeStateError("Worker 正在处理其他任务")
        maintenance = await self.session.scalar(
            select(WorkerMaintenanceOperation.id)
            .where(WorkerMaintenanceOperation.active_worker_id == worker.id)
            .limit(1)
            .with_for_update()
        )
        if maintenance:
            raise RuntimeStateError("Worker 正在维护")
        return worker

    async def start(self, task_id, *, restart=False, expected_session_id=None):
        task = await self.lock_task(task_id)
        if expected_session_id and task.current_session_id != expected_session_id:
            raise RuntimeStateError("Session 已切换")
        if task.pending_message_id:
            raise RuntimeStateError("已有待确认输入，请等待自动重发")
        if task.current_session_id and not restart:
            raise RuntimeStateError("任务已有 Session，请继续当前会话")
        worker = await self._worker(task)
        previous_session = task.current_session_id
        task.claimed_at = task.claimed_at or datetime.now(UTC)
        worker.status = "WORKING"
        await self.project(task, "RUNNING", detail="准备工作区")
        task = await self.lock_task(task_id)
        if task.pending_message_id or task.current_session_id != previous_session:
            await self.session.commit()
            return task
        try:
            if restart and task.current_session_id:
                async with connect(worker, start=True) as client:
                    await self._stop_native(client, Path(task.workspace), task.current_session_id)
            task.workspace = await prepare_workspace(task, worker)
            task.attachments = await materialize_task_attachments(
                worker, task.workspace, task.attachments or []
            )
            from app.runtime.workspace_changes import workspace_head_commit

            base_commit = await workspace_head_commit(worker, task.workspace)
            await TaskService(self.session).add_event(
                task,
                "WORKSPACE_PREPARED",
                "work",
                "工作区已准备",
                {"workspace": task.workspace, "base_commit": base_commit},
                commit=False,
            )
            task.current_session_id = None
            task.last_message_id = None
            category = await self.session.get(TaskCategory, task.category_id)
            bind_task_prompt_context(task, resolve_task_prompt_context(task, category))
            outbound = message_id()
            task.pending_message_id = outbound
            task.pending_prompt = {
                "action": "prompt_async",
                "body": {
                    "messageID": outbound,
                    "parts": [{"type": "text", "text": build_task_prompt(task, Path(task.workspace))}],
                },
            }
            task.waiting_reason = None
            task.claimed_at = task.claimed_at or datetime.now(UTC)
            worker.status = "WORKING"
            if restart:
                task.started_at = datetime.now(UTC)
            await self.project(task, "RUNNING", detail="正在提交任务")
            return await self.deliver_first_submission(task_id)
        except Exception as exc:
            await self.session.rollback()
            current = await self.lock_task(task_id)
            if current.status == "RUNNING" and not current.pending_message_id:
                from app.runtime.redaction import redact_text

                current.waiting_reason = "start_error"
                await self.project(current, "WAITING", detail=f"启动未完成：{redact_text(str(exc))[:500]}")
            raise

    async def send(
        self,
        task_id,
        body,
        *,
        expected_session_id=None,
        expected_submission_id=None,
        native_action="prompt_async",
    ):
        task = await self.lock_task(task_id)
        if task.status not in {"RUNNING", "WAITING"} or not task.current_session_id:
            raise RuntimeStateError("任务尚未开始或已结束")
        if task.pending_message_id:
            raise RuntimeStateError("已有待确认输入，请等待自动重发")
        if expected_session_id and task.current_session_id != expected_session_id:
            raise RuntimeStateError("Session 已切换")
        if expected_submission_id and task.last_message_id != expected_submission_id:
            raise RuntimeStateError("当前回合已变化")
        worker = await resolve_environment(self.session, task)
        async with connect(worker) as client:
            if await client.status(Path(task.workspace), task.current_session_id) in {"busy", "retry"}:
                raise RuntimeStateError("当前回复仍在执行")
        outbound = message_id()
        task.pending_message_id = outbound
        task.pending_prompt = {"action": native_action, "body": {**body, "messageID": outbound}}
        task.waiting_reason = None
        await self.project(task, "RUNNING", detail="正在发送输入")
        return await self.deliver_first_submission(task_id)

    async def _all_messages(self, client, directory, session_id):
        messages, before, seen = [], None, set()
        while True:
            page = await client.messages_page(directory, session_id, limit=200, before=before)
            if page.get("truncated"):
                raise RuntimeStateError("原生消息分页不完整")
            batch = page.get("messages") or []
            messages.extend(batch)
            before = page.get("next_before")
            if not before:
                break
            if before in seen:
                raise RuntimeStateError("原生消息游标重复")
            seen.add(before)
        return list(reversed(messages))

    async def deliver_first_submission(self, task_id):
        return await self.retry_submission(task_id)

    async def retry_submission(self, task_id):
        task = await self.lock_task(task_id)
        if not task.pending_message_id or task.status not in {"RUNNING", "WAITING"}:
            await self.session.commit()
            return task
        worker = await resolve_environment(self.session, task)
        if worker.status == "OFFLINE":
            await self.session.commit()
            return task
        pending = dict(task.pending_prompt or {})
        outbound = task.pending_message_id
        directory = Path(task.workspace)
        try:
            async with connect(worker, start=True) as client:
                if task.current_session_id:
                    try:
                        status = await client.status(directory, task.current_session_id)
                        if pending.get("request_id"):
                            from app.worker_gateway.data_channel import retire_submission

                            await retire_submission(pending["request_id"])
                            messages = await self._all_messages(client, directory, task.current_session_id)
                            if any((x.get("info") or {}).get("id") == outbound for x in messages):
                                task.last_message_id = outbound
                                task.pending_message_id = None
                                task.pending_prompt = None
                                await self.session.commit()
                                return task
                            if status in {"busy", "retry"}:
                                await self.session.commit()
                                return task
                            await self._stop_native(client, directory, task.current_session_id)
                    except OpenCodeSessionNotFound:
                        task.current_session_id = None
                if not task.current_session_id:
                    native = await client.create_session(
                        directory,
                        title=task.title,
                        model_name=gateway_model(worker.model_name or get_settings().opencode_model),
                        agent=get_settings().opencode_agent or "build",
                    )
                    task.current_session_id = native["id"]
                    from app.runtime.runtime_context import (
                        build_runtime_context_part,
                        generate_runtime_context,
                    )

                    body = dict(pending["body"])
                    body["parts"] = [
                        *[
                            part
                            for part in body.get("parts", [])
                            if not (
                                part.get("synthetic")
                                and str(part.get("text", "")).startswith("ASTRA_RUNTIME_CONTEXT: ")
                            )
                        ],
                        build_runtime_context_part(generate_runtime_context(task)),
                    ]
                    pending["body"] = body
                request_id = "submit_" + uuid4().hex
                pending["request_id"] = request_id
                task.pending_prompt = pending
                # Native pointer + pending round are durable before transmission.
                await self.session.commit()
                task = await self.lock_task(task_id)
                if (
                    task.pending_message_id != outbound
                    or task.current_session_id is None
                    or (task.pending_prompt or {}).get("request_id") != request_id
                ):
                    await self.session.rollback()
                    return task
                session_id = task.current_session_id
                await client.request(
                    "POST",
                    f"/session/{session_id}/{pending['action']}",
                    directory=directory,
                    body=pending["body"],
                    command_request_id=request_id,
                    session_id=session_id,
                    task_id=task.id,
                )
                # Row lock serializes send/stop/restart; epoch is checked by Gateway.
                if task.pending_message_id == outbound and task.current_session_id == session_id:
                    task.last_message_id = outbound
                    task.pending_message_id = None
                    task.pending_prompt = None
                    task.waiting_reason = None
                    await self.project(task, "RUNNING", detail="执行中")
                return task
        except Exception as exc:
            if type(exc) is OpenCodeExecutionError:
                from app.runtime.redaction import redact_text

                # A native 4xx response confirms rejection, so there is no unknown input to resend.
                task.pending_message_id = None
                task.pending_prompt = None
                task.waiting_reason = "input_rejected"
                return await self.project(task, "WAITING", detail=redact_text(str(exc))[:500])
            # Unknown acceptance is retried on the same Task/Session after recovery.
            task.current_progress = "提交未确认，连接恢复后自动重发"
            await TaskService(self.session).add_event(
                task,
                "SUBMISSION_UNCONFIRMED",
                "work",
                task.current_progress,
                {"message_id": outbound, "error_type": type(exc).__name__},
            )
            return task

    async def _native_busy(self, client, directory, sessions):
        for session_id in sessions:
            if await client.status(directory, session_id) in {"busy", "retry"}:
                return True
        return False

    async def _stop_native(self, client, directory, session_id):
        try:
            sessions = await session_tree(client, directory, session_id)
        except OpenCodeSessionNotFound:
            return
        # Stop the parent first so it cannot launch another child during cancellation.
        for native_id in [session_id, *sorted(sessions - {session_id})]:
            try:
                await client.abort(directory, native_id)
            except OpenCodeSessionNotFound:
                pass
        deadline = asyncio.get_running_loop().time() + get_settings().opencode_restart_abort_timeout_seconds
        while True:
            busy = False
            for native_id in sessions:
                try:
                    busy |= await client.status(directory, native_id) in {"busy", "retry"}
                except OpenCodeSessionNotFound:
                    pass
            if not busy:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeStateError("OpenCode 尚未停止，Worker 保持占用")
            await asyncio.sleep(0.1)

    async def stop(self, task_id, *, cancel_task=True):
        task = await self.lock_task(task_id)
        if task.status in {"SUCCEEDED", "CANCELED"}:
            await self.session.commit()
            return task
        if task.pending_prompt and task.pending_prompt.get("request_id"):
            from app.worker_gateway.data_channel import retire_submission

            await retire_submission(task.pending_prompt["request_id"])
        if task.current_session_id:
            worker = await resolve_environment(self.session, task)
            async with connect(worker) as client:
                await self._stop_native(client, Path(task.workspace), task.current_session_id)
        task.pending_message_id = None
        task.pending_prompt = None
        task.waiting_reason = None if cancel_task else "paused"
        return await self.project(
            task,
            "CANCELED" if cancel_task else "WAITING",
            detail="任务已取消" if cancel_task else "已停止当前回复",
        )

    async def confirm_execution_stop(self, task_id, *, operator, reason):
        task = await self.lock_task(task_id)
        if task.status not in {"RUNNING", "WAITING"}:
            raise RuntimeStateError("任务没有在途执行")
        await TaskService(self.session).add_event(
            task,
            "EXECUTION_FORCE_RELEASED",
            "work",
            "操作者明确接受强制释放后果",
            {"operator": operator, "reason": reason},
            commit=False,
        )
        if task.pending_prompt and task.pending_prompt.get("request_id"):
            from app.worker_gateway.data_channel import retire_submission

            await retire_submission(task.pending_prompt["request_id"])
        task.pending_message_id = None
        task.pending_prompt = None
        task.waiting_reason = None
        return await self.project(task, "CANCELED", detail="已由操作者确认停止并释放")

    async def refresh(self, task_id, *, client=None):
        task = await self.lock_task(task_id)
        if task.status not in {"RUNNING", "WAITING"}:
            await self.session.commit()
            return task
        if task.pending_message_id:
            await self.session.commit()
            return await self.retry_submission(task_id)
        if not task.current_session_id:
            await self.session.commit()
            return task
        worker = await resolve_environment(self.session, task)
        try:
            if client:
                return await self._refresh_native(task, client)
            async with connect(worker) as native:
                return await self._refresh_native(task, native)
        except OpenCodeSessionNotFound:
            task.waiting_reason = "session_missing"
            return await self.project(task, "WAITING", detail="原生 Session 不存在，请重新开始")
        except Exception:
            task.current_progress = "连接异常，保留原 Worker 占用"
            await self.session.commit()
            return task

    async def _refresh_native(self, task, client):
        directory = Path(task.workspace)
        allowed = await session_tree(client, directory, task.current_session_id)
        questions = [q for q in await client.questions(directory) if q.get("sessionID") in allowed]
        permissions = [p for p in await client.permissions(directory) if p.get("sessionID") in allowed]
        busy = await self._native_busy(client, directory, allowed)
        if questions or permissions:
            task.waiting_reason = "question" if questions else "permission"
            await self.project(task, "WAITING", detail="执行端需要回答或授权")
            await record_assistance_requests(self.session, task, [x["id"] for x in questions + permissions])
            await self.session.commit()
        elif busy:
            task.waiting_reason = None
            await self.project(task, "RUNNING", detail="执行中")
        elif task.waiting_reason not in {"paused", "input_rejected", "start_error"}:
            task.waiting_reason = "confirmation"
            await self.project(task, "WAITING", detail="本轮执行结束，请人工处理或确认完成")
        else:
            await self.session.commit()
        return task

    async def continue_task(self, task_id):
        task = await self.lock_task(task_id)
        if task.status not in {"RUNNING", "WAITING"} or not task.current_session_id:
            raise RuntimeStateError("任务尚未绑定可继续的 Session")
        await TaskService(self.session).add_event(
            task, "TASK_CONTINUED", "work", "继续当前 Session", {"session_id": task.current_session_id}
        )
        return task

    async def confirm_completion(self, task_id):
        task = await self.lock_task(task_id)
        if task.status == "SUCCEEDED":
            await self.session.commit()
            return task
        if task.status != "WAITING" or task.pending_message_id or not task.current_session_id:
            raise RuntimeStateError("请先结束当前执行再确认完成")
        worker = await resolve_environment(self.session, task)
        async with connect(worker) as client:
            directory = Path(task.workspace)
            allowed = await session_tree(client, directory, task.current_session_id)
            if (
                await self._native_busy(client, directory, allowed)
                or any(q.get("sessionID") in allowed for q in await client.questions(directory))
                or any(p.get("sessionID") in allowed for p in await client.permissions(directory))
            ):
                raise RuntimeStateError("仍有原生执行、提问或权限请求，不能确认完成")
        if get_settings().experience_review_enabled:
            from app.runtime.experience_review.coordinator import enqueue_in_transaction

            snapshot = {
                "task_id": task.id,
                "task": {
                    "id": task.id,
                    "title": task.title,
                    "description": task.description,
                    "task_type": task.task_type,
                    "category_id": task.category_id,
                },
                "root_session_id": task.current_session_id,
                "source_session_ids": sorted(allowed),
                "agent_worker_id": task.agent_worker_id,
                "workspace": task.workspace,
                "turn_anchor_id": task.last_message_id,
                "confirmed_at": datetime.now(UTC).isoformat(),
                "native_watermarks": {task.current_session_id: task.last_message_id or ""},
            }
            event = await TaskService(self.session).add_event(
                task,
                "TASK_COMPLETION_CONFIRMED",
                "review",
                "任务确认完成，待经验复盘",
                snapshot,
                commit=False,
            )
            await enqueue_in_transaction(self.session, task=task, event=event, snapshot=snapshot)
        task.waiting_reason = None
        return await self.project(task, "SUCCEEDED", detail="任务已人工确认完成")

    async def disconnected(self, task_id, error):
        task = await self.lock_task(task_id)
        if task.status in {"RUNNING", "WAITING"}:
            task.current_progress = "连接异常，保留原 Worker 占用"
        await self.session.commit()

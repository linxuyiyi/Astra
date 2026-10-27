"""DSH 原生操作适配；Task 状态、占用和人工确认仍调用 RuntimeService。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.config.settings import get_settings
from app.domain import RuntimeSessionStatus, SessionTerminalReason, TaskStatus
from app.executors.opencode_prompt import (
    bind_task_prompt_context,
    build_task_prompt,
    resolve_task_prompt_context,
)
from app.models import RuntimeSession, TaskCategory
from app.runtime.dsh_client import DshRuntimeClient
from app.runtime.environment import environment_snapshot
from app.runtime.runtime_context import (
    build_runtime_context_part,
    generate_runtime_context,
    hash_runtime_context,
    runtime_context_id,
)
from app.runtime.session_connection import prepare_workspace, resolve_runtime_session_environment
from app.runtime.session_state import SessionState
from app.runtime.wake_outbox import record_assistance_requests
from app.runtime.workspace_changes import workspace_head_commit


def session_state(snapshot: dict) -> SessionState:
    """仅依据持久 receipt/ACP prompt 返回值，不根据日志数或时间推断成功。"""
    sid = snapshot.get("submission_id")
    if snapshot.get("permissions"):
        return SessionState(RuntimeSessionStatus.WAITING.value, last_message_id=sid, hitl="permission")
    state = snapshot.get("state")
    if state == "running":
        return SessionState(RuntimeSessionStatus.RUNNING.value, last_message_id=sid)
    if state == "succeeded" and snapshot.get("stop_reason") == "end_turn":
        return SessionState(
            RuntimeSessionStatus.SUCCEEDED.value, SessionTerminalReason.COMPLETED.value, "end_turn", sid
        )
    if state in {"stopped", "interrupted"}:
        return SessionState(
            RuntimeSessionStatus.INTERRUPTED.value,
            SessionTerminalReason.ABORTED.value,
            snapshot.get("stop_reason"),
            sid,
        )
    return SessionState(
        RuntimeSessionStatus.UNKNOWN.value, SessionTerminalReason.TRANSPORT_UNKNOWN.value, last_message_id=sid
    )


class DshLifecycle:
    def __init__(self, runtime):
        self.runtime = runtime
        self.db = runtime.session

    def error(self, text: str):
        from app.runtime.lifecycle import RuntimeStateError

        return RuntimeStateError(text)

    async def binding(self, task):
        return await self.db.scalar(
            select(RuntimeSession)
            .where(RuntimeSession.id == task.current_session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def client(self, binding: RuntimeSession) -> DshRuntimeClient:
        environment = await resolve_runtime_session_environment(self.db, binding)
        return DshRuntimeClient(environment.id, binding)

    async def start(self, task, environment, initial_id, old_binding, *, restart=False):
        """调用方已经用既有调度/维护规则完成原子 reservation。"""
        if old_binding:
            client = await self.client(old_binding)
            old_binding.fencing_token += 1
            fence = old_binding.fencing_token
            await self.db.commit()
            result = await client.cancel(fence=fence)
            if not result.get("stop_confirmed"):
                raise self.error("旧 DSH 进程尚未确认停止")
        if old_binding and old_binding.environment.get("environment_id") != environment.id:
            raise self.error("DSH 重新开始保留原 Worker；迁移到其他 Worker 需要新建任务")
        workspace = await prepare_workspace(task, environment, reuse_existing=bool(task.workspace))
        snapshot = environment_snapshot(environment)
        base = await workspace_head_commit(environment, workspace)
        if base:
            snapshot["base_commit"] = base
        from app.runtime.workspace_files import materialize_task_attachments

        attachments = await materialize_task_attachments(environment, workspace, list(task.attachments or []))
        task = await self.runtime.lock_task(task.id)
        if task.pending_message_id != initial_id:
            raise self.error("启动期间提交绑定已变化")
        token = generate_runtime_context()
        digest = hash_runtime_context(token)
        binding = RuntimeSession(
            id=f"dsh_{uuid4().hex}",
            runtime_engine="dsh",
            task_id=task.id,
            employee_id=task.employee_id,
            workspace=workspace,
            workspace_base_commit=base,
            environment=snapshot,
            status=RuntimeSessionStatus.UNKNOWN.value,
            model_name=environment.model_name,
            fencing_token=0,
            runtime_context_hash=digest,
            runtime_context_id=runtime_context_id(digest),
        )
        self.db.add(binding)
        await self.db.flush()
        await self.runtime._supersede_session(task.current_session_id, binding.id, interrupted=True)
        task.current_session_id = binding.id
        task.workspace, task.base_commit = workspace, base
        task.attachments = attachments
        task.execution_environment_snapshot = snapshot
        task.last_message_id = initial_id
        category = await self.db.get(TaskCategory, task.category_id) if task.category_id else None
        bind_task_prompt_context(task, resolve_task_prompt_context(task, category))
        prompt = build_task_prompt(task, Path(workspace), runtime_engine="dsh")
        prompt += "\n" + build_runtime_context_part(token)["text"]
        task.pending_prompt = {"engine": "dsh", "text": prompt}
        task.runtime_error = None
        if restart:
            await self.runtime._begin_restart(task, datetime.now(UTC))
        await self.db.commit()
        client = await self.client(binding)
        prepared = await client.prepare(model=environment.model_name)
        task = await self.runtime.lock_task(task.id)
        if task.current_session_id != binding.id or task.pending_message_id != initial_id:
            raise self.error("Session 创建期间绑定已变化，请对账后继续")
        binding.native_session_id = prepared["native_session_id"]
        binding.engine_metadata = {
            key: prepared.get(key)
            for key in (
                "worker_id",
                "dsh_version",
                "acp_version",
                "profile_hash",
                "assembly_hash",
            )
        }
        await self.db.commit()
        return await self.deliver(task.id)

    async def deliver(self, task_id: str):
        task = await self.runtime.lock_task(task_id)
        if not task.pending_message_id or not task.pending_prompt:
            return task
        binding = await self.binding(task)
        if not binding or not binding.native_session_id:
            raise self.error("DSH Session 创建结果未知，不能另建或重发")
        from app.runtime.lifecycle import _cancel_active

        if _cancel_active(task):
            raise self.error("正在停止当前 Session")
        sid, text = task.pending_message_id, task.pending_prompt["text"]
        # fence 固定在这次提交上；重试先查 Worker receipt，不提升 fence 重发。
        pending = dict(task.pending_prompt)
        if "fence" not in pending:
            binding.fencing_token += 1
            pending["fence"] = binding.fencing_token
            pending["generation"] = task.pending_attempt + 1
            task.pending_prompt = pending
        fence = pending["fence"]
        client = await self.client(binding)
        await self.db.commit()
        # 不论 HTTP ACK 是否丢失，同一 submission 都由持久账本去重。
        result = await client.submit(
            submission_id=sid, text=text, generation=pending["generation"], fence=fence
        )
        task = await self.runtime.lock_task(task_id)
        expected_id = binding.id
        binding = await self.binding(task)
        if not binding or task.current_session_id != expected_id or binding.fencing_token != fence:
            await self.db.rollback()
            raise self.error("提交期间已停止或更换绑定，请刷新核对")
        if result.get("submission_id") != sid:
            raise self.error("Worker 受理记录与提交不一致")
        task.pending_message_id = None
        task.pending_prompt = None  # 已接受的用户输入不复制为 Nexus 历史。
        task.last_message_id = sid
        task.runtime_connection = "CONNECTED"
        task.runtime_error = None
        await self.runtime.apply_session_state(task, session_state(result))
        return task

    async def send(
        self, task_id: str, text: str, *, expected_binding_id: str,
        expected_submission_id: str | None = None,
        expected_source: tuple[str, str] | None = None,
    ):
        task = await self.runtime.lock_task(task_id)
        binding = await self.binding(task)
        if not binding or binding.id != expected_binding_id or binding.runtime_engine != "dsh":
            raise self.error("Session 已切换，请刷新后再发送")
        if expected_submission_id is not None and task.last_message_id != expected_submission_id:
            raise self.error("当前回合已变化，请重新查询求助")
        if (expected_source is not None
                and (task.responsible_employee_id, task.source_session_id) != expected_source):
            raise self.error("任务来源已变化")
        if task.status in {TaskStatus.SUCCEEDED.value, TaskStatus.CANCELED.value}:
            raise self.error("任务已结束")
        from app.runtime.lifecycle import _cancel_active

        if task.pending_message_id or _cancel_active(task):
            raise self.error("当前提交尚未确认或正在停止")
        client = await self.client(binding)
        anchor, fence = task.last_message_id, binding.fencing_token
        await self.db.commit()
        result = await client.snapshot()
        if result["state"] in {"running", "unknown", "preparing"} or result.get("permissions"):
            raise self.error("当前轮仍在执行或结果未知，请先停止或处理审批")
        task = await self.runtime.lock_task(task_id)
        expected_id = binding.id
        binding = await self.binding(task)
        if (
            not binding
            or task.current_session_id != expected_id
            or task.last_message_id != anchor
            or binding.fencing_token != fence
            or task.pending_message_id
            or _cancel_active(task)
            or (expected_source is not None
                and (task.responsible_employee_id, task.source_session_id) != expected_source)
        ):
            raise self.error("发送期间当前轮已变化")
        task.pending_message_id = f"sub_{uuid4().hex}"
        task.pending_attempt += 1
        task.turn_started_at = datetime.now(UTC)
        task.pending_prompt = {"engine": "dsh", "text": text}
        await self.db.commit()
        return await self.deliver(task_id)

    async def refresh(self, task_id: str):
        task = await self.runtime.lock_task(task_id)
        if task.status in {TaskStatus.SUCCEEDED.value, TaskStatus.CANCELED.value}:
            return task
        binding = await self.binding(task)
        if not binding:
            raise self.error("DSH 绑定不存在")
        client = await self.client(binding)
        binding_id, fence = binding.id, binding.fencing_token
        await self.db.commit()
        if not binding.native_session_id:
            result = await client.lookup()
            task = await self.runtime.lock_task(task_id)
            binding = await self.binding(task)
            if not binding or binding.id != binding_id or binding.fencing_token != fence:
                return task
            if not result.get("native_session_id"):
                raise self.error("DSH 原生 Session 创建结果未知，不能自动创建替代 Session")
            binding.native_session_id = result["native_session_id"]
            binding.engine_metadata = {
                key: result.get(key)
                for key in (
                    "worker_id",
                    "dsh_version",
                    "acp_version",
                    "profile_hash",
                    "assembly_hash",
                )
            }
            await self.db.commit()
        result = await client.snapshot()
        task = await self.runtime.lock_task(task_id)
        binding = await self.binding(task)
        if not binding or task.current_session_id != binding_id or binding.fencing_token != fence:
            return task
        if task.pending_message_id and result.get("submission_id") == task.pending_message_id:
            task.last_message_id = task.pending_message_id
            task.pending_message_id = None
            task.pending_prompt = None
        task.runtime_connection = "CONNECTED"
        task.runtime_error = None
        from app.runtime.lifecycle import _cancel_record

        cancel = _cancel_record(task)
        if cancel and cancel.get("reason") == "completion":
            await self.db.commit()
            return await self.confirm(task_id, expected_binding_id=binding_id)
        if cancel and result.get("stop_confirmed") and result.get("fence") == binding.fencing_token:
            return await self.finish_stop(
                task, binding, cancel_task=bool(cancel.get("close", True)), reason=cancel.get("reason")
            )
        state = session_state(result)
        started = task.turn_started_at
        if (
            state.status == RuntimeSessionStatus.RUNNING.value
            and task.status == TaskStatus.RUNNING.value
            and started
            and (datetime.now(UTC) - started.replace(tzinfo=UTC)).total_seconds()
            >= get_settings().opencode_timeout_seconds
        ):
            await self.db.commit()
            return await self.stop(
                task_id, cancel_task=False, expected_binding_id=binding_id, reason="timeout"
            )
        await record_assistance_requests(self.db, task, [
            f"permission:{item['id']}" for item in result.get("permissions", [])
        ])
        await self.runtime.apply_session_state(task, state)
        return task

    async def stop(
        self, task_id: str, *, cancel_task: bool, expected_binding_id: str | None = None, reason=None
    ):
        from app.runtime.lifecycle import _cancel_record, _set_cancel_token

        task = await self.runtime.lock_task(task_id)
        if task.status in {TaskStatus.SUCCEEDED.value, TaskStatus.CANCELED.value}:
            raise self.error("任务已结束")
        binding = await self.binding(task)
        if expected_binding_id and (not binding or binding.id != expected_binding_id):
            raise self.error("Session 已切换")
        if not binding:
            raise self.error("DSH 绑定不存在")
        token = uuid4().hex
        _set_cancel_token(task, token, hold_seconds=600, close_task=cancel_task)
        suspension = dict(task.suspension)
        suspension["cancel_token"]["reason"] = reason
        task.suspension = suspension
        binding.fencing_token += 1
        fence = binding.fencing_token
        client = await self.client(binding)
        await self.db.commit()
        result = await client.cancel(fence=fence)
        if result.get("stop_confirmed") is not True or result.get("fence") != fence:
            raise self.error("Worker 尚未确认本轮执行停止，保留占用")
        task = await self.runtime.lock_task(task_id)
        record = _cancel_record(task)
        expected_id = binding.id
        binding = await self.binding(task)
        if (
            not binding
            or task.current_session_id != expected_id
            or binding.fencing_token != fence
            or not record
            or record.get("id") != token
        ):
            raise self.error("取消期间绑定或令牌已变化")
        return await self.finish_stop(task, binding, cancel_task=cancel_task, reason=reason)

    async def finish_stop(self, task, binding, *, cancel_task: bool, reason=None):
        task.execution_stop_confirmed_at = datetime.now(UTC)
        task.execution_stop_session_id = binding.id
        task.execution_stop_attempt = task.pending_attempt
        task.execution_stop_operator = "dsh-worker"
        task.execution_stop_reason = "Worker 已持久隔离旧提交并确认归属进程组停止"
        task = await self.runtime._write_canceled_task(
            task,
            cancel_task=cancel_task,
            usage_messages=None,
            native_session_count=None,
            forced_offline_cancel=False,
            stop_error=None,
        )
        if reason == "timeout":
            await self.runtime.apply_session_state(
                task,
                SessionState(
                    RuntimeSessionStatus.INTERRUPTED.value,
                    SessionTerminalReason.TIMEOUT.value,
                    last_message_id=task.last_message_id,
                ),
            )
        return task

    async def confirm(self, task_id: str, *, expected_binding_id: str | None = None):
        task = await self.runtime.lock_task(task_id)
        if task.status == TaskStatus.SUCCEEDED.value:
            return task
        if task.status == TaskStatus.CANCELED.value:
            raise self.error("任务已终止")
        binding = await self.binding(task)
        if expected_binding_id and (not binding or binding.id != expected_binding_id):
            raise self.error("Session 已切换")
        if not binding or task.pending_message_id:
            raise self.error("仍有未确认输入或 Session 绑定缺失")
        client = await self.client(binding)
        anchor, fence = task.last_message_id, binding.fencing_token
        confirmed_binding_id = binding.id
        await self.db.commit()
        result = await client.snapshot()
        from app.runtime.lifecycle import _cancel_active, _cancel_record, _set_cancel_token

        state = session_state(result)
        recorded = _cancel_record(task)
        recovering_dispose = (
            result.get("state") == "disposing"
            and result.get("stop_reason") == "end_turn"
            and recorded
            and recorded.get("reason") == "completion"
        )
        if state.status != RuntimeSessionStatus.SUCCEEDED.value and not recovering_dispose:
            raise self.error("当前轮尚未正常结束，不能确认完成")

        task = await self.runtime.lock_task(task_id)
        binding = await self.binding(task)
        if not binding or binding.id != confirmed_binding_id or binding.fencing_token != fence:
            raise self.error("确认期间当前轮已变化")
        cancel = _cancel_record(task)
        if _cancel_active(task) and (not cancel or cancel.get("reason") != "completion"):
            raise self.error("当前正在停止执行，不能同时确认完成")
        if not cancel or cancel.get("reason") != "completion":
            _set_cancel_token(task, uuid4().hex, hold_seconds=600, close_task=False)
            suspension = dict(task.suspension)
            suspension["cancel_token"]["reason"] = "completion"
            task.suspension = suspension
            binding.fencing_token += 1
        fence = binding.fencing_token
        await self.db.commit()
        disposed = await client.dispose(fence=fence)
        state = session_state(disposed)
        if (
            not disposed.get("stop_confirmed")
            or disposed.get("fence") != fence
            or state.status != RuntimeSessionStatus.SUCCEEDED.value
        ):
            raise self.error("ACP Host 尚未确认回收，保留 Worker 占用")
        # 复盘与业务完成分离。缺失轨迹阻止成功复盘，不覆盖已证明的原生轮次结果。
        evidence = {"runtime_engine": "dsh", "native_session_id": binding.native_session_id}
        trace = None
        try:
            page = await client.read_trace_page(limit=1)
            if page.get("complete"):
                trace = page
                evidence.update(native_trace_sha256=page["sha256"], native_event_count=page["event_count"])
            else:
                evidence["source_error"] = "native_trace_incomplete"
        except Exception:
            evidence["source_error"] = "native_trace_unavailable"
        task = await self.runtime.lock_task(task_id)
        expected_id = binding.id
        binding = await self.binding(task)
        if not binding or binding.id != expected_id or binding.fencing_token != fence:
            raise self.error("确认期间当前轮已变化")
        usage = trace.get("native_usage_totals", {}) if trace else {}
        for target, key in (
            ("input_tokens", "inputTokens"),
            ("output_tokens", "outputTokens"),
            ("total_tokens", "totalTokens"),
            ("cache_read_tokens", "cacheReadTokens"),
            ("cache_write_tokens", "cacheWriteTokens"),
        ):
            setattr(binding, target, usage.get(key))
        if trace:
            binding.llm_call_count = trace.get("native_usage_records")
            binding.tool_call_count = trace.get("event_types", {}).get("tool/call", 0)
        _set_cancel_token(task, None)
        return await self.runtime._finish_confirmation(
            task_id,
            session_id=binding.id,
            workspace=binding.workspace,
            anchor=anchor,
            final_state=state,
            messages=None,
            count=1,
            allowed_sessions=[binding.id],
            engine_evidence=evidence,
        )

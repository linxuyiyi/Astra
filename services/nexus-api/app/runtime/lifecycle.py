"""Task business lifecycle backed by one native OpenCode Session.

There is no Run execution state machine, event mirror, or message database here.
The only retained outbound content is an unacknowledged submission, cleared on ACK.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from time import time_ns
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.config.settings import get_settings
from app.domain import (
    EmployeeStatus,
    ExecutionMode,
    RuntimeSessionStatus,
    SessionTerminalReason,
    TaskStatus,
)
from app.event_bus import event_bus
from app.executors.errors import OpenCodeExecutionError, OpenCodeSessionNotFound
from app.executors.opencode_prompt import (
    bind_task_prompt_context,
    build_task_prompt,
    resolve_task_prompt_context,
)
from app.executors.usage import (
    parse_execution_seconds,
    parse_opencode_usage,
    parse_usage_breakdowns,
)
from app.models import (
    AgentWorker,
    ExperienceReviewJob,
    KnowledgeItem,
    OpenCodeExecutionEnvironment,
    RuntimeSession,
    Task,
    TaskCategory,
)
from app.runtime.environment import environment_snapshot
from app.runtime.occupancy import clear_execution_stop, occupying_clause, sync_employee_occupancy
from app.runtime.opencode_model import gateway_model
from app.runtime.runtime_context import (
    build_runtime_context_part,
    generate_runtime_context,
    hash_runtime_context,
    runtime_context_id,
)
from app.runtime.session_connection import (
    connect,
    prepare_workspace,
    resolve_environment,
    resolve_runtime_session_environment,
    session_tree,
)
from app.runtime.session_state import SessionState, derive_session_state
from app.runtime.submission import body_fetched_at, get_attempt, mark_body_fetched, put_attempt
from app.runtime.wake_outbox import record_wake
from app.runtime.worker_channel import read_worker_channel
from app.runtime.workspace_changes import workspace_head_commit
from app.runtime.workspace_files import materialize_task_attachments
from app.services import TaskService

logger = logging.getLogger(__name__)

_TERMINAL_SESSION_STATUSES = {
    RuntimeSessionStatus.SUCCEEDED.value,
    RuntimeSessionStatus.FAILED.value,
    RuntimeSessionStatus.INTERRUPTED.value,
}

def _task_result_concluded(task: Task) -> bool:
    """Only explicit business closure invalidates a late submission ACK."""
    return task.status in {TaskStatus.SUCCEEDED.value, TaskStatus.CANCELED.value}


def _confirm_blocked(session_status: str) -> str:
    if session_status == RuntimeSessionStatus.RUNNING.value:
        return "Session 仍在执行，请先停止当前回复"
    if session_status == RuntimeSessionStatus.WAITING.value:
        return "仍有问题或权限请求，请先在 Workspace 处理"
    if session_status == RuntimeSessionStatus.UNKNOWN.value:
        return "连接异常，状态待确认，不能确认任务完成"
    return "当前轮尚未正常结束，不能确认任务完成"


_CANCEL_TOKEN = "cancel_token"
_ACCEPTANCE_REASON = "执行结束，待确认"


def _suspension(task: Task) -> dict:
    return dict(task.suspension) if isinstance(task.suspension, dict) else {}


def _cancel_record(task: Task) -> dict | None:
    record = _suspension(task).get(_CANCEL_TOKEN)
    return record if isinstance(record, dict) and record.get("id") else None


def _cancel_active(task: Task, now: datetime | None = None) -> bool:
    record = _cancel_record(task)
    if not record:
        return False
    try:
        deadline = datetime.fromisoformat(str(record.get("until")))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline > (now or datetime.now(UTC))


def _set_cancel_token(
    task: Task, token: str | None, *, hold_seconds: int = 0, close_task: bool = True
) -> None:
    data = _suspension(task)
    if token:
        deadline = datetime.now(UTC) + timedelta(seconds=hold_seconds)
        data[_CANCEL_TOKEN] = {
            "id": token,
            "until": deadline.isoformat(),
            "close": close_task,
        }
    else:
        data.pop(_CANCEL_TOKEN, None)
    task.suspension = data or None


def _same_observed_turn(task: Task, observed: tuple) -> bool:
    session_id, anchor, pending, started = observed
    return (
        task.current_session_id == session_id
        and task.last_message_id == anchor
        and task.pending_message_id == pending
        and task.turn_started_at == started
    )


def _prompt_data(task: Task) -> dict:
    return dict(task.pending_prompt) if isinstance(task.pending_prompt, dict) else {}


def _pending_in_flight(task: Task) -> bool:
    return bool(_prompt_data(task).get("in_flight"))


def _arm_in_flight(prompt: dict) -> dict:
    armed = dict(prompt)
    armed["in_flight"] = True
    armed["in_flight_since"] = datetime.now(UTC).isoformat()
    return armed


def _without_in_flight(task: Task) -> None:
    data = _prompt_data(task)
    data.pop("in_flight", None)
    data.pop("in_flight_since", None)
    task.pending_prompt = data or None


def _in_flight_expired(task: Task, now: datetime | None = None) -> bool:
    raw = _prompt_data(task).get("in_flight_since")
    if not raw:
        return True
    try:
        started = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    limit = get_settings().opencode_control_timeout_seconds
    return (now or datetime.now(UTC)) - started >= timedelta(seconds=limit)


def _message_visible(messages: list[dict] | None, message_id: str | None) -> bool:
    if not messages or not message_id:
        return False
    return any(
        isinstance(item, dict)
        and isinstance(item.get("info"), dict)
        and item["info"].get("id") == message_id
        for item in messages
    )


def _submission_still_unproven(
    task: Task, messages: list[dict] | None, *, forced: bool = False
) -> bool:
    """An in-flight POST can still start after the Session looks idle."""
    if forced or not task.pending_message_id or not _pending_in_flight(task):
        return False
    if _message_visible(messages, task.pending_message_id):
        return False
    return not _in_flight_expired(task)


def _cancel_closes_task(task: Task) -> bool:
    record = _cancel_record(task)
    if not record or "close" not in record:
        return True
    return bool(record.get("close"))


_QUESTION_REASON = "请在 Workspace 回答问题或权限请求"
_RECOVERY_REASON = {
    SessionTerminalReason.ASSISTANT_ERROR.value: "OpenCode 返回执行错误，可在 Workspace 继续",
    SessionTerminalReason.NO_REPLY.value: "当前轮无回复，可在 Workspace 继续",
    SessionTerminalReason.TIMEOUT.value: "当前轮达到时间上限，可在 Workspace 继续",
    SessionTerminalReason.INCOMPLETE.value: "当前轮未完整结束，可在 Workspace 继续",
    SessionTerminalReason.ABORTED.value: "当前回复已停止，可在 Workspace 继续",
    SessionTerminalReason.UNCONFIRMED.value: "当前轮结果无法确认，状态待确认，可在 Workspace 核对后继续",
}


def message_id():
    # Native OpenCode messages are ordered lexically by their 48-bit time prefix.
    stamp = ((time_ns() // 1_000_000) * 0x1000) & ((1 << 48) - 1)
    return f"msg_{stamp:012x}{token_hex(7)}"


class RuntimeStateError(ValueError):
    pass


class MessageHistoryIncomplete(RuntimeStateError):
    """The transcript was not fully read, so the round result is unknown."""


_MAX_MESSAGE_PAGES = 20


class RuntimeService:
    def __init__(self, session):
        self.session = session

    async def lock_task(self, task_id):
        task = await self.session.scalar(
            select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True)
        )
        if not task:
            raise RuntimeStateError("任务不存在")
        return task

    async def project(self, task, status, *, detail=None):
        status = TaskStatus(status)
        if status == TaskStatus.RUNNING and task.status != status:
            task.turn_started_at = datetime.now(UTC)
        changed = task.status != status
        if changed:
            await TaskService(self.session).transition(
                task,
                status,
                current_progress=detail,
                commit=False,
            )
        if task.employee_id:
            await sync_employee_occupancy(self.session, task.employee_id, preferred=task)
        if status == TaskStatus.RUNNING:
            # A reopened task starts a new business completion attempt.
            task.submitted_at = None
            task.confirmed_at = None
            task.human_required_reason = None
            task.human_intervention = None
        await self.session.commit()
        if changed:
            await event_bus.publish({"type": "TASK_STATUS", "task_id": task.id, "status": task.status})

    async def _runtime_session(self, task: Task) -> RuntimeSession | None:
        if not task.current_session_id:
            return None
        return await self.session.get(RuntimeSession, task.current_session_id)

    async def _sync_session_state(self, task: Task, state: SessionState) -> RuntimeSession | None:
        """Persist the derived OpenCode outcome on the bound RuntimeSession."""
        runtime_session = await self._runtime_session(task)
        if not runtime_session:
            return None
        now = datetime.now(UTC)
        runtime_session.status = state.status
        runtime_session.terminal_reason = state.terminal_reason
        runtime_session.finish_reason = state.finish_reason
        if state.last_message_id:
            runtime_session.last_message_id = state.last_message_id
        runtime_session.last_activity_at = now
        if state.status in _TERMINAL_SESSION_STATUSES:
            runtime_session.finished_at = state.finished_at or now
        elif state.status in {
            RuntimeSessionStatus.RUNNING.value,
            RuntimeSessionStatus.WAITING.value,
        }:
            # finished_at only ever holds the current terminal time.
            runtime_session.finished_at = None
        return runtime_session

    async def _update_session_usage(
        self, task: Task, messages: list[dict], *, native_session_count: int | None = None
    ) -> None:
        """Store aggregate token/tool counters derived from native messages."""
        runtime_session = await self._runtime_session(task)
        if not runtime_session:
            return
        runtime_session.native_session_count = max(
            1, native_session_count or runtime_session.native_session_count or 1
        )
        runtime_session.execution_seconds = parse_execution_seconds(messages)
        # Native message info is the single token/model source. Step-finish
        # parts may repeat the same usage and must not be counted a second time.
        # Tool parts are still consumed for call/status/duration aggregation.
        events: list[dict] = []
        for message in messages:
            info = message.get("info") if isinstance(message.get("info"), dict) else {}
            events.append({"info": info})
            for part in message.get("parts", []):
                if not isinstance(part, dict):
                    continue
                part_type = str(part.get("type", "")).lower()
                if "tool" in part_type:
                    events.append({"part": part})
        token_usage, tools, model_name = parse_opencode_usage(events)
        runtime_session.input_tokens = token_usage.input_tokens
        runtime_session.output_tokens = token_usage.output_tokens
        runtime_session.cache_read_tokens = token_usage.cache_read_tokens
        runtime_session.cache_write_tokens = token_usage.cache_write_tokens
        runtime_session.total_tokens = token_usage.total_tokens
        runtime_session.llm_call_count = token_usage.llm_call_count
        if tools:
            runtime_session.tool_usage = [
                {
                    "tool_name": item.name,
                    "call_count": item.call_count,
                    "success_count": item.success_count,
                    "failure_count": item.failure_count,
                    "duration_ms": item.duration_ms,
                }
                for item in tools
            ]
            runtime_session.tool_call_count = sum(item.call_count for item in tools)
        if model_name:
            runtime_session.model_name = model_name
        # Attribute consumption to the real message day and the real message
        # model so daily trend and per-model totals never reuse Session counters.
        timezone = ZoneInfo(get_settings().timezone)
        fallback_date = datetime.now(UTC).astimezone(timezone).date().isoformat()
        usage_by_day, model_usage = parse_usage_breakdowns(
            messages,
            timezone=timezone,
            fallback_date=fallback_date,
            fallback_model=runtime_session.model_name or model_name,
        )
        runtime_session.usage_by_day = usage_by_day
        runtime_session.model_usage = model_usage

    async def apply_session_state(
        self, task: Task, state: SessionState
    ) -> None:
        """Persist one derived Session outcome, project it independently of review features."""
        await self._sync_session_state(task, state)
        await self._project_session_state(task, state)

    async def _project_session_state(self, task: Task, state: SessionState) -> None:
        """Project one Session result onto the business Task without closing it."""
        if _task_result_concluded(task):
            await self.session.commit()
            return
        if state.status == RuntimeSessionStatus.UNKNOWN.value:
            # 通信失败不改 Task 状态。缺失和传输未知的原因在这里写。
            if state.terminal_reason == SessionTerminalReason.SESSION_MISSING.value:
                task.waiting_reason = "SESSION_MISSING"
            elif state.terminal_reason == SessionTerminalReason.TRANSPORT_UNKNOWN.value:
                task.waiting_reason = "TRANSPORT_RECOVERY"
            await self.session.commit()
            return
        if state.terminal_reason == SessionTerminalReason.UNCONFIRMED.value:
            task.waiting_reason = "TRANSPORT_RECOVERY"
            await self.session.commit()
            return
        previous_status = TaskStatus(task.status)
        previous_reason = task.human_required_reason
        if state.status == RuntimeSessionStatus.RUNNING.value:
            task.failure_reason = None
            task.execution_error = None
            task.waiting_reason = None
            await self.project(task, TaskStatus.RUNNING)
            return
        if state.status == RuntimeSessionStatus.SUCCEEDED.value:
            detail = _ACCEPTANCE_REASON
            task.failure_reason = None
            task.execution_error = None
            task.waiting_reason = "EXECUTION_RESULT_REVIEW"
        elif state.status == RuntimeSessionStatus.WAITING.value:
            detail = _QUESTION_REASON
            task.failure_reason = None
            task.execution_error = None
            if state.hitl == "permission":
                task.waiting_reason = "HITL_PERMISSION"
            else:
                task.waiting_reason = "HITL_QUESTION"
        elif state.status in {RuntimeSessionStatus.FAILED.value, RuntimeSessionStatus.INTERRUPTED.value}:
            detail = _RECOVERY_REASON.get(state.terminal_reason, "当前轮执行异常，可在 Workspace 继续")
            task.failure_reason = detail
            task.execution_error = {
                "code": state.terminal_reason or "session_failed",
                "message": detail,
                "source": "opencode_session",
                "session_id": task.current_session_id,
            }
            reason = state.terminal_reason
            if reason == SessionTerminalReason.TIMEOUT.value:
                task.waiting_reason = "TIMEOUT"
            elif reason == SessionTerminalReason.ABORTED.value:
                task.waiting_reason = "MANUAL_INTERVENTION"
            elif reason in {
                SessionTerminalReason.ASSISTANT_ERROR.value,
                SessionTerminalReason.NO_REPLY.value,
                SessionTerminalReason.INCOMPLETE.value,
            }:
                task.waiting_reason = "RETRY_EXHAUSTED"
            elif reason == SessionTerminalReason.COMPLETED.value:
                task.waiting_reason = "EXECUTION_RESULT_REVIEW"
            elif reason == SessionTerminalReason.SESSION_MISSING.value:
                task.waiting_reason = "SESSION_MISSING"
            elif reason == SessionTerminalReason.TRANSPORT_UNKNOWN.value:
                task.waiting_reason = "TRANSPORT_RECOVERY"
            else:
                task.waiting_reason = None
        else:
            task.waiting_reason = None
            await self.session.commit()
            return
        task.human_required_reason = detail
        if task.status == TaskStatus.WAITING:
            task.current_progress = detail
        await self._project_waiting(task, previous_status, previous_reason, detail)

    async def _project_waiting(
        self,
        task: Task,
        previous_status: TaskStatus,
        previous_reason: str | None,
        detail: str,
    ) -> None:
        """Project WAITING, and wake again when the same-round reason changes."""
        event = None
        if previous_status is TaskStatus.WAITING and previous_reason != detail:
            event = await TaskService(self.session).add_event(
                task,
                "TASK_WAITING",
                "work",
                f"等待原因变为{detail}",
                {"from_reason": previous_reason, "to_reason": detail},
                commit=False,
            )
            await record_wake(self.session, task, event, TaskStatus.WAITING, TaskStatus.WAITING)
        await self.project(task, TaskStatus.WAITING, detail=detail)
        if event is not None:
            await event_bus.publish(
                {"type": "TASK_WAITING", "task_id": task.id, "event_id": event.id}
            )

    async def _supersede_session(self, old_id: str | None, new_id: str, *, interrupted: bool) -> None:
        if not old_id or old_id == new_id:
            return
        old = await self.session.get(RuntimeSession, old_id)
        if not old:
            return
        if old.status not in _TERMINAL_SESSION_STATUSES:
            old.status = RuntimeSessionStatus.INTERRUPTED.value
            old.terminal_reason = SessionTerminalReason.ABORTED.value
            old.finished_at = datetime.now(UTC)
        old.superseded_by = new_id
        old.superseded_at = datetime.now(UTC)

    async def _wait_for_session_stop(
        self, client, directory: Path, session_id: str, *, message: str | None = None
    ) -> None:
        """Wait until this Session can no longer write the workspace."""
        timeout = get_settings().opencode_restart_abort_timeout_seconds
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                status = await client.status(directory, session_id)
            except OpenCodeSessionNotFound:
                return
            if status not in {"busy", "retry"}:
                return
            await asyncio.sleep(0.2)
        raise RuntimeStateError(message or "旧 Session 未在超时时间内停止，未创建新 Session")

    async def _reject_maintenance(self, environment) -> None:
        """Refuse start/send while a maintenance reservation is active.

        The check is a conditional update on the environment row in the caller's
        transaction, so it cannot pass on a stale identity-map copy.
        """

        environment_id = getattr(environment, "id", None)
        if environment_id is None:
            return
        from fastapi import HTTPException

        from app.runtime.remote_provisioning import guard_environment_write

        stored = await self.session.get(OpenCodeExecutionEnvironment, environment_id)
        if stored is None:
            return
        try:
            await guard_environment_write(self.session, stored)
        except HTTPException as exc:
            await self.session.rollback()
            raise RuntimeStateError(str(exc.detail)) from exc

    async def start(self, task_id: str, *, restart: bool = False):
        task = await self.lock_task(task_id)
        if task.current_session_id and not restart:
            raise RuntimeStateError("任务已有 Session，请在 Workspace 中继续；重新开始需显式创建新 Session")
        if task.status == TaskStatus.RUNNING and not restart:
            raise RuntimeStateError("任务仍在运行，请在 Workspace 中继续；重新开始需显式创建新 Session")
        if task.pending_message_id:
            raise RuntimeStateError("任务有未确认的输入，请先停止或重连")

        old_session_id = task.current_session_id if restart else None
        old_binding = (
            await self.session.get(RuntimeSession, old_session_id)
            if old_session_id
            else None
        )
        employee = await self.session.scalar(
            select(AgentWorker)
            .where(AgentWorker.id == task.employee_id)
            .options(selectinload(AgentWorker.execution_environment))
            .with_for_update()
        )
        if not employee or not employee.enabled or not employee.execution_environment:
            raise RuntimeStateError("请先分配已配置运行环境的员工")
        from app.runtime.assignment import worker_assignment_block

        blocked = worker_assignment_block(
            employee, task_type=task.task_type,
            responsible_employee_id=task.responsible_employee_id,
        )
        if blocked:
            raise RuntimeStateError(blocked)
        occupied = await self.session.scalar(
            select(Task.id).where(occupying_clause(employee.id), Task.id != task.id).limit(1)
        )
        if occupied:
            raise RuntimeStateError("员工正在处理其他任务")
        if (
            old_session_id
            and old_binding is None
            and task.status in {TaskStatus.RUNNING, TaskStatus.WAITING}
        ):
            raise RuntimeStateError("旧 Session 绑定缺失，无法确认旧执行已停止")

        environment = employee.execution_environment
        from fastapi import HTTPException

        from app.runtime.remote_provisioning import guard_environment_write

        try:
            await guard_environment_write(self.session, environment)
            from app.runtime.maintenance import scheduling_block

            blocked = await scheduling_block(self.session, environment)
            if blocked:
                raise RuntimeStateError(blocked)
        except HTTPException as exc:
            raise RuntimeStateError(str(exc.detail)) from exc
        from app.runtime.dispatcher import runtime_assignment_block

        blocked = runtime_assignment_block(environment)
        if blocked:
            raise RuntimeStateError(blocked)
        new_snapshot = environment_snapshot(environment)
        initial_id = message_id()
        reservation = {"pending_message_id": initial_id, "pending_attempt": 0}
        if restart:
            clear_execution_stop(task)
            reservation.update(
                execution_stop_confirmed_at=None,
                execution_stop_session_id=None,
                execution_stop_attempt=None,
                execution_stop_operator=None,
                execution_stop_reason=None,
            )
        reserved = await self.session.execute(
            update(Task)
            .where(Task.id == task.id, Task.pending_message_id.is_(None))
            .values(**reservation)
        )
        if reserved.rowcount != 1:
            raise RuntimeStateError("任务正在启动，请先重连核对或停止")
        await sync_employee_occupancy(self.session, employee.id, preferred=task)
        employee.status = EmployeeStatus.WORKING
        await self.session.commit()

        created_session_id: str | None = None
        new_workspace: str | None = None
        try:
            # Stop the previous execution using the immutable RuntimeSession binding,
            # never the employee's newly edited environment.
            if old_binding and old_binding.status not in _TERMINAL_SESSION_STATUSES:
                old_environment = await resolve_runtime_session_environment(
                    self.session, old_binding
                )
                await self.session.commit()
                try:
                    async with connect(old_environment, start=True) as old_client:
                        await old_client.abort(Path(old_binding.workspace), old_binding.id)
                        await self._wait_for_session_stop(
                            old_client,
                            Path(old_binding.workspace),
                            old_binding.id,
                        )
                except OpenCodeSessionNotFound:
                    stale = await self.session.get(RuntimeSession, old_binding.id)
                    if stale and stale.status not in _TERMINAL_SESSION_STATUSES:
                        stale.status = RuntimeSessionStatus.INTERRUPTED.value
                        stale.terminal_reason = SessionTerminalReason.SESSION_MISSING.value
                        stale.finished_at = datetime.now(UTC)
                        await self.session.commit()
                except RuntimeStateError:
                    # The old Session accepted the connection but is still writing the
                    # workspace. Never start a second writer, even for a LOCAL server.
                    raise
                except Exception:
                    if (
                        old_environment.mode != ExecutionMode.LOCAL
                        or self._local_port_in_use(old_environment.opencode_server_port)
                    ):
                        raise
                    stale = await self.session.get(RuntimeSession, old_binding.id)
                    if stale and stale.status not in _TERMINAL_SESSION_STATUSES:
                        stale.status = RuntimeSessionStatus.INTERRUPTED.value
                        stale.terminal_reason = SessionTerminalReason.SESSION_MISSING.value
                        stale.finished_at = datetime.now(UTC)
                        await self.session.commit()

            old_location = old_binding.environment if old_binding else task.execution_environment_snapshot
            location_keys = ("mode", "host", "port", "username", "workspace_root")
            reuse_workspace = bool(
                task.workspace
                and old_location
                and all(old_location.get(key) == new_snapshot.get(key) for key in location_keys)
            )
            new_workspace = await prepare_workspace(
                task,
                environment,
                reuse_existing=reuse_workspace,
            )
            base_commit = await workspace_head_commit(environment, new_workspace)
            if base_commit:
                new_snapshot["base_commit"] = base_commit

            # New environment relocation cannot safely reuse materialized attachment
            # paths from the old host. Preserve metadata, but only materialize payloads
            # that still carry their original data URL.
            attachments = list(task.attachments or [])
            materialized_attachments = attachments
            if attachments and reuse_workspace:
                materialized_attachments = await materialize_task_attachments(
                    environment, new_workspace, attachments
                )
            elif attachments:
                materialized_attachments = []
                for item in attachments:
                    copied = dict(item)
                    if copied.get("content"):
                        materialized_attachments.extend(
                            await materialize_task_attachments(
                                environment, new_workspace, [copied]
                            )
                        )
                    else:
                        copied.pop("workspace_path", None)
                        materialized_attachments.append(copied)

            async with connect(environment, start=True) as client:
                runtime_token = generate_runtime_context()
                settings = get_settings()
                health = await client.health()
                reported = health.get("version")
                if isinstance(reported, str) and reported.strip():
                    server_version = reported.strip().removeprefix("v")
                else:
                    server_version = "unknown"
                new_snapshot["opencode_version"] = server_version
                raw_model = environment.model_name or settings.opencode_model
                effective_model = gateway_model(raw_model)
                created = await client.create_session(
                    Path(new_workspace),
                    title=task.title,
                    model_name=effective_model,
                    agent=settings.opencode_agent or "build",
                )
                created_session_id = created["id"]

                task = await self.lock_task(task_id)
                if (
                    task.pending_message_id != initial_id
                    or task.current_session_id != old_session_id
                ):
                    await self.session.rollback()
                    await client.abort(Path(new_workspace), created_session_id)
                    return await self.session.get(Task, task_id)

                # Only now switch the business pointer to the new environment/session.
                task.workspace = new_workspace
                previous_snapshot = (
                    task.execution_environment_snapshot
                    if isinstance(task.execution_environment_snapshot, dict)
                    else {}
                )
                new_snapshot = {**previous_snapshot, **new_snapshot}
                task.execution_environment_snapshot = new_snapshot
                if new_snapshot.get("base_commit"):
                    task.base_commit = new_snapshot["base_commit"]
                task.attachments = materialized_attachments
                task.current_session_id = created_session_id
                clear_execution_stop(task)
                now = datetime.now(UTC)
                context_digest = hash_runtime_context(runtime_token)
                self.session.add(
                    RuntimeSession(
                        id=created_session_id,
                        task_id=task.id,
                        employee_id=employee.id,
                        workspace=new_workspace,
                        workspace_base_commit=base_commit,
                        environment=new_snapshot,
                        status=RuntimeSessionStatus.RUNNING.value,
                        started_at=now,
                        last_activity_at=now,
                        model_name=effective_model,
                        runtime_context_hash=context_digest,
                        runtime_context_id=runtime_context_id(context_digest),
                    )
                )
                # Flush the new binding before superseding the old Session so the
                # whole round (new RuntimeSession + old supersede) is one
                # transaction: any failure rolls everything back atomically.
                await self.session.flush()
                await self._supersede_session(
                    old_session_id,
                    created_session_id,
                    interrupted=True,
                )
                category = (
                    await self.session.get(TaskCategory, task.category_id)
                    if task.category_id
                    else None
                )
                bind_task_prompt_context(task, resolve_task_prompt_context(task, category))
                task.pending_prompt = {
                    "action": "prompt_async",
                    "body": {
                        "messageID": initial_id,
                        "parts": [
                            {
                                "type": "text",
                                "text": build_task_prompt(
                                    task,
                                    Path(new_workspace),
                                    native_question=True,
                                ),
                            },
                            build_runtime_context_part(runtime_token),
                        ],
                    },
                }
                task.last_message_id = initial_id
                task.runtime_connection = "CONNECTED"
                task.runtime_error = None
                task.failure_reason = None
                task.execution_error = None
                task.finished_at = None
                if restart:
                    # An explicit restart is a new execution round: the new Session
                    # must not inherit the previous round's status or timeout
                    # window. A definitive rejection returns it to WAITING;
                    # ambiguous acknowledgements retain their recovery evidence.
                    task.turn_started_at = now
                    task.confirmed_at = None
                    task.submitted_at = None
                    await self.session.execute(
                        update(ExperienceReviewJob)
                        .where(
                            ExperienceReviewJob.task_id_snapshot == task.id,
                            ExperienceReviewJob.status.in_(
                                ("PENDING", "COLLECTING", "ANALYZING", "VALIDATING", "WRITING", "RETRY_WAIT")
                            ),
                        )
                        .values(
                            status="SUPERSEDED",
                            error_code="new_round",
                            error_message="任务已开始新的执行轮次",
                            finished_at=now,
                            lease_owner=None,
                            lease_token=None,
                            lease_until=None,
                        )
                    )
                    await self.session.execute(
                        update(KnowledgeItem)
                        .where(
                            KnowledgeItem.source_task_id == task.id,
                            KnowledgeItem.origin == "system_review",
                            KnowledgeItem.status == "candidate",
                        )
                        .values(
                            review_note="任务已开始新执行轮次，候选可能过期；审核时请确认是否仍适用。"
                        )
                    )
                    await TaskService(self.session).transition(task, TaskStatus.RUNNING, commit=False)
                await self.session.commit()
            return await self.deliver_first_submission(task.id)
        except Exception as exc:
            await self.session.rollback()
            definitive_failure = (
                isinstance(exc, OpenCodeSessionNotFound) or type(exc) is OpenCodeExecutionError
            )
            task = await self.lock_task(task_id)
            abort_unbound_session = False
            if task.pending_message_id == initial_id:
                if definitive_failure or not task.pending_prompt:
                    task.pending_message_id = None
                    task.pending_prompt = None
                    task.runtime_error = str(exc)
                    owns_new_session = bool(
                        created_session_id
                        and task.current_session_id == created_session_id
                    )
                    if owns_new_session and task.status == TaskStatus.RUNNING:
                        # The new round definitively failed to start. Never leave
                        # the Task looking RUNNING with a dead Session: fail the
                        # round and mark its RuntimeSession interrupted.
                        task.failure_reason = str(exc)
                        task.execution_error = {
                            "code": "submission_failed",
                            "message": str(exc),
                            "source": "opencode_session",
                            "session_id": created_session_id,
                        }
                        runtime_session = await self.session.get(
                            RuntimeSession, created_session_id
                        )
                        if runtime_session and runtime_session.status not in _TERMINAL_SESSION_STATUSES:
                            runtime_session.status = RuntimeSessionStatus.INTERRUPTED.value
                            runtime_session.terminal_reason = SessionTerminalReason.ABORTED.value
                            runtime_session.finished_at = datetime.now(UTC)
                        await self.project(
                            task, TaskStatus.WAITING, detail="重新开始失败，可检查配置后继续"
                        )
                    else:
                        await sync_employee_occupancy(self.session, task.employee_id)
                        await self.session.commit()
                    abort_unbound_session = True
                else:
                    # OpenCode may already hold the submitted prompt and only the ACK
                    # was lost. Keep pending + created Session as recovery evidence so
                    # background refresh can reconcile; never abort a live Session.
                    task.runtime_connection = "UNAVAILABLE"
                    task.runtime_error = str(exc)
                    await self.session.commit()
            else:
                await self.session.rollback()
            if abort_unbound_session and created_session_id and new_workspace:
                try:
                    async with connect(environment, start=True) as client:
                        await client.abort(Path(new_workspace), created_session_id)
                except Exception:
                    logger.exception(
                        "Failed to abort unbound restart Session %s",
                        created_session_id,
                    )
            raise

    async def send(self, task_id, body, *, native_action="prompt_async", expected_session_id=None):
        if native_action not in {"prompt_async", "command"}:
            raise RuntimeStateError("不支持的 Session 输入")

        # Read the binding first, then release the DB transaction before touching
        # OpenCode. Runtime/network latency must never extend a row lock lifetime.
        task = await self.session.get(Task, task_id)
        if not task:
            raise RuntimeStateError("任务不存在")
        if not task.current_session_id:
            raise RuntimeStateError("Session 不存在；请显式启动任务")
        if expected_session_id and task.current_session_id != expected_session_id:
            raise RuntimeStateError("当前 Session 已切换，请刷新 Workspace")
        if task.pending_message_id:
            raise RuntimeStateError("上次输入尚未确认，请重连核对 Session 后再发送")
        if _task_result_concluded(task):
            raise RuntimeStateError("任务已结束；如需重新执行，请显式重新开始")
        if _cancel_active(task):
            raise RuntimeStateError("任务正在取消，不能提交新输入")
        submitted_session = task.current_session_id
        workspace = task.workspace
        employee_id = task.employee_id
        environment = await resolve_environment(self.session, task)
        await self._reject_maintenance(environment)
        await self.session.commit()

        outbound_id = body.get("messageID") or message_id()
        if not re.fullmatch(r"msg_[a-zA-Z0-9]{8,100}", outbound_id):
            raise RuntimeStateError("消息 ID 无效")
        body = {**body, "messageID": outbound_id}

        async with connect(environment, start=True) as client:
            allowed_sessions = await session_tree(client, Path(workspace), submitted_session)
            pending = await client.questions(Path(workspace)) + await client.permissions(Path(workspace))
            if any(item.get("sessionID") in allowed_sessions for item in pending):
                raise RuntimeStateError("请先在 Workspace 回答当前 Session 树中的问题或权限请求")

        # Short transaction: revalidate the binding and reserve this submission.
        task = await self.lock_task(task_id)
        if task.current_session_id != submitted_session:
            await self.session.rollback()
            raise RuntimeStateError("当前 Session 已切换，请刷新 Workspace")
        if _task_result_concluded(task):
            await self.session.rollback()
            raise RuntimeStateError("任务已结束")
        if task.pending_message_id:
            await self.session.rollback()
            raise RuntimeStateError("已有待确认输入")
        if _cancel_active(task):
            await self.session.rollback()
            raise RuntimeStateError("任务正在取消，不能提交新输入")
        employee = await self.session.scalar(
            select(AgentWorker)
            .where(AgentWorker.id == employee_id)
            .with_for_update()
        )
        occupied = await self.session.scalar(
            select(Task.id).where(occupying_clause(employee_id), Task.id != task.id).limit(1)
        )
        if occupied:
            await self.session.rollback()
            raise RuntimeStateError("员工正在处理其他任务")
        await self._reject_maintenance(environment)
        clear_execution_stop(task)
        task.last_message_id = outbound_id
        started = datetime.now(UTC)
        prompt = _arm_in_flight({"action": native_action, "body": body})
        expected_attempt = task.pending_attempt or 0
        new_attempt = expected_attempt + 1
        request_id = f"req_{token_hex(8)}"
        put_attempt(task, outbound_id, new_attempt, request_id)
        task.pending_prompt = prompt
        task.turn_started_at = started
        reserved = await self.session.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.pending_message_id.is_(None),
                Task.pending_attempt == expected_attempt,
            )
            .values(
                pending_message_id=outbound_id,
                pending_attempt=new_attempt,
                turn_started_at=started,
                pending_prompt=prompt,
                submission_attempts=task.submission_attempts,
                execution_stop_confirmed_at=None,
                execution_stop_session_id=None,
                execution_stop_attempt=None,
                execution_stop_operator=None,
                execution_stop_reason=None,
            )
        )
        if reserved.rowcount != 1:
            await self.session.rollback()
            raise RuntimeStateError("已有待确认输入")
        if employee:
            await sync_employee_occupancy(self.session, employee.id, preferred=task)
            employee.status = EmployeeStatus.WORKING
        # This reservation belongs to a new turn on the same native Session.
        # A previous abort/timeout must not invalidate its eventual ACK.
        await self._sync_session_state(task, SessionState(status=RuntimeSessionStatus.RUNNING.value))
        await self.session.commit()
        use_worker = await read_worker_channel(self.session, environment.id) == "worker"
        if not use_worker:
            await self._note_body_fetched(task_id, outbound_id, new_attempt)

        try:
            async with connect(environment, start=True) as client:
                await client.session(Path(workspace), submitted_session)
                worker_call = {}
                if use_worker:
                    worker_call = {
                        "command_request_id": request_id,
                        "attempt_ref": {
                            "task_id": task_id,
                            "message_id": outbound_id,
                            "attempt": new_attempt,
                        },
                        "session_id": submitted_session,
                    }
                await client.request(
                    "POST",
                    f"/session/{submitted_session}/{native_action}",
                    directory=Path(workspace),
                    body=body,
                    **worker_call,
                )
        except Exception as exc:
            await self._settle_failed_submission(task_id, outbound_id, exc)
            raise

        task = await self.lock_task(task_id)
        if _cancel_active(task) and not _task_result_concluded(task):
            _without_in_flight(task)
            await self.session.commit()
            await self._complete_cancel(task_id, submission_id=outbound_id)
            raise RuntimeStateError("任务正在取消，不能提交新输入")
        # A background refresh may have already confirmed this exact message and
        # cleared pending. Only an actual control operation invalidates the
        # submission; session identity and the bound RuntimeSession are the
        # facts, never a Task status projected for a different round.
        background_acked = task.pending_message_id is None
        if await self._submission_invalidated(
            task, submitted_session=submitted_session, pending_id=outbound_id
        ):
            newer_turn = task.current_session_id == submitted_session and task.last_message_id != outbound_id
            await self.session.commit()
            if not newer_turn:
                async with connect(environment, start=True) as client:
                    await client.abort(Path(workspace), submitted_session)
            return await self.session.get(Task, task_id)
        if background_acked:
            # Observation already recorded the native state. Acceptance alone
            # cannot overwrite a completed, failed or waiting turn.
            task.runtime_connection = "CONNECTED"
            task.runtime_error = None
            await self.session.commit()
            return task
        task.pending_message_id = None
        task.pending_prompt = None
        task.runtime_connection = "CONNECTED"
        task.runtime_error = None
        task.failure_reason = None
        await self._mark_session_running(task)
        return task

    async def _submission_invalidated(
        self, task: Task, *, submitted_session: str, pending_id: str
    ) -> bool:
        """Whether a control operation invalidated an in-flight submission.

        A background ACK that only clears ``pending_message_id`` is normal
        convergence, never invalidation. The round is invalidated only when the
        Task points at a different Session, the reservation was replaced, or the
        bound RuntimeSession was interrupted (stop/cancel/timeout). This is
        Session-based on purpose: a Task status projected to RUNNING for a new
        round must not be able to mask a stale in-flight submission.
        """

        if task.current_session_id != submitted_session or _task_result_concluded(task):
            return True
        if task.last_message_id != pending_id:
            return True
        if task.pending_message_id is not None and task.pending_message_id != pending_id:
            return True
        runtime_session = await self.session.get(RuntimeSession, submitted_session)
        return bool(
            runtime_session
            and runtime_session.status == RuntimeSessionStatus.INTERRUPTED.value
        )

    async def _mark_session_running(self, task: Task) -> None:
        await self.apply_session_state(
            task, SessionState(status=RuntimeSessionStatus.RUNNING.value)
        )

    async def _note_body_fetched(self, task_id: str, message_id: str, attempt: int) -> bool:
        task = await self.lock_task(task_id)
        if task.pending_message_id != message_id or task.pending_attempt != attempt:
            await self.session.rollback()
            return False
        try:
            mark_body_fetched(task, message_id, attempt, datetime.now(UTC))
        except KeyError:
            await self.session.rollback()
            raise RuntimeStateError("没有这次提交的请求记录，不能发送") from None
        await self.session.commit()
        return True

    async def _all_messages(self, client, directory: Path, session_id: str) -> list[dict]:
        collected: list[dict] = []
        before = None
        seen: set[str] = set()
        for _page in range(_MAX_MESSAGE_PAGES):
            page = await client.messages_page(directory, session_id, limit=100, before=before)
            batch = list(page.get("messages") or [])
            cursor = page.get("next_before")
            if cursor and str(cursor) in seen:
                raise MessageHistoryIncomplete("消息没有读完，结果未知")
            if not batch:
                if cursor:
                    raise MessageHistoryIncomplete("消息没有读完，结果未知")
                return collected
            collected.extend(batch)
            if not cursor:
                return collected
            seen.add(str(cursor))
            before = cursor
        raise MessageHistoryIncomplete("消息没有读完，结果未知")

    async def deliver_first_submission(self, task_id: str):
        """Post the prompt reserved by start(). This is not a human retry."""

        task = await self.lock_task(task_id)
        if _task_result_concluded(task):
            await self.session.rollback()
            raise RuntimeStateError("任务已结束，不能重试输入")
        if _cancel_active(task):
            await self.session.rollback()
            raise RuntimeStateError("任务正在取消，不能提交新输入")
        if not task.pending_message_id or not task.pending_prompt:
            await self.session.rollback()
            raise RuntimeStateError("没有待确认输入")
        if task.pending_attempt:
            await self.session.rollback()
            raise RuntimeStateError("这次输入已经提交，请人工重试")
        pending_id = task.pending_message_id
        expected = task.pending_attempt or 0
        attempt = expected + 1
        prompt = _arm_in_flight(_prompt_data(task))
        clear_execution_stop(task)
        put_attempt(task, pending_id, attempt, f"req_{token_hex(8)}")
        claimed = await self.session.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.pending_message_id == pending_id,
                Task.pending_attempt == expected,
            )
            .values(
                pending_attempt=attempt,
                pending_prompt=prompt,
                submission_attempts=task.submission_attempts,
                execution_stop_confirmed_at=None,
                execution_stop_session_id=None,
                execution_stop_attempt=None,
                execution_stop_operator=None,
                execution_stop_reason=None,
            )
        )
        if claimed.rowcount != 1:
            await self.session.rollback()
            raise RuntimeStateError("已有提交正在进行")
        return await self._transmit_attempt(task, prompt, attempt)

    async def retry_submission(self, task_id: str, *, confirm_unknown: bool = False):
        """Human retry. Recovery, reconnect, dispatch, heartbeat, and timeout never call this."""

        task = await self.lock_task(task_id)
        if _task_result_concluded(task):
            await self.session.rollback()
            raise RuntimeStateError("任务已结束，不能重试输入")
        if _cancel_active(task):
            await self.session.rollback()
            raise RuntimeStateError("任务正在取消，不能提交新输入")
        if not task.pending_message_id or not task.pending_prompt or not task.current_session_id:
            await self.session.rollback()
            raise RuntimeStateError("没有待确认输入")
        if _pending_in_flight(task) and not _in_flight_expired(task):
            await self.session.rollback()
            raise RuntimeStateError("上次提交仍在途，未到超时，不能重试")
        pending_id = task.pending_message_id
        expected = task.pending_attempt or 0
        record = get_attempt(task, pending_id, expected)
        if record is None or not record.get("request_id"):
            await self.session.rollback()
            raise RuntimeStateError("没有这次提交的请求记录，不能判断是否已发送")
        fetched = body_fetched_at(task, pending_id, expected)
        if fetched is None:
            from app.worker_gateway.data_channel import UnfetchedSealError, seal_unfetched_request

            try:
                await seal_unfetched_request(self.session, str(record["request_id"]))
            except UnfetchedSealError as exc:
                await self.session.rollback()
                raise RuntimeStateError(str(exc)) from exc
        if fetched is not None:
            limit = get_settings().opencode_control_timeout_seconds
            ready_at = fetched + timedelta(seconds=limit * 2)
            if datetime.now(UTC) < ready_at:
                await self.session.rollback()
                raise RuntimeStateError("请求体已拉取，结果未知，请等到本地上限和落库余量之后再确认")
            if not confirm_unknown:
                await self.session.rollback()
                raise RuntimeStateError("结果未知，需要再次确认后才能重发")
        prompt = _arm_in_flight(_prompt_data(task))
        attempt = expected + 1
        clear_execution_stop(task)
        put_attempt(task, pending_id, attempt, f"req_{token_hex(8)}")
        claimed = await self.session.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.pending_message_id == pending_id,
                Task.pending_attempt == expected,
            )
            .values(
                pending_attempt=attempt,
                pending_prompt=prompt,
                submission_attempts=task.submission_attempts,
                execution_stop_confirmed_at=None,
                execution_stop_session_id=None,
                execution_stop_attempt=None,
                execution_stop_operator=None,
                execution_stop_reason=None,
            )
        )
        if claimed.rowcount != 1:
            await self.session.rollback()
            raise RuntimeStateError("已有提交正在进行")
        return await self._transmit_attempt(task, prompt, attempt)

    async def _transmit_attempt(self, task: Task, pending_prompt: dict, attempt: int):
        task_id = task.id
        pending_id = task.pending_message_id
        session_id = task.current_session_id
        workspace = task.workspace
        environment = await resolve_environment(self.session, task)
        await self.session.commit()
        use_worker = await read_worker_channel(self.session, environment.id) == "worker"
        record = get_attempt(task, pending_id, attempt) or {}
        request_id = record.get("request_id")

        async with connect(environment, start=True) as client:
            try:
                messages = await self._all_messages(client, Path(workspace), session_id)
            except MessageHistoryIncomplete:
                task = await self.lock_task(task_id)
                if task.pending_message_id == pending_id:
                    task.runtime_error = "消息没有读完，结果未知"
                    await self.session.commit()
                else:
                    await self.session.rollback()
                raise
            except Exception as exc:
                await self.disconnected(task_id, exc)
                raise
            if _message_visible(messages, pending_id):
                task = await self.lock_task(task_id)
                if task.pending_message_id == pending_id:
                    _without_in_flight(task)
                    await self.session.commit()
                else:
                    await self.session.rollback()
                return await self.refresh(task_id, client=client)
            sent_now = False
            try:
                if use_worker:
                    if not request_id:
                        raise RuntimeStateError("没有这次提交的请求记录，不能发送")
                else:
                    noted = await self._note_body_fetched(task_id, pending_id, attempt)
                    if not noted:
                        raise RuntimeStateError("提交已经变化，未重发")
                worker_call = {}
                if use_worker:
                    worker_call = {
                        "command_request_id": request_id,
                        "attempt_ref": {"task_id": task_id, "message_id": pending_id, "attempt": attempt},
                        "session_id": session_id,
                    }
                await client.request(
                    "POST",
                    f"/session/{session_id}/{pending_prompt['action']}",
                    directory=Path(workspace),
                    body=pending_prompt["body"],
                    **worker_call,
                )
                sent_now = True
            except Exception as exc:
                # A failed prompt call can still be settled for cancel. A failed
                # message read never reaches here and keeps in_flight.
                current = await self.session.get(Task, task_id)
                canceling = bool(
                    current and (_cancel_active(current) or current.status == TaskStatus.CANCELED)
                )
                if canceling:
                    await self._settle_failed_submission(task_id, pending_id, exc)
                elif not isinstance(exc, RuntimeStateError):
                    task = await self.lock_task(task_id)
                    if task.pending_message_id == pending_id:
                        _without_in_flight(task)
                        await self.session.commit()
                    else:
                        await self.session.rollback()
                raise

            # Revalidate after the network write. stop/delete/restart may have
            # invalidated this reservation while the request was in flight.
            task = await self.lock_task(task_id)
            if _cancel_active(task) and not _task_result_concluded(task):
                _without_in_flight(task)
                await self.session.commit()
                await self._complete_cancel(task_id, submission_id=pending_id)
                raise RuntimeStateError("任务正在取消，不能提交新输入")
            # A background refresh ACK clears pending without invalidating the
            # submission. Only a session switch, an interrupted bound Session, or
            # a different reservation is a real control-operation failure.
            background_acked = task.pending_message_id is None
            stale = await self._submission_invalidated(
                task, submitted_session=session_id, pending_id=pending_id
            )
            if stale:
                newer_turn = task.current_session_id == session_id and task.last_message_id != pending_id
                await self.session.rollback()
                if not newer_turn and sent_now:
                    try:
                        await client.abort(Path(workspace), session_id)
                    except Exception:
                        logger.exception(
                            "Failed to abort stale submission for task %s session %s",
                            task_id,
                            session_id,
                        )
                raise RuntimeStateError("待确认输入已被控制操作失效，当前 Session 已停止")

            if background_acked:
                # Preserve the state already observed for this accepted input.
                task.runtime_connection = "CONNECTED"
                task.runtime_error = None
                await self.session.commit()
                return task

            task.pending_message_id = None
            task.pending_prompt = None
            task.runtime_connection = "CONNECTED"
            task.runtime_error = None
            await self._mark_session_running(task)
            return task

    @staticmethod
    def _local_port_in_use(port: int) -> bool:
        with socket.socket() as probe:
            probe.settimeout(0.3)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    async def _collect_final_usage(
        self, client, directory: Path, session_id: str
    ) -> tuple[list[dict] | None, int | None]:
        """Best-effort final usage capture; cancellation semantics never depend on it."""
        try:
            allowed_sessions = await session_tree(client, directory, session_id)
            limit = get_settings().opencode_final_usage_max_messages
            messages: list[dict] = []
            for native_session_id in [session_id, *sorted(allowed_sessions - {session_id})]:
                before: str | None = None
                try:
                    while len(messages) < limit:
                        page = await client.messages_page(
                            directory,
                            native_session_id,
                            limit=min(100, limit - len(messages)),
                            before=before,
                        )
                        batch = page.get("messages") if isinstance(page, dict) else []
                        if not batch:
                            break
                        messages.extend(batch)
                        before = page.get("next_before") if isinstance(page, dict) else None
                        if not before:
                            break
                except OpenCodeSessionNotFound:
                    continue
            if len(messages) >= limit:
                logger.warning(
                    "Final usage capture for %s reached the %s-message bound",
                    session_id,
                    limit,
                )
            return messages, len(allowed_sessions)
        except Exception:
            logger.exception("Failed to collect final usage for Session %s", session_id)
            return None, None

    async def _settle_failed_submission(self, task_id: str, outbound_id: str, exc: Exception) -> None:
        """Record a finished attempt, then abort if this task is already being canceled."""
        task = await self.lock_task(task_id)
        canceling = _cancel_active(task) or task.status == TaskStatus.CANCELED
        if task.pending_message_id == outbound_id:
            definitive = type(exc) is OpenCodeExecutionError or isinstance(exc, OpenCodeSessionNotFound)
            if definitive:
                task.pending_message_id = None
                task.pending_prompt = None
            else:
                # The HTTP call returned. Keep the reservation until cancel can prove
                # the prompt will not keep running, but it is no longer in flight.
                _without_in_flight(task)
            task.runtime_connection = "UNAVAILABLE"
            task.runtime_error = str(exc)
            await self.session.commit()
        else:
            await self.session.rollback()
        if not canceling:
            return
        try:
            await self._complete_cancel(task_id, submission_id=outbound_id)
        except RuntimeStateError:
            logger.exception("In-flight submission for task %s stayed busy during cancel", task_id)

    async def _write_canceled_task(
        self,
        task: Task,
        *,
        cancel_task: bool,
        usage_messages: list[dict] | None,
        native_session_count: int | None,
        forced_offline_cancel: bool,
        stop_error: Exception | None,
    ) -> Task:
        runtime_session = await self._runtime_session(task)
        if usage_messages is not None:
            await self._update_session_usage(
                task, usage_messages, native_session_count=native_session_count
            )
        if runtime_session and (
            runtime_session.status not in _TERMINAL_SESSION_STATUSES
            or task.pending_message_id or task.pending_prompt
        ):
            runtime_session.status = RuntimeSessionStatus.INTERRUPTED.value
            runtime_session.terminal_reason = SessionTerminalReason.ABORTED.value
            runtime_session.finished_at = datetime.now(UTC)
        if forced_offline_cancel and cancel_task:
            await TaskService(self.session).add_event(
                task,
                "RUNTIME_OFFLINE_CANCELED",
                "runtime",
                "Runtime 已不可执行，按明确取消意图收敛任务",
                {"error": str(stop_error) if stop_error else "session missing"},
                commit=False,
            )
        _set_cancel_token(task, None)
        task.pending_message_id = None
        task.pending_prompt = None
        task.runtime_error = None
        if cancel_task:
            # 投影之外只在确认和取消时清空 waiting_reason。
            task.waiting_reason = None
        await self.project(
            task,
            TaskStatus.CANCELED if cancel_task else TaskStatus.WAITING,
            detail="任务已终止" if cancel_task else "当前回复已停止，可在 Workspace 继续",
        )
        return task

    async def _stop_native(
        self, environment, workspace: str, session_id: str
    ) -> tuple[list[dict] | None, int | None, bool]:
        """Abort and wait. Returns usage, session count, and whether the runtime is gone."""
        async with connect(environment, start=True) as client:
            directory = Path(workspace)
            await client.abort(directory, session_id)
            try:
                watched = await session_tree(client, directory, session_id)
            except OpenCodeSessionNotFound:
                watched = set()
            for watched_id in sorted(watched or {session_id}):
                await self._wait_for_session_stop(
                    client,
                    directory,
                    watched_id,
                    message="执行尚未停止，未取消任务，也未释放 Worker",
                )
            usage, count = await self._collect_final_usage(client, directory, session_id)
            return usage, count, False

    async def _complete_cancel(self, task_id: str, *, submission_id: str | None = None) -> Task:
        """Abort a submission whose result is known, then close the task if it is still ours."""
        await self.session.rollback()
        task = await self.lock_task(task_id)
        if (
            task.status == TaskStatus.CANCELED
            and submission_id
            and task.pending_message_id not in {None, submission_id}
        ):
            await self.session.rollback()
            return task
        session_id = task.current_session_id
        workspace = task.workspace
        pending_id = task.pending_message_id
        anchor = task.last_message_id
        cancel_task = _cancel_closes_task(task)
        environment = await resolve_environment(self.session, task) if session_id else None
        await self.session.commit()

        forced = False
        usage_messages: list[dict] | None = None
        native_count: int | None = None
        if session_id and environment:
            try:
                usage_messages, native_count, _ = await self._stop_native(
                    environment, workspace, session_id
                )
            except RuntimeStateError:
                raise
            except OpenCodeSessionNotFound:
                forced = True
            except Exception:
                if (
                    environment.mode == ExecutionMode.LOCAL
                    and not self._local_port_in_use(environment.opencode_server_port)
                ):
                    forced = True
                else:
                    logger.exception("Cancel reconcile could not reach Session %s", session_id)
                    raise

        task = await self.lock_task(task_id)
        if _task_result_concluded(task) or not _cancel_active(task):
            await self.session.rollback()
            return task
        if session_id and task.current_session_id != session_id:
            await self.session.rollback()
            return task
        if task.last_message_id != anchor or task.pending_message_id not in {None, pending_id}:
            await self.session.rollback()
            return task
        if _submission_still_unproven(task, usage_messages, forced=forced):
            await self.session.rollback()
            return task
        return await self._write_canceled_task(
            task,
            cancel_task=cancel_task,
            usage_messages=usage_messages,
            native_session_count=native_count,
            forced_offline_cancel=forced,
            stop_error=None,
        )

    async def _release_cancel_token(self, task_id: str, token: str) -> None:
        """Drop this cancel reservation after the stop attempt leaves the critical section."""
        await self.session.rollback()
        task = await self.lock_task(task_id)
        record = _cancel_record(task)
        if record and record.get("id") == token:
            _set_cancel_token(task, None)
            await self.session.commit()
        else:
            await self.session.rollback()

    async def _abandon_cancel(self, task: Task, token: str) -> None:
        """Clear only this cancel reservation. A newer submission stays reserved."""
        record = _cancel_record(task)
        if record and record.get("id") == token:
            _set_cancel_token(task, None)
            await self.session.commit()
        else:
            await self.session.rollback()

    async def stop(self, task_id, *, cancel_task: bool = True):
        task = await self.lock_task(task_id)
        if _task_result_concluded(task):
            await self.session.rollback()
            raise RuntimeStateError("任务已结束，没有正在执行的任务可终止")
        session_id = task.current_session_id
        workspace = task.workspace
        observed = (
            session_id,
            task.last_message_id,
            task.pending_message_id,
            task.turn_started_at,
        )
        token = token_hex(16)
        hold = get_settings().opencode_restart_abort_timeout_seconds + 300
        _set_cancel_token(task, token, hold_seconds=hold, close_task=cancel_task)
        environment = await resolve_environment(self.session, task) if session_id else None
        await self.session.commit()

        forced_offline_cancel = False
        stop_error = None
        usage_messages: list[dict] | None = None
        native_session_count: int | None = None
        token_held = True
        try:
            if session_id and environment:
                try:
                    usage_messages, native_session_count, _ = await self._stop_native(
                        environment, workspace, session_id
                    )
                except RuntimeStateError:
                    raise
                except OpenCodeSessionNotFound:
                    # A restarted Serve that cannot find the Session proves there is
                    # no live execution attached to this binding.
                    forced_offline_cancel = True
                except Exception as exc:
                    stop_error = exc
                    # Only LOCAL + no listener is safe to force-cancel. For remote
                    # transport loss or an occupied-but-unhealthy local port we cannot
                    # prove the Agent has stopped writing the Workspace.
                    if (
                        environment.mode == ExecutionMode.LOCAL
                        and not self._local_port_in_use(environment.opencode_server_port)
                    ):
                        forced_offline_cancel = True
                    else:
                        raise

            task = await self.lock_task(task_id)
            if session_id and task.current_session_id != session_id:
                await self._abandon_cancel(task, token)
                token_held = False
                raise RuntimeStateError("取消期间 Session 已切换，请刷新后重试")
            if not _same_observed_turn(task, observed) or not _cancel_active(task):
                await self._abandon_cancel(task, token)
                token_held = False
                raise RuntimeStateError("取消期间已有新的输入，未取消任务，也未释放 Worker")
            record = _cancel_record(task)
            if not record or record.get("id") != token:
                await self._abandon_cancel(task, token)
                token_held = False
                raise RuntimeStateError("取消期间已有新的输入，未取消任务，也未释放 Worker")
            if _task_result_concluded(task):
                await self._abandon_cancel(task, token)
                token_held = False
                raise RuntimeStateError("任务已结束")
            if _submission_still_unproven(task, usage_messages, forced=forced_offline_cancel):
                # The reserved POST has not reached OpenCode yet. Closing now would
                # drop the reservation while that request can still start execution.
                await self.session.rollback()
                token_held = False
                raise RuntimeStateError("取消中，在途提交结果未明，未关闭任务，也未释放 Worker")
            task = await self._write_canceled_task(
                task,
                cancel_task=cancel_task,
                usage_messages=usage_messages,
                native_session_count=native_session_count,
                forced_offline_cancel=forced_offline_cancel,
                stop_error=stop_error,
            )
            token_held = False
            return task
        except Exception as exc:
            if token_held:
                await self._release_cancel_token(task_id, token)
            if stop_error is not None and not isinstance(exc, RuntimeStateError):
                await self.disconnected(task_id, stop_error)
            raise

    async def refresh(self, task_id, *, client=None):
        task = await self.session.get(Task, task_id)
        if not task:
            raise RuntimeStateError("任务不存在")
        if not task.current_session_id or _task_result_concluded(task):
            return task
        if task.pending_message_id and not task.pending_prompt:
            task.runtime_error = "Session 启动尚未确认；请先停止，再显式重新开始"
            await self.session.commit()
            return task

        session_id = task.current_session_id
        workspace = task.workspace
        observed_anchor = task.last_message_id
        observed_turn = task.turn_started_at
        if client is None:
            environment = await resolve_environment(self.session, task)
            await self.session.commit()
            async with connect(environment, start=True) as connection:
                return await self.refresh(task_id, client=connection)

        # Do not keep even a read transaction open while waiting on Runtime I/O.
        await self.session.commit()
        directory = Path(workspace)
        try:
            status = await client.status(directory, session_id)
            allowed_sessions = await session_tree(client, directory, session_id)
            questions = await client.questions(directory)
            permissions = await client.permissions(directory)
        except OpenCodeSessionNotFound:
            task = await self.lock_task(task_id)
            if task.current_session_id == session_id and _cancel_active(task):
                pending_id = task.pending_message_id
                await self.session.rollback()
                return await self._complete_cancel(task_id, submission_id=pending_id)
            if task.current_session_id == session_id:
                task.runtime_connection = "UNAVAILABLE"
                task.runtime_error = (
                    "Session 不存在，连接异常，状态待确认。Worker 与 Workspace 仍保留，请人工处理。"
                )
                runtime_session = await self._runtime_session(task)
                missing = SessionState(
                    status=RuntimeSessionStatus.UNKNOWN.value,
                    terminal_reason=SessionTerminalReason.SESSION_MISSING.value,
                )
                if runtime_session and runtime_session.status in _TERMINAL_SESSION_STATUSES:
                    await self._project_session_state(task, missing)
                else:
                    await self.apply_session_state(task, missing)
            else:
                await self.session.rollback()
            raise

        has_pending = any(
            item.get("sessionID") in allowed_sessions for item in questions + permissions
        )
        task_snapshot = await self.lock_task(task_id)
        if (task_snapshot.current_session_id != session_id or task_snapshot.last_message_id != observed_anchor
                or task_snapshot.turn_started_at != observed_turn or _task_result_concluded(task_snapshot)):
            await self.session.rollback()
            return task_snapshot
        binding = await self._runtime_session(task_snapshot)
        settled = (
            status == "idle"
            and not has_pending
            and not task_snapshot.pending_message_id
            and binding is not None
        )
        canceling = _cancel_active(task_snapshot)
        if not canceling and settled and binding.status == RuntimeSessionStatus.SUCCEEDED.value and (
            task_snapshot.status in {TaskStatus.RUNNING, TaskStatus.WAITING}
        ):
            # A stored success still has to correct a stale business projection.
            # Once Task is already waiting for acceptance, skip another history read.
            task_snapshot.runtime_connection = "CONNECTED"
            task_snapshot.runtime_error = None
            recovered = SessionState(
                status=RuntimeSessionStatus.SUCCEEDED.value,
                terminal_reason=binding.terminal_reason or SessionTerminalReason.COMPLETED.value,
                finish_reason=binding.finish_reason,
                finished_at=binding.finished_at,
            )
            projected = (
                task_snapshot.status == TaskStatus.WAITING
                and task_snapshot.human_required_reason == _ACCEPTANCE_REASON
                and task_snapshot.current_progress == _ACCEPTANCE_REASON
            )
            # 传输恢复后仍走同一投影，清掉过期的 TRANSPORT_RECOVERY。
            if projected:
                await self.apply_session_state(task_snapshot, recovered)
            else:
                await self._project_session_state(task_snapshot, recovered)
            return task_snapshot
        confirmed_terminal = (
            binding and binding.terminal_reason != SessionTerminalReason.UNCONFIRMED.value
        )
        waiting_for_terminal = task_snapshot.status == TaskStatus.WAITING and binding and (
            binding.status in {RuntimeSessionStatus.FAILED.value, RuntimeSessionStatus.INTERRUPTED.value}
        )
        if not canceling and settled and confirmed_terminal and waiting_for_terminal:
            # unconfirmed is not a settled fact: a later transcript can still
            # show that this same round actually finished. A confirmed terminal
            # result replaces a stale transport-recovery reason through the
            # same projection.
            task_snapshot.runtime_connection = "CONNECTED"
            task_snapshot.runtime_error = None
            await self.apply_session_state(
                task_snapshot,
                SessionState(
                    status=binding.status,
                    terminal_reason=binding.terminal_reason,
                    finish_reason=binding.finish_reason,
                    finished_at=binding.finished_at,
                ),
            )
            return task_snapshot
        turn_started = task_snapshot.turn_started_at
        task_status = task_snapshot.status
        await self.session.commit()

        timed_out = False
        if (
            status in {"busy", "retry"}
            and not has_pending
            and task_status == TaskStatus.RUNNING
            and turn_started
        ):
            started = turn_started if turn_started.tzinfo else turn_started.replace(tzinfo=UTC)
            if (datetime.now(UTC) - started).total_seconds() >= get_settings().opencode_timeout_seconds:
                await client.abort(directory, session_id)
                timed_out = True
        if (
            not canceling
            and status in {"busy", "retry"}
            and not task_snapshot.pending_message_id
            and not timed_out
        ):
            task = await self.lock_task(task_id)
            if (task.current_session_id != session_id or task.last_message_id != observed_anchor
                    or task.turn_started_at != observed_turn or _task_result_concluded(task)):
                await self.session.rollback()
                return task
            task.runtime_connection = "CONNECTED"
            task.runtime_error = None
            observed = RuntimeSessionStatus.WAITING if has_pending else RuntimeSessionStatus.RUNNING
            await self.apply_session_state(task, SessionState(status=observed.value))
            return task
        try:
            messages = await self._all_messages(client, directory, session_id)
            usage_messages = list(messages)
            for child_session_id in sorted(allowed_sessions - {session_id}):
                try:
                    usage_messages.extend(
                        await self._all_messages(client, directory, child_session_id)
                    )
                except OpenCodeSessionNotFound:
                    continue
        except MessageHistoryIncomplete:
            task = await self.lock_task(task_id)
            if task.current_session_id == session_id and not _task_result_concluded(task):
                task.runtime_error = "消息没有读完，结果未知"
                await self.session.commit()
                return task
            await self.session.rollback()
            return task

        task = await self.lock_task(task_id)
        if (task.current_session_id != session_id or task.last_message_id != observed_anchor
                or task.turn_started_at != observed_turn or _task_result_concluded(task)):
            await self.session.rollback()
            return task
        if task.runtime_connection != "CONNECTED":
            await TaskService(self.session).add_event(
                task, "RUNTIME_CONNECTED", "runtime", "OpenCode Runtime 已连接", commit=False
            )
        task.runtime_connection = "CONNECTED"
        task.runtime_error = None
        if _task_result_concluded(task):
            await self.session.commit()
            return task

        await self._update_session_usage(
            task, usage_messages, native_session_count=len(allowed_sessions)
        )
        if _cancel_active(task):
            if _submission_still_unproven(task, messages):
                await self.session.commit()
                return task
            pending_id = task.pending_message_id
            await self.session.rollback()
            return await self._complete_cancel(task_id, submission_id=pending_id)
        if task.pending_message_id:
            if any(item["info"]["id"] == task.pending_message_id for item in messages):
                task.pending_message_id = None
                task.pending_prompt = None
            else:
                task.runtime_error = "输入尚未被 Session 确认，请核对后重试提交"
                await self.session.commit()
                return task
        state = derive_session_state(
            messages=messages,
            server_status=status,
            has_pending_question=has_pending,
            has_pending_permission=False,
            pending_ack=False,
            turn_anchor_id=task.last_message_id,
            turn_started_at=task.turn_started_at,
            timed_out=timed_out,
            grace_seconds=get_settings().opencode_no_reply_grace_seconds,
        )
        await self.apply_session_state(task, state)
        return task

    async def continue_task(self, task_id: str) -> Task:
        """人工继续沿用原来的 Worker、worktree 和 Session。"""

        task = await self.lock_task(task_id)
        if task.status not in {TaskStatus.WAITING.value, TaskStatus.RUNNING.value}:
            raise RuntimeStateError("只有未结束的任务可以继续")
        if not task.current_session_id or not task.workspace or not task.employee_id:
            raise RuntimeStateError("任务尚未绑定 Session，不能继续")
        session_id = task.current_session_id
        workspace = task.workspace
        employee_id = task.employee_id
        await self.session.commit()
        task = await self.lock_task(task_id)
        if (
            task.current_session_id != session_id
            or task.workspace != workspace
            or task.employee_id != employee_id
        ):
            await self.session.rollback()
            raise RuntimeStateError("任务绑定已变化")
        task.current_progress = "继续当前 Session"
        await TaskService(self.session).add_event(
            task,
            "TASK_CONTINUED",
            "work",
            "继续当前 Session",
            {"session_id": session_id, "workspace": workspace, "employee_id": employee_id},
            commit=False,
        )
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def confirm_completion(self, task_id: str):
        """Close the business task only on an explicit human action."""
        task = await self.session.get(Task, task_id)
        if not task:
            raise RuntimeStateError("任务不存在")
        if task.status == TaskStatus.SUCCEEDED:
            return task
        if task.status == TaskStatus.CANCELED or not task.current_session_id:
            raise RuntimeStateError("任务已终止或尚未启动")
        if task.pending_message_id:
            raise RuntimeStateError("仍有未确认输入，不能确认任务完成")
        session_id, workspace, anchor = task.current_session_id, task.workspace, task.last_message_id
        environment = await resolve_environment(self.session, task)
        await self.session.commit()
        async with connect(environment, start=True) as client:
            directory = Path(workspace)
            await client.session(directory, session_id)
            allowed = await session_tree(client, directory, session_id)
            allowed_sessions = sorted(allowed)
            for sid in allowed:
                if await client.status(directory, sid) in {"busy", "retry"}:
                    raise RuntimeStateError("Session 仍在执行，请先停止当前回复")
            pending = await client.questions(directory) + await client.permissions(directory)
            if any(item.get("sessionID") in allowed for item in pending):
                raise RuntimeStateError("仍有问题或权限请求，请先在 Workspace 处理")
            messages, count = await self._collect_final_usage(client, directory, session_id)
            # Capture the native root outcome before taking the Task lock. Usage
            # includes descendants; they must never determine the root's result.
            final_state = None
            try:
                root_messages = await client.messages(directory, session_id)
                final_state = derive_session_state(
                    messages=root_messages,
                    server_status="idle",
                    has_pending_question=False,
                    has_pending_permission=False,
                    pending_ack=False,
                    turn_anchor_id=anchor,
                    turn_started_at=task.turn_started_at,
                )
            except Exception:
                # Native existence, idle status and pending-input checks above
                # already permit human closure. Index/review capture must not
                # add another prerequisite for completing the business task.
                logger.exception("Failed to capture confirmed Session outcome for %s", session_id)
        task = await self.lock_task(task_id)
        if task.current_session_id != session_id or task.last_message_id != anchor or task.pending_message_id:
            await self.session.rollback()
            raise RuntimeStateError("确认期间 Session 或输入已变化，请重试")
        if task.status == TaskStatus.SUCCEEDED:
            return task
        if task.status == TaskStatus.CANCELED:
            await self.session.rollback()
            raise RuntimeStateError("任务已终止")
        if final_state is not None and final_state.status != RuntimeSessionStatus.SUCCEEDED.value:
            await self.session.rollback()
            raise RuntimeStateError(_confirm_blocked(final_state.status))
        if final_state is None:
            binding = await self._runtime_session(task)
            if not binding or binding.status != RuntimeSessionStatus.SUCCEEDED.value:
                await self.session.rollback()
                raise RuntimeStateError("当前轮结果无法确认，不能确认任务完成")
        if messages is not None:
            await self._update_session_usage(task, messages, native_session_count=count)
        if final_state is not None:
            await self._sync_session_state(task, final_state)
        task.confirmed_at = datetime.now(UTC)
        task.completed_at = task.completed_at or task.confirmed_at
        task.human_required_reason = None
        task.human_intervention = None
        # 投影之外只在确认和取消时清空 waiting_reason。
        task.waiting_reason = None
        if get_settings().experience_review_enabled:
            from app.runtime.experience_review.coordinator import enqueue_in_transaction

            confirmed_at = task.confirmed_at
            snapshot = {
                "task_id": task.id,
                "task": {
                    "id": task.id,
                    "title": task.title,
                    "description": task.description,
                    "task_type": task.task_type,
                    "category_id": task.category_id,
                    "repository_id": task.repository_id,
                    "employee_id": task.employee_id,
                },
                "root_session_id": session_id,
                "source_session_ids": allowed_sessions,
                "employee_id": task.employee_id,
                "workspace": workspace,
                "turn_anchor_id": anchor,
                "turn_started_at": task.turn_started_at.isoformat() if task.turn_started_at else None,
                "confirmed_at": confirmed_at.isoformat(),
                "native_watermarks": {session_id: anchor or ""},
            }
            event = await TaskService(self.session).add_event(
                task,
                "TASK_COMPLETION_CONFIRMED",
                "review",
                "人工确认完成，待经验复盘",
                snapshot,
                commit=False,
            )
            await self.session.flush()
            await enqueue_in_transaction(self.session, task=task, event=event, snapshot=snapshot)
        await self.project(task, TaskStatus.SUCCEEDED, detail="任务已人工确认完成")
        return task

    async def disconnected(self, task_id, error):
        task = await self.lock_task(task_id)
        if task.runtime_connection != "UNAVAILABLE":
            await TaskService(self.session).add_event(
                task,
                "RUNTIME_DISCONNECTED",
                "runtime",
                "OpenCode Runtime 连接中断，保留任务状态",
                commit=False,
            )
        task.runtime_connection = "UNAVAILABLE"
        task.runtime_error = f"连接异常，状态待确认：{error}"
        runtime_session = await self._runtime_session(task)
        lost = SessionState(
            status=RuntimeSessionStatus.UNKNOWN.value,
            terminal_reason=SessionTerminalReason.TRANSPORT_UNKNOWN.value,
        )
        if runtime_session and runtime_session.status in _TERMINAL_SESSION_STATUSES:
            await self._project_session_state(task, lost)
        else:
            await self.apply_session_state(task, lost)

    async def confirm_execution_stop(self, task_id: str, *, operator: str, reason: str) -> Task:
        """Record a human stop confirmation. This does not change Task status."""

        operator = operator.strip()
        reason = reason.strip()
        if not operator or not reason:
            raise RuntimeStateError("确认执行停止需要操作人和原因")
        task = await self.lock_task(task_id)
        if task.status != TaskStatus.CANCELED and not _cancel_active(task):
            await self.session.rollback()
            raise RuntimeStateError("只能确认已取消或正在取消的任务已经停止")
        now = datetime.now(UTC)
        task.execution_stop_confirmed_at = now
        task.execution_stop_session_id = task.current_session_id
        task.execution_stop_attempt = task.pending_attempt
        task.execution_stop_operator = operator
        task.execution_stop_reason = reason
        await TaskService(self.session).add_event(
            task,
            "EXECUTION_STOP_CONFIRMED",
            "work",
            "人工确认执行已停止",
            {
                "operator": operator,
                "reason": reason,
                "session_id": task.current_session_id,
                "pending_attempt": task.pending_attempt,
            },
            commit=False,
        )
        await sync_employee_occupancy(self.session, task.employee_id)
        await self.session.commit()
        return task

    async def refresh_employee_heartbeats(self):
        employees = (
            await self.session.scalars(
                select(AgentWorker)
                .options(selectinload(AgentWorker.execution_environment))
                .where(AgentWorker.enabled.is_(True))
            )
        ).all()
        # The initial SELECT starts a transaction. Close it before any network
        # probe so no autoflushed employee UPDATE can remain locked while a later
        # OpenCode health check stalls.
        await self.session.commit()

        healthy: list[str] = []
        for employee in employees:
            environment = employee.execution_environment
            if not environment:
                continue

            occupying = await self.session.scalar(
                select(Task.id).where(occupying_clause(employee.id)).limit(1)
            )
            await self.session.commit()

            # Employee Serve is intentionally on-demand. No occupying task means an
            # absent OpenCode listener is normal; rely on the last explicit
            # environment check instead of marking the employee OFFLINE.
            if occupying is None:
                fresh = await self.session.get(AgentWorker, employee.id)
                if fresh:
                    await sync_employee_occupancy(self.session, fresh.id)
                    if fresh.status not in {EmployeeStatus.PAUSED, EmployeeStatus.ERROR}:
                        if environment.status == "AVAILABLE":
                            fresh.status = EmployeeStatus.IDLE
                            fresh.last_heartbeat = datetime.now(UTC)
                            healthy.append(fresh.id)
                        else:
                            fresh.status = EmployeeStatus.OFFLINE
                await self.session.commit()
                continue

            try:
                # Active work expects a Serve. Start/recover it if necessary,
                # then verify health without holding any database transaction.
                async with connect(environment, start=True) as client:
                    await client.health()
                probe_error = None
            except Exception as exc:
                probe_error = exc

            fresh = await self.session.get(AgentWorker, employee.id)
            if not fresh:
                await self.session.rollback()
                continue
            await sync_employee_occupancy(self.session, fresh.id)
            current = await self.session.scalar(
                select(Task).where(occupying_clause(fresh.id)).limit(1)
            )
            if probe_error is not None:
                if fresh.status not in {EmployeeStatus.PAUSED, EmployeeStatus.ERROR}:
                    fresh.status = EmployeeStatus.OFFLINE
                await self.session.commit()
                continue

            fresh.last_heartbeat = datetime.now(UTC)
            healthy.append(fresh.id)
            if fresh.status not in {EmployeeStatus.PAUSED, EmployeeStatus.ERROR}:
                fresh.status = (
                    EmployeeStatus.WAIT_HUMAN
                    if current and current.status == TaskStatus.WAITING
                    else EmployeeStatus.WORKING
                    if current
                    else EmployeeStatus.IDLE
                )
            await self.session.commit()
        return healthy

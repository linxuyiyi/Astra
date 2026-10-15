# -*- coding: utf-8 -*-
"""运行 admission 冻结、Task 排空与落盘核对（OpenSpec 任务 3.2.3.1）。

本模块是删除前置屏障（非删除本身），只负责在员工进入删除中后：

* **admission 短栅栏**：独立 ASGI 中间件在真实 ``POST /chat/`` 上按 ``agent_id``
  取得每 Agent 短栅栏，核对可信 Nexus 事实后才放行下游；栅栏覆盖下游 handler
  **真正完成**（原生 ``registry.spawn`` 登记真实 Task 后返回 ``started``），
  不把 SSE 纳入；外层 HTTP 取消时 shield 真实下游 Task 直到其退出再释放栅栏，
  避免「已受理但尚未登记」的输入被漏掉。不设会破坏未来 6×20MiB 附件的极小
  请求上限。
* **同一栅栏的 freeze/quiesce**：与 admission 共用每 Agent ``asyncio.Lock``，
  核对 Nexus ``deleting`` 与稳定操作身份后置服务临时冻结，捕获该 Agent 所有
  真实 Session 与 Task，**立即释放栅栏再等待**。服务冻结集合不是事实源：重启
  后 admission 与 Bus 首次/实际入锁仍查 Nexus，查询不可用一律 fail-closed。
* **Bus 迟到来锁拒绝**：由 :class:`~nexus_agent_service.persistence_observation.ObservedMessageBus`
  在 Session 运行锁等待前与取得后调用 :meth:`FreezeController.session_blocked`，
  阻止迟到的 native wakeup 进入执行或生成合成 error 历史（原生该锁位于 error
  reporter ``try`` 之外）。
* **正确排空**：同一操作对同一个真实 Task 只请求一次取消（保存真实对象身份，
  不依赖 ``cancelling`` 计数）；重试只 shield 等待，``wait_for(shield(task))``
  超时**不**把取消传递给真实 Task/``_persist``；等待真实 Task 结束后再核对
  registry、Session 锁与 BG registry 自然清理（BG done callback 先 pop 本地
  handle 再异步 ``registry_del``，绝不主动 drop 制造空）。必要循环只等待事实
  变化，有总 deadline。
* **保存证明**：对每个可信 Binding 调用已验收 ``readback_verify`` 核对所有
  未确认条目；未知/缺失/不一致或索引错误一律阻止确认。旧 pending 绝不被新
  checkpoint 清零。返回最小脱敏事实，不含 Msg/State 全文、配置或密钥。

边界（研究第 17 节）：本模块只证明 ``execution_drained`` 与
``persistence_verified``；``accepted_input_archival`` 明确 ``not_verified``——
HITL 触发只 enqueue、Dispatcher 破坏性 drain 后 spawn，冻结拒绝时输入可能已
离队且未 persist，公开队列没有 peek/count，绝不 drain/drop 偷看或填 0。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Awaitable, Callable, Protocol

import httpx

from agentscope.app.message_bus import MessageBusKeys

from .persistence_index import Binding
from .persistence_observation import readback_verify
from .write_barrier import _buffer_body, _send_json, await_real_task

logger = logging.getLogger("nexus_agent_service.freeze")

TOKEN_HEADER = "X-Nexus-Internal-Token"
USER_HEADER = "X-User-ID"
RUN_FACT_PATH = "/api/internal/run-fact"
CHAT_PATH = "/chat/"

# 轮询间隔与默认排空预算。总 deadline 是硬边界，循环只等待事实变化。
POLL_INTERVAL = 0.02
DEFAULT_QUIESCE_TIMEOUT = 20.0


class FreezeError(RuntimeError):
    """冻结/排空过程的基础错误。"""


class FreezeRejectedError(FreezeError):
    """Nexus 事实不满足或请求绑定不一致，冻结被拒绝。"""


class RunFrozenError(FreezeError):
    """Session 运行锁因冻结被拒绝；位于原生 error reporter try 之外。"""


@dataclass(frozen=True)
class RunFact:
    """Nexus 只读运行事实（来自 ``/api/internal/run-fact``）。"""

    runnable: bool
    deleting: bool
    operation_id: str | None
    agent_id: str | None
    employee_id: str | None
    session_id: str | None
    reason: str

    def matches_operation(self, agent_id: str, operation_id: str) -> bool:
        return (
            self.deleting
            and self.agent_id == agent_id
            and self.operation_id is not None
            and self.operation_id == operation_id
        )


def _denied_fact(reason: str) -> RunFact:
    return RunFact(
        runnable=False,
        deleting=False,
        operation_id=None,
        agent_id=None,
        employee_id=None,
        session_id=None,
        reason=reason,
    )


class FreezeVerifier(Protocol):
    """Nexus 事实核对接口。实现必须 fail-closed。"""

    async def check(
        self,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> RunFact: ...

    async def aclose(self) -> None: ...


class AllowAllFreezeVerifier:
    """未配置内部秘密时的放行核对器（不启用删除屏障，仅保留既有行为）。

    仅在**没有**配置内部秘密时使用：此时删除流程根本无法被 Nexus 触发，因此
    不引入额外拒绝；一旦配置秘密，正式装配使用 :class:`NexusFreezeVerifier`，
    任何查询失败都会 fail-closed。
    """

    async def check(
        self,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> RunFact:
        return RunFact(
            runnable=True,
            deleting=False,
            operation_id=None,
            agent_id=agent_id,
            employee_id=None,
            session_id=session_id,
            reason="freeze_unconfigured",
        )

    async def aclose(self) -> None:  # pragma: no cover - 无资源
        return None


class NexusFreezeVerifier:
    """通过 HTTP 只读核对 Nexus 真实运行事实的默认核对器。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        user_id: str,
        *,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._user_id = user_id
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            trust_env=False,
            timeout=httpx.Timeout(timeout),
        )

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def check(
        self,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> RunFact:
        if not agent_id and not session_id:
            return _denied_fact("missing_identity")
        try:
            response = await self._client.post(
                RUN_FACT_PATH,
                json={"agent_id": agent_id, "session_id": session_id},
                headers={
                    TOKEN_HEADER: self._token,
                    USER_HEADER: self._user_id,
                },
            )
        except httpx.HTTPError:
            return _denied_fact("freeze_unavailable")
        if response.status_code != 200:
            return _denied_fact("freeze_denied")
        try:
            payload = response.json()
        except ValueError:
            return _denied_fact("freeze_invalid")
        if not isinstance(payload, dict):
            return _denied_fact("freeze_invalid")
        return RunFact(
            runnable=payload.get("runnable") is True,
            deleting=payload.get("deleting") is True,
            operation_id=_opt_str(payload.get("operation_id")),
            agent_id=_opt_str(payload.get("agent_id")),
            employee_id=_opt_str(payload.get("employee_id")),
            session_id=_opt_str(payload.get("session_id")),
            reason="ok",
        )


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


# ----------------------------------------------------------------------
# 单 Session 排空报告
# ----------------------------------------------------------------------
@dataclass
class SessionDrainState:
    session_id: str
    chat_task: asyncio.Task | None
    bg_tasks: list[Any] = field(default_factory=list)
    # 捕获瞬间的真实存活事实：供报告证明冻结捕获的是**在途**真实 Task，而非
    # 恰好已 idle 的会话；终态报告仍以实时 ``done()`` 为准。
    chat_alive_at_capture: bool = False
    bg_alive_at_capture: int = 0


@dataclass
class QuiesceReport:
    """一次 freeze/quiesce 的最小脱敏事实。"""

    operation_id: str
    agent_id: str
    generation_id: str | None
    sessions: list[dict] = field(default_factory=list)
    execution_drained: bool = False
    persistence_verified: bool = False
    detail: str = ""
    accepted_input_archival: str = "not_verified"

    def to_dict(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "agent_id": self.agent_id,
            "generation_id": self.generation_id,
            "sessions": list(self.sessions),
            "execution_drained": bool(self.execution_drained),
            "persistence_verified": bool(self.persistence_verified),
            "accepted_input_archival": self.accepted_input_archival,
            "detail": self.detail,
        }


class FreezeController:
    """服务侧每 Agent admission 栅栏 + 冻结集合 + 排空编排。

    栅栏是 ``asyncio.Lock``（每 Agent 一个），admission 与 freeze 共用；冻结
    集合只是**临时屏障**，不是事实源——每次放行/入锁仍向 Nexus 查询。
    """

    def __init__(
        self,
        app: Any,
        verifier: FreezeVerifier,
        *,
        user_id: str,
        generation_id: str | None = None,
    ) -> None:
        self._app = app
        self._verifier = verifier
        self._user_id = user_id
        self._generation_id = generation_id
        self._fences: dict[str, asyncio.Lock] = {}
        self._frozen_agents: dict[str, str] = {}
        self._frozen_sessions: set[str] = set()
        # 每个 Agent 本次操作冻结的 Session，便于显式释放时精确回收。
        self._agent_sessions: dict[str, set[str]] = {}
        # 已请求取消的真实 Task 身份（id(task) → task），同一操作/重试只请求
        # 一次取消；不依赖 ``task.cancelling()``（原生工具可能 uncancel）。
        self._cancel_requested: dict[int, asyncio.Task] = {}

    def _fence(self, agent_id: str) -> asyncio.Lock:
        lock = self._fences.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._fences[agent_id] = lock
        return lock

    # ------------------------------------------------------------------
    # Admission / Bus 查询
    # ------------------------------------------------------------------
    async def check_binding(
        self,
        agent_id: str,
        session_id: str,
    ) -> RunFact:
        """核对单个可信 Agent/Session 绑定（admission 使用）。"""
        try:
            fact = await self._verifier.check(
                agent_id=agent_id,
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001 - 一律 fail-closed
            return _denied_fact("freeze_error")
        return fact

    @asynccontextmanager
    async def admission_guard(self, agent_id: str) -> AsyncGenerator[None, None]:
        """每 Agent 短栅栏：覆盖下游 handler 真正完成后再释放。"""
        async with self._fence(agent_id):
            yield

    async def session_blocked(self, session_id: str) -> bool:
        """Session 运行锁放行判断：本地冻结集合 + 真实 Nexus 事实。

        本地集合只是快速否定；未命中时必须查 Nexus（重启后集合为空亦然），
        查询异常/未知绑定一律拒绝。
        """
        if session_id in self._frozen_sessions:
            return True
        try:
            fact = await self._verifier.check(session_id=session_id)
        except Exception:  # noqa: BLE001 - 查询失败不得因空缓冲放行
            return True
        if fact.deleting or not fact.runnable:
            return True
        if fact.agent_id is not None and fact.agent_id in self._frozen_agents:
            return True
        return False

    # ------------------------------------------------------------------
    # Freeze / quiesce
    # ------------------------------------------------------------------
    async def freeze_and_quiesce(
        self,
        *,
        operation_id: str,
        agent_id: str,
        session_ids: list[str],
        timeout: float = DEFAULT_QUIESCE_TIMEOUT,
    ) -> QuiesceReport:
        """核对 Nexus 事实后冻结并排空该 Agent 的真实运行。"""
        # 控制器直调也必须拒绝非法超时（bool/NaN/Infinity/<=0）：否则会创建
        # 无界 drain Task 或让总 deadline 失效。
        if isinstance(timeout, bool):
            raise FreezeError("冻结超时非法。")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError):
            raise FreezeError("冻结超时非法。")
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise FreezeError("冻结超时非法。")
        fence = self._fence(agent_id)
        async with fence:
            fact = await self._verifier.check(agent_id=agent_id)
            if not fact.matches_operation(agent_id, operation_id):
                raise FreezeRejectedError(
                    "Nexus 删除事实或操作身份不一致，拒绝冻结。",
                )
            targets = await self._resolve_targets(agent_id, session_ids)
            self._frozen_agents[agent_id] = operation_id
            self._frozen_sessions.update(targets)
            self._agent_sessions[agent_id] = set(targets)
            captured = self._capture(agent_id, targets)
        # 栅栏立即释放，之后才发起取消与等待。
        #
        # 真实排空在独立 Task 中进行：冻结请求被取消/超时时只 shield 等待，
        # 绝不打断真实 Task（native ``_persist`` 在会话锁内完成）、绝不提前
        # 释放栅栏或伪造完成；真实任务有界完成/失败后才如实向外传播外层取消。
        drain_task = asyncio.ensure_future(
            self._drain(
                operation_id=operation_id,
                agent_id=agent_id,
                captured=captured,
                timeout=timeout_value,
            ),
        )
        await await_real_task(drain_task)
        return drain_task.result()

    def release(self, agent_id: str, operation_id: str) -> bool:
        """显式释放服务临时冻结（仅用于受控恢复/测试；重启即清空）。"""
        if self._frozen_agents.get(agent_id) != operation_id:
            return False
        self._frozen_agents.pop(agent_id, None)
        for sid in self._agent_sessions.pop(agent_id, set()):
            self._frozen_sessions.discard(sid)
        return True

    async def _resolve_targets(
        self,
        agent_id: str,
        session_ids: list[str],
    ) -> list[str]:
        """合并该 Agent 的真实 Session 与请求声明的 Session。

        请求声明的 Session 必须确实属于该 Agent（与 Storage 绑定精确一致），
        否则拒绝；同时捕获该 Agent 的**所有**真实 Session，避免漏掉未声明但
        仍可能有运行的任务。
        """
        storage = self._app.state.storage
        records = await storage.list_sessions(self._user_id, agent_id)
        real: set[str] = set()
        for record in records:
            sid = getattr(record, "id", None)
            if isinstance(sid, str) and sid:
                real.add(sid)
        for sid in session_ids:
            if sid not in real:
                raise FreezeRejectedError(
                    "请求的 Session 与真实 Nexus/Storage 绑定不一致，拒绝冻结。",
                )
        return sorted(real | set(session_ids))

    def _capture(
        self,
        agent_id: str,
        targets: list[str],
    ) -> dict[str, SessionDrainState]:
        registry = self._app.state.chat_run_registry
        bg_manager = self._app.state.background_task_manager
        captured: dict[str, SessionDrainState] = {}
        for sid in targets:
            task = registry.get(sid)
            bg_tasks = [
                bg
                for bg in bg_manager.tasks.values()
                if bg.session_id == sid and bg.agent_id == agent_id
            ]
            captured[sid] = SessionDrainState(
                session_id=sid,
                chat_task=task,
                bg_tasks=bg_tasks,
                chat_alive_at_capture=task is not None and not task.done(),
                bg_alive_at_capture=sum(
                    1 for bg in bg_tasks if not bg.asyncio_task.done()
                ),
            )
        return captured

    def _request_cancel_once(self, task: asyncio.Task) -> None:
        """对同一真实 Task 只请求一次取消；已在取消中的 Task 只登记不二次取消。

        幂等集合以真实对象身份为准（``id(task)`` → task），不依赖
        ``task.cancelling()``：native 工具可能 ``uncancel``，之后重试仍不应再
        取消。但**首次接管**一个已处于取消中（``cancelling() > 0``，可能正在
        ``finally`` 保存）的 Task 时，只登记对象并等待其自然结束，绝不再次
        ``cancel()``，避免 cancelling 计数增加打断真实持久化尾部。
        """
        key = id(task)
        if key in self._cancel_requested:
            return
        self._cancel_requested[key] = task
        if task.done() or task.cancelling() > 0:
            return
        task.cancel()

    async def _await_task(
        self,
        task: asyncio.Task,
        deadline: float,
    ) -> None:
        """shield 等待真实 Task 结束；外层取消不打断真实 Task，退出后如实传播。

        与 :func:`await_real_task` 同一语义：外层取消只记住，真实 Task 有界
        完成/失败后才传播；超时只返回，绝不把取消传给真实 Task/``_persist``。
        """
        loop = asyncio.get_event_loop()
        outer_cancelled: asyncio.CancelledError | None = None
        while not task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                # ``asyncio.wait`` 只等待完成，不读取也不传播真实 Task 自身的
                # 取消/异常，因此能把「外层取消」与「真实 Task 自身取消结束」
                # 明确区分：前者在此处抛出，后者只是完成。
                await asyncio.wait(
                    {task},
                    timeout=min(remaining, POLL_INTERVAL),
                )
            except asyncio.CancelledError as exc:
                # 外层取消：真实 Task 继续运行；记住并等待其退出后传播。
                outer_cancelled = outer_cancelled or exc
                continue
        if outer_cancelled is not None:
            raise outer_cancelled

    async def _drain(
        self,
        *,
        operation_id: str,
        agent_id: str,
        captured: dict[str, SessionDrainState],
        timeout: float,
    ) -> QuiesceReport:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(timeout, 0.0)

        # 对每个真实 Task 只请求一次取消（chat 与 BG 一致）。
        for state in captured.values():
            if state.chat_task is not None:
                self._request_cancel_once(state.chat_task)
            for bg in state.bg_tasks:
                self._request_cancel_once(bg.asyncio_task)

        # 等待所有真实 Task 退出（只 shield 等待，不二次取消）。
        for state in captured.values():
            if state.chat_task is not None:
                await self._await_task(state.chat_task, deadline)
            for bg in state.bg_tasks:
                await self._await_task(bg.asyncio_task, deadline)

        # 等待 registry / Session 锁 / BG registry 自然清理（有总 deadline）。
        while True:
            clean = await self._all_clean(agent_id, captured)
            if clean:
                break
            if loop.time() >= deadline:
                break
            await asyncio.sleep(POLL_INTERVAL)

        return await self._build_report(
            operation_id=operation_id,
            agent_id=agent_id,
            captured=captured,
        )

    async def _session_clean(
        self,
        agent_id: str,
        sid: str,
        state: SessionDrainState,
    ) -> tuple[bool, dict]:
        """核对单 Session 是否真实排空（捕获 Task/实时 registry/锁/BG）。

        返回 ``(clean, facts)``；``facts`` 为脱敏存活事实。**捕获的真实 Task
        是否结束始终计入排空判断**（不因 registry 已清理而漏判），实时
        registry/锁/BG 只是补充事实。
        """
        bus = self._app.state.message_bus
        registry = self._app.state.chat_run_registry
        bg_manager = self._app.state.background_task_manager

        chat_task = state.chat_task
        captured_chat_alive = chat_task is not None and not chat_task.done()
        captured_bg_alive_ids = [
            getattr(bg, "id", "")
            for bg in state.bg_tasks
            if not bg.asyncio_task.done()
        ]
        registry_present = registry.get(sid) is not None
        session_locked = await bus.is_locked(MessageBusKeys.session_lock(sid))
        live_bg = await bus.registry_getall(MessageBusKeys.bg_tasks(sid))
        bg_ids = sorted(
            key for key, value in live_bg.items() if isinstance(value, str)
        )
        local_bg = [
            bg.id
            for bg in bg_manager.tasks.values()
            if bg.session_id == sid and bg.agent_id == agent_id
        ]
        clean = not (
            captured_chat_alive
            or captured_bg_alive_ids
            or registry_present
            or session_locked
            or bg_ids
            or local_bg
        )
        facts = {
            "captured_chat_present": chat_task is not None,
            "captured_chat_alive": captured_chat_alive,
            "captured_chat_alive_at_capture": state.chat_alive_at_capture,
            "captured_bg_task_ids": sorted(
                getattr(bg, "id", "") for bg in state.bg_tasks
            ),
            "captured_bg_alive_ids": captured_bg_alive_ids,
            "captured_bg_alive_at_capture": state.bg_alive_at_capture,
            "chat_task_present": registry_present,
            "session_locked": session_locked,
            "bg_task_ids": bg_ids,
            "local_bg_task_ids": local_bg,
        }
        return clean, facts

    async def _all_clean(
        self,
        agent_id: str,
        captured: dict[str, SessionDrainState],
    ) -> bool:
        for sid, state in captured.items():
            clean, _ = await self._session_clean(agent_id, sid, state)
            if not clean:
                return False
        return True

    async def _collect_pending(
        self,
        index: Any,
        *,
        agent_id: str,
        sid: str,
        session_entry: dict,
        reason_parts: list[str],
        reason: str,
    ) -> None:
        """列出未确认条目（不改动索引），供未排空时明确“仍需等待”。"""
        binding = Binding(
            user_id=self._user_id,
            agent_id=agent_id,
            session_id=sid,
        )
        try:
            entries = await index.unconfirmed(binding)
        except Exception as exc:  # noqa: BLE001 - 读取失败也必须阻断
            reason_parts.append(f"pending_read_error:{type(exc).__name__}")
            return
        for entry in entries:
            session_entry["pending"].append(
                {
                    "entry_id": entry.entry_id,
                    "kind": entry.kind,
                    "subject_id": entry.subject_id,
                    "reason": reason,
                },
            )

    async def _build_report(
        self,
        *,
        operation_id: str,
        agent_id: str,
        captured: dict[str, SessionDrainState],
    ) -> QuiesceReport:
        storage = self._app.state.storage
        index = self._app.state.nexus_persistence_index

        report = QuiesceReport(
            operation_id=operation_id,
            agent_id=agent_id,
            generation_id=self._generation_id,
        )
        execution_drained = True
        persistence_verified = True
        entries_by_sid: dict[str, dict] = {}

        for sid, state in captured.items():
            # 只在**执行真实排空后**才读回确认：任何捕获 Task/实时 registry/锁/
            # BG 未排空都保留 pending、不 set_digest/confirm，明确仍需等待；
            # 绝不在停写前读回确认，也绝不把 execution drain 当已受理输入归档。
            clean, facts = await self._session_clean(agent_id, sid, state)
            session_entry: dict = {
                "session_id": sid,
                **facts,
                "pending": [],
                "persistence_verified": False,
                "reasons": [],
            }
            entries_by_sid[sid] = session_entry
            reason_parts: list[str] = session_entry["reasons"]

            record = await storage.get_session(self._user_id, agent_id, sid)
            if not clean:
                execution_drained = False
                persistence_verified = False
                reason_parts.append("execution_not_drained")
                if record is None:
                    reason_parts.append("session_missing")
                else:
                    await self._collect_pending(
                        index,
                        agent_id=agent_id,
                        sid=sid,
                        session_entry=session_entry,
                        reason_parts=reason_parts,
                        reason="execution_not_drained",
                    )
                report.sessions.append(session_entry)
                continue

            session_verified = True
            if record is None:
                session_verified = False
                reason_parts.append("session_missing")
            else:
                binding = Binding(
                    user_id=self._user_id,
                    agent_id=agent_id,
                    session_id=sid,
                )
                try:
                    result = await readback_verify(index, storage, binding)
                except Exception as exc:  # noqa: BLE001 - 索引/读回失败必须阻断
                    session_verified = False
                    reason_parts.append(f"readback_error:{type(exc).__name__}")
                else:
                    for entry in result.blocked:
                        session_entry["pending"].append(
                            {
                                "entry_id": entry.entry_id,
                                "kind": entry.kind,
                                "subject_id": entry.subject_id,
                                "reason": entry.reason,
                            },
                        )
                    if result.blocked:
                        session_verified = False
                        reason_parts.append("persistence_unconfirmed")

                # 读回之后立即核对本 Session 真实 Task/registry/锁/BG。
                recheck_clean, _ = await self._session_clean(
                    agent_id,
                    sid,
                    state,
                )
                if not recheck_clean:
                    execution_drained = False
                    session_verified = False
                    reason_parts.append("execution_changed_during_readback")

            session_entry["persistence_verified"] = session_verified
            if not session_verified:
                persistence_verified = False
            report.sessions.append(session_entry)

        # 全部读回完成、设置返回报告前，统一复核**全部** captured 与实时
        # Task/registry/锁/BG：多 Session 交错时，读回窗口内新登记的真实 Task
        # （例如 s2 读回期间落在 s1 registry 上的 late Task）不得被逐 Session
        # 的旧快照漏判。非 clean 阻断并**刷新行事实**，绝不再沿用旧的存活 false。
        for sid in list(captured):
            entry = entries_by_sid[sid]
            live_clean, live_facts = await self._session_clean(
                agent_id,
                sid,
                captured[sid],
            )
            entry.update(live_facts)
            if not live_clean:
                execution_drained = False
                persistence_verified = False
                entry["persistence_verified"] = False
                if (
                    "execution_not_drained" not in entry["reasons"]
                    and "execution_changed_during_readback"
                    not in entry["reasons"]
                ):
                    entry["reasons"].append(
                        "execution_changed_during_readback",
                    )

        report.execution_drained = execution_drained
        report.persistence_verified = persistence_verified
        if not (execution_drained and persistence_verified):
            report.detail = (
                "存在捕获的真实 Task/运行锁/BG 未排空或写入未确认，禁止进入"
                "归档/删除；已受理输入归档仍为 not_verified。"
            )
        else:
            report.detail = (
                "chat 运行、Session 锁与已登记 BG registry 已排空且写入已确认；"
                "未登记的原生后台投递器/定时器不在此范围，已受理输入归档仍为"
                "not_verified。"
            )
        return report


# ----------------------------------------------------------------------
# Admission 中间件（独立 ASGI，不改官方 router）
# ----------------------------------------------------------------------
def _chat_binding(body: bytes) -> tuple[str | None, str | None]:
    """从真实 POST /chat 请求体解析可信 agent/session 标识。

    只读结构化字段；解析失败/缺失即返回 ``None``，由调用方 fail-closed。
    """
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    agent_id = payload.get("agent_id")
    session_id = payload.get("session_id")
    return (
        agent_id if isinstance(agent_id, str) and agent_id else None,
        session_id if isinstance(session_id, str) and session_id else None,
    )


class AdmissionMiddleware:
    """在真实 ``POST /chat/`` 上按 Agent 短栅栏执行 admission 的 ASGI 中间件。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict,
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") != "POST" or scope.get("path") != CHAT_PATH:
            await self.app(scope, receive, send)
            return

        body, replay = await _buffer_body(receive)
        agent_id, session_id = _chat_binding(body)
        if agent_id is None or session_id is None:
            await _send_json(
                send,
                403,
                "运行请求缺少可信 Agent/Session 绑定，已拒绝。",
            )
            return

        app_obj = scope.get("app")
        controller: FreezeController | None = getattr(
            getattr(app_obj, "state", None),
            "nexus_freeze_controller",
            None,
        )
        if controller is None:
            await _send_json(
                send,
                503,
                "运行 admission 未就绪，已拒绝。",
            )
            return

        async with controller.admission_guard(agent_id):
            fact = await controller.check_binding(agent_id, session_id)
            if (
                not fact.runnable
                or fact.deleting
                or fact.agent_id != agent_id
            ):
                await _send_json(
                    send,
                    409,
                    "员工当前不可运行或正在删除，已拒绝本次运行。",
                )
                return
            # 栅栏覆盖下游 handler 真正完成（原生 registry.spawn 登记 Task 后
            # 才返回 started）；外层取消时 shield 真实任务，避免漏掉已受理
            # 尚未登记的输入。
            task = asyncio.ensure_future(self.app(scope, replay, send))
            await await_real_task(task)

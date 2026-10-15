# -*- coding: utf-8 -*-
"""原生持久化写入观察接入（OpenSpec 任务 3.2.3.1）。

只通过**公开扩展点**观察官方运行是否真实提交消息与会话状态，不复制官方
实现、不 monkeypatch 生产原生函数、不导入官方私有 ``_service``/``_manager``：

* :class:`ObservedMessageBus` 继承公开 ``InMemoryMessageBus``，**先真正取得
  原生 Session 锁再建立/设置本轮唯一 ``checkpoint_id`` 与共享观察对象**
  （``ContextVar``），任何 ``__aenter__`` 失败/取消都不遗留 scope；非 Session
  运行锁（checkpoint/inbox）一律透传。只有**真实 owner Task** 才能按既有重入
  语义使用该 scope；继承 scope 的子 Task 不得宣称持有运行锁（明确拒绝，避免
  源任务等待子任务的锁环），不同 Session/已封盘同样拒绝。子任务
  ``asyncio.create_task`` 继承同一观察对象，因此 ``_persist`` 里的真实写入仍
  归属同一 scope。Session 运行锁退出时先**同步封盘**拒绝新受理，再可靠等待
  所有已受理 writer **归零**（内部 drain waiter 被取消/失败也不能当成排空完成），
  之后才释放原生 Session 锁并 reset 上下文。已封盘/关闭 scope 上的迟到观察/
  写入（含 session 身份不符）一律 **fail-closed 明确拒绝**，绝不并入新 scope、
  也不伪装成独立健康条目或复用已确认条目。
* :class:`ObservationMiddleware`（公开 ``MiddlewareBase.on_reply``）以可信闭包
  ``(user_id, agent_id, session_id)`` 与真实 ``get_session`` 记录校验身份，向
  同 scope 追加每个真实回复事件，并建立该 scope 唯一的未知摘要 State 期待项；
  scope 缺失/封盘/关闭时明确拒绝，绝不静默关闭观察再继续运行模型。
* 更早的公开切点：``register_inbox_consumer`` 只调用
  ``registry_set(inbox_consumer(sid), INBOX_CONSUMER_FIELD, "1", ttl_secs=...)``，
  Bus 在精确匹配当前运行 scope 时先 ensure 同一条 State 期待，再委托原生，
  覆盖「user 输入写入」到「on_reply」之间的缺口。
* :class:`ObservedStorage`（公开 ``AsyncSQLAlchemyStorage`` 子类）的每个
  message/State writer 在首次 ``await`` 前受理**内存 writer lease**（覆盖绑定
  读取/索引/native super 真实 commit/confirm 全过程，``finally`` 释放），在
  ``super`` 前持久 ``begin`` 并冻结预期，``super`` 真实提交后才 ``confirm``；
  每次写入都是独立条目，旧条目不会被后续成功清除。

本模块只保存写入证明元数据（摘要），绝不保存消息/State 全文；已验收的
``digests.message_digest`` 语义保持不变，终态字段只在本模块组合进最终摘要。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Awaitable, Callable

from agentscope.app.message_bus import InMemoryMessageBus, MessageBusKeys
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.event import (
    AgentEvent,
    ExternalExecutionResultEvent,
    ReplyStartEvent,
    UserConfirmResultEvent,
    UserInterruptEvent,
)
from agentscope.message import AssistantMsg, Msg
from agentscope.middleware import MiddlewareBase
from agentscope.state import AgentState

from .digests import message_digest, state_digest
from .persistence_index import (
    Binding,
    PersistenceIndex,
    PersistenceIndexConflictError,
    PersistenceIndexError,
)

_SESSION_LOCK_PREFIX = MessageBusKeys.session_lock("")

# 续写（HITL / parked interrupt）事件类型：不发新 START，需按旧 Msg 重建观察。
_HALT_CONTINUATION_EVENTS = (
    UserConfirmResultEvent,
    ExternalExecutionResultEvent,
    UserInterruptEvent,
)


class PersistenceObservationClosedError(PersistenceIndexError):
    """在已关闭的运行 scope 上尝试观察或写入（迟到任务/已释放锁）。

    fail-closed：绝不换成独立健康条目掩盖，也绝不复用已确认条目。
    """


# ----------------------------------------------------------------------
# 摘要：内容 hash 与终态字段 hash 的组合（已验收 message_digest 语义不变）
# ----------------------------------------------------------------------
def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def terminal_digest(msg: Msg) -> str:
    """实际 writer 对象终态字段（finished_reason/error）的规范摘要。

    只返回哈希，错误正文绝不落盘。
    """
    reason = getattr(msg, "finished_reason", None)
    if hasattr(reason, "value"):
        reason = reason.value
    error = getattr(msg, "error", None)
    if error is None:
        error_dump = None
    elif hasattr(error, "model_dump"):
        error_dump = error.model_dump(mode="json")
    elif isinstance(error, dict):
        error_dump = error
    else:
        error_dump = {"type": type(error).__name__}
    return hashlib.sha256(
        _canonical_json({"finished_reason": reason, "error": error_dump}).encode(
            "utf-8",
        ),
    ).hexdigest()


def observed_message_digest(msg: Msg) -> str:
    """写入证明使用的最终消息摘要 = 内容 hash + 终态字段 hash。

    ``message_digest`` 已验收为**内容**摘要并刻意排除原生 hook 外补充的终态；
    这里在同一模块内把终态字段摘要组合进来，作为持久条目的最终摘要，readback
    用同一函数复核，不悄悄修改 ``message_digest`` 语义。
    """
    return hashlib.sha256(
        _canonical_json(
            {
                "content": message_digest(msg),
                "terminal": terminal_digest(msg),
            },
        ).encode("utf-8"),
    ).hexdigest()


# ----------------------------------------------------------------------
# 运行 scope
# ----------------------------------------------------------------------
def _session_id_from_lock_key(key: str) -> str | None:
    if key.startswith(_SESSION_LOCK_PREFIX):
        session_id = key[len(_SESSION_LOCK_PREFIX) :]
        return session_id or None
    return None


@dataclass
class _ObservedReply:
    """scope 内某个回复的重建观察（只活在内存，不持久全文）。

    ``msg`` 为 ``None`` 表示已登记 unknown 摘要期待但尚未重建内容
    （continuation 读旧 Msg 之前/失败时）。
    """

    entry_id: str
    msg: Msg | None = None
    consumed: bool = False


class ObservationScope:
    """单次 Session 运行锁持有的观察 scope（内存对象，无 Msg/State 持久副本）。

    通过**内存 writer lease** 关闭「native super 已进入提交但 Session 锁已释放」
    的同根竞态：每个 message/State writer 在首次 ``await`` 前受理 lease，覆盖
    绑定读取/索引/native super 真实 commit/confirm 全过程，``finally`` 释放。
    Session 运行锁退出时先同步封盘（拒绝新受理），再 shield 等待所有已受理
    writer 真实退出，之后才释放原生 Session 锁。
    """

    def __init__(self, session_id: str, index: PersistenceIndex) -> None:
        self.session_id = session_id
        self.index = index
        self.checkpoint_id = uuid.uuid4().hex
        self.binding: Binding | None = None
        self.closed = False
        self._sealed = False
        self._active_writers = 0
        self._drained = asyncio.Event()
        # 真实运行锁所有者：ContextVar 继承不等于锁所有权，只有该 Task 才能按
        # 既有重入语义使用本 scope；继承 scope 的子 Task 只能用于已受理 writer
        # lease，不能宣称持有运行锁（否则会造成源任务等待子任务的锁环）。
        self.owner_task = asyncio.current_task()
        self.state_entry_id: str | None = None
        self.replies: dict[str, _ObservedReply] = {}
        self._state_lock = asyncio.Lock()

    def bind(self, binding: Binding) -> None:
        """绑定可信身份（由装配闭包校验后写入）。"""
        if binding.session_id != self.session_id:
            raise PersistenceIndexConflictError(
                "身份绑定的 session 与运行 scope 不一致，拒绝观察。",
            )
        if self.binding is None:
            self.binding = binding
        elif self.binding != binding:
            raise PersistenceIndexConflictError(
                "运行 scope 已绑定不同身份，拒绝覆盖。",
            )

    @property
    def sealed(self) -> bool:
        return self._sealed

    def require_open(self) -> None:
        """已封盘/关闭的 scope 必须明确拒绝，绝不继续观察或写入。"""
        if self._sealed or self.closed:
            raise PersistenceObservationClosedError(
                "运行 scope 已关闭（锁已释放或迟到任务），拒绝观察与写入。",
            )

    def admit_writer(self, session_id: str, user_id: str) -> None:
        """受理一个 writer lease；封盘/关闭或身份不符时 fail-closed。"""
        if self._sealed or self.closed:
            raise PersistenceObservationClosedError(
                "运行 scope 已封盘，拒绝受理新的原生写入。",
            )
        if self.session_id != session_id:
            raise PersistenceIndexConflictError(
                "写入 session 与运行 scope 不一致，拒绝观察。",
            )
        binding = self.binding
        if binding is not None and binding.user_id != user_id:
            raise PersistenceIndexConflictError(
                "写入 user 与运行 scope 身份不一致，拒绝观察。",
            )
        self._active_writers += 1

    def release_writer(self) -> None:
        """释放 writer lease；封盘且全部退出时置位排空事件。"""
        self._active_writers -= 1
        if self._active_writers <= 0 and self._sealed:
            self._active_writers = 0
            self._drained.set()

    async def seal_and_drain(self) -> None:
        """同步封盘（拒绝新受理），再可靠等待所有已受理 writer 真实退出。

        完成条件只能是**已受理 writer 归零**（``_drained`` 置位），绝不能以内部
        ``Event.wait`` waiter 的 ``done()`` 作为完成；内部 waiter 被取消/失败时
        读取其真实结果、保留错误并继续等待，直到真实 writer 全部退出后才传播。
        outer 重复取消同样保持排空，绝不提前放锁。
        """
        self._sealed = True
        self.closed = True
        if self._active_writers <= 0:
            self._drained.set()

        pending: BaseException | None = None
        while not self._drained.is_set():
            waiter = asyncio.ensure_future(self._drained.wait())
            try:
                await asyncio.shield(waiter)
            except asyncio.CancelledError as exc:
                pending = pending or exc
            except BaseException as exc:  # noqa: BLE001
                pending = pending or exc
            finally:
                if not waiter.done():
                    waiter.cancel()
                try:
                    await asyncio.shield(waiter)
                except BaseException:  # noqa: BLE001 - 读取/清理结果，不吞
                    if waiter.done() and not waiter.cancelled():
                        waiter.exception()
        if pending is not None:
            raise pending

    async def ensure_state_entry(self, *, require_open: bool = True) -> str:
        """确保本 scope 唯一的未知摘要 State 期待项存在。"""
        if require_open:
            self.require_open()
        if self.state_entry_id is not None:
            return self.state_entry_id
        async with self._state_lock:
            if require_open:
                self.require_open()
            if self.state_entry_id is not None:
                return self.state_entry_id
            if self.binding is None:
                raise PersistenceIndexError(
                    "缺少可信身份绑定，无法建立 State 写入期待。",
                )
            entry_id = await self.index.begin(
                binding=self.binding,
                kind="state",
                subject_id=self.checkpoint_id,
                digest=None,
            )
            # await 之后保持关闭事实校验：已建 pending 不自动清除。
            if require_open:
                self.require_open()
            self.state_entry_id = entry_id
            return entry_id

    async def register_pending_reply(self, reply_id: str) -> _ObservedReply:
        """登记 unknown 摘要消息期待（内容重建前，或读旧 Msg 失败时保留）。"""
        self.require_open()
        existing = self.replies.get(reply_id)
        if existing is not None:
            return existing
        if self.binding is None:
            raise PersistenceIndexError(
                "缺少可信身份绑定，无法建立消息写入期待。",
            )
        entry_id = await self.index.begin(
            binding=self.binding,
            kind="message",
            subject_id=reply_id,
            digest=None,
        )
        # await 之后保持关闭事实校验：已建 unknown pending 不自动清除。
        self.require_open()
        observed = _ObservedReply(entry_id=entry_id, msg=None)
        self.replies[reply_id] = observed
        return observed

    async def observe_reply_start(self, reply_id: str, name: str) -> _ObservedReply:
        """真实 START：建立未知摘要消息期待并用公开事件重建内容观察。"""
        observed = await self.register_pending_reply(reply_id)
        if observed.msg is None:
            observed.msg = AssistantMsg(id=reply_id, name=name, content=[])
        return observed


_CURRENT_SCOPE: ContextVar[ObservationScope | None] = ContextVar(
    "nexus_persistence_observation_scope",
    default=None,
)


def current_scope() -> ObservationScope | None:
    return _CURRENT_SCOPE.get()


# ----------------------------------------------------------------------
# 公开 MessageBus 子类：只有 Session 运行锁建立 scope
# ----------------------------------------------------------------------
class ObservedMessageBus(InMemoryMessageBus):
    """公开 ``InMemoryMessageBus`` 子类：只观察运行 scope 与 inbox consumer 切点。"""

    def __init__(self, index: PersistenceIndex) -> None:
        super().__init__()
        self._index = index
        # 运行冻结控制：None 表示未配置（保持既有行为）；正式装配会注入。
        self._freeze_controller: Any = None

    def set_freeze_controller(self, controller: Any) -> None:
        """注入运行冻结控制（每 Agent admission 栅栏 + 删除事实核对）。"""
        self._freeze_controller = controller

    async def _guard_frozen(self, session_id: str) -> None:
        """Session 运行锁放行前的冻结检查；冻结/查询失败一律拒绝。

        与原生 error reporter ``try`` 无关：本检查发生在锁取得前后、原生
        handler 进入之前，拒绝不会合成 assistant error 尾部。
        """
        controller = self._freeze_controller
        if controller is None:
            return
        if await controller.session_blocked(session_id):
            from .freeze import RunFrozenError

            raise RunFrozenError(
                "Session 运行已被 Nexus 删除事实冻结，拒绝迟到运行。",
            )

    @asynccontextmanager
    async def acquire_lock(
        self,
        key: str,
        *,
        ttl_secs: int = 600,
    ) -> AsyncGenerator[None, None]:
        session_id = _session_id_from_lock_key(key)
        if session_id is None:
            # 非 Session 运行锁（checkpoint/inbox/...）：透传，不新建 scope、
            # 不产生持久 pending。正常 _persist 子 Task 只取 checkpoint 锁。
            async with super().acquire_lock(key, ttl_secs=ttl_secs):
                yield
            return

        current = _CURRENT_SCOPE.get()
        if current is not None:
            # 上下文已有运行 scope：只有**真实 owner Task** 且同 Session 未封盘，
            # 才按既有重入语义直接 yield（原生 asyncio.Lock 非重入，避免自锁）。
            # 继承 scope 的子 Task 不得宣称持有运行锁，必须明确拒绝以避免
            # 源任务等待子任务的锁环；不同 Session 同样拒绝。
            owner = asyncio.current_task() is current.owner_task
            if not owner:
                raise PersistenceObservationClosedError(
                    "非 owner Task 不得使用继承的运行 scope 取得 Session 运行锁。",
                )
            current.require_open()
            if current.session_id != session_id:
                raise PersistenceIndexConflictError(
                    "运行锁 session 与当前 scope 不一致，拒绝透传。",
                )
            yield
            return

        # 冻结检查（等待前）：清理已终止 Session 的迟到 native wakeup。
        await self._guard_frozen(session_id)

        # 关键：先**真正取得原生 Session 运行锁**，再建立/设置当前 scope；任何
        # __aenter__ 失败/取消都不会遗留 scope。排空在锁持有范围内完成——先同步
        # 封盘拒绝新受理，再等待所有已受理 writer 真实退出，之后才释放锁并 reset。
        async with super().acquire_lock(key, ttl_secs=ttl_secs):
            # 冻结检查（取得后）：等待期间冻结的迟到运行同样必须拒绝。
            await self._guard_frozen(session_id)
            scope = ObservationScope(session_id, self._index)
            token = _CURRENT_SCOPE.set(scope)
            try:
                yield
            finally:
                try:
                    await scope.seal_and_drain()
                finally:
                    _CURRENT_SCOPE.reset(token)

    async def registry_set(
        self,
        namespace: str,
        field: str,
        value: str,
        *,
        ttl_secs: int | None = None,
    ) -> None:
        """精确匹配当前运行 scope 的 inbox consumer 注册时先 ensure State 期待。"""
        scope = _CURRENT_SCOPE.get()
        if (
            scope is not None
            and not scope.sealed
            and not scope.closed
            and scope.binding is not None
            and namespace == MessageBusKeys.inbox_consumer(scope.session_id)
            and field == MessageBusKeys.INBOX_CONSUMER_FIELD
            and value == "1"
            and ttl_secs == MessageBusKeys.SESSION_RUN_TTL_SECS
        ):
            await scope.ensure_state_entry()
        await super().registry_set(
            namespace,
            field,
            value,
            ttl_secs=ttl_secs,
        )


# ----------------------------------------------------------------------
# 公开 Storage 子类：真实写入的 begin/confirm 观察
# ----------------------------------------------------------------------
class ObservedStorage(AsyncSQLAlchemyStorage):
    """公开 ``AsyncSQLAlchemyStorage`` 子类：观察真实消息/State 提交。

    入口即深拷贝实际 ``Msg``/``AgentState`` 并把它交给原生 ``super``，使
    await 索引期间原对象改变也不会使摘要与落盘内容分叉；readback 用同一函数
    复核。
    """

    def __init__(self, index: PersistenceIndex, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._index = index

    async def _resolve_binding(
        self,
        user_id: str,
        session_id: str,
        agent_id: str | None = None,
    ) -> Binding:
        """由真实 storage Session 记录/可信 scope 闭包确定身份，不用 Msg.name。

        在 scope 上下文里，受理 lease 已在任何 ``await`` 之前完成 session/user
        身份校验；这里只补齐 agent 维度并优先使用可信闭包绑定，仅在装配前
        setup 失败（尚无绑定）时才回退到真实 Session 记录。
        """
        scope = _CURRENT_SCOPE.get()
        if scope is not None and scope.binding is not None:
            binding = scope.binding
            if binding.user_id != user_id or (
                agent_id is not None and binding.agent_id != agent_id
            ):
                raise PersistenceIndexConflictError(
                    "写入参数与运行 scope 身份不一致，拒绝观察。",
                )
            return binding

        record = await self.get_session(user_id, agent_id or "", session_id)
        if record is None or record.user_id != user_id:
            raise PersistenceIndexError(
                "无法确认会话身份绑定，拒绝记录写入观察。",
            )
        resolved_agent_id = getattr(record, "agent_id", None)
        if not isinstance(resolved_agent_id, str) or not resolved_agent_id:
            raise PersistenceIndexError(
                "会话记录缺少有效 agent_id，拒绝记录写入观察。",
            )
        if agent_id is not None and resolved_agent_id != agent_id:
            raise PersistenceIndexConflictError(
                "写入 agent_id 与会话记录不一致，拒绝观察。",
            )
        return Binding(
            user_id=user_id,
            agent_id=resolved_agent_id,
            session_id=session_id,
        )

    @staticmethod
    def _admit_scope_writer(
        session_id: str,
        user_id: str,
    ) -> ObservationScope | None:
        """在任何 await 之前受理 writer lease（无 scope 时返回 None）。"""
        scope = _CURRENT_SCOPE.get()
        if scope is None:
            return None
        scope.admit_writer(session_id, user_id)
        return scope

    async def upsert_message(
        self,
        user_id: str,
        session_id: str,
        msg: Msg,
    ) -> None:
        admitted = self._admit_scope_writer(session_id, user_id)
        try:
            snapshot = msg.model_copy(deep=True)
            binding = await self._resolve_binding(user_id, session_id)
            combined = observed_message_digest(snapshot)

            scope = _CURRENT_SCOPE.get()
            observed = (
                scope.replies.get(snapshot.id) if scope is not None else None
            )
            if observed is not None and not observed.consumed:
                observed.consumed = True
                if observed.msg is not None and (
                    message_digest(observed.msg) == message_digest(snapshot)
                ):
                    await self._index.set_digest(
                        binding=binding,
                        entry_id=observed.entry_id,
                        digest=combined,
                    )
                    await super().upsert_message(user_id, session_id, snapshot)
                    await self._index.confirm(
                        binding=binding,
                        entry_id=observed.entry_id,
                        digest=combined,
                    )
                    return
                # 内容与事件重建不一致（或内容尚未重建）：不假确认，保留 pending；
                # 已受理的写入在锁保护下完成原生提交。
                await super().upsert_message(user_id, session_id, snapshot)
                return

            entry_id = await self._index.begin(
                binding=binding,
                kind="message",
                subject_id=snapshot.id,
                digest=combined,
            )
            await super().upsert_message(user_id, session_id, snapshot)
            await self._index.confirm(
                binding=binding,
                entry_id=entry_id,
                digest=combined,
            )
        finally:
            if admitted is not None:
                admitted.release_writer()

    async def update_session_state(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        state: AgentState,
    ) -> None:
        admitted = self._admit_scope_writer(session_id, user_id)
        try:
            snapshot = state.model_copy(deep=True)
            binding = await self._resolve_binding(user_id, session_id, agent_id)
            # 实际 State 身份校验：不悄悄改写外来 State，也不凭参数假确认。
            # 公开 AgentState 只有 session_id 身份字段，不做超出原生能力的推断。
            if snapshot.session_id != session_id or (
                snapshot.session_id != binding.session_id
            ):
                raise PersistenceIndexConflictError(
                    "实际 State.session_id 与可信绑定不一致，拒绝提交观察。",
                )
            digest = state_digest(snapshot)

            scope = _CURRENT_SCOPE.get()
            if scope is not None and scope.binding is not None:
                entry_id = await scope.ensure_state_entry(require_open=False)
                await self._index.set_digest(
                    binding=binding,
                    entry_id=entry_id,
                    digest=digest,
                )
                await super().update_session_state(
                    user_id,
                    agent_id,
                    session_id,
                    snapshot,
                )
                await self._index.confirm(
                    binding=binding,
                    entry_id=entry_id,
                    digest=digest,
                )
                return

            entry_id = await self._index.begin(
                binding=binding,
                kind="state",
                subject_id=uuid.uuid4().hex,
                digest=digest,
            )
            await super().update_session_state(
                user_id,
                agent_id,
                session_id,
                snapshot,
            )
            await self._index.confirm(
                binding=binding,
                entry_id=entry_id,
                digest=digest,
            )
        finally:
            if admitted is not None:
                admitted.release_writer()


# ----------------------------------------------------------------------
# 公开 on_reply 观察中间件
# ----------------------------------------------------------------------
class ObservationMiddleware(MiddlewareBase):
    """绑定真实 user/agent/session 的 on_reply 持久写入观察器。"""

    def __init__(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        storage: ObservedStorage,
    ) -> None:
        self._user_id = user_id
        self._agent_id = agent_id
        self._session_id = session_id
        self._storage = storage

    def _require_scope(self) -> ObservationScope:
        """取得当前运行 scope；缺失/封盘/关闭或身份不符一律明确拒绝。

        绝不因 scope 已关闭而静默关闭观察再继续运行模型（那会掩盖真实写入）。
        """
        scope = _CURRENT_SCOPE.get()
        if scope is None:
            raise PersistenceIndexError(
                "受控运行缺少观察 scope，拒绝无观察运行。",
            )
        if scope.session_id != self._session_id:
            raise PersistenceIndexConflictError(
                "运行 scope 身份与观察器闭包不一致，拒绝观察。",
            )
        scope.require_open()
        return scope

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        scope = self._require_scope()
        if scope.binding is not None:
            # 首次进入 on_reply：先建立该 scope 唯一的未知摘要 State 期待项，
            # 以捕获「真正 update 入口从未到达」。
            await scope.ensure_state_entry()
            # await 之后保持关闭事实校验：已有 pending 不能自动清除。
            scope.require_open()

            inputs = input_kwargs.get("inputs")
            if isinstance(inputs, _HALT_CONTINUATION_EVENTS):
                await self._observe_continuation(scope, agent, inputs)
                scope.require_open()

        async for item in next_handler(**input_kwargs):
            await self._observe_item(scope, item)
            yield item

    @staticmethod
    def _is_parked(agent: Any) -> bool:
        state = getattr(agent, "state", None)
        checker = getattr(state, "has_awaiting_tool_calls", None)
        if not callable(checker):
            return False
        name = getattr(agent, "name", None)
        try:
            return bool(checker(name))
        except Exception:  # noqa: BLE001 - 无法判定时按非续写处理
            return False

    async def _observe_continuation(
        self,
        scope: ObservationScope,
        agent: Any,
        inputs: Any,
    ) -> None:
        """续写（HITL/interrupt）观察：用原生 ``agent.state.reply_id`` 定位。

        仅对真实 parked（有等待外部响应的工具调用）续写建立期待；普通 idle
        no-op interrupt 不创造不存在的新回复期待。先登记 unknown 消息期待，再
        读旧 Msg；读失败/缺失明确失败并保留期待，绝不静默报告健康。
        """
        if not self._is_parked(agent):
            return
        reply_id = getattr(getattr(agent, "state", None), "reply_id", None)
        if not isinstance(reply_id, str) or not reply_id:
            raise PersistenceIndexError(
                "续写缺少可信 agent.state.reply_id，拒绝观察。",
            )
        observed = await scope.register_pending_reply(reply_id)
        if observed.msg is not None:
            observed.msg.append_event(inputs)
            return
        old = await self._storage.get_message(
            self._user_id,
            self._session_id,
            reply_id,
        )
        if old is None:
            raise PersistenceIndexError(
                "续写旧回复在原生存储中缺失，保留未知期待并失败。",
            )
        if old.id != reply_id:
            raise PersistenceIndexConflictError(
                "续写读回的旧回复 id 与可信 reply_id 不一致，拒绝观察。",
            )
        observed.msg = old.model_copy(deep=True)
        observed.msg.append_event(inputs)

    async def _observe_item(
        self,
        scope: ObservationScope,
        item: Any,
    ) -> None:
        if isinstance(item, Msg):
            # 原生流过滤的最终 fallback Msg 不属于事件重建内容，忽略。
            return
        if not isinstance(item, AgentEvent):
            return
        if isinstance(item, ReplyStartEvent):
            await scope.observe_reply_start(item.reply_id, item.name)
            return
        observed = scope.replies.get(item.reply_id)
        if observed is not None and observed.msg is not None:
            observed.msg.append_event(item)


def make_observation_factory(
    storage: ObservedStorage,
    index: PersistenceIndex,
) -> Callable[..., Awaitable[list[MiddlewareBase]]]:
    """构造观察中间件工厂；装配成功后才把可信身份绑定到当前 scope。"""

    async def factory(
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace: Any = None,
    ) -> list[MiddlewareBase]:
        binding = Binding(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        scope = _CURRENT_SCOPE.get()
        if scope is not None and scope.session_id == session_id:
            record = await storage.get_session(user_id, agent_id, session_id)
            if (
                record is not None
                and getattr(record, "user_id", None) == user_id
                and getattr(record, "agent_id", None) == agent_id
            ):
                scope.bind(binding)
        return [
            ObservationMiddleware(user_id, agent_id, session_id, storage),
        ]

    return factory


# ----------------------------------------------------------------------
# 公开 readback：停写后逐条核对并确认
# ----------------------------------------------------------------------
@dataclass
class ReadbackResult:
    """单条未确认条目的核对结果。"""

    entry_id: str
    kind: str
    subject_id: str
    confirmed: bool
    reason: str


@dataclass
class ReadbackReport:
    """指定可信绑定的核对报告。"""

    binding: Binding
    results: list[ReadbackResult] = field(default_factory=list)

    @property
    def blocked(self) -> list[ReadbackResult]:
        return [r for r in self.results if not r.confirmed]


async def readback_verify(
    index: PersistenceIndex,
    storage: ObservedStorage,
    binding: Binding,
) -> ReadbackReport:
    """停写后核对指定可信绑定的未确认条目。

    先用 ``get_session`` 校验 user/agent/session，再读取实际 Msg/State、用与
    写入相同的摘要函数复核；精确匹配才 ``confirm``。未知摘要/缺失/不一致继续
    阻断。读回错误一律抛出，绝不返回空健康。
    """
    report = ReadbackReport(binding=binding)

    record = await storage.get_session(
        binding.user_id,
        binding.agent_id,
        binding.session_id,
    )
    if (
        record is None
        or getattr(record, "user_id", None) != binding.user_id
        or getattr(record, "agent_id", None) != binding.agent_id
        or getattr(record, "id", None) != binding.session_id
    ):
        raise PersistenceIndexError(
            "readback 无法确认可信会话绑定，拒绝返回核对结果。",
        )

    entries = await index.unconfirmed(binding)
    for entry in entries:
        if entry.digest is None:
            report.results.append(
                ReadbackResult(
                    entry_id=entry.entry_id,
                    kind=entry.kind,
                    subject_id=entry.subject_id,
                    confirmed=False,
                    reason="摘要未知，等待真正写入入口。",
                ),
            )
            continue

        if entry.kind == "message":
            actual = await storage.get_message(
                binding.user_id,
                binding.session_id,
                entry.subject_id,
            )
            if actual is None:
                report.results.append(
                    ReadbackResult(
                        entry_id=entry.entry_id,
                        kind=entry.kind,
                        subject_id=entry.subject_id,
                        confirmed=False,
                        reason="原生消息缺失，无法确认。",
                    ),
                )
                continue
            computed = observed_message_digest(actual)
        elif entry.kind == "state":
            computed = state_digest(record.state)
        else:
            raise PersistenceIndexError(f"未知的观察条目类型: {entry.kind!r}")

        if computed != entry.digest:
            report.results.append(
                ReadbackResult(
                    entry_id=entry.entry_id,
                    kind=entry.kind,
                    subject_id=entry.subject_id,
                    confirmed=False,
                    reason="实际提交内容与记录摘要不一致，继续阻断。",
                ),
            )
            continue

        await index.confirm(
            binding=binding,
            entry_id=entry.entry_id,
            digest=entry.digest,
        )
        report.results.append(
            ReadbackResult(
                entry_id=entry.entry_id,
                kind=entry.kind,
                subject_id=entry.subject_id,
                confirmed=True,
                reason="已核对并确认。",
            ),
        )

    return report

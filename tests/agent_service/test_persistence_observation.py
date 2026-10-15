# -*- coding: utf-8 -*-
"""原生持久化写入观察接入测试（OpenSpec 任务 3.2.3.1）。

只使用真实公开组件：
- 真实 ``PersistenceIndex``（临时 SQLite，真实提交）；
- 真实 ``ObservedStorage``（真实 ``AsyncSQLAlchemyStorage`` 子类 + 临时 SQLite）；
- 真实 ``ObservedMessageBus``（真实 ``InMemoryMessageBus`` 子类）；
- 真实 ``ObservationMiddleware`` / 真实公开事件类型与 ``Msg``/``AgentState``。

不导入官方私有模块，不用字符串假事件冒充原生事件。
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest
from agentscope.app.message_bus import MessageBusKeys
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    AsyncSQLAlchemyStorage,
    ChatModelConfig,
    SessionConfig,
)
from agentscope.event import (
    ExternalExecutionResultEvent,
    ReplyFinishedReason,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    UserConfirmResultEvent,
    UserInterruptEvent,
)
from agentscope.message import (
    AssistantMsg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    UserMsg,
)
from agentscope.state import AgentState
from agentscope.types import ErrorInfo

from nexus_agent_service.persistence_index import (
    Binding,
    PersistenceIndex,
    PersistenceIndexConflictError,
)
from nexus_agent_service.persistence_observation import (
    ObservationMiddleware,
    ObservationScope,
    ObservedMessageBus,
    ObservedStorage,
    PersistenceObservationClosedError,
    _CURRENT_SCOPE,
    current_scope,
    make_observation_factory,
    observed_message_digest,
    readback_verify,
    terminal_digest,
)
from nexus_agent_service.digests import message_digest

USER = "user-obs-01"
AGENT = "agent-obs-01"
SESSION = "session-obs-01"


@pytest.fixture
def binding() -> Binding:
    return Binding(user_id=USER, agent_id=AGENT, session_id=SESSION)


async def _build_storage(tmp_path: Path) -> tuple[PersistenceIndex, ObservedStorage]:
    index = PersistenceIndex(tmp_path / "idx")
    storage = ObservedStorage(
        index,
        url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        create_tables=True,
        engine_kwargs={"hide_parameters": True},
    )
    return index, storage


async def _seed(
    storage: ObservedStorage,
    agent_id: str = AGENT,
    session_id: str = SESSION,
) -> None:
    record = AgentRecord(
        id=agent_id,
        user_id=USER,
        data=AgentData(id=agent_id, name="agent", system_prompt="p"),
    )
    await storage.upsert_agent(USER, record)
    config = SessionConfig(
        workspace_id=f"ws-{session_id}",
        chat_model_config=ChatModelConfig(
            type="recording",
            credential_id="cred",
            model="recording",
            parameters={},
        ),
    )
    await storage.upsert_session(USER, agent_id, config, session_id=session_id)


def _assistant(reply_id: str = "r1", text: str = "hi") -> AssistantMsg:
    msg = AssistantMsg(id=reply_id, name="agent", content=[])
    msg.append_event(
        TextBlockStartEvent(
            session_id=SESSION,
            reply_id=reply_id,
            block_id="b1",
            text="",
        ),
    )
    msg.append_event(
        TextBlockDeltaEvent(
            session_id=SESSION,
            reply_id=reply_id,
            block_id="b1",
            delta=text,
        ),
    )
    msg.append_event(
        TextBlockEndEvent(
            session_id=SESSION,
            reply_id=reply_id,
            block_id="b1",
        ),
    )
    return msg


# ----------------------------------------------------------------------
# 摘要
# ----------------------------------------------------------------------
def test_observed_digest_covers_terminal_state_without_touching_message_digest():
    clean = _assistant()
    errored = _assistant()
    errored.finished_reason = ReplyFinishedReason.ERROR
    errored.error = {"message": "boom", "type": "RuntimeError"}

    # 已验收 message_digest 特意排除 hook 外终态，语义保持不变。
    assert message_digest(clean) == message_digest(errored)
    assert terminal_digest(clean) != terminal_digest(errored)

    # 最终持久摘要必须覆盖终态字段差异。
    assert observed_message_digest(clean) != observed_message_digest(errored)


def test_observed_digest_stable_for_equivalent_objects():
    a = _assistant()
    b = _assistant()
    assert observed_message_digest(a) == observed_message_digest(b)
    assert len(observed_message_digest(a)) == 64


# ----------------------------------------------------------------------
# MessageBus scope
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_session_lock_establishes_single_scope(tmp_path: Path):
    index = PersistenceIndex(tmp_path)
    bus = ObservedMessageBus(index)
    async with index:
        assert current_scope() is None
        async with bus.acquire_lock(MessageBusKeys.session_lock(SESSION)):
            scope = current_scope()
            assert scope is not None
            assert scope.session_id == SESSION
            # 嵌套的其他锁（checkpoint/inbox）必须透传：同一 scope 对象。
            async with bus.acquire_lock(
                MessageBusKeys.session_event_checkpoint_lock(SESSION),
            ):
                assert current_scope() is scope
        assert current_scope() is None


@pytest.mark.asyncio
async def test_non_session_lock_does_not_create_scope(tmp_path: Path):
    index = PersistenceIndex(tmp_path)
    bus = ObservedMessageBus(index)
    async with index:
        async with bus.acquire_lock(MessageBusKeys.inbox_lock(SESSION)):
            assert current_scope() is None
        # 普通锁探测不产生 pending。
        assert await bus.is_locked(MessageBusKeys.session_lock(SESSION)) is False
        assert await bus.try_lock(MessageBusKeys.session_lock(SESSION)) is True
        await bus.unlock(MessageBusKeys.session_lock(SESSION))
        assert await index.unconfirmed(
            Binding(USER, AGENT, SESSION),
        ) == []


@pytest.mark.asyncio
async def test_registry_set_inbox_consumer_ensures_state_expectation(
    tmp_path: Path,
    binding: Binding,
):
    index = PersistenceIndex(tmp_path)
    bus = ObservedMessageBus(index)
    async with index:
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        token = _CURRENT_SCOPE.set(scope)
        try:
            # 非精确匹配（TTL 不同）不建立期待。
            await bus.registry_set(
                MessageBusKeys.inbox_consumer(SESSION),
                MessageBusKeys.INBOX_CONSUMER_FIELD,
                "1",
                ttl_secs=1,
            )
            assert scope.state_entry_id is None

            await bus.registry_set(
                MessageBusKeys.inbox_consumer(SESSION),
                MessageBusKeys.INBOX_CONSUMER_FIELD,
                "1",
                ttl_secs=MessageBusKeys.SESSION_RUN_TTL_SECS,
            )
            assert scope.state_entry_id is not None
            pending = await index.unconfirmed(binding)
            assert len(pending) == 1
            assert pending[0].kind == "state"
            assert pending[0].subject_id == scope.checkpoint_id
            assert pending[0].digest is None
        finally:
            _CURRENT_SCOPE.reset(token)


# ----------------------------------------------------------------------
# Storage 观察：begin 先于 super，super 提交后才 confirm
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_message_pending_until_super_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)

        pause = asyncio.Event()
        release = asyncio.Event()
        original = AsyncSQLAlchemyStorage.upsert_message

        async def slow_upsert(self, user_id, session_id, msg):
            pause.set()
            await release.wait()
            return await original(self, user_id, session_id, msg)

        monkeypatch.setattr(AsyncSQLAlchemyStorage, "upsert_message", slow_upsert)

        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        token = _CURRENT_SCOPE.set(scope)
        try:
            msg = UserMsg(name="user", content="hi", id="m-slow")
            task = asyncio.ensure_future(storage.upsert_message(USER, SESSION, msg))
            await asyncio.wait_for(pause.wait(), timeout=10)
            # 真实保存尚未完成：pending 仍在。
            pending = await index.unconfirmed(binding)
            assert any(
                p.kind == "message" and p.subject_id == "m-slow"
                for p in pending
            )
            release.set()
            await task
            assert await index.unconfirmed(binding) == []
        finally:
            _CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_two_replies_one_state_in_single_scope(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        token = _CURRENT_SCOPE.set(scope)
        try:
            await scope.ensure_state_entry()
            # 两个真实回复观察 + 两次真实消息写入。
            for reply_id, text in (("r1", "one"), ("r2", "two")):
                observed = await scope.observe_reply_start(reply_id, "agent")
                # 事件重建内容观察（与真实 writer 相同来源）。
                observed.msg.append_event(
                    TextBlockStartEvent(
                        session_id=SESSION,
                        reply_id=reply_id,
                        block_id="b1",
                        text="",
                    ),
                )
                observed.msg.append_event(
                    TextBlockDeltaEvent(
                        session_id=SESSION,
                        reply_id=reply_id,
                        block_id="b1",
                        delta=text,
                    ),
                )
                observed.msg.append_event(
                    TextBlockEndEvent(
                        session_id=SESSION,
                        reply_id=reply_id,
                        block_id="b1",
                    ),
                )
                writer = _assistant(reply_id, text)
                await storage.upsert_message(USER, SESSION, writer)

            state = AgentState(
                session_id=SESSION,
                summary="s",
                context=[],
                reply_id="r2",
            )
            await storage.update_session_state(USER, AGENT, SESSION, state)

            entries = await index.unconfirmed(binding)
            assert entries == []
        finally:
            _CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_continuation_preserves_old_name_and_id(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        old = _assistant("r-old", "first")
        old.name = "original-name"
        await storage.upsert_message(USER, SESSION, old)

        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        token = _CURRENT_SCOPE.set(scope)
        try:
            loaded = await storage.get_message(USER, SESSION, "r-old")
            observed = await scope.register_pending_reply("r-old")
            assert observed.msg is None
            observed.msg = loaded.model_copy(deep=True)
            assert observed.msg.id == "r-old"
            assert observed.msg.name == "original-name"

            # 真实 writer 与重建使用同一旧 Msg + 新事件。
            writer = loaded.model_copy(deep=True)
            writer.append_event(
                TextBlockStartEvent(
                    session_id=SESSION,
                    reply_id="r-old",
                    block_id="b2",
                    text="",
                ),
            )
            writer.append_event(
                TextBlockDeltaEvent(
                    session_id=SESSION,
                    reply_id="r-old",
                    block_id="b2",
                    delta="more",
                ),
            )
            writer.append_event(
                TextBlockEndEvent(
                    session_id=SESSION,
                    reply_id="r-old",
                    block_id="b2",
                ),
            )
            observed.msg.append_event(
                TextBlockStartEvent(
                    session_id=SESSION,
                    reply_id="r-old",
                    block_id="b2",
                    text="",
                ),
            )
            observed.msg.append_event(
                TextBlockDeltaEvent(
                    session_id=SESSION,
                    reply_id="r-old",
                    block_id="b2",
                    delta="more",
                ),
            )
            observed.msg.append_event(
                TextBlockEndEvent(
                    session_id=SESSION,
                    reply_id="r-old",
                    block_id="b2",
                ),
            )
            await storage.upsert_message(USER, SESSION, writer)
            assert await index.unconfirmed(binding) == []
        finally:
            _CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_content_mismatch_does_not_confirm(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        token = _CURRENT_SCOPE.set(scope)
        try:
            observed = await scope.observe_reply_start("r1", "agent")
            observed.msg.append_event(
                TextBlockStartEvent(
                    session_id=SESSION,
                    reply_id="r1",
                    block_id="b1",
                    text="",
                ),
            )
            observed.msg.append_event(
                TextBlockDeltaEvent(
                    session_id=SESSION,
                    reply_id="r1",
                    block_id="b1",
                    delta="expected",
                ),
            )
            # writer 内容不同：不得假确认。
            await storage.upsert_message(USER, SESSION, _assistant("r1", "different"))
            pending = await index.unconfirmed(binding)
            assert len(pending) == 1
            assert pending[0].subject_id == "r1"
            assert pending[0].digest is None
            # 原生写入仍真实完成。
            assert await storage.get_message(USER, SESSION, "r1") is not None
        finally:
            _CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_concurrent_scopes_do_not_cross(tmp_path: Path):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage, "agent-c1", "session-c1")
        await _seed(storage, "agent-c2", "session-c2")

        async def run(agent_id: str, session_id: str) -> None:
            binding = Binding(USER, agent_id, session_id)
            scope = ObservationScope(session_id, index)
            scope.bind(binding)
            token = _CURRENT_SCOPE.set(scope)
            try:
                await scope.ensure_state_entry()
                await asyncio.sleep(0)
                await storage.upsert_message(
                    USER,
                    session_id,
                    UserMsg(name="user", content=f"m-{session_id}", id=f"m-{session_id}"),
                )
                await storage.update_session_state(
                    USER,
                    agent_id,
                    session_id,
                    AgentState(
                        session_id=session_id,
                        summary=session_id,
                        context=[],
                        reply_id="r",
                    ),
                )
            finally:
                _CURRENT_SCOPE.reset(token)

        await asyncio.gather(
            run("agent-c1", "session-c1"),
            run("agent-c2", "session-c2"),
        )

        for agent_id, session_id in (
            ("agent-c1", "session-c1"),
            ("agent-c2", "session-c2"),
        ):
            binding = Binding(USER, agent_id, session_id)
            assert await index.unconfirmed(binding) == []
            assert (
                await storage.get_message(USER, session_id, f"m-{session_id}")
            ) is not None


@pytest.mark.asyncio
async def test_message_super_failure_keeps_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)

        async def boom(self, user_id, session_id, msg):
            raise RuntimeError("native commit failed")

        monkeypatch.setattr(AsyncSQLAlchemyStorage, "upsert_message", boom)

        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        token = _CURRENT_SCOPE.set(scope)
        try:
            with pytest.raises(RuntimeError):
                await storage.upsert_message(
                    USER,
                    SESSION,
                    UserMsg(name="user", content="x", id="m-fail"),
                )
            pending = await index.unconfirmed(binding)
            assert len(pending) == 1
            assert pending[0].subject_id == "m-fail"
            assert pending[0].digest is not None
        finally:
            _CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_state_super_failure_keeps_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)

        async def boom(self, user_id, agent_id, session_id, state):
            raise RuntimeError("native state commit failed")

        monkeypatch.setattr(AsyncSQLAlchemyStorage, "update_session_state", boom)

        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        token = _CURRENT_SCOPE.set(scope)
        try:
            await scope.ensure_state_entry()
            state = AgentState(session_id=SESSION, summary="s", context=[], reply_id="r")
            with pytest.raises(RuntimeError):
                await storage.update_session_state(USER, AGENT, SESSION, state)
            pending = await index.unconfirmed(binding)
            assert len(pending) == 1
            assert pending[0].kind == "state"
            assert pending[0].digest is not None
        finally:
            _CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_cancellation_does_not_confirm_unfinished_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)

        started = asyncio.Event()

        async def hang(self, user_id, session_id, msg):
            started.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(AsyncSQLAlchemyStorage, "upsert_message", hang)

        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        token = _CURRENT_SCOPE.set(scope)
        try:
            task = asyncio.ensure_future(
                storage.upsert_message(
                    USER,
                    SESSION,
                    UserMsg(name="user", content="x", id="m-cancel"),
                ),
            )
            await asyncio.wait_for(started.wait(), timeout=10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            pending = await index.unconfirmed(binding)
            assert len(pending) == 1
            assert pending[0].subject_id == "m-cancel"
            assert pending[0].confirmed is False
        finally:
            _CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_setup_error_message_without_start_is_independent(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        token = _CURRENT_SCOPE.set(scope)
        try:
            # hook 外 setup ERROR：无 START、无 on_reply，也未建立 State 期待。
            err = AssistantMsg(id="err-1", name=AGENT, content=[])
            err.finished_reason = ReplyFinishedReason.ERROR
            err.error = ErrorInfo(message="setup failed", type="setup")
            await storage.upsert_message(USER, SESSION, err)
            # 独立确认，且没有虚假的 State 期待。
            assert await index.unconfirmed(binding) == []
            assert scope.state_entry_id is None
            assert await storage.get_message(USER, SESSION, "err-1") is not None
        finally:
            _CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_scope_out_state_write_is_independent(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        state = AgentState(session_id=SESSION, summary="x", context=[], reply_id="r")
        await storage.update_session_state(USER, AGENT, SESSION, state)
        # 无 scope 时独立 begin/confirm，不丢失真实 writer。
        assert await index.unconfirmed(binding) == []


# ----------------------------------------------------------------------
# readback
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_readback_confirms_native_committed_but_unconfirmed(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        # 真实原生提交（绕过观察，模拟 confirm 失败后的恢复场景）。
        writer = _assistant("r-rb", "payload")
        await AsyncSQLAlchemyStorage.upsert_message(
            storage,
            USER,
            SESSION,
            writer,
        )
        state = AgentState(session_id=SESSION, summary="s", context=[], reply_id="r-rb")
        await AsyncSQLAlchemyStorage.update_session_state(
            storage,
            USER,
            AGENT,
            SESSION,
            state,
        )
        # 对应持久索引条目（digest 已知，未确认）。
        await index.begin(
            binding,
            "message",
            "r-rb",
            observed_message_digest(writer),
        )
        from nexus_agent_service.digests import state_digest

        await index.begin(binding, "state", "chk", state_digest(state))

        report = await readback_verify(index, storage, binding)
        assert report.blocked == []
        assert await index.unconfirmed(binding) == []


@pytest.mark.asyncio
async def test_readback_blocks_unknown_and_mismatch(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        # 未知摘要（State 入口从未到达）必须继续阻断。
        await index.begin(binding, "state", "never", None)
        # 内容不一致必须继续阻断。
        writer = _assistant("r-bad", "actual")
        await AsyncSQLAlchemyStorage.upsert_message(storage, USER, SESSION, writer)
        await index.begin(
            binding,
            "message",
            "r-bad",
            observed_message_digest(_assistant("r-bad", "other")),
        )
        report = await readback_verify(index, storage, binding)
        assert len(report.blocked) == 2
        # 仍然全部未确认。
        assert len(await index.unconfirmed(binding)) == 2


@pytest.mark.asyncio
async def test_readback_empty_ok_when_nothing_pending(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        report = await readback_verify(index, storage, binding)
        assert report.results == []
        assert report.blocked == []


@pytest.mark.asyncio
async def test_readback_rejects_terminal_state_mismatch(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        # 实际提交为 COMPLETED；记录摘要来自 ERROR 终态 → 必须拒绝确认。
        committed = _assistant("r-term", "body")
        await AsyncSQLAlchemyStorage.upsert_message(
            storage,
            USER,
            SESSION,
            committed,
        )
        errored = _assistant("r-term", "body")
        errored.finished_reason = ReplyFinishedReason.ERROR
        await index.begin(
            binding,
            "message",
            "r-term",
            observed_message_digest(errored),
        )
        report = await readback_verify(index, storage, binding)
        assert len(report.blocked) == 1
        assert await index.unconfirmed(binding) != []


@pytest.mark.asyncio
async def test_persistent_file_has_no_message_or_state_fulltext(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    secret = "NEXUS-SECRET-PAYLOAD-TEXT"
    async with index, storage:
        await _seed(storage)
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        token = _CURRENT_SCOPE.set(scope)
        try:
            await scope.ensure_state_entry()
            await storage.upsert_message(
                USER,
                SESSION,
                UserMsg(name="user", content=secret, id="m-secret"),
            )
            state = AgentState(
                session_id=SESSION,
                summary=secret,
                context=[],
                reply_id="r",
            )
            await storage.update_session_state(USER, AGENT, SESSION, state)
        finally:
            _CURRENT_SCOPE.reset(token)

    # 索引持久文件（db 及 WAL）绝不能包含消息/State 正文。
    for name in (
        "persistence_index.sqlite3",
        "persistence_index.sqlite3-wal",
        "persistence_index.sqlite3-shm",
    ):
        path = tmp_path / "idx" / name
        if path.exists():
            assert secret.encode() not in path.read_bytes()


# ----------------------------------------------------------------------
# 修复 1：closed scope 的迟到写入必须拒绝，不能覆盖新状态
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_closed_scope_late_state_write_rejected(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    bus = ObservedMessageBus(index)
    async with index, storage:
        await _seed(storage)
        old_state = AgentState(
            session_id=SESSION,
            summary="old",
            context=[],
            reply_id="r-old",
        )
        new_state = AgentState(
            session_id=SESSION,
            summary="new",
            context=[],
            reply_id="r-new",
        )
        child_started = asyncio.Event()
        child_go = asyncio.Event()
        captured: dict = {}

        async def child() -> None:
            # 该 Task 在 scope 内创建，继承同一（后来被关闭的）scope。
            child_started.set()
            await child_go.wait()
            try:
                await storage.update_session_state(
                    USER,
                    AGENT,
                    SESSION,
                    old_state,
                )
                captured["error"] = None
            except BaseException as exc:  # noqa: BLE001 - 记录后断言
                captured["error"] = exc

        async with bus.acquire_lock(MessageBusKeys.session_lock(SESSION)):
            scope = current_scope()
            assert scope is not None
            scope.bind(binding)
            await scope.ensure_state_entry()
            await storage.update_session_state(USER, AGENT, SESSION, old_state)
            child = asyncio.ensure_future(child())
            await asyncio.wait_for(child_started.wait(), timeout=10)

        # 父退出 scope（closed），child 仍持有旧 closed scope。
        # 在无 scope 上下文保存 new_state（独立条目）。
        assert current_scope() is None
        await storage.update_session_state(USER, AGENT, SESSION, new_state)

        child_go.set()
        await child

        # 迟到写入被明确拒绝，未覆盖 new_state。
        assert isinstance(
            captured["error"],
            PersistenceObservationClosedError,
        )
        record = await storage.get_session(USER, AGENT, SESSION)
        assert record is not None
        assert record.state.summary == "new"
        # 两次合法写入都已确认，无残留 pending。
        assert await index.unconfirmed(binding) == []


@pytest.mark.asyncio
async def test_closed_scope_late_message_write_rejected(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    bus = ObservedMessageBus(index)
    async with index, storage:
        await _seed(storage)
        child_started = asyncio.Event()
        child_go = asyncio.Event()
        captured: dict = {}

        async def child() -> None:
            child_started.set()
            await child_go.wait()
            try:
                await storage.upsert_message(
                    USER,
                    SESSION,
                    UserMsg(name="user", content="late", id="m-late"),
                )
                captured["error"] = None
            except BaseException as exc:  # noqa: BLE001
                captured["error"] = exc

        async with bus.acquire_lock(MessageBusKeys.session_lock(SESSION)):
            scope = current_scope()
            assert scope is not None
            scope.bind(binding)
            await scope.ensure_state_entry()
            child = asyncio.ensure_future(child())
            await asyncio.wait_for(child_started.wait(), timeout=10)

        child_go.set()
        await child

        assert isinstance(
            captured["error"],
            PersistenceObservationClosedError,
        )
        # 原生写入被拒绝：消息不存在。
        assert await storage.get_message(USER, SESSION, "m-late") is None


@pytest.mark.asyncio
async def test_inflight_state_writer_blocks_new_run_until_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding: Binding,
):
    """native super 已进入提交时 owner 不能提前放锁；新运行必须等排空后才进锁。"""
    index, storage = await _build_storage(tmp_path)
    bus = ObservedMessageBus(index)
    async with index, storage:
        await _seed(storage)
        old_state = AgentState(
            session_id=SESSION,
            summary="old",
            context=[],
            reply_id="old",
        )
        new_state = AgentState(
            session_id=SESSION,
            summary="new",
            context=[],
            reply_id="new",
        )
        pause_enabled = asyncio.Event()
        child_paused = asyncio.Event()
        release_child = asyncio.Event()
        original = AsyncSQLAlchemyStorage.update_session_state

        async def pausing(self, user_id, agent_id, session_id, state):
            if pause_enabled.is_set() and state.summary == "old":
                child_paused.set()
                await release_child.wait()
            return await original(self, user_id, agent_id, session_id, state)

        monkeypatch.setattr(
            AsyncSQLAlchemyStorage,
            "update_session_state",
            pausing,
        )

        owner_in_lock = asyncio.Event()
        owner_done = asyncio.Event()
        new_acquired = asyncio.Event()
        new_run_go = asyncio.Event()
        child_ref: dict = {}

        async def child() -> None:
            await storage.update_session_state(
                USER,
                AGENT,
                SESSION,
                old_state,
            )

        async def owner() -> None:
            async with bus.acquire_lock(MessageBusKeys.session_lock(SESSION)):
                scope = current_scope()
                assert scope is not None
                scope.bind(binding)
                await scope.ensure_state_entry()
                await storage.update_session_state(
                    USER,
                    AGENT,
                    SESSION,
                    old_state,
                )
                pause_enabled.set()
                child_ref["task"] = asyncio.ensure_future(child())
                await child_paused.wait()
                owner_in_lock.set()
                # 此处退出 scope：排空会等待已受理 child 真实退出。
            await child_ref["task"]
            owner_done.set()

        async def new_run() -> None:
            await new_run_go.wait()
            async with bus.acquire_lock(MessageBusKeys.session_lock(SESSION)):
                new_acquired.set()
                await storage.update_session_state(
                    USER,
                    AGENT,
                    SESSION,
                    new_state,
                )

        owner_task = asyncio.ensure_future(owner())
        await asyncio.wait_for(owner_in_lock.wait(), timeout=10)

        new_task = asyncio.ensure_future(new_run())
        new_run_go.set()
        await asyncio.sleep(0.05)
        # child 仍在真实 base 保存中：owner 未完成释放，新运行不能进锁。
        assert not owner_done.is_set()
        assert not new_acquired.is_set()

        # 由独立控制流程放行 child（不在 owner 调用栈内自锁）。
        release_child.set()
        await asyncio.wait_for(owner_task, timeout=10)
        await asyncio.wait_for(new_task, timeout=10)

        # 最终原生状态为 new（新运行最后提交），且写入证明一致。
        record = await storage.get_session(USER, AGENT, SESSION)
        assert record is not None
        assert record.state.summary == "new"
        assert await index.unconfirmed(binding) == []


@pytest.mark.asyncio
async def test_inflight_message_writer_blocks_new_run_until_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding: Binding,
):
    """message writer 同样受 lease 保护：新运行必须等其真实提交后才进锁。"""
    index, storage = await _build_storage(tmp_path)
    bus = ObservedMessageBus(index)
    async with index, storage:
        await _seed(storage)
        pause_enabled = asyncio.Event()
        child_paused = asyncio.Event()
        release_child = asyncio.Event()
        original = AsyncSQLAlchemyStorage.upsert_message

        async def pausing(self, user_id, session_id, msg):
            if (
                pause_enabled.is_set()
                and getattr(msg, "id", None) == "m-shared"
            ):
                child_paused.set()
                await release_child.wait()
            return await original(self, user_id, session_id, msg)

        monkeypatch.setattr(AsyncSQLAlchemyStorage, "upsert_message", pausing)

        owner_in_lock = asyncio.Event()
        owner_done = asyncio.Event()
        new_acquired = asyncio.Event()
        new_run_go = asyncio.Event()

        async def owner() -> None:
            async with bus.acquire_lock(MessageBusKeys.session_lock(SESSION)):
                scope = current_scope()
                assert scope is not None
                scope.bind(binding)
                await scope.ensure_state_entry()
                pause_enabled.set()
                child_ref = asyncio.ensure_future(
                    storage.upsert_message(
                        USER,
                        SESSION,
                        UserMsg(name="user", content="old", id="m-shared"),
                    ),
                )
                await child_paused.wait()
                owner_in_lock.set()
            await child_ref
            owner_done.set()

        async def new_run() -> None:
            await new_run_go.wait()
            async with bus.acquire_lock(MessageBusKeys.session_lock(SESSION)):
                new_acquired.set()
                await storage.upsert_message(
                    USER,
                    SESSION,
                    UserMsg(name="user", content="new", id="m-shared"),
                )

        owner_task = asyncio.ensure_future(owner())
        await asyncio.wait_for(owner_in_lock.wait(), timeout=10)
        new_task = asyncio.ensure_future(new_run())
        new_run_go.set()
        await asyncio.sleep(0.05)
        assert not owner_done.is_set()
        assert not new_acquired.is_set()

        release_child.set()
        await asyncio.wait_for(owner_task, timeout=10)
        await asyncio.wait_for(new_task, timeout=10)

        stored = await storage.get_message(USER, SESSION, "m-shared")
        assert stored is not None
        assert stored.get_text_content() == "new"


@pytest.mark.asyncio
async def test_repeated_cancel_owner_still_drains_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding: Binding,
):
    """重复取消 owner 仍须等真实 writer 退出后才放锁，之后新运行才能进入。"""
    index, storage = await _build_storage(tmp_path)
    bus = ObservedMessageBus(index)
    async with index, storage:
        await _seed(storage)
        old_state = AgentState(
            session_id=SESSION,
            summary="old",
            context=[],
            reply_id="old",
        )
        pause_enabled = asyncio.Event()
        child_paused = asyncio.Event()
        release_child = asyncio.Event()
        original = AsyncSQLAlchemyStorage.update_session_state

        async def pausing(self, user_id, agent_id, session_id, state):
            if pause_enabled.is_set() and state.summary == "old":
                child_paused.set()
                await release_child.wait()
            return await original(self, user_id, agent_id, session_id, state)

        monkeypatch.setattr(
            AsyncSQLAlchemyStorage,
            "update_session_state",
            pausing,
        )

        child_ref: dict = {}
        owner_in_lock = asyncio.Event()

        async def owner() -> None:
            async with bus.acquire_lock(MessageBusKeys.session_lock(SESSION)):
                scope = current_scope()
                assert scope is not None
                scope.bind(binding)
                await scope.ensure_state_entry()
                pause_enabled.set()
                child_ref["task"] = asyncio.ensure_future(
                    storage.update_session_state(
                        USER,
                        AGENT,
                        SESSION,
                        old_state,
                    ),
                )
                await child_paused.wait()
                owner_in_lock.set()

        owner_task = asyncio.ensure_future(owner())
        await asyncio.wait_for(owner_in_lock.wait(), timeout=10)

        owner_task.cancel()
        await asyncio.sleep(0.02)
        owner_task.cancel()
        await asyncio.sleep(0.02)
        # 重复取消不能提前放锁。
        assert not owner_task.done()
        release_child.set()

        await asyncio.gather(owner_task, return_exceptions=True)
        assert owner_task.done()
        # child 真实完成。
        assert child_ref["task"].done()

        # 锁已释放：新运行可进入并提交。
        async with bus.acquire_lock(MessageBusKeys.session_lock(SESSION)):
            scope = current_scope()
            assert scope is not None
            assert not scope.closed


@pytest.mark.asyncio
async def test_scope_identity_mismatch_write_rejected(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        await _seed(storage, "agent-other", "session-other")
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        token = _CURRENT_SCOPE.set(scope)
        try:
            # 同一上下文中的不同 session 写入必须 fail-closed，不当合法嵌套。
            with pytest.raises(PersistenceIndexConflictError):
                await storage.upsert_message(
                    USER,
                    "session-other",
                    UserMsg(name="user", content="x", id="m-other"),
                )
        finally:
            _CURRENT_SCOPE.reset(token)


# ----------------------------------------------------------------------
# 修复 2：实际 State.session_id 身份校验
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_state_session_id_mismatch_rejected(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        token = _CURRENT_SCOPE.set(scope)
        try:
            await scope.ensure_state_entry()
            foreign = AgentState(
                session_id="other-session",
                summary="x",
                context=[],
                reply_id="r",
            )
            with pytest.raises(PersistenceIndexConflictError):
                await storage.update_session_state(
                    USER,
                    AGENT,
                    SESSION,
                    foreign,
                )
            # 外来 State 未被提交，State 期待仍是 unknown。
            record = await storage.get_session(USER, AGENT, SESSION)
            assert record is not None
            assert record.state.summary != "x"
            pending = await index.unconfirmed(binding)
            assert len(pending) == 1
            assert pending[0].kind == "state"
            assert pending[0].digest is None
        finally:
            _CURRENT_SCOPE.reset(token)


# ----------------------------------------------------------------------
# 修复 3：continuation 读失败/缺失/伪造/partial/no-START
# ----------------------------------------------------------------------
def _parked_agent(reply_id: str = "r-real") -> types.SimpleNamespace:
    call = ToolCallBlock(
        id="call-1",
        name="tool",
        input="{}",
        state=ToolCallState.ASKING,
    )
    last = AssistantMsg(id=reply_id, name="agent", content=[call])
    state = AgentState(
        session_id=SESSION,
        summary="",
        context=[last],
        reply_id=reply_id,
    )
    return types.SimpleNamespace(name="agent", state=state)


async def _drive_on_reply(middleware, agent, inputs, events=()) -> list:
    async def next_handler(**kwargs):
        for event in events:
            yield event

    collected: list = []
    async for item in middleware.on_reply(
        agent,
        {"inputs": inputs},
        next_handler,
    ):
        collected.append(item)
    return collected


@pytest.mark.asyncio
async def test_continuation_read_failure_keeps_unknown_expectation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        middleware = ObservationMiddleware(USER, AGENT, SESSION, storage)
        agent = _parked_agent("r-real")

        async def boom(*args, **kwargs):
            raise OSError("read failed")

        monkeypatch.setattr(storage, "get_message", boom)
        token = _CURRENT_SCOPE.set(scope)
        try:
            with pytest.raises(OSError):
                await _drive_on_reply(
                    middleware,
                    agent,
                    UserConfirmResultEvent(
                        reply_id="r-real",
                        confirm_results=[],
                    ),
                )
            # 期待已登记且保留 unknown（不报告健康）。
            pending = await index.unconfirmed(binding)
            subjects = {(p.kind, p.subject_id) for p in pending}
            assert ("message", "r-real") in subjects
            message_entry = next(
                p for p in pending if p.kind == "message"
            )
            assert message_entry.digest is None
        finally:
            _CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_continuation_missing_old_msg_keeps_unknown_expectation(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        middleware = ObservationMiddleware(USER, AGENT, SESSION, storage)
        agent = _parked_agent("r-missing")
        token = _CURRENT_SCOPE.set(scope)
        try:
            with pytest.raises(Exception):
                await _drive_on_reply(
                    middleware,
                    agent,
                    ExternalExecutionResultEvent(
                        reply_id="r-missing",
                        execution_results=[],
                    ),
                )
            pending = await index.unconfirmed(binding)
            message_entry = next(
                p for p in pending if p.kind == "message"
            )
            assert message_entry.subject_id == "r-missing"
            assert message_entry.digest is None
        finally:
            _CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_continuation_uses_trusted_reply_id_not_forged_input(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        old = _assistant("r-real", "first")
        await storage.upsert_message(USER, SESSION, old)
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        middleware = ObservationMiddleware(USER, AGENT, SESSION, storage)
        agent = _parked_agent("r-real")
        token = _CURRENT_SCOPE.set(scope)
        try:
            # 输入自报 reply_id 伪造为 r-forged，但可信来源是 agent.state.reply_id。
            await _drive_on_reply(
                middleware,
                agent,
                UserConfirmResultEvent(reply_id="r-forged", confirm_results=[]),
            )
            assert "r-real" in scope.replies
            assert "r-forged" not in scope.replies
            observed = scope.replies["r-real"]
            assert observed.msg is not None
            assert observed.msg.id == "r-real"
            assert observed.msg.name == "agent"
        finally:
            _CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_continuation_partial_no_start_unknown_not_cleared_by_readback(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        old = _assistant("r-part", "first")
        await storage.upsert_message(USER, SESSION, old)
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        middleware = ObservationMiddleware(USER, AGENT, SESSION, storage)
        agent = _parked_agent("r-part")
        token = _CURRENT_SCOPE.set(scope)
        try:
            # 续写只观察，没有后续真实写入（partial/no-START）。
            await _drive_on_reply(
                middleware,
                agent,
                UserConfirmResultEvent(reply_id="r-part", confirm_results=[]),
            )
        finally:
            _CURRENT_SCOPE.reset(token)

        # readback 不得清掉 unknown message 期待。
        report = await readback_verify(index, storage, binding)
        reasons = {r.subject_id: r.confirmed for r in report.results}
        assert reasons.get("r-part") is False
        pending = await index.unconfirmed(binding)
        assert any(
            p.kind == "message" and p.subject_id == "r-part" and p.digest is None
            for p in pending
        )


# ----------------------------------------------------------------------
# 修复 4：UserInterruptEvent 续写
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_user_interrupt_parked_is_observed(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        old = _assistant("r-int", "first")
        await storage.upsert_message(USER, SESSION, old)
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        middleware = ObservationMiddleware(USER, AGENT, SESSION, storage)
        agent = _parked_agent("r-int")
        token = _CURRENT_SCOPE.set(scope)
        try:
            await _drive_on_reply(
                middleware,
                agent,
                UserInterruptEvent(reply_id="r-int"),
            )
            assert "r-int" in scope.replies
            observed = scope.replies["r-int"]
            assert observed.msg is not None
            assert observed.msg.id == "r-int"
        finally:
            _CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_user_interrupt_idle_noop_creates_no_expectation(
    tmp_path: Path,
    binding: Binding,
):
    index, storage = await _build_storage(tmp_path)
    async with index, storage:
        await _seed(storage)
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        middleware = ObservationMiddleware(USER, AGENT, SESSION, storage)
        # 非 parked：state 无等待工具调用。
        idle = types.SimpleNamespace(
            name="agent",
            state=AgentState(
                session_id=SESSION,
                summary="",
                context=[],
                reply_id="r-idle",
            ),
        )
        token = _CURRENT_SCOPE.set(scope)
        try:
            await _drive_on_reply(
                middleware,
                idle,
                UserInterruptEvent(reply_id="r-idle"),
            )
            assert scope.replies == {}
        finally:
            _CURRENT_SCOPE.reset(token)


# ----------------------------------------------------------------------
# 锁所有权/清理缺陷（真实 Task + Event）
# ----------------------------------------------------------------------
class _ControllableDrain:
    """可控 drain：暴露内部 waiter 供测试取消，完成条件仍是 is_set。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.last_waiter: asyncio.Future | None = None

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    def wait(self):
        self.last_waiter = asyncio.ensure_future(self._event.wait())
        return self.last_waiter


@pytest.mark.asyncio
async def test_inherited_scope_child_cannot_acquire_parent_session_lock(
    tmp_path: Path,
    binding: Binding,
):
    """ContextVar 继承 ≠ 锁所有权：继承 scope 的子 Task 不能用父持有的运行锁。"""
    index = PersistenceIndex(tmp_path)
    bus = ObservedMessageBus(index)
    async with index:
        child_done = asyncio.Event()
        owner_in_lock = asyncio.Event()
        owner_exited = asyncio.Event()
        result: dict = {}

        async def child() -> None:
            try:
                async with asyncio.timeout(0.2):
                    async with bus.acquire_lock(
                        MessageBusKeys.session_lock(SESSION),
                    ):
                        result["entered"] = True
            except BaseException as exc:  # noqa: BLE001 - 记录后断言
                result["error"] = exc
            finally:
                child_done.set()

        async def owner() -> None:
            async with bus.acquire_lock(MessageBusKeys.session_lock(SESSION)):
                scope = current_scope()
                assert scope is not None
                scope.bind(binding)
                # 子 Task 在 owner 上下文内创建 → 继承 scope。
                child_task = asyncio.ensure_future(child())
                owner_in_lock.set()
                await asyncio.wait_for(child_done.wait(), timeout=10)
                await child_task
            owner_exited.set()

        owner_task = asyncio.ensure_future(owner())
        await asyncio.wait_for(owner_in_lock.wait(), timeout=10)
        await asyncio.wait_for(owner_exited.wait(), timeout=10)
        await owner_task

        # 子 Task 明确被拒绝，未进入父持有的运行锁。
        assert "entered" not in result
        assert isinstance(
            result.get("error"),
            PersistenceObservationClosedError,
        )


@pytest.mark.asyncio
async def test_lock_wait_timeout_leaves_no_residual_scope(
    tmp_path: Path,
    binding: Binding,
):
    """等待运行锁超时/取消不遗留 scope，再次等待仍不能绕过真实持锁者。"""
    index = PersistenceIndex(tmp_path)
    bus = ObservedMessageBus(index)
    async with index:
        holder_in_lock = asyncio.Event()
        release_holder = asyncio.Event()
        result: dict = {}

        async def holder() -> None:
            async with bus.acquire_lock(MessageBusKeys.session_lock(SESSION)):
                holder_in_lock.set()
                await release_holder.wait()

        async def waiter() -> None:
            try:
                async with asyncio.timeout(0.05):
                    async with bus.acquire_lock(
                        MessageBusKeys.session_lock(SESSION),
                    ):
                        result["entered1"] = True
            except TimeoutError:
                result["timeout1"] = True
            result["scope_after1"] = current_scope()

            try:
                async with asyncio.timeout(0.05):
                    async with bus.acquire_lock(
                        MessageBusKeys.session_lock(SESSION),
                    ):
                        result["entered2"] = True
            except TimeoutError:
                result["timeout2"] = True
            result["scope_after2"] = current_scope()

        holder_task = asyncio.ensure_future(holder())
        await asyncio.wait_for(holder_in_lock.wait(), timeout=10)
        waiter_task = asyncio.ensure_future(waiter())
        await asyncio.wait_for(waiter_task, timeout=10)

        assert result.get("timeout1") is True
        assert result.get("timeout2") is True
        assert "entered1" not in result
        assert "entered2" not in result
        assert result.get("scope_after1") is None
        assert result.get("scope_after2") is None

        release_holder.set()
        await asyncio.wait_for(holder_task, timeout=10)


@pytest.mark.asyncio
async def test_drain_internal_waiter_cancel_keeps_waiting_for_writer(
    tmp_path: Path,
    binding: Binding,
):
    """内部 drain waiter 被取消时仍以 writer 归零为完成，之后才安全传播错误。"""
    index = PersistenceIndex(tmp_path)
    async with index:
        scope = ObservationScope(SESSION, index)
        scope.bind(binding)
        scope.admit_writer(SESSION, USER)  # 真实 writer 在途，active=1
        drain = _ControllableDrain()
        scope._drained = drain  # type: ignore[assignment]

        task = asyncio.ensure_future(scope.seal_and_drain())
        for _ in range(2000):
            if drain.last_waiter is not None:
                break
            await asyncio.sleep(0.001)
        assert drain.last_waiter is not None

        # 取消内部 Event.wait waiter：不能因此认为排空完成而提前放锁。
        drain.last_waiter.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()

        # 真实 writer 退出后，排空完成并传播被保留的错误。
        scope.release_writer()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        assert scope._active_writers == 0
        assert scope.sealed is True

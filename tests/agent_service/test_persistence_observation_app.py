# -*- coding: utf-8 -*-
"""原生持久化写入观察的官方 app 集成测试（OpenSpec 任务 3.2.3.1）。

通过官方 ``build_app`` + 官方 FastAPI 应用客户端 + recording 公开 ``ChatModelBase``
驱动**真实公开 ChatService 运行路径**（而非手动调用子类），验证真实消息与 State
提交、持久索引确认，以及 readback 复核。
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel

from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    AsyncSQLAlchemyStorage,
    ChatModelConfig,
    SessionConfig,
)
from agentscope.credential import CredentialBase, CredentialFactory
from agentscope.event import ReplyFinishedReason
from agentscope.message import TextBlock
from agentscope.model import ChatModelBase, ChatResponse

from nexus_agent_service.app import build_app
from nexus_agent_service.config import ServiceConfig
from nexus_agent_service.guard import RunAuthorization
from nexus_agent_service.persistence_index import Binding, PersistenceIndex
from nexus_agent_service.persistence_observation import readback_verify

USER = "nexus-service"
AGENT = "agent-app-1"
SESSION = "session-app-1"
CREDENTIAL_ID = "cred-recording"


class RecordingChatModel(ChatModelBase):
    """使用真实公开 ``ChatModelBase`` 的 recording 模型（非 AI）。"""

    class Parameters(BaseModel):
        pass

    def __init__(self, credential=None, model="recording", parameters=None, **kwargs):
        from agentscope.formatter import DeepSeekChatFormatter

        self.credential = credential
        self.model = model
        self.parameters = parameters or RecordingChatModel.Parameters()
        self.stream = False
        self.max_retries = 0
        self.retry_delay = 0
        self.context_size = 32768
        self.formatter = DeepSeekChatFormatter()
        self.calls = 0

    @classmethod
    def list_models(cls):
        return []

    async def __call__(self, messages=None, tools=None, tool_choice=None):
        self.calls += 1
        return ChatResponse(
            content=[TextBlock(text="ANSWER")],
            is_last=True,
        )


class RecordingCredential(CredentialBase):
    type: Literal["nexus_recording_credential"] = "nexus_recording_credential"

    @classmethod
    def get_chat_model_class(cls):
        return RecordingChatModel


class FailingChatModel(RecordingChatModel):
    """在真正调用模型时抛错，触发原生 hook 外 ERROR 终态。"""

    async def __call__(self, messages=None, tools=None, tool_choice=None):
        self.calls += 1
        raise RuntimeError("model exploded")


class FailingRecordingCredential(CredentialBase):
    type: Literal["nexus_failing_recording_credential"] = (
        "nexus_failing_recording_credential"
    )

    @classmethod
    def get_chat_model_class(cls):
        return FailingChatModel


class AllowAuthorizer:
    async def authorize(self, user_id, agent_id, session_id):
        return RunAuthorization(allowed=True, reason="ok")

    async def aclose(self):
        return None


def _config(data_dir: Path) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8766,
        data_dir=data_dir,
        internal_user_id=USER,
        model_api_key=None,
        model_base_url="https://api.deepseek.com",
        model_name="recording",
        credential_id=CREDENTIAL_ID,
        credential_name="recording",
    )


def _confirmed_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT kind, COUNT(*) FROM persistence_index "
            "WHERE confirmed = 1 GROUP BY kind;",
        ).fetchall()
    finally:
        conn.close()
    return {kind: count for kind, count in rows}


def _all_kinds(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT kind, COUNT(*) FROM persistence_index GROUP BY kind;",
        ).fetchall()
    finally:
        conn.close()
    return {kind: count for kind, count in rows}


async def _await_run(app, session_id: str, timeout: float = 20.0) -> None:
    registry = app.state.chat_run_registry
    deadline = asyncio.get_event_loop().time() + timeout
    task = None
    while asyncio.get_event_loop().time() < deadline:
        task = registry.get(session_id)
        if task is not None:
            break
        await asyncio.sleep(0.01)
    assert task is not None, "chat run task 未在期望时间内出现"
    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


async def _seed_agent_session(storage, agent_id: str, session_id: str) -> None:
    await storage.upsert_agent(
        USER,
        AgentRecord(
            id=agent_id,
            user_id=USER,
            data=AgentData(id=agent_id, name="agent", system_prompt="p"),
        ),
    )
    await storage.upsert_session(
        USER,
        agent_id,
        SessionConfig(
            workspace_id=f"ws-{session_id}",
            chat_model_config=ChatModelConfig(
                type="nexus_recording_credential",
                credential_id=CREDENTIAL_ID,
                model="recording",
                parameters={},
            ),
        ),
        session_id=session_id,
    )


async def _post_chat(client: httpx.AsyncClient, agent_id: str, session_id: str) -> None:
    resp = await client.post(
        "/chat/",
        json={
            "agent_id": agent_id,
            "session_id": session_id,
            "input": {
                "name": "user",
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            },
        },
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_official_app_run_commits_and_confirms(tmp_path: Path):
    CredentialFactory.register_credential(RecordingCredential)
    app = build_app(_config(tmp_path), guard_authorizer=AllowAuthorizer())

    async with app.router.lifespan_context(app):
        storage = app.state.storage
        # 真实引导 recording 凭据并seed 员工/Session（公开 storage API）。
        await storage.upsert_credential(
            USER,
            RecordingCredential(id=CREDENTIAL_ID, name="recording"),
        )
        await _seed_agent_session(storage, AGENT, SESSION)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            await _post_chat(client, AGENT, SESSION)
            await _await_run(app, SESSION)

        # 真实消息提交：应包含用户输入与 recording 模型回复正文。
        messages, _ = await storage.list_messages(USER, SESSION, limit=50)
        assert any(m.role == "user" for m in messages)
        assistant_texts = [
            block.text
            for m in messages
            if m.role == "assistant"
            for block in m.content
            if getattr(block, "type", None) == "text"
        ]
        assert any("ANSWER" in text for text in assistant_texts)

        # 会话 State 已真实提交。
        session_record = await storage.get_session(USER, AGENT, SESSION)
        assert session_record is not None

        # 官方运行产生的写入全部确认；readback 逐条复核无阻断。
        binding = Binding(user_id=USER, agent_id=AGENT, session_id=SESSION)
        index: PersistenceIndex = app.state.nexus_persistence_index
        assert await index.unconfirmed(binding) == []
        report = await readback_verify(index, storage, binding)
        assert report.blocked == []

    # 索引在 lifespan 内真实打开并确认：确认条目含 message 与 state。
    counts = _confirmed_counts(tmp_path / "persistence" / "persistence_index.sqlite3")
    assert counts.get("message", 0) >= 1
    assert counts.get("state", 0) >= 1


@pytest.mark.asyncio
async def test_official_app_concurrent_sessions_do_not_cross(tmp_path: Path):
    CredentialFactory.register_credential(RecordingCredential)
    app = build_app(_config(tmp_path), guard_authorizer=AllowAuthorizer())
    sessions = (("agent-x1", "session-x1"), ("agent-x2", "session-x2"))

    async with app.router.lifespan_context(app):
        storage = app.state.storage
        await storage.upsert_credential(
            USER,
            RecordingCredential(id=CREDENTIAL_ID, name="recording"),
        )
        for agent_id, session_id in sessions:
            await _seed_agent_session(storage, agent_id, session_id)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            await asyncio.gather(
                *(
                    _post_chat(client, agent_id, session_id)
                    for agent_id, session_id in sessions
                ),
            )
            await asyncio.gather(
                *(_await_run(app, session_id) for _, session_id in sessions),
            )

        index: PersistenceIndex = app.state.nexus_persistence_index
        for agent_id, session_id in sessions:
            binding = Binding(user_id=USER, agent_id=agent_id, session_id=session_id)
            assert await index.unconfirmed(binding) == []
            messages, _ = await storage.list_messages(USER, session_id, limit=50)
            assert any(m.role == "assistant" for m in messages)
            # 绑定不串：本 Session 索引条目只属于本绑定。
            report = await readback_verify(index, storage, binding)
            assert report.blocked == []


def _assistant_error_flags(messages) -> list:
    return [
        m.finished_reason
        for m in messages
        if m.role == "assistant"
    ]


@pytest.mark.asyncio
async def test_official_app_setup_failure_has_no_fake_state_expectation(
    tmp_path: Path,
):
    """装配失败只有合成诊断 ERROR 消息，绝不建立虚假 State 期待。"""
    CredentialFactory.register_credential(RecordingCredential)
    app = build_app(_config(tmp_path), guard_authorizer=AllowAuthorizer())
    agent_id, session_id = "agent-setup-fail", "session-setup-fail"

    async with app.router.lifespan_context(app):
        storage = app.state.storage
        await storage.upsert_agent(
            USER,
            AgentRecord(
                id=agent_id,
                user_id=USER,
                data=AgentData(id=agent_id, name="agent", system_prompt="p"),
            ),
        )
        # 指向不存在的凭据 → 原生装配在取模型时失败。
        await storage.upsert_session(
            USER,
            agent_id,
            SessionConfig(
                workspace_id=f"ws-{session_id}",
                chat_model_config=ChatModelConfig(
                    type="nexus_recording_credential",
                    credential_id="missing-credential",
                    model="recording",
                    parameters={},
                ),
            ),
            session_id=session_id,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            await _post_chat(client, agent_id, session_id)
            await _await_run(app, session_id)

        messages, _ = await storage.list_messages(USER, session_id, limit=50)
        assert any(
            reason == ReplyFinishedReason.ERROR
            for reason in _assistant_error_flags(messages)
        )
        binding = Binding(USER, agent_id, session_id)
        index: PersistenceIndex = app.state.nexus_persistence_index
        assert await index.unconfirmed(binding) == []

    # 没有 State 写入入口 → 索引里绝不出现 state 条目。
    kinds = _all_kinds(tmp_path / "persistence" / "persistence_index.sqlite3")
    assert "state" not in kinds
    assert kinds.get("message", 0) >= 1


@pytest.mark.asyncio
async def test_official_app_hook_external_error_terminal_confirmed(
    tmp_path: Path,
):
    """普通运行中模型报错：hook 外补的 ERROR 终态也被核对确认。"""
    CredentialFactory.register_credential(RecordingCredential)
    CredentialFactory.register_credential(FailingRecordingCredential)
    app = build_app(_config(tmp_path), guard_authorizer=AllowAuthorizer())
    agent_id, session_id = "agent-err", "session-err"

    async with app.router.lifespan_context(app):
        storage = app.state.storage
        await storage.upsert_credential(
            USER,
            FailingRecordingCredential(id="cred-failing", name="failing"),
        )
        await storage.upsert_agent(
            USER,
            AgentRecord(
                id=agent_id,
                user_id=USER,
                data=AgentData(id=agent_id, name="agent", system_prompt="p"),
            ),
        )
        await storage.upsert_session(
            USER,
            agent_id,
            SessionConfig(
                workspace_id=f"ws-{session_id}",
                chat_model_config=ChatModelConfig(
                    type="nexus_failing_recording_credential",
                    credential_id="cred-failing",
                    model="recording",
                    parameters={},
                ),
            ),
            session_id=session_id,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            await _post_chat(client, agent_id, session_id)
            await _await_run(app, session_id)

        messages, _ = await storage.list_messages(USER, session_id, limit=50)
        assert any(
            reason == ReplyFinishedReason.ERROR
            for reason in _assistant_error_flags(messages)
        )
        binding = Binding(USER, agent_id, session_id)
        index: PersistenceIndex = app.state.nexus_persistence_index
        assert await index.unconfirmed(binding) == []
        report = await readback_verify(index, storage, binding)
        assert report.blocked == []


@pytest.mark.asyncio
async def test_official_app_message_confirm_failure_short_circuits_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """消息 confirm 异常短路：State writer 从未进入，unknown State 期待仍在。"""
    CredentialFactory.register_credential(RecordingCredential)
    app = build_app(_config(tmp_path), guard_authorizer=AllowAuthorizer())
    agent_id, session_id = "agent-short", "session-short"

    async with app.router.lifespan_context(app):
        storage = app.state.storage
        await storage.upsert_credential(
            USER,
            RecordingCredential(id=CREDENTIAL_ID, name="recording"),
        )
        await _seed_agent_session(storage, agent_id, session_id)
        index: PersistenceIndex = app.state.nexus_persistence_index

        # 第 1 次 confirm 是 user 输入；第 2 次是 _persist 的回复消息，
        # 让其失败即可短路后续 State 写入。
        calls = {"n": 0}
        original_confirm = index.confirm

        async def failing_confirm(binding, entry_id, digest):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("index confirm failed")
            return await original_confirm(binding, entry_id, digest)

        monkeypatch.setattr(index, "confirm", failing_confirm)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            await _post_chat(client, agent_id, session_id)
            await _await_run(app, session_id)

        binding = Binding(USER, agent_id, session_id)
        monkeypatch.setattr(index, "confirm", original_confirm)
        pending = await index.unconfirmed(binding)
        state_entries = [p for p in pending if p.kind == "state"]
        assert len(state_entries) == 1
        assert state_entries[0].digest is None


@pytest.mark.asyncio
async def test_official_app_cancel_with_slow_failing_save_keeps_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """取消遇到慢且失败的真实保存：不假确认，State 期待保持 pending。"""
    CredentialFactory.register_credential(RecordingCredential)
    app = build_app(_config(tmp_path), guard_authorizer=AllowAuthorizer())
    agent_id, session_id = "agent-cancel", "session-cancel"

    async with app.router.lifespan_context(app):
        storage = app.state.storage
        await storage.upsert_credential(
            USER,
            RecordingCredential(id=CREDENTIAL_ID, name="recording"),
        )
        await _seed_agent_session(storage, agent_id, session_id)

        pause = asyncio.Event()
        release = asyncio.Event()

        async def slow_failing_state(self, user_id, agent_id, session_id, state):
            pause.set()
            await release.wait()
            raise RuntimeError("native state save failed")

        monkeypatch.setattr(
            AsyncSQLAlchemyStorage,
            "update_session_state",
            slow_failing_state,
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            await _post_chat(client, agent_id, session_id)
            registry = app.state.chat_run_registry
            task = None
            for _ in range(2000):
                task = registry.get(session_id)
                if task is not None:
                    break
                await asyncio.sleep(0.005)
            assert task is not None
            await asyncio.wait_for(pause.wait(), timeout=10)
            task.cancel()
            release.set()
            await asyncio.gather(task, return_exceptions=True)

        binding = Binding(USER, agent_id, session_id)
        index: PersistenceIndex = app.state.nexus_persistence_index
        pending = await index.unconfirmed(binding)
        state_entries = [p for p in pending if p.kind == "state"]
        assert len(state_entries) == 1
        # 已冻结 digest 但真实保存失败/被取消，绝不被确认。
        assert state_entries[0].confirmed is False

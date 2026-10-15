# -*- coding: utf-8 -*-
"""运行 admission 冻结、Task 排空与落盘核对（OpenSpec 任务 3.2.3.1）。

使用真实官方 ``build_app`` + 官方 FastAPI 客户端 + 公开 recording ``ChatModelBase``
驱动真实公开 ChatService 运行路径，配合可控的 Nexus 事实核对器，覆盖：

* admission 在真实 ``POST /chat/`` 按 Nexus 事实拒绝（deleting/未知绑定）；
* 冻结真实在途运行（慢模型）并排空，重复 quiesce 不二次取消；
* Session 运行锁在等待前/取得后按冻结事实拒绝迟到 native wakeup，且**不**新增
  assistant error 尾部；
* 撤销/重启后仍按 Nexus 事实拒绝（集合为空不得放行）；
* 落盘核对失败时 ``persistence_verified`` 为 False。

recording 模型明确非 AI；这些是组件/框架确定性交错验收，不是真实模型进程尾验证。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel

from agentscope.app.message_bus import MessageBusKeys
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    ChatModelConfig,
    SessionConfig,
)
from agentscope.credential import CredentialBase, CredentialFactory
from agentscope.message import TextBlock
from agentscope.model import ChatModelBase, ChatResponse

from nexus_agent_service.app import build_app
from nexus_agent_service.config import ServiceConfig
from nexus_agent_service.freeze import (
    FreezeController,
    FreezeError,
    RunFact,
    RunFrozenError,
    SessionDrainState,
)
from nexus_agent_service.guard import RunAuthorization
from nexus_agent_service.persistence_index import Binding, PersistenceIndex
from nexus_agent_service.persistence_observation import ObservedMessageBus

USER = "nexus-service"
AGENT = "agent-freeze-1"
SESSION = "session-freeze-1"
CREDENTIAL_ID = "cred-freeze-recording"
TOKEN = "freeze-internal-token"


class _Gate:
    """进程内可复位的确定性闸门（供慢模型暂停/放行）。"""

    entered = asyncio.Event()
    release = asyncio.Event()

    @classmethod
    def reset(cls) -> None:
        cls.entered = asyncio.Event()
        cls.release = asyncio.Event()


class SlowRecordingChatModel(ChatModelBase):
    """真实公开 ``ChatModelBase`` 的 recording 模型；调用时阻塞在闸门。"""

    class Parameters(BaseModel):
        pass

    def __init__(self, credential=None, model="recording", parameters=None, **kwargs):
        from agentscope.formatter import DeepSeekChatFormatter

        self.credential = credential
        self.model = model
        self.parameters = parameters or SlowRecordingChatModel.Parameters()
        self.stream = False
        self.max_retries = 0
        self.retry_delay = 0
        self.context_size = 32768
        self.formatter = DeepSeekChatFormatter()

    @classmethod
    def list_models(cls):
        return []

    async def __call__(self, messages=None, tools=None, tool_choice=None):
        _Gate.entered.set()
        await _Gate.release.wait()
        return ChatResponse(content=[TextBlock(text="ANSWER")], is_last=True)


class RecordingCredential(CredentialBase):
    type: Literal["nexus_freeze_recording_credential"] = (
        "nexus_freeze_recording_credential"
    )

    @classmethod
    def get_chat_model_class(cls):
        return SlowRecordingChatModel


class AllowAuthorizer:
    async def authorize(self, user_id, agent_id, session_id):
        return RunAuthorization(allowed=True, reason="ok")

    async def aclose(self):
        return None


class FakeFreezeVerifier:
    """可控 Nexus 事实核对器（不冒充真实服务）。"""

    def __init__(self) -> None:
        self.runnable = True
        self.deleting = False
        self.operation_id: str | None = None
        self.fail = False
        self.calls: list[tuple[str | None, str | None]] = []

    async def check(self, *, agent_id=None, session_id=None) -> RunFact:
        self.calls.append((agent_id, session_id))
        if self.fail:
            return RunFact(False, False, None, None, None, None, "freeze_unavailable")
        return RunFact(
            runnable=self.runnable,
            deleting=self.deleting,
            operation_id=self.operation_id,
            agent_id=agent_id,
            employee_id="emp-1",
            session_id=session_id,
            reason="ok",
        )

    async def aclose(self) -> None:
        return None


class RaisingFreezeVerifier:
    """模拟新服务代次启动即查询失败：任何 check 都抛异常（fail-closed）。"""

    async def check(self, *, agent_id=None, session_id=None) -> RunFact:
        raise RuntimeError("nexus run-fact unavailable")

    async def aclose(self) -> None:
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
        nexus_internal_token=TOKEN,
    )


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
                type="nexus_freeze_recording_credential",
                credential_id=CREDENTIAL_ID,
                model="recording",
                parameters={},
            ),
        ),
        session_id=session_id,
    )


def _internal_headers():
    return {"X-Nexus-Internal-Token": TOKEN, "X-User-ID": USER}


async def _post_chat(client, agent_id=AGENT, session_id=SESSION):
    return await client.post(
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


async def _wait_task(app, session_id, timeout=10.0):
    registry = app.state.chat_run_registry
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        task = registry.get(session_id)
        if task is not None:
            return task
        await asyncio.sleep(0.005)
    raise AssertionError("chat run task 未出现")


@pytest.fixture(autouse=True)
def _reset_gate():
    _Gate.reset()
    yield
    _Gate.release.set()


# ----------------------------------------------------------------------
# 单元：Session 运行锁冻结（等待前/取得后）
# ----------------------------------------------------------------------
class _AlwaysFrozenVerifier:
    async def check(self, *, agent_id=None, session_id=None) -> RunFact:
        return RunFact(False, False, None, None, None, None, "down")

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_bus_lock_rejected_when_frozen(tmp_path):
    index = PersistenceIndex(tmp_path / "idx")
    bus = ObservedMessageBus(index)
    controller = FreezeController(
        app=_FakeAppForBus(),
        verifier=_AlwaysFrozenVerifier(),
        user_id=USER,
    )
    bus.set_freeze_controller(controller)
    from agentscope.app.message_bus import MessageBusKeys

    async with index:
        with pytest.raises(RunFrozenError):
            async with bus.acquire_lock(
                MessageBusKeys.session_lock(SESSION),
            ):
                pass


class _FakeAppForBus:
    """仅供 Bus 冻结单测使用的最小 app.state 占位。"""

    class state:  # noqa: N801
        chat_run_registry = None
        background_task_manager = None
        message_bus = None
        storage = None


@pytest.mark.asyncio
async def test_controller_session_blocked_matrix():
    controller = FreezeController(
        app=_FakeAppForBus(),
        verifier=FakeFreezeVerifier(),
        user_id=USER,
    )
    assert await controller.session_blocked(SESSION) is False
    # 查询失败：不得因空缓存放行。
    controller._verifier.fail = True
    assert await controller.session_blocked("other") is True
    # deleting：阻止。
    controller._verifier.fail = False
    controller._verifier.deleting = True
    assert await controller.session_blocked(SESSION) is True
    # 运行中不可运行：阻止。
    controller._verifier.deleting = False
    controller._verifier.runnable = False
    assert await controller.session_blocked(SESSION) is True
    # 本地冻结集合：阻止。
    controller._frozen_sessions.add("frozen-sess")
    controller._verifier.runnable = True
    assert await controller.session_blocked("frozen-sess") is True


@pytest.mark.asyncio
async def test_request_cancel_once_does_not_double_cancel():
    """首次接管已在取消中的真实 Task：只登记对象，不二次 cancel 打断保存尾部。"""
    controller = FreezeController(
        app=_FakeAppForBus(),
        verifier=FakeFreezeVerifier(),
        user_id=USER,
    )
    in_finally = asyncio.Event()
    finalized = asyncio.Event()

    async def work() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            in_finally.set()
            await asyncio.sleep(0.2)
            finalized.set()

    task = asyncio.ensure_future(work())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait_for(in_finally.wait(), timeout=5)
    assert task.cancelling() == 1

    controller._request_cancel_once(task)
    # 已在取消中的真实 Task 不得被二次 cancel（计数不增）。
    assert task.cancelling() == 1
    results = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    # 真实保存尾部完整跑完（未被二次取消打断）。
    assert finalized.is_set()

    # 同一真实对象/操作重试：仍不取消。
    controller._request_cancel_once(task)
    assert task.cancelling() == 1


class _FakeState:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class _FakeBusNoRuns:
    async def is_locked(self, key) -> bool:
        return False

    async def registry_getall(self, namespace) -> dict:
        return {}


class _FakeRegistryEmpty:
    def get(self, session_id):
        return None


class _FakeBgManagerEmpty:
    tasks: dict = {}


class _FakeStorageEmpty:
    async def get_session(self, user_id, agent_id, session_id):
        return None


class _FakeIndexEmpty:
    async def unconfirmed(self, binding) -> list:
        return []


@pytest.mark.asyncio
async def test_build_report_flags_captured_alive_task():
    """registry 空/锁空/无 BG，但捕获的真实 Task 仍存活：绝不误报已排空。"""
    app = _FakeAppForBus()
    app.state = _FakeState(
        message_bus=_FakeBusNoRuns(),
        chat_run_registry=_FakeRegistryEmpty(),
        background_task_manager=_FakeBgManagerEmpty(),
        storage=_FakeStorageEmpty(),
        nexus_persistence_index=_FakeIndexEmpty(),
    )
    controller = FreezeController(
        app=app,
        verifier=FakeFreezeVerifier(),
        user_id=USER,
    )

    async def never() -> None:
        await asyncio.Event().wait()

    task = asyncio.ensure_future(never())
    captured = {
        SESSION: SessionDrainState(
            session_id=SESSION,
            chat_task=task,
            chat_alive_at_capture=True,
        ),
    }
    try:
        report = await controller._build_report(
            operation_id="op",
            agent_id=AGENT,
            captured=captured,
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert report.execution_drained is False
    assert report.persistence_verified is False
    assert report.accepted_input_archival == "not_verified"
    session = report.sessions[0]
    assert session["captured_chat_present"] is True
    assert session["captured_chat_alive"] is True
    assert session["captured_chat_alive_at_capture"] is True
    assert session["persistence_verified"] is False
    assert "execution_not_drained" in session["reasons"]


class _MultiStorage:
    async def get_session(self, user_id, agent_id, session_id):
        from types import SimpleNamespace

        from agentscope.state import AgentState

        return SimpleNamespace(
            user_id=user_id,
            agent_id=agent_id,
            id=session_id,
            state=AgentState(session_id=session_id),
        )


class _EmptyIndex:
    async def unconfirmed(self, binding) -> list:
        return []


@pytest.mark.asyncio
async def test_build_report_multisession_late_registry_blocks(monkeypatch):
    """多 Session 交错：读回窗口内落在已读回 Session 的 late Task 必须阻断。"""
    from types import SimpleNamespace

    from agentscope.app.message_bus import InMemoryMessageBus

    import nexus_agent_service.freeze as freeze_module

    registry = SimpleNamespace(tasks={})
    registry.get = registry.tasks.get
    app = SimpleNamespace(
        state=SimpleNamespace(
            storage=_MultiStorage(),
            nexus_persistence_index=_EmptyIndex(),
            message_bus=InMemoryMessageBus(),
            chat_run_registry=registry,
            background_task_manager=SimpleNamespace(tasks={}),
        ),
    )
    controller = FreezeController(
        app=app,
        verifier=FakeFreezeVerifier(),
        user_id=USER,
    )

    original = freeze_module.readback_verify
    entered_second = asyncio.Event()
    release_second = asyncio.Event()

    async def gated(index, storage, binding):
        if binding.session_id == "s2":
            entered_second.set()
            await release_second.wait()
        return await original(index, storage, binding)

    monkeypatch.setattr(freeze_module, "readback_verify", gated)

    build = asyncio.ensure_future(
        controller._build_report(
            operation_id="op",
            agent_id=AGENT,
            captured={
                "s1": SessionDrainState(session_id="s1", chat_task=None),
                "s2": SessionDrainState(session_id="s2", chat_task=None),
            },
        ),
    )

    async def late_never() -> None:
        await asyncio.Event().wait()

    late = asyncio.ensure_future(late_never())
    try:
        # s1 已读回并复查；在 s2 读回窗口内把 late Task 登记到 s1 registry。
        await asyncio.wait_for(entered_second.wait(), 5)
        registry.tasks["s1"] = late
        release_second.set()
        report = await build
    finally:
        release_second.set()
        late.cancel()
        await asyncio.gather(late, return_exceptions=True)
        if not build.done():
            await build

    # 最终统一复核必须阻断，且刷新 s1 行事实，绝不沿用旧存活 false。
    assert report.execution_drained is False
    assert report.persistence_verified is False
    entries = {s["session_id"]: s for s in report.sessions}
    assert entries["s1"]["chat_task_present"] is True
    assert entries["s1"]["persistence_verified"] is False


# ----------------------------------------------------------------------
# integration：真实 app
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admission_rejects_deleting_without_running(tmp_path: Path):
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    verifier.deleting = True
    verifier.operation_id = "op-1"
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await app.state.storage.upsert_credential(
            USER,
            RecordingCredential(id=CREDENTIAL_ID, name="recording"),
        )
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            resp = await _post_chat(client)
            assert resp.status_code == 409
        # 未登记运行、未落库任何消息。
        assert app.state.chat_run_registry.get(SESSION) is None
        messages, _ = await app.state.storage.list_messages(USER, SESSION, limit=50)
        assert messages == []


@pytest.mark.asyncio
async def test_admission_denies_unknown_binding(tmp_path: Path):
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    # 会话不存在 → Nexus 事实不可运行（模拟 403 转成的不可运行）。
    verifier.runnable = False
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            resp = await _post_chat(client, agent_id="ghost", session_id="ghost")
            assert resp.status_code == 409


@pytest.mark.asyncio
async def test_freeze_drains_inflight_and_is_idempotent(tmp_path: Path):
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await app.state.storage.upsert_credential(
            USER,
            RecordingCredential(id=CREDENTIAL_ID, name="recording"),
        )
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            resp = await _post_chat(client)
            assert resp.status_code == 200, resp.text
            task = await _wait_task(app, SESSION)
            await asyncio.wait_for(_Gate.entered.wait(), timeout=10)
            assert not task.done()

            # Nexus 事实切到 deleting + 操作身份。
            verifier.deleting = True
            verifier.operation_id = "op-drain"
            freeze_resp = await client.post(
                "/nexus/internal/freeze",
                json={
                    "operation_id": "op-drain",
                    "agent_id": AGENT,
                    "session_ids": [SESSION],
                    "timeout_secs": 10.0,
                },
                headers=_internal_headers(),
            )
            assert freeze_resp.status_code == 200, freeze_resp.text
            report = freeze_resp.json()
            assert report["execution_drained"] is True
            assert report["persistence_verified"] is True
            assert report["accepted_input_archival"] == "not_verified"
            assert app.state.chat_run_registry.get(SESSION) is None

            # 重复 quiesce：不得二次取消、结果稳定。
            second = await client.post(
                "/nexus/internal/freeze",
                json={
                    "operation_id": "op-drain",
                    "agent_id": AGENT,
                    "session_ids": [SESSION],
                    "timeout_secs": 10.0,
                },
                headers=_internal_headers(),
            )
            assert second.status_code == 200
            assert second.json()["execution_drained"] is True


@pytest.mark.asyncio
async def test_freeze_rejects_mismatched_operation(tmp_path: Path):
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    verifier.deleting = True
    verifier.operation_id = "real-op"
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/nexus/internal/freeze",
                json={
                    "operation_id": "wrong-op",
                    "agent_id": AGENT,
                    "session_ids": [SESSION],
                    "timeout_secs": 5.0,
                },
                headers=_internal_headers(),
            )
            assert resp.status_code == 409


@pytest.mark.asyncio
async def test_freeze_rejects_unknown_session_binding(tmp_path: Path):
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    verifier.deleting = True
    verifier.operation_id = "op-1"
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/nexus/internal/freeze",
                json={
                    "operation_id": "op-1",
                    "agent_id": AGENT,
                    "session_ids": ["not-real-session"],
                    "timeout_secs": 5.0,
                },
                headers=_internal_headers(),
            )
            assert resp.status_code == 409


@pytest.mark.asyncio
async def test_freeze_requires_internal_token(tmp_path: Path):
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/nexus/internal/freeze",
                json={
                    "operation_id": "op-1",
                    "agent_id": AGENT,
                    "session_ids": [SESSION],
                },
            )
            assert resp.status_code == 403


@pytest.mark.asyncio
async def test_freeze_rejects_wrong_internal_tenant(tmp_path: Path):
    """内部 token 正确但 X-User-ID 非内部租户：与 run-fact 一致 fail-closed。"""
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            for path in (
                "/nexus/internal/freeze",
                "/nexus/internal/unfreeze",
            ):
                resp = await client.post(
                    path,
                    json={
                        "operation_id": "op-1",
                        "agent_id": AGENT,
                        "session_ids": [SESSION],
                    },
                    headers={
                        "X-Nexus-Internal-Token": TOKEN,
                        "X-User-ID": "intruder",
                    },
                )
                assert resp.status_code == 403


def _freeze_timeout_body(token: str) -> bytes:
    base = {
        "operation_id": "op-1",
        "agent_id": AGENT,
        "session_ids": [SESSION],
    }
    raw = json.dumps(base)[:-1] + f', "timeout_secs": {token}}}'
    return raw.encode("utf-8")


@pytest.mark.parametrize("bad_timeout", [0, -1, True, False, "abc"])
@pytest.mark.asyncio
async def test_freeze_rejects_illegal_timeout(tmp_path: Path, bad_timeout):
    """HTTP 内部冻结必须拒绝 bool/<=0/非数值的非法超时。"""
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    verifier.deleting = True
    verifier.operation_id = "op-1"
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/nexus/internal/freeze",
                json={
                    "operation_id": "op-1",
                    "agent_id": AGENT,
                    "session_ids": [SESSION],
                    "timeout_secs": bad_timeout,
                },
                headers=_internal_headers(),
            )
            assert resp.status_code == 400


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.asyncio
async def test_freeze_rejects_nonfinite_timeout(tmp_path: Path, token):
    """NaN/Infinity 会让 ``<= 0`` 校验失效并失去总 deadline：必须拒绝。"""
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    verifier.deleting = True
    verifier.operation_id = "op-1"
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/nexus/internal/freeze",
                content=_freeze_timeout_body(token),
                headers={
                    **_internal_headers(),
                    "content-type": "application/json",
                },
            )
            assert resp.status_code == 400


@pytest.mark.parametrize(
    "bad_timeout",
    [0, -1, True, float("nan"), float("inf"), float("-inf"), "abc"],
)
@pytest.mark.asyncio
async def test_controller_rejects_illegal_timeout(bad_timeout):
    """控制器直调同样拒绝非法超时，不创建无界 drain Task。"""
    controller = FreezeController(
        app=_FakeAppForBus(),
        verifier=FakeFreezeVerifier(),
        user_id=USER,
    )
    with pytest.raises(FreezeError):
        await controller.freeze_and_quiesce(
            operation_id="op",
            agent_id=AGENT,
            session_ids=[SESSION],
            timeout=bad_timeout,
        )


@pytest.mark.asyncio
async def test_late_wakeup_rejected_without_error_tail(tmp_path: Path):
    """冻结后直接驱动真实 ChatService.run：锁被拒且不新增 error 尾部。"""
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    # 先正常跑一轮，产生一条真实回复。
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await app.state.storage.upsert_credential(
            USER,
            RecordingCredential(id=CREDENTIAL_ID, name="recording"),
        )
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        _Gate.release.set()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            await _post_chat(client)
            task = await _wait_task(app, SESSION)
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        messages_before, _ = await app.state.storage.list_messages(
            USER, SESSION, limit=50,
        )
        before_count = len(messages_before)

        # 冻结该会话（deleting），再触发一次迟到 native wakeup。
        verifier.deleting = True
        verifier.operation_id = "op-late"
        controller: FreezeController = app.state.nexus_freeze_controller
        async with controller.admission_guard(AGENT):
            controller._frozen_sessions.add(SESSION)
        chat_service = app.state.chat_service
        # ChatService.run 吞掉异常；冻结应使运行锁取得被拒。
        await chat_service.run(
            user_id=USER,
            session_id=SESSION,
            agent_id=AGENT,
            input_msg=None,
        )
        messages_after, _ = await app.state.storage.list_messages(
            USER, SESSION, limit=50,
        )
        assert len(messages_after) == before_count
        assert not any(
            getattr(m, "finished_reason", None) is not None
            and getattr(getattr(m, "finished_reason"), "value", None) == "error"
            for m in messages_after
        )


@pytest.mark.asyncio
async def test_run_waiting_lock_recheck_rejects_without_error_tail(
    tmp_path: Path,
):
    """等待 Session 锁期间事实切 deleting：取得锁后复查拒绝，且不写任何消息。"""
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await app.state.storage.upsert_credential(
            USER,
            RecordingCredential(id=CREDENTIAL_ID, name="recording"),
        )
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        _Gate.release.set()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            await _post_chat(client)
            task = await _wait_task(app, SESSION)
            await asyncio.wait_for(asyncio.shield(task), timeout=10)

        messages_before, _ = await app.state.storage.list_messages(
            USER,
            SESSION,
            limit=50,
        )
        before_count = len(messages_before)

        # 用同一 Bus 持有该 Session 运行锁，阻塞后续 run 在原生锁等待处。
        bus = app.state.message_bus
        key = MessageBusKeys.session_lock(SESSION)
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def holder() -> None:
            async with bus.acquire_lock(key):
                holder_entered.set()
                await release_holder.wait()

        holder_task = asyncio.ensure_future(holder())
        await asyncio.wait_for(holder_entered.wait(), timeout=5)

        chat_service = app.state.chat_service
        run_task = asyncio.ensure_future(
            chat_service.run(
                user_id=USER,
                session_id=SESSION,
                agent_id=AGENT,
                input_msg=None,
            ),
        )
        # 让 run 通过锁前检查（runnable）并阻塞在原生锁上。
        await asyncio.sleep(0.05)
        # 等待期间事实切换为 deleting，然后释放锁。
        verifier.deleting = True
        verifier.operation_id = "op-wait"
        release_holder.set()
        await asyncio.wait_for(holder_task, timeout=5)
        await asyncio.wait_for(run_task, timeout=5)

        messages_after, _ = await app.state.storage.list_messages(
            USER,
            SESSION,
            limit=50,
        )
        assert len(messages_after) == before_count
        assert not any(
            getattr(m, "finished_reason", None) is not None
            and getattr(getattr(m, "finished_reason"), "value", None) == "error"
            for m in messages_after
        )


@pytest.mark.asyncio
async def test_restart_deleting_still_rejected(tmp_path: Path):
    """新进程代次（新 controller）在 deleting 事实下仍拒绝运行。"""
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    verifier.deleting = True
    verifier.operation_id = "op-restart"
    # 复用同一 data_dir 模拟重启：新 app / 新 controller（冻结集合为空）。
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            resp = await _post_chat(client)
            assert resp.status_code == 409


@pytest.mark.asyncio
async def test_restart_query_failure_still_rejected(tmp_path: Path):
    """新服务代次启动即查询失败：admission/Session 锁均 fail-closed 拒绝。"""
    CredentialFactory.register_credential(RecordingCredential)
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=RaisingFreezeVerifier(),
    )
    async with app.router.lifespan_context(app):
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            resp = await _post_chat(client)
            assert resp.status_code == 409
        # 未登记运行、未落库任何消息，也不生成合成 error 尾。
        assert app.state.chat_run_registry.get(SESSION) is None
        messages, _ = await app.state.storage.list_messages(USER, SESSION, limit=50)
        assert messages == []


@pytest.mark.asyncio
async def test_bus_lock_post_acquire_recheck_rejects_after_fact_change(
    tmp_path: Path,
):
    """等待 Session 锁期间事实从 runnable 切 deleting：取得锁后的复查必须拒绝。"""
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        bus = app.state.message_bus
        key = MessageBusKeys.session_lock(SESSION)
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def holder() -> None:
            async with bus.acquire_lock(key):
                holder_entered.set()
                await release_holder.wait()

        holder_task = asyncio.ensure_future(holder())
        await asyncio.wait_for(holder_entered.wait(), timeout=5)

        outcome: dict = {}

        async def waiter() -> None:
            try:
                async with bus.acquire_lock(key):
                    outcome["acquired"] = True
            except RunFrozenError:
                outcome["frozen"] = True

        waiter_task = asyncio.ensure_future(waiter())
        # 给 waiter 足够时间通过锁前检查（runnable）并阻塞在原生锁上。
        await asyncio.sleep(0.05)
        # 等待期间事实切换为 deleting，然后释放锁。
        verifier.deleting = True
        verifier.operation_id = "op-lock"
        release_holder.set()
        await asyncio.wait_for(holder_task, timeout=5)
        await asyncio.wait_for(waiter_task, timeout=5)
        assert outcome.get("frozen") is True
        assert "acquired" not in outcome


@pytest.mark.asyncio
async def test_freeze_persistence_failure_blocks_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await app.state.storage.upsert_credential(
            USER,
            RecordingCredential(id=CREDENTIAL_ID, name="recording"),
        )
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        index: PersistenceIndex = app.state.nexus_persistence_index
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            await _post_chat(client)
            task = await _wait_task(app, SESSION)
            await asyncio.wait_for(_Gate.entered.wait(), timeout=10)
            del task

            # 索引确认失败：readback 必须阻断，不得伪装成功。
            original = index.confirm

            async def failing_confirm(*args, **kwargs):
                raise RuntimeError("index confirm failed")

            monkeypatch.setattr(index, "confirm", failing_confirm)

            verifier.deleting = True
            verifier.operation_id = "op-fail"
            freeze_resp = await client.post(
                "/nexus/internal/freeze",
                json={
                    "operation_id": "op-fail",
                    "agent_id": AGENT,
                    "session_ids": [SESSION],
                    "timeout_secs": 10.0,
                },
                headers=_internal_headers(),
            )
            assert freeze_resp.status_code == 200, freeze_resp.text
            report = freeze_resp.json()
            monkeypatch.setattr(index, "confirm", original)
            assert report["persistence_verified"] is False


class _SlowDownstream:
    """下游 ASGI 应用：进入后停顿，用于验证 admission 外层取消 shield。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.completed = asyncio.Event()

    async def __call__(self, scope, receive, send):
        self.started.set()
        await asyncio.sleep(0.2)
        self.completed.set()
        await send(
            {"type": "http.response.start", "status": 200, "headers": []},
        )
        await send({"type": "http.response.body", "body": b"{}"})


@pytest.mark.asyncio
async def test_admission_outer_cancel_shields_downstream():
    from nexus_agent_service.freeze import AdmissionMiddleware

    controller = FreezeController(
        app=_FakeAppForBus(),
        verifier=FakeFreezeVerifier(),
        user_id=USER,
    )

    class _App:
        class state:  # noqa: N801
            nexus_freeze_controller = controller

    downstream = _SlowDownstream()
    middleware = AdmissionMiddleware(downstream)
    scope = {"type": "http", "method": "POST", "path": "/chat/", "app": _App()}
    body = b'{"agent_id":"a","session_id":"s","input":null}'

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    outer = asyncio.ensure_future(middleware(scope, receive, send))
    await asyncio.wait_for(downstream.started.wait(), timeout=5)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    # 外层取消后真实下游仍完成（未被取消）。
    assert downstream.completed.is_set()
    assert sent and sent[0]["status"] == 200


class _BodyEchoDownstream:
    """记录下游实际收到的请求体字节数（验证未设极小请求上限）。"""

    def __init__(self) -> None:
        self.body_len = 0

    async def __call__(self, scope, receive, send):
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        self.body_len = len(body)
        await send(
            {"type": "http.response.start", "status": 200, "headers": []},
        )
        await send({"type": "http.response.body", "body": b"{}"})


@pytest.mark.asyncio
async def test_admission_does_not_impose_small_body_limit():
    from nexus_agent_service.freeze import AdmissionMiddleware

    controller = FreezeController(
        app=_FakeAppForBus(),
        verifier=FakeFreezeVerifier(),
        user_id=USER,
    )

    class _App:
        class state:  # noqa: N801
            nexus_freeze_controller = controller

    downstream = _BodyEchoDownstream()
    middleware = AdmissionMiddleware(downstream)
    # 约 3 MiB 的正文：远超任何“极小上限”，不得被 admission 截断/拒绝。
    big_text = "x" * (3 * 1024 * 1024)
    body = json.dumps(
        {"agent_id": "a", "session_id": "s", "input": big_text},
    ).encode("utf-8")
    scope = {"type": "http", "method": "POST", "path": "/chat/", "app": _App()}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        return None

    await middleware(scope, receive, send)
    assert downstream.body_len == len(body)


@pytest.mark.asyncio
async def test_freeze_drains_background_task(tmp_path: Path):
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        bg_manager = app.state.background_task_manager

        async def _bg_work():
            await asyncio.sleep(30)

        bg_task = asyncio.ensure_future(_bg_work())
        await bg_manager.register_task(bg_task, SESSION, AGENT, USER, "T")
        assert len(bg_manager.tasks) == 1

        verifier.deleting = True
        verifier.operation_id = "op-bg"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/nexus/internal/freeze",
                json={
                    "operation_id": "op-bg",
                    "agent_id": AGENT,
                    "session_ids": [SESSION],
                    "timeout_secs": 10.0,
                },
                headers=_internal_headers(),
            )
            assert resp.status_code == 200, resp.text
            report = resp.json()
            assert report["execution_drained"] is True
        assert bg_task.done()
        assert len(bg_manager.tasks) == 0


@pytest.mark.asyncio
async def test_freeze_waits_for_delayed_bg_registry_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """BG done callback 先 pop 本地句柄再异步 registry_del：延迟清理须等待。"""
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        bg_manager = app.state.background_task_manager
        bus = app.state.message_bus

        async def _bg_work():
            await asyncio.sleep(30)

        bg_task = asyncio.ensure_future(_bg_work())
        await bg_manager.register_task(bg_task, SESSION, AGENT, USER, "T")

        original_del = bus.registry_del

        async def slow_del(namespace, field):
            await asyncio.sleep(0.2)
            return await original_del(namespace, field)

        monkeypatch.setattr(bus, "registry_del", slow_del)

        verifier.deleting = True
        verifier.operation_id = "op-delay"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/nexus/internal/freeze",
                json={
                    "operation_id": "op-delay",
                    "agent_id": AGENT,
                    "session_ids": [SESSION],
                    "timeout_secs": 5.0,
                },
                headers=_internal_headers(),
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["execution_drained"] is True
        assert bg_task.done()
        assert await bus.registry_getall(MessageBusKeys.bg_tasks(SESSION)) == {}


@pytest.mark.asyncio
async def test_freeze_timeout_does_not_cancel_slow_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """超时返回不代表取消：真实保存继续真实落盘，绝不把取消传给 ``_persist``。

    强化点：以真实 Storage/SQLite 读回 Msg/State 与索引确认，不用计数代替
    提交；超时先 ``execution_drained/persistence_verified`` 均为 False 且保留
    pending，放行保存后重复 quiesce 最终真实确认；子任务无隐藏失败。
    """
    from agentscope.app.storage import AsyncSQLAlchemyStorage

    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await app.state.storage.upsert_credential(
            USER,
            RecordingCredential(id=CREDENTIAL_ID, name="recording"),
        )
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        _Gate.release.set()
        pause = asyncio.Event()
        release = asyncio.Event()
        original = AsyncSQLAlchemyStorage.update_session_state
        state_saves = {"done": 0}

        async def slow_state(self, user_id, agent_id, session_id, state):
            pause.set()
            await release.wait()
            state_saves["done"] += 1
            return await original(self, user_id, agent_id, session_id, state)

        monkeypatch.setattr(
            AsyncSQLAlchemyStorage,
            "update_session_state",
            slow_state,
        )

        binding = Binding(USER, AGENT, SESSION)
        index: PersistenceIndex = app.state.nexus_persistence_index
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            await _post_chat(client)
            task = await _wait_task(app, SESSION)
            await asyncio.wait_for(pause.wait(), timeout=10)

            verifier.deleting = True
            verifier.operation_id = "op-timeout"
            resp = await client.post(
                "/nexus/internal/freeze",
                json={
                    "operation_id": "op-timeout",
                    "agent_id": AGENT,
                    "session_ids": [SESSION],
                    "timeout_secs": 0.3,
                },
                headers=_internal_headers(),
            )
            assert resp.status_code == 200
            report = resp.json()
            # 超时：捕获的真实 Task 仍存活，未排空且写入未确认。
            assert report["execution_drained"] is False
            assert report["persistence_verified"] is False
            assert report["accepted_input_archival"] == "not_verified"
            session = report["sessions"][0]
            assert session["captured_chat_alive"] is True
            assert session["persistence_verified"] is False
            # 停写前保留 pending，绝不读回确认：索引中仍有未确认条目。
            assert await index.unconfirmed(binding), "停写前不得确认任何条目"
            assert session["pending"], "未排空报告必须保留 pending"
            # 真实 Task 尚未结束（真实保存未被取消）。
            assert not task.done()

            # 放行真实保存；native 在取消时仍 await ``persist_task`` 完成。
            release.set()
            results = await asyncio.gather(task, return_exceptions=True)
            # 真实 Task 因先前取消以 CancelledError 结束；无其它隐藏失败。
            assert isinstance(results[0], asyncio.CancelledError)

        # 真实提交核对：paused 的 state 保存真实落入 SQLite，无隐藏失败。
        assert state_saves["done"] == 1
        record = await app.state.storage.get_session(USER, AGENT, SESSION)
        assert record is not None and record.state is not None
        messages, _ = await app.state.storage.list_messages(
            USER,
            SESSION,
            limit=50,
        )
        assert any(
            getattr(m, "role", None) == "assistant" for m in messages
        ), "真实 assistant 消息未落盘"

        # 重复 quiesce：真实 Task 已结束，最终真实确认成功。
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            second = await client.post(
                "/nexus/internal/freeze",
                json={
                    "operation_id": "op-timeout",
                    "agent_id": AGENT,
                    "session_ids": [SESSION],
                    "timeout_secs": 5.0,
                },
                headers=_internal_headers(),
            )
            assert second.status_code == 200, second.text
            report2 = second.json()
            assert report2["execution_drained"] is True
            assert report2["persistence_verified"] is True
        assert await index.unconfirmed(binding) == []


@pytest.mark.asyncio
async def test_freeze_outer_cancel_still_drains_then_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """外层取消冻结请求：真实排空继续到有界完成，再如实传播外层取消。

    验证「真实 Task 自身取消结束」与「外层取消」区分：外层取消不打断 native
    ``_persist``，也不在真实持久化完成前返回；真实保存落盘后外层取消才传播。
    """
    from agentscope.app.storage import AsyncSQLAlchemyStorage

    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await app.state.storage.upsert_credential(
            USER,
            RecordingCredential(id=CREDENTIAL_ID, name="recording"),
        )
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        _Gate.release.set()
        pause = asyncio.Event()
        release = asyncio.Event()
        original = AsyncSQLAlchemyStorage.update_session_state
        saved = {"done": 0}

        async def slow_state(self, user_id, agent_id, session_id, state):
            pause.set()
            await release.wait()
            saved["done"] += 1
            return await original(self, user_id, agent_id, session_id, state)

        monkeypatch.setattr(
            AsyncSQLAlchemyStorage,
            "update_session_state",
            slow_state,
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            await _post_chat(client)
            chat_task = await _wait_task(app, SESSION)
            await asyncio.wait_for(pause.wait(), timeout=10)

            verifier.deleting = True
            verifier.operation_id = "op-cancel"
            controller: FreezeController = app.state.nexus_freeze_controller
            freeze_task = asyncio.ensure_future(
                controller.freeze_and_quiesce(
                    operation_id="op-cancel",
                    agent_id=AGENT,
                    session_ids=[SESSION],
                    timeout=10.0,
                ),
            )
            # 让冻结真实发起取消并停在等待持久化上。
            await asyncio.sleep(0.2)
            freeze_task.cancel()
            await asyncio.sleep(0.2)
            # 外层取消不得打断真实排空：冻结仍在等待真实持久化完成。
            assert not freeze_task.done()
            assert not chat_task.done()

            release.set()
            with pytest.raises(asyncio.CancelledError):
                await freeze_task

        # 真实持久化完整完成（无隐藏失败）；真实 State 已落盘。
        assert saved["done"] == 1
        record = await app.state.storage.get_session(USER, AGENT, SESSION)
        assert record is not None and record.state is not None


@pytest.mark.asyncio
async def test_admission_still_allows_when_runnable(tmp_path: Path):
    """对照：事实可运行时 admission 正常放行并真实跑完。"""
    CredentialFactory.register_credential(RecordingCredential)
    verifier = FakeFreezeVerifier()
    app = build_app(
        _config(tmp_path),
        guard_authorizer=AllowAuthorizer(),
        freeze_verifier=verifier,
    )
    async with app.router.lifespan_context(app):
        await app.state.storage.upsert_credential(
            USER,
            RecordingCredential(id=CREDENTIAL_ID, name="recording"),
        )
        await _seed_agent_session(app.state.storage, AGENT, SESSION)
        _Gate.release.set()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"X-User-ID": USER},
        ) as client:
            resp = await _post_chat(client)
            assert resp.status_code == 200
            task = await _wait_task(app, SESSION)
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        binding = Binding(USER, AGENT, SESSION)
        index: PersistenceIndex = app.state.nexus_persistence_index
        assert await index.unconfirmed(binding) == []

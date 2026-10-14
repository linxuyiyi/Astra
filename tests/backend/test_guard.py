# -*- coding: utf-8 -*-
"""运行 guard 授权端点与中间件测试（OpenSpec 任务 3.2.2）。

真实官方 ASGI + SQLite 验证内部授权端点：正确秘密放行并返回实际允许的工具名；
错误/缺失秘密、未知 Session、绑定不一致、员工禁用、能力失效均 fail-closed。
单元（标记 mock）验证 guard 中间件：鉴权失败不进入模型；按授权结果重建
toolkit（只含所选 Skill 与受控 MCP facade）、schema/permission 两处约束、
facade 以真实 state session 校验并委托原 MCP 工具。
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from _stack import INTERNAL_TOKEN, real_stack


def _valid_payload(**overrides) -> dict:
    payload = {
        "name": "星河",
        "domain": "测试自动化工程师",
        "responsibilities": "负责自动化脚本开发与维护。",
        "instructions": "收到消息后先判断能否直接回答。",
        "avatar": "👩🏻‍💻",
    }
    payload.update(overrides)
    return payload


def _headers(token: str | None = INTERNAL_TOKEN) -> dict:
    return {"X-Nexus-Internal-Token": token} if token is not None else {}


async def _ready_conversation(client):
    employee = (
        await client.post("/api/employees", json=_valid_payload())
    ).json()
    conv = (
        await client.post(
            "/api/conversations",
            headers={"X-Nexus-Owner": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
            json={
                "employee_id": employee["id"],
                "client_key": "guard-ready-key",
            },
        )
    ).json()
    assert conv["status"] == "ready", conv
    return employee, conv


@pytest.mark.asyncio
async def test_authorize_run_endpoint_real(tmp_path):
    async with real_stack(tmp_path, model_key="test-model-key") as (
        backend,
        adapter,
        client,
        upstream,
    ):
        employee, conv = await _ready_conversation(client)
        body = {
            "user_id": "nexus-service",
            "agent_id": conv["agent_id"],
            "session_id": conv["session_id"],
        }

        ok = await client.post(
            "/api/internal/authorize-run",
            headers=_headers(),
            json=body,
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["allowed"] is True
        assert ok.json()["employee_id"] == employee["id"]
        assert ok.json()["skills"] == []
        assert ok.json()["tools"] == []
        assert ok.json()["mcps"] == []

        # 错误 / 缺失秘密。
        assert (
            await client.post(
                "/api/internal/authorize-run",
                headers=_headers("wrong-token"),
                json=body,
            )
        ).status_code == 403
        assert (
            await client.post(
                "/api/internal/authorize-run",
                headers={},
                json=body,
            )
        ).status_code == 403

        # 非内部 user_id 拒绝。
        assert (
            await client.post(
                "/api/internal/authorize-run",
                headers=_headers(),
                json={**body, "user_id": "someone-else"},
            )
        ).status_code == 403

        # 未知 Session。
        assert (
            await client.post(
                "/api/internal/authorize-run",
                headers=_headers(),
                json={**body, "session_id": "unknown-session"},
            )
        ).status_code == 403

        # Agent 与 Session 绑定不一致。
        assert (
            await client.post(
                "/api/internal/authorize-run",
                headers=_headers(),
                json={**body, "agent_id": "some-other-agent"},
            )
        ).status_code == 403

        # 员工禁用后拒绝新运行。
        await client.patch(
            f"/api/employees/{employee['id']}",
            json={"enabled": False},
        )
        denied = await client.post(
            "/api/internal/authorize-run",
            headers=_headers(),
            json=body,
        )
        assert denied.status_code == 403
        assert "员工" in denied.json()["detail"]


@pytest.mark.asyncio
async def test_authorize_run_unconfigured_token_fails_closed(tmp_path):
    async with real_stack(
        tmp_path,
        token=None,
        model_key="test-model-key",
    ) as (backend, adapter, client, upstream):
        await client.post("/api/employees", json=_valid_payload())
        response = await client.post(
            "/api/internal/authorize-run",
            headers=_headers(),
            json={"agent_id": "a", "session_id": "s"},
        )
        assert response.status_code == 503


@pytest.mark.asyncio
async def test_guard_unconfigured_prevents_ready_conversation(tmp_path):
    """AgentScope 未配置运行 guard 时，Nexus 不得创建被当作 ready 的会话。"""
    async with real_stack(
        tmp_path,
        token=None,
        model_key="test-model-key",
    ) as (backend, adapter, client, upstream):
        employee = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        response = await client.post(
            "/api/conversations",
            headers={"X-Nexus-Owner": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
            json={
                "employee_id": employee["id"],
                "client_key": "guard-unconfigured-key",
            },
        )
        assert response.status_code == 503, response.text
        assert "guard" in response.json()["detail"]["message"]


# ----------------------------------------------------------------------
# guard 中间件单元测试（标记 mock，不冒充真实联调）
# ----------------------------------------------------------------------
from nexus_agent_service.guard import (  # noqa: E402
    NexusControlledTool,
    RunAuthorization,
    RunGuardMiddleware,
    RunNotAuthorizedError,
)


class _Authorizer:
    def __init__(self, authorization):
        self._authorization = authorization
        self.calls = 0

    async def authorize(self, user_id, agent_id, session_id):
        self.calls += 1
        return self._authorization


class _BoomAuthorizer:
    async def authorize(self, user_id, agent_id, session_id):
        raise RuntimeError("authorization_unavailable")


class _FakeMCPTool:
    is_read_only = False
    is_concurrency_safe = False
    mcp_name = "nexus-compute"
    description = "compute"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name):
        self.name = name
        self.called = 0

    async def check_permissions(self, tool_input, context):
        from agentscope.permission import (
            PermissionBehavior,
            PermissionDecision,
        )

        return PermissionDecision(behavior=PermissionBehavior.ASK, message="mcp")

    async def check_read_only(self, tool_input):
        return False

    async def __call__(self, **kwargs):
        self.called += 1
        return {"result": "ok", "kwargs": kwargs}


class _FakeMCPConfig:
    def __init__(self, type_="http_mcp", url="http://127.0.0.1:1/mcp"):
        self.type = type_
        self.url = url


class _FakeMCPClient:
    def __init__(self, tools, *, name="nexus-compute", is_stateful=False, config=None):
        self._tools = tools
        self.name = name
        self.is_stateful = is_stateful
        self.mcp_config = config or _FakeMCPConfig()
        self.list_calls = 0

    async def list_tools(self):
        self.list_calls += 1
        return self._tools


class _FakeSkill:
    def __init__(self, name):
        from agentscope.skill import Skill

        self._skill = Skill(
            name=name,
            description="d",
            dir="/tmp/skill",
            markdown="---\nname: x\n---\n",
            updated_at=0.0,
        )

    def __getattr__(self, item):
        return getattr(self._skill, item)


def _skill(name):
    from agentscope.skill import Skill

    return Skill(
        name=name,
        description="d",
        dir="/tmp/skill",
        markdown="---\nname: x\n---\n",
        updated_at=0.0,
    )


class _FakeWorkspace:
    def __init__(self, skills, clients):
        self._skills = skills
        self._clients = clients

    async def list_skills(self, agent_id=None):
        return self._skills

    async def list_mcps(self, agent_id=None, session_id=None):
        return self._clients


class _StubToolkit:
    def __init__(self):
        self.tools: list = []

    async def get_tool(self, name):
        return None

    async def remove_tool(self, names):
        return None


class _StubAgent:
    def __init__(self):
        self.toolkit = _StubToolkit()


@pytest.mark.asyncio
async def test_guard_rebuilds_toolkit_exact_selection_and_facades():
    tool = _FakeMCPTool("mcp__nexus-compute__compute")
    workspace = _FakeWorkspace(
        skills=[_skill("compute-skill"), _skill("unselected-skill")],
        clients=[
            _FakeMCPClient(
                [tool, _FakeMCPTool("mcp__nexus-compute__read_marker")],
            ),
        ],
    )
    authorizer = _Authorizer(
        RunAuthorization(
            allowed=True,
            reason="ok",
            skills=("compute-skill",),
            tools=("mcp__nexus-compute__compute",),
            mcps=("nexus-compute",),
        ),
    )
    mw = RunGuardMiddleware(
        authorizer,
        "nexus-service",
        "agent-1",
        "sess-1",
        workspace,
    )
    agent = _StubAgent()

    async def next_handler(**kwargs):
        if False:
            yield None

    async for _ in mw.on_reply(agent, {}, next_handler):
        pass

    # 只装配精确所选 MCP 工具与 Skill facade；未选 MCP/Skill 不入表、无动态源。
    toolkit = agent.toolkit
    basic = toolkit.tool_groups[0]
    assert [t.name for t in basic.tools] == [
        "mcp__nexus-compute__compute",
        "Skill",
    ]
    assert basic.mcps == []
    assert [s.name for s in basic.skills_or_loaders] == ["compute-skill"]
    mcp_facade, skill_facade = basic.tools
    assert isinstance(mcp_facade, NexusControlledTool)
    assert isinstance(skill_facade, NexusControlledTool)
    assert mcp_facade.is_state_injected is True
    assert mcp_facade.is_mcp is False
    assert mcp_facade.is_external_tool is False
    assert skill_facade.name == "Skill"
    assert skill_facade.is_state_injected is True
    assert skill_facade.is_mcp is False

    # schema 过滤：只保留允许名（Bash/未选 MCP 被剔除）。
    captured: dict = {}

    async def model_next(**kwargs):
        captured.update(kwargs)
        return "model-ok"

    schemas = [
        {"function": {"name": "Bash"}},
        {"function": {"name": "mcp__nexus-compute__compute"}},
        {"function": {"name": "mcp__nexus-compute__read_marker"}},
        {"function": {"name": "mcp__evil__steal"}},
        {"function": {"name": "Skill"}},
    ]
    await mw.on_model_call(agent, {"tools": schemas}, model_next)
    assert [t["function"]["name"] for t in captured["tools"]] == [
        "mcp__nexus-compute__compute",
        "Skill",
    ]

    # 权限：未选工具 DENY 且不进入原生链；选中工具交回原生链并保留其结果。
    from agentscope.permission import PermissionBehavior, PermissionDecision

    class _ToolCall:
        def __init__(self, name):
            self.name = name

    seen = {"calls": 0}

    async def perm_next(**kwargs):
        seen["calls"] += 1
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message="native-chain",
        )

    selected = await mw.on_check_permission(
        agent,
        {"tool_call": _ToolCall("mcp__nexus-compute__compute")},
        perm_next,
    )
    assert selected.behavior is PermissionBehavior.ASK
    assert seen["calls"] == 1
    denied = await mw.on_check_permission(
        agent,
        {"tool_call": _ToolCall("Bash")},
        perm_next,
    )
    assert denied.behavior is PermissionBehavior.DENY
    assert seen["calls"] == 1

    # facade 以真实 state session 校验，并委托原工具执行。
    class _State:
        session_id = "sess-1"

    result = await mcp_facade(_agent_state=_State(), expression="2+2")
    assert result["result"] == "ok"
    assert tool.called == 1
    with pytest.raises(RunNotAuthorizedError):
        await mcp_facade(_agent_state=type("S", (), {"session_id": "other"})(), x=1)


@pytest.mark.asyncio
async def test_guard_stateful_mcp_client_excluded_fails_closed():
    """真实 workspace 客户端为有状态时不得装配；被选中则 fail-closed。"""
    tool = _FakeMCPTool("mcp__nexus-compute__compute")
    workspace = _FakeWorkspace(
        skills=[],
        clients=[_FakeMCPClient([tool], is_stateful=True)],
    )
    authorizer = _Authorizer(
        RunAuthorization(
            allowed=True,
            reason="ok",
            tools=("mcp__nexus-compute__compute",),
            mcps=("nexus-compute",),
        ),
    )
    mw = RunGuardMiddleware(authorizer, "nexus-service", "a", "s", workspace)
    agent = _StubAgent()

    async def next_handler(**kwargs):
        if False:
            yield None

    with pytest.raises(RunNotAuthorizedError):
        async for _ in mw.on_reply(agent, {}, next_handler):
            pass


@pytest.mark.asyncio
async def test_guard_sse_mcp_client_excluded_fails_closed():
    """SSE 传输客户端在本版本不可装配。"""
    tool = _FakeMCPTool("mcp__nexus-compute__compute")
    workspace = _FakeWorkspace(
        skills=[],
        clients=[
            _FakeMCPClient(
                [tool],
                config=_FakeMCPConfig(url="http://127.0.0.1:1/sse"),
            ),
        ],
    )
    authorizer = _Authorizer(
        RunAuthorization(
            allowed=True,
            reason="ok",
            tools=("mcp__nexus-compute__compute",),
            mcps=("nexus-compute",),
        ),
    )
    mw = RunGuardMiddleware(authorizer, "nexus-service", "a", "s", workspace)
    agent = _StubAgent()

    async def next_handler(**kwargs):
        if False:
            yield None

    with pytest.raises(RunNotAuthorizedError):
        async for _ in mw.on_reply(agent, {}, next_handler):
            pass


@pytest.mark.asyncio
async def test_guard_empty_selection_installs_no_skill_or_tool():
    """授权为空选择时绝不装配任意已安装 Skill/MCP，也不虚构 Skill 能力。"""
    workspace = _FakeWorkspace(
        skills=[_skill("compute-skill")],
        clients=[_FakeMCPClient([_FakeMCPTool("mcp__nexus-compute__compute")])],
    )
    authorizer = _Authorizer(
        RunAuthorization(allowed=True, reason="ok", skills=(), tools=()),
    )
    mw = RunGuardMiddleware(authorizer, "nexus-service", "a", "s", workspace)
    agent = _StubAgent()

    async def next_handler(**kwargs):
        if False:
            yield None

    async for _ in mw.on_reply(agent, {}, next_handler):
        pass

    basic = agent.toolkit.tool_groups[0]
    assert basic.tools == []
    assert basic.skills_or_loaders == []
    # Skill 不在允许集合：应被拒绝，且不进入下游。
    from agentscope.permission import PermissionBehavior

    async def perm_next(**kwargs):
        raise AssertionError("未选工具不得进入原生链")

    class _ToolCall:
        def __init__(self, name):
            self.name = name

    denied = await mw.on_check_permission(
        agent,
        {"tool_call": _ToolCall("Skill")},
        perm_next,
    )
    assert denied.behavior is PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_guard_expected_skill_missing_fails_closed():
    workspace = _FakeWorkspace(skills=[], clients=[])
    authorizer = _Authorizer(
        RunAuthorization(allowed=True, reason="ok", skills=("missing-skill",)),
    )
    mw = RunGuardMiddleware(authorizer, "nexus-service", "a", "s", workspace)
    agent = _StubAgent()

    async def next_handler(**kwargs):
        if False:
            yield None

    with pytest.raises(RunNotAuthorizedError):
        async for _ in mw.on_reply(agent, {}, next_handler):
            pass


@pytest.mark.asyncio
async def test_guard_expected_mcp_tool_missing_fails_closed():
    workspace = _FakeWorkspace(
        skills=[],
        clients=[_FakeMCPClient([_FakeMCPTool("mcp__nexus-compute__other")])],
    )
    authorizer = _Authorizer(
        RunAuthorization(
            allowed=True,
            reason="ok",
            tools=("mcp__nexus-compute__compute",),
            mcps=("nexus-compute",),
        ),
    )
    mw = RunGuardMiddleware(authorizer, "nexus-service", "a", "s", workspace)
    agent = _StubAgent()

    async def next_handler(**kwargs):
        if False:
            yield None

    with pytest.raises(RunNotAuthorizedError):
        async for _ in mw.on_reply(agent, {}, next_handler):
            pass


@pytest.mark.asyncio
async def test_guard_duplicate_mcp_tool_name_rejected():
    name = "mcp__nexus-compute__compute"
    workspace = _FakeWorkspace(
        skills=[],
        clients=[
            _FakeMCPClient([_FakeMCPTool(name)]),
            _FakeMCPClient([_FakeMCPTool(name)]),
        ],
    )
    authorizer = _Authorizer(
        RunAuthorization(
            allowed=True,
            reason="ok",
            tools=(name,),
            mcps=("nexus-compute",),
        ),
    )
    mw = RunGuardMiddleware(authorizer, "nexus-service", "a", "s", workspace)
    agent = _StubAgent()

    async def next_handler(**kwargs):
        if False:
            yield None

    with pytest.raises(RunNotAuthorizedError):
        async for _ in mw.on_reply(agent, {}, next_handler):
            pass


@pytest.mark.asyncio
async def test_guard_permission_preserves_original_deny_and_skips_body():
    """原工具 check_permissions 返回 DENY 时，执行体绝不被调用。"""
    from agentscope.permission import PermissionBehavior, PermissionDecision

    class _DenyTool(_FakeMCPTool):
        async def check_permissions(self, tool_input, context):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="denied by original",
            )

    tool = _DenyTool("mcp__nexus-compute__compute")
    workspace = _FakeWorkspace(
        skills=[],
        clients=[_FakeMCPClient([tool])],
    )
    authorizer = _Authorizer(
        RunAuthorization(
            allowed=True,
            reason="ok",
            tools=("mcp__nexus-compute__compute",),
            mcps=("nexus-compute",),
        ),
    )
    mw = RunGuardMiddleware(authorizer, "nexus-service", "a", "s", workspace)
    agent = _StubAgent()

    async def next_handler(**kwargs):
        if False:
            yield None

    async for _ in mw.on_reply(agent, {}, next_handler):
        pass
    facade = agent.toolkit.tool_groups[0].tools[0]

    # next_handler 模拟原生权限引擎：解析工具并调用其 check_permissions。
    async def native_chain(**kwargs):
        tool_input = kwargs.get("tool_input", {})
        return await facade.check_permissions(tool_input, None)

    class _ToolCall:
        name = "mcp__nexus-compute__compute"

    decision = await mw.on_check_permission(
        agent,
        {"tool_call": _ToolCall(), "tool_input": {"expression": "2+2"}},
        native_chain,
    )
    assert decision.behavior is PermissionBehavior.DENY
    assert tool.called == 0


@pytest.mark.asyncio
async def test_authorizer_rejects_malformed_skill_field():
    """授权响应 skills/tools 非法结构不得静默过滤为空，必须拒绝。"""
    import httpx

    from nexus_agent_service.guard import NexusGuardAuthorizer

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"allowed": True, "skills": "not-a-list", "tools": []},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://backend.test",
    )
    authorizer = NexusGuardAuthorizer(
        "http://backend.test",
        "token",
        client=client,
    )
    try:
        result = await authorizer.authorize("nexus-service", "a", "s")
    finally:
        await client.aclose()
    assert result.allowed is False
    assert result.reason == "authorization_invalid"


@pytest.mark.asyncio
async def test_guard_deny_prevents_model_call_recording_mock():
    """鉴权失败（如员工禁用）时真实 Agent 不进入模型调用（mock recording）。"""
    from agentscope.agent import Agent
    from agentscope.message import Msg
    from agentscope.model import ChatModelBase
    from agentscope.tool import Bash, Toolkit

    class RecordingModel(ChatModelBase):
        class Parameters(BaseModel):
            pass

        def __init__(self):
            self.credential = None
            self.model = "recording"
            self.parameters = RecordingModel.Parameters()
            self.stream = False
            self.max_retries = 0
            self.retry_delay = 0
            self.context_size = 32768
            self.called = False

        async def __call__(self, messages=None, tools=None, tool_choice=None):
            self.called = True
            return None

    model = RecordingModel()
    agent = Agent(
        name="recording",
        system_prompt="p",
        model=model,
        toolkit=Toolkit(tools=[Bash()]),
        middlewares=[
            RunGuardMiddleware(
                _Authorizer(
                    RunAuthorization(allowed=False, reason="disabled"),
                ),
                "nexus-service",
                "a",
                "s",
                None,
            ),
        ],
    )
    message = Msg(
        name="user",
        role="user",
        content=[{"type": "text", "text": "hi"}],
    )
    with pytest.raises(RunNotAuthorizedError):
        await agent.reply(message)
    assert model.called is False


@pytest.mark.asyncio
async def test_guard_callback_failure_fails_closed():
    mw = RunGuardMiddleware(_BoomAuthorizer(), "nexus-service", "a", "s", None)
    agent = _StubAgent()

    async def next_handler(**kwargs):
        raise AssertionError("next_handler must not run when guard fails")

    with pytest.raises(RunNotAuthorizedError):
        async for _ in mw.on_reply(agent, {}, next_handler):
            pass


@pytest.mark.asyncio
async def test_guard_rechecks_before_each_run():
    """每次真实运行前重新鉴权：排队的触发在启动前发现被禁用即拒绝。"""

    class _SequenceAuthorizer:
        def __init__(self, decisions):
            self._decisions = list(decisions)
            self.calls = 0

        async def authorize(self, user_id, agent_id, session_id):
            decision = self._decisions[min(self.calls, len(self._decisions) - 1)]
            self.calls += 1
            return decision

    authorizer = _SequenceAuthorizer(
        [
            RunAuthorization(allowed=True, reason="ok"),
            RunAuthorization(allowed=False, reason="disabled"),
        ],
    )
    workspace = _FakeWorkspace([], [])
    mw = RunGuardMiddleware(authorizer, "nexus-service", "a", "s", workspace)
    agent = _StubAgent()
    ran = {"n": 0}

    async def next_handler(**kwargs):
        ran["n"] += 1
        if False:
            yield None

    async for _ in mw.on_reply(agent, {}, next_handler):
        pass
    assert ran["n"] == 1
    with pytest.raises(RunNotAuthorizedError):
        async for _ in mw.on_reply(agent, {}, next_handler):
            pass
    assert ran["n"] == 1


@pytest.mark.asyncio
async def test_authorize_rechecks_employee_after_network(tmp_path, monkeypatch):
    """网络能力核对之间被禁用：最终短事务复核必须拒绝。"""
    from nexus_backend import guard as guard_mod
    from nexus_backend.db import Employee

    async with real_stack(tmp_path, model_key="test-model-key") as (
        backend,
        adapter,
        client,
        upstream,
    ):
        employee, conv = await _ready_conversation(client)
        body = {
            "user_id": "nexus-service",
            "agent_id": conv["agent_id"],
            "session_id": conv["session_id"],
        }

        async def fake_resolve(adapter, conversation, agent_id, session_id):
            # 模拟目录网络核对期间员工被禁用。
            async with backend.state.db.session() as session:
                row = await session.get(Employee, conversation.employee_id)
                row.enabled = False
                await session.commit()
            return [], [], []

        monkeypatch.setattr(guard_mod, "_resolve_capabilities", fake_resolve)
        response = await client.post(
            "/api/internal/authorize-run",
            headers=_headers(),
            json=body,
        )
        assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_guard_library_map_excludes_disabled_and_unsupported():
    """运行授权使用的库名映射与资源投影同源：忽略 disabled/不可选会被拒绝。"""
    from nexus_backend import guard as guard_mod

    class A:
        async def list_mcps(self):
            return [
                {"id": "m1", "name": "ok", "enabled": True},
                {"id": "m2", "name": "off", "enabled": False},
                {"id": "m3", "name": "stdio", "enabled": True},
            ]

        async def mcp_support_map(self):
            return [
                {"id": "m1", "selectable": True, "reason": None},
                {"id": "m2", "selectable": True, "reason": None},
                {
                    "id": "m3",
                    "selectable": False,
                    "reason": (
                        "当前版本仅支持无状态 MCP；有状态或 stdio 条目不可选。"
                    ),
                },
            ]

    names = await guard_mod._library_name_map(A(), "mcps")
    assert names == {"m1": "ok"}


@pytest.mark.asyncio
async def test_deny_all_authorizer_blocks_run():
    """未配置秘密的 DenyAllAuthorizer 一律拒绝，不进入下游/模型。"""
    from nexus_agent_service.guard import DenyAllAuthorizer

    mw = RunGuardMiddleware(DenyAllAuthorizer(), "nexus-service", "a", "s", None)
    agent = _StubAgent()

    async def next_handler(**kwargs):
        raise AssertionError("DenyAll 不得进入下游")

    with pytest.raises(RunNotAuthorizedError):
        async for _ in mw.on_reply(agent, {}, next_handler):
            pass


@pytest.mark.asyncio
async def test_guard_colliding_tool_name_cannot_bind_unselected_mcp():
    """工具名跨 MCP 冲突：所选 a 缺失时不得用未选 a__b 补位，且不枚举未选客户端。"""
    unselected = _FakeMCPClient(
        [_FakeMCPTool("mcp__a__b__c")],
        name="a__b",
    )
    workspace = _FakeWorkspace(skills=[], clients=[unselected])
    authorizer = _Authorizer(
        # 可信授权：选中所选 MCP 名 a、期望工具 mcp__a__b__c（由 a + b__c 生成）。
        RunAuthorization(
            allowed=True,
            reason="ok",
            tools=("mcp__a__b__c",),
            mcps=("a",),
        ),
    )
    mw = RunGuardMiddleware(authorizer, "nexus-service", "a", "s", workspace)
    agent = _StubAgent()

    async def next_handler(**kwargs):
        if False:
            yield None

    with pytest.raises(RunNotAuthorizedError):
        async for _ in mw.on_reply(agent, {}, next_handler):
            pass
    # 未选客户端绝不能被 list_tools 调用。
    assert unselected.list_calls == 0


@pytest.mark.asyncio
async def test_guard_selected_zero_tool_mcp_still_verified():
    """所选 MCP 存在但真实 0 工具：仍核对存在/支持，不误配未选客户端的工具。"""
    selected = _FakeMCPClient([], name="a")
    unselected = _FakeMCPClient(
        [_FakeMCPTool("mcp__a__b__c")],
        name="a__b",
    )
    workspace = _FakeWorkspace(skills=[], clients=[selected, unselected])
    authorizer = _Authorizer(
        RunAuthorization(
            allowed=True,
            reason="ok",
            tools=("mcp__a__b__c",),
            mcps=("a",),
        ),
    )
    mw = RunGuardMiddleware(authorizer, "nexus-service", "a", "s", workspace)
    agent = _StubAgent()

    async def next_handler(**kwargs):
        if False:
            yield None

    with pytest.raises(RunNotAuthorizedError):
        async for _ in mw.on_reply(agent, {}, next_handler):
            pass
    assert selected.list_calls == 1  # 所选客户端仍被核对（0 工具）
    assert unselected.list_calls == 0


@pytest.mark.asyncio
async def test_guard_selected_client_tool_name_resolved_by_identity():
    """同名工具只归所选客户端：所选 a + 工具 b__c 正常装配。"""
    selected = _FakeMCPClient(
        [_FakeMCPTool("mcp__a__b__c")],
        name="a",
    )
    workspace = _FakeWorkspace(skills=[], clients=[selected])
    authorizer = _Authorizer(
        RunAuthorization(
            allowed=True,
            reason="ok",
            tools=("mcp__a__b__c",),
            mcps=("a",),
        ),
    )
    mw = RunGuardMiddleware(authorizer, "nexus-service", "a", "s", workspace)
    agent = _StubAgent()

    async def next_handler(**kwargs):
        if False:
            yield None

    async for _ in mw.on_reply(agent, {}, next_handler):
        pass
    assert [t.name for t in agent.toolkit.tool_groups[0].tools] == [
        "mcp__a__b__c",
    ]
    assert selected.list_calls == 1


@pytest.mark.asyncio
async def test_authorizer_rejects_tools_without_mcp_names():
    """授权响应有 tools 却缺 mcps：无法唯一确定客户端身份，必须拒绝。"""
    import httpx

    from nexus_agent_service.guard import NexusGuardAuthorizer

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "allowed": True,
                "skills": [],
                "tools": ["mcp__a__b__c"],
                "mcps": [],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://backend.test",
    )
    authorizer = NexusGuardAuthorizer(
        "http://backend.test",
        "token",
        client=client,
    )
    try:
        result = await authorizer.authorize("nexus-service", "a", "s")
    finally:
        await client.aclose()
    assert result.allowed is False
    assert result.reason == "authorization_invalid"

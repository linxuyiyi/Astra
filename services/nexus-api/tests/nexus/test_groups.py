"""人类群投递、时间线，以及员工邀请的 workspace 选择。"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


def employee_body(**overrides):
    payload = {
        "name": "星河",
        "domain": "测试自动化工程师",
        "description": "负责自动化脚本",
        "instructions": "能直接回答的问题直接回答；需要同事时联系对方。",
        "avatar": "👩🏻‍💻",
        "enabled": True,
        "knowledge": [],
        "skills": [],
        "tools": [],
        "model_route_id": "route-test",
    }
    payload.update(overrides)
    return payload


def _agent_service():
    spec = importlib.util.spec_from_file_location(
        "nexus_agent_service_team",
        ROOT / "services/agent-service/agent_service.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_invite_uses_mailbox_workspace_and_not_a_private_or_other_team():
    module = _agent_service()

    class Session:
        def __init__(self, session_id: str, workspace_id: str) -> None:
            self.id = session_id
            self.config = type("Config", (), {"workspace_id": workspace_id})()

    private_a = Session("priv-u1", "ws-u1")
    private_b = Session("priv-u2", "ws-u2")
    mailbox_a = Session("mail-a", "ws-mail-a")
    mailbox_b = Session("mail-b", "ws-mail-b")
    other_team = Session("team-old", "ws-team-old")
    chosen_a = module.select_invite_session([private_a, other_team, mailbox_a], "mail-a")
    chosen_b = module.select_invite_session([private_b, mailbox_b], "mail-b")
    assert chosen_a.config.workspace_id == "ws-mail-a"
    assert chosen_b.config.workspace_id == "ws-mail-b"
    assert module.select_invite_session([private_a, other_team], None) is None
    assert module.select_invite_session([private_a], "missing") is None
    assert len({private_a.config.workspace_id, private_b.config.workspace_id, "ws-mail-a", "ws-mail-b"}) == 4

    class Storage:
        def __init__(self) -> None:
            self.sessions = [private_a, other_team, mailbox_a]

        async def list_sessions(self, user_id: str, agent_id: str):
            return list(self.sessions)

        async def get_session(self, *args):
            raise AssertionError("邀请不应再读私聊")

    async def lookup(agent_id: str) -> str:
        return "mail-a"

    view = module.MailboxSessionView(Storage(), lookup)
    listed = asyncio.run(view.list_sessions("nexus", "agent-a"))
    assert [item.id for item in listed] == ["mail-a"]


def test_record_write_takes_the_checkpoint_lock_and_skips_a_running_session():
    module = _agent_service()

    class Keys:
        @staticmethod
        def session_lock(session_id: str) -> str:
            return f"run:{session_id}"

        @staticmethod
        def session_event_checkpoint_lock(session_id: str) -> str:
            return f"ckpt:{session_id}"

        @staticmethod
        def session_events(session_id: str) -> str:
            return f"events:{session_id}"

    class Storage:
        def __init__(self) -> None:
            self.saved = []

        async def upsert_message(self, user_id, session_id, message) -> None:
            self.saved.append((user_id, session_id, message.id))

    class Bus:
        def __init__(self, locked: bool) -> None:
            self.locked = locked
            self.held: list[str] = []
            self.published = []

        async def is_locked(self, key: str) -> bool:
            return self.locked and key.startswith("run:")

        def acquire_lock(self, key: str):
            bus = self

            class Guard:
                async def __aenter__(self):
                    bus.held.append(key)

                async def __aexit__(self, *args):
                    return False

            return Guard()

        async def publish(self, key: str, payload: dict) -> None:
            self.published.append((key, payload))

    message = type("Msg", (), {"id": "m1"})()
    storage = Storage()
    idle = Bus(False)
    asyncio.run(
        module.write_record_message(
            storage,
            idle,
            message,
            user_id="nexus",
            session_id="record",
            keys=Keys,
        )
    )
    assert storage.saved == [("nexus", "record", "m1")]
    assert idle.held == ["ckpt:record"]
    assert idle.published[0][1]["type"] == "record"
    with pytest.raises(module.RecordRunning):
        asyncio.run(
            module.write_record_message(
                storage,
                Bus(True),
                message,
                user_id="nexus",
                session_id="record",
                keys=Keys,
            )
        )
    assert len(storage.saved) == 1


def test_launcher_keeps_team_tools_and_does_not_construct_agent_create():
    source = (ROOT / "services/agent-service/agent_service.py").read_text(encoding="utf-8")
    ast.parse(source)
    assert "AgentCreate" in source
    assert "AgentCreate(" not in source
    assert "TeamCreate(" not in source
    assert "write_record_message" in source
    assert "MailboxSessionView" in source


def _group_security():
    spec = importlib.util.spec_from_file_location(
        "nexus_group_security", ROOT / "services/agent-service/group_security.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_service_requires_service_key(monkeypatch):
    module = _group_security()
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "service-key")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    application = FastAPI()

    @application.get("/chat/")
    def chat():
        return {"ok": True}

    @application.get("/health")
    def health():
        return {"ok": True}

    application.add_middleware(module.AgentServiceKeyMiddleware)
    client = TestClient(application)
    assert client.get("/health").status_code == 200
    assert client.get("/chat/").status_code == 401
    assert client.get("/chat/", headers={"X-Nexus-Internal-Key": "wrong"}).status_code == 401
    assert client.get("/chat/", headers={"X-Nexus-Internal-Key": "service-key"}).status_code == 200


def test_group_fence_comes_from_original_input(monkeypatch):
    module = _group_security()

    class Message:
        def __init__(self, metadata):
            self.metadata = metadata

    class Chat:
        async def run(self, user_id, session_id, agent_id, input_msg=None, sop_dispatch=None):
            del user_id, session_id, agent_id, sop_dispatch
            return module.execution_id.get()

    module.install_run_context(Chat)
    chat = Chat()
    assert asyncio.run(chat.run("nexus", "s", "a", [Message({"discussion_id": "dis-1"})])) == "dis-1"
    assert module.execution_id.get() == ""
    assert asyncio.run(chat.run("nexus", "s", "a", Message({}))) == ""
    assert asyncio.run(chat.run("nexus", "s", "a", [Message({"discussion_id": "dis-1"})])) == "dis-1"
    assert module.execution_id.get() == ""


def test_group_toolkit_filters_tools_by_server_origin(monkeypatch):
    from types import SimpleNamespace

    module = _group_security()

    class Tool:
        def __init__(self, name, *, is_mcp=False, mcp_name=""):
            self.name = name
            self.is_mcp = is_mcp
            self.mcp_name = mcp_name

    class Registered:
        def __init__(self, tool):
            self.tool = tool
            self.group = "basic"

    class Toolkit:
        def __init__(self, tools, mcps):
            self.tool_groups = [SimpleNamespace(name="basic", tools=tools, mcps=mcps)]
            self.tools = tools

        async def _get_available_tools(self, groups):
            del groups
            return {tool.name: Registered(tool) for tool in self.tools}

        async def call_tool(self, tool_call, state):
            del tool_call, state
            yield "called"

    policy = {
        "generation": 1,
        "discussion_id": "dis-1",
        "allowed_tools": ["Bash"],
        "mcp_refs": {"ref-demo": "demo"},
        "task_allowed": True,
        "task_mcp_url": "http://task.local/mcp",
        "skill_viewer": False,
    }

    async def loader(session_id, tool_name="", fence="", mcp_ref=""):
        del session_id, tool_name, fence, mcp_ref
        return policy

    task_client = SimpleNamespace(
        name="nexus-task",
        mcp_config=SimpleNamespace(
            url="http://task.local/mcp",
            headers={"X-Nexus-Session-Id": "ses-1", "X-Nexus-Discussion-Id": "dis-1"},
        ),
    )
    tools = [
        Tool("Bash"),
        Tool("mcp__demo__lookup", is_mcp=True, mcp_name="demo"),
        Tool("mcp__evil__lookup", is_mcp=True, mcp_name="evil"),
        Tool("mcp__nexus-task__nexus_task_get", is_mcp=True, mcp_name="nexus-task"),
    ]

    async def factory(**_kwargs):
        return Toolkit(tools, [task_client])

    assembled = module.secure_group_toolkit(factory, "http://nexus", loader)
    kwargs = {
        "session_record": SimpleNamespace(id="ses-1"),
        "agent_record": SimpleNamespace(
            data=SimpleNamespace(
                chat_config=SimpleNamespace(react_config=SimpleNamespace(max_iters=99))
            )
        ),
        "user_id": "nexus",
    }
    token = module.execution_id.set("dis-1")
    try:
        toolkit = asyncio.run(assembled(**kwargs))
        available = asyncio.run(toolkit._get_available_tools(["basic"]))
        assert set(available) == {"Bash", "mcp__demo__lookup", "mcp__nexus-task__nexus_task_get"}
        assert kwargs["agent_record"].data.chat_config.react_config.max_iters == 2
        with pytest.raises(PermissionError):
            asyncio.run(_collect(toolkit, "mcp__evil__lookup"))
        assert asyncio.run(_collect(toolkit, "mcp__demo__lookup")) == ["called"]
        policy["task_allowed"] = False
        assert set(asyncio.run(toolkit._get_available_tools(["basic"]))) == {"Bash", "mcp__demo__lookup"}
        policy["task_allowed"] = True
        policy["allowed_tools"] = []
        assert set(asyncio.run(toolkit._get_available_tools(["basic"]))) == {
            "mcp__demo__lookup",
            "mcp__nexus-task__nexus_task_get",
        }
        with pytest.raises(PermissionError):
            asyncio.run(_collect(toolkit, "Bash"))
    finally:
        module.execution_id.reset(token)


async def _collect(toolkit, name):
    from types import SimpleNamespace

    return [chunk async for chunk in toolkit.call_tool(SimpleNamespace(name=name), None)]


def test_group_toolkit_rejects_reused_session_without_matching_fence():
    from types import SimpleNamespace

    module = _group_security()

    async def loader(session_id, tool_name="", fence="", mcp_ref=""):
        del session_id, tool_name, fence, mcp_ref
        return {"generation": 1, "discussion_id": "dis-1", "allowed_tools": [], "mcp_refs": {}}

    async def factory(**_kwargs):
        raise AssertionError("没有原始群输入时不能组装工具")

    assembled = module.secure_group_toolkit(factory, "http://nexus", loader)
    kwargs = {"session_record": SimpleNamespace(id="ses-1"), "agent_record": None, "user_id": "nexus"}
    with pytest.raises(PermissionError):
        asyncio.run(assembled(**kwargs))

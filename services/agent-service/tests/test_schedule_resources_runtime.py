"""官方 SQL/scheduler/inbox/Agent + 真实 HTTP MCP；模型为确定性桩。"""

import asyncio
import io
import json
import socket
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("stateful", [False, True])
def test_official_schedule_resources_complete_and_release(
    tmp_path, monkeypatch, stateful
):
    monkeypatch.syspath_prepend(str(ROOT / "services/nexus-api"))
    monkeypatch.setenv(
        "AGENTSCOPE_SQL_URL", f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}"
    )
    monkeypatch.setenv(
        "ASTRA_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'nexus.db'}"
    )
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "schedule-test-internal")
    monkeypatch.setenv("NEXUS_AGENTSCOPE_USER_ID", "nexus")

    async def scenario():
        from agent_service import allow_nexus_task_mcp, without_team_and_task_tools
        from agentscope.app import create_app
        from agentscope.app._service import _chat
        from agentscope.app._service._toolkit import get_toolkit
        from agentscope.app.message_bus import InMemoryMessageBus, MessageBusKeys
        from agentscope.app.storage import (
            AgentData,
            AgentRecord,
            MCPRecord,
            SkillRecord,
        )
        from agentscope.app.workspace_manager import IsolationPolicy
        from agentscope.credential import OpenAICredential
        from agentscope.mcp import MCPClient
        from agentscope.message import TextBlock, ToolCallBlock
        from agentscope.model import ChatModelBase, ChatResponse
        from agentscope.permission import PermissionBehavior, PermissionDecision
        from agentscope.tool import MCPTool
        from agentscope_sql import create_storage, initialize_database
        from app.config.settings import get_settings
        from app.sql_storage import create_test_schema
        from group_security import AgentServiceKeyMiddleware, secure_group_toolkit
        from mcp.server.fastmcp import FastMCP
        from nexus_api.agentscope import AgentScopeClient
        from nexus_api.db import connect, utcnow
        from nexus_api.resources import ResourceStore
        from nexus_api.schedule_resources import schedule_resources_router
        from nexus_api.schedules import schedule_user_id
        from schedule_access import ScheduleResourceAccessPolicy
        from schedule_resources import (
            install_schedule_preparation,
            register_schedule_resources,
            schedule_toolkit,
        )
        from schedule_workspace import ScheduleWorkspaceManager

        get_settings.cache_clear()
        create_test_schema(tmp_path / "nexus.db")
        await initialize_database()
        calls, schemas, models = [], [], []
        server = FastMCP("schedule-probe", stateless_http=True)

        @server.tool()
        async def probe() -> str:
            """确认日程实际调用了 MCP。"""
            calls.append("probe")
            return "SCHEDULE_MCP_OK"

        @server.tool()
        async def nexus_task_list() -> str:
            """确认日程实际调用任务工具。"""
            calls.append("task")
            return "SCHEDULE_TASK_OK"

        mcp_app = server.streamable_http_app()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        mcp_url = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
        monkeypatch.setenv("NEXUS_TASK_MCP_URL", mcp_url)
        runner = uvicorn.Server(
            uvicorn.Config(mcp_app, log_level="error", lifespan="on")
        )
        runner.install_signal_handlers = lambda: None
        server_task = asyncio.create_task(runner.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not runner.started:
                    await asyncio.sleep(0.01)
            bus = InMemoryMessageBus()
            storage = create_storage()
            workspace = ScheduleWorkspaceManager(
                str(tmp_path / "workspaces"), isolation=IsolationPolicy.PER_SESSION
            )
            agent_app = create_app(
                storage=storage,
                message_bus=bus,
                workspace_manager=workspace,
                resource_access_policy=ScheduleResourceAccessPolicy("nexus"),
                enable_scheduler=False,
                enable_channel_worker=False,
            )
            register_schedule_resources(agent_app)
            agent_app.add_middleware(AgentServiceKeyMiddleware)
            nexus_app = FastAPI()
            nexus_app.include_router(schedule_resources_router())
            nexus_app.state.resources = ResourceStore(tmp_path / "resources")
            real_client = httpx.AsyncClient
            failure = {"active": False}

            class Routed(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    if failure["active"] and request.url.path.endswith(
                        "/schedule-sessions/prepare"
                    ):
                        return httpx.Response(
                            502, json={"detail": "secret-upstream-token"}
                        )
                    target = {"agent.test": agent_app, "nexus.test": nexus_app}.get(
                        request.url.host
                    )
                    if target:
                        return await httpx.ASGITransport(
                            app=target
                        ).handle_async_request(request)
                    return await self.network.handle_async_request(request)

                def __init__(self):
                    self.network = httpx.AsyncHTTPTransport()

                async def aclose(self):
                    await self.network.aclose()

            def client(*args, **kwargs):
                kwargs.setdefault("transport", Routed())
                return real_client(*args, **kwargs)

            monkeypatch.setattr(httpx, "AsyncClient", client)
            nexus_app.state.agentscope = AgentScopeClient("http://agent.test", "nexus")
            monkeypatch.setattr(_chat.ChatService, "run", _chat.ChatService.run)
            monkeypatch.setattr(MCPTool, "check_permissions", MCPTool.check_permissions)
            install_schedule_preparation(_chat.ChatService, "http://nexus.test")
            allow_nexus_task_mcp(MCPTool, PermissionDecision, PermissionBehavior)
            monkeypatch.setattr(
                _chat,
                "get_toolkit",
                schedule_toolkit(
                    secure_group_toolkit(
                        without_team_and_task_tools(get_toolkit), "http://nexus.test"
                    )
                ),
            )

            class Model(ChatModelBase):
                def __init__(self):
                    super().__init__(
                        OpenAICredential(api_key="test-only"),
                        "test-model",
                        self.Parameters(),
                    )
                    self.step = 0

                async def _call_api(self, model_name, messages, tools=None, **kwargs):
                    transcript = json.dumps([m.model_dump(mode="json") for m in messages], ensure_ascii=False)
                    for error in ("ToolNotFoundError", "SkillNotFoundError", "ToolGroupInactiveError"):
                        assert error not in transcript
                    expected = {1: "你是责任员工", 2: "LIBRARY_SKILL_OK", 3: "SCHEDULE_MCP_OK", 4: "SCHEDULE_TASK_OK"}
                    if self.step in expected:
                        assert expected[self.step] in transcript
                    names = [t["function"]["name"] for t in tools or []]
                    schemas.append(names)
                    assert "Skill" in names
                    assert "mcp__library__probe" in names
                    assert "mcp__nexus-task__nexus_task_list" in names
                    assert "Bash" not in names
                    outputs = [
                        ToolCallBlock(
                            id=uuid4().hex, name="Skill", input='{"skill":"任务管理"}'
                        ),
                        ToolCallBlock(
                            id=uuid4().hex,
                            name="Skill",
                            input='{"skill":"日程验证技能"}',
                        ),
                        ToolCallBlock(
                            id=uuid4().hex, name="mcp__library__probe", input="{}"
                        ),
                        ToolCallBlock(
                            id=uuid4().hex,
                            name="mcp__nexus-task__nexus_task_list",
                            input="{}",
                        ),
                        TextBlock(text="SCHEDULE_COMPLETE_OK"),
                    ]
                    block = outputs[self.step]
                    self.step += 1
                    return ChatResponse(content=[block], is_last=True)

            async def model_for(*args):
                model = Model()
                models.append(model)
                return model

            monkeypatch.setattr(_chat, "get_model", model_for)
            async with agent_app.router.lifespan_context(agent_app):
                agent = AgentRecord(
                    user_id="nexus",
                    data=AgentData(name="日程员工", system_prompt="执行日程"),
                )
                await storage.upsert_agent("nexus", agent)
                credential = OpenAICredential(
                    api_key="test-only", base_url="http://127.0.0.1:1/v1"
                )
                await storage.upsert_credential("nexus", credential)
                library = MCPRecord(
                    user_id="nexus",
                    client=MCPClient(
                        name="library",
                        is_stateful=False,
                        mcp_config={"type": "http_mcp", "url": mcp_url},
                    ),
                )
                await storage.upsert_mcp("nexus", library)
                library_skill = SkillRecord(
                    user_id="nexus",
                    name="日程验证技能",
                    hub_id="test-hub",
                    card_id="test",
                )
                await storage.upsert_skill("nexus", library_skill)
                skill_archive = io.BytesIO()
                with zipfile.ZipFile(skill_archive, "w") as archive:
                    archive.writestr(
                        "schedule/SKILL.md",
                        "---\nname: 日程验证技能\ndescription: 日程库技能验证\n---\nLIBRARY_SKILL_OK\n",
                    )

                async def download(*args):
                    async def stream():
                        yield skill_archive.getvalue()

                    return SimpleNamespace(stream=stream(), format="zip")

                agent_app.state.skill_hubs["test-hub"] = SimpleNamespace(
                    download=download
                )
                with connect() as db:
                    db.execute(
                        text("""INSERT INTO t_nexus_employees
                        (id,name,domain,description,instructions,avatar,enabled,knowledge_json,
                         skills_json,tools_json,mcp_json,agentscope_agent_id,created_at,updated_at)
                        VALUES ('emp-schedule','日程员工','测试','','','🤖',1,'[]',
                        :k,'[]',:m,:a,:t,:t)"""),
                        {
                            "k": json.dumps(["task-management", library_skill.id]),
                            "m": json.dumps([library.id]),
                            "a": agent.id,
                            "t": utcnow(),
                        },
                    )
                    db.commit()
                owner = schedule_user_id("alice")
                async with client(
                    base_url="http://agent.test",
                    headers={
                        "X-User-ID": owner,
                        "X-Nexus-Internal-Key": "schedule-test-internal",
                    },
                ) as http:
                    response = await http.post(
                        "/schedule/",
                        json={
                            "name": "资源测试",
                            "description": "执行技能和 MCP",
                            "cron_expression": "0 * * * *",
                            "timezone": "Asia/Shanghai",
                            "agent_id": agent.id,
                            "stateful": stateful,
                            "permission_mode": "dont_ask",
                            "chat_model_config": {
                                "type": "openai_credential",
                                "credential_id": credential.id,
                                "model": "test",
                                "parameters": {},
                            },
                        },
                    )
                    assert response.status_code == 201, response.text
                    schedule = await storage.get_schedule(
                        owner, response.json()["schedule_id"]
                    )
                    scheduler = agent_app.state.scheduler_manager
                    seen = set()

                    async def trigger_and_reply():
                        await scheduler._build_trigger(schedule)()
                        async with asyncio.timeout(20):
                            while True:
                                sessions = await storage.list_sessions_by_schedule(
                                    owner, schedule.id
                                )
                                for session in sessions:
                                    messages, _ = await storage.list_messages(
                                        owner, session.id
                                    )
                                    for msg in messages:
                                        if (
                                            msg.role == "assistant"
                                            and msg.finished_at
                                            and msg.id not in seen
                                            and not await bus.is_locked(
                                                MessageBusKeys.session_lock(session.id)
                                            )
                                            and not await bus.is_locked(f"nexus:schedule-prepare:{session.id}")
                                        ):
                                            seen.add(msg.id)
                                            return session, msg
                                await asyncio.sleep(0.03)

                    for _ in range(2):
                        session, reply = await trigger_and_reply()
                        assert str(reply.finished_reason) == "completed", (
                            reply.model_dump()
                        )
                        assert "SCHEDULE_COMPLETE_OK" in str(reply.content)
                        assert not await bus.is_locked(
                            MessageBusKeys.session_lock(session.id)
                        )
                    assert calls == ["probe", "task", "probe", "task"]
                    sessions = await storage.list_sessions_by_schedule(
                        owner, schedule.id
                    )
                    assert len(sessions) == (1 if stateful else 2)
                    workdirs = set()
                    for session in sessions:
                        workspace_instance = await workspace.get_workspace(
                            owner, agent.id, session.id, session.config.workspace_id
                        )
                        workdirs.add(workspace_instance.workdir)
                        assert "/schedules/" in workspace_instance.workdir
                        skills = await workspace_instance.list_skills(agent_id=agent.id)
                        assert len(skills) == 2
                        current = await storage.get_session(owner, agent.id, session.id)
                        assert "basic" in current.state.tool_context.activated_groups
                    assert len(workdirs) == len(sessions)
                    assert all(model.step == 5 for model in models)
                    failure["active"] = True
                    failed_session, failed_reply = await trigger_and_reply()
                    assert str(failed_reply.finished_reason) == "error", (
                        failed_reply.model_dump()
                    )
                    assert str(failed_reply.error.type) == "setup"
                    assert "secret-upstream-token" not in str(failed_reply.model_dump())
                    assert not await bus.is_locked(
                        MessageBusKeys.session_lock(failed_session.id)
                    )
                    assert len(models) == 2
                    failure["active"] = False
                    _, recovered_reply = await trigger_and_reply()
                    assert str(recovered_reply.finished_reason) == "completed", (
                        recovered_reply.model_dump()
                    )
                    assert len(models) == 3
                    other = await http.get(
                        "/nexus/schedule-session",
                        params={"agent_id": agent.id, "session_id": sessions[0].id},
                        headers={"X-User-ID": schedule_user_id("bob")},
                    )
                    assert other.status_code == 404
            await nexus_app.state.agentscope.aclose()
        finally:
            runner.should_exit = True
            await asyncio.wait_for(server_task, 10)
            sock.close()
            get_settings.cache_clear()

    asyncio.run(scenario())

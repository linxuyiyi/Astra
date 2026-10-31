"""任务技能与 MCP 挂接走真实 AgentScopeClient，对面是按 2.0.10 语义实现的路由。"""

import asyncio

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from nexus_api.agentscope import AgentScopeClient
from nexus_api.task_skill import sync_task_capability


def _workspace():
    skills: dict[str, dict[str, str]] = {}
    mcps: dict[tuple[str, str], dict] = {}
    deleted: list[tuple[str, str, str]] = []
    health: dict[tuple[str, str], bool] = {}
    failed_uploads: list[str] = []
    app = FastAPI()

    @app.post("/workspace/skill/upload", status_code=201)
    async def upload_skill(
        agent_id: str,
        session_id: str,
        manifest: str = Form(...),
        files: list[UploadFile] = File(...),
    ):
        del manifest
        raw = await files[0].read()
        if raw == b"fail":
            raise HTTPException(status_code=500, detail="skill upload failed")
        name = ""
        for line in raw.decode().splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip()
                break
        if "fail" in failed_uploads:
            raise HTTPException(status_code=500, detail="skill upload failed")
        skills[agent_id] = {"name": name, "dir": "task-management"}
        return {}

    @app.get("/workspace/skill")
    async def list_skills(agent_id: str, session_id: str):
        del session_id
        item = skills.get(agent_id)
        return [item] if item else []

    @app.delete("/workspace/skill/{name}", status_code=204)
    async def delete_skill(name: str, agent_id: str, session_id: str):
        deleted.append(("skill", name, session_id))
        current = skills.get(agent_id)
        if current is None or current["name"] != name:
            raise HTTPException(status_code=404, detail="missing")
        skills.pop(agent_id, None)
        return Response(status_code=204)

    @app.post("/workspace/mcp", status_code=201)
    async def add_mcp(agent_id: str, session_id: str, body: dict):
        key = (agent_id, session_id)
        if key in mcps:
            raise HTTPException(status_code=409, detail="exists")
        mcps[key] = body
        return {}

    @app.get("/workspace/mcp")
    async def list_mcp(agent_id: str, session_id: str):
        key = (agent_id, session_id)
        item = mcps.get(key)
        if not item:
            return []
        healthy = health.get(key, True)
        return [
            {
                **item,
                "is_healthy": healthy,
                "error": "" if healthy else "MCP 连接失败",
                "tools": [{"name": "nexus_task_get"}] if healthy else [],
            }
        ]

    @app.delete("/workspace/mcp/{name}", status_code=204)
    async def delete_mcp(name: str, agent_id: str, session_id: str):
        deleted.append(("mcp", name, session_id))
        mcps.pop((agent_id, session_id), None)
        return Response(status_code=204)

    return app, skills, mcps, deleted, health, failed_uploads


async def _sync(app, employee, sessions):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agentscope.test") as http:
        client = AgentScopeClient("http://agentscope.test", "nexus", http)
        return await sync_task_capability(client, employee, sessions)


def test_sync_task_capability_is_idempotent_and_updates_url(monkeypatch):
    async def scenario():
        app, skills, mcps, deleted, _health, _failed = _workspace()
        employee = {
            "id": "emp_1",
            "agentscope_agent_id": "agt_1",
            "skills": ["task-management"],
        }
        sessions = [
            {"session_id": "mailbox", "conversation_id": "", "channel_id": ""},
            {"session_id": "private", "conversation_id": "conv_1", "channel_id": ""},
            {"session_id": "team", "conversation_id": "", "channel_id": "chan_1"},
        ]
        monkeypatch.setenv("NEXUS_TASK_MCP_URL", "http://task.test/mcp")
        result = await _sync(app, employee, sessions)
        assert result["status"] == "applied"
        assert skills["agt_1"]["name"] == "任务管理"
        assert set(mcps) == {("agt_1", "private"), ("agt_1", "team")}
        for item in mcps.values():
            assert "Authorization" not in item["mcp_config"]["headers"]
            assert "X-Nexus-Task-Capability-Version" not in item["mcp_config"]["headers"]
        assert mcps[("agt_1", "private")]["mcp_config"]["headers"]["X-Nexus-Conversation-Id"] == "conv_1"
        assert mcps[("agt_1", "team")]["mcp_config"]["headers"]["X-Nexus-Channel-Id"] == "chan_1"
        deletes_before = len(deleted)

        again = await _sync(app, employee, sessions)
        assert again["status"] == "applied"
        assert len(deleted) == deletes_before

        monkeypatch.setenv("NEXUS_TASK_MCP_URL", "http://task2.test/mcp")
        updated = await _sync(app, employee, sessions)
        assert updated["status"] == "applied"
        assert {item[1] for item in deleted if item[0] == "mcp"} == {"nexus-task"}
        assert all(
            item["mcp_config"]["url"] == "http://task2.test/mcp" for item in mcps.values()
        )

        employee["skills"] = []
        disabled = await _sync(app, employee, sessions)
        assert disabled["status"] == "applied"
        assert skills == {}
        assert mcps == {}
        assert ("skill", "任务管理", "mailbox") in deleted

        employee["skills"] = ["task-management"]
        enabled = await _sync(app, employee, sessions)
        assert enabled["status"] == "applied"
        assert skills["agt_1"]["name"] == "任务管理"
        assert set(mcps) == {("agt_1", "private"), ("agt_1", "team")}

    asyncio.run(scenario())


def test_skill_upload_failure_does_not_block_mcp_registration(monkeypatch):
    async def scenario():
        app, _skills, mcps, _deleted, _health, failed = _workspace()
        failed.append("fail")
        employee = {
            "id": "emp_1",
            "agentscope_agent_id": "agt_1",
            "skills": ["task-management"],
        }
        sessions = [{"session_id": "private", "conversation_id": "conv_1", "channel_id": ""}]
        monkeypatch.setenv("NEXUS_TASK_MCP_URL", "http://task.test/mcp")
        result = await _sync(app, employee, sessions)
        assert result["status"] == "error"
        assert result["failures"][0]["stage"] == "skill_upload"
        assert "任务技能上传失败" in result["detail"]
        assert ("agt_1", "private") in mcps

    asyncio.run(scenario())


def test_discovery_failure_is_reported_in_sync_result(monkeypatch):
    async def scenario():
        app, _skills, mcps, _deleted, health, _failed = _workspace()
        employee = {
            "id": "emp_1",
            "agentscope_agent_id": "agt_1",
            "skills": ["task-management"],
        }
        sessions = [{"session_id": "private", "conversation_id": "conv_1", "channel_id": ""}]
        health[("agt_1", "private")] = False
        monkeypatch.setenv("NEXUS_TASK_MCP_URL", "http://task.test/mcp")
        result = await _sync(app, employee, sessions)
        assert result["status"] == "error"
        assert result["failures"] == [
            {
                "stage": "mcp_discovery",
                "session_id": "private",
                "detail": "MCP 连接失败",
            }
        ]
        assert "任务 MCP 连接或工具发现失败" in result["detail"]
        assert ("agt_1", "private") in mcps

    asyncio.run(scenario())


def test_attach_uploads_skill_and_mcp_independently(monkeypatch):
    from nexus_api.task_skill import attach_task_capability

    async def scenario():
        app, _skills, mcps, _deleted, _health, failed = _workspace()
        failed.append("fail")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://agentscope.test",
        ) as http:
            client = AgentScopeClient("http://agentscope.test", "nexus", http)
            employee = {
                "id": "emp_1",
                "agentscope_agent_id": "agt_1",
                "skills": ["task-management"],
            }
            await attach_task_capability(
                client,
                employee,
                "private",
                conversation_id="conv_1",
            )
            assert ("agt_1", "private") in mcps

    monkeypatch.setenv("NEXUS_TASK_MCP_URL", "http://task.test/mcp")
    asyncio.run(scenario())


def test_task_mcp_address_uses_configured_listener_port_and_preserves_explicit_url(monkeypatch):
    from types import SimpleNamespace

    from nexus_api.task_skill import task_mcp_url

    monkeypatch.delenv("NEXUS_TASK_MCP_URL", raising=False)
    monkeypatch.setattr("app.config.settings.get_settings", lambda: SimpleNamespace(task_mcp_port=18202))
    assert task_mcp_url() == "http://127.0.0.1:18202/mcp"
    monkeypatch.setattr("app.config.settings.get_settings", lambda: SimpleNamespace(task_mcp_port=19202))
    assert task_mcp_url() == "http://127.0.0.1:19202/mcp"
    monkeypatch.setenv("NEXUS_TASK_MCP_URL", "http://task.internal:18202/mcp")
    assert task_mcp_url() == "http://task.internal:18202/mcp"

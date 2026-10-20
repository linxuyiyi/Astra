"""任务 MCP 和技能挂接走真实 AgentScopeClient，对面是按 2.0.10 语义实现的路由。"""

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
    app = FastAPI()

    @app.post("/workspace/skill/upload", status_code=201)
    async def upload_skill(
        agent_id: str,
        session_id: str,
        manifest: str = Form(...),
        files: list[UploadFile] = File(...),
    ):
        del session_id, manifest
        raw = await files[0].read()
        name = ""
        for line in raw.decode().splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip()
                break
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
        item = mcps.get((agent_id, session_id))
        return [item] if item else []

    @app.delete("/workspace/mcp/{name}", status_code=204)
    async def delete_mcp(name: str, agent_id: str, session_id: str):
        deleted.append(("mcp", name, session_id))
        mcps.pop((agent_id, session_id), None)
        return Response(status_code=204)

    return app, skills, mcps, deleted


def test_rotated_key_replaces_mcp_and_skill_revokes_by_stored_name(monkeypatch):
    async def scenario():
        app, skills, mcps, deleted = _workspace()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agentscope.test") as http:
            client = AgentScopeClient("http://agentscope.test", "nexus", http)
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
            monkeypatch.setenv("NEXUS_TASK_MCP_KEY", "old-key")
            await sync_task_capability(client, employee, sessions)
            assert skills["agt_1"]["name"] == "任务管理"
            assert set(mcps) == {("agt_1", "mailbox"), ("agt_1", "private"), ("agt_1", "team")}
            assert all(
                item["mcp_config"]["headers"]["Authorization"] == "Bearer old-key" for item in mcps.values()
            )
            deletes_before = len(deleted)

            await sync_task_capability(client, employee, sessions)
            assert len(deleted) == deletes_before

            monkeypatch.setenv("NEXUS_TASK_MCP_KEY", "new-key")
            await sync_task_capability(client, employee, sessions)
            assert {item[1] for item in deleted if item[0] == "mcp"} == {"nexus-task"}
            assert all(
                item["mcp_config"]["headers"]["Authorization"] == "Bearer new-key" for item in mcps.values()
            )
            assert mcps[("agt_1", "private")]["mcp_config"]["headers"]["X-Nexus-Conversation-Id"] == "conv_1"
            assert mcps[("agt_1", "team")]["mcp_config"]["headers"]["X-Nexus-Channel-Id"] == "chan_1"

            employee["skills"] = []
            await sync_task_capability(client, employee, sessions)
            assert skills == {}
            assert mcps == {}
            assert ("skill", "任务管理", "mailbox") in deleted
            assert ("skill", "task-management", "mailbox") not in deleted

            employee["skills"] = ["task-management"]
            await sync_task_capability(client, employee, sessions)
            assert skills["agt_1"]["name"] == "任务管理"
            assert all(
                item["mcp_config"]["headers"]["Authorization"] == "Bearer new-key" for item in mcps.values()
            )

    asyncio.run(scenario())

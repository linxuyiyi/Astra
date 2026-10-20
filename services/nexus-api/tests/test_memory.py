"""压缩配置和按 workspace 装配的长期记忆。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from nexus_api.agentscope import AgentScopeClient
from nexus_api.main import create_app, session_transcript

ROOT = Path(__file__).resolve().parents[3]


def employee_body(**overrides):
    payload = {
        "name": "星河",
        "domain": "测试自动化工程师",
        "description": "负责自动化脚本",
        "instructions": "直接回答。",
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
        "nexus_agent_service_memory",
        ROOT / "services/agent-service/agent_service.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_memory_uses_the_workspace_and_a_failure_stays_out_of_the_chat():
    module = _agent_service()

    class Middleware:
        def __init__(self, *, workdir: str, backend) -> None:
            self.workdir = workdir
            self.backend = backend

    class Workspace:
        def __init__(self, workdir: str) -> None:
            self.workdir = workdir

        def get_backend(self):
            return f"backend:{self.workdir}"

    missing = asyncio.run(module.memory_middlewares("nexus", "agent-a", "s1", Workspace("/ws/u1")))
    assert missing == []
    assert module.memory_status("agent-a")["status"] == "degraded"
    assert module.memory_status("agent-b")["ok"] is True

    def capture(*, workdir, backend):
        return Middleware(workdir=workdir, backend=backend)

    one = module.attach_memory(Workspace("/ws/u1"), capture, key="agent-a")
    other = module.attach_memory(Workspace("/ws/u2"), capture, key="agent-b")
    assert one[0].workdir == "/ws/u1"
    assert other[0].workdir == "/ws/u2"
    assert one[0].backend == "backend:/ws/u1"
    assert other[0].backend == "backend:/ws/u2"
    assert one[0].workdir != other[0].workdir
    assert module.memory_status("agent-a")["ok"] is True

    def broken(*, workdir, backend):
        raise RuntimeError("记忆组件不可用")

    assert module.attach_memory(Workspace("/ws/u1"), broken, key="agent-a") == []
    status = module.memory_status("agent-a")
    assert status["status"] == "degraded"
    assert "记忆组件不可用" in status["error"]
    assert module.memory_status("agent-b")["ok"] is True

    class Hook:
        def __init__(self, *, workdir, backend) -> None:
            self.workdir = workdir

        async def on_system_prompt(self, agent, current_prompt: str) -> str:
            raise RuntimeError("hook failed")

        async def on_reply(self, agent, input_kwargs, next_handler):
            raise RuntimeError("hook failed")
            yield None

    guarded = module.attach_memory(Workspace("/ws/hook"), lambda **kwargs: Hook(**kwargs), key="agent-hook")
    assert asyncio.run(guarded[0].on_system_prompt(None, "原来的提示")) == "原来的提示"

    async def next_handler(**_kwargs):
        yield "继续"

    async def collect():
        return [item async for item in guarded[0].on_reply(None, {}, next_handler)]

    assert asyncio.run(collect()) == ["继续"]
    assert module.memory_status("agent-hook")["status"] == "degraded"
    assert module.memory_status("agent-b")["status"] == "ok"


def test_private_workspaces_stay_apart_and_rebind_keeps_the_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    session_bodies: list[dict] = []
    agent_bodies: list[dict] = []
    sessions = {"session-a": [], "session-b": [], "session-rebind": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/":
            body = json.loads(request.content.decode())
            agent_bodies.append(body)
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/sessions/" and request.method == "POST":
            body = json.loads(request.content.decode())
            session_bodies.append(body)
            if body.get("workspace_id") == "ws-conv-reused":
                return httpx.Response(201, json={"session_id": "session-rebind"})
            name = body.get("workspace_id") or body.get("name")
            return httpx.Response(201, json={"session_id": f"session-{name}"})
        if request.url.path == "/chat/":
            return httpx.Response(200, json={"status": "started", "session_id": "ignored"})
        if request.method == "GET" and request.url.path.endswith("/messages"):
            session_id = request.url.path.split("/")[2]
            return httpx.Response(
                200,
                json={"messages": sessions.get(session_id, []), "has_more": False},
            )
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    with TestClient(create_app()) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        employee = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        assert agent_bodies[0]["chat_config"]["context_config"] == {
            "trigger_ratio": 0.8,
            "reserve_ratio": 0.1,
        }
        first = test_client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers={"X-Verified-UID": "u1"},
            json={},
        ).json()
        second = test_client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers={"X-Verified-UID": "u2"},
            json={},
        ).json()
        assert first["id"] != second["id"]
        opened = test_client.post(
            f"/api/v1/conversations/{first['id']}/messages",
            headers={"X-Verified-UID": "u1"},
            json={"text": "只属于甲"},
        )
        test_client.post(
            f"/api/v1/conversations/{second['id']}/messages",
            headers={"X-Verified-UID": "u2"},
            json={"text": "只属于乙"},
        )
        private = [item for item in session_bodies if item.get("workspace_id")]
        assert len(private) == 2
        assert private[0]["workspace_id"] != private[1]["workspace_id"]
        assert opened.json()["agentscope"]["session_id"]
        again = test_client.post(
            f"/api/v1/conversations/{first['id']}/messages",
            headers={"X-Verified-UID": "u1"},
            json={"text": "还是甲"},
        )
        assert again.json()["agentscope"]["session_id"] == opened.json()["agentscope"]["session_id"]
        session_id = opened.json()["agentscope"]["session_id"]
        sessions[session_id] = [
            {
                "id": "old",
                "role": "user",
                "name": "u1",
                "content": [{"type": "text", "text": "压缩前的那件事"}],
            },
            {
                "id": "new",
                "role": "assistant",
                "content": [{"type": "text", "text": "窗口里只剩这句"}],
            },
        ]
        history = test_client.get(
            f"/api/v1/conversations/{first['id']}",
            headers={"X-Verified-UID": "u1"},
        ).json()["messages"]
        assert [item["text"] for item in history] == ["压缩前的那件事", "窗口里只剩这句"]
        assert session_transcript(sessions[session_id], employee["id"])[0]["text"] == "压缩前的那件事"
        from nexus_api.db import connect

        connection = connect(tmp_path / "nexus.sqlite3")
        connection.execute(
            "UPDATE conversations SET workspace_id = ? WHERE id = ?",
            ("ws-conv-reused", first["id"]),
        )
        connection.commit()
        connection.close()
        rebound = test_client.post(
            f"/api/v1/conversations/{first['id']}/rebind",
            headers={"X-Verified-UID": "u1"},
        )
        assert rebound.status_code == 200, rebound.text
        assert rebound.json() == {"session_id": "session-rebind", "workspace_id": "ws-conv-reused"}
        assert session_bodies[-1]["workspace_id"] == "ws-conv-reused"
        assert rebound.json()["session_id"] != session_id

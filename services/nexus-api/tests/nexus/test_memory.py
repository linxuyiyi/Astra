"""压缩配置和按 workspace 装配的长期记忆。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
from sqlalchemy import text as sql_text

from nexus_api.agentscope import AgentScopeClient
from nexus_api.main import create_app
from tests.nexus.agent_transport import AgentChatTransport
from tests.nexus.clients import TestClient

ROOT = Path(__file__).resolve().parents[4]


def employee_body(**overrides):
    payload = {
        "name": "星河",
        "domain": "测试自动化工程师",
        "description": "负责自动化脚本",
        "instructions": "直接回答。",
        "avatar": "robot-v1-0",
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
    session_missing = False
    saved_file, missing_file = "att_" + "a" * 32, "att_" + "b" * 32
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
        if request.method == "GET" and request.url.path.endswith("/status"):
            return httpx.Response(404 if session_missing else 200, json={"status": "idle"})
        if request.url.path == "/nexus/workspace-attachments/verify":
            body = json.loads(request.content)
            assert body["workspace_id"] == "ws-conv-reused"
            assert body["keys"] == [saved_file, missing_file]
            return httpx.Response(200, json={"missing": [missing_file] if session_missing else []})
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
            httpx.AsyncClient(transport=AgentChatTransport(handler), base_url="http://agentscope.test"),
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
            json={"client_id": "test-send-179", "text": "只属于甲"},
        )
        test_client.post(
            f"/api/v1/conversations/{second['id']}/messages",
            headers={"X-Verified-UID": "u2"},
            json={"client_id": "test-send-184", "text": "只属于乙"},
        )
        private = [item for item in session_bodies if item.get("workspace_id")]
        assert len(private) == 2
        assert private[0]["workspace_id"] != private[1]["workspace_id"]
        assert opened.json()["agentscope"]["session_id"]
        again = test_client.post(
            f"/api/v1/conversations/{first['id']}/messages",
            headers={"X-Verified-UID": "u1"},
            json={"client_id": "test-send-193", "text": "还是甲"},
        )
        assert again.json()["agentscope"]["session_id"] == opened.json()["agentscope"]["session_id"]
        session_id = opened.json()["agentscope"]["session_id"]
        sessions[session_id] = [
            {
                "id": "old",
                "created_at": "2026-11-12T00:00:00Z",
                "role": "user",
                "name": "u1",
                "content": [{"type": "text", "text": "压缩前的那件事"}],
            },
            {
                "id": "new",
                "created_at": "2026-11-12T00:00:01Z",
                "role": "assistant",
                "content": [{"type": "text", "text": "窗口里只剩这句"}],
            },
        ]
        history = test_client.get(
            f"/api/v1/conversations/{first['id']}/messages",
            headers={"X-Verified-UID": "u1"},
        ).json()["messages"]
        assert [item["content"][0]["text"] for item in history] == ["压缩前的那件事", "窗口里只剩这句"]
        from nexus_api.db import connect

        connection = connect(tmp_path / "nexus.db")
        connection.execute(
            sql_text("UPDATE t_nexus_conversations SET workspace_id = :p0 WHERE id = :p1"),
            {"p0": "ws-conv-reused", "p1": first["id"]},
        )
        from nexus_api.sql import insert_record

        for key in (saved_file, missing_file):
            insert_record(connection, "t_nexus_attachment_refs", {
                "id": key, "owner_uid": "u1", "scope_kind": "private", "scope_id": first["id"],
                "agent_id": employee["agentscope_agent_id"], "session_id": session_id, "name": "file.txt",
                "media_type": "text/plain", "size": 1, "sha256": "f" * 64, "linked": 1,
                "created_at": "2026-11-12T00:00:00+00:00",
            }, ignore_keys=["id"])
        connection.commit()
        connection.close()
        original_count = len(session_bodies)
        preserved = test_client.post(
            f"/api/v1/conversations/{first['id']}/rebind",
            headers={"X-Verified-UID": "u1"},
        )
        assert preserved.json() == {
            "session_id": session_id, "workspace_id": "ws-conv-reused", "missing_attachment_ids": [],
        }
        assert len(session_bodies) == original_count
        session_missing = True
        rebound = test_client.post(
            f"/api/v1/conversations/{first['id']}/rebind",
            headers={"X-Verified-UID": "u1"},
        )
        assert rebound.status_code == 200, rebound.text
        assert rebound.json() == {
            "session_id": "session-rebind", "workspace_id": "ws-conv-reused",
            "missing_attachment_ids": [missing_file],
        }
        assert session_bodies[-1]["workspace_id"] == "ws-conv-reused"
        assert rebound.json()["session_id"] != session_id
        with connect(tmp_path / "nexus.db") as db:
            rows = db.execute(sql_text(
                "SELECT id,session_id,linked FROM t_nexus_attachment_refs ORDER BY id",
            )).all()
        assert rows == [(saved_file, "session-rebind", 1), (missing_file, "session-rebind", -1)]
        response = test_client.get(f"/api/v1/attachments/{missing_file}", headers={"X-Verified-UID": "u1"})
        assert response.status_code == 404

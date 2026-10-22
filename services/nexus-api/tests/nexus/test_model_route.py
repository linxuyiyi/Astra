"""员工模型只走已启用的网关路由。密钥不出现在页面响应里。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from nexus_api.agentscope import AgentScopeClient
from nexus_api.main import create_app
from nexus_api.model_route import gateway_base_url
from tests.nexus.clients import TestClient


def test_gateway_base_rejects_a_vendor_host(monkeypatch):
    monkeypatch.setenv("NEXUS_GATEWAY_OPENAI_BASE", "https://api.deepseek.com/v1")
    with pytest.raises(Exception) as raised:
        gateway_base_url()
    assert "直连厂商" in str(raised.value)


def test_route_is_injected_and_a_change_keeps_the_private_session(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_AGENTSCOPE_BASE_URL", "http://agentscope.test")
    monkeypatch.setenv("NEXUS_GATEWAY_API_KEY", "gw-secret-value")
    monkeypatch.setenv("NEXUS_GATEWAY_OPENAI_BASE", "http://127.0.0.1:18110/v1")
    routes = [
        {"id": "route-1", "model": "deepseek-v4-flash", "enabled": True},
        {"id": "route-2", "model": "gpt-4.1", "enabled": True},
        {"id": "route-off", "model": "hidden", "enabled": False},
    ]

    async def loader(_url):
        return routes

    monkeypatch.setattr("nexus_api.model_route.fetch_routes", loader)
    seen: list[httpx.Request] = []
    sessions = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/agent/" and request.method == "POST":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/agent/agent-1" and request.method == "PATCH":
            return httpx.Response(200, json={})
        if request.url.path == "/credential/" and request.method == "GET":
            return httpx.Response(200, json={"credentials": []})
        if request.url.path == "/credential/" and request.method == "POST":
            return httpx.Response(201, json={"credential_id": "cred-gw"})
        if request.url.path == "/sessions/" and request.method == "POST":
            sessions["n"] += 1
            return httpx.Response(201, json={"session_id": f"ses-{sessions['n']}"})
        if request.method == "PATCH" and request.url.path.startswith("/sessions/"):
            return httpx.Response(200, json={})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=[])
        if request.url.path == "/chat/":
            return httpx.Response(200, json={"status": "started", "session_id": "ses-2"})
        return httpx.Response(200, json={})

    application = create_app()
    with TestClient(application) as client:
        application.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        created = client.post(
            "/api/v1/employees",
            json={
                "name": "星河",
                "domain": "测试自动化",
                "description": "负责自动化脚本",
                "instructions": "直接回答。",
                "model_route_id": "route-off",
            },
        )
        assert created.status_code == 422
        assert "未启用" in created.json()["detail"]
        assert "gw-secret-value" not in created.text

        created = client.post(
            "/api/v1/employees",
            json={
                "name": "星河",
                "domain": "测试自动化",
                "description": "负责自动化脚本",
                "instructions": "直接回答。",
                "model_route_id": "route-1",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["employee"]["model_route_id"] == "route-1"
        assert "gw-secret-value" not in created.text
        assert "api.deepseek.com" not in created.text

        credential = next(item for item in seen if item.url.path == "/credential/" and item.method == "POST")
        posted = json.loads(credential.content.decode())
        assert posted["data"]["base_url"] == "http://127.0.0.1:18110/v1"
        assert posted["data"]["type"] == "openai_credential"
        mailbox = next(item for item in seen if item.url.path == "/sessions/" and item.method == "POST")
        mailbox_body = json.loads(mailbox.content.decode())
        assert mailbox_body["chat_model_config"]["model"] == "deepseek-v4-flash"
        assert mailbox_body["chat_model_config"]["credential_id"] == "cred-gw"
        assert "gw-secret-value" not in json.dumps(mailbox_body)

        employee_id = body["employee"]["id"]
        headers = {"X-Owner-Id": "alice"}
        conversation = client.post(
            f"/api/v1/employees/{employee_id}/conversations",
            headers=headers,
            json={"name": "私聊"},
        ).json()
        before = sessions["n"]
        sent = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=headers,
            json={"text": "你好"},
        )
        assert sent.status_code == 200, sent.text
        assert sessions["n"] == before + 1
        private_id = sent.json()["agentscope"]["session_id"]

        seen.clear()
        updated = client.patch(
            f"/api/v1/employees/{employee_id}",
            json={
                "name": "星河",
                "domain": "测试自动化",
                "description": "负责自动化脚本",
                "instructions": "直接回答。",
                "model_route_id": "route-2",
            },
        )
        assert updated.status_code == 200, updated.text
        assert "gw-secret-value" not in updated.text
        assert sessions["n"] == before + 1
        patches = [item for item in seen if item.method == "PATCH" and item.url.path.startswith("/sessions/")]
        assert patches
        patched = json.loads(patches[-1].content.decode())
        assert patched["chat_model_config"]["model"] == "gpt-4.1"
        listed = client.get(f"/api/v1/employees/{employee_id}/conversations", headers=headers).json()
        assert listed["items"][0]["agentscope_session_id"] == private_id


def test_empty_route_refuses_a_vendor_session(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_AGENTSCOPE_BASE_URL", "http://agentscope.test")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/agent/" and request.method == "POST":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        return httpx.Response(200, json={})

    application = create_app()
    with TestClient(application) as client:
        application.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        created = client.post(
            "/api/v1/employees",
            json={
                "name": "星河",
                "domain": "测试自动化",
                "description": "负责自动化脚本",
                "instructions": "直接回答。",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["agentscope"]["ok"] is False
        assert "请先选择模型路由" in body["agentscope"]["detail"]
        assert body["employee"]["mailbox_session_id"] in {None, ""}
        assert not any(item.method == "POST" and item.url.path == "/sessions/" for item in seen)
        assert "api.deepseek.com" not in created.text


def test_gateway_credential_updates_when_the_key_changes():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "credentials": [
                        {
                            "id": "cred-1",
                            "data": {
                                "type": "openai_credential",
                                "base_url": "http://127.0.0.1:18110/v1",
                                "api_key": "old-key",
                            },
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"id": "cred-1", "credential_id": "cred-1"})

    client = AgentScopeClient(
        "http://agentscope.test",
        "nexus",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
    )

    async def once(key: str) -> str:
        return await client.ensure_gateway_credential(key, "http://127.0.0.1:18110/v1")

    assert asyncio.run(once("old-key")) == "cred-1"
    assert [item.method for item in seen] == ["GET"]
    seen.clear()
    assert asyncio.run(once("new-key")) == "cred-1"
    assert [item.method for item in seen] == ["GET", "PATCH"]
    body = json.loads(seen[1].content.decode())
    assert body["data"]["api_key"] == "new-key"
    assert body["data"]["base_url"] == "http://127.0.0.1:18110/v1"


def test_empty_route_without_bootstrap_does_not_create_a_session(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG", raising=False)
    monkeypatch.setenv("NEXUS_GATEWAY_API_KEY", "gw-test-key")
    monkeypatch.setenv("NEXUS_GATEWAY_OPENAI_BASE", "http://127.0.0.1:18110/v1")

    async def loader(_url):
        return [{"id": "route-1", "model": "opencode/deepseek-v4.1-flash-free", "enabled": True}]

    monkeypatch.setattr("nexus_api.model_route.fetch_routes", loader)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/agent/" and request.method == "POST":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/agent/agent-1" and request.method == "PATCH":
            return httpx.Response(200, json={})
        if request.url.path == "/credential/" and request.method == "GET":
            return httpx.Response(200, json={"credentials": []})
        if request.url.path == "/credential/" and request.method == "POST":
            return httpx.Response(201, json={"credential_id": "cred-gw"})
        if request.url.path == "/sessions/" and request.method == "POST":
            return httpx.Response(201, json={"session_id": "ses-later"})
        if request.method == "PATCH" and request.url.path.startswith("/sessions/"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    employee = {
        "name": "星河",
        "domain": "测试自动化",
        "description": "负责自动化脚本",
        "instructions": "直接回答。",
    }
    application = create_app()
    with TestClient(application) as client:
        application.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        created = client.post("/api/v1/employees", json=employee)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["agentscope"]["ok"] is False
        assert "请先选择模型路由" in body["agentscope"]["detail"]
        assert body["employee"]["mailbox_session_id"] in {None, ""}
        assert not any(item.method == "POST" and item.url.path == "/sessions/" for item in seen)

        headers = {"X-Owner-Id": "alice"}
        conversation = client.post(
            f"/api/v1/employees/{body['employee']['id']}/conversations",
            headers=headers,
            json={"name": "私聊"},
        ).json()
        sent = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=headers,
            json={"text": "你好"},
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["agentscope"]["accepted"] is False
        assert "请先选择模型路由" in sent.json()["agentscope"]["detail"]
        assert not any(item.method == "POST" and item.url.path == "/sessions/" for item in seen)

        updated = client.patch(
            f"/api/v1/employees/{body['employee']['id']}",
            json={**employee, "model_route_id": "route-1"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["employee"]["mailbox_session_id"] == "ses-later"
        mailbox = next(item for item in seen if item.method == "POST" and item.url.path == "/sessions/")
        posted = json.loads(mailbox.content.decode())
        assert posted["chat_model_config"]["model"] == "opencode/deepseek-v4.1-flash-free"
        assert "gw-test-key" not in updated.text
        assert "gw-test-key" not in mailbox.content.decode()

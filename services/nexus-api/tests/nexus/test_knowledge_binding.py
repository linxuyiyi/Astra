"""员工会话只带上自己绑定的 AgentScope 知识库。"""

import json

import httpx
from fastapi.testclient import TestClient

from nexus_api.agentscope import AgentScopeClient
from nexus_api.main import create_app


def employee_body(name: str, domain: str, knowledge: list[str]) -> dict:
    return {
        "name": name,
        "domain": domain,
        "description": "负责资料",
        "instructions": "按资料回答。",
        "enabled": True,
        "knowledge": knowledge,
        "skills": [],
        "tools": [],
        "model_route_id": "route-test",
    }


def test_session_receives_only_the_bound_knowledge_bases(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_AGENTSCOPE_BASE_URL", "http://agentscope.test")
    seen: list[httpx.Request] = []
    sessions = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/agent/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.method == "PATCH" and request.url.path.startswith("/agent/"):
            return httpx.Response(200, json={})
        if request.url.path == "/sessions/" and request.method == "POST":
            sessions["n"] += 1
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"session-{body['name']}-{sessions['n']}"})
        if request.method == "PATCH" and request.url.path.startswith("/sessions/"):
            return httpx.Response(200, json={})
        if request.url.path == "/knowledge_bases/":
            return httpx.Response(
                200,
                json={
                    "knowledge_bases": [{"id": "kb-1", "name": "泄压步骤"}],
                    "total": 1,
                    "page": 1,
                    "page_size": 128,
                },
            )
        if request.url.path == "/chat/":
            return httpx.Response(200, json={"status": "started", "session_id": "session-chat"})
        return httpx.Response(404, json={"detail": "missing"})

    application = create_app()
    with TestClient(application) as client:
        client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        bound = client.post("/api/v1/employees", json=employee_body("星河", "资料", ["kb-1"]))
        assert bound.status_code == 201, bound.text
        unbound = client.post("/api/v1/employees", json=employee_body("未绑定", "其他", []))
        assert unbound.status_code == 201, unbound.text

        posts = [
            json.loads(item.content.decode())
            for item in seen
            if item.url.path == "/sessions/" and item.method == "POST"
        ]
        bound_mailbox = next(item for item in posts if item["name"] == "星河的邮箱")
        unbound_mailbox = next(item for item in posts if item["name"] == "未绑定的邮箱")
        assert bound_mailbox["knowledge_config"] == {"knowledge_base_ids": ["kb-1"]}
        assert "knowledge_config" not in unbound_mailbox

        employee_id = bound.json()["employee"]["id"]
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
            json={"text": "怎么泄压"},
        )
        assert sent.status_code == 200, sent.text
        assert sessions["n"] == before + 1
        private_posts = [
            json.loads(item.content.decode())
            for item in seen
            if item.url.path == "/sessions/" and item.method == "POST"
        ]
        assert private_posts[-1]["knowledge_config"] == {"knowledge_base_ids": ["kb-1"]}

        seen.clear()
        cleared = client.patch(
            f"/api/v1/employees/{employee_id}",
            json=employee_body("星河", "资料", []),
        )
        assert cleared.status_code == 200, cleared.text
        assert sessions["n"] == before + 1
        patches = [
            json.loads(item.content.decode())
            for item in seen
            if item.method == "PATCH" and item.url.path.startswith("/sessions/")
        ]
        knowledge_patches = [item for item in patches if "knowledge_config" in item]
        assert knowledge_patches
        assert all(item["knowledge_config"] == {"knowledge_base_ids": []} for item in knowledge_patches)

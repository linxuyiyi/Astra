"""员工绑定要在目录、工作区和 toolkit 里同时出现才算生效。"""

import asyncio
import json

import httpx

from nexus_api.agentscope import AgentScopeClient
from nexus_api.capability import binding_revision, inspect_employee, missing_bindings
from nexus_api.main import create_app
from tests.nexus.clients import TestClient


def test_missing_binding_names_the_gap():
    assert missing_bindings(["review"], {"review"}, set(), {"review"}) == ["review"]
    assert missing_bindings(["review"], {"review"}, {"review"}, {"review"}) == []


def test_unavailable_catalog_keeps_applied_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_AGENTSCOPE_BASE_URL", "")
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/employees",
            json={
                "name": "星河",
                "domain": "测试",
                "description": "负责",
                "instructions": "直接回答",
                "skills": ["task-management"],
                "knowledge": [],
                "tools": [],
            },
        )
    assert response.status_code == 201, response.text
    capability = response.json()["employee"]["capability"]
    assert capability["desired_revision"] == binding_revision(["task-management"])
    assert capability["applied_revision"] == ""
    assert capability["runtime_status"] == "error"
    assert capability["error"]


def test_session_toolkit_must_list_the_binding():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/skill":
            return httpx.Response(200, json=[{"name": "review"}])
        if request.url.path == "/mcp":
            return httpx.Response(200, json=[])
        if request.url.path == "/workspace/skill":
            return httpx.Response(200, json=[{"name": "review"}])
        if request.url.path == "/workspace/mcp":
            return httpx.Response(200, json=[{"name": "library", "tools": [{"name": "search"}]}])
        return httpx.Response(404)

    client = AgentScopeClient(
        "http://agentscope.test",
        "nexus",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
    )
    ready = asyncio.run(
        inspect_employee(
            client,
            {
                "skills": ["review"],
                "tools": [],
                "agentscope_agent_id": "agent",
                "mailbox_session_id": "mail",
                "capability": {},
            },
            "now",
        )
    )
    assert ready["runtime_status"] == "applied"
    assert {"/skill", "/mcp", "/workspace/skill", "/workspace/mcp"} <= set(seen)
    missed = asyncio.run(
        inspect_employee(
            client,
            {
                "skills": ["absent"],
                "tools": [],
                "agentscope_agent_id": "agent",
                "mailbox_session_id": "mail",
                "capability": {"applied_revision": "kept"},
            },
            "now",
        )
    )
    assert missed["applied_revision"] == "kept"
    assert missed["runtime_status"] == "error"
    assert json.loads(json.dumps(missed))["error"]

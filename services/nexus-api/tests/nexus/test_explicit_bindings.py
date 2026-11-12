"""Skill/MCP 通过管理资源独立配置，名称不触发平台自动能力。"""

import pytest

from nexus_api.main import create_app
from tests.nexus.clients import TestClient
from tests.nexus.test_resources import FakeAgentScope, attach, employee_body, skill_markdown


def private_session(client, employee):
    headers = {"X-Owner-Id": "resource-owner"}
    conversation = client.post(
        f"/api/v1/employees/{employee['id']}/conversations", headers=headers, json={"name": "配置验证"}
    ).json()
    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages", headers=headers,
        json={"client_id": "test-send-16", "text": "验证绑定"}
    )
    assert response.status_code == 200, response.text
    return conversation["id"], response.json()["agentscope"]["session_id"]


def test_empty_catalog_and_employee_chat_do_not_create_resources(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake = FakeAgentScope()
    fake.skill_records = []
    fake.mcp_records = []
    with TestClient(create_app()) as client:
        attach(client, fake)
        assert client.get("/api/v1/config/skills").json()["items"] == []
        assert client.get("/api/v1/config/mcps").json()["items"] == []
        unknown = client.post("/api/v1/employees", json=employee_body(skills=["task-management"]))
        assert unknown.status_code == 422
        employee = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        _, sid = private_session(client, employee)
        assert fake.workspace(employee["agentscope_agent_id"], sid) == ([], [])
        assert fake.uploads == fake.added_mcps == []
        assert client.get("/api/v1/config/skills").json()["items"] == []
        assert client.get("/api/v1/config/mcps").json()["items"] == []


@pytest.mark.parametrize("mcp_name", ["nexus-task", "nexus-schedule", "业务查询"])
def test_skill_and_mcp_binding_and_unbinding_are_independent(tmp_path, monkeypatch, mcp_name):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake = FakeAgentScope()
    fake.mcp_records[0]["name"] = mcp_name
    with TestClient(create_app()) as client:
        attach(client, fake)
        skill = client.post(
            "/api/v1/config/skills", json={"content": skill_markdown("任务管理", "显式配置的普通技能")}
        ).json()["skill"]
        employee = client.post("/api/v1/employees", json=employee_body(skills=[skill["id"]])).json()[
            "employee"
        ]
        _, sid = private_session(client, employee)
        agent_id = employee["agentscope_agent_id"]
        assert [item["name"] for item in fake.workspace(agent_id, sid)[0]] == ["任务管理"]
        assert fake.workspace(agent_id, sid)[1] == []
        path = f"/api/v1/employees/{employee['id']}"
        for _ in range(3):
            saved = client.patch(path, json=employee_body(skills=[skill["id"]], mcps=["ext-mcp"]))
            assert saved.status_code == 200, saved.text
            assert saved.json()["sync"]["status"] == "applied"
            assert saved.json()["employee"]["capability"]["runtime_status"] == "applied"
            assert [item["name"] for item in fake.workspace(agent_id, sid)[1]] == [mcp_name]
        # 只解绑 Skill，MCP 仍可用。
        assert client.patch(path, json=employee_body(mcps=["ext-mcp"])).json()["sync"]["ok"]
        skills, mcps = fake.workspace(agent_id, sid)
        assert skills == []
        assert [item["name"] for item in mcps] == [mcp_name]
        assert client.patch(path, json=employee_body(skills=[skill["id"]], mcps=["ext-mcp"])).json()["sync"][
            "ok"
        ]
        # 只解绑 MCP，Skill 保留。
        assert client.patch(path, json=employee_body(skills=[skill["id"]])).json()["sync"]["ok"]
        skills, mcps = fake.workspace(agent_id, sid)
        assert [item["name"] for item in skills] == ["任务管理"]
        assert mcps == []

"""员工技能装入会话工作区。MockTransport 按 AgentScope 2.0.10 的 from-library 语义返回。"""

from __future__ import annotations

import asyncio
import json

import httpx
from sqlalchemy import text as sql_text

from nexus_api.agentscope import AgentScopeClient
from nexus_api.capability import inspect_employee
from nexus_api.db import connect
from nexus_api.main import create_app, db_path
from tests.nexus.clients import TestClient

SKILL_ID = "failure-analysis"
SKILL_NAME = "失败分析"


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


class FakeAgentScope:
    def __init__(
        self,
        *,
        workspace_skills: list[dict[str, str]] | None = None,
        failed: dict[str, str] | None = None,
    ) -> None:
        self.catalog = [{"id": SKILL_ID, "name": SKILL_NAME}]
        self.workspace_skills = workspace_skills or []
        self.failed = failed or {}
        self.from_library: list[dict] = []
        self.catalog_calls = 0
        self.sessions = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path == "/agent/" and method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if method == "PATCH" and path.startswith("/agent/"):
            return httpx.Response(200, json={})
        if path == "/sessions/" and method == "POST":
            self.sessions += 1
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"ses-{self.sessions}-{body['name']}"})
        if method == "PATCH" and path.startswith("/sessions/"):
            return httpx.Response(200, json={})
        if path == "/skill" and method == "GET":
            self.catalog_calls += 1
            return httpx.Response(200, json=self.catalog)
        if path == "/mcp" and method == "GET":
            return httpx.Response(200, json=[])
        if path == "/workspace/skill/from-library" and method == "POST":
            body = json.loads(request.content.decode())
            self.from_library.append(
                {
                    "agent_id": request.url.params.get("agent_id"),
                    "session_id": request.url.params.get("session_id"),
                    "skill_ids": body["skill_ids"],
                }
            )
            return httpx.Response(
                201,
                json={"added": body["skill_ids"], "failed": self.failed},
            )
        if path == "/workspace/skill" and method == "GET":
            return httpx.Response(200, json=self.workspace_skills)
        if path == "/workspace/mcp" and method == "GET":
            return httpx.Response(200, json=[])
        if path in {"/workspace/skill/upload", "/workspace/mcp"}:
            return httpx.Response(200, json={})
        if path == "/chat/" and method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(200, json={"status": "started", "session_id": body["session_id"]})
        return httpx.Response(404, json={"detail": "missing"})


def _client(test_client: TestClient, fake: FakeAgentScope) -> None:
    test_client.app.state.agentscope = AgentScopeClient(
        "http://agentscope.test",
        "nexus",
        httpx.AsyncClient(
            transport=httpx.MockTransport(fake.handler),
            base_url="http://agentscope.test",
        ),
    )


def test_create_installs_employee_skills_in_mailbox(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake = FakeAgentScope(workspace_skills=[{"name": SKILL_NAME}])
    with TestClient(create_app()) as test_client:
        _client(test_client, fake)
        created = test_client.post("/api/v1/employees", json=employee_body(skills=[SKILL_ID]))
        assert created.status_code == 201, created.text
        body = created.json()
        employee = body["employee"]
        assert body["agentscope"]["ok"] is True

    mailbox = [item for item in fake.from_library if item["session_id"] == employee["mailbox_session_id"]]
    assert mailbox, fake.from_library
    assert fake.catalog_calls > 0
    assert mailbox[0]["agent_id"] == employee["agentscope_agent_id"]
    assert mailbox[0]["skill_ids"] == [SKILL_ID]


def test_task_skill_only_does_not_use_the_library(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake = FakeAgentScope(workspace_skills=[{"name": "任务管理"}])
    with TestClient(create_app()) as test_client:
        _client(test_client, fake)
        created = test_client.post("/api/v1/employees", json=employee_body(skills=["task-management"]))
        assert created.status_code == 201, created.text
        assert created.json()["employee"]["skills"] == ["task-management"]

    assert fake.from_library == []


def test_private_send_and_rebind_sync_existing_employee_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake = FakeAgentScope(workspace_skills=[{"name": SKILL_NAME}])
    with TestClient(create_app()) as test_client:
        _client(test_client, fake)
        employee = test_client.post("/api/v1/employees", json=employee_body(skills=[SKILL_ID])).json()[
            "employee"
        ]
        headers = {"X-Owner-Id": "browser-a"}
        conversation = test_client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers=headers,
            json={"name": "私聊"},
        ).json()
        fake.from_library.clear()
        sent = test_client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=headers,
            json={"text": "看一下失败日志"},
        )
        assert sent.status_code == 200, sent.text
        session_id = sent.json()["agentscope"]["session_id"]
        assert session_id
        private = [item for item in fake.from_library if item["session_id"] == session_id]
        assert private, fake.from_library
        assert private[0]["skill_ids"] == [SKILL_ID]

        fake.from_library.clear()
        rebound = test_client.post(
            f"/api/v1/conversations/{conversation['id']}/rebind",
            headers=headers,
        )
        assert rebound.status_code == 200, rebound.text
        rebound_session = rebound.json()["session_id"]
        rebound_calls = [item for item in fake.from_library if item["session_id"] == rebound_session]
        assert rebound_calls, fake.from_library
        assert rebound_calls[0]["skill_ids"] == [SKILL_ID]


def test_group_session_installs_employee_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake = FakeAgentScope(workspace_skills=[{"name": SKILL_NAME}])
    with TestClient(create_app()) as test_client:
        _client(test_client, fake)
        employee = test_client.post("/api/v1/employees", json=employee_body(skills=[SKILL_ID])).json()[
            "employee"
        ]
        channel = test_client.post(
            "/api/v1/channels",
            json={"name": "故障协同", "description": "自动化失败", "member_ids": [employee["id"]]},
        ).json()
        fake.from_library.clear()
        sent = test_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            json={"text": "@星河 看一下失败日志", "client_id": "skill-test-0001"},
        )
        assert sent.status_code == 200, sent.text
        import time

        deadline = time.monotonic() + 3
        session_id = None
        while not session_id and time.monotonic() < deadline:
            members = test_client.get(f"/api/v1/channels/{channel['id']}/members").json()["members"]
            session_id = next(
                (m["agentscope_session_id"] for m in members if m["member_type"] == "employee"), None
            )
            time.sleep(0.03)
        assert session_id
        group = [item for item in fake.from_library if item["session_id"] == session_id]
        assert group, fake.from_library
        assert group[0]["skill_ids"] == [SKILL_ID]


def test_patch_skill_change_resyncs_existing_conversations(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake = FakeAgentScope(workspace_skills=[{"name": SKILL_NAME}])
    with TestClient(create_app()) as test_client:
        _client(test_client, fake)
        employee = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        headers = {"X-Owner-Id": "browser-a"}
        conversation = test_client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers=headers,
            json={"name": "私聊"},
        ).json()
        connection = connect(db_path())
        connection.execute(
            sql_text("""
                UPDATE t_nexus_conversations SET agentscope_session_id = :p0 WHERE id = :p1
                """),
            {"p0": "ses-existing", "p1": conversation["id"]},
        )
        connection.commit()
        connection.close()
        fake.from_library.clear()

        updated = test_client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=[SKILL_ID]),
        )
        assert updated.status_code == 200, updated.text
        synced = {item["session_id"] for item in fake.from_library if item["skill_ids"] == [SKILL_ID]}
        assert employee["mailbox_session_id"] in synced
        assert "ses-existing" in synced

        fake.from_library.clear()
        unchanged = test_client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=[SKILL_ID]),
        )
        assert unchanged.status_code == 200, unchanged.text
        assert "ses-existing" not in {item["session_id"] for item in fake.from_library}


def test_failed_library_skill_keeps_save_successful_and_reports_error(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake = FakeAgentScope(failed={SKILL_NAME: "hub unavailable"})
    with TestClient(create_app()) as test_client:
        _client(test_client, fake)
        created = test_client.post("/api/v1/employees", json=employee_body(skills=[SKILL_ID]))
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["agentscope"]["ok"] is True
        assert "hub unavailable" in body["agentscope"]["detail"]
        capability = body["employee"]["capability"]
        assert capability["runtime_status"] == "error"
        assert SKILL_NAME in capability["error"]

        updated = test_client.patch(
            f"/api/v1/employees/{body['employee']['id']}",
            json=employee_body(skills=[SKILL_ID]),
        )
        assert updated.status_code == 200, updated.text


def test_capability_resolves_skill_id_to_workspace_name():
    async def scenario():
        fake = FakeAgentScope(workspace_skills=[{"name": SKILL_NAME}])
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fake.handler),
            base_url="http://agentscope.test",
        ) as http:
            client = AgentScopeClient("http://agentscope.test", "nexus", http)
            employee = {
                "skills": [SKILL_ID],
                "tools": [],
                "agentscope_agent_id": "agent-1",
                "mailbox_session_id": "ses-mail",
                "capability": {},
            }
            ready = await inspect_employee(client, employee, "now")
            assert ready["runtime_status"] == "applied", ready
            fake.workspace_skills = []
            missing = await inspect_employee(client, employee, "now")
            assert missing["runtime_status"] == "error"
            assert SKILL_NAME in missing["error"]

    asyncio.run(scenario())

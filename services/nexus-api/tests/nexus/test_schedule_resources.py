"""日程同步复用通用规则，并区分平台目录和安排者工作区身份。"""

import json

import httpx
import pytest
from sqlalchemy import text

from nexus_api.db import connect
from nexus_api.main import create_app
from nexus_api.schedules import schedule_user_id
from tests.nexus.clients import TestClient
from tests.nexus.test_resources import FakeAgentScope, attach, employee_body, skill_markdown


class ScheduledAgentScope(FakeAgentScope):
    user = schedule_user_id("alice")
    enabled = True
    fail_install = False
    fail_skill_by_id = False

    def __init__(self):
        super().__init__()
        self.calls = []

    def handler(self, request):
        self.calls.append((request.method, request.url.path, request.headers["X-User-ID"]))
        path = request.url.path
        if path == "/nexus/schedule-session":
            if request.headers["X-User-ID"] != self.user or request.url.params["session_id"] != "scheduled":
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "user_id": self.user,
                    "agent_id": "agent-日程员工",
                    "session_id": "scheduled",
                    "schedule_id": "schedule-1",
                    "enabled": self.enabled,
                },
            )
        if path.startswith("/nexus/schedule-library/"):
            assert request.headers["X-User-ID"] == self.user
            if self.fail_install:
                return httpx.Response(503, json={"detail": "secret-token"})
            kind = path.rsplit("/", 1)[1]
            if kind == "skill" and self.fail_skill_by_id:
                return httpx.Response(
                    201, json={"added": [], "failed": {"ext-skill": "missing from library"}}
                )
            request = httpx.Request(
                request.method,
                str(request.url).replace(path, f"/workspace/{kind}/from-library"),
                headers=request.headers,
                content=request.content,
            )
        return super().handler(request)


@pytest.fixture
def scheduled(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_AGENTSCOPE_BASE_URL", "")
    monkeypatch.setenv("NEXUS_TASK_MCP_KEY", "test-task-secret")
    fake = ScheduledAgentScope()
    with TestClient(create_app()) as client:
        attach(client, fake)
        response = client.post(
            "/api/v1/employees",
            json=employee_body(
                name="日程员工", skills=["task-management", "ext-skill"], mcps=["ext-mcp"], tools=[]
            ),
        )
        assert response.status_code == 201, response.text
        employee = response.json()["employee"]
        with connect() as db:
            db.execute(
                text("UPDATE t_nexus_employees SET tools_json='[\"Read\"]' WHERE id=:i"),
                {"i": employee["id"]},
            )
            db.commit()
        fake.calls.clear()
        fake.uploads.clear()
        fake.library_skills.clear()
        fake.library_mcps.clear()
        yield client, fake, employee


def context(fake, **overrides):
    return {"user_id": fake.user, "agent_id": "agent-日程员工", "session_id": "scheduled", **overrides}


def test_prepare_without_private_is_idempotent_and_revokes(scheduled):
    client, fake, employee = scheduled
    path = "/api/v1/internal/schedule-sessions/prepare"
    first = client.post(path, json=context(fake))
    assert first.status_code == 200, first.text
    key = ("agent-日程员工", "scheduled")
    skills, mcps = fake.workspace(*key)
    assert {s["name"] for s in skills} == {"任务管理", "外部技能"}
    assert {m["name"] for m in mcps} == {"nexus-task", "外部 MCP"}
    task = next(m for m in mcps if m["name"] == "nexus-task")
    headers = task["mcp_config"]["headers"]
    assert headers["X-Nexus-Session-Id"] == "scheduled"
    assert headers["X-Nexus-Conversation-Id"] == ""
    assert headers["X-Nexus-Schedule-User-Id"] == fake.user
    assert headers["Authorization"] == "Bearer test-task-secret"
    assert client.post(path, json=context(fake)).status_code == 200
    assert len([u for u in fake.uploads if u["name"] == "任务管理"]) == 1
    assert fake.library_skills.count("ext-skill") == 1
    assert fake.library_mcps.count("ext-mcp") == 1
    assert all(
        uid == fake.user for _, p, uid in fake.calls if p.startswith(("/workspace/", "/sessions/", "/nexus/"))
    )
    assert all(uid == "nexus" for _, p, uid in fake.calls if p in {"/skill", "/mcp"})
    with connect() as db:
        assert db.execute(text("SELECT COUNT(*) FROM t_nexus_conversations")).scalar() == 0
        db.execute(
            text("UPDATE t_nexus_employees SET skills_json='[]',mcp_json='[]' WHERE id=:i"),
            {"i": employee["id"]},
        )
        db.commit()
    assert client.post(path, json=context(fake)).status_code == 200
    assert fake.workspace(*key) == ([], [])


def test_prepare_rejects_failure_running_owner_and_disabled(scheduled):
    client, fake, employee = scheduled
    path = "/api/v1/internal/schedule-sessions/prepare"
    fake.fail_install = True
    response = client.post(path, json=context(fake))
    assert response.status_code == 502, response.text
    assert "secret-token" not in response.text
    fake.fail_install = False
    assert client.post(path, json=context(fake, user_id=schedule_user_id("bob"))).status_code == 502
    fake.statuses["scheduled"] = "running"
    assert client.post(path, json=context(fake)).status_code == 409
    fake.statuses.clear()
    with connect() as db:
        db.execute(text("UPDATE t_nexus_employees SET enabled=0 WHERE id=:i"), {"i": employee["id"]})
        db.commit()
    assert client.post(path, json=context(fake)).status_code == 403
    assert client.post(path, json=context(fake, user_id="nexus")).status_code == 422
    assert client.post(path, json=context(fake), headers={"X-Nexus-Internal-Key": "wrong"}).status_code == 401


def test_policy_limits_tools_and_task_identity(scheduled):
    client, fake, employee = scheduled
    path = "/api/v1/internal/schedule-sessions/policy"
    response = client.get(path, params=context(fake))
    assert response.status_code == 200, response.text
    assert response.json()["employee_id"] == employee["id"]
    assert response.json()["task_allowed"] is True
    assert response.json()["initiator_id"] == fake.user
    assert client.get(path, params={**context(fake), "tool_name": "Bash"}).status_code == 403
    assert client.get(path, params={**context(fake), "tool_name": "Read"}).status_code == 200
    assert client.get(path, params={**context(fake), "tool_name": "Skill"}).status_code == 200
    task = {**context(fake), "tool_name": "mcp__nexus-task__nexus_task_list", "mcp_ref": "__task__"}
    assert client.get(path, params=task).status_code == 200
    with connect() as db:
        db.execute(text("UPDATE t_nexus_employees SET skills_json='[]' WHERE id=:i"), {"i": employee["id"]})
        db.commit()
    assert client.get(path, params=task).status_code == 403


def test_skill_install_failure_keyed_by_id_does_not_report_prepared(scheduled):
    client, fake, _ = scheduled
    fake.fail_skill_by_id = True
    response = client.post("/api/v1/internal/schedule-sessions/prepare", json=context(fake))
    assert response.status_code == 502, response.text
    skills, mcps = fake.workspace("agent-日程员工", "scheduled")
    assert not any(s["name"] == "任务管理" for s in skills)
    assert not any(m["name"] == "nexus-task" for m in mcps)


def test_local_skill_rename_retires_the_old_name_in_schedule_workspace(scheduled):
    client, fake, employee = scheduled
    created = client.post("/api/v1/config/skills", json={"content": skill_markdown("旧日程技能", "日程验证")})
    assert created.status_code == 201, created.text
    skill_id = created.json()["skill"]["id"]
    with connect() as db:
        db.execute(
            text("UPDATE t_nexus_employees SET skills_json=:s WHERE id=:i"),
            {"s": json.dumps(["task-management", skill_id]), "i": employee["id"]},
        )
        db.commit()
    path = "/api/v1/internal/schedule-sessions/prepare"
    assert client.post(path, json=context(fake)).status_code == 200
    renamed = client.patch(
        f"/api/v1/config/skills/{skill_id}", json={"content": skill_markdown("新日程技能", "日程验证")}
    )
    assert renamed.status_code == 200, renamed.text
    assert client.post(path, json=context(fake)).status_code == 200
    assert client.post(path, json=context(fake)).status_code == 200
    skills, _ = fake.workspace("agent-日程员工", "scheduled")
    assert {s["name"] for s in skills} == {"任务管理", "新日程技能"}

"""Nexus 自建 Skill/MCP、员工绑定与 AgentScope 同步。"""

from __future__ import annotations

import json
import re
import sqlite3

import httpx
import pytest
import yaml

from nexus_api.agentscope import AgentScopeClient
from nexus_api.main import create_app, db_path
from nexus_api.resources import mcp_registry_status
from tests.nexus.agent_transport import AgentChatTransport
from tests.nexus.clients import TestClient


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
        "mcps": [],
        "tools": [],
        "model_route_id": "route-test",
    }
    payload.update(overrides)
    return payload


def skill_markdown(name: str, description: str, body: str = "执行步骤") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n"


def skill_name(markdown: str) -> str:
    match = re.match(r"^---\s*\n(.*?)\n---", markdown, re.S)
    assert match
    return str((yaml.safe_load(match.group(1)) or {})["name"])


class FakeAgentScope:
    def __init__(self) -> None:
        self.skill_records = [
            {
                "id": "ext-skill",
                "name": "外部技能",
                "enabled": True,
                "version": "1.0",
                "description": "来自技能库",
            },
        ]
        self.mcp_records = [
            {"id": "ext-mcp", "name": "外部 MCP", "enabled": True, "description": "来自 MCP 库"},
        ]
        self.workspace_skills: dict[tuple[str, str], list[dict]] = {}
        self.workspace_mcps: dict[tuple[str, str], list[dict]] = {}
        self.statuses: dict[str, str] = {}
        self.uploads: list[dict] = []
        self.added_mcps: list[dict] = []
        self.library_skills: list[str] = []
        self.library_mcps: list[str] = []
        self.mcp_failure = ""
        self.registry_failure = False
        self.sessions = 0

    def workspace(self, agent_id: str, session_id: str) -> tuple[list[dict], list[dict]]:
        return (
            self.workspace_skills.setdefault((agent_id, session_id), []),
            self.workspace_mcps.setdefault((agent_id, session_id), []),
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        agent_id = request.url.params.get("agent_id", "")
        session_id = request.url.params.get("session_id", "")
        if path == "/agent/" and method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if method == "PATCH" and path.startswith("/agent/"):
            return httpx.Response(200, json={})
        if method == "DELETE" and path.startswith("/agent/"):
            return httpx.Response(204)
        if path == "/sessions/" and method == "POST":
            self.sessions += 1
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"ses-{self.sessions}-{body.get('name', '')}"})
        if path.endswith("/status") and method == "GET":
            session = path.split("/")[-2]
            return httpx.Response(
                200,
                json={"session_id": session, "status": self.statuses.get(session, "idle")},
            )
        if method == "PATCH" and path.startswith("/sessions/"):
            return httpx.Response(200, json={})
        if path == "/skill" and method == "GET":
            return httpx.Response(200, json=self.skill_records)
        if path.startswith("/skill/") and method == "GET":
            record = next((item for item in self.skill_records if item["id"] == path.split("/")[-1]), None)
            if record is None:
                return httpx.Response(404, json={"detail": "missing"})
            return httpx.Response(200, json={**record, "markdown": "# 外部技能\n\n正文"})
        if path == "/mcp" and method == "GET":
            records = [
                {k: v for k, v in item.items() if k not in {"mcp_config", "version"}}
                for item in self.mcp_records
            ]
            return httpx.Response(200, json=records)
        if path.startswith("/nexus/mcp-registry/"):
            assert request.headers["X-User-ID"] == "nexus"
            if self.registry_failure:
                return httpx.Response(503)
            name = path.rsplit("/", 1)[-1]
            if method == "DELETE":
                self.mcp_records = [item for item in self.mcp_records if item["name"] != name]
                return httpx.Response(204)
            body = json.loads(request.content)
            record = next((item for item in self.mcp_records if item["name"] == name), None)
            if record is None:
                record = {"id": f"registry-{name}", "name": name}
                self.mcp_records.append(record)
            record.update(
                enabled=body["enabled"], version=str(body["revision"]),
                mcp_config=body["client"]["mcp_config"],
            )
            return httpx.Response(200, json={"id": record["id"]})
        if path == "/workspace/skill" and method == "GET":
            skills, _ = self.workspace(agent_id, session_id)
            return httpx.Response(200, json=skills)
        if path == "/workspace/skill/upload" and method == "POST":
            manifest = None
            skill_md = ""
            # httpx stores multipart bodies; parse the manifest from the form data is unnecessary here.
            # The client sends SKILL.md content as the first file; detect its name.
            raw = request.content.decode("utf-8", "ignore")
            match = re.search(r"name: ([^\r\n\\\"]+)", raw)
            if match:
                name = match.group(1)
            else:
                name = "未命名技能"
            skill_md = raw
            skills, _ = self.workspace(agent_id, session_id)
            entry = {
                "name": name,
                "description": "",
                "dir": f"/workspace/skills/{name}",
                "markdown": skill_md,
                "updated_at": 1,
            }
            skills[:] = [item for item in skills if item["name"] != name]
            skills.append(entry)
            self.uploads.append(
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "name": name,
                    "manifest": manifest,
                }
            )
            return httpx.Response(201, json={})
        if path == "/workspace/skill/from-library" and method == "POST":
            body = json.loads(request.content.decode())
            skills, _ = self.workspace(agent_id, session_id)
            failed = {}
            for skill_id in body.get("skill_ids", []):
                record = next((item for item in self.skill_records if item["id"] == skill_id), None)
                if record is None:
                    failed[skill_id] = "missing"
                    continue
                self.library_skills.append(skill_id)
                if not any(item["name"] == record["name"] for item in skills):
                    skills.append(
                        {
                            "name": record["name"],
                            "description": record.get("description", ""),
                            "dir": f"/workspace/skills/{record['name']}",
                            "markdown": "# 外部技能\n\n正文",
                            "updated_at": 1,
                        }
                    )
            return httpx.Response(201, json={"added": list(body.get("skill_ids", [])), "failed": failed})
        if path == "/workspace/mcp" and method == "GET":
            _, mcps = self.workspace(agent_id, session_id)
            return httpx.Response(200, json=mcps)
        if path == "/nexus/mcp-probe" and method == "POST":
            body = json.loads(request.content.decode())
            _, mcps = self.workspace(agent_id, session_id)
            if any(item["name"] == body["name"] for item in mcps):
                return httpx.Response(409, json={"detail": "exists"})
            mcps.append(
                {
                    "name": body["name"],
                    "is_stateful": body.get("is_stateful", False),
                    "mcp_config": body.get("mcp_config", {}),
                    "is_healthy": not self.mcp_failure,
                    "tools": [] if self.mcp_failure else [{"name": "echo", "description": "回显"}],
                    "error": self.mcp_failure or None,
                }
            )
            self.added_mcps.append({"agent_id": agent_id, "session_id": session_id, "body": body})
            return httpx.Response(201, json={})
        if path == "/workspace/mcp/from-library" and method == "POST":
            body = json.loads(request.content.decode())
            _, mcps = self.workspace(agent_id, session_id)
            failed = {}
            for mcp_id in body.get("mcp_ids", []):
                record = next((item for item in self.mcp_records if item["id"] == mcp_id), None)
                if record is None:
                    failed[mcp_id] = "missing"
                    continue
                self.library_mcps.append(mcp_id)
                if not any(item["name"] == record["name"] for item in mcps):
                    mcps.append(
                        {
                            "name": record["name"],
                            "is_stateful": False,
                            "mcp_config": json.loads(json.dumps(record.get("mcp_config") or {
                                "type": "http_mcp", "url": "http://library/mcp", "headers": {}
                            })),
                            "is_healthy": True,
                            "tools": [{"name": "library_tool", "description": "库工具"}],
                        }
                    )
            return httpx.Response(201, json={"added": list(body.get("mcp_ids", [])), "failed": failed})
        if path.startswith("/workspace/mcp/") and method == "DELETE":
            name = path.split("/")[-1]
            _, mcps = self.workspace(agent_id, session_id)
            mcps[:] = [item for item in mcps if item["name"] != name]
            return httpx.Response(204)
        if path.startswith("/workspace/skill/") and method == "DELETE":
            name = path.split("/")[-1]
            skills, _ = self.workspace(agent_id, session_id)
            skills[:] = [item for item in skills if item["name"] != name]
            return httpx.Response(204)
        if path == "/chat/" and method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(200, json={"status": "started", "session_id": body["session_id"]})
        return httpx.Response(404, json={"detail": f"missing {method} {path}"})


def attach(test_client: TestClient, fake: FakeAgentScope) -> None:
    test_client.app.state.agentscope = AgentScopeClient(
        "http://agentscope.test",
        "nexus",
        httpx.AsyncClient(
            transport=AgentChatTransport(fake.handler),
            base_url="http://agentscope.test",
        ),
    )


@pytest.fixture()
def fake():
    return FakeAgentScope()


def test_skill_crud_versions_restore_and_restart(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        created = client.post(
            "/api/v1/config/skills",
            json={"content": skill_markdown("发布检查", "上线前检查", "第一版")},
        )
        assert created.status_code == 201, created.text
        skill = created.json()["skill"]
        skill_id = skill["id"]
        assert skill["source"] == "nexus"
        assert skill["version"] == 1
        assert skill["files"][0]["path"] == "SKILL.md"

        listed = client.get("/api/v1/config/skills").json()
        assert any(item["id"] == skill_id for item in listed["items"])
        public = client.get("/api/v1/skills").json()
        assert {"id": skill_id, "name": "发布检查"} in public["items"]
        assert all(item["id"] != "task-management" for item in public["items"])
        assert all(set(item) == {"id", "name"} for item in public["items"])

        edited = client.patch(
            f"/api/v1/config/skills/{skill_id}",
            json={"content": skill_markdown("发布检查", "上线前检查", "第二版")},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["skill"]["version"] == 2

        versions = client.get(f"/api/v1/config/skills/{skill_id}/versions").json()["versions"]
        assert [item["version"] for item in versions] == [2, 1]

        restored = client.post(f"/api/v1/config/skills/{skill_id}/versions/1/restore")
        assert restored.status_code == 200, restored.text
        assert restored.json()["skill"]["version"] == 3
        detail = client.get(f"/api/v1/config/skills/{skill_id}").json()["skill"]
        skill_md = next(item for item in detail["files"] if item["path"] == "SKILL.md")
        assert "第一版" in skill_md["content"]

    with TestClient(create_app()) as client:
        persisted = client.get(f"/api/v1/config/skills/{skill_id}")
        assert persisted.status_code == 200, persisted.text
        assert persisted.json()["skill"]["name"] == "发布检查"
        assert persisted.json()["skill"]["version"] == 3


def test_skill_upload_directory_and_validation(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        upload = client.post(
            "/api/v1/config/skills/upload",
            data={
                "paths": json.dumps(["包/SKILL.md", "包/references/check.md"]),
                "enabled": "true",
            },
            files=[
                ("files", ("SKILL.md", skill_markdown("目录技能", "带资源").encode(), "text/markdown")),
                ("files", ("check.md", b"checklist", "text/markdown")),
            ],
        )
        assert upload.status_code == 201, upload.text
        detail = client.get(f"/api/v1/config/skills/{upload.json()['skill']['id']}").json()["skill"]
        assert {item["path"] for item in detail["files"]} == {"SKILL.md", "references/check.md"}

        bad = client.post(
            "/api/v1/config/skills",
            json={"content": "# 没有 frontmatter"},
        )
        assert bad.status_code == 422
        assert "frontmatter" in bad.json()["detail"]


def test_skill_delete_is_blocked_while_bound_and_unbind_removes_workspace(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        skill = client.post(
            "/api/v1/config/skills",
            json={"content": skill_markdown("绑定技能", "被员工引用")},
        ).json()["skill"]
        employee = client.post(
            "/api/v1/employees",
            json=employee_body(skills=[skill["id"]]),
        ).json()["employee"]
        mailbox = employee["mailbox_session_id"]
        assert fake.workspace(employee["agentscope_agent_id"], mailbox)[0]

        blocked = client.delete(f"/api/v1/config/skills/{skill['id']}")
        assert blocked.status_code == 409
        assert "星河" in blocked.json()["detail"]

        updated = client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=[]),
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["sync"]["status"] == "applied"
        assert fake.workspace(employee["agentscope_agent_id"], mailbox)[0] == []

        removed = client.delete(f"/api/v1/config/skills/{skill['id']}")
        assert removed.status_code == 200


def test_mcp_secret_is_not_echoed_and_connection_test_returns_tools(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        created = client.post(
            "/api/v1/config/mcps",
            json={
                "name": "回显服务",
                "description": "测试 MCP",
                "url": "http://10.0.0.8:9000/mcp",
                "headers": [{"key": "X-Tenant", "value": "qa"}],
                "auth_type": "bearer",
                "token": "super-secret-token",
                "timeout": 12,
            },
        )
        assert created.status_code == 201, created.text
        mcp = created.json()["mcp"]
        assert mcp["has_token"] is True
        assert "super-secret-token" not in created.text

        listed = client.get("/api/v1/config/mcps")
        assert "super-secret-token" not in listed.text
        detail = client.get(f"/api/v1/config/mcps/{mcp['id']}")
        assert "super-secret-token" not in detail.text
        assert detail.json()["mcp"]["has_token"] is True

        tested = client.post(
            "/api/v1/config/mcps/test",
            json={
                "url": "http://10.0.0.8:9000/mcp",
                "auth_type": "bearer",
                "token": "one-off",
            },
        )
        assert tested.status_code == 200, tested.text
        assert tested.json()["ok"] is True
        assert tested.json()["tools"][0]["name"] == "echo"

        saved_test = client.post(f"/api/v1/config/mcps/{mcp['id']}/test")
        assert saved_test.status_code == 200, saved_test.text
        assert saved_test.json()["ok"] is True
        # 探测用的临时 MCP 不应留在 AgentScope 库或工作区里。
        assert fake.added_mcps
        assert not any(record["name"].startswith("nexus_probe_") for record in fake.mcp_records)

        updated = client.patch(
            f"/api/v1/config/mcps/{mcp['id']}",
            json={"url": "http://10.0.0.9:9000/mcp"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["mcp"]["has_token"] is True
        assert "super-secret-token" not in updated.text

        cleared = client.patch(
            f"/api/v1/config/mcps/{mcp['id']}",
            json={"token": ""},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["mcp"]["has_token"] is False


def test_mcp_error_does_not_echo_token(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        mcp = client.post(
            "/api/v1/config/mcps",
            json={
                "name": "错误回显",
                "url": "http://10.0.0.8:9000/mcp",
                "auth_type": "bearer",
                "token": "super-secret-token",
            },
        ).json()["mcp"]
        fake.mcp_failure = "auth failed for super-secret-token"
        tested = client.post(f"/api/v1/config/mcps/{mcp['id']}/test")
        assert tested.status_code == 200, tested.text
        assert tested.json()["ok"] is False
        assert "super-secret-token" not in tested.text
        assert "[redacted]" in tested.text


def test_employee_binds_mcp_and_unbind_removes_it(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        mcp = client.post(
            "/api/v1/config/mcps",
            json={"name": "私有 MCP", "url": "http://10.1.2.3:8080/mcp"},
        ).json()["mcp"]
        employee = client.post(
            "/api/v1/employees",
            json=employee_body(mcps=[mcp["id"]]),
        ).json()["employee"]
        agent_id = employee["agentscope_agent_id"]
        mailbox = employee["mailbox_session_id"]
        assert [item["name"] for item in fake.workspace(agent_id, mailbox)[1]] == [mcp["runtime_name"]]

        blocked = client.delete(f"/api/v1/config/mcps/{mcp['id']}")
        assert blocked.status_code == 409

        headers = {"X-Owner-Id": "browser-a"}
        conversation = client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers=headers,
            json={"name": "私聊"},
        ).json()
        sent = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=headers,
            json={"client_id": "test-send-479", "text": "你好"},
        )
        assert sent.status_code == 200, sent.text
        session_id = sent.json()["agentscope"]["session_id"]
        assert {item["name"] for item in fake.workspace(agent_id, session_id)[1]} == {
            mcp["runtime_name"],
        }

        updated = client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(mcps=[]),
        )
        assert updated.status_code == 200, updated.text
        assert fake.workspace(agent_id, session_id)[1] == []
        assert client.delete(f"/api/v1/config/mcps/{mcp['id']}").status_code == 200


def test_external_resources_bind_from_library(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        catalog = client.get("/api/v1/config/skills").json()
        assert any(item["id"] == "ext-skill" and item["source"] == "agentscope" for item in catalog["items"])
        mcps = client.get("/api/v1/config/mcps").json()
        assert any(item["id"] == "ext-mcp" and item["source"] == "agentscope" for item in mcps["items"])

        employee = client.post(
            "/api/v1/employees",
            json=employee_body(skills=["ext-skill"], mcps=["ext-mcp"]),
        ).json()["employee"]
        agent_id = employee["agentscope_agent_id"]
        mailbox = employee["mailbox_session_id"]
        names = {item["name"] for item in fake.workspace(agent_id, mailbox)[0]}
        assert "外部技能" in names
        assert fake.library_skills == ["ext-skill"]
        assert fake.library_mcps == ["ext-mcp"]


def test_disabled_resource_is_removed_and_reenabled_restores(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        skill = client.post(
            "/api/v1/config/skills",
            json={"content": skill_markdown("停用技能", "停用测试")},
        ).json()["skill"]
        employee = client.post(
            "/api/v1/employees",
            json=employee_body(skills=[skill["id"]]),
        ).json()["employee"]
        agent_id = employee["agentscope_agent_id"]
        mailbox = employee["mailbox_session_id"]

        disabled = client.patch(
            f"/api/v1/config/skills/{skill['id']}",
            json={"enabled": False},
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["sync"]["status"] == "applied"
        assert fake.workspace(agent_id, mailbox)[0] == []
        listed = client.get("/api/v1/config/skills").json()
        item = next(item for item in listed["items"] if item["id"] == skill["id"])
        assert item["sync"]["status"] == "disabled"

        enabled = client.patch(
            f"/api/v1/config/skills/{skill['id']}",
            json={"enabled": True},
        )
        assert enabled.status_code == 200, enabled.text
        assert {item["name"] for item in fake.workspace(agent_id, mailbox)[0]} == {"停用技能"}


def test_running_session_is_left_alone_and_retry_reports_pending(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        skill = client.post(
            "/api/v1/config/skills",
            json={"content": skill_markdown("运行技能", "运行中不拆改")},
        ).json()["skill"]
        employee = client.post(
            "/api/v1/employees",
            json=employee_body(skills=[skill["id"]]),
        ).json()["employee"]
        fake.statuses[employee["mailbox_session_id"]] = "running"
        edited = client.patch(
            f"/api/v1/config/skills/{skill['id']}",
            json={"content": skill_markdown("运行技能", "运行中不拆改", "新版本")},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["sync"]["status"] == "pending"
        item = next(
            item for item in client.get("/api/v1/config/skills").json()["items"] if item["id"] == skill["id"]
        )
        assert item["sync"]["status"] == "pending"
        retry = client.post(f"/api/v1/config/skills/{skill['id']}/sync")
        assert retry.json()["sync"]["status"] == "pending"


def test_agentscope_unavailable_keeps_config_pending(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        skill = client.post(
            "/api/v1/config/skills",
            json={"content": skill_markdown("离线技能", "AgentScope 不可用")},
        ).json()["skill"]
        employee = client.post(
            "/api/v1/employees",
            json=employee_body(skills=[skill["id"]]),
        ).json()["employee"]
        client.app.state.agentscope = AgentScopeClient(None, "nexus")
        edited = client.patch(
            f"/api/v1/config/skills/{skill['id']}",
            json={"content": skill_markdown("离线技能", "AgentScope 不可用", "仍保存")},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["sync"]["status"] == "pending"
        detail = client.get(f"/api/v1/config/skills/{skill['id']}").json()["skill"]
        assert "仍保存" in detail["files"][0]["content"]
        connection = sqlite3.connect(db_path())
        row = connection.execute(
            "SELECT status FROM t_nexus_resource_sync WHERE resource_type='skill' AND resource_id=?",
            (skill["id"],),
        ).fetchone()
        connection.close()
        assert row is not None and row[0] == "pending"
        assert employee["id"]


def test_unbind_while_running_is_retried_by_the_next_sync(tmp_path, monkeypatch, fake):
    """运行中解绑只记 pending 时，下一次同步也必须能收回（不能依赖 previous）。"""

    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        skill = client.post(
            "/api/v1/config/skills",
            json={"content": skill_markdown("待收回", "运行中解绑")},
        ).json()["skill"]
        employee = client.post(
            "/api/v1/employees",
            json=employee_body(skills=[skill["id"]]),
        ).json()["employee"]
        agent_id = employee["agentscope_agent_id"]
        mailbox = employee["mailbox_session_id"]
        assert [item["name"] for item in fake.workspace(agent_id, mailbox)[0]] == ["待收回"]

        fake.statuses[mailbox] = "running"
        unbound = client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=[]),
        )
        assert unbound.status_code == 200, unbound.text
        assert unbound.json()["sync"]["status"] == "pending"
        assert [item["name"] for item in fake.workspace(agent_id, mailbox)[0]] == ["待收回"]

        fake.statuses[mailbox] = "idle"
        retried = client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=[]),
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["sync"]["status"] == "applied"
        assert fake.workspace(agent_id, mailbox)[0] == []

        connection = sqlite3.connect(db_path())
        rows = connection.execute(
            "SELECT resource_id FROM t_nexus_resource_sync WHERE status = 'pending_remove'"
        ).fetchall()
        connection.close()
        assert rows == []


def test_renaming_a_skill_retires_the_old_workspace_name(tmp_path, monkeypatch, fake):
    """改名后旧运行时名必须收回；否则新旧两版会同时留在工作区。"""

    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        skill = client.post(
            "/api/v1/config/skills",
            json={"content": skill_markdown("旧名字", "第一版")},
        ).json()["skill"]
        employee = client.post(
            "/api/v1/employees",
            json=employee_body(skills=[skill["id"]]),
        ).json()["employee"]
        agent_id = employee["agentscope_agent_id"]
        mailbox = employee["mailbox_session_id"]
        assert [item["name"] for item in fake.workspace(agent_id, mailbox)[0]] == ["旧名字"]

        renamed = client.patch(
            f"/api/v1/config/skills/{skill['id']}",
            json={"content": skill_markdown("新名字", "第二版")},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["sync"]["status"] == "applied"
        assert [item["name"] for item in fake.workspace(agent_id, mailbox)[0]] == ["新名字"]

        unbound = client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=[]),
        )
        assert unbound.status_code == 200, unbound.text
        assert fake.workspace(agent_id, mailbox)[0] == []


def test_idle_unbind_failure_keeps_the_removal_intent(tmp_path, monkeypatch, fake):
    """idle 时删除失败也要留下待办；下一次同步不能只显示 applied 却没收回。"""

    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    original = fake.handler
    state = {"fail_skill": True}

    def flaky(request: httpx.Request) -> httpx.Response:
        if (
            state["fail_skill"]
            and request.method == "DELETE"
            and request.url.path.endswith("/workspace/skill/失败收回")
        ):
            return httpx.Response(503, json={"detail": "临时不可用"})
        return original(request)

    fake.handler = flaky
    with TestClient(create_app()) as client:
        attach(client, fake)
        skill = client.post(
            "/api/v1/config/skills",
            json={"content": skill_markdown("失败收回", "删除一次失败")},
        ).json()["skill"]
        employee = client.post(
            "/api/v1/employees",
            json=employee_body(skills=[skill["id"]]),
        ).json()["employee"]
        agent_id = employee["agentscope_agent_id"]
        mailbox = employee["mailbox_session_id"]
        assert [item["name"] for item in fake.workspace(agent_id, mailbox)[0]] == ["失败收回"]

        failed = client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=[]),
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["sync"]["status"] == "error"
        assert [item["name"] for item in fake.workspace(agent_id, mailbox)[0]] == ["失败收回"]

        connection = sqlite3.connect(db_path())
        pending = connection.execute(
            "SELECT detail FROM t_nexus_resource_sync WHERE status = 'pending_remove'"
        ).fetchall()
        connection.close()
        assert pending == [("失败收回",)]

        state["fail_skill"] = False
        retried = client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=[]),
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["sync"]["status"] == "applied"
        assert fake.workspace(agent_id, mailbox)[0] == []


def test_renaming_a_skill_retires_suffixed_installs(tmp_path, monkeypatch, fake):
    """上游实际名带 “ (2)” 后缀时，改名也要按原始名收回全部匹配。"""

    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        skill = client.post(
            "/api/v1/config/skills",
            json={"content": skill_markdown("旧名", "第一版")},
        ).json()["skill"]
        employee = client.post(
            "/api/v1/employees",
            json=employee_body(skills=[skill["id"]]),
        ).json()["employee"]
        agent_id = employee["agentscope_agent_id"]
        mailbox = employee["mailbox_session_id"]
        skills, _ = fake.workspace(agent_id, mailbox)
        skills.append(
            {
                "name": "旧名 (2)",
                "description": "",
                "dir": "/workspace/skills/旧名 (2)",
                "markdown": "# 旧名\n",
                "updated_at": 1,
            }
        )

        renamed = client.patch(
            f"/api/v1/config/skills/{skill['id']}",
            json={"content": skill_markdown("新名", "第二版")},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["sync"]["status"] == "applied"
        assert sorted(item["name"] for item in fake.workspace(agent_id, mailbox)[0]) == ["新名"]


def test_unbind_intent_survives_a_workspace_read_failure(tmp_path, monkeypatch, fake):
    """解绑后第一次同步就读不到工作区：待办必须已经提交，下一次仍能收回。"""

    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    original = fake.handler
    state = {"fail_read": False}

    def flaky(request: httpx.Request) -> httpx.Response:
        if state["fail_read"] and request.method == "GET" and request.url.path == "/workspace/skill":
            return httpx.Response(503, json={"detail": "工作区暂不可用"})
        return original(request)

    fake.handler = flaky
    with TestClient(create_app()) as client:
        attach(client, fake)
        skill = client.post(
            "/api/v1/config/skills",
            json={"content": skill_markdown("读取失败", "第一次读不到工作区")},
        ).json()["skill"]
        employee = client.post(
            "/api/v1/employees",
            json=employee_body(skills=[skill["id"]]),
        ).json()["employee"]
        agent_id = employee["agentscope_agent_id"]
        mailbox = employee["mailbox_session_id"]
        assert [item["name"] for item in fake.workspace(agent_id, mailbox)[0]] == ["读取失败"]

        state["fail_read"] = True
        failed = client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=[]),
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["sync"]["status"] == "error"
        assert [item["name"] for item in fake.workspace(agent_id, mailbox)[0]] == ["读取失败"]

        connection = sqlite3.connect(db_path())
        pending = connection.execute(
            "SELECT detail FROM t_nexus_resource_sync WHERE status = 'pending_remove'"
        ).fetchall()
        connection.close()
        assert pending == [("读取失败",)]

        state["fail_read"] = False
        retried = client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=[]),
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["sync"]["status"] == "applied"
        assert fake.workspace(agent_id, mailbox)[0] == []


def test_unbind_intent_survives_a_sync_crash(tmp_path, monkeypatch, fake):
    """绑定已提交、同步没跑完（相当于进程退出）：待办仍在库里，重试能收回。"""

    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        skill = client.post(
            "/api/v1/config/skills",
            json={"content": skill_markdown("崩溃收回", "同步中途退出")},
        ).json()["skill"]
        employee = client.post(
            "/api/v1/employees",
            json=employee_body(skills=[skill["id"]]),
        ).json()["employee"]
        agent_id = employee["agentscope_agent_id"]
        mailbox = employee["mailbox_session_id"]

        async def crash(*_args, **_kwargs):
            raise RuntimeError("模拟同步中途进程退出")

        with monkeypatch.context() as patch:
            patch.setattr("nexus_api.main.sync_employee_resources", crash)
            with pytest.raises(RuntimeError):
                client.patch(
                    f"/api/v1/employees/{employee['id']}",
                    json=employee_body(skills=[]),
                )

        connection = sqlite3.connect(db_path())
        stored = json.loads(
            connection.execute(
                "SELECT skills_json FROM t_nexus_employees WHERE id = ?", (employee["id"],)
            ).fetchone()[0]
        )
        pending = connection.execute(
            "SELECT detail FROM t_nexus_resource_sync WHERE status = 'pending_remove'"
        ).fetchall()
        connection.close()
        assert stored == []
        assert pending == [("崩溃收回",)]

        retried = client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=[]),
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["sync"]["status"] == "applied"
        assert fake.workspace(agent_id, mailbox)[0] == []


@pytest.mark.parametrize("condition,expected", [("failed", "error"), ("running", "pending")])
def test_employee_save_persists_final_resource_sync_snapshot(
    tmp_path, monkeypatch, fake, condition, expected
):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        attach(client, fake)
        employee = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        original_revision = employee["capability"]["applied_revision"]
        conversation = client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers={"X-Owner-Id": "owner-a"},
            json={"name": "私聊"},
        ).json()
        sent = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers={"X-Owner-Id": "owner-a"},
            json={"client_id": "test-send-899", "text": "初始化测试会话"},
        )
        assert sent.status_code == 200, sent.text
        session_id = sent.json()["agentscope"]["session_id"]
        original_handler = fake.handler

        def fail_private(request):
            if (
                condition == "failed"
                and request.url.path == "/workspace/mcp/from-library"
                and request.url.params.get("session_id") == session_id
            ):
                return httpx.Response(503, json={"detail": "private workspace unavailable"})
            return original_handler(request)

        if condition == "running":
            fake.statuses[session_id] = "running"
        fake.handler = fail_private
        attach(client, fake)
        response = client.patch(f"/api/v1/employees/{employee['id']}", json=employee_body(mcps=["ext-mcp"]))
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["sync"]["status"] == expected, result
        saved = result["employee"]["capability"]
        assert saved["runtime_status"] == expected, saved
        assert saved["applied_revision"] == original_revision
        current = next(
            item for item in client.get("/api/v1/employees").json()["items"] if item["id"] == employee["id"]
        )
        assert current["capability"] == saved


@pytest.mark.parametrize(
    "records,enabled,expected",
    [
        (None, True, "error"),
        ({}, True, "pending"),
        ({"mcp-test": {"enabled": True}}, True, "applied"),
        ({"mcp-test": {}}, True, "applied"),
        ({"mcp-test": {"enabled": False}}, False, "applied"),
        ({"mcp-test": {"enabled": False}}, True, "pending"),
        ({"mcp-test": {}}, False, "pending"),
    ],
)
def test_mcp_registry_status_without_version(records, enabled, expected):
    row = {"runtime_name": "mcp-test", "revision": 2, "enabled": enabled}
    assert mcp_registry_status(row, records)["status"] == expected


def test_unbound_mcp_register_retry_update_and_delete(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    base = "/api/v1/config/mcps"
    with TestClient(create_app()) as client:
        attach(client, fake)
        fake.registry_failure = True
        response = client.post(base, json={"name": "未绑定资源", "url": "http://mcp.test/mcp"})
        assert response.status_code == 201
        assert response.json()["sync"]["status"] == "error"
        mcp = response.json()["mcp"]
        path = f"{base}/{mcp['id']}"
        name = mcp["runtime_name"]
        assert mcp["employees"] == []
        assert client.get(path).json()["mcp"]["registry"]["status"] == "pending"

        fake.registry_failure = False
        assert client.post(f"{path}/sync").json()["sync"]["ok"] is True
        assert client.post(f"{path}/sync").json()["sync"]["ok"] is True
        [record] = [row for row in fake.mcp_records if row["name"] == name]
        before = record["id"]
        assert not fake.workspace_mcps

        updated = client.patch(path, json={"url": "http://new.test/mcp", "enabled": False}).json()
        assert updated["sync"]["ok"] is True
        assert record["id"] == before and not record["enabled"]
        assert record["mcp_config"]["url"] == "http://new.test/mcp"
        assert client.get(path).json()["mcp"]["registry"]["status"] == "applied"

        fake.registry_failure = True
        assert client.delete(path).status_code == 502
        assert client.get(path).status_code == 200
        fake.registry_failure = False
        assert client.delete(path).status_code == 200
        assert not any(row["name"] == name for row in fake.mcp_records)


def test_all_bound_mcps_are_installed_per_private_session(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake.mcp_records.append({"id": "second-mcp", "name": "第二个 MCP", "enabled": True})
    original, chats = fake.handler, []

    def handler(request):
        if request.url.path == "/chat/":
            body = json.loads(request.content)
            installed = fake.workspace(body["agent_id"], body["session_id"])[1]
            assert {item["name"] for item in installed} == {"外部 MCP", "第二个 MCP"}
            chats.append(body)
        return original(request)

    fake.handler = handler
    with TestClient(create_app()) as client:
        attach(client, fake)
        employee = client.post(
            "/api/v1/employees", json=employee_body(mcps=["ext-mcp", "second-mcp"]),
        ).json()["employee"]
        sessions = []
        for owner in ("browser-a", "browser-b"):
            headers = {"X-Owner-Id": owner}
            conversation = client.post(
                f"/api/v1/employees/{employee['id']}/conversations", headers=headers, json={},
            ).json()
            path = f"/api/v1/conversations/{conversation['id']}/messages"
            response = client.post(
                path, headers=headers, json={"text": "首次运行", "client_id": "resource-1"},
            )
            assert response.status_code == 200, response.text
            sid = response.json()["agentscope"]["session_id"]
            sessions.append(sid)
            fake.workspace(employee["agentscope_agent_id"], sid)[1].clear()
            response = client.post(
                path, headers=headers, json={"text": "继续运行", "client_id": "resource-2"},
            )
            assert response.status_code == 200, response.text
        assert len(set(sessions)) == 2 and len(chats) == 4


@pytest.mark.parametrize("fault", ["missing", "unhealthy", "permission", "status", "catalog"])
def test_mcp_partial_mount_blocks_chat_and_recovers_same_session(tmp_path, monkeypatch, fake, fault):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake.mcp_records.append({"id": "second-mcp", "name": "第二个 MCP", "enabled": True})
    original, chats, broken = fake.handler, [], False

    def handler(request):
        if request.url.path == "/chat/":
            chats.append(json.loads(request.content))
        if broken:
            if fault == "permission" and request.method == "PATCH":
                if json.loads(request.content).get("permission_mode") == "bypass":
                    return httpx.Response(503, json={"detail": "permission update unavailable"})
            if fault == "status" and request.url.path.endswith("/status"):
                return httpx.Response(503, json={"detail": "native status unavailable"})
            if fault == "catalog" and request.url.path == "/mcp":
                return httpx.Response(200, json=[fake.mcp_records[0]])
        response = original(request)
        if broken and request.url.path == "/workspace/mcp/from-library":
            _, mcps = fake.workspace(
                request.url.params["agent_id"], request.url.params["session_id"],
            )
            if fault == "missing":
                mcps[:] = [item for item in mcps if item["name"] != "第二个 MCP"]
            elif fault == "unhealthy":
                for item in mcps:
                    if item["name"] == "第二个 MCP":
                        item["is_healthy"] = False
        return response

    fake.handler = handler
    with TestClient(create_app()) as client:
        attach(client, fake)
        employee = client.post(
            "/api/v1/employees", json=employee_body(mcps=["ext-mcp", "second-mcp"]),
        ).json()["employee"]
        headers = {"X-Owner-Id": "browser-a"}
        conversation = client.post(
            f"/api/v1/employees/{employee['id']}/conversations", headers=headers, json={},
        ).json()
        path = f"/api/v1/conversations/{conversation['id']}/messages"
        first = client.post(
            path, headers=headers, json={"text": "首次运行", "client_id": "resource-1"},
        )
        assert first.status_code == 200, first.text
        sid = first.json()["agentscope"]["session_id"]
        workspace = fake.workspace(employee["agentscope_agent_id"], sid)[1]
        # Force repair. An applied revision alone cannot mask an unhealthy mount.
        workspace[-1]["is_healthy"] = False
        broken = True
        refused = client.post(
            path, headers=headers, json={"text": "缺少工具时不要运行", "client_id": "resource-3"},
        )
        assert refused.status_code == 503, refused.text
        assert "资源未就绪" in refused.json()["detail"]
        assert len(chats) == 1
        broken = False
        recovered = client.post(
            path, headers=headers, json={"text": "恢复后继续", "client_id": "resource-4"},
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["agentscope"]["session_id"] == sid
        assert len(chats) == 2
        assert {item["name"] for item in workspace} == {"外部 MCP", "第二个 MCP"}
        assert all(item["is_healthy"] for item in workspace)


def test_peer_mailbox_mount_failure_blocks_delivery_and_preserves_mcp_permission(
    tmp_path, monkeypatch, fake,
):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    original, modes, broken = fake.handler, {}, False

    def handler(request):
        if request.method == "PATCH" and request.url.path.startswith("/sessions/"):
            modes[request.url.path.removeprefix("/sessions/")] = json.loads(request.content).get(
                "permission_mode",
            )
        if broken and request.url.path == "/workspace/mcp":
            return httpx.Response(503, json={"detail": "workspace unavailable"})
        return original(request)

    fake.handler = handler
    with TestClient(create_app()) as client:
        attach(client, fake)
        sender = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        target = client.post(
            "/api/v1/employees", json=employee_body(name="知微", domain="防线运营人员", mcps=["ext-mcp"]),
        ).json()["employee"]
        headers = {"X-Owner-Id": "browser-a"}
        conversation = client.post(
            f"/api/v1/employees/{sender['id']}/conversations", headers=headers, json={},
        ).json()
        private = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages", headers=headers,
            json={"client_id": "test-send-1118", "text": "开始"},
        ).json()["agentscope"]["session_id"]
        for origin in (sender["mailbox_session_id"], private):
            params = {"session_id": origin, "name": "知微", "message": "协作请求"}
            broken = True
            refused = client.get("/api/v1/peers/resolve", params=params)
            assert refused.status_code == 503, refused.text
            broken = False
            accepted = client.get("/api/v1/peers/resolve", params=params)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["session_id"] == target["mailbox_session_id"]
            assert modes[target["mailbox_session_id"]] == "bypass"

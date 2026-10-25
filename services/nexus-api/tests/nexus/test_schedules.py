"""员工/身份适配的权限边界；官方机制另由 Agent Service 真实 SQL 测试覆盖。"""

import copy

import httpx
import pytest
from sqlalchemy import text

from nexus_api.agentscope import AgentScopeError
from nexus_api.db import connect
from nexus_api.main import create_app
from nexus_api.schedules import schedule_user_id
from tests.nexus.clients import TestClient


@pytest.fixture
def schedules(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_AGENTSCOPE_BASE_URL", "")
    app = create_app()
    with TestClient(app) as client:
        employee = client.post(
            "/api/v1/employees",
            json={
                "name": "日程员工",
                "domain": "运营",
                "description": "日程",
                "instructions": "执行安排",
                "model_route_id": "route-test",
            },
        ).json()["employee"]
        with connect() as db:
            db.execute(
                text("UPDATE t_nexus_employees SET agentscope_agent_id='agent-1' WHERE id=:id"),
                {"id": employee["id"]},
            )
            db.commit()
        store, calls = {}, []

        async def request(method, path, **kwargs):
            uid = kwargs["headers"]["X-User-ID"]
            calls.append((uid, method, path, kwargs))
            rows = store.setdefault(uid, {})
            if path == "/schedule/":
                if method == "GET":
                    return httpx.Response(200, json={"schedules": list(rows.values())})
                payload = kwargs["json"]
                if payload["timezone"] == "invalid":
                    raise AgentScopeError("invalid timezone", 422)
                sid = f"schedule-{len(rows)}"
                rows[sid] = {
                    "id": sid,
                    "agent_id": payload["agent_id"],
                    "created_at": "2026-10-23T00:00:00Z",
                    "data": copy.deepcopy(payload),
                }
                return httpx.Response(201, json={"schedule_id": sid})
            if path.startswith("/sessions/"):
                return httpx.Response(
                    200,
                    json={
                        "messages": [{"id": "m", "content": "done"}],
                        "has_more": False,
                        "is_running": False,
                    },
                )
            sid = path.split("/")[2]
            if sid not in rows:
                raise AgentScopeError("not found", 404)
            if method == "PATCH":
                rows[sid]["data"].update(kwargs["json"])
                return httpx.Response(200, json=rows[sid])
            if method == "DELETE":
                del rows[sid]
                return httpx.Response(204)
            if path.endswith("/sessions"):
                return httpx.Response(
                    200,
                    json={
                        "sessions": [
                            {"id": "session-1", "created_at": "today", "state": {"private": "hidden"}}
                        ]
                    },
                )
            if path.startswith("/sessions/"):
                return httpx.Response(
                    200,
                    json={
                        "messages": [{"id": "m", "content": "done"}],
                        "has_more": False,
                        "is_running": False,
                    },
                )
            raise AssertionError(path)

        monkeypatch.setattr(app.state.agentscope, "request", request)
        app.state.agentscope.base_url = "http://agentscope.test"
        yield client, employee, store, calls


def body(employee, **overrides):
    return {
        "employee_id": employee["id"],
        "name": "晨报",
        "description": "输出日报",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        **overrides,
    }


def test_two_owners_and_untrusted_identity(schedules):
    client, employee, store, calls = schedules
    a = {"X-Verified-UID": "bob", "X-Owner-Id": "alice", "X-User-ID": "injected"}
    b = {"X-Owner-Id": "bob"}
    created = client.post("/api/v1/schedules?uid=bob", headers=a, json=body(employee))
    assert created.status_code == 201, created.text
    sid = created.json()["schedule_id"]
    assert list(store) == [schedule_user_id("alice")]
    sent = calls[-1][3]["json"]
    assert sent["chat_model_config"]["credential_id"] == "cred-test"
    assert sent["permission_mode"] == "dont_ask"
    assert client.get("/api/v1/schedules", headers=b).json()["total"] == 0
    for method, path, payload in [
        ("PATCH", f"/{sid}", {"enabled": False}),
        ("DELETE", f"/{sid}", None),
        ("GET", f"/{sid}/sessions", None),
        ("GET", f"/{sid}/sessions/session-1/messages", None),
    ]:
        assert client.request(method, "/api/v1/schedules" + path, headers=b, json=payload).status_code == 404
    row = client.get("/api/v1/schedules", headers=a).json()["items"][0]
    assert row["owner_uid"] == "alice"
    assert "chat_model_config" not in row["data"]
    assert client.post("/api/v1/schedules", headers=b, json=body(employee)).status_code == 201
    assert len(store) == 2


def test_lifecycle_and_sessions(schedules):
    client, employee, _, calls = schedules
    h = {"X-Owner-Id": "alice"}
    sid = client.post("/api/v1/schedules", headers=h, json=body(employee)).json()["schedule_id"]
    path = f"/api/v1/schedules/{sid}"
    assert client.patch(path, headers=h, json={"enabled": False}).json()["data"]["enabled"] is False
    update = body(employee, name="晚报", cron_expression="0 18 * * *", stateful=True)
    del update["employee_id"]
    assert client.put(path, headers=h, json=update).json()["data"]["name"] == "晚报"
    sessions = client.get(path + "/sessions", headers=h).json()
    assert sessions["items"] == [{"id": "session-1", "created_at": "today"}]
    # 历史内容必须属于这个日程，不能猜测任意会话。
    assert client.get(path + "/sessions/foreign/messages", headers=h).status_code == 404
    assert not any(call[2] == "/sessions/foreign/messages" for call in calls)
    assert client.get(path + "/sessions/session-1/messages", headers=h).status_code == 200
    assert client.delete(path, headers=h).status_code == 204
    assert client.get("/api/v1/schedules", headers=h).json()["total"] == 0


def test_validation_failures_and_paging(schedules, monkeypatch):
    client, employee, _, _ = schedules
    h = {"X-Owner-Id": "alice"}
    assert client.get("/api/v1/schedules", headers={"Cookie": "nexus_session=invalid"}).status_code == 401
    for extra in [{"owner_uid": "bob"}, {"user_id": "bob"}, {"chat_model_config": {}}, {"name": "   "}]:
        assert client.post("/api/v1/schedules", headers=h, json=body(employee, **extra)).status_code == 422
    assert (
        client.post("/api/v1/schedules", headers=h, json=body(employee, timezone="invalid")).status_code
        == 422
    )
    for index in range(3):
        assert (
            client.post("/api/v1/schedules", headers=h, json=body(employee, name=f"晨报{index}")).status_code
            == 201
        )
    page = client.get("/api/v1/schedules?page=2&page_size=2", headers=h).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert page["employee_options"] == [{"id": employee["id"], "name": employee["name"]}]
    result = client.get("/api/v1/schedules?q=晨报1&enabled=true", headers=h).json()
    assert result["total"] == 1 and result["items"][0]["data"]["name"] == "晨报1"
    assert client.get("/api/v1/schedules?employee_id=other", headers=h).json()["total"] == 0
    assert client.get("/api/v1/schedules?enabled=false", headers=h).json()["total"] == 0
    with connect() as db:
        db.execute(text("UPDATE t_nexus_employees SET enabled=0"))
        db.commit()
    assert client.post("/api/v1/schedules", headers=h, json=body(employee)).status_code == 409
    assert client.patch("/api/v1/schedules/schedule-0", headers=h, json={"enabled": False}).status_code == 200
    assert client.patch("/api/v1/schedules/schedule-0", headers=h, json={"enabled": True}).status_code == 409

    async def unavailable(*args, **kwargs):
        raise AgentScopeError("offline")

    monkeypatch.setattr(client.app.state.agentscope, "request", unavailable)
    assert client.get("/api/v1/schedules", headers=h).status_code == 502


def test_internal_schedule_is_scoped_to_private_session_owner(schedules, monkeypatch):
    client, employee, store, calls = schedules
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "internal-test")
    with connect() as db:
        db.execute(
            text(
                """
                INSERT INTO t_nexus_conversations
                    (id, employee_id, owner_id, kind, name, agentscope_session_id,
                     workspace_id, created_at, updated_at)
                VALUES
                    ('conv-internal', :employee_id, 'alice', 'private', '私聊',
                     'ses-private', 'ws-internal', '2026-10-23T00:00:00Z',
                     '2026-10-23T00:00:00Z')
                """
            ),
            {"employee_id": employee["id"]},
        )
        db.execute(
            text(
                """
                INSERT INTO t_nexus_conversations
                    (id, employee_id, owner_id, kind, name, agentscope_session_id,
                     workspace_id, created_at, updated_at)
                VALUES
                    ('conv-team', :employee_id, 'alice', 'team', '团队',
                     'ses-team', 'ws-team', '2026-10-23T00:00:00Z',
                     '2026-10-23T00:00:00Z')
                """
            ),
            {"employee_id": employee["id"]},
        )
        db.commit()

    headers = {
        "X-Nexus-Internal-Key": "internal-test",
        "X-Nexus-Session-Id": "ses-private",
        "X-Nexus-Employee-Id": employee["id"],
    }
    created = client.post(
        "/api/v1/internal/schedules",
        headers=headers,
        json={
            "name": "早报",
            "description": "每个工作日整理当天重点",
            "cron_expression": "0 9 * * 1-5",
            "timezone": "Asia/Shanghai",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["enabled"] is False
    sid = created.json()["id"]
    assert list(store) == [schedule_user_id("alice")]
    sent = calls[-1][3]
    assert calls[-1][0] == schedule_user_id("alice")
    assert sent["json"]["agent_id"] == "agent-1"
    assert sent["json"]["chat_model_config"]["credential_id"] == "cred-test"
    assert sent["json"]["enabled"] is False

    listing = client.get("/api/v1/internal/schedules", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["id"] == sid
    view = client.get(f"/api/v1/internal/schedules/{sid}", headers=headers)
    assert view.status_code == 200
    assert view.json()["data"]["name"] == "早报"

    missing = dict(headers)
    missing["X-Nexus-Internal-Key"] = "wrong"
    assert client.get("/api/v1/internal/schedules", headers=missing).status_code == 401
    unknown = dict(headers)
    unknown["X-Nexus-Session-Id"] = "ses-missing"
    assert client.get("/api/v1/internal/schedules", headers=unknown).status_code == 404
    team = dict(headers)
    team["X-Nexus-Session-Id"] = "ses-team"
    assert client.get("/api/v1/internal/schedules", headers=team).status_code == 404
    wrong_employee = dict(headers)
    wrong_employee["X-Nexus-Employee-Id"] = "other"
    assert client.get("/api/v1/internal/schedules", headers=wrong_employee).status_code == 409

    with connect() as db:
        db.execute(
            text("UPDATE t_nexus_employees SET enabled = 0 WHERE id = :p0"),
            {"p0": employee["id"]},
        )
        db.commit()
    assert client.get("/api/v1/internal/schedules", headers=headers).status_code == 409

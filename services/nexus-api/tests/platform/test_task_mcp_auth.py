import httpx
import pytest
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config.settings import Settings
from app.task_mcp import NexusIdentityMiddleware, authenticate_mcp
from app.task_tools import nexus_employee_id


def test_authenticate_rejects_missing_and_forged_identity():
    settings = Settings(task_mcp_key="secret")
    with pytest.raises(PermissionError):
        authenticate_mcp({"authorization": "Bearer secret"}, settings)
    with pytest.raises(PermissionError):
        authenticate_mcp(
            {"authorization": "Bearer other", "x-nexus-employee-id": "emp_1"},
            settings,
        )
    with pytest.raises(PermissionError):
        authenticate_mcp(
            {"authorization": "Bearer secret", "x-nexus-employee-id": "emp_1"},
            Settings(task_mcp_key="  "),
        )
    assert (
        authenticate_mcp(
            {"authorization": "Bearer secret", "x-nexus-employee-id": " emp_1 "},
            settings,
        )
        == "emp_1"
    )


async def test_middleware_does_not_trust_employee_header_alone(monkeypatch):
    from app.config.settings import get_settings
    from nexus_api.db import connect, utcnow

    monkeypatch.setenv("NEXUS_TASK_MCP_KEY", "secret")
    get_settings.cache_clear()
    now = utcnow()
    with connect() as db:
        db.execute(
            text(
                """INSERT INTO t_nexus_employees
                (id,name,domain,description,instructions,avatar,enabled,knowledge_json,
                 skills_json,tools_json,created_at,updated_at)
                VALUES ('emp_9','员工','测试','','','🤖',1,'[]','[\"task-management\"]','[]',:t,:t)"""
            ),
            {"t": now},
        )
        db.execute(
            text(
                """INSERT INTO t_nexus_conversations
                (id,employee_id,owner_id,name,agentscope_session_id,created_at,updated_at)
                VALUES ('conv-9','emp_9','u9','私聊','session-9',:t,:t)"""
            ),
            {"t": now},
        )
        db.commit()

    async def endpoint(_request):
        return JSONResponse({"employee": nexus_employee_id.get()})

    app = Starlette(routes=[Route("/mcp", endpoint, methods=["POST"])])
    app.add_middleware(NexusIdentityMiddleware)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mcp"
        ) as client:
            forged = await client.post("/mcp", headers={"X-Nexus-Employee-Id": "emp_9"})
            assert forged.status_code == 401
            accepted = await client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer secret",
                    "X-Nexus-Employee-Id": "emp_9",
                    "X-Nexus-Session-Id": "session-9",
                },
            )
            foreign_session = await client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer secret",
                    "X-Nexus-Employee-Id": "emp_9",
                    "X-Nexus-Session-Id": "session-other",
                },
            )
            health = await client.get("/health")
        assert accepted.status_code == 200
        assert accepted.json()["employee"] == "emp_9"
        assert foreign_session.status_code == 403
        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert nexus_employee_id.get() == ""
    finally:
        get_settings.cache_clear()


async def test_schedule_task_identity_requires_official_policy_and_current_capability(monkeypatch):
    from nexus_api.group_task_auth import authorize_task_source

    calls = []
    state = {"task_allowed": True, "employee_id": "emp-schedule"}

    async def policy(_self, method, url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json=state, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", policy)
    user = "nexus-schedule-" + "a" * 64
    assert await authorize_task_source("schedule-session", "emp-schedule", "", "", user, "agent") == user
    assert calls[0]["params"]["session_id"] == "schedule-session"
    state["task_allowed"] = False
    with pytest.raises(PermissionError):
        await authorize_task_source("schedule-session", "emp-schedule", "", "", user, "agent")
    state["task_allowed"] = True
    with pytest.raises(PermissionError):
        await authorize_task_source("schedule-session", "other-employee", "", "", user, "agent")
    with pytest.raises(PermissionError):
        await authorize_task_source("schedule-session", "emp-schedule", "forged-channel", "", user, "agent")
    with pytest.raises(PermissionError):
        await authorize_task_source("schedule-session", "emp-schedule", "", "", user, "")
    assert len(calls) == 3

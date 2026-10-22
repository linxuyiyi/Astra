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
                """INSERT INTO t_nexus_users
                (id,username,display_name,password_hash,permissions_json,enabled,created_at)
                VALUES ('u9','u9','u9','fixture','[\"task:execute\"]',1,:t)"""
            ),
            {"t": now},
        )
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

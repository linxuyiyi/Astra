import httpx
import pytest
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

    monkeypatch.setenv("NEXUS_TASK_MCP_KEY", "secret")
    get_settings.cache_clear()

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
                headers={"Authorization": "Bearer secret", "X-Nexus-Employee-Id": "emp_9"},
            )
            health = await client.get("/health")
        assert accepted.status_code == 200
        assert accepted.json()["employee"] == "emp_9"
        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert nexus_employee_id.get() == ""
    finally:
        get_settings.cache_clear()

"""内网 Task MCP：显式员工参数、存在性校验和普通业务校验。"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Task, TaskCategory
from app.task_mcp import HealthMiddleware, build_mcp_server
from nexus_api.db_models import employees


@pytest.fixture
async def mcp(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await db.execute(
            employees.insert().values(
                id="emp-plain",
                name="普通员工",
                domain="测试",
                description="",
                instructions="",
                avatar="robot",
                enabled=0,
                skills_json="[]",
                mcp_json="[]",
                knowledge_json="[]",
                tools_json="[]",
                created_at="t",
                updated_at="t",
            )
        )
        db.add_all(
            [
                TaskCategory(id="cat-plain", name="测试"),
            ]
        )
        await db.commit()
    import httpx
    from fastapi import FastAPI

    from app.api.task_tool_routes import router
    from app.database import get_session

    api = FastAPI()
    api.include_router(router)
    async def session():
        async with factory() as db:
            yield db
    api.dependency_overrides[get_session] = session
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "mcp-internal-test")
    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.task_mcp.httpx.AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.ASGITransport(app=api),
    ))
    yield build_mcp_server(), factory
    await engine.dispose()


def create_args(**overrides):
    return {
        "employee_id": "emp-plain",
        "task_type": "REQUIREMENT",
        "category_id": "cat-plain",
        "title": "独立 MCP 创建任务",
        "description": "完成开发",
        **overrides,
    }


async def test_create_rejects_missing_employee_without_creating_task(mcp):
    server, factory = mcp
    result = (
        await server.call_tool("nexus_task_create", create_args(employee_id="missing"))
    ).structured_content
    assert result == {"ok": False, "error": "员工不存在"}
    async with factory() as db:
        assert await db.scalar(select(Task.id)) is None


async def test_category_and_task_state_checks_remain_business_rules(mcp):
    server, _ = mcp
    result = (
        await server.call_tool("nexus_task_create", create_args(category_id="missing"))
    ).structured_content
    assert result == {"ok": False, "error": "请选择已启用的任务分类"}
    result = (await server.call_tool("nexus_task_get", {"task_id": "missing"})).structured_content
    assert result == {"ok": False, "error": "任务不存在"}


def test_task_mcp_initializes_and_lists_tools_without_business_context():
    from fastapi.testclient import TestClient

    server = build_mcp_server()
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host="127.0.0.1"
    )
    app.add_middleware(HealthMiddleware)
    headers = {"accept": "application/json, text/event-stream", "host": "127.0.0.1:18202"}
    with TestClient(app) as client:
        assert client.get("/health").json() == {"ok": True}
        initialized = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "task-test", "version": "1"},
                },
            },
            headers=headers,
        )
        assert initialized.status_code == 200, initialized.text
        tools = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=headers
        )
        assert tools.status_code == 200, tools.text
        assert {"nexus_task_get", "nexus_task_create", "nexus_task_reply"} <= {
            item["name"] for item in tools.json()["result"]["tools"]
        }


async def test_internal_task_tools_require_key_and_reject_private_methods(monkeypatch):
    import httpx

    from app.main import app

    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "internal-route-test")
    root = "/api/v1/internal/task-tools"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(root + "/categories", json={})).status_code == 401
        assert (await client.post(root + "/categories", json={}, headers={
            "X-Nexus-Internal-Key": "wrong",
        })).status_code == 401
        valid = {"X-Nexus-Internal-Key": "internal-route-test"}
        assert (await client.post(root + "/categories", json={}, headers=valid)).status_code == 200
        assert (await client.post(root + "/_runtime", json={}, headers=valid)).status_code == 422
        assert (await client.get("/api/worker-gateway/health")).status_code == 404


async def test_task_mcp_calls_control_api_without_owning_ssh(monkeypatch):
    import json

    import httpx

    from app.runtime.worker_connection import connections

    async def no_ssh(*args, **kwargs):
        raise AssertionError("Task MCP 不能直接建立 SSH")

    monkeypatch.setattr(connections, "get", no_ssh)
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "mcp-route-test")
    seen = []

    def respond(request):
        seen.append(request)
        assert request.headers["X-Nexus-Internal-Key"] == "mcp-route-test"
        assert json.loads(request.content) == {"task_id": "T-mcp"}
        return httpx.Response(200, json={"ok": True, "task_id": "T-mcp", "status": "RUNNING"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.task_mcp.httpx.AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(respond),
    ))
    result = await build_mcp_server().call_tool("nexus_task_get", {"task_id": "T-mcp"})
    assert result.structured_content["status"] == "RUNNING"
    assert len(seen) == 1 and seen[0].url.path == "/api/v1/internal/task-tools/get"

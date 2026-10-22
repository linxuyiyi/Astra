"""日程 MCP：工具到 Nexus 内部接口的契约、鉴权和私聊挂接。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nexus_api import schedule_mcp, schedule_skill


@pytest.fixture(autouse=True)
def schedule_context(monkeypatch):
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "internal-test")
    monkeypatch.setenv("NEXUS_SCHEDULE_MCP_KEY", "mcp-test")
    monkeypatch.setenv("NEXUS_SCHEDULE_MCP_URL", "http://schedule-mcp.test/mcp")
    tokens = [
        (schedule_mcp.SESSION_ID, schedule_mcp.SESSION_ID.set("ses-private")),
        (schedule_mcp.EMPLOYEE_ID, schedule_mcp.EMPLOYEE_ID.set("emp-1")),
        (schedule_mcp.CONVERSATION_ID, schedule_mcp.CONVERSATION_ID.set("conv-1")),
    ]
    schedule_skill.reset_schedule_mcp_attachments()
    yield
    for variable, token in tokens:
        variable.reset(token)
    schedule_skill.reset_schedule_mcp_attachments()


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def request(self, method: str, path: str, **kwargs):
        self.calls.append({"method": method, "path": path, **kwargs})
        return self.response


@pytest.mark.asyncio
async def test_create_defaults_to_disabled_and_forwards_session_headers(monkeypatch):
    fake = FakeClient(FakeResponse(201, {"id": "schedule-1", "data": {"enabled": False}}))
    monkeypatch.setattr(schedule_mcp, "_client", lambda: fake)

    result = await schedule_mcp.create_schedule(
        "早报", "每个工作日整理当天重点", "0 9 * * 1-5"
    )

    assert result["ok"] is True
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/internal/schedules"
    assert call["headers"]["X-Nexus-Internal-Key"] == "internal-test"
    assert call["headers"]["X-Nexus-Session-Id"] == "ses-private"
    assert call["headers"]["X-Nexus-Employee-Id"] == "emp-1"
    assert call["json"]["enabled"] is False
    assert call["json"]["timezone"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_list_and_view_are_session_scoped(monkeypatch):
    items = [{"id": "schedule-1", "data": {"name": "早报"}}]
    listed_fake = FakeClient(FakeResponse(200, {"items": items, "total": 1}))
    monkeypatch.setattr(schedule_mcp, "_client", lambda: listed_fake)
    listed = await schedule_mcp.list_schedules()
    assert listed == {"ok": True, "schedules": items, "total": 1}
    assert listed_fake.calls[0]["path"] == "/api/v1/internal/schedules"

    viewed_fake = FakeClient(FakeResponse(200, items[0]))
    monkeypatch.setattr(schedule_mcp, "_client", lambda: viewed_fake)
    viewed = await schedule_mcp.view_schedule("schedule-1")
    assert viewed == {"ok": True, "schedule": items[0]}
    assert viewed_fake.calls[0]["path"] == "/api/v1/internal/schedules/schedule-1"


@pytest.mark.asyncio
async def test_official_error_is_returned_as_tool_error(monkeypatch):
    fake = FakeClient(FakeResponse(422, {"detail": "invalid timezone"}))
    monkeypatch.setattr(schedule_mcp, "_client", lambda: fake)

    result = await schedule_mcp.create_schedule("早报", "内容", "0 9 * * *", "Bad/Zone")

    assert result == {"ok": False, "error": "invalid timezone"}


@pytest.mark.asyncio
async def test_missing_internal_key_does_not_call_nexus(monkeypatch):
    monkeypatch.delenv("NEXUS_INTERNAL_KEY")
    fake = FakeClient(FakeResponse(200, {}))
    monkeypatch.setattr(schedule_mcp, "_client", lambda: fake)

    result = await schedule_mcp.create_schedule("早报", "内容", "0 9 * * *")

    assert result["ok"] is False
    assert fake.calls == []


def test_health_is_public_and_mcp_requires_bearer():
    app = schedule_mcp.build_app()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/mcp").status_code == 401
        assert (
            client.get(
                "/mcp",
                headers={
                    "Authorization": "Bearer wrong",
                    "X-Nexus-Employee-Id": "emp-1",
                    "X-Nexus-Session-Id": "ses-1",
                },
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/mcp",
                headers={
                    "Authorization": "Bearer mcp-test",
                    "X-Nexus-Employee-Id": "emp-1",
                },
            ).status_code
            == 401
        )


@pytest.mark.asyncio
async def test_attach_schedule_mcp_is_idempotent_and_skips_without_key(monkeypatch):
    class Client:
        configured = True

        def __init__(self):
            self.calls = []

        async def add_workspace_mcp(self, agent_id, session_id, **kwargs):
            self.calls.append((agent_id, session_id, kwargs))

    client = Client()
    employee = {"id": "emp-1", "agentscope_agent_id": "agent-1"}
    await schedule_skill.attach_schedule_mcp(client, employee, "ses-1", "conv-1")
    await schedule_skill.attach_schedule_mcp(client, employee, "ses-1", "conv-1")
    assert len(client.calls) == 1
    agent_id, session_id, kwargs = client.calls[0]
    assert (agent_id, session_id) == ("agent-1", "ses-1")
    assert kwargs["name"] == "nexus-schedule"
    assert kwargs["url"] == "http://schedule-mcp.test/mcp"
    assert kwargs["headers"]["Authorization"] == "Bearer mcp-test"
    assert kwargs["headers"]["X-Nexus-Conversation-Id"] == "conv-1"

    monkeypatch.delenv("NEXUS_SCHEDULE_MCP_KEY")
    schedule_skill.reset_schedule_mcp_attachments()
    await schedule_skill.attach_schedule_mcp(client, employee, "ses-2", "conv-2")
    assert len(client.calls) == 1

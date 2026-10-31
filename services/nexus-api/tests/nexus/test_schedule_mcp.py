"""日程 MCP：工具到 Nexus 内部接口的契约、上下文和私聊挂接。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nexus_api import schedule_mcp, schedule_skill

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "schedule-test", "version": "1"},
    },
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


@pytest.fixture(autouse=True)
def schedule_context(monkeypatch):
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "internal-test")
    monkeypatch.setenv("NEXUS_SCHEDULE_MCP_URL", "http://schedule-mcp.test/mcp")
    tokens = [
        (schedule_mcp.SESSION_ID, schedule_mcp.SESSION_ID.set("ses-private")),
        (schedule_mcp.EMPLOYEE_ID, schedule_mcp.EMPLOYEE_ID.set("emp-1")),
        (schedule_mcp.CONVERSATION_ID, schedule_mcp.CONVERSATION_ID.set("conv-1")),
    ]
    yield
    for variable, token in tokens:
        variable.reset(token)


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
    assert "Authorization" not in call["headers"]
    assert call["json"]["enabled"] is False
    assert call["json"]["timezone"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_missing_session_context_returns_business_error_without_calling_nexus(monkeypatch):
    fake = FakeClient(FakeResponse(200, {}))
    monkeypatch.setattr(schedule_mcp, "_client", lambda: fake)
    token = schedule_mcp.SESSION_ID.set("")

    result = await schedule_mcp.create_schedule("早报", "内容", "0 9 * * *")

    schedule_mcp.SESSION_ID.reset(token)
    assert result["ok"] is False
    assert "业务上下文" in result["error"]
    assert fake.calls == []


@pytest.mark.asyncio
async def test_missing_internal_key_does_not_call_nexus(monkeypatch):
    monkeypatch.delenv("NEXUS_INTERNAL_KEY")
    fake = FakeClient(FakeResponse(200, {}))
    monkeypatch.setattr(schedule_mcp, "_client", lambda: fake)

    result = await schedule_mcp.create_schedule("早报", "内容", "0 9 * * *")

    assert result["ok"] is False
    assert fake.calls == []


def test_health_and_discovery_do_not_require_bearer_or_session_headers():
    app = schedule_mcp.build_app()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        headers = {
            "accept": "application/json, text/event-stream",
            "host": "127.0.0.1:18203",
        }
        initialized = client.post("/mcp", json=INITIALIZE, headers=headers)
        assert initialized.status_code == 200, initialized.text
        assert initialized.json()["result"]["serverInfo"]["name"] == "nexus-schedule"
        tools = client.post("/mcp", json=TOOLS_LIST, headers=headers)
        assert tools.status_code == 200, tools.text
        names = {item["name"] for item in tools.json()["result"]["tools"]}
        assert {"schedule_create", "schedule_list", "schedule_view"} <= names


@pytest.mark.asyncio
async def test_attach_schedule_mcp_has_no_shared_key_and_reuses_agentscope_idempotency():
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
    assert len(client.calls) == 2
    agent_id, session_id, kwargs = client.calls[0]
    assert (agent_id, session_id) == ("agent-1", "ses-1")
    assert kwargs["name"] == "nexus-schedule"
    assert kwargs["url"] == "http://schedule-mcp.test/mcp"
    assert "Authorization" not in kwargs["headers"]
    assert kwargs["headers"]["X-Nexus-Conversation-Id"] == "conv-1"

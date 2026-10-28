import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from nr_version_mcp import server


def _tool_payload(exc_info) -> dict:
    return json.loads(str(exc_info.value))


def test_missing_session_cookie_returns_configuration_error(monkeypatch):
    monkeypatch.delenv("NR_MCP_SESSION_COOKIE", raising=False)

    with pytest.raises(ToolError) as exc_info:
        server.list_versions()

    payload = _tool_payload(exc_info)
    assert payload["code"] == "configuration_error"
    assert "NR_MCP_SESSION_COOKIE" in payload["message"]


def test_invalid_page_returns_structured_validation_error(monkeypatch):
    monkeypatch.setenv("NR_MCP_SESSION_COOKIE", "sessionid=test-session")

    with pytest.raises(ToolError) as exc_info:
        server.list_versions(page=0)

    payload = _tool_payload(exc_info)
    assert payload["code"] == "validation_error"
    assert "page" in payload["details"]
    assert "test-session" not in str(exc_info.value)


def test_invalid_version_id_returns_structured_validation_error(monkeypatch):
    monkeypatch.setenv("NR_MCP_SESSION_COOKIE", "sessionid=test-session")

    with pytest.raises(ToolError) as exc_info:
        server.get_version("not-a-uuid")

    payload = _tool_payload(exc_info)
    assert payload["code"] == "validation_error"
    assert "version_id" in payload["details"]


def test_all_tools_are_marked_read_only():
    tools = asyncio.run(server.mcp.list_tools())

    assert {tool.name for tool in tools} == {"list_versions", "get_version"}
    for tool in tools:
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def _server_params(fake_api: Any, **env_overrides: str) -> StdioServerParameters:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(SRC_DIR),
        "NR_API_BASE_URL": fake_api.base_url,
        "NR_SESSION_COOKIE": fake_api.expected_cookie,
    }
    env.update(env_overrides)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "nr_version_mcp"],
        env=env,
        cwd=str(SRC_DIR.parent),
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_mcp_lists_read_only_tools(fake_api: Any) -> None:
    async def scenario() -> None:
        async with stdio_client(_server_params(fake_api)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()

        by_name = {tool.name: tool for tool in tools.tools}
        assert set(by_name) == {"search_product_versions", "get_product_version"}
        for tool in by_name.values():
            assert tool.annotations is not None
            assert tool.annotations.readOnlyHint is True

    _run(scenario())


def test_mcp_search_uses_default_status_and_pagination(fake_api: Any) -> None:
    async def scenario() -> dict[str, Any]:
        async with stdio_client(_server_params(fake_api)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "search_product_versions",
                    {"keyword": "NR", "page": 1, "page_size": 1},
                )
        assert result.isError is False
        assert result.structuredContent is not None
        return result.structuredContent

    payload = _run(scenario())

    assert payload["count"] == 2
    assert payload["page"] == 1
    assert payload["page_size"] == 1
    assert len(payload["results"]) == 1
    assert payload["results"][0]["status"] == "RELEASED"
    assert payload["applied_statuses"] == ["RELEASED"]
    assert fake_api.requests[-1]["cookie"] == fake_api.expected_cookie


def test_mcp_detail_and_filter_capabilities(fake_api: Any) -> None:
    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with stdio_client(_server_params(fake_api)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                search = await session.call_tool(
                    "search_product_versions",
                    {"status": ["ARCHIVED"], "product_name": "NR 测试套件"},
                )
                detail = await session.call_tool(
                    "get_product_version",
                    {"version_id": "11111111-1111-4111-8111-111111111111"},
                )
        assert search.isError is False and detail.isError is False
        return search.structuredContent, detail.structuredContent

    search_payload, detail_payload = _run(scenario())

    assert search_payload["applied_statuses"] == ["ARCHIVED"]
    assert [item["version"] for item in search_payload["results"]] == ["v0.9.0"]
    assert detail_payload["version"] == "v1.2.0"
    assert detail_payload["description"] == "稳定版本"


def test_mcp_reports_not_found_without_leaking_cookie(fake_api: Any) -> None:
    async def scenario() -> Any:
        async with stdio_client(_server_params(fake_api)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(
                    "get_product_version",
                    {"version_id": "99999999-9999-4999-8999-999999999999"},
                )

    result = _run(scenario())

    assert result.isError is True
    text = result.content[0].text
    assert "not_found" in text
    assert fake_api.expected_cookie not in text


def test_mcp_rejects_missing_cookie(fake_api: Any) -> None:
    async def scenario() -> Any:
        params = _server_params(fake_api, NR_SESSION_COOKIE="")
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("search_product_versions", {})

    result = _run(scenario())

    assert result.isError is True
    assert "config_error" in result.content[0].text


def test_mcp_rejects_non_loopback_base_url(fake_api: Any) -> None:
    async def scenario() -> Any:
        params = _server_params(fake_api, NR_API_BASE_URL="https://nr.example.com/api/v1")
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("search_product_versions", {})

    result = _run(scenario())

    assert result.isError is True
    assert "回环" in result.content[0].text
    assert fake_api.requests == []

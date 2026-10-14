# -*- coding: utf-8 -*-
"""真实无状态 Streamable HTTP MCP 的官方客户端行为测试（任务 3.2.2）。

夹具为测试专用真实 FastMCP HTTP 服务：走官方 ``MCPClient`` 的真实
``list_tools``/``call``，无假工具返回。覆盖：

* 发现与调用真实工具，``enable_tools`` 精确选择生效，未选工具不可调用；
* 不同 HTTP 请求（发现/调用）互不依赖，可重复调用；
* 协议错误、执行超时、慢调用取消后再次调用仍可用；
* 无跨请求 cancel-scope 报错、无本地悬挂 transport 任务、无 offload watcher；
* 真实工具用官方 ``ToolAnnotations(readOnlyHint=True)`` 声明只读语义，原生
  ``PermissionEngine`` 读取真实 schema 后对只读工具给出 ALLOW。

不声称远端操作取消会回滚；stdio 不在本文件正向验收范围（见负向验收）。
"""
from __future__ import annotations

import asyncio

import pytest
from agentscope.mcp import MCPClient, HttpMCPConfig

from mcp_fixture_http import (
    HTTP_MCP_MARKER,
    HTTP_MCP_NAME,
    SELECTED_TOOL,
    UNSELECTED_TOOL,
    running_http_mcp,
)


def _client(url: str, **kwargs) -> MCPClient:
    return MCPClient(
        name=HTTP_MCP_NAME,
        is_stateful=False,
        mcp_config=HttpMCPConfig(url=url),
        **kwargs,
    )


async def _text(result) -> str:
    return "".join(
        block.text
        for block in result.content
        if getattr(block, "type", None) == "text"
    )


def _tool_name(tool: str) -> str:
    return f"mcp__{HTTP_MCP_NAME}__{tool}"


def _dangling_tasks(baseline: set) -> list:
    leftovers = []
    for task in asyncio.all_tasks():
        if task in baseline or task.done():
            continue
        name = getattr(task.get_coro(), "__qualname__", "")
        if "streamable" in name or "offload" in name or "MCP" in name:
            leftovers.append(name)
    return leftovers


@pytest.mark.asyncio
async def test_http_mcp_discovery_selection_and_call():
    with running_http_mcp() as url:
        client = _client(url, enable_tools=[SELECTED_TOOL])
        tools = await client.list_tools()
        assert [t.name for t in tools] == [_tool_name(SELECTED_TOOL)]
        # 未选工具不在官方 list_tools 结果中：不可被装配/调用。
        assert _tool_name(UNSELECTED_TOOL) not in {t.name for t in tools}

        result = await tools[0](a=20, b=22)
        assert "42" in await _text(result)


@pytest.mark.asyncio
async def test_http_mcp_repeat_requests_are_independent():
    """不同 HTTP 发现/调用请求互不依赖，可重复调用。"""
    with running_http_mcp() as url:
        client = _client(url)
        first = {t.name for t in await client.list_tools()}
        second = {t.name for t in await client.list_tools()}
        assert first == second == {
            _tool_name(SELECTED_TOOL),
            _tool_name(UNSELECTED_TOOL),
        }
        tools = {t.name: t for t in await client.list_tools()}
        assert "3" in await _text(await tools[_tool_name(SELECTED_TOOL)](a=1, b=2))
        assert "7" in await _text(await tools[_tool_name(SELECTED_TOOL)](a=3, b=4))
        assert HTTP_MCP_MARKER in await _text(
            await tools[_tool_name(UNSELECTED_TOOL)](),
        )


@pytest.mark.asyncio
async def test_http_mcp_protocol_error_then_recovers():
    """协议错误后同一客户端再次调用真实工具仍可用。"""
    with running_http_mcp() as url:
        bad = _client(url.rsplit("/mcp", 1)[0] + "/not-an-mcp")
        with pytest.raises(Exception):
            await bad.list_tools()

        good = _client(url, enable_tools=[SELECTED_TOOL])
        tools = await good.list_tools()
        assert "5" in await _text(await tools[0](a=2, b=3))


@pytest.mark.asyncio
async def test_http_mcp_timeout_then_recovers():
    """执行超时后再次调用（无延迟）仍可用。"""
    with running_http_mcp() as url:
        client = _client(url, enable_tools=[SELECTED_TOOL], execution_timeout=0.3)
        tools = await client.list_tools()
        with pytest.raises(Exception):
            await tools[0](a=1, b=1, delay=2.0)
        assert "9" in await _text(await tools[0](a=4, b=5))


@pytest.mark.asyncio
async def test_http_mcp_slow_call_cancelled_then_recovers():
    """慢调用取消后再次调用仍可用；不承诺远端回滚。"""
    with running_http_mcp() as url:
        client = _client(url, enable_tools=[SELECTED_TOOL])
        tools = await client.list_tools()
        task = asyncio.ensure_future(tools[0](a=1, b=2, delay=5.0))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "8" in await _text(await tools[0](a=3, b=5))


@pytest.mark.asyncio
async def test_http_mcp_no_dangling_transport_tasks():
    """无状态调用结束后不残留 transport 任务，关闭无 cancel-scope 报错。"""
    baseline = set(asyncio.all_tasks())
    with running_http_mcp() as url:
        client = _client(url)
        for _ in range(3):
            tools = await client.list_tools()
            await tools[0](a=1, b=1)
        await client.close()  # 无状态 close 为空操作，绝不抛 cancel-scope 错误
        await asyncio.sleep(0.2)
        assert _dangling_tasks(baseline) == []


@pytest.mark.asyncio
async def test_http_mcp_read_only_annotation_yields_native_allow():
    """真实工具声明 readOnlyHint：原生 PermissionEngine 读取 schema 后 ALLOW。"""
    from agentscope.permission import (
        PermissionBehavior,
        PermissionContext,
        PermissionEngine,
        PermissionMode,
    )

    from nexus_agent_service.guard import NexusControlledTool

    with running_http_mcp() as url:
        client = _client(url)
        tools = {t.name: t for t in await client.list_tools()}
        compute = tools[_tool_name(SELECTED_TOOL)]
        marker = tools[_tool_name(UNSELECTED_TOOL)]
        # 官方 MCPClient 从真实 ToolAnnotations 解析出只读语义。
        assert compute.is_read_only is True
        assert marker.is_read_only is True

        engine = PermissionEngine(PermissionContext(mode=PermissionMode.DEFAULT))
        # 真实 MCP 工具与受控 facade 都应走只读快速路径得到 ALLOW。
        for tool in (
            compute,
            marker,
            NexusControlledTool(compute, "sess-1"),
        ):
            decision = await engine.check_permission(tool, {"a": 1, "b": 2})
            assert decision.behavior is PermissionBehavior.ALLOW

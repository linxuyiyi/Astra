# -*- coding: utf-8 -*-
"""真实资源目录投影测试（OpenSpec 任务 3.2.2）。

真实官方 ASGI + SQLite 验证：空目录返回真实空态；无 knowledge manager 时
知识库为 unavailable（非空数组伪装）。模拟上游验证：传输/协议错误标记
unavailable；MCP 原始配置/env/认证 URL 绝不出现在投影中。
"""
from __future__ import annotations

import httpx
import pytest

from _stack import mock_stack, real_stack


@pytest.mark.asyncio
async def test_catalog_real_empty_and_knowledge_unavailable(tmp_path):
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        response = await client.get("/api/resources")
        assert response.status_code == 200, response.text
        data = response.json()

        assert data["skills"]["available"] is True
        assert data["skills"]["items"] == []
        assert data["mcps"]["available"] is True
        assert data["mcps"]["items"] == []
        # 未配置 knowledge_base_manager：官方返回 503 feature-disabled，必须
        # 标记 unavailable 并给出原因，不伪装成成功空目录。
        assert data["knowledge"]["available"] is False
        assert data["knowledge"]["items"] == []
        assert data["knowledge"]["reason"]

        single = (await client.get("/api/resources/skill")).json()
        assert single["kind"] == "skill"
        assert single["skills"]["available"] is True
        assert (
            await client.get("/api/resources/unknown")
        ).status_code == 404


def _catalog_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/skill":
        return httpx.Response(
            200,
            json=[
                {
                    "id": "skill-1",
                    "name": "计算技能",
                    "description": "描述",
                    "enabled": True,
                    "markdown": "SECRET-MARKDOWN",
                },
            ],
        )
    if path == "/mcp":
        return httpx.Response(
            200,
            json=[
                {
                    "id": "mcp-1",
                    "name": "危险 MCP",
                    "description": "desc",
                    "enabled": True,
                    "is_stateful": True,
                    "mcp_config": {
                        "type": "stdio_mcp",
                        "command": "run",
                        "env": {"API_KEY": "SECRET-ENV-TOKEN"},
                    },
                    "url": "https://user:SECRET-URL@example.test/sse",
                },
            ],
        )
    if path == "/nexus/mcp-support":
        # 只读安全接口：只给 id/selectable/reason，绝不带配置/URL/env。
        return httpx.Response(
            200,
            json=[
                {
                    "id": "mcp-1",
                    "selectable": False,
                    "reason": (
                        "当前版本仅支持无状态 MCP；有状态或 stdio 条目不可选。"
                    ),
                },
            ],
        )
    if path == "/knowledge_bases/":
        return httpx.Response(
            503,
            json={
                "detail": "Knowledge base feature is disabled SECRET-REASON",
            },
        )
    return httpx.Response(404, json={"detail": "unexpected"})


@pytest.mark.asyncio
async def test_catalog_projection_excludes_secrets(tmp_path):
    async with mock_stack(tmp_path, _catalog_handler) as (b, a, client):
        response = await client.get("/api/resources")
        assert response.status_code == 200
        text = response.text
        assert "SECRET-ENV-TOKEN" not in text
        assert "SECRET-URL" not in text
        assert "SECRET-MARKDOWN" not in text
        # 503 的原因原文（可能含内部细节）也不回显。
        assert "SECRET-REASON" not in text

        data = response.json()
        assert data["skills"]["items"] == [
            {
                "id": "skill-1",
                "name": "计算技能",
                "description": "描述",
                "enabled": True,
                "selectable": True,
                "reason": None,
            },
        ]
        # stateful MCP 条目在本版本不可选：安全投影只给 selectable/reason，
        # 绝不回显原始 mcp_config/env/URL。
        assert data["mcps"]["items"] == [
            {
                "id": "mcp-1",
                "name": "危险 MCP",
                "description": "desc",
                "enabled": True,
                "selectable": False,
                "reason": "当前版本仅支持无状态 MCP；有状态或 stdio 条目不可选。",
            },
        ]
        assert data["knowledge"]["available"] is False


@pytest.mark.asyncio
async def test_catalog_transport_error_is_unavailable_not_empty(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/skill":
            raise httpx.ConnectError("down")
        if request.url.path == "/mcp":
            return httpx.Response(200, json=[])
        if request.url.path == "/nexus/mcp-support":
            return httpx.Response(200, json=[])
        if request.url.path == "/knowledge_bases/":
            return httpx.Response(200, json={"knowledge_bases": [], "total": 0})
        return httpx.Response(404, json={})

    async with mock_stack(tmp_path, handler) as (b, a, client):
        data = (await client.get("/api/resources")).json()
        assert data["skills"]["available"] is False
        assert data["skills"]["items"] == []
        assert data["skills"]["reason"]
        # 其他类别仍按真实结果投影。
        assert data["mcps"]["available"] is True
        assert data["knowledge"]["available"] is True


@pytest.mark.asyncio
async def test_knowledge_catalog_pages_fully(tmp_path):
    """知识库目录必须完整分页：第 101 个资源不能被误判为不存在。"""
    pages = {
        1: {
            "knowledge_bases": [
                {"id": f"kb-{i}", "name": f"库{i}"} for i in range(1, 101)
            ],
            "total": 101,
            "page": 1,
            "page_size": 100,
        },
        2: {
            "knowledge_bases": [{"id": "kb-101", "name": "库101"}],
            "total": 101,
            "page": 2,
            "page_size": 100,
        },
    }
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        if request.url.path == "/mcp":
            return httpx.Response(200, json=[])
        if request.url.path == "/nexus/mcp-support":
            return httpx.Response(200, json=[])
        if request.url.path == "/knowledge_bases/":
            page = int(request.url.params.get("page", "1"))
            seen.append(page)
            return httpx.Response(200, json=pages[page])
        return httpx.Response(404, json={})

    async with mock_stack(tmp_path, handler) as (b, a, client):
        data = (await client.get("/api/resources")).json()
        assert data["knowledge"]["available"] is True
        assert len(data["knowledge"]["items"]) == 101
        assert any(item["id"] == "kb-101" for item in data["knowledge"]["items"])
        assert seen == [1, 2]


@pytest.mark.asyncio
async def test_catalog_malformed_entry_is_unavailable(tmp_path):
    """非法 name/enabled 不能被默认值冒充正常，整类目录标记不可用。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/skill":
            return httpx.Response(
                200,
                json=[{"id": "s1", "name": 123, "enabled": True}],
            )
        if request.url.path == "/mcp":
            return httpx.Response(
                200,
                json=[{"id": "m1", "name": "m", "enabled": "yes"}],
            )
        if request.url.path == "/nexus/mcp-support":
            return httpx.Response(200, json=[])
        if request.url.path == "/knowledge_bases/":
            return httpx.Response(
                200,
                json={"knowledge_bases": [], "total": 0},
            )
        return httpx.Response(404, json={})

    async with mock_stack(tmp_path, handler) as (b, a, client):
        data = (await client.get("/api/resources")).json()
        assert data["skills"]["available"] is False
        assert data["mcps"]["available"] is False
        assert data["knowledge"]["available"] is True


@pytest.mark.parametrize(
    "entry, expected",
    [
        ({}, (False, "当前版本仅支持无状态 MCP；有状态或 stdio 条目不可选。")),
        (
            {"is_stateful": True},
            (False, "当前版本仅支持无状态 MCP；有状态或 stdio 条目不可选。"),
        ),
        (
            # 库视图不含 mcp_config：不得默认判可选（删除旧的 True 分支）。
            {"is_stateful": False},
            (False, "MCP 连接配置缺失或不可读，不可选。"),
        ),
        (
            {
                "is_stateful": False,
                "mcp_config": {"type": "stdio_mcp"},
            },
            (False, "当前版本仅支持 Streamable HTTP MCP；stdio 等类型不可选。"),
        ),
        (
            {
                "is_stateful": False,
                "mcp_config": {"type": "http_mcp", "url": "https://h/mcp"},
            },
            (True, None),
        ),
        (
            {
                "is_stateful": False,
                "mcp_config": {"type": "http_mcp", "url": "https://h/sse?k=1"},
            },
            (False, "当前版本不支持 SSE 传输 MCP。"),
        ),
        (
            {
                "is_stateful": False,
                "mcp_config": {"type": "http_mcp", "url": "https://h/messages/"},
            },
            (False, "当前版本不支持 SSE 传输 MCP。"),
        ),
        (
            {
                "is_stateful": False,
                "mcp_config": {"type": "http_mcp", "url": "ftp://h/mcp"},
            },
            (False, "MCP 连接地址不是有效 HTTP(S) URL，不可选。"),
        ),
        (
            {
                "is_stateful": False,
                "mcp_config": {"type": "http_mcp", "url": ""},
            },
            (False, "MCP 连接地址缺失，不可选。"),
        ),
    ],
)
def test_mcp_support_pure_function(entry, expected):
    from nexus_backend.resources import mcp_support

    selectable, reason = mcp_support(entry)
    assert (selectable, reason) == expected


def _mcp_handlers(library, support, *, support_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        if request.url.path == "/mcp":
            return httpx.Response(200, json=library)
        if request.url.path == "/nexus/mcp-support":
            if support_status != 200:
                return httpx.Response(support_status, json={"detail": "boom"})
            return httpx.Response(200, json=support)
        if request.url.path == "/knowledge_bases/":
            return httpx.Response(
                200,
                json={"knowledge_bases": [], "total": 0},
            )
        return httpx.Response(404, json={})

    return handler


_LIBRARY = [
    {
        "id": "m1",
        "name": "静态",
        "description": "d",
        "enabled": True,
        "is_stateful": False,
        "mcp_config": {
            "type": "http_mcp",
            "url": "https://user:SECRET-URL@example.test/mcp",
            "headers": {"Authorization": "SECRET-HEADER"},
        },
    },
]


@pytest.mark.asyncio
async def test_mcp_support_merge_missing_id_is_unavailable(tmp_path):
    """支持目录缺项：库/支持目录必须一一完整，缺项不可用、不 fallback。"""
    support = []  # 缺 m1
    async with mock_stack(
        tmp_path,
        _mcp_handlers(_LIBRARY, support),
    ) as (b, a, client):
        data = (await client.get("/api/resources")).json()
        assert data["mcps"]["available"] is False
        assert data["mcps"]["items"] == []
        assert data["mcps"]["reason"]


@pytest.mark.asyncio
async def test_mcp_support_merge_extra_id_is_unavailable(tmp_path):
    """支持目录多余 ID：不完整一致即不可用。"""
    support = [
        {"id": "m1", "selectable": True, "reason": None},
        {"id": "m-extra", "selectable": True, "reason": None},
    ]
    async with mock_stack(
        tmp_path,
        _mcp_handlers(_LIBRARY, support),
    ) as (b, a, client):
        data = (await client.get("/api/resources")).json()
        assert data["mcps"]["available"] is False


@pytest.mark.asyncio
async def test_mcp_support_duplicate_id_is_unavailable(tmp_path):
    support = [
        {"id": "m1", "selectable": True, "reason": None},
        {"id": "m1", "selectable": False, "reason": "x"},
    ]
    async with mock_stack(
        tmp_path,
        _mcp_handlers(_LIBRARY, support),
    ) as (b, a, client):
        data = (await client.get("/api/resources")).json()
        assert data["mcps"]["available"] is False


@pytest.mark.asyncio
async def test_mcp_support_request_failure_is_unavailable(tmp_path):
    support = [{"id": "m1", "selectable": True, "reason": None}]
    async with mock_stack(
        tmp_path,
        _mcp_handlers(_LIBRARY, support, support_status=500),
    ) as (b, a, client):
        data = (await client.get("/api/resources")).json()
        assert data["mcps"]["available"] is False


@pytest.mark.asyncio
async def test_mcp_sse_support_marks_unselectable_without_secrets(tmp_path):
    """无状态 SSE 由安全支持目录判不可选；响应不含 config/URL/header。"""
    library = [dict(_LIBRARY[0])]
    library[0]["mcp_config"] = {
        "type": "http_mcp",
        "url": "https://user:SECRET-URL@example.test/sse?key=SECRET-KEY",
        "headers": {"Authorization": "SECRET-HEADER"},
    }
    support = [
        {
            "id": "m1",
            "selectable": False,
            "reason": "当前版本不支持 SSE 传输 MCP。",
        },
    ]
    async with mock_stack(
        tmp_path,
        _mcp_handlers(library, support),
    ) as (b, a, client):
        response = await client.get("/api/resources")
        text = response.text
        assert "SECRET-URL" not in text
        assert "SECRET-KEY" not in text
        assert "SECRET-HEADER" not in text
        data = response.json()
        assert data["mcps"]["available"] is True
        assert data["mcps"]["items"] == [
            {
                "id": "m1",
                "name": "静态",
                "description": "d",
                "enabled": True,
                "selectable": False,
                "reason": "当前版本不支持 SSE 传输 MCP。",
            },
        ]

# -*- coding: utf-8 -*-
"""真实资源目录投影（OpenSpec 任务 3.2.2）。

从官方 ``GET /skill``、``GET /mcp``、``GET /knowledge_bases/`` 读取真实
目录，只向浏览器投影稳定安全字段（顶层 ID、名称、描述、enabled/可用状态，以及
``selectable``/``reason`` 支持标记），绝不返回 MCP 原始 config、env、认证 URL
或 credential 内容。MCP 支持范围由最小纯函数 :func:`mcp_support` 统一判定：
本版本仅支持官方无状态 Streamable HTTP，stdio/SSE/stateful 安全投影为不可选。

每类资源都区分：

* ``available=True``：上游返回成功，``items`` 可为真实空数组；
* ``available=False``：feature-disabled（官方 503）或传输/协议错误，携带
  安全原因，``items=[]``，**绝不冒充真实空目录**。

员工保存默认关联时复用同一投影核对资源是否真实存在且 enabled；目录不可用
或资源失效时明确失败，不静默保存只有标签的能力。
"""
from __future__ import annotations

from typing import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request

from .adapter import AgentScopeHTTPClient, UpstreamError

# 浏览器接口使用的资源类别 → 官方目录投影键。
KIND_API = {
    "skill": "skills",
    "mcp": "mcps",
    "knowledge": "knowledge",
}
# 员工默认关联字段 → 目录投影键。
FIELD_KIND = {
    "knowledge": "knowledge",
    "skills": "skills",
    "tools": "mcps",
}


def mcp_support(entry: object) -> tuple[bool, str | None]:
    """最小纯函数：判断一个 MCP 条目在当前版本是否可选。

    本版本明确只支持官方**无状态 Streamable HTTP** 客户端：

    * ``is_stateful`` 必须恰为 ``False``（stdio 官方强制有状态，stateful 条目
      一律不可选）；
    * 必须携带可读的 ``mcp_config``：``type`` 为 ``http_mcp``、URL 为有效
      HTTP(S) URL，且 path 不以 ``/sse`` 或 ``/messages/`` 结尾（与官方
      ``MCPClient._is_sse`` 选择逻辑一致）。

    官方库视图（``GET /mcp``）刻意隐藏 ``mcp_config``，不能据此判定可选；库层
    的可选性由专用安全接口 :meth:`AgentScopeHTTPClient.mcp_support_map` 提供
    （服务侧按真实 ``MCPClient`` 属性计算），本函数只接受带配置的条目。

    返回 ``(selectable, reason)``；``reason`` 为安全中文原因，绝不包含配置、
    URL、env 或密钥内容。
    """
    if not isinstance(entry, dict):
        return False, "MCP 条目结构不可读，不可选。"
    if entry.get("is_stateful") is not False:
        return False, "当前版本仅支持无状态 MCP；有状态或 stdio 条目不可选。"
    config = entry.get("mcp_config")
    if not isinstance(config, dict):
        return False, "MCP 连接配置缺失或不可读，不可选。"
    if config.get("type") != "http_mcp":
        return False, "当前版本仅支持 Streamable HTTP MCP；stdio 等类型不可选。"
    url = config.get("url")
    if not isinstance(url, str) or not url:
        return False, "MCP 连接地址缺失，不可选。"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False, "MCP 连接地址不是有效 HTTP(S) URL，不可选。"
    if parts.path.endswith("/sse") or parts.path.endswith("/messages/"):
        return False, "当前版本不支持 SSE 传输 MCP。"
    return True, None


def _safe_item(
    item: dict,
    *,
    enabled_default: bool,
    selectable: bool = True,
    reason: str | None = None,
) -> dict | None:
    """严格投影单条资源（不含 MCP 可选性）；契约不符返回 ``None``。

    ``id`` 必须是非空字符串；``name``/``description`` 缺省为 ``""``（合法官方
    字段的正常缺省），但若字段存在却不是字符串即视为契约失败；``enabled`` 缺省
    使用明确默认值，存在但不是布尔值也视为契约失败。绝不静默丢弃非法条目后把
    目录说成成功空目录。``selectable``/``reason`` 由调用方给出（Skill/Knowledge
    恒为 ``True``；MCP 由安全支持接口合并而来）。
    """
    if not isinstance(item, dict):
        return None
    identifier = item.get("id")
    if not isinstance(identifier, str) or not identifier:
        return None
    if "name" in item and not isinstance(item.get("name"), str):
        return None
    if "description" in item and not isinstance(item.get("description"), str):
        return None
    name = item.get("name")
    description = item.get("description")
    if "enabled" in item:
        enabled = item.get("enabled")
        if not isinstance(enabled, bool):
            return None
    else:
        enabled = enabled_default
    return {
        "id": identifier,
        "name": name if isinstance(name, str) else "",
        "description": description if isinstance(description, str) else "",
        "enabled": enabled,
        "selectable": selectable,
        "reason": reason,
    }


def _parse_support_map(payload: object) -> dict[str, tuple[bool, str | None]] | None:
    """严格解析 ``/nexus/mcp-support`` 响应；任何契约不符返回 ``None``。"""
    if not isinstance(payload, list):
        return None
    result: dict[str, tuple[bool, str | None]] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            return None
        identifier = entry.get("id")
        selectable = entry.get("selectable")
        reason = entry.get("reason")
        if not isinstance(identifier, str) or not identifier:
            return None
        if not isinstance(selectable, bool):
            return None
        if reason is not None and not isinstance(reason, str):
            return None
        if identifier in result:
            return None
        result[identifier] = (selectable, reason)
    return result


async def _project_mcps(adapter: AgentScopeHTTPClient) -> dict:
    """把库目录与安全支持目录按 ID 完整合并；任何缺项/重复/非法即不可用。

    官方库视图不含 ``mcp_config``，不能自行判断传输支持；这里用只读
    ``/nexus/mcp-support`` 提供的真实 ``MCPClient`` 判定结果与库列表按 ID
    **一一完整合并**，缺项/多余/重复/非法字段一律标记目录不可用，绝不 fallback
    为“可选”。
    """
    try:
        raw = await adapter.list_mcps()
    except UpstreamError as exc:
        return _unavailable(_reason_for(exc))
    if not isinstance(raw, list):
        return _unavailable("官方目录响应格式异常。")
    try:
        support_raw = await adapter.mcp_support_map()
    except UpstreamError as exc:
        return _unavailable(_reason_for(exc))
    support = _parse_support_map(support_raw)
    if support is None:
        return _unavailable("MCP 支持目录响应格式异常。")

    items: list[dict] = []
    library_ids: list[str] = []
    for entry in raw:
        projected = _safe_item(entry, enabled_default=True)
        if projected is None:
            return _unavailable("官方目录条目不符合投影契约。")
        library_ids.append(projected["id"])
        items.append(projected)
    if len(set(library_ids)) != len(library_ids):
        return _unavailable("官方 MCP 目录存在重复 ID。")
    if set(library_ids) != set(support):
        # 支持目录必须与库目录一一完整一致；缺项/多余都不能猜测。
        return _unavailable("MCP 支持目录与库目录不完整一致。")
    for item in items:
        selectable, reason = support[item["id"]]
        item["selectable"] = selectable
        item["reason"] = reason
    return {"available": True, "reason": None, "items": items}


def _unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason, "items": []}


def _reason_for(exc: UpstreamError) -> str:
    """把上游失败收敛为安全中文原因，绝不回显响应原文。"""
    if exc.status_code == 503:
        return "官方功能未启用或暂不可用（feature disabled）。"
    if exc.status_code is None:
        return "无法连接 AgentScope 服务。"
    if exc.status_code is not None and 400 <= exc.status_code < 500:
        return "官方目录拒绝了本次读取。"
    return "官方目录读取失败。"


async def project_kind(
    adapter: AgentScopeHTTPClient,
    kind: str,
) -> dict:
    """读取并投影某一类资源目录（``kind`` 取 ``skills``/``mcps``/``knowledge``）。"""
    if kind == "mcps":
        # MCP 需要与安全支持目录按 ID 完整合并，单独处理。
        return await _project_mcps(adapter)
    if kind == "skills":
        fetch: Callable[[], Awaitable[object]] = adapter.list_skills
        enabled_default = True
    elif kind == "knowledge":
        # 完整分页，避免第 101 个真实资源被误判为不存在。
        fetch = adapter.list_all_knowledge_bases
        enabled_default = True
    else:
        raise ValueError(f"未知资源类别：{kind}")

    try:
        payload = await fetch()
    except UpstreamError as exc:
        return _unavailable(_reason_for(exc))

    if kind == "knowledge":
        raw = (
            payload.get("knowledge_bases")
            if isinstance(payload, dict)
            else None
        )
    else:
        raw = payload
    if not isinstance(raw, list):
        return _unavailable("官方目录响应格式异常。")

    items = []
    for entry in raw:
        projected = _safe_item(
            entry,
            enabled_default=enabled_default,
        )
        if projected is None:
            # 任一官方条目不符合投影契约：整个目录标记不可用，不用“少一条的
            # 成功空目录”冒充正常。
            return _unavailable("官方目录条目不符合投影契约。")
        items.append(projected)
    return {"available": True, "reason": None, "items": items}


async def catalog(adapter: AgentScopeHTTPClient) -> dict:
    """一次性读取三类真实目录。"""
    return {
        kind: await project_kind(adapter, kind)
        for kind in ("skills", "mcps", "knowledge")
    }


def register_resource_routes(app: FastAPI) -> None:
    @app.get("/api/resources", tags=["resources"])
    async def list_resources(request: Request) -> dict:
        """三类资源目录的安全投影。"""
        adapter: AgentScopeHTTPClient = request.app.state.adapter
        return await catalog(adapter)

    @app.get("/api/resources/{kind}", tags=["resources"])
    async def list_resource_kind(request: Request, kind: str) -> dict:
        key = KIND_API.get(kind)
        if key is None:
            raise HTTPException(status_code=404, detail="未知资源类别。")
        adapter: AgentScopeHTTPClient = request.app.state.adapter
        return {"kind": kind, key: await project_kind(adapter, key)}

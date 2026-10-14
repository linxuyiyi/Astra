#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试用真实本地 MCP（stdio）服务器。

提供可验证的计算与标记读取工具，供真实 MCP 进程证据使用；它不是产品默认
资源，也不代表模型能力。
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp_server = FastMCP("nexus-compute")

# 运行期可设置的标记，用于验证“标记读取”确实来自该 MCP 进程。
_MARKER = {"value": "nexus-mcp-marker"}


@mcp_server.tool()
def compute(expression: str) -> str:
    """计算一个受限的整数/浮点算术表达式并返回结果。"""
    allowed = set("0123456789+-*/(). ")
    if not expression or not set(expression) <= allowed:
        return "error: unsupported expression"
    try:
        value = eval(expression, {"__builtins__": {}}, {})  # noqa: S307
    except Exception:  # noqa: BLE001
        return "error: evaluation failed"
    return str(value)


@mcp_server.tool()
def read_marker() -> str:
    """返回该 MCP 进程持有的标记，证明工具由真实进程执行。"""
    return _MARKER["value"]


if __name__ == "__main__":
    mcp_server.run()

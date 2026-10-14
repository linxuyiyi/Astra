# -*- coding: utf-8 -*-
"""测试专用真实无状态 Streamable HTTP MCP 服务器（任务 3.2.2）。

用官方 ``mcp.server.fastmcp.FastMCP`` 构建，绑定动态本机端口，只在测试/冒烟
中注入，**不是产品默认注册资源**。它提供两个真实工具：

* ``compute_plus``：真实整数加法，用于验证所选工具确实被调用；
* ``read_marker``：未选工具，用于验证 ``enable_tools`` 精确选择生效。

服务以真实 uvicorn 线程在独立端口运行，客户端通过官方 ``MCPClient`` 的真实
Streamable HTTP 传输访问，无假工具返回。
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import contextmanager

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

HTTP_MCP_NAME = "nexus-compute-http"
SELECTED_TOOL = "compute_plus"
UNSELECTED_TOOL = "read_marker"
HTTP_MCP_MARKER = "nexus-http-marker"

# 这两个夹具工具真实为纯只读（不产生副作用）：用官方 ToolAnnotations 如实声明
# ``readOnlyHint=True``，使原生 PermissionEngine 依据真实 schema 得到 ALLOW，
# 而不是被误判为需要 ASK。生产非只读工具的原生 ASK 语义不受影响。
_READ_ONLY = ToolAnnotations(readOnlyHint=True)


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_port(port: int, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("HTTP MCP 夹具未在预期时间内就绪")


def build_http_mcp() -> FastMCP:
    server = FastMCP(HTTP_MCP_NAME)

    @server.tool(annotations=_READ_ONLY)
    async def compute_plus(a: int, b: int, delay: float = 0.0) -> int:
        """计算两个整数之和并返回结果；``delay`` 仅用于超时/取消验收。"""
        if delay:
            await asyncio.sleep(delay)
        return a + b

    @server.tool(annotations=_READ_ONLY)
    def read_marker() -> str:
        """返回该 MCP 进程持有的标记（未选工具，用于负向验收）。"""
        return HTTP_MCP_MARKER

    return server


@contextmanager
def running_http_mcp():
    """在真实本机端口启动无状态 Streamable HTTP MCP，退出时停止。"""
    server_app = build_http_mcp()
    port = free_port()
    server_app.settings.host = "127.0.0.1"
    server_app.settings.port = port
    app = server_app.streamable_http_app()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_port(port)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def http_mcp_declaration(url: str) -> dict:
    """官方库/workspace 装载用的无状态 Streamable HTTP MCP 声明。"""
    return {
        "name": HTTP_MCP_NAME,
        "is_stateful": False,
        "mcp_config": {"type": "http_mcp", "url": url},
    }

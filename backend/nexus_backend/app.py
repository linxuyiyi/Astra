# -*- coding: utf-8 -*-
"""Nexus 业务后端最小服务。

当前只提供真实的上游健康连通检查；员工、私聊、团队等业务在后续任务建立。
后端只保存到 AgentScope 服务的连接配置，不保存模型密钥或模型参数。

``GET /api/health`` 会实际调用 AgentScope 的 ``/health`` 与 ``/nexus/health``，
核对固定版本与初始化状态；上游不可用或版本不符时明确报告不可用，不伪造
成功。``ready`` 仅表示本进程 lifespan 是否完成，供控制脚本判断进程归属与
生命周期。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request, Response

from .adapter import AgentScopeHTTPClient
from .config import EXPECTED_AGENTSCOPE_VERSION, BackendConfig
from .db import Database
from .employees import register_employee_routes


def build_app(
    config: BackendConfig,
    instance_id: str | None = None,
    *,
    adapter: AgentScopeHTTPClient | None = None,
) -> FastAPI:
    """构建 Nexus 后端 FastAPI 应用。

    ``adapter`` 可注入以复用连接或测试；未提供时按配置新建。注入的
    adapter 若使用外部 client，``aclose`` 不会替调用方关闭它。
    """
    config.data_dir.mkdir(parents=True, exist_ok=True)

    adapter = adapter or AgentScopeHTTPClient(
        config.agentscope_service_url,
        config.internal_user_id,
        request_timeout=config.request_timeout,
        connect_timeout=config.connect_timeout,
    )
    database = Database(config.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[Any]:
        app.state.adapter = adapter
        # 新库直接建表；不做旧模型迁移。
        await database.init()
        app.state.nexus_backend_ready = True
        try:
            yield
        finally:
            app.state.nexus_backend_ready = False
            await database.dispose()
            await adapter.aclose()

    app = FastAPI(
        title="Nexus Backend",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.nexus_config = config
    app.state.adapter = adapter
    app.state.db = database
    # 单进程每员工串行锁：覆盖读/校验/上游/本地/补偿的整个业务操作（含原生
    # 网络调用序列）；只有数据库事务保持短小、不在事务内做网络 I/O。
    app.state.employee_locks = {}
    app.state.nexus_backend_ready = False
    app.state.nexus_instance_id = instance_id

    _register_routes(app)
    register_employee_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    @app.get("/api/health", tags=["nexus"])
    async def api_health(request: Request, response: Response) -> dict:
        """后端健康：实际探测 AgentScope 的组件、版本与初始化。

        * ``ready``：本进程 lifespan 是否完成（控制脚本据此判断归属）。
        * ``status``：``ok``（上游连通且版本/初始化正常）、
          ``unavailable``（本进程已就绪但上游不可用）、``initializing``。
        * 未就绪或上游不可用时返回 ``503``，不伪装健康。
        """
        state = request.app.state
        ready = bool(getattr(state, "nexus_backend_ready", False))
        config: BackendConfig = state.nexus_config
        adapter: AgentScopeHTTPClient = state.adapter

        upstream = await _probe_upstream(adapter, config)
        healthy = ready and upstream["healthy"]
        if not ready:
            status = "initializing"
            reason = "后端尚未完成启动初始化。"
        elif not upstream["healthy"]:
            status = "unavailable"
            reason = upstream["reason"]
        else:
            status = "ok"
            reason = None

        response.status_code = 200 if healthy else 503
        return {
            "status": status,
            "ready": ready,
            "healthy": healthy,
            "instance_id": getattr(state, "nexus_instance_id", None),
            "agentscope": upstream,
            "reason": reason,
        }


async def _probe_upstream(
    adapter: AgentScopeHTTPClient,
    config: BackendConfig,
) -> dict:
    """实际探测官方服务，返回脱敏结构与是否健康。"""
    native = await adapter.probe_native_health()
    extension = await adapter.probe_extension_health()

    info: dict = {
        "base_url": adapter.base_url,
        "reachable": native.reachable,
        "http_status": native.http_status,
        "service_status": native.body.get("status"),
        "version": native.body.get("version"),
        "expected_version": EXPECTED_AGENTSCOPE_VERSION,
        "version_ok": native.body.get("version")
        == EXPECTED_AGENTSCOPE_VERSION,
        "components": native.body.get("components"),
        "extension_ready": (
            extension.body.get("ready") if extension.reachable else None
        ),
        "model_configured": (
            extension.body.get("model_configured")
            if extension.reachable
            else None
        ),
    }

    def fail(reason: str) -> dict:
        info["healthy"] = False
        info["reason"] = reason
        return info

    if not native.reachable:
        return fail(f"无法连接 AgentScope 服务：{native.error}")
    if native.http_status != 200 or native.body.get("status") != "ok":
        return fail(
            "AgentScope 服务未就绪"
            f"（HTTP {native.http_status}）：{native.error or '组件未全部就绪'}",
        )
    if not info["version_ok"]:
        return fail(
            "AgentScope 服务版本不符："
            f"期望 {EXPECTED_AGENTSCOPE_VERSION}，实际 {info['version']}",
        )
    if not extension.reachable:
        return fail(f"无法读取 AgentScope 扩展健康：{extension.error}")
    if extension.body.get("ready") is not True:
        # 只回传安全分类，绝不回显上游响应原文（可能含 SQL 参数或凭据）。
        return fail(
            "AgentScope 扩展未就绪"
            f"（HTTP {extension.http_status}）："
            f"{extension.error or '初始化未完成'}",
        )

    info["healthy"] = True
    info["reason"] = None
    return info

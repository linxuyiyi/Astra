# -*- coding: utf-8 -*-
"""Nexus 独立 AgentScope 服务的应用装配。

基于官方 ``agentscope.app.create_app``，只通过公开接口与公开扩展点配置：

* ``AsyncSQLAlchemyStorage``（SQLite + aiosqlite）持久化原生记录；
* 单进程 ``InMemoryMessageBus``；
* ``SessionWorkspaceManager``（公开 ``WorkspaceManagerBase`` 扩展），显式
  ``IsolationPolicy.PER_SESSION``，按原生持久 workspace ID 建立独立物理目录；
* 关闭 scheduler 与 channel worker。

不导入官方私有模块，不复制或 Fork 官方核心源码，也不替换原生路由；
仅在官方应用之上追加极小 ``/nexus/*`` 只读扩展。
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from agentscope.app import create_app
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import IsolationPolicy
from fastapi import FastAPI, Request, Response
from fastapi.middleware import Middleware

from .bootstrap import ModelBootstrapStatus, bootstrap_default_model
from .config import ServiceConfig
from .workspace import SessionWorkspaceManager
from .write_barrier import NativeWriteBarrierMiddleware


def build_app(config: ServiceConfig, instance_id: str | None = None) -> FastAPI:
    """构建并返回官方 AgentScope FastAPI 应用。"""
    # aiosqlite 的 DEBUG 日志会把绑定参数（含模型密钥）原样打印，且不受
    # SQLAlchemy ``hide_parameters`` 约束；在装配前收紧该 logger 级别。
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.workspace_dir.mkdir(parents=True, exist_ok=True)

    storage = AsyncSQLAlchemyStorage(
        url=config.database_url,
        create_tables=True,
        # 日志/异常中隐藏 SQL 绑定参数，避免模型密钥随参数泄露。
        engine_kwargs={"hide_parameters": True},
    )
    # 官方 LocalWorkspaceManager 2.0.10 的 workdir 固定为 basedir/agent_id，
    # 同员工不同 Session 仍共用目录；这里用公开扩展按持久 workspace_id 分目录。
    workspace_manager = SessionWorkspaceManager(
        basedir=str(config.workspace_dir),
        isolation=IsolationPolicy.PER_SESSION,
    )

    # 每次 build/start 生成新的服务代次标识（非空 UUID）。Nexus 用它区分
    # “旧单进程请求是否已随重启退出”，不作为可空的控制脚本实例标识。
    generation_id = uuid.uuid4().hex

    app = create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=workspace_manager,
        enable_scheduler=False,
        enable_channel_worker=False,
        extra_middlewares=[
            Middleware(
                NativeWriteBarrierMiddleware,
                storage=storage,
                user_id=config.internal_user_id,
                generation_id=generation_id,
            ),
        ],
        title="Nexus AgentScope Service",
    )

    # 官方 lifespan 负责进入 storage/message_bus/workspace_manager 等资源。
    # 包一层，使默认模型引导发生在资源就绪之后；引导失败会明确抛出脱敏
    # 错误，使服务不进入就绪状态，而不是伪装健康。
    official_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def nexus_lifespan(app_: FastAPI) -> AsyncIterator[Any]:
        async with official_lifespan(app_) as state:
            app_.state.nexus_model_status = await bootstrap_default_model(
                storage,
                config,
            )
            app_.state.nexus_ready = True
            try:
                yield state
            finally:
                app_.state.nexus_ready = False

    app.router.lifespan_context = nexus_lifespan

    app.state.nexus_config = config
    # 本次 build/start 的服务代次标识（每次构建新生成，永不为 None）。
    app.state.nexus_generation_id = generation_id
    # 由 scripts/services.py 注入的本次启动随机实例标识（手动启动为 None）。
    app.state.nexus_instance_id = instance_id
    # 就绪状态由真实 lifespan 推进：未完成前不为 ok，退出后清除。
    app.state.nexus_ready = False
    app.state.nexus_model_status = ModelBootstrapStatus(
        configured=False,
        credential_id=None,
        model_name=config.model_name,
        reason="服务尚未完成启动初始化。",
    )

    _register_nexus_routes(app)
    return app


def _register_nexus_routes(app: FastAPI) -> None:
    """仅追加只读扩展路由；不改动官方原生路由。"""

    @app.get("/nexus/health", tags=["nexus"])
    async def nexus_health(request: Request, response: Response) -> dict:
        """服务健康与模型配置状态。

        原生 ``/health`` 只证明组件挂载，不探测模型；这里明确区分
        “服务健康”与“模型未配置”，不伪装模型可用。``status`` 反映真实
        lifespan 就绪状态：未就绪返回 503，退出后清除就绪。
        """
        ready = bool(getattr(request.app.state, "nexus_ready", False))
        status: ModelBootstrapStatus = request.app.state.nexus_model_status
        response.status_code = 200 if ready else 503
        return {
            "status": "ok" if ready else "initializing",
            "ready": ready,
            "generation_id": getattr(
                request.app.state,
                "nexus_generation_id",
                None,
            ),
            "instance_id": getattr(
                request.app.state,
                "nexus_instance_id",
                None,
            ),
            "model_configured": status.configured,
            "model_name": status.model_name,
            "reason": status.reason,
        }

    @app.get("/nexus/defaults", tags=["nexus"])
    async def nexus_defaults(request: Request) -> dict:
        """返回默认 ChatModelConfig，供 Nexus 创建原生 Session。

        只含 credential_id/type/model/parameters，绝不返回密钥；模型未
        配置时 ``chat_model_config`` 为 ``null`` 并给出原因。
        """
        config: ServiceConfig = request.app.state.nexus_config
        status: ModelBootstrapStatus = request.app.state.nexus_model_status
        return {
            "model_configured": status.configured,
            "chat_model_config": (
                config.default_chat_model_config() if status.configured else None
            ),
            "reason": status.reason,
        }

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
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware import Middleware

from .bootstrap import (
    EmbeddingBootstrapStatus,
    ModelBootstrapStatus,
    bootstrap_default_model,
    bootstrap_embedding_credential,
)
from .config import ServiceConfig
from .guard import (
    DenyAllAuthorizer,
    NexusGuardAuthorizer,
    make_run_guard_factory,
    mcp_client_support,
)
from .knowledge import ObservedCollectionPerKbManager
from .workspace import SessionWorkspaceManager
from .write_barrier import NativeWriteBarrierMiddleware


def _build_knowledge_manager(config: ServiceConfig, storage: object):
    """按需构建**公开观察** manager + 官方 Qdrant/LocalBlobStore。

    默认关闭。开启时要求已安装 ``qdrant-client`` 与 Ollama embedding 依赖；
    缺失则明确报错（fail closed），不静默降级为假目录。目录从实际
    ``AGENTSCOPE_DATA_DIR`` 派生，隔离实例不会写入项目默认 data。真正可检索
    还要求服务端已引导 embedding 凭据并创建知识库；本函数只装配资源，向量库
    与 blob 的进入/退出由 ``create_app`` 生命周期管理，不重复进入。
    """
    if not config.knowledge_enabled:
        return None, None, None, None
    try:
        from agentscope.app.rag.blob_store import LocalBlobStore
        from agentscope.rag import ApproxTokenChunker, QdrantStore, TextParser
    except ImportError as exc:  # pragma: no cover - 取决于可选依赖
        raise RuntimeError(
            "AGENTSCOPE_KNOWLEDGE_ENABLED 已开启，但缺少 qdrant-client 或 "
            "Ollama embedding 依赖；请同步交付依赖后再启用知识库。",
        ) from exc
    vector_dir = str(config.knowledge_vector_dir)
    blob_dir = str(config.knowledge_blob_dir)
    vector_store = QdrantStore(path=vector_dir)
    # 只多用公开子类包装 get_knowledge 观察检索；其余生命周期沿用官方。
    manager = ObservedCollectionPerKbManager(storage, vector_store)
    blob_store = LocalBlobStore(blob_dir)
    # 显式注册与官方默认相同的公开 parser/chunker，避免依赖隐式默认漂移。
    return manager, blob_store, [TextParser()], [ApproxTokenChunker]


def build_app(
    config: ServiceConfig,
    instance_id: str | None = None,
    *,
    guard_authorizer: NexusGuardAuthorizer | None = None,
    skill_hubs: list | None = None,
    mcp_hubs: list | None = None,
) -> FastAPI:
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

    (
        knowledge_manager,
        blob_store,
        knowledge_parsers,
        knowledge_chunkers,
    ) = _build_knowledge_manager(config, storage)

    # 运行 guard：正式 build **始终**安装。配置了内部秘密时使用 HTTP 授权器
    # 回调 Nexus；否则使用 fail-closed 的 DenyAllAuthorizer（拒绝一切运行），
    # 不提供“离线跳过 guard”的生产开关。单元测试可显式注入 authorizer。
    owned_authorizer: NexusGuardAuthorizer | None = None
    if guard_authorizer is None:
        if config.nexus_internal_token:
            owned_authorizer = NexusGuardAuthorizer(
                config.nexus_guard_url,
                config.nexus_internal_token,
            )
            guard_authorizer = owned_authorizer
        else:
            guard_authorizer = DenyAllAuthorizer()
    guard_configured = not isinstance(guard_authorizer, DenyAllAuthorizer)

    extra_agent_middlewares = make_run_guard_factory(guard_authorizer)

    app = create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=workspace_manager,
        knowledge_base_manager=knowledge_manager,
        knowledge_parsers=knowledge_parsers,
        knowledge_chunkers=knowledge_chunkers,
        blob_store=blob_store,
        enable_scheduler=False,
        enable_channel_worker=False,
        skill_hubs=skill_hubs,
        mcp_hubs=mcp_hubs,
        extra_middlewares=[
            Middleware(
                NativeWriteBarrierMiddleware,
                storage=storage,
                user_id=config.internal_user_id,
                generation_id=generation_id,
            ),
        ],
        extra_agent_middlewares=extra_agent_middlewares,
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
            # 知识库启用时按固定 id 引导原生 OllamaCredential；未启用不写记录。
            app_.state.nexus_embedding_status = (
                await bootstrap_embedding_credential(storage, config)
            )
            app_.state.nexus_ready = True
            try:
                yield state
            finally:
                app_.state.nexus_ready = False
                if owned_authorizer is not None:
                    await owned_authorizer.aclose()

    app.router.lifespan_context = nexus_lifespan

    app.state.nexus_config = config
    # 本次 build/start 的服务代次标识（每次构建新生成，永不为 None）。
    app.state.nexus_generation_id = generation_id
    # 运行 guard 是否配置了真实授权（缺秘密时为 fail-closed 拒绝一切运行）。
    app.state.nexus_guard_configured = guard_configured
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
    app.state.nexus_embedding_status = EmbeddingBootstrapStatus(
        enabled=config.knowledge_enabled,
        configured=False,
        credential_id=None,
        model_name=config.embedding_model,
        dimensions=config.embedding_dimensions,
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
        embedding: EmbeddingBootstrapStatus = (
            request.app.state.nexus_embedding_status
        )
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
            "guard_configured": bool(
                getattr(request.app.state, "nexus_guard_configured", False),
            ),
            # 知识库/embedding 引导状态（脱敏；仅连接信息，不含密钥）。
            "knowledge_enabled": embedding.enabled,
            "embedding_configured": embedding.configured,
            "embedding_credential_id": embedding.credential_id,
            "embedding_model": embedding.model_name,
            "embedding_dimensions": embedding.dimensions,
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
            "guard_configured": bool(
                getattr(request.app.state, "nexus_guard_configured", False),
            ),
        }

    @app.get("/nexus/mcp-support", tags=["nexus"])
    async def nexus_mcp_support(request: Request) -> list:
        """按服务端内部租户返回每个已安装 MCP 的安全可选性。

        官方 ``GET /mcp`` 的 ``MCPView`` 刻意隐藏 ``mcp_config``，无法判断
        传输类型；这里从公开 ``StorageBase.list_mcps`` 读取真实 ``MCPRecord``
        （其 ``client`` 有公开 ``is_stateful``/``mcp_config``），用同一支持
        条件计算，仅返回 ``id``/``selectable``/``reason``，**绝不连接 MCP、
        绝不返回配置/URL/env/header/credential**。调用方必须携带与服务端内部
        租户一致的 ``X-User-ID``。
        """
        config: ServiceConfig = request.app.state.nexus_config
        user_id = config.internal_user_id
        if request.headers.get("X-User-ID") != user_id:
            raise HTTPException(status_code=403, detail="租户不匹配。")
        storage = request.app.state.storage
        records = await storage.list_mcps(user_id)
        result = []
        for record in records:
            selectable, reason = mcp_client_support(record.client)
            result.append(
                {
                    "id": record.id,
                    "selectable": selectable,
                    "reason": reason,
                },
            )
        return result

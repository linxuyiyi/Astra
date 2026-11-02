"""Nexus 单一服务入口。

同一个进程提供员工对话（``/api/v1``）、任务平台（``/api``）、Worker
Gateway WebSocket 和前端静态资源。网关数据面、Task MCP 与 AgentScope 仍是
独立基础设施进程。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.gateway_routes import gateway_error_handler
from app.api.gateway_routes import router as gateway_router
from app.api.library_routes import library_error_handler, library_principal
from app.api.library_routes import router as library_router
from app.api.maintenance_routes import maintenance_actor
from app.api.maintenance_routes import router as maintenance_router
from app.api.repository_routes import router as repository_router
from app.api.routes import router as platform_router
from app.api.speech_routes import router as speech_router
from app.api.worker_routes import router as worker_router
from app.api.workspace_routes import execution_stop_actor
from app.api.workspace_routes import router as workspace_router
from app.gateway.security import CredentialError
from app.gateway.service import GatewayError
from app.library_service import LibraryError, LibraryPrincipal
from app.main import lifespan as platform_lifespan
from app.worker_gateway.routes import router as worker_gateway_router

from .agentscope_proxy import router as agentscope_proxy_router
from .auth import require_user
from .main import create_app as create_chat_app
from .nebula import router as nebula_router

WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
API_PREFIXES = ("api/", "v1/", "agentscope-api/", "docs", "redoc", "openapi.json")


def session_maintenance_actor(request: Request) -> str:
    """统一服务记录临时 UID；公司登录接入后使用公司身份。"""
    return require_user(request)["uid"]


def session_library_principal(request: Request) -> LibraryPrincipal:
    user = require_user(request)
    return LibraryPrincipal(user_id=user["uid"], role="admin")


def _include_platform(app: FastAPI) -> None:
    app.dependency_overrides[maintenance_actor] = session_maintenance_actor
    app.dependency_overrides[library_principal] = session_library_principal
    app.dependency_overrides[execution_stop_actor] = session_maintenance_actor
    app.include_router(platform_router)
    app.include_router(repository_router)
    app.include_router(worker_router)
    app.include_router(worker_gateway_router)
    app.include_router(maintenance_router)
    app.include_router(workspace_router)
    app.include_router(library_router)
    app.include_router(gateway_router)
    app.include_router(speech_router)
    app.include_router(nebula_router)
    app.add_exception_handler(LibraryError, library_error_handler)
    app.add_exception_handler(GatewayError, gateway_error_handler)
    app.add_exception_handler(CredentialError, gateway_error_handler)


def _mount_frontend(app: FastAPI) -> None:
    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend(full_path: str):
        if full_path.startswith(API_PREFIXES):
            raise HTTPException(status_code=404, detail="Not Found")
        index = WEB_DIST / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="前端尚未构建")
        if full_path:
            candidate = (WEB_DIST / full_path).resolve()
            if not candidate.is_relative_to(WEB_DIST.resolve()):
                raise HTTPException(status_code=404, detail="Not Found")
            if candidate.is_file():
                return FileResponse(candidate)
            if candidate.is_dir() and (candidate / "index.html").is_file():
                return FileResponse(candidate / "index.html")
            if full_path.startswith("agentscope-ui"):
                agent_index = WEB_DIST / "agentscope-ui" / "index.html"
                if agent_index.is_file():
                    return FileResponse(agent_index)
            if full_path.startswith(("assets/", "opencode-workspace/")):
                raise HTTPException(status_code=404, detail="静态资源不存在")
        return FileResponse(index)


def create_service() -> FastAPI:
    app = create_chat_app()
    chat_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(scope: FastAPI) -> AsyncIterator[None]:
        async with chat_lifespan(scope):
            async with platform_lifespan(scope):
                yield

    app.router.lifespan_context = lifespan
    _include_platform(app)
    # 必须在兜底静态路由之前注册，否则 /agentscope-api/ 会被当成前端页面。
    app.include_router(agentscope_proxy_router)
    _mount_frontend(app)
    return app


app = create_service()

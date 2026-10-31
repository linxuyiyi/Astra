from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from app.config.settings import get_settings
from app.database import SessionLocal
from app.library_schemas import (
    LibraryFeedbackInput,
    LibrarySearchInput,
)
from app.library_service import KnowledgeService, LibraryError
from app.llm_wiki import LlmWikiError
from app.runtime.runtime_context import RuntimeContextError


def _authority(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url or "")
    host = parsed.hostname
    if not host or host in {"0.0.0.0", "::", "*"} or "*" in host:
        return None
    literal = f"[{host}]" if ":" in host else host
    authority = f"{literal}:{parsed.port}" if parsed.port else literal
    scheme = parsed.scheme or "http"
    return authority, f"{scheme}://{authority}"


def transport_security(settings=None) -> TransportSecuritySettings:
    """Allow only the explicitly configured MCP resource/issuer hosts.

    The SDK defaults to loopback-only when DNS rebinding protection is on. A
    real Worker reaches the service through ``mcp_resource_server_url``, so that
    configured authority (and the configured bind/issuer) must be allowed
    explicitly. Request Host headers are never trusted as a source.
    """

    resolved = settings or get_settings()
    hosts: list[str] = []
    origins: list[str] = []

    def add(authority: str, origin: str) -> None:
        if authority not in hosts:
            hosts.append(authority)
        if origin not in origins:
            origins.append(origin)

    for url in (
        resolved.mcp_resource_server_url,
        resolved.mcp_issuer_url,
    ):
        parsed = _authority(url)
        if parsed is not None:
            add(*parsed)
    if resolved.mcp_host not in {"0.0.0.0", "::", ""}:
        parsed = _authority(f"http://{resolved.mcp_host}:{resolved.mcp_port}")
        if parsed is not None:
            add(*parsed)

    for authority in ("127.0.0.1:*", "localhost:*", "[::1]:*"):
        if authority not in hosts:
            hosts.append(authority)
    for origin in (
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        "https://127.0.0.1:*",
        "https://localhost:*",
        "https://[::1]:*",
    ):
        if origin not in origins:
            origins.append(origin)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build_mcp_server() -> MCPServer:
    server = MCPServer(
        name="NEXUS Digital Library",
        title="NEXUS 数字图书馆",
        version="1.0.0",
        description="Agent Worker 查询、引用和反馈正式知识的服务。",
        instructions=(
            "先使用 library_search 查找正式知识，再用 library_get 将选中的 Markdown 放入任务上下文。"
            "只负责执行任务；任务总结与经验由人工确认完成后的后台复盘统一处理。"
        ),
    )

    @server.tool(name="library_search", structured_output=True)
    async def library_search(
        runtime_context: str,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """搜索 Wiki；runtime_context 用于记录本次知识读取所属 Task/Session。"""
        try:
            payload = LibrarySearchInput(query=query, limit=limit)
        except ValidationError as exc:
            raise ToolError(str(exc)) from None
        async with SessionLocal() as session:
            try:
                results = await KnowledgeService(session).mcp_search(
                    payload, runtime_context=runtime_context
                )
            except (LibraryError, LlmWikiError, RuntimeContextError) as exc:
                raise ToolError(str(exc)) from None
        return [result.model_dump(mode="json") for result in results]

    @server.tool(name="library_get", structured_output=True)
    async def library_get(runtime_context: str, resource_id: str) -> dict[str, Any]:
        """读取 Wiki 完整 Markdown，并记录本次读取所属 Task/Session。"""
        async with SessionLocal() as session:
            try:
                result = await KnowledgeService(session).mcp_get(
                    resource_id, runtime_context=runtime_context
                )
            except (LibraryError, LlmWikiError, RuntimeContextError) as exc:
                raise ToolError(str(exc)) from None
        return result.model_dump(mode="json")

    @server.tool(name="library_related", structured_output=True)
    async def library_related(resource_id: str) -> dict[str, Any]:
        """查询相关知识能力状态；当前不支持可靠的按页 Graph 邻居查询。"""
        async with SessionLocal() as session:
            try:
                return await KnowledgeService(session).mcp_related(resource_id)
            except (LibraryError, LlmWikiError) as exc:
                raise ToolError(str(exc)) from None

    @server.tool(name="library_feedback", structured_output=True)
    async def library_feedback(
        resource_id: str,
        feedback_type: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """在 Nexus 记录 helpful、incorrect、outdated、missing 或 mismatch 反馈。"""
        try:
            payload = LibraryFeedbackInput(
                resource_id=resource_id,
                feedback_type=feedback_type,
                comment=comment,
            )
        except ValidationError as exc:
            raise ToolError(str(exc)) from None
        async with SessionLocal() as session:
            try:
                result = await KnowledgeService(session).mcp_feedback(payload)
            except (LibraryError, LlmWikiError) as exc:
                raise ToolError(str(exc)) from None
        return result.model_dump(mode="json")

    return server


mcp = build_mcp_server()


def main() -> None:
    settings = get_settings()
    try:
        if settings.mcp_transport == "stdio":
            mcp.run(transport="stdio")
            return
        mcp.run(
            transport="streamable-http",
            host=settings.mcp_host,
            port=settings.mcp_port,
            streamable_http_path=settings.mcp_streamable_http_path,
            stateless_http=True,
            json_response=True,
            transport_security=transport_security(settings),
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

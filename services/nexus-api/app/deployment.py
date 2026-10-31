"""显式部署步骤；不运行于普通服务请求，也不保存明文密钥。"""

from __future__ import annotations

import argparse
import asyncio
import os
from uuid import uuid4

from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal, validate_database
from app.gateway.models import GatewayKey, utcnow
from app.gateway.security import gateway_key_digest, gateway_key_prefix
from app.gateway.urls import gateway_base
from app.llm_wiki import LlmWikiClient, LlmWikiError


async def register_gateway_key() -> None:
    from nexus_api.model_route import gateway_api_key

    raw = gateway_api_key()
    gateway_base(os.environ.get("NEXUS_GATEWAY_OPENAI_BASE", ""))
    await validate_database()
    async with SessionLocal() as session:
        key = await session.scalar(select(GatewayKey).where(GatewayKey.key_digest == gateway_key_digest(raw)))
        if key:
            if (
                not key.enabled
                or key.revoked_at
                or key.deleted_at
                or (key.expires_at and key.expires_at <= utcnow())
            ):
                raise RuntimeError("平台网关密钥已禁用、删除或过期，请显式提供有效密钥")
            return
        session.add(
            GatewayKey(
                id="gk_" + uuid4().hex,
                name="Nexus platform",
                owner_id="nexus-platform",
                key_prefix=gateway_key_prefix(raw),
                key_digest=gateway_key_digest(raw),
                allowed_models=[],
                enabled=True,
            )
        )
        await session.commit()


async def check_wiki_project() -> None:
    settings = get_settings()
    if not settings.llm_wiki_api_token or not settings.llm_wiki_project_id:
        raise RuntimeError("已有 llm_wiki 需要真实 ASTRA_LLM_WIKI_API_TOKEN 和 ASTRA_LLM_WIKI_PROJECT_ID")
    client = LlmWikiClient(settings)
    await client.health()
    await client.search("Nexus deployment verification", limit=1)


def _local_mcp_url(host: str, port: int, path: str) -> str:
    address = str(host or "").strip()
    if address in {"", "0.0.0.0", "::", "[::]"}:
        address = "127.0.0.1"
    if ":" in address and not address.startswith("["):
        address = f"[{address}]"
    return f"http://{address}:{port}{path}"


async def _direct_mcp_tools(url: str) -> list[str]:
    """用官方 MCP 客户端完成 initialize 和 tools/list。"""

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(url) as streams:
        # 不同 MCP SDK 版本返回二元或三元组；前两项始终是读写流。
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [str(tool.name) for tool in result.tools]


async def verify_mcp() -> None:
    """部署验收：直接执行 MCP 协议，并通过 AgentScope 工作区真实注册和连接。"""

    from nexus_api.agentscope import AgentScopeClient, AgentScopeError
    from nexus_api.schedule_skill import schedule_mcp_url
    from nexus_api.task_skill import task_mcp_url

    settings = get_settings()
    if settings.mcp_transport != "streamable-http":
        raise RuntimeError("库 MCP 未使用 streamable-http，无法执行标准 MCP 验收")
    task_url = task_mcp_url()
    schedule_url = schedule_mcp_url()
    library_url = _local_mcp_url(
        settings.mcp_host,
        settings.mcp_port,
        settings.mcp_streamable_http_path,
    )
    direct = (
        ("Library", library_url, "library_search"),
        ("Task", task_url, "nexus_task_get"),
        ("Schedule", schedule_url, "schedule_create"),
    )
    for label, url, required in direct:
        try:
            tools = await _direct_mcp_tools(url)
        except Exception as exc:
            raise RuntimeError(
                f"{label} MCP 初始化或工具发现失败：{type(exc).__name__}"
            ) from None
        if required not in tools:
            raise RuntimeError(f"{label} MCP 工具发现缺少 {required}")

    client = AgentScopeClient(settings.agentscope_base_url, settings.agentscope_user_id)
    if not client.configured:
        raise RuntimeError("AgentScope 未连接，无法验证 MCP 注册")
    agent_id = ""
    session_id = ""
    installed: list[str] = []
    cleanup_errors: list[str] = []
    failure: Exception | None = None
    try:
        agent_id = await client.create_agent(
            f"Nexus MCP 部署校验 {uuid4().hex[:8]}",
            "只用于部署时验证 MCP 注册与工具发现，验收后删除。",
            "MCP 部署校验",
        )
        session_id = await client.create_probe_session(agent_id, "Nexus MCP 部署校验")
        for name, url in (("nexus-task", task_url), ("nexus-schedule", schedule_url)):
            await client.add_workspace_mcp(
                agent_id,
                session_id,
                url=url,
                headers={},
                name=name,
            )
            installed.append(name)
        items = await client.list_workspace_mcp(agent_id, session_id)
        for name in installed:
            item = next(
                (row for row in items if str(row.get("name") or "") == name),
                None,
            )
            if item is None:
                raise RuntimeError(f"AgentScope 工作区未返回 {name}")
            if not item.get("is_healthy"):
                raise RuntimeError(
                    f"AgentScope 注册的 {name} 连接失败：{item.get('error') or '未返回错误'}"
                )
            tools = item.get("tools")
            if not isinstance(tools, list) or not tools:
                raise RuntimeError(f"AgentScope 注册的 {name} 工具发现为空")
    except AgentScopeError as exc:
        failure = RuntimeError(f"AgentScope MCP 注册或连接失败：{exc.detail}")
    except Exception as exc:  # noqa: BLE001 - 部署验收需要把失败原样带出
        failure = exc
    finally:
        for name in installed:
            try:
                await client.remove_workspace_mcp(agent_id, session_id, name)
            except AgentScopeError as exc:
                cleanup_errors.append(f"移除 {name} 失败：{exc.detail}")
        if session_id:
            try:
                await client.delete_session(agent_id, session_id)
            except AgentScopeError as exc:
                cleanup_errors.append(f"删除校验会话失败：{exc.detail}")
        if agent_id:
            try:
                await client.delete_agent(agent_id)
            except AgentScopeError as exc:
                cleanup_errors.append(f"删除校验 Agent 失败：{exc.detail}")
        await client.aclose()
    if cleanup_errors:
        detail = "；".join(cleanup_errors)
        if failure is not None:
            detail += f"；原始失败：{failure}"
        raise RuntimeError(detail)
    if failure is not None:
        raise RuntimeError(str(failure)) from None


def main() -> None:
    parser = argparse.ArgumentParser(description="Nexus 部署初始化与鉴权预检")
    parser.add_argument("action", choices=["gateway-key", "wiki", "mcp"])
    action = parser.parse_args().action
    try:
        if action == "gateway-key":
            asyncio.run(register_gateway_key())
        elif action == "wiki":
            asyncio.run(check_wiki_project())
        else:
            asyncio.run(verify_mcp())
    except (RuntimeError, ValueError, LlmWikiError) as exc:
        prefix = "[失败] MCP 验收：" if action == "mcp" else ""
        # 这些领域诊断不包含 URL 或密钥；其他驱动异常统一隐藏细节。
        parser.exit(1, f"{prefix}{exc}\n")
    except Exception:
        message = "部署检查失败，请检查数据库或服务连接与权限"
        if action == "mcp":
            message = "[失败] MCP 验收：无法通过 AgentScope 或 MCP 服务连接"
        parser.exit(1, message + "\n")
    print(
        {
            "gateway-key": "平台网关密钥已登记",
            "wiki": "llm_wiki 项目鉴权与检索通过",
            "mcp": "内置 MCP 初始化、工具发现与 AgentScope 注册连接通过",
        }[action]
    )


if __name__ == "__main__":
    main()

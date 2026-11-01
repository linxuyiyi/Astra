"""把内置的日程 MCP 安装到私聊 Session；失败不阻塞聊天。"""

from __future__ import annotations

import logging
import os

from .agentscope import AgentScopeClient, AgentScopeError

logger = logging.getLogger("nexus.schedule_mcp")


def schedule_mcp_url() -> str:
    from app.config.settings import get_settings

    explicit = os.environ.get("NEXUS_SCHEDULE_MCP_URL", "").strip()
    return explicit or f"http://127.0.0.1:{get_settings().schedule_mcp_port}/mcp"


def schedule_mcp_headers(employee_id: str, session_id: str, conversation_id: str = "") -> dict[str, str]:
    return {
        "X-Nexus-Employee-Id": employee_id,
        "X-Nexus-Session-Id": session_id,
        "X-Nexus-Conversation-Id": conversation_id,
    }


async def _discovery_error(client: AgentScopeClient, agent_id: str, session_id: str) -> str:
    items = await client.list_workspace_mcp(agent_id, session_id)
    target = next((item for item in items if str(item.get("name") or "") == "nexus-schedule"), None)
    if target is None:
        return "AgentScope 工作区没有返回 nexus-schedule"
    if not target.get("is_healthy"):
        return str(target.get("error") or "MCP 连接失败")
    tools = target.get("tools")
    if not isinstance(tools, list) or not tools:
        return "MCP 工具发现返回空列表"
    return ""


async def attach_schedule_mcp(
    client: AgentScopeClient,
    employee: dict,
    session_id: str,
    conversation_id: str = "",
) -> str:
    """幂等挂接；成功返回空串，失败记录错误并返回描述（调用方可继续聊天）。"""

    agent_id = employee.get("agentscope_agent_id")
    if not client.configured or not agent_id or not session_id:
        return ""
    try:
        await client.add_workspace_mcp(
            str(agent_id),
            session_id,
            url=schedule_mcp_url(),
            headers=schedule_mcp_headers(str(employee["id"]), session_id, conversation_id),
            name="nexus-schedule",
        )
        detail = await _discovery_error(client, str(agent_id), session_id)
    except AgentScopeError as exc:
        detail = exc.detail
    if detail:
        logger.error("日程 MCP 不可用 session=%s：%s", session_id, detail)
    return detail

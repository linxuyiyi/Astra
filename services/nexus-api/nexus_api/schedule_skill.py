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


async def attach_schedule_mcp(
    client: AgentScopeClient,
    employee: dict,
    session_id: str,
    conversation_id: str = "",
) -> None:
    """幂等挂接；每次调用交给 AgentScope 判别已有配置是否一致。"""

    agent_id = employee.get("agentscope_agent_id")
    if not client.configured or not agent_id or not session_id:
        return
    try:
        await client.add_workspace_mcp(
            str(agent_id),
            session_id,
            url=schedule_mcp_url(),
            headers=schedule_mcp_headers(str(employee["id"]), session_id, conversation_id),
            name="nexus-schedule",
        )
    except AgentScopeError as exc:
        logger.warning("日程 MCP 未挂上：%s", exc.detail)

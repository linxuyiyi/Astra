"""把内置的日程 MCP 安装到私聊 Session；失败不阻塞聊天。"""

from __future__ import annotations

import logging
import os

from .agentscope import AgentScopeClient, AgentScopeError

logger = logging.getLogger("nexus.schedule_mcp")

_attached: set[tuple[str, str]] = set()


def schedule_mcp_url() -> str:
    return os.environ.get("NEXUS_SCHEDULE_MCP_URL", "http://127.0.0.1:18203/mcp").strip()


def schedule_mcp_headers(employee_id: str, session_id: str, conversation_id: str = "") -> dict[str, str]:
    headers = {
        "X-Nexus-Employee-Id": employee_id,
        "X-Nexus-Session-Id": session_id,
        "X-Nexus-Conversation-Id": conversation_id,
    }
    key = os.environ.get("NEXUS_SCHEDULE_MCP_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def reset_schedule_mcp_attachments() -> None:
    _attached.clear()


async def attach_schedule_mcp(
    client: AgentScopeClient,
    employee: dict,
    session_id: str,
    conversation_id: str = "",
) -> None:
    """幂等挂接；没有密钥时跳过，AgentScope 暂时不可用时只记 warning。"""

    agent_id = employee.get("agentscope_agent_id")
    if not client.configured or not agent_id or not session_id:
        return
    if not os.environ.get("NEXUS_SCHEDULE_MCP_KEY", "").strip():
        logger.warning("NEXUS_SCHEDULE_MCP_KEY 未设置，日程 MCP 不会挂接")
        return
    cache_key = (str(agent_id), session_id)
    if cache_key in _attached:
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
        return
    _attached.add(cache_key)

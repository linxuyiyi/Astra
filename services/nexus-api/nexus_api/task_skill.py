"""内置任务技能。安装失败不能挡住员工保存或发消息。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .agentscope import AgentScopeClient, AgentScopeError

TASK_SKILL_ID = "task-management"
TASK_SKILL_NAME = "任务管理"
logger = logging.getLogger("nexus.task_skill")


def skill_directory() -> Path:
    return Path(__file__).resolve().parents[2] / "agent-service" / "skills" / "task-management"


def with_builtin_skill(items: list[dict[str, str]]) -> list[dict[str, str]]:
    if any(item.get("id") == TASK_SKILL_ID for item in items):
        return items
    return [{"id": TASK_SKILL_ID, "name": TASK_SKILL_NAME}, *items]


def task_mcp_url() -> str:
    return os.environ.get("NEXUS_TASK_MCP_URL", "http://127.0.0.1:8202/mcp")


def task_mcp_headers(
    employee_id: str,
    session_id: str,
    *,
    conversation_id: str = "",
    channel_id: str = "",
    discussion_id: str = "",
) -> dict[str, str]:
    headers = {
        "X-Nexus-Employee-Id": employee_id,
        "X-Nexus-Session-Id": session_id,
        "X-Nexus-Conversation-Id": conversation_id,
        "X-Nexus-Channel-Id": channel_id,
        "X-Nexus-Discussion-Id": discussion_id,
    }
    key = os.environ.get("NEXUS_TASK_MCP_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    else:
        logger.warning("NEXUS_TASK_MCP_KEY 未设置，Task MCP 会拒绝这次挂接")
    return headers


async def attach_task_capability(
    client: AgentScopeClient,
    employee: dict,
    session_id: str,
    *,
    conversation_id: str = "",
    channel_id: str = "",
    discussion_id: str = "",
) -> None:
    if TASK_SKILL_ID not in (employee.get("skills") or []):
        return
    agent_id = employee.get("agentscope_agent_id")
    if not client.configured or not agent_id or not session_id:
        return
    try:
        await client.upload_workspace_skill(agent_id, session_id, skill_directory())
        await client.add_workspace_mcp(
            agent_id,
            session_id,
            url=task_mcp_url(),
            headers=task_mcp_headers(
                employee["id"],
                session_id,
                conversation_id=conversation_id,
                channel_id=channel_id,
                discussion_id=discussion_id,
            ),
        )
    except AgentScopeError as exc:
        logger.warning("任务技能或 MCP 未挂上：%s", exc.detail)


def _is_task_skill(item: dict) -> bool:
    skill_name = str(item.get("name") or "").strip()
    directory = str(item.get("dir") or item.get("path") or "").replace("\\", "/").rstrip("/")
    folder = directory.rsplit("/", 1)[-1]
    return skill_name in {TASK_SKILL_NAME, TASK_SKILL_ID} or folder == TASK_SKILL_ID


async def revoke_task_skill(client: AgentScopeClient, agent_id: str, session_id: str) -> None:
    """按 AgentScope 保存的技能名删除。本地目录 id 和 frontmatter 名称不是同一个值。"""

    listed = await client.list_workspace_skills(agent_id, session_id)
    targets: list[str] = []
    for item in listed:
        skill_name = str(item.get("name") or "").strip()
        if skill_name and _is_task_skill(item) and skill_name not in targets:
            targets.append(skill_name)
    if TASK_SKILL_NAME not in targets:
        targets.append(TASK_SKILL_NAME)
    for skill_name in targets:
        await client.remove_workspace_skill(agent_id, session_id, skill_name)
    listed = await client.list_workspace_skills(agent_id, session_id)
    remaining = [item for item in listed if _is_task_skill(item)]
    if remaining:
        raise AgentScopeError("任务技能收回后仍在工作区")


async def sync_task_capability(client: AgentScopeClient, employee: dict, sessions: list[dict]) -> None:
    """员工保存时安装或收回任务技能。收回要覆盖已有 Session，重新勾选后再装回去。"""

    agent_id = employee.get("agentscope_agent_id")
    if not client.configured or not agent_id:
        return
    usable = [item for item in sessions if (item.get("session_id") or "").strip()]
    if not usable:
        return
    enabled = TASK_SKILL_ID in (employee.get("skills") or [])
    if enabled:
        try:
            await client.upload_workspace_skill(agent_id, usable[0]["session_id"], skill_directory())
        except AgentScopeError as exc:
            logger.warning("任务技能未装上：%s", exc.detail)
        for item in usable:
            try:
                if not item.get("conversation_id") and not item.get("channel_id"):
                    installed = await client.list_workspace_mcp(agent_id, item["session_id"])
                    if any(m.get("name") == "nexus-task" for m in installed):
                        await client.remove_workspace_mcp(agent_id, item["session_id"])
                    continue
                await client.add_workspace_mcp(
                    agent_id,
                    item["session_id"],
                    url=task_mcp_url(),
                    headers=task_mcp_headers(
                        employee["id"],
                        item["session_id"],
                        conversation_id=item.get("conversation_id") or "",
                        channel_id=item.get("channel_id") or "",
                    ),
                )
            except AgentScopeError as exc:
                logger.warning("任务 MCP 未挂上：%s", exc.detail)
        return
    try:
        await revoke_task_skill(client, agent_id, usable[0]["session_id"])
    except AgentScopeError as exc:
        logger.warning("任务技能未收回：%s", exc.detail)
    for item in usable:
        try:
            await client.remove_workspace_mcp(agent_id, item["session_id"])
        except AgentScopeError as exc:
            logger.warning("任务 MCP 未收回：%s", exc.detail)

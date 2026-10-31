"""内置任务能力：技能说明和 MCP 连接分别挂接，失败要能被员工配置页看到。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .agentscope import AgentScopeClient, AgentScopeError

TASK_SKILL_ID = "task-management"
TASK_SKILL_NAME = "任务管理"
TASK_MCP_NAME = "nexus-task"
logger = logging.getLogger("nexus.task_skill")


def skill_directory() -> Path:
    return Path(__file__).resolve().parents[2] / "agent-service" / "skills" / "task-management"


def task_mcp_url() -> str:
    from app.config.settings import get_settings

    explicit = os.environ.get("NEXUS_TASK_MCP_URL", "").strip()
    return explicit or f"http://127.0.0.1:{get_settings().task_mcp_port}/mcp"


def task_mcp_headers(
    employee_id: str,
    session_id: str,
    *,
    conversation_id: str = "",
    channel_id: str = "",
    discussion_id: str = "",
) -> dict[str, str]:
    """Nexus 挂接时写入的会话来源。Task MCP 在工具调用时用它重验业务上下文。"""

    return {
        "X-Nexus-Employee-Id": employee_id,
        "X-Nexus-Session-Id": session_id,
        "X-Nexus-Conversation-Id": conversation_id,
        "X-Nexus-Channel-Id": channel_id,
        "X-Nexus-Discussion-Id": discussion_id,
    }


def _has_business_scope(item: dict[str, Any]) -> bool:
    """任务 MCP 只装入私聊和群会话；邮箱没有可信的人类发起者。"""

    return bool(
        str(item.get("conversation_id") or "").strip()
        or str(item.get("channel_id") or "").strip()
    )


def _headers(employee: dict[str, Any], item: dict[str, Any]) -> dict[str, str]:
    return task_mcp_headers(
        str(employee["id"]),
        str(item["session_id"]),
        conversation_id=str(item.get("conversation_id") or ""),
        channel_id=str(item.get("channel_id") or ""),
        discussion_id=str(item.get("discussion_id") or ""),
    )


async def attach_task_capability(
    client: AgentScopeClient,
    employee: dict,
    session_id: str,
    *,
    conversation_id: str = "",
    channel_id: str = "",
    discussion_id: str = "",
    strict: bool = False,
) -> None:
    """一次会话开始前确保技能和 MCP 都在；两者互不阻塞。"""

    if TASK_SKILL_ID not in (employee.get("skills") or []):
        return
    agent_id = employee.get("agentscope_agent_id")
    if not client.configured or not agent_id or not session_id:
        return
    failures: list[str] = []
    try:
        await client.upload_workspace_skill(agent_id, session_id, skill_directory())
    except AgentScopeError as exc:
        failures.append(f"任务技能未上传：{exc.detail}")

    if conversation_id or channel_id:
        try:
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
                name=TASK_MCP_NAME,
            )
        except AgentScopeError as exc:
            failures.append(f"任务 MCP 注册失败：{exc.detail}")

    if not failures:
        return
    for failure in failures:
        logger.warning("任务能力未挂接：%s", failure)
    if strict:
        raise AgentScopeError("；".join(failures))


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


async def _discovery_error(client: AgentScopeClient, agent_id: str, session_id: str) -> str:
    """经 AgentScope 的工作区接口做真实连接和工具发现，而不是只相信注册返回。"""

    items = await client.list_workspace_mcp(agent_id, session_id)
    target = next(
        (item for item in items if str(item.get("name") or "") == TASK_MCP_NAME),
        None,
    )
    if target is None:
        return "AgentScope 工作区没有返回 nexus-task"
    if not target.get("is_healthy"):
        return str(target.get("error") or "MCP 连接失败")
    tools = target.get("tools")
    if not isinstance(tools, list) or not tools:
        return "MCP 工具发现返回空列表"
    return ""


async def sync_task_capability(
    client: AgentScopeClient,
    employee: dict,
    sessions: list[dict],
) -> dict[str, Any]:
    """员工保存或手动重试时收敛任务能力，并返回可直接展示的同步结果。"""

    enabled = TASK_SKILL_ID in (employee.get("skills") or [])
    agent_id = employee.get("agentscope_agent_id")
    usable = [item for item in sessions if str(item.get("session_id") or "").strip()]
    if not client.configured or not agent_id:
        return {
            "status": "pending" if enabled else "applied",
            "detail": "AgentScope 未连接，任务能力尚未核对" if enabled else "",
            "failures": [],
            "sessions": 0,
        }
    if not usable:
        return {
            "status": "error" if enabled else "applied",
            "detail": "员工还没有可挂接的会话" if enabled else "",
            "failures": [{"stage": "session", "detail": "员工还没有可挂接的会话"}] if enabled else [],
            "sessions": 0,
        }

    failures: list[dict[str, str]] = []
    targets = [item for item in usable if _has_business_scope(item)]
    if enabled:
        # 技能说明只装到第一个会话供员工配置页核对；各会话使用前由
        # attach_task_capability 再装。它失败不能挡住 MCP 注册。
        try:
            await client.upload_workspace_skill(agent_id, str(usable[0]["session_id"]), skill_directory())
        except AgentScopeError as exc:
            failures.append({"stage": "skill_upload", "detail": exc.detail})

        # 邮箱没有可信的人类发起者，历史上误挂的 Task MCP 要收回。
        for item in usable:
            if _has_business_scope(item):
                continue
            try:
                installed = await client.list_workspace_mcp(agent_id, str(item["session_id"]))
                if any(str(row.get("name") or "") == TASK_MCP_NAME for row in installed):
                    await client.remove_workspace_mcp(agent_id, str(item["session_id"]), TASK_MCP_NAME)
            except AgentScopeError as exc:
                failures.append(
                    {
                        "stage": "mcp_remove",
                        "session_id": str(item["session_id"]),
                        "detail": exc.detail,
                    }
                )

        for item in targets:
            session_id = str(item["session_id"])
            try:
                await client.add_workspace_mcp(
                    agent_id,
                    session_id,
                    url=task_mcp_url(),
                    headers=_headers(employee, item),
                    name=TASK_MCP_NAME,
                )
            except AgentScopeError as exc:
                failures.append(
                    {
                        "stage": "mcp_register",
                        "session_id": session_id,
                        "detail": exc.detail,
                    }
                )
                continue
            try:
                detail = await _discovery_error(client, agent_id, session_id)
            except AgentScopeError as exc:
                detail = exc.detail
            if detail:
                failures.append(
                    {
                        "stage": "mcp_discovery",
                        "session_id": session_id,
                        "detail": detail,
                    }
                )
        returned_sessions = len(targets)
    else:
        for item in usable:
            session_id = str(item["session_id"])
            try:
                await revoke_task_skill(client, agent_id, session_id)
            except AgentScopeError as exc:
                failures.append(
                    {
                        "stage": "skill_revoke",
                        "session_id": session_id,
                        "detail": exc.detail,
                    }
                )
            try:
                await client.remove_workspace_mcp(agent_id, session_id, TASK_MCP_NAME)
            except AgentScopeError as exc:
                failures.append(
                    {
                        "stage": "mcp_remove",
                        "session_id": session_id,
                        "detail": exc.detail,
                    }
                )
        returned_sessions = len(usable)

    labels = {
        "session": "没有可用会话",
        "skill_upload": "任务技能上传失败",
        "skill_revoke": "任务技能收回失败",
        "mcp_register": "任务 MCP 注册失败",
        "mcp_discovery": "任务 MCP 连接或工具发现失败",
        "mcp_remove": "任务 MCP 收回失败",
    }
    detail = "；".join(
        dict.fromkeys(
            f"{labels.get(item['stage'], item['stage'])}：{item['detail']}"
            for item in failures
        )
    )[:800]
    return {
        "status": "error" if failures else "applied",
        "detail": detail,
        "failures": failures,
        "sessions": returned_sessions,
    }

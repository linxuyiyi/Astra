"""员工绑定的 desired/applied。放在员工 JSON 里，不新建表。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .agentscope import AgentScopeClient, AgentScopeError
from .task_skill import TASK_SKILL_ID, TASK_SKILL_NAME


def binding_revision(names: list[str]) -> str:
    payload = json.dumps(sorted({name for name in names if name}), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _names(items: list[dict[str, Any]]) -> set[str]:
    return {str(item.get("name") or item.get("id") or "") for item in items if isinstance(item, dict)}


def _skill_names(items: list[dict[str, Any]]) -> dict[str, str]:
    """技能库记录 id 到显示名的映射。缺 id 的旧响应仍然按 name 参与比对。"""

    names: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("id") or item.get("skill_id") or "")
        if not name:
            continue
        for key in ("id", "skill_id", "name"):
            value = str(item.get(key) or "")
            if value:
                names.setdefault(value, name)
    return names


def missing_bindings(
    expected: list[str],
    catalog: set[str],
    workspace: set[str],
    toolkit: set[str],
) -> list[str]:
    return [name for name in expected if name not in catalog or name not in workspace or name not in toolkit]


def settle(
    current: dict[str, Any] | None,
    *,
    desired: str,
    missing: list[str],
    error: str,
    checked_at: str,
) -> dict[str, Any]:
    record = dict(current or {})
    previous = str(record.get("applied_revision") or "")
    record["desired_revision"] = desired
    record["checked_at"] = checked_at
    if error:
        record["runtime_status"] = "error"
        record["error"] = error
        record["applied_revision"] = previous
        return record
    if missing:
        record["runtime_status"] = "error"
        record["error"] = "未发现：" + "、".join(missing)
        record["applied_revision"] = previous
        return record
    record["applied_revision"] = desired
    record["runtime_status"] = "applied"
    record["error"] = ""
    return record


async def inspect_employee(
    client: AgentScopeClient,
    employee: dict[str, Any],
    checked_at: str,
    *,
    custom_skills: dict[str, str] | None = None,
    custom_mcps: dict[str, str] | None = None,
) -> dict[str, Any]:
    """核对员工在邮箱会话里的实际资源。

    Nexus 自建资源按数据库记录解析运行名；AgentScope 已安装资源按原生 id 解析。
    停用的资源不进入期望集合，但保留在绑定里等重新启用。
    """

    skill_ids = [str(name) for name in employee.get("skills", []) if name]
    mcp_ids = [str(name) for name in employee.get("mcps", []) if name]
    tools = [str(name) for name in employee.get("tools", []) if name]
    custom_skills = custom_skills or {}
    custom_mcps = custom_mcps or {}
    desired = binding_revision([*skill_ids, *mcp_ids, *tools])
    current = employee.get("capability") if isinstance(employee.get("capability"), dict) else {}
    if not skill_ids and not mcp_ids and not tools:
        return settle(current, desired=desired, missing=[], error="", checked_at=checked_at)
    agent_id = employee.get("agentscope_agent_id") or ""
    session_id = employee.get("mailbox_session_id") or ""
    if not client.configured or not agent_id or not session_id:
        return settle(current, desired=desired, missing=[], error="绑定尚未核对", checked_at=checked_at)
    try:
        catalog_items = await client.list_skill_records()
        user_mcp = await client.list_mcp_records()
        workspace_skill_items = await client.list_workspace_skills(agent_id, session_id)
        workspace_mcp = await client.list_workspace_mcp(agent_id, session_id)
    except AgentScopeError as exc:
        return settle(current, desired=desired, missing=[], error=exc.detail, checked_at=checked_at)
    skill_names = _skill_names(catalog_items)
    catalog = _names(catalog_items)
    mcp_by_id = {
        str(item.get("id") or ""): str(item.get("name") or "")
        for item in user_mcp
        if item.get("id")
    }
    workspace_skills = _names(workspace_skill_items)
    mcp_names = _names(workspace_mcp)
    tool_names: set[str] = set()
    for item in workspace_mcp:
        tools_in_mcp = item.get("tools") if isinstance(item, dict) else None
        if isinstance(tools_in_mcp, list):
            tool_names.update(_names(tools_in_mcp))

    expected_skills: list[str] = []
    expected_mcps: list[str] = []
    missing: list[str] = []
    for skill_id in skill_ids:
        if skill_id == TASK_SKILL_ID:
            name = next(
                (item for item in (TASK_SKILL_NAME, TASK_SKILL_ID) if item in workspace_skills),
                TASK_SKILL_NAME,
            )
            expected_skills.append(name)
            catalog.add(name)
            continue
        if skill_id.startswith("nsk_"):
            name = custom_skills.get(skill_id, "")
            if name:
                expected_skills.append(name)
                # 自建技能不在 AgentScope 技能库，只在工作区里核对。
                catalog.add(name)
            # 停用或已删除的自建技能不再要求出现在运行环境里。
            continue
        name = skill_names.get(skill_id, "")
        if not name:
            missing.append(skill_id)
            continue
        expected_skills.append(name)
        catalog.add(name)
    for mcp_id in mcp_ids:
        if mcp_id.startswith("nmp_"):
            name = custom_mcps.get(mcp_id, "")
            if name:
                expected_mcps.append(name)
            continue
        name = mcp_by_id.get(mcp_id, "")
        if not name:
            missing.append(mcp_id)
            continue
        expected_mcps.append(name)

    for name in expected_skills:
        if name not in workspace_skills or name not in catalog:
            missing.append(name)
    for name in expected_mcps:
        if name not in mcp_names:
            missing.append(name)
    return settle(
        current,
        desired=desired,
        missing=missing,
        error="",
        checked_at=checked_at,
    )

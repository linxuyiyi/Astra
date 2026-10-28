"""员工当前工具绑定的共同权限规则；会话来源由各入口单独验证。"""

from fastapi import HTTPException
from sqlalchemy import text

from .task_skill import TASK_SKILL_ID, task_mcp_url


def employee_tool_policy(db, employee):
    if not employee["enabled"]:
        raise HTTPException(403, "员工已禁用")
    mcp_names = {}
    for ref in employee["mcps"]:
        row = (
            db.execute(text("SELECT runtime_name,enabled FROM t_nexus_mcps WHERE id=:i"), {"i": ref})
            .mappings()
            .first()
        )
        if row is None:
            mcp_names[ref] = ""
        elif row["enabled"]:
            mcp_names[ref] = row["runtime_name"]
    return {
        "allowed_tools": list(employee["tools"]),
        "mcp_refs": mcp_names,
        "skill_viewer": bool(employee["skills"]),
        "task_allowed": TASK_SKILL_ID in employee["skills"],
        "task_mcp_url": task_mcp_url(),
    }


def require_employee_tool(policy, tool_name, mcp_ref):
    if tool_name and not (
        tool_name in policy["allowed_tools"]
        or (tool_name == "Skill" and policy["skill_viewer"])
        or (
            mcp_ref == "__task__"
            and policy["task_allowed"]
            and tool_name.startswith("mcp__nexus-task__nexus_task_")
        )
        or mcp_ref in policy["mcp_refs"]
    ):
        raise HTTPException(403, "用户权限与员工岗位权限不允许该工具")

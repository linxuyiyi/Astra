"""官方日程 Session 的员工资源收敛和权限；不保存第二份会话归属。"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from .agentscope import AgentScopeClient, AgentScopeError
from .auth import require_internal
from .db import connect, employee_from_row
from .resources import RETIRE_PREFIX, sync_employee_resources
from .task_skill import TASK_SKILL_ID, TASK_SKILL_NAME, skill_directory, task_mcp_headers, task_mcp_url
from .tool_policy import employee_tool_policy, require_employee_tool

logger = logging.getLogger("nexus.schedule_resources")


class ScheduleSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(pattern=r"^nexus-schedule-[0-9a-f]{64}$")
    agent_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)


class ScheduleResourceClient(AgentScopeClient):
    """目录属于平台；工作区和 Session 属于日程安排者。"""

    def __init__(self, parent, user_id):
        super().__init__(parent.base_url, parent.user_id, parent._client())
        self.schedule_user_id = user_id

    async def request(self, method, path, **kwargs):
        scoped = path.startswith(("/workspace/", "/sessions/", "/nexus/schedule-"))
        if path in {"/workspace/mcp", "/workspace/skill"}:
            scoped = True
        if path in {"/workspace/skill/from-library", "/workspace/mcp/from-library"}:
            kind = path.split("/")[2]
            path = f"/nexus/schedule-library/{kind}"
        headers = dict(kwargs.pop("headers", {}))
        if scoped:
            headers["X-User-ID"] = self.schedule_user_id
        return await super().request(method, path, headers=headers, **kwargs)

    async def set_permission_mode(self, agent_id, session_id, mode):
        # 准备日程不能吞掉 PATCH 失败，否则后续 MCP 权限可能仍是 DENY。
        await self.request(
            "PATCH", f"/sessions/{session_id}", params={"agent_id": agent_id}, json={"permission_mode": mode}
        )


async def schedule_employee(client, context: ScheduleSession):
    response = await client.request(
        "GET",
        "/nexus/schedule-session",
        headers={"X-User-ID": context.user_id},
        params={"agent_id": context.agent_id, "session_id": context.session_id},
    )
    source = response.json()
    if any(source.get(k) != getattr(context, k) for k in ("user_id", "agent_id", "session_id")):
        raise HTTPException(403, "日程会话归属不匹配")
    if not source.get("schedule_id"):
        raise HTTPException(403, "日程来源无效")
    with connect() as db:
        row = (
            db.execute(
                text("SELECT * FROM t_nexus_employees WHERE agentscope_agent_id=:a"), {"a": context.agent_id}
            )
            .mappings()
            .first()
        )
    if row is None or not row["enabled"]:
        raise HTTPException(403, "日程员工已禁用或不存在")
    return employee_from_row(row), source


async def schedule_policy(client, context, tool_name="", mcp_ref=""):
    employee, source = await schedule_employee(client, context)
    with connect() as db:
        tools_policy = employee_tool_policy(db, employee)
    mcp_names = tools_policy["mcp_refs"]
    unresolved = {ref for ref, name in mcp_names.items() if not name}
    if unresolved:
        records = {str(item["id"]): item for item in await client.list_mcp_records()}
        for ref in unresolved:
            record = records.get(ref)
            if record and record.get("enabled", True):
                mcp_names[ref] = str(record["name"])
            else:
                mcp_names.pop(ref, None)
    require_employee_tool(tools_policy, tool_name, mcp_ref)
    return {
        "managed": True,
        **tools_policy,
        "initiator_id": context.user_id,
        "employee_id": employee["id"],
        "discussion_id": "",
        "generation": 0,
        "schedule_id": source["schedule_id"],
    }


def is_task_skill(item):
    return bool(re.fullmatch(re.escape(TASK_SKILL_NAME) + r"(?: \(\d+\))?", str(item.get("name", ""))))


async def prepare_schedule(client, context, store):
    employee, source = await schedule_employee(client, context)
    scoped = ScheduleResourceClient(client, context.user_id)
    if await scoped.session_status(context.agent_id, context.session_id) == "running":
        raise HTTPException(409, "日程会话正在运行，不能修改资源")
    with connect() as db:
        rows = (
            db.execute(
                text("""SELECT resource_type,resource_id FROM t_nexus_resource_sync
            WHERE employee_id=:e AND session_id=:s"""),
                {"e": employee["id"], "s": context.session_id},
            )
            .mappings()
            .all()
        )
        rows = [r for r in rows if not r["resource_id"].startswith(RETIRE_PREFIX)]
        previous = {
            "skills": [r["resource_id"] for r in rows if r["resource_type"] == "skill"],
            "mcps": [r["resource_id"] for r in rows if r["resource_type"] == "mcp"],
        }
        result = await sync_employee_resources(scoped, employee, [context.session_id], db, store, previous)
    if not result["ok"]:
        # 详细且脱敏的资源状态已由通用同步器保留，接口不输出 MCP 凭据。
        raise HTTPException(502, "日程员工资源尚未同步完成")
    installed = await scoped.list_workspace_skills(context.agent_id, context.session_id)
    tasks = [item for item in installed if is_task_skill(item)]
    if TASK_SKILL_ID in employee["skills"]:
        if not tasks:
            try:
                await scoped.upload_workspace_skill(
                    context.agent_id,
                    context.session_id,
                    skill_directory(),
                )
            except AgentScopeError as exc:
                # 技能说明和 MCP 连接是两件事，重复上传失败不挡住任务工具。
                logger.warning("日程会话任务技能未上传：%s", exc.detail)
        headers = task_mcp_headers(employee["id"], context.session_id)
        headers["X-Nexus-Schedule-User-Id"] = context.user_id
        headers["X-Nexus-Schedule-Agent-Id"] = context.agent_id
        await scoped.add_workspace_mcp(
            context.agent_id, context.session_id, url=task_mcp_url(), headers=headers
        )
    else:
        for item in tasks:
            await scoped.remove_workspace_skill(context.agent_id, context.session_id, item["name"])
        installed_mcps = await scoped.list_workspace_mcp(context.agent_id, context.session_id)
        if any(item.get("name") == "nexus-task" for item in installed_mcps):
            await scoped.remove_workspace_mcp(context.agent_id, context.session_id)
    await scoped.set_permission_mode(
        context.agent_id, context.session_id, "bypass" if employee["mcps"] else "dont_ask"
    )
    return {"ok": True, "schedule_id": source["schedule_id"]}


def schedule_resources_router():
    router = APIRouter(prefix="/api/v1/internal/schedule-sessions", dependencies=[Depends(require_internal)])

    @router.post("/prepare")
    async def prepare(body: ScheduleSession, request: Request):
        try:
            return await prepare_schedule(request.app.state.agentscope, body, request.app.state.resources)
        except AgentScopeError as exc:
            raise HTTPException(502, "日程资源准备的 AgentScope 请求失败") from exc

    @router.get("/policy")
    async def policy(
        request: Request, context: ScheduleSession = Depends(), tool_name: str = "", mcp_ref: str = ""
    ):
        try:
            return await schedule_policy(request.app.state.agentscope, context, tool_name, mcp_ref)
        except AgentScopeError as exc:
            raise HTTPException(502, "日程来源验证失败") from exc

    return router

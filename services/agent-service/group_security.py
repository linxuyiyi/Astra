"""按原始运行上下文校验工具；会话复用不能借用下一位发起者的授权。"""
from __future__ import annotations

import hmac
import os
from contextvars import ContextVar
from copy import deepcopy

import httpx
from starlette.responses import JSONResponse

execution_id: ContextVar[str] = ContextVar("nexus_execution_id", default="")


def headers():
    return {"X-Nexus-Internal-Key": os.environ.get("NEXUS_INTERNAL_KEY", "")}


def install_run_context(chat_service_class):
    original = chat_service_class.run
    async def run(self, user_id, session_id, agent_id, input_msg=None, sop_dispatch=None):
        inputs = input_msg if isinstance(input_msg, list) else [input_msg]
        fence = ""
        for msg in inputs:
            metadata = getattr(msg, "metadata", None) or {}
            if metadata.get("discussion_id"):
                fence = str(metadata["discussion_id"])
        token = execution_id.set(fence)
        try:
            return await original(self, user_id, session_id, agent_id, input_msg, sop_dispatch)
        finally:
            execution_id.reset(token)
    chat_service_class.run = run


class AgentServiceKeyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") != "/health":
            presented = dict(scope["headers"]).get(b"x-nexus-internal-key", b"").decode()
            expected = headers()["X-Nexus-Internal-Key"]
            if not expected or not hmac.compare_digest(presented, expected):
                return await JSONResponse({"detail": "服务间认证失败"}, status_code=401)(scope, receive, send)
        await self.app(scope, receive, send)


def secure_group_toolkit(factory, nexus_api, policy_loader=None):
    async def load(session_id, tool_name="", fence="", mcp_ref=""):
        from schedule_resources import schedule_run

        context = schedule_run.get()
        if policy_loader:
            return await policy_loader(session_id, tool_name, fence, mcp_ref)
        async with httpx.AsyncClient(timeout=10) as client:
            if context:
                response = await client.get(f"{nexus_api}/api/v1/internal/schedule-sessions/policy", params={
                    "session_id": session_id, "user_id": context.user_id, "agent_id": context.agent_id,
                    "tool_name": tool_name, "mcp_ref": mcp_ref}, headers=headers())
            else:
                response = await client.get(f"{nexus_api}/api/v1/internal/group-policy", params={
                    "session_id":session_id,"tool_name":tool_name,"execution_id":fence,"mcp_ref":mcp_ref}, headers=headers())
        response.raise_for_status()
        return response.json()

    async def assembled(**kwargs):
        from schedule_resources import schedule_run

        context = schedule_run.get()
        if context and context.interrupt:
            return await factory(**kwargs)
        sid = kwargs["session_record"].id
        fence = execution_id.get()
        policy = await load(sid, fence=fence)
        # 群调用必须来自此次 /chat/ 原始输入，不能从复用 Session 猜测发起者。
        if policy.get("generation") and (not fence or fence != policy["discussion_id"]):
            raise PermissionError("群讨论运行来源无效")
        if policy.get("generation"):
            # 装配器可能共享员工记录；群预算只作用于本次会话。
            kwargs["agent_record"] = deepcopy(kwargs["agent_record"])
            kwargs["agent_record"].data.chat_config.react_config.max_iters = 8
        toolkit = await factory(**kwargs)
        original = toolkit._get_available_tools
        original_call = toolkit.call_tool

        async def mcp_ref_for(tool, current):
            if not getattr(tool, "is_mcp", False):
                return ""
            for ref, runtime_name in current.get("mcp_refs", {}).items():
                if not runtime_name:
                    record = await kwargs["storage"].get_mcp(kwargs["user_id"], ref)
                    runtime_name = record.name if record else ""
                if runtime_name == tool.mcp_name:
                    return ref
            return ""

        async def origin_ref(name, registered, current):
            tool = registered.tool
            if not getattr(tool, "is_mcp", False):
                return ""
            if tool.mcp_name == "nexus-task" and name.startswith("mcp__nexus-task__nexus_task_"):
                clients = [client for group in toolkit.tool_groups for client in group.mcps if client.name == "nexus-task"]
                if len(clients) == 1:
                    config = clients[0].mcp_config
                    if (getattr(config, "url", "") == current.get("task_mcp_url")
                        and getattr(config, "headers", {}).get("X-Nexus-Session-Id") == sid
                        and getattr(config, "headers", {}).get("X-Nexus-Discussion-Id", "") == fence):
                        return "__task__" if current.get("task_allowed") else ""
            return await mcp_ref_for(tool, current)

        async def permitted(name, registered, current):
            tool = registered.tool
            if getattr(tool, "is_mcp", False):
                return bool(await origin_ref(name, registered, current))
            if name == "Skill":
                from agentscope.tool._builtin._skill import SkillViewer
                return isinstance(tool, SkillViewer) and bool(current.get("skill_viewer"))
            return name in current.get("allowed_tools", [])

        async def available(groups):
            current = await load(sid, fence=fence)
            result = await original(groups)
            return {name: registered for name, registered in result.items() if await permitted(name, registered, current)}

        async def call_tool(tool_call, state):
            current = await load(sid, fence=fence)
            registered = (await original([g.name for g in toolkit.tool_groups])).get(tool_call.name)
            ref = await origin_ref(tool_call.name, registered, current) if registered else ""
            if not registered or not await permitted(tool_call.name, registered, current):
                raise PermissionError("工具来源没有授权")
            await load(sid, tool_call.name, fence, ref)
            async for chunk in original_call(tool_call, state):
                yield chunk

        toolkit._get_available_tools = available
        toolkit.call_tool = call_tool
        return toolkit
    return assembled

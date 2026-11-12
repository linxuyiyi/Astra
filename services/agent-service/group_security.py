"""按原始运行上下文校验工具；会话复用不能借用下一位发起者的授权。"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path

import httpx
from starlette.responses import JSONResponse

execution_id: ContextVar[str] = ContextVar("nexus_execution_id", default="")
delivery_only: ContextVar[bool] = ContextVar("nexus_delivery_only", default=False)


def headers():
    return {"X-Nexus-Internal-Key": os.environ.get("NEXUS_INTERNAL_KEY", "")}


def install_run_context(chat_service_class):
    original = chat_service_class.run
    # AgentScope serializes the run itself with Redis. This lock orders
    # receipt checks with the run in the Agent Service process.
    locks = tuple(asyncio.Lock() for _ in range(128))

    async def run(self, user_id, session_id, agent_id, input_msg=None, sop_dispatch=None):
        inputs = input_msg if isinstance(input_msg, list) else [input_msg]
        fence = ""
        staging = False
        for msg in inputs:
            metadata = getattr(msg, "metadata", None) or {}
            if metadata.get("discussion_id"):
                fence = str(metadata["discussion_id"])
                staging = metadata.get("delivery_only") is True
        token = execution_id.set(fence)
        delivery_token = delivery_only.set(staging)
        try:
            if not any((getattr(msg, "metadata", None) or {}).get("nexus_deduplicate") for msg in inputs):
                return await original(self, user_id, session_id, agent_id, input_msg, sop_dispatch)
            async with locks[hash((user_id, session_id)) % len(locks)]:
                pending = []
                for msg in inputs:
                    metadata = getattr(msg, "metadata", None) or {}
                    if metadata.get("nexus_deduplicate") and await self._storage.get_message(
                        user_id, session_id, msg.id,
                    ):
                        continue
                    pending.append(msg)
                if not pending or (not fence and not any(
                    (getattr(msg, "metadata", None) or {}).get("nexus_deduplicate") for msg in pending
                )):
                    return None
                return await original(self, user_id, session_id, agent_id, pending, sop_dispatch)
        finally:
            delivery_only.reset(delivery_token)
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


_LOCAL_FILE_ARGUMENT = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "Grep": "path", "Glob": "path"}


def workspace_file_call(tool_call, workdir):
    field = _LOCAL_FILE_ARGUMENT[tool_call.name]
    args = json.loads(tool_call.input)
    root = Path(workdir).resolve(strict=True)
    supplied = Path(args.get(field) or ".")
    if ".." in supplied.parts:
        raise PermissionError("文件路径越出当前工作区")
    target = (root / supplied).resolve()
    if not target.is_relative_to(root):
        raise PermissionError("文件路径越出当前工作区")
    if target.is_relative_to(root / "attachments") or (tool_call.name == "Grep" and target == root):
        raise PermissionError("请使用 read_chat_attachment 读取会话附件")
    args[field] = str(target)
    return tool_call.model_copy(update={"input": json.dumps(args)})


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
            return await mcp_ref_for(tool, current)

        async def permitted(name, registered, current):
            if delivery_only.get():
                return False
            tool = registered.tool
            if current.get("task_source") and name in {"Bash", "PowerShell"} and not getattr(tool, "is_mcp", False):
                # LocalBackend executes on the host without a filesystem sandbox.
                return False
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
            authorization = await load(sid, tool_call.name, fence, ref)
            if authorization.get("task_source") and tool_call.name in _LOCAL_FILE_ARGUMENT and not ref:
                tool_call = workspace_file_call(tool_call, kwargs["workspace"].workdir)
            tool = registered.tool
            is_task_create = tool_call.name == "nexus_task_create" or (
                bool(ref) and tool_call.name == f"mcp__{tool.mcp_name}__nexus_task_create"
            )
            if is_task_create and authorization.get("task_source"):
                arguments = json.loads(tool_call.input)
                arguments.update(authorization["task_source"])
                tool_call = tool_call.model_copy(update={"input": json.dumps(arguments)})
            async for chunk in original_call(tool_call, state):
                yield chunk

        toolkit._get_available_tools = available
        toolkit.call_tool = call_tool
        return toolkit
    return assembled


def install_nexus_runtime(chat_service, realtime_service, toolkit_factory, nexus_api):
    """AgentScope 2.0.10 运行入口的授权、去重与日程准备。"""
    from schedule_resources import install_schedule_preparation, schedule_toolkit

    install_run_context(chat_service.ChatService)
    install_schedule_preparation(chat_service.ChatService, nexus_api)
    toolkit = schedule_toolkit(secure_group_toolkit(toolkit_factory, nexus_api))
    chat_service.get_toolkit = toolkit
    realtime_service.get_toolkit = toolkit

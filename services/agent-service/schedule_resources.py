"""日程运行的资源准备适配；官方 scheduler、Session 和运行状态保持唯一事实源。"""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request

logger = logging.getLogger("nexus.schedule_resources")


@dataclass
class ScheduleRun:
    user_id: str
    agent_id: str
    session_id: str
    error: str = ""
    interrupt: bool = False


schedule_run: ContextVar[ScheduleRun | None] = ContextVar(
    "nexus_schedule_run", default=None
)


def is_schedule_user(user_id: str) -> bool:
    prefix = "nexus-schedule-"
    suffix = user_id.removeprefix(prefix)
    return (
        user_id.startswith(prefix)
        and len(suffix) == 64
        and all(c in "0123456789abcdef" for c in suffix)
    )


async def schedule_session(storage, user_id: str, agent_id: str, session_id: str):
    from agentscope.app.storage import ScheduleOrigin

    if not is_schedule_user(user_id):
        raise HTTPException(404, "日程会话不存在")
    session = await storage.get_session(user_id, agent_id, session_id)
    if session is None or not isinstance(session.origin, ScheduleOrigin):
        raise HTTPException(404, "日程会话不存在")
    schedule = await storage.get_schedule(user_id, session.origin.schedule_id)
    if schedule is None or schedule.agent_id != agent_id:
        raise HTTPException(404, "日程来源不匹配")
    return session, schedule


def install_schedule_preparation(chat_service_class, nexus_api: str) -> None:
    """运行前同步工作区；失败留给官方装配错误处理，不调用模型。"""
    from agentscope.app.storage import ScheduleOrigin
    from agentscope.event import UserInterruptEvent
    from group_security import headers

    original = chat_service_class.run

    async def run(
        self, user_id, session_id, agent_id, input_msg=None, sop_dispatch=None
    ):
        if not is_schedule_user(user_id):
            return await original(
                self, user_id, session_id, agent_id, input_msg, sop_dispatch
            )
        session = await self._storage.get_session(user_id, agent_id, session_id)
        if session is None or not isinstance(session.origin, ScheduleOrigin):
            return await original(
                self, user_id, session_id, agent_id, input_msg, sop_dispatch
            )
        context = ScheduleRun(
            user_id,
            agent_id,
            session_id,
            interrupt=isinstance(input_msg, UserInterruptEvent),
        )
        token = schedule_run.set(context)
        try:
            if context.interrupt:
                return await original(
                    self, user_id, session_id, agent_id, input_msg, sop_dispatch
                )
            async with AsyncExitStack() as stack:
                try:
                    # 准备与整次运行共用互斥，下一次唤醒不能在本次推理中途拆改资源。
                    await stack.enter_async_context(
                        self._message_bus.acquire_lock(
                            f"nexus:schedule-prepare:{session_id}", ttl_secs=300
                        )
                    )
                    async with httpx.AsyncClient(timeout=180) as client:
                        response = await client.post(
                            f"{nexus_api}/api/v1/internal/schedule-sessions/prepare",
                            headers=headers(),
                            json={
                                "user_id": user_id,
                                "agent_id": agent_id,
                                "session_id": session_id,
                            },
                        )
                    response.raise_for_status()
                    if response.json().get("ok") is not True:
                        raise RuntimeError("日程资源未就绪")
                except Exception as exc:  # noqa: BLE001 - 交给官方装配错误处理，禁止继续推理
                    # 不把 MCP URL、Authorization 或原始上游响应写入对话。
                    context.error = "日程资源准备失败，请检查员工资源与服务连接"
                    logger.warning(
                        "日程资源准备失败 session=%s error_type=%s status=%s",
                        session_id,
                        type(exc).__name__,
                        exc.response.status_code
                        if isinstance(exc, httpx.HTTPStatusError)
                        else None,
                    )
                return await original(
                    self, user_id, session_id, agent_id, input_msg, sop_dispatch
                )
        finally:
            schedule_run.reset(token)

    chat_service_class.run = run


def schedule_toolkit(factory):
    async def assembled(**kwargs):
        context = schedule_run.get()
        if context and context.error:
            raise RuntimeError(context.error)
        toolkit = await factory(**kwargs)
        if context and not context.interrupt:
            kwargs["session_record"].state.tool_context.activated_groups = [
                g.name for g in toolkit.tool_groups
            ]
        return toolkit

    return assembled


class PlatformLibraryView:
    """仅库记录读取使用平台 owner；所有 Session/工作区读取继续使用日程身份。"""

    def __init__(self, storage, owner_id):
        self.storage = storage
        self.owner_id = owner_id

    async def get_skill(self, user_id, resource_id):
        return await self.storage.get_skill(self.owner_id, resource_id)

    async def get_mcp(self, user_id, resource_id):
        return await self.storage.get_mcp(self.owner_id, resource_id)


def register_schedule_resources(app) -> None:
    from agentscope.app._router._schema import (
        AddFromLibraryRequest,
        AddSkillsFromLibraryRequest,
    )
    from agentscope.app._router._workspace import (
        add_mcps_from_library,
        add_skills_from_library,
    )

    @app.get("/nexus/schedule-session")
    async def source(request: Request, agent_id: str, session_id: str):
        user_id = request.headers.get("X-User-ID", "")
        session, schedule = await schedule_session(
            app.state.storage, user_id, agent_id, session_id
        )
        return {
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session.id,
            "schedule_id": schedule.id,
            "enabled": schedule.data.enabled,
        }

    @app.post("/nexus/schedule-library/{kind}")
    async def install(kind: str, request: Request, agent_id: str, session_id: str):
        user_id = request.headers.get("X-User-ID", "")
        await schedule_session(app.state.storage, user_id, agent_id, session_id)
        library = PlatformLibraryView(
            app.state.storage, os.environ.get("NEXUS_AGENTSCOPE_USER_ID", "nexus")
        )
        common = {
            "agent_id": agent_id,
            "session_id": session_id,
            "user_id": user_id,
            "storage": library,
            "workspace_service": app.state.workspace_service,
        }
        payload = await request.json()
        if kind == "skill":
            return await add_skills_from_library(
                body=AddSkillsFromLibraryRequest.model_validate(payload),
                skill_hubs=app.state.skill_hubs,
                **common,
            )
        if kind == "mcp":
            return await add_mcps_from_library(
                body=AddFromLibraryRequest.model_validate(payload), **common
            )
        raise HTTPException(404, "资源类型不存在")

"""AgentScope 2.0.10 没有只追加 Session 历史的 HTTP 路由。"""

import asyncio

from agentscope.app.deps import get_current_user_id
from agentscope.app.message_bus import MessageBusKeys
from agentscope.message import Msg
from chat_attachments import with_attachment_hint
from fastapi import Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field


class SessionMessages(BaseModel):
    agent_id: str
    session_id: str
    messages: list[Msg] = Field(max_length=128)


async def stage_messages(storage, bus, user_id, body):
    async with (
        bus.acquire_lock(f"nexus:retire:{body.session_id}"),
        bus.acquire_lock(MessageBusKeys.session_lock(body.session_id)),
    ):
        session = await storage.get_session(user_id, body.agent_id, body.session_id)
        if session is None or session.agent_id != body.agent_id:
            raise HTTPException(404, "会话不存在")
        if any(message.role != "user" for message in body.messages):
            raise HTTPException(422, "只允许补送原始用户消息")

        # 原生历史按 Msg.id 幂等。先保存上下文，再写可用于重试判断的消息日志。
        # 如果中途退出，下一次补齐日志时复用已存在的上下文项。
        context_ids = {message.id for message in session.state.context}
        pending = []
        changed = False
        for message in body.messages:
            if await storage.get_message(user_id, body.session_id, message.id):
                continue
            message = with_attachment_hint(message)
            pending.append(message)
            if message.id not in context_ids:
                session.state.context.append(message)
                context_ids.add(message.id)
                changed = True
        if changed:
            await storage.update_session_state(user_id, body.agent_id, body.session_id, session.state)
        for message in pending:
            await storage.upsert_message(user_id, body.session_id, message)


def register_session_messages(app, storage, bus):
    @app.post("/nexus/session-messages", status_code=204)
    async def append_session_history(body: SessionMessages, user_id: str = Depends(get_current_user_id)):
        await stage_messages(storage, bus, user_id, body)

    @app.get("/nexus/session-messages/{message_id}", status_code=204)
    async def accepted_message(
        message_id: str, agent_id: str, session_id: str, wait: bool = False,
        user_id: str = Depends(get_current_user_id),
    ):
        session = await storage.get_session(user_id, agent_id, session_id)
        if not session or session.agent_id != agent_id:
            raise HTTPException(404, "会话不存在")
        deadline = asyncio.get_running_loop().time() + (4 if wait else 0)
        while True:
            if await storage.get_message(user_id, session_id, message_id):
                return Response(status_code=204)
            if asyncio.get_running_loop().time() >= deadline:
                raise HTTPException(404, "消息尚未落盘")
            await asyncio.sleep(0.1)

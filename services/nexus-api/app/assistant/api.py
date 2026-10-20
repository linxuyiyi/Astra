"""平台助手 REST + SSE API。

浏览器只与 NEXUS Backend 通信：不直接访问 OpenCode、不解析 OpenCode 内部
Tool Call、不持有 OpenCode 密码。助手服务异常时入口仍然存在，但显示服务
异常，且不影响 Task / Employee 等平台核心能力。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.client import AssistantOpenCodeUnavailable
from app.assistant.models import AssistantConversation, AssistantOperation
from app.assistant.operations import OperationError, OperationService
from app.assistant.orchestrator import AssistantConversationBusy, get_orchestrator
from app.assistant.rate_limit import assistant_rate_limiter
from app.assistant.schemas import (
    AssistantHealth,
    ConversationCreate,
    ConversationRead,
    MessageRead,
    MessageWrite,
    OperationRead,
)
from app.assistant.service import ConversationService
from app.config.settings import get_settings
from app.database import get_session

router = APIRouter(prefix="/api/assistant")


def require_orchestrator():
    settings = get_settings()
    orchestrator = get_orchestrator()
    if not settings.assistant_enabled or orchestrator is None:
        raise HTTPException(status_code=503, detail="平台助手未启用")
    return orchestrator


async def require_conversation(conversation_id: str, session: AsyncSession) -> AssistantConversation:
    conversation = await ConversationService(session).get(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="助手会话不存在")
    return conversation


def operation_read(operation) -> OperationRead:
    return OperationRead(
        operation_id=operation.operation_id,
        conversation_id=operation.conversation_id,
        operation_type=operation.operation_type,
        risk_level=operation.risk_level,
        status=operation.status,
        preview=operation.preview_json,
        result=operation.result_json,
        expires_at=operation.expires_at,
        confirmed_at=operation.confirmed_at,
        executed_at=operation.executed_at,
        created_at=operation.created_at,
    )


# ── Conversation ──────────────────────────────────────────────


@router.post("/conversations", response_model=ConversationRead, status_code=201)
async def create_conversation(
    payload: ConversationCreate | None = None,
    session: AsyncSession = Depends(get_session),
):
    orchestrator = require_orchestrator()
    service = ConversationService(session, orchestrator)
    conversation = await service.create(payload.title if payload else None)
    return conversation


@router.get("/conversations", response_model=list[ConversationRead])
async def list_conversations(session: AsyncSession = Depends(get_session)):
    return await ConversationService(session).list()


@router.get("/conversations/{conversation_id}", response_model=ConversationRead)
async def get_conversation(conversation_id: str, session: AsyncSession = Depends(get_session)):
    return await require_conversation(conversation_id, session)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, session: AsyncSession = Depends(get_session)):
    orchestrator = get_orchestrator()
    deleted = await ConversationService(session, orchestrator).soft_delete(conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="助手会话不存在")
    return None


# ── Message ───────────────────────────────────────────────────


@router.post("/conversations/{conversation_id}/messages", response_model=MessageRead, status_code=201)
async def post_message(
    conversation_id: str,
    payload: MessageWrite,
    session: AsyncSession = Depends(get_session),
):
    orchestrator = require_orchestrator()
    conversation = await require_conversation(conversation_id, session)
    settings = get_settings()
    retry_after = await assistant_rate_limiter.consume(
        settings.assistant_user_id,
        limit=settings.assistant_rate_limit_per_minute,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="助手请求过于频繁，请稍后重试",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        message = await orchestrator.send_message(conversation, payload.content, payload.page_context)
    except AssistantConversationBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AssistantOpenCodeUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return MessageRead(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        message_type=message.message_type,
        content=message.content,
        status=message.status,
        created_at=message.created_at,
        updated_at=message.updated_at,
        parts=[],
    )


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
async def list_messages(conversation_id: str, session: AsyncSession = Depends(get_session)):
    await require_conversation(conversation_id, session)
    messages = await ConversationService(session).messages(conversation_id)
    operations = {
        operation.operation_id: operation
        for operation in (
            await session.execute(
                select(AssistantOperation).where(AssistantOperation.conversation_id == conversation_id)
            )
        )
        .scalars()
        .all()
    }
    result: list[MessageRead] = []
    for message in messages:
        read = MessageRead.model_validate(message)
        for part in read.parts:
            if part.part_type != "CONFIRM_CARD":
                continue
            operation = operations.get(str(part.payload.get("operation_id") or ""))
            # 页面刷新后按操作真实状态渲染确认卡，不误导用户重复确认。
            if operation and operation.status != part.payload.get("status"):
                part.payload = {
                    **part.payload,
                    "status": operation.status,
                    "result": operation.result_json,
                }
        result.append(read)
    return result


@router.post("/conversations/{conversation_id}/abort", status_code=202)
async def abort_generation(conversation_id: str, session: AsyncSession = Depends(get_session)):
    orchestrator = require_orchestrator()
    await require_conversation(conversation_id, session)
    await orchestrator.abort(conversation_id)
    return {"status": "aborted"}


# ── Event（平台自己的 SSE）──────────────────────────────────────


@router.get("/conversations/{conversation_id}/events")
async def conversation_events(
    conversation_id: str,
    after_seq: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    await require_conversation(conversation_id, session)
    orchestrator = require_orchestrator()
    hub = orchestrator.hub

    async def generate():
        yield f"event: connected\ndata: {json.dumps({'conversation_id': conversation_id})}\n\n"
        # after_seq 传入 JS Number.MAX_SAFE_INTEGER 表示“只要实时事件”：
        # 前端刚用 messages 接口恢复过完整状态，回放只会造成重复。
        live_only = after_seq >= 9007199254740991
        last_seq = hub.current_seq(conversation_id) if live_only else after_seq
        if not live_only:
            for envelope in hub.replay(conversation_id, after_seq):
                last_seq = max(last_seq, int(envelope.get("seq") or 0))
                yield f"event: assistant\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"
        queue = hub.register(conversation_id)
        keepalive_ticks = 0
        try:
            while True:
                try:
                    envelope = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    keepalive_ticks += 1
                    if keepalive_ticks >= 15:
                        keepalive_ticks = 0
                        yield ": keepalive\n\n"
                    continue
                if envelope.get("event") == "assistant.conversation.deleted":
                    return
                seq = int(envelope.get("seq") or 0)
                if seq <= last_seq:
                    continue
                last_seq = seq
                keepalive_ticks = 0
                yield f"event: assistant\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"
        finally:
            hub.unregister(conversation_id, queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Operation ─────────────────────────────────────────────────


@router.get("/operations/{operation_id}", response_model=OperationRead)
async def get_operation(operation_id: str, session: AsyncSession = Depends(get_session)):
    operation = await OperationService(session).get(operation_id)
    if not operation or operation.user_id != get_settings().assistant_user_id:
        raise HTTPException(status_code=404, detail="操作不存在")
    return operation_read(operation)


@router.post("/operations/{operation_id}/confirm", response_model=OperationRead)
async def confirm_operation(operation_id: str, session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    try:
        await OperationService(session, settings).confirm(operation_id, settings.assistant_user_id)
    except OperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    operation = await OperationService(session).get(operation_id)
    assert operation is not None
    orchestrator = get_orchestrator()
    if orchestrator and operation.conversation_id and operation.status == "SUCCESS":
        await orchestrator.hub.publish(
            operation.conversation_id,
            {
                "event": "assistant.operation.completed",
                "message_id": operation.message_id,
                "data": {
                    "operation_id": operation.operation_id,
                    "operation_type": operation.operation_type,
                    "status": operation.status,
                    "result": operation.result_json,
                },
            },
        )
    return operation_read(operation)


@router.post("/operations/{operation_id}/cancel", response_model=OperationRead)
async def cancel_operation(operation_id: str, session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    try:
        operation = await OperationService(session).cancel(operation_id, settings.assistant_user_id)
    except OperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    orchestrator = get_orchestrator()
    if orchestrator and operation.conversation_id:
        await orchestrator.hub.publish(
            operation.conversation_id,
            {
                "event": "assistant.operation.cancelled",
                "message_id": operation.message_id,
                "data": {"operation_id": operation.operation_id},
            },
        )
    return operation_read(operation)


# ── Health ────────────────────────────────────────────────────


@router.get("/health", response_model=AssistantHealth)
async def assistant_health():
    settings = get_settings()
    orchestrator = get_orchestrator()
    if not settings.assistant_enabled:
        return AssistantHealth(
            available=False,
            assistant="disabled",
            opencode_healthy=False,
            opencode_url=settings.resolved_assistant_opencode_url,
            detail="平台助手未启用（ASTRA_ASSISTANT_ENABLED=false）",
        )
    if orchestrator is None:
        return AssistantHealth(
            available=False,
            assistant="stopped",
            opencode_healthy=False,
            opencode_url=settings.resolved_assistant_opencode_url,
            detail="助手服务未启动",
        )
    try:
        probe = await orchestrator.client.health()
        return AssistantHealth(
            available=True,
            assistant="ok",
            opencode_healthy=True,
            opencode_version=probe.get("version"),
            opencode_url=orchestrator.client.base_url,
        )
    except Exception as exc:
        return AssistantHealth(
            available=False,
            assistant="degraded",
            opencode_healthy=False,
            opencode_url=orchestrator.client.base_url,
            detail=str(exc),
        )

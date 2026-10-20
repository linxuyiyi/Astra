"""助手编排器：消息发送管线 + OpenCode 事件监听与落库。

- 聊天不使用持续等待完整回复的普通 HTTP 请求，而是 prompt_async + SSE；
- 前端接收的是平台自己的 SSE（assistant.* 事件），不接触 OpenCode Schema；
- OpenCode Session 丢失时自动重建并恢复必要历史摘要；
- 停止助手生成 ≠ 取消已经创建的业务 Task，两者完全独立。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select

from app.assistant.cards import CARD_PART_TYPE, confirm_card
from app.assistant.client import AssistantOpenCodeClient, AssistantOpenCodeUnavailable
from app.assistant.context_service import AssistantContextService
from app.assistant.event_hub import AssistantEventHub, assistant_event_hub
from app.assistant.event_mapper import map_server_event, normalize_tool_name, session_id_of
from app.assistant.models import (
    AssistantContextRef,
    AssistantConversation,
    AssistantMessage,
    AssistantMessagePart,
    AssistantOperation,
    AssistantToolCall,
)
from app.assistant.operations import OperationService
from app.assistant.schemas import PageContext
from app.config.settings import Settings, get_settings
from app.database import SessionLocal

logger = logging.getLogger(__name__)

PREPARE_TOOLS = {
    "nexus_task_prepare_create",
    "nexus_task_prepare_cancel",
    "nexus_task_prepare_retry",
    "nexus_task_prepare_resume",
    "nexus_task_prepare_reassign",
}

REF_BY_CARD = {
    "task": "TASK",
    "employee": "EMPLOYEE",
    "session": "SESSION",
    "knowledge": "KNOWLEDGE",
}


def new_conversation_id() -> str:
    return f"AC-{uuid4().hex[:12].upper()}"


def new_message_id() -> str:
    return f"AM-{uuid4().hex[:12].upper()}"


def parse_tool_output(output: Any) -> Any:
    """MCP 工具输出可能是 JSON 字符串、内容块列表或结构化对象。"""
    if isinstance(output, list):
        texts = []
        for block in output:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
        if texts:
            joined = "\n".join(texts).strip()
            try:
                return json.loads(joined)
            except ValueError:
                return joined
        return output
    if isinstance(output, str):
        text = output.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except ValueError:
                return output
    return output


class AssistantConversationBusy(RuntimeError):
    """同一会话已有一轮正在生成，拒绝并发提交。"""


class AssistantOrchestrator:
    def __init__(
        self,
        settings: Settings | None = None,
        client: AssistantOpenCodeClient | None = None,
        hub: AssistantEventHub | None = None,
    ):
        self.settings = settings or get_settings()
        self.client = client or AssistantOpenCodeClient(self.settings)
        self.hub = hub or assistant_event_hub
        self._listener: asyncio.Task | None = None
        self._stopping = False
        self._running_turns: set[str] = set()
        self._active_messages: dict[str, str] = {}
        self._text_buffers: dict[str, dict[str, str]] = {}
        self._part_seq: dict[str, int] = {}
        self._session_map: dict[str, str] = {}
        self._turn_timeouts: dict[str, asyncio.Task[None]] = {}
        # OpenCode 的 part 事件不携带角色；角色由 message.updated 单独提供。
        self._message_roles: dict[str, dict[str, str]] = {}
        # 推理片段的 part_id：其文本与增量不得进入用户可见内容。
        self._reasoning_parts: set[str] = set()

    # ── 生命周期 ────────────────────────────────────────────────

    async def start(self) -> None:
        self._stopping = False
        if self._listener is None or self._listener.done():
            self._listener = asyncio.create_task(self._listen(), name="assistant-event-listener")

    async def stop(self) -> None:
        self._stopping = True
        timeout_tasks = list(self._turn_timeouts.values())
        self._turn_timeouts.clear()
        for task in timeout_tasks:
            task.cancel()
        if timeout_tasks:
            await asyncio.gather(*timeout_tasks, return_exceptions=True)
        if self._listener:
            self._listener.cancel()
            await asyncio.gather(self._listener, return_exceptions=True)
            self._listener = None

    async def _listen(self) -> None:
        backoff = 1.0
        # 首次连接前也做一次恢复：进程重启后数据库里可能有未完成的 STREAMING 消息。
        needs_resync = True
        while not self._stopping:
            try:
                if needs_resync:
                    await self._resync_active_turns()
                needs_resync = True
                async for raw in self.client.stream_events():
                    if self._stopping:
                        return
                    backoff = 1.0
                    try:
                        await self.handle_event(raw)
                    except Exception:
                        logger.exception("助手事件处理失败")
                if self._stopping:
                    return
                # 事件流正常关闭：退避后重连。
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping:
                    return
                logger.warning("助手 OpenCode 事件流中断：%s（%ss 后重连）", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    async def _resync_active_turns(self) -> None:
        """重连后按 OpenCode 消息事实恢复断线期间错过的回合。

        数据库中的 STREAMING 助手消息是唯一可靠的“进行中回合”标记；
        内存状态（_running_turns）只作补充。
        """
        try:
            await self.client.health()
        except Exception:
            return
        async with SessionLocal() as session:
            streaming = (
                (
                    await session.execute(
                        select(AssistantMessage).where(AssistantMessage.status == "STREAMING")
                    )
                )
                .scalars()
                .all()
            )
        pending: dict[str, str] = {item.conversation_id: item.id for item in streaming}
        for conversation_id in set(pending) | set(self._running_turns):
            try:
                await self._resync_conversation(conversation_id, pending.get(conversation_id))
            except Exception:
                logger.exception("助手回合恢复失败：%s", conversation_id)

    async def _resync_conversation(self, conversation_id: str, message_id: str | None) -> None:
        async with SessionLocal() as session:
            conversation = await session.get(AssistantConversation, conversation_id)
            if not conversation or not conversation.opencode_session_id:
                self._running_turns.discard(conversation_id)
                return
            try:
                server_status = await self.client.status(conversation.opencode_session_id)
                raw_messages = await self.client.messages(conversation.opencode_session_id)
            except Exception:
                # Session 已不可用时保留已有内容，但明确标记失败；下一轮发送会
                # 自动创建新的 OpenCode Session 并带上最近历史。
                server_status = "missing"
                raw_messages = []

            latest_info: dict[str, Any] = {}
            latest_parts: list[dict[str, Any]] = []
            for entry in reversed(raw_messages):
                info = entry.get("info") if isinstance(entry.get("info"), dict) else {}
                if info.get("role") != "assistant":
                    continue
                latest_info = info
                parts = entry.get("parts")
                latest_parts = (
                    [part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else []
                )
                break
            text_parts = {
                str(part.get("id") or f"text-{index}"): str(part.get("text") or "")
                for index, part in enumerate(latest_parts)
                if part.get("type") == "text" and part.get("text")
            }
            content = "\n\n".join(text_parts.values())
            message = await session.get(AssistantMessage, message_id) if message_id else None
            if server_status not in {"idle", "missing"} and message is not None:
                message.content = content or message.content
                message.opencode_message_id = str(latest_info.get("id") or "") or None
                message.updated_at = datetime.now(UTC)
                await session.commit()
                self._active_messages[conversation_id] = message.id
                self._text_buffers[conversation_id] = text_parts
                self._running_turns.add(conversation_id)
                self._start_turn_timeout(conversation_id, conversation.opencode_session_id, message.id)
                return
            if message is None:
                if not content:
                    self._running_turns.discard(conversation_id)
                    return
                message = AssistantMessage(
                    id=new_message_id(),
                    conversation_id=conversation_id,
                    role="ASSISTANT",
                    message_type="TEXT",
                    content=content,
                    status="COMPLETED",
                )
                session.add(message)
            else:
                message.content = content or message.content
                message.status = "FAILED" if server_status == "missing" else "COMPLETED"
                message.opencode_message_id = str(latest_info.get("id") or "") or None
                message.updated_at = datetime.now(UTC)
            await session.commit()
            final_message_id = message.id
        self._active_messages.pop(conversation_id, None)
        self._text_buffers.pop(conversation_id, None)
        self._running_turns.discard(conversation_id)
        await self.hub.publish(
            conversation_id,
            {
                "event": "assistant.failed" if server_status == "missing" else "assistant.message.completed",
                "message_id": final_message_id,
                "data": {
                    "resynced": True,
                    **(
                        {"error": "助手 OpenCode Session 已失效，请重新发送消息"}
                        if server_status == "missing"
                        else {}
                    ),
                },
            },
        )
        if server_status != "missing":
            await self.hub.publish(
                conversation_id,
                {
                    "event": "assistant.completed",
                    "message_id": final_message_id,
                    "data": {"resynced": True},
                },
            )

    # ── 发送消息 ────────────────────────────────────────────────

    async def send_message(
        self,
        conversation: AssistantConversation,
        content: str,
        page_context: PageContext | None,
    ) -> AssistantMessage:
        async with SessionLocal() as session:
            conversation = await session.get(AssistantConversation, conversation.id)
            if not conversation:
                raise ValueError("会话不存在")
            active_message = await session.scalar(
                select(AssistantMessage.id)
                .where(
                    AssistantMessage.conversation_id == conversation.id,
                    AssistantMessage.role == "ASSISTANT",
                    AssistantMessage.status == "STREAMING",
                )
                .limit(1)
            )
            if active_message or conversation.id in self._running_turns:
                raise AssistantConversationBusy("当前会话仍在生成，请先停止或等待完成")
            user_message = AssistantMessage(
                id=new_message_id(),
                conversation_id=conversation.id,
                role="USER",
                message_type="TEXT",
                content=content.strip(),
                status="COMPLETED",
            )
            session.add(user_message)
            context_service = AssistantContextService(session)
            context_block, refs = await context_service.build_context_block(conversation, page_context)
            for ref_type, ref_id in refs:
                session.add(
                    AssistantContextRef(
                        conversation_id=conversation.id,
                        message_id=user_message.id,
                        ref_type=ref_type,
                        ref_id=ref_id,
                    )
                )
            try:
                session_id, recovered = await self._ensure_session(session, conversation)
            except Exception as exc:
                # OpenCode 不可用时不提交用户消息；块退出会自动丢弃未提交变更。
                raise AssistantOpenCodeUnavailable(f"助手 OpenCode Server 不可用：{exc}") from exc
            history = ""
            if recovered:
                history = await context_service.history_summary(conversation)
            # 先落库一条 STREAMING 助手消息：即使事件流中断，回合也有持久标记可恢复。
            placeholder = AssistantMessage(
                id=new_message_id(),
                conversation_id=conversation.id,
                role="ASSISTANT",
                message_type="TEXT",
                content="",
                status="STREAMING",
            )
            session.add(placeholder)
            conversation.last_message_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(user_message)
            await session.refresh(placeholder)
            user_message_id = user_message.id
            placeholder_id = placeholder.id

        self._active_messages[conversation.id] = placeholder_id
        self._text_buffers.setdefault(conversation.id, {})
        self._part_seq.setdefault(placeholder_id, 0)
        prompt = self._build_prompt(context_block, history, content)
        self._running_turns.add(conversation.id)
        self._start_turn_timeout(conversation.id, session_id, placeholder_id)
        try:
            await self.client.prompt_async(session_id, text=prompt)
        except Exception as exc:
            async with SessionLocal() as fail_session:
                failed_message = await fail_session.get(AssistantMessage, placeholder_id)
                if failed_message and failed_message.status == "STREAMING":
                    failed_message.status = "FAILED"
                    await fail_session.commit()
            self._active_messages.pop(conversation.id, None)
            self._running_turns.discard(conversation.id)
            self._cancel_turn_timeout(conversation.id)
            await self.hub.publish(
                conversation.id,
                {
                    "event": "assistant.failed",
                    "message_id": placeholder_id,
                    "data": {"error": f"助手服务暂时不可用：{exc}"},
                },
            )
            raise AssistantOpenCodeUnavailable(f"助手 OpenCode Server 不可用：{exc}") from exc

        # 极短回合可能在 prompt_async 返回前已经由事件监听器完成。
        if conversation.id not in self._running_turns:
            return user_message
        await self.hub.publish(
            conversation.id,
            {"event": "assistant.started", "message_id": user_message_id, "data": {}},
        )
        await self.hub.publish(
            conversation.id,
            {"event": "assistant.status", "message_id": user_message_id, "data": {"state": "THINKING"}},
        )
        return user_message

    def _start_turn_timeout(self, conversation_id: str, session_id: str, message_id: str) -> None:
        self._cancel_turn_timeout(conversation_id)
        self._turn_timeouts[conversation_id] = asyncio.create_task(
            self._enforce_turn_timeout(conversation_id, session_id, message_id),
            name=f"assistant-timeout-{conversation_id}",
        )

    def _cancel_turn_timeout(self, conversation_id: str) -> None:
        task = self._turn_timeouts.pop(conversation_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    async def _enforce_turn_timeout(self, conversation_id: str, session_id: str, message_id: str) -> None:
        try:
            await asyncio.sleep(self.settings.assistant_request_timeout_seconds)
            if (
                conversation_id not in self._running_turns
                or self._active_messages.get(conversation_id) != message_id
            ):
                return
            try:
                await self.client.abort(session_id)
            except Exception:
                logger.warning("助手超时后停止 OpenCode Session 失败：%s", conversation_id)
            await self._finalize(
                conversation_id,
                status="FAILED",
                error=f"助手响应超过 {self.settings.assistant_request_timeout_seconds} 秒，已停止生成",
            )
        except asyncio.CancelledError:
            raise
        finally:
            if self._turn_timeouts.get(conversation_id) is asyncio.current_task():
                self._turn_timeouts.pop(conversation_id, None)

    def _build_prompt(self, context_block: str, history: str, content: str) -> str:
        sections = ["[平台上下文]", context_block or "- 无"]
        if history:
            sections.extend(["", "[最近对话历史（用于会话恢复）]", history])
        sections.extend(["", "[用户消息]", content.strip()])
        return "\n".join(sections)

    async def _ensure_session(self, session, conversation: AssistantConversation) -> tuple[str, bool]:
        """OpenCode Session 与 Conversation 一一对应；失效时重建并恢复历史。"""
        if conversation.opencode_session_id:
            try:
                if await self.client.session_exists(conversation.opencode_session_id):
                    self._session_map[conversation.opencode_session_id] = conversation.id
                    return conversation.opencode_session_id, False
            except Exception:
                pass
        created = await self.client.create_session(title=conversation.title or "平台助手会话")
        conversation.opencode_session_id = created
        self._session_map[created] = conversation.id
        return created, True

    # ── 停止生成（不影响已创建的业务 Task）────────────────────────

    async def abort(self, conversation_id: str) -> None:
        async with SessionLocal() as session:
            conversation = await session.get(AssistantConversation, conversation_id)
            session_id = conversation.opencode_session_id if conversation else None
        if session_id:
            try:
                await self.client.abort(session_id)
            except Exception:
                logger.warning("助手停止生成失败：%s", conversation_id)
        await self._finalize(conversation_id, status="COMPLETED", aborted=True)

    # ── OpenCode 事件处理 ───────────────────────────────────────

    async def _conversation_id_for(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        cached = self._session_map.get(session_id)
        if cached:
            return cached
        async with SessionLocal() as session:
            conversation = await session.scalar(
                select(AssistantConversation).where(
                    AssistantConversation.opencode_session_id == session_id,
                    AssistantConversation.status == "ACTIVE",
                )
            )
        if conversation:
            self._session_map[session_id] = conversation.id
            return conversation.id
        return None

    async def handle_event(self, raw: dict[str, Any]) -> None:
        session_id = session_id_of(raw)
        conversation_id = await self._conversation_id_for(session_id)
        if not conversation_id:
            return
        for mapped in map_server_event(raw):
            name = mapped["event"]
            data = mapped["data"]
            if name == "assistant.message.info":
                await self._on_message_info(conversation_id, data)
            elif name == "assistant.message.reasoning":
                part_id = str(data.get("part_id") or "")
                if part_id:
                    if len(self._reasoning_parts) > 500:
                        self._reasoning_parts.clear()
                    self._reasoning_parts.add(part_id)
            elif name == "assistant.message.delta":
                await self._apply_text(
                    conversation_id,
                    data.get("opencode_message_id"),
                    data.get("part_id") or "text",
                    text=str(data.get("text") or ""),
                )
            elif name == "assistant.message.text_delta":
                await self._apply_text(
                    conversation_id,
                    data.get("opencode_message_id"),
                    data.get("part_id") or "text",
                    delta=str(data.get("delta") or ""),
                )
            elif name == "assistant.tool.started":
                await self._emit(conversation_id, "assistant.tool.started", data)
            elif name == "assistant.tool.completed":
                await self._on_tool_completed(conversation_id, session_id, data)
            elif name == "assistant.tool.failed":
                await self._record_tool_call(conversation_id, session_id, data, status="FAILED")
                await self._emit(conversation_id, "assistant.tool.failed", data)
            elif name == "assistant.completed":
                if conversation_id in self._running_turns:
                    await self._finalize(conversation_id, status="COMPLETED")
            elif name == "assistant.failed":
                await self._finalize(conversation_id, status="FAILED", error=data.get("error"))

    async def _emit(self, conversation_id: str, name: str, data: dict[str, Any]) -> None:
        await self.hub.publish(
            conversation_id,
            {
                "event": name,
                "message_id": self._active_messages.get(conversation_id),
                "data": data,
            },
        )

    async def _ensure_assistant_message(
        self, session, conversation_id: str, opencode_message_id: str | None
    ) -> AssistantMessage:
        message_id = self._active_messages.get(conversation_id)
        if message_id:
            message = await session.get(AssistantMessage, message_id)
            if message:
                return message
        # 进程重启后沿用数据库中仍在进行的 STREAMING 消息，避免重复创建。
        existing = await session.scalar(
            select(AssistantMessage)
            .where(
                AssistantMessage.conversation_id == conversation_id,
                AssistantMessage.role == "ASSISTANT",
                AssistantMessage.status == "STREAMING",
            )
            .order_by(AssistantMessage.created_at.desc())
            .limit(1)
        )
        if existing:
            self._active_messages[conversation_id] = existing.id
            self._text_buffers.setdefault(conversation_id, {})
            self._part_seq.setdefault(existing.id, 0)
            return existing
        message = AssistantMessage(
            id=new_message_id(),
            conversation_id=conversation_id,
            role="ASSISTANT",
            message_type="TEXT",
            content="",
            status="STREAMING",
            opencode_message_id=opencode_message_id,
        )
        session.add(message)
        await session.commit()
        await session.refresh(message)
        self._active_messages[conversation_id] = message.id
        self._text_buffers.setdefault(conversation_id, {})
        self._part_seq.setdefault(message.id, 0)
        return message

    async def _next_part_seq(self, session, message_id: str) -> int:
        """同时参考数据库与内存游标，进程重启后仍不会复用 part.seq。"""
        persisted = await session.scalar(
            select(func.max(AssistantMessagePart.seq)).where(AssistantMessagePart.message_id == message_id)
        )
        seq = max(int(persisted or 0), self._part_seq.get(message_id, 0)) + 1
        self._part_seq[message_id] = seq
        return seq

    async def _on_message_info(self, conversation_id: str, data: dict) -> None:
        opencode_message_id = str(data.get("opencode_message_id") or "")
        role = str(data.get("role") or "")
        if opencode_message_id and role:
            roles = self._message_roles.setdefault(conversation_id, {})
            if len(roles) > 200:
                roles.clear()
            roles[opencode_message_id] = role
        if role == "assistant" and data.get("error"):
            await self._finalize(conversation_id, status="FAILED", error=str(data.get("error")))

    def _is_assistant_text(self, conversation_id: str, opencode_message_id: str | None) -> bool:
        """part 事件不带角色：按 message.updated 记录的角色过滤用户消息回显。"""
        role = self._message_roles.get(conversation_id, {}).get(str(opencode_message_id or ""))
        if role == "user":
            return False
        if role == "assistant":
            return True
        # 角色未知：仅在活跃回合内按助手文本处理。
        return conversation_id in self._running_turns

    async def _apply_text(
        self,
        conversation_id: str,
        opencode_message_id: str | None,
        part_id: str,
        *,
        text: str | None = None,
        delta: str | None = None,
    ) -> None:
        if not self._is_assistant_text(conversation_id, opencode_message_id):
            return
        if part_id in self._reasoning_parts:
            return
        async with SessionLocal() as session:
            message = await self._ensure_assistant_message(session, conversation_id, opencode_message_id)
            first = not message.content
            buffer = self._text_buffers.setdefault(conversation_id, {})
            if text is not None:
                # message.part.updated 携带该 part 的完整文本，直接覆盖。
                buffer[part_id] = text
            else:
                buffer[part_id] = buffer.get(part_id, "") + (delta or "")
            content = "\n\n".join(item for item in buffer.values() if item)
            if content == message.content:
                return
            first = first and bool(content)
            message.content = content
            message.updated_at = datetime.now(UTC)
            await session.commit()
        if first:
            await self._emit(
                conversation_id,
                "assistant.message.created",
                {"content": content},
            )
        await self._emit(
            conversation_id,
            "assistant.message.delta",
            {"content": content},
        )

    async def _on_tool_completed(self, conversation_id: str, session_id: str | None, data: dict) -> None:
        tool_name = str(data.get("tool") or "")
        output = parse_tool_output(data.get("output"))
        await self._record_tool_call(
            conversation_id, session_id, {**data, "output": output}, status="COMPLETED"
        )
        if not isinstance(output, dict):
            await self._emit(conversation_id, "assistant.tool.completed", {"tool": tool_name})
            return

        card = output.get("card")
        if isinstance(card, dict) and card.get("card_type"):
            await self._append_card(conversation_id, card)

        if normalize_tool_name(tool_name) in PREPARE_TOOLS and output.get("operation_id"):
            await self._attach_confirmation(conversation_id, output)

        await self._emit(
            conversation_id,
            "assistant.tool.completed",
            {"tool": tool_name, "operation_id": output.get("operation_id")},
        )

    async def _record_tool_call(
        self, conversation_id: str, session_id: str | None, data: dict, *, status: str
    ) -> None:
        def _sanitize(value: Any) -> Any:
            try:
                json.dumps(value, ensure_ascii=False, default=str)
                return value
            except (TypeError, ValueError):
                return str(value)

        async with SessionLocal() as session:
            message = await self._ensure_assistant_message(
                session, conversation_id, data.get("opencode_message_id")
            )
            session.add(
                AssistantToolCall(
                    conversation_id=conversation_id,
                    message_id=message.id,
                    opencode_session_id=session_id,
                    tool_name=str(data.get("tool") or "")[:160],
                    arguments_json=(
                        _sanitize(data.get("input")) if isinstance(data.get("input"), dict) else {}
                    ),
                    result_json={"output": _sanitize(data.get("output"))}
                    if status == "COMPLETED"
                    else {"error": str(data.get("error") or "")},
                    status=status,
                )
            )
            await session.commit()

    async def _append_card(self, conversation_id: str, card: dict[str, Any]) -> None:
        part_type = CARD_PART_TYPE.get(str(card.get("card_type") or ""), "STATUS")
        async with SessionLocal() as session:
            message = await self._ensure_assistant_message(session, conversation_id, None)
            seq = await self._next_part_seq(session, message.id)
            session.add(
                AssistantMessagePart(message_id=message.id, part_type=part_type, seq=seq, payload_json=card)
            )
            ref_type = REF_BY_CARD.get(str(card.get("card_type") or ""))
            if ref_type and card.get("id"):
                session.add(
                    AssistantContextRef(
                        conversation_id=conversation_id,
                        message_id=message.id,
                        ref_type=ref_type,
                        ref_id=str(card["id"]),
                    )
                )
            await session.commit()
        await self._emit(
            conversation_id,
            "assistant.card.created",
            {"part": {"part_type": part_type, "seq": seq, "payload": card}},
        )

    async def _attach_confirmation(self, conversation_id: str, output: dict[str, Any]) -> None:
        operation_id = str(output.get("operation_id") or "")
        message_id = self._active_messages.get(conversation_id)
        async with SessionLocal() as session:
            await OperationService(session).link_conversation(operation_id, conversation_id, message_id)
            operation = await session.scalar(
                select(AssistantOperation).where(AssistantOperation.operation_id == operation_id)
            )
            if not operation:
                return
            message = (
                await session.get(AssistantMessage, message_id) if message_id else None
            ) or await self._ensure_assistant_message(session, conversation_id, None)
            seq = await self._next_part_seq(session, message.id)
            payload = confirm_card(
                operation.operation_id,
                operation.operation_type,
                operation.risk_level,
                operation.preview_json,
            )
            session.add(
                AssistantMessagePart(
                    message_id=message.id, part_type="CONFIRM_CARD", seq=seq, payload_json=payload
                )
            )
            await session.commit()
        await self.hub.publish(
            conversation_id,
            {
                "event": "assistant.operation.confirm_required",
                "message_id": message_id,
                "data": {
                    "operation_id": operation_id,
                    "operation": {
                        "operation_id": operation_id,
                        "operation_type": operation.operation_type,
                        "risk_level": operation.risk_level,
                        "status": operation.status,
                        "preview": operation.preview_json,
                        "expires_at": operation.expires_at.isoformat() if operation.expires_at else None,
                    },
                    "part": {"part_type": "CONFIRM_CARD", "seq": seq, "payload": payload},
                },
            },
        )

    async def _finalize(
        self, conversation_id: str, *, status: str, error: str | None = None, aborted: bool = False
    ) -> None:
        message_id = self._active_messages.pop(conversation_id, None)
        self._text_buffers.pop(conversation_id, None)
        self._running_turns.discard(conversation_id)
        self._cancel_turn_timeout(conversation_id)
        if message_id:
            self._part_seq.pop(message_id, None)
        if message_id:
            async with SessionLocal() as session:
                message = await session.get(AssistantMessage, message_id)
                if message and message.status == "STREAMING":
                    message.status = "FAILED" if status == "FAILED" else "COMPLETED"
                    message.updated_at = datetime.now(UTC)
                    await session.commit()
            if status == "FAILED":
                await self.hub.publish(
                    conversation_id,
                    {
                        "event": "assistant.failed",
                        "message_id": message_id,
                        "data": {"error": error or "助手执行异常"},
                    },
                )
            else:
                await self.hub.publish(
                    conversation_id,
                    {
                        "event": "assistant.message.completed",
                        "message_id": message_id,
                        "data": {"aborted": aborted},
                    },
                )
        if status == "FAILED":
            if not message_id:
                await self.hub.publish(
                    conversation_id,
                    {
                        "event": "assistant.failed",
                        "message_id": None,
                        "data": {"error": error or "助手执行异常"},
                    },
                )
        else:
            await self.hub.publish(
                conversation_id,
                {"event": "assistant.completed", "message_id": message_id, "data": {"aborted": aborted}},
            )


_orchestrator: AssistantOrchestrator | None = None


def set_orchestrator(orchestrator: AssistantOrchestrator | None) -> None:
    global _orchestrator
    _orchestrator = orchestrator


def get_orchestrator() -> AssistantOrchestrator | None:
    return _orchestrator

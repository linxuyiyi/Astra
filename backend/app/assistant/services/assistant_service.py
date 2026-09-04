import json
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.assistant.models import AssistantMessage, AssistantMessagePart
from app.assistant.opencode.event_mapper import EventMapper
from app.assistant.schemas import MessageCreate
from app.assistant.services.conversation_service import ConversationService
from app.config.settings import get_settings
from app.executors.opencode_server import OpenCodeServerClient


class AssistantService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.settings = get_settings()
        self.opencode_client = OpenCodeServerClient(self.settings)
        self.event_mapper = EventMapper(self.opencode_client)
        self.conversation_service = ConversationService(db)

    async def send_message(
        self, conversation_id: str, user_id: str, message_in: MessageCreate
    ) -> AssistantMessage:
        conv = await self.conversation_service.get_conversation(conversation_id)
        if not conv or conv.user_id != user_id:
            raise ValueError("Conversation not found")

        # 1. Save user message
        msg_id = f"MSG-{uuid.uuid4().hex[:8].upper()}"
        msg = AssistantMessage(
            id=msg_id,
            conversation_id=conversation_id,
            role="USER",
            message_type="TEXT",
            content=message_in.content,
            status="COMPLETED"
        )
        self.db.add(msg)
        await self.db.commit()
        await self.db.refresh(msg)
        msg_dict = {
            "id": msg.id,
            "conversation_id": msg.conversation_id,
            "role": msg.role,
            "message_type": msg.message_type,
            "content": msg.content,
            "status": msg.status,
            "opencode_message_id": msg.opencode_message_id,
            "created_at": msg.created_at,
            "updated_at": msg.updated_at,
            "parts": []
        }

        # 2. Build Agent Context
        context = {
            "current_user_id": user_id,
            "page_context": message_in.page_context,
            # Additional context like references can be added here
        }

        # 3. Send async prompt to OpenCode
        directory = Path(__file__).parent.parent / "workspace"
        await self.opencode_client.prompt(
            directory=directory,
            session_id=conv.opencode_session_id,
            prompt=message_in.content,
            agent=None,  # OpenCode handles agent fallback/config natively
            system=str(context) if context else None
        )

        return msg_dict

    async def stream_events(self, conversation_id: str, request: Request) -> AsyncGenerator[str, None]:
        conv = await self.conversation_service.get_conversation(conversation_id)
        if not conv:
            yield "data: {\"error\": \"Conversation not found\"}\n\n"
            return

        directory = Path(__file__).parent.parent / "workspace"
        async for sse_msg in self.event_mapper.stream_and_map(
            conv.opencode_session_id, conversation_id, directory
        ):
            if await request.is_disconnected():
                break

            # Intercept specific events to persist
            if sse_msg.startswith("data: "):
                data_str = sse_msg[6:].strip()
                if data_str:
                    try:
                        event_data = json.loads(data_str)
                        await self._persist_event(conversation_id, event_data)
                    except Exception:
                        pass

            yield sse_msg

    async def _persist_event(self, conversation_id: str, event_data: dict):
        event_type = event_data.get("event")

        # We only persist the final message state when the session is idle
        if event_type == "assistant.message.completed":
            conv = await self.conversation_service.get_conversation(conversation_id)
            if not conv:
                return
            directory = Path(__file__).parent.parent / "workspace"
            try:
                # Fetch full message history from OpenCode
                oc_messages = await self.opencode_client.messages(directory, conv.opencode_session_id)
                if not oc_messages:
                    return
                # We expect the last message to be the assistant's reply
                last_oc_msg = oc_messages[-1]
                if last_oc_msg.get("role") != "assistant":
                    return

                msg_id = last_oc_msg.get("id")
                # Ensure we don't save duplicates
                existing_msg = await self.db.execute(
                    select(AssistantMessage).where(AssistantMessage.opencode_message_id == msg_id)
                )
                if existing_msg.scalar_one_or_none():
                    return

                assistant_msg = AssistantMessage(
                    id=f"MSG-{uuid.uuid4().hex[:8].upper()}",
                    conversation_id=conversation_id,
                    role="ASSISTANT",
                    message_type="MIXED",
                    status="COMPLETED",
                    opencode_message_id=msg_id
                )
                self.db.add(assistant_msg)

                parts = last_oc_msg.get("parts", [])
                for i, p in enumerate(parts):
                    part_type = p.get("type", "text").upper()
                    if part_type == "TOOL":
                        # OpenCode Tool part structure:
                        # {"type": "tool", "tool": "nexus_task_search", "state": {"output": {...}}}
                        tool_name = p.get("tool", "")
                        state = p.get("state", {})
                        output = state.get("output", {})

                        if "prepare_create" in tool_name or "cancel" in tool_name:
                            part_type = "CONFIRM_CARD"
                        elif "task_" in tool_name and "items" in output and len(output["items"]) > 0:
                            part_type = "TASK_CARD"
                            output = output["items"][0]
                        elif "employee_" in tool_name and "items" in output and len(output["items"]) > 0:
                            part_type = "EMPLOYEE_CARD"
                            output = output["items"][0]
                        elif "run_" in tool_name and "items" in output and len(output["items"]) > 0:
                            part_type = "RUN_CARD"
                            output = output["items"][0]
                        elif "knowledge_" in tool_name and "items" in output and len(output["items"]) > 0:
                            part_type = "KNOWLEDGE_CARD"
                            output = output["items"][0]
                        else:
                            part_type = "TEXT"

                        msg_part = AssistantMessagePart(
                            id=f"PRT-{uuid.uuid4().hex[:8].upper()}",
                            message_id=assistant_msg.id,
                            part_type=part_type,
                            seq=i,
                            payload_json=output
                        )
                        self.db.add(msg_part)
                    else:
                        msg_part = AssistantMessagePart(
                            id=f"PRT-{uuid.uuid4().hex[:8].upper()}",
                            message_id=assistant_msg.id,
                            part_type="TEXT",
                            seq=i,
                            payload_json={"text": p.get("text", "")}
                        )
                        self.db.add(msg_part)

                await self.db.commit()
            except Exception:
                # Log error in real app
                pass
    async def list_messages(self, conversation_id: str) -> list[AssistantMessage]:
        result = await self.db.execute(
            select(AssistantMessage)
            .where(AssistantMessage.conversation_id == conversation_id)
            .options(selectinload(AssistantMessage.parts))
            .order_by(AssistantMessage.created_at.asc())
        )
        return list(result.scalars().all())

    async def abort_generation(self, conversation_id: str) -> None:
        conv = await self.conversation_service.get_conversation(conversation_id)
        if conv:
            directory = Path(__file__).parent.parent / "workspace"
            await self.opencode_client.abort(directory, conv.opencode_session_id)

"""会话服务：Conversation 与 OpenCode Session 一一对应的生命周期管理。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.assistant.models import AssistantConversation, AssistantMessage
from app.assistant.orchestrator import AssistantOrchestrator, new_conversation_id
from app.config.settings import Settings, get_settings


class ConversationService:
    def __init__(
        self,
        session: AsyncSession,
        orchestrator: AssistantOrchestrator | None = None,
        settings: Settings | None = None,
    ):
        self.session = session
        self.orchestrator = orchestrator
        self.settings = settings or get_settings()

    @property
    def user_id(self) -> str:
        # 用户身份由后端注入；当前平台为单租户管理台，使用配置身份。
        return self.settings.assistant_user_id

    async def create(self, title: str | None = None) -> AssistantConversation:
        conversation = AssistantConversation(
            id=new_conversation_id(),
            user_id=self.user_id,
            title=(title or "新的会话").strip()[:240],
            status="ACTIVE",
        )
        self.session.add(conversation)
        await self.session.commit()
        await self.session.refresh(conversation)
        if self.orchestrator:
            conversation_id = conversation.id
            try:
                session_id, _ = await self.orchestrator._ensure_session(self.session, conversation)
                conversation.opencode_session_id = session_id
                await self.session.commit()
                await self.session.refresh(conversation)
            except Exception:
                # OpenCode 暂不可用时仍保留平台会话，稍后发送消息时自动重建映射。
                # 回滚后不得再触碰旧实例属性（可能触发同步懒加载），按 ID 重新查询。
                await self.session.rollback()
                conversation = await self.session.get(AssistantConversation, conversation_id)
        return conversation

    async def list(self) -> list[AssistantConversation]:
        rows = (
            (
                await self.session.execute(
                    select(AssistantConversation)
                    .where(
                        AssistantConversation.status == "ACTIVE",
                        AssistantConversation.user_id == self.user_id,
                    )
                    .order_by(
                        AssistantConversation.last_message_at.desc().nulls_last(),
                        AssistantConversation.created_at.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def get(self, conversation_id: str) -> AssistantConversation | None:
        conversation = await self.session.get(AssistantConversation, conversation_id)
        if not conversation or conversation.status == "DELETED" or conversation.user_id != self.user_id:
            return None
        return conversation

    async def messages(self, conversation_id: str, limit: int = 200) -> list[AssistantMessage]:
        rows = (
            (
                await self.session.execute(
                    select(AssistantMessage)
                    .where(AssistantMessage.conversation_id == conversation_id)
                    .options(selectinload(AssistantMessage.parts))
                    .order_by(AssistantMessage.created_at.asc(), AssistantMessage.id.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def soft_delete(self, conversation_id: str) -> bool:
        conversation = await self.session.get(AssistantConversation, conversation_id)
        if not conversation or conversation.user_id != self.user_id:
            return False
        conversation.status = "DELETED"
        conversation.updated_at = datetime.now(UTC)
        await self.session.commit()
        if self.orchestrator and conversation.opencode_session_id:
            try:
                await self.orchestrator.client.delete_session(conversation.opencode_session_id)
            except Exception:
                pass
        return True

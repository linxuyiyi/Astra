import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.models import AssistantConversation
from app.assistant.schemas import ConversationCreate
from app.config.settings import get_settings
from app.executors.opencode_server import OpenCodeServerClient


class ConversationService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.settings = get_settings()
        self.opencode_client = OpenCodeServerClient(self.settings)

    async def create_conversation(self, user_id: str, data: ConversationCreate) -> AssistantConversation:
        # 1. Create OpenCode Session
        directory = Path(__file__).parent.parent / "workspace"
        directory.mkdir(parents=True, exist_ok=True)
        opencode_session_id = await self.opencode_client.create_session(
            directory, title=data.title, model_name=self.settings.opencode_model, agent="platform-assistant"
        )

        # 2. Save Conversation
        conv_id = f"CONV-{uuid.uuid4().hex[:8].upper()}"
        conv = AssistantConversation(
            id=conv_id,
            user_id=user_id,
            title=data.title,
            opencode_session_id=opencode_session_id,
            status="ACTIVE"
        )
        self.db.add(conv)
        await self.db.commit()
        await self.db.refresh(conv)
        return conv

    async def list_conversations(self, user_id: str) -> list[AssistantConversation]:
        result = await self.db.execute(
            select(AssistantConversation)
            .where(AssistantConversation.user_id == user_id)
            .where(AssistantConversation.status != "DELETED")
            .order_by(AssistantConversation.updated_at.desc())
        )
        return list(result.scalars().all())

    async def get_conversation(self, conversation_id: str) -> AssistantConversation | None:
        result = await self.db.execute(
            select(AssistantConversation).where(AssistantConversation.id == conversation_id)
        )
        return result.scalar_one_or_none()

    async def delete_conversation(self, conversation_id: str) -> None:
        conv = await self.get_conversation(conversation_id)
        if conv:
            conv.status = "DELETED"
            await self.db.commit()

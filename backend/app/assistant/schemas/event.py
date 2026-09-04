from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AssistantEvent(BaseModel):
    event: str = Field(description="事件名称 (如 assistant.message.delta)")
    conversation_id: str | None = None
    message_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: dict[str, Any] = Field(default_factory=dict, description="事件 payload")

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MessagePartBase(BaseModel):
    part_type: str = Field(description="片段类型")
    seq: int = Field(default=0, description="序号")
    payload_json: dict[str, Any] = Field(default_factory=dict, description="内容或卡片数据")

class MessagePartResponse(MessagePartBase):
    id: str
    message_id: str
    created_at: datetime

    model_config = {"from_attributes": True}

class MessageBase(BaseModel):
    role: str = Field(description="角色 (USER, ASSISTANT, SYSTEM)")
    message_type: str = Field(description="消息类型 (TEXT, CARD, STATUS, ERROR)")
    content: str = Field(default="", description="纯文本内容 (如有)")
    status: str = Field(default="COMPLETED", description="状态 (PENDING, STREAMING, COMPLETED, FAILED)")

class MessageCreate(BaseModel):
    content: str = Field(description="用户输入的内容")
    page_context: dict[str, Any] | None = Field(default=None, description="可选的页面上下文")

class MessageResponse(MessageBase):
    id: str
    conversation_id: str
    opencode_message_id: str | None = None
    parts: list[MessagePartResponse] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

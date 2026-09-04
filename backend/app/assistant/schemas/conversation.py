from datetime import datetime

from pydantic import BaseModel, Field


class ConversationBase(BaseModel):
    title: str = Field(default="新对话", description="对话标题")
    status: str = Field(default="ACTIVE", description="状态 (ACTIVE, ARCHIVED, DELETED)")

class ConversationCreate(ConversationBase):
    pass

class ConversationResponse(ConversationBase):
    id: str = Field(description="对话 ID")
    user_id: str = Field(description="创建用户 ID")
    opencode_session_id: str | None = Field(default=None, description="绑定的 OpenCode Session ID")
    last_message_at: datetime = Field(description="最后消息时间")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

class ContextRefCreate(BaseModel):
    ref_type: str = Field(description="上下文类型 (TASK, RUN, EMPLOYEE, KNOWLEDGE)")
    ref_id: str = Field(description="业务对象 ID")

class ContextRefResponse(ContextRefCreate):
    id: str
    conversation_id: str
    message_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

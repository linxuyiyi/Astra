from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas import ORMModel


class PageContext(BaseModel):
    """前端当前页面上下文。后端会根据权限重新查询真实业务对象，
    不把前端提交的对象当作可信数据。"""

    route: str | None = None
    type: Literal["task", "employee", "session", "knowledge", "library", "dashboard"] | None = None
    id: str | None = None


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=240)


class ConversationRead(ORMModel):
    id: str
    user_id: str
    title: str
    status: str
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MessagePartRead(ORMModel):
    id: int
    part_type: str
    seq: int
    payload: dict[str, Any] = Field(default_factory=dict, validation_alias="payload_json")
    created_at: datetime


class MessageRead(ORMModel):
    id: str
    conversation_id: str
    role: str
    message_type: str
    content: str
    status: str
    created_at: datetime
    updated_at: datetime
    parts: list[MessagePartRead] = Field(default_factory=list)


class MessageWrite(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    page_context: PageContext | None = None


class OperationRead(BaseModel):
    operation_id: str
    conversation_id: str | None
    operation_type: str
    risk_level: str
    status: str
    preview: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None
    confirmed_at: datetime | None
    executed_at: datetime | None
    created_at: datetime


class AssistantEventRead(BaseModel):
    """平台自己的 SSE 事件结构；前端完全不依赖 OpenCode Event Schema。"""

    event: str
    seq: int = 0
    conversation_id: str
    message_id: str | None = None
    timestamp: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class AssistantHealth(BaseModel):
    available: bool
    assistant: str
    opencode_healthy: bool
    opencode_version: str | None = None
    opencode_url: str
    detail: str = ""

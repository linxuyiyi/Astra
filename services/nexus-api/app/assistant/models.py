"""持久化模型：会话、消息、消息片段、上下文引用、操作和工具调用审计。

OpenCode Session 不作为唯一会话存储：即使 OpenCode 崩溃，平台数据库仍保留
完整会话记录，可重建 Session 并恢复必要历史摘要。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base, UTCDateTime, utcnow


class AssistantConversation(Base):
    __tablename__ = "t_nexus_assistant_conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(240), default="新的会话")
    opencode_session_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    messages: Mapped[list[AssistantMessage]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class AssistantMessage(Base):
    __tablename__ = "t_nexus_assistant_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("t_nexus_assistant_conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(24), index=True)
    message_type: Mapped[str] = mapped_column(String(32), default="TEXT")
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="COMPLETED", index=True)
    opencode_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    conversation: Mapped[AssistantConversation] = relationship(back_populates="messages")
    parts: Mapped[list[AssistantMessagePart]] = relationship(
        back_populates="message", cascade="all, delete-orphan", order_by="AssistantMessagePart.seq"
    )


class AssistantMessagePart(Base):
    """消息片段：刷新页面后能够完整恢复文本、业务卡片和确认卡。"""

    __tablename__ = "t_nexus_assistant_message_parts"
    __table_args__ = (UniqueConstraint("message_id", "seq", name="uq_assistant_part_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("t_nexus_assistant_messages.id"), index=True)
    part_type: Mapped[str] = mapped_column(String(32), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[dict[str, Any]] = mapped_column("payload", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    message: Mapped[AssistantMessage] = relationship(back_populates="parts")


class AssistantContextRef(Base):
    """会话与业务对象关联，用于解析“刚才那个任务”“这个员工”等上下文。"""

    __tablename__ = "t_nexus_assistant_context_refs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("t_nexus_assistant_conversations.id"), index=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ref_type: Mapped[str] = mapped_column(String(32), index=True)
    ref_id: Mapped[str] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class AssistantOperation(Base):
    """prepare → confirm 两步操作；operation_id 同时作为幂等键。"""

    __tablename__ = "t_nexus_assistant_operations"
    __table_args__ = (Index("ix_assistant_operation_conversation", "conversation_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("t_nexus_assistant_conversations.id"), nullable=True
    )
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[str] = mapped_column(String(120), index=True)
    operation_type: Mapped[str] = mapped_column(String(48), index=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="L2")
    status: Mapped[str] = mapped_column(String(32), default="PREPARED", index=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    preview_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class AssistantToolCall(Base):
    """Tool Call 审计：用户说了什么 → Agent 调了什么 → 平台执行了什么。"""

    __tablename__ = "t_nexus_assistant_tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opencode_session_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(160), index=True)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="COMPLETED", index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)

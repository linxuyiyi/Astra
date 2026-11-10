from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME, LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.runtime.presentation import RuntimeProjection

Text = Text().with_variant(LONGTEXT(), "mysql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Persist UTC without relying on a database-specific timezone column."""

    impl = DateTime().with_variant(DATETIME(fsp=6), "mysql")
    cache_ok = True

    def process_bind_param(self, value, _dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, _dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class TaskCategory(Base):
    __tablename__ = "t_nexus_task_categories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    task_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    question_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class AgentWorker(RuntimeProjection, Base):
    """一个 Worker 对应一份环境配置及一个当前连接身份。"""

    __tablename__ = "t_nexus_agent_workers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    status: Mapped[str] = mapped_column(String(32), default="OFFLINE", index=True)
    task_types: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["ISSUE", "REQUIREMENT"])
    category_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    runtime_engine: Mapped[str] = mapped_column(String(16), default="opencode")
    mode: Mapped[str] = mapped_column(String(32), default="LINUX_REMOTE")
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ssh_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_root: Mapped[str] = mapped_column(Text, default="/opt/nexus/workspace")
    repository_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    git_token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skill_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    mcp_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    opencode_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    opencode_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    tasks: Mapped[list["Task"]] = relationship(back_populates="agent_worker")

    @property
    def has_ssh_password(self) -> bool:
        return bool(self.ssh_password)

    @property
    def has_git_token(self) -> bool:
        return bool(self.git_token_ciphertext)


class Task(RuntimeProjection, Base):
    __tablename__ = "t_nexus_tasks"
    __table_args__ = (Index("ix_tasks_dispatch", "status", "priority", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    task_type: Mapped[str] = mapped_column(String(80), index=True)
    category_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    priority: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    acceptance_criteria: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(64), default="manual")
    external_task_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    origin_employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    external_reference_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    agent_worker_id: Mapped[str | None] = mapped_column(
        ForeignKey("t_nexus_agent_workers.id"), nullable=True, index=True
    )
    current_session_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    workspace: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    pending_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    pending_prompt: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    waiting_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_progress: Mapped[str] = mapped_column(Text, default="等待领取")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    agent_worker: Mapped[AgentWorker | None] = relationship(back_populates="tasks")
    events: Mapped[list["Event"]] = relationship(back_populates="task", cascade="all, delete-orphan")

    @property
    def execution_released(self) -> bool:
        from app.runtime.occupancy import task_occupies

        return not task_occupies(self)


class Event(Base):
    __tablename__ = "t_nexus_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("t_nexus_tasks.id"), nullable=True, index=True)
    employee_id: Mapped[str | None] = mapped_column(ForeignKey("t_nexus_agent_workers.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    phase: Mapped[str] = mapped_column(String(80), default="system")
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)

    task: Mapped[Task | None] = relationship(back_populates="events")


class TaskWakeOutbox(Base):
    """同一事务里记下的唤醒。投递失败可重试，被新状态盖过的行不再发送。"""

    __tablename__ = "t_nexus_task_wake_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("t_nexus_events.id", ondelete="CASCADE"), unique=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    origin_employee_id: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64))
    external_task_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    hint: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class ExperienceReviewJob(Base):
    """人工确认完成后的一次独立经验复盘。"""

    __tablename__ = "t_nexus_experience_review_jobs"
    __table_args__ = (
        Index("ix_experience_review_status_retry", "status", "next_retry_at"),
        Index("ix_experience_review_status_lease", "status", "lease_until"),
        Index("ix_experience_review_task_confirmed", "task_id_snapshot", "confirmed_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    confirmation_event_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    review_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("t_nexus_tasks.id", ondelete="SET NULL"), nullable=True
    )
    task_id_snapshot: Mapped[str] = mapped_column(String(64), index=True)
    root_session_id: Mapped[str] = mapped_column(String(160))
    agent_worker_id_snapshot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    turn_anchor_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    phase: Mapped[str] = mapped_column(String(32), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    source_message_count: Mapped[int] = mapped_column(Integer, default=0)
    segment_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_segment_count: Mapped[int] = mapped_column(Integer, default=0)
    candidate_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    zero_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32), default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ExperienceReviewSegment(Base):
    """大轨迹中的一个可独立分析、可断点恢复的片段。"""

    __tablename__ = "t_nexus_experience_review_segments"
    __table_args__ = (UniqueConstraint("job_id", "segment_index", name="uq_experience_review_segment"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("t_nexus_experience_review_jobs.id", ondelete="CASCADE"), index=True
    )
    segment_index: Mapped[int] = mapped_column(Integer)
    source_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_digest: Mapped[str] = mapped_column(String(64))
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    next_retry_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    analysis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class KnowledgeSpace(Base):
    __tablename__ = "t_nexus_knowledge_spaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("t_nexus_knowledge_spaces.id"), nullable=True, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    access_scope: Mapped[str] = mapped_column(String(160), default="public", index=True)
    created_by: Mapped[str] = mapped_column(String(120), default="system")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    items: Mapped[list["KnowledgeItem"]] = relationship(back_populates="space")


class KnowledgeItem(Base):
    __tablename__ = "t_nexus_knowledge_items"
    __table_args__ = (
        Index("ix_knowledge_items_browse", "status", "space_id", "updated_at"),
        Index("ix_knowledge_items_experience", "type", "status", "published_at"),
        UniqueConstraint("dedupe_key", "fingerprint", name="uq_knowledge_dedupe"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    space_id: Mapped[str | None] = mapped_column(
        ForeignKey("t_nexus_knowledge_spaces.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(240), index=True)
    type: Mapped[str] = mapped_column(String(32), default="document", index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    content_md: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    # Sync state is independent from the business lifecycle above: a PUBLISHED
    # knowledge can be FAILED to synchronize and must never roll back.
    sync_status: Mapped[str] = mapped_column(
        String(16), default="not_synced", server_default="not_synced", index=True
    )
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    source_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), default="unverified", index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(240), nullable=True)
    source_employee_id: Mapped[str | None] = mapped_column(
        ForeignKey("t_nexus_agent_workers.id"), nullable=True, index=True
    )
    source_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("t_nexus_tasks.id"), nullable=True, index=True
    )
    source_session_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    origin: Mapped[str] = mapped_column(String(32), default="manual", server_default="manual")
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    experience_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verification_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    source_ref_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    experience_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120))
    updated_by: Mapped[str] = mapped_column(String(120))
    published_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    agentscope_kb_id: Mapped[str] = mapped_column(String(64), default="", server_default="", index=True)
    agentscope_document_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    agentscope_sync_status: Mapped[str] = mapped_column(
        String(16), default="not_synced", server_default="not_synced"
    )
    agentscope_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_count: Mapped[int] = mapped_column(Integer, default=0)
    helpful_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    space: Mapped[KnowledgeSpace | None] = relationship(back_populates="items")
    sources: Mapped[list["KnowledgeSource"]] = relationship(
        back_populates="knowledge", cascade="all, delete-orphan"
    )
    evidence: Mapped[list["ExperienceEvidence"]] = relationship(
        back_populates="knowledge", cascade="all, delete-orphan"
    )


class KnowledgeSource(Base):
    __tablename__ = "t_nexus_knowledge_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    knowledge_id: Mapped[str] = mapped_column(
        ForeignKey("t_nexus_knowledge_items.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    knowledge: Mapped[KnowledgeItem] = relationship(back_populates="sources")


class KnowledgeRelation(Base):
    __tablename__ = "t_nexus_knowledge_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_knowledge_id",
            "target_knowledge_id",
            "relation_type",
            name="uq_knowledge_relation",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_knowledge_id: Mapped[str] = mapped_column(
        ForeignKey("t_nexus_knowledge_items.id", ondelete="CASCADE"), index=True
    )
    target_knowledge_id: Mapped[str] = mapped_column(
        ForeignKey("t_nexus_knowledge_items.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(32), default="related")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class KnowledgeUsage(Base):
    __tablename__ = "t_nexus_knowledge_usage"
    __table_args__ = (Index("ix_knowledge_usage_summary", "action", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    knowledge_id: Mapped[str | None] = mapped_column(
        ForeignKey("t_nexus_knowledge_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    employee_id: Mapped[str | None] = mapped_column(
        ForeignKey("t_nexus_agent_workers.id"), nullable=True, index=True
    )
    task_id: Mapped[str | None] = mapped_column(ForeignKey("t_nexus_tasks.id"), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(80), index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class KnowledgeFeedback(Base):
    __tablename__ = "t_nexus_knowledge_feedback"
    __table_args__ = (
        UniqueConstraint("knowledge_id", "actor_key", "feedback_type", name="uq_knowledge_feedback_actor"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    knowledge_id: Mapped[str | None] = mapped_column(
        ForeignKey("t_nexus_knowledge_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_key: Mapped[str] = mapped_column(String(200))
    user_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    employee_id: Mapped[str | None] = mapped_column(
        ForeignKey("t_nexus_agent_workers.id"), nullable=True, index=True
    )
    feedback_type: Mapped[str] = mapped_column(String(32), index=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("t_nexus_tasks.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class KnowledgeFavorite(Base):
    __tablename__ = "t_nexus_knowledge_favorites"
    __table_args__ = (UniqueConstraint("knowledge_id", "user_id", name="uq_knowledge_favorite_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    knowledge_id: Mapped[str] = mapped_column(
        ForeignKey("t_nexus_knowledge_items.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ExperienceEvidence(Base):
    __tablename__ = "t_nexus_experience_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    knowledge_id: Mapped[str] = mapped_column(
        ForeignKey("t_nexus_knowledge_items.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str | None] = mapped_column(ForeignKey("t_nexus_tasks.id"), nullable=True, index=True)
    source_session_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    part_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_type: Mapped[str] = mapped_column(String(64))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_system: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    native_session_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    validation_status: Mapped[str] = mapped_column(
        String(16), default="unverified", server_default="unverified"
    )
    captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    knowledge: Mapped[KnowledgeItem] = relationship(back_populates="evidence")


class KnowledgeAudit(Base):
    __tablename__ = "t_nexus_knowledge_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    knowledge_id: Mapped[str] = mapped_column(
        ForeignKey("t_nexus_knowledge_items.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(48), index=True)
    actor_id: Mapped[str] = mapped_column(String(120), index=True)
    actor_role: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class WorkerMaintenanceOperation(Base):
    """独立持久化的 Agent Worker 维护操作；HTTP 断线不改变它。"""

    __tablename__ = "t_nexus_worker_maintenance_operations"
    __table_args__ = (
        UniqueConstraint("active_worker_id", name="uq_worker_maintenance_active_worker"),
        Index("ix_worker_maintenance_state_lease", "state", "lease_until"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_worker_id: Mapped[str] = mapped_column(
        ForeignKey("t_nexus_agent_workers.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), default="QUEUED", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued", server_default="queued")
    requested_by: Mapped[str] = mapped_column(String(120), default="api")
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    target_revision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    before_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_channel: Mapped[str | None] = mapped_column(String(16), nullable=True)
    execution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_of_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class WorkerMaintenanceEvent(Base):
    """维护步骤事件；自增 id 作为 SSE 可续传游标。"""

    __tablename__ = "t_nexus_worker_maintenance_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("t_nexus_worker_maintenance_operations.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    type: Mapped[str] = mapped_column(String(32), default="step")
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)

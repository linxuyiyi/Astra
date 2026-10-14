# -*- coding: utf-8 -*-
"""Nexus 业务后端的 SQLAlchemy async + SQLite 最小存储。

这里只保存 **必要的业务扩展**（员工领域、职责/工作指令编辑字段、头像、
启用状态、资源默认关联与原生 Agent 关联），绝不复制 AgentScope 原生的
消息、AgentState、Memory 或 Session；姓名与实际执行 prompt 以原生
``AgentRecord`` 为事实源。

* 一个员工一行，``agent_id`` 唯一指向原生顶层 ``AgentRecord.id``；创建意图
  先落库时 ``agent_id`` 可为空，待核对原生记录后再绑定；
* ``normalized_name`` / ``normalized_domain`` 是忽略大小写的唯一性索引，
  由数据库约束兜底并发重复；
* ``sync_state`` 与 ``write_intent`` 是跨服务写入的最小事实源：网络调用前先
  持久化非 ready 与最少写入意图，核对原生记录确认一致后才回到 ready，禁止
  用可运行的 ready 掩盖未确认写入；
* Skill/Knowledge/Tool 默认关联字段为后续资源绑定任务预留，当前不写入
  非空值，也不据此宣称能力已生效；
* 新库直接 ``create_tables``，不做旧模型迁移。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Nexus 业务扩展表基类。"""


class Employee(Base):
    """数字员工的业务扩展记录。

    事实源划分见模块 docstring：原生负责身份与执行 prompt，本表负责产品
    分栏、唯一性与关联。
    """

    __tablename__ = "nexus_employees"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 创建意图先落库时为空；核对到原生顶层 id 后再绑定，故允许 nullable。
    agent_id: Mapped[str | None] = mapped_column(
        String(128),
        unique=True,
        index=True,
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(24))
    normalized_name: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
    )
    domain: Mapped[str] = mapped_column(String(42))
    normalized_domain: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        index=True,
    )
    responsibilities: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text)
    avatar: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 以下资源默认关联留给后续能力绑定任务；当前只保存空列表，不假称生效。
    knowledge: Mapped[list] = mapped_column(JSON, default=list)
    skills: Mapped[list] = mapped_column(JSON, default=list)
    tools: Mapped[list] = mapped_column(JSON, default=list)
    # "ready"：本地与原生一致；"creating"/"updating"：已持久化写入意图但
    # 原生结果未确认；"incomplete"：结果不明确，禁止用于新运行。
    sync_state: Mapped[str] = mapped_column(String(16), default="ready")
    # 单调写入修订号：每次创建/编辑写入意图时递增，失败或恢复都不回退，供
    # 原生服务按修订拒绝迟到的旧写请求。当前原生 prompt 里的修订标记与已接受
    # 修订以此为准。
    write_revision: Mapped[int] = mapped_column(Integer, default=0)
    # 最小写入意图（op 与目标字段、编译后的 prompt），供 reconcile 核对原生
    # 记录后恢复 ready；无未完成写入时为 None。
    write_intent: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class Conversation(Base):
    """匿名 owner 与原生 Session 的最小业务关联（任务 3.2.2）。

    事实源划分：原生 Agent/Session/消息仍是执行与内容事实源；本表只保存
    owner 归属、员工关联、唯一原生 Session 绑定、能力快照与必要的创建状态
    索引，绝不复制原生消息、AgentState、Memory 或模型参数。

    * ``session_id`` 唯一指向原生 Session 顶层 id；创建结果未知时为空，待
      核对后按稳定业务 id 恢复；
    * ``status`` 为 ``creating``（原生 Session 已请求但能力未全部确认）、
      ``ready``（原生 Session、模型、Knowledge/Skill/MCP 全部确认）、
      ``incomplete``（无法判定，禁止新运行）；
    * ``capability_snapshot`` 保存创建时实际使用的 Knowledge/Skill/MCP ID，
      供运行 guard 按会话既有能力核对，不因员工默认关联后续变化而漂移；
    * ``(owner, client_key)`` 唯一约束实现同一客户端幂等创建键不重复 Session。
    """

    __tablename__ = "nexus_conversations"
    __table_args__ = (
        UniqueConstraint(
            "owner",
            "client_key",
            name="uq_conversation_owner_client_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    employee_id: Mapped[str] = mapped_column(String(64), index=True)
    # 创建/绑定后的原生顶层 Agent id 快照；未确认时为空。
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 唯一原生 Session id；创建结果未知时为空，恢复后绑定。
    session_id: Mapped[str | None] = mapped_column(
        String(128),
        unique=True,
        index=True,
        nullable=True,
    )
    # 精确原生 Session.name 关联标记，用于未知结果时按唯一名称核对恢复。
    session_marker: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(72))
    capability_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # 最小创建意图（瞬时）：占位落盘时持久化本次创建的目标（原生 Agent id、
    # 期望模型配置、Knowledge/Skill/MCP 稳定 ID、创建时服务代次）。恢复只按此
    # 意图核对原生 Session，绝不从后续可能被编辑的 Employee 默认值重新猜目标。
    # 同一字段同时承载 Knowledge 创建目标，不另建机制。**转为 ready 时清空**：
    # 已核对的原生绑定即事实源，不再复制原生模型参数；未完成必须保留以核对。
    create_intent: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        default=None,
    )
    # 幂等键：同一 owner 下重复提交返回同一 Conversation，不重复建 Session。
    client_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="creating")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class Database:
    """异步 SQLite 存储：建表、会话工厂与销毁。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{path}",
            # 并发写入时让 SQLite 等待锁而不是立即报 locked；唯一约束仍兜底。
            connect_args={"timeout": 30},
        )
        self._sessionmaker = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

    @property
    def path(self) -> Path:
        return self._path

    async def init(self) -> None:
        """建表并做最小 schema 升级，保持当前开发数据库可用。

        只对已存在的 ``nexus_conversations`` 表补齐本版本新增的 ``create_intent``
        列；不迁移旧工程数据、不引入版本兼容平台。
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await self._ensure_conversation_columns(conn)

    async def _ensure_conversation_columns(self, conn) -> None:
        result = await conn.exec_driver_sql(
            "PRAGMA table_info(nexus_conversations)",
        )
        columns = {row[1] for row in result}
        if columns and "create_intent" not in columns:
            await conn.exec_driver_sql(
                "ALTER TABLE nexus_conversations ADD COLUMN create_intent JSON",
            )

    async def dispose(self) -> None:
        await self._engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            yield session

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

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text
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
        """建表（仅新库；无旧模型迁移）。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            yield session

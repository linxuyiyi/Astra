"""官方 AgentScope SQL 存储装配与显式初始化。"""

from __future__ import annotations

import argparse
import asyncio
import inspect as python_inspect
import os
from contextlib import asynccontextmanager
from pathlib import Path


def database_url() -> str:
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    raw = os.environ.get("AGENTSCOPE_SQL_URL", "").strip()
    if not raw:
        raise RuntimeError("请配置独立 AgentScope 数据库 AGENTSCOPE_SQL_URL")
    try:
        parsed = make_url(raw)
    except (ArgumentError, ValueError):
        raise RuntimeError("AGENTSCOPE_SQL_URL 不是有效 SQL 数据库地址") from None
    if parsed.drivername not in {"mysql+asyncmy", "mysql+aiomysql", "sqlite+aiosqlite"}:
        raise RuntimeError(
            "AGENTSCOPE_SQL_URL 需使用 MySQL 异步驱动或显式 SQLite 开发库"
        )
    return raw


def create_storage():
    from agentscope.app.storage import AsyncSQLAlchemyStorage

    return AsyncSQLAlchemyStorage(
        database_url(),
        create_tables=False,
        auto_migrate=False,
        engine_kwargs={"pool_pre_ping": True, "pool_recycle": 1800},
    )


def migration_config():
    from agentscope.app.storage import AsyncSQLAlchemyStorage
    from alembic.config import Config

    path = Path(python_inspect.getfile(AsyncSQLAlchemyStorage)).parent / "_alembic"
    config = Config()
    config.set_main_option("script_location", str(path))
    # ConfigParser treats percent as interpolation; engines must receive the original URL.
    config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
    return config


def official_schema():
    from agentscope.app.storage._sql._tables import _Base
    from alembic.script import ScriptDirectory

    return _Base.metadata, ScriptDirectory.from_config(
        migration_config()
    ).get_current_head()


async def check_schema(*, allow_empty: bool = False) -> None:
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import create_async_engine

    metadata, head = official_schema()
    engine = create_async_engine(database_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
            if allow_empty and not tables:
                return
            if tables - set(metadata.tables) - {"alembic_version"}:
                raise RuntimeError(
                    "AgentScope 需使用独立空数据库，不能复用 Nexus 或其他业务库"
                )
            if "alembic_version" not in tables or not set(metadata.tables).issubset(
                tables
            ):
                raise RuntimeError(
                    "AgentScope schema 未初始化；请执行 agentscope_sql.py init"
                )
            versions = (
                (
                    await connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    )
                )
                .scalars()
                .all()
            )
            if versions != [head]:
                raise RuntimeError(
                    "AgentScope schema 版本不匹配；请执行官方 SQL 初始化"
                )
            if engine.dialect.name == "mysql" and not allow_empty:
                precision = await message_precision(connection)
                if precision != 6:
                    raise RuntimeError(
                        "AgentScope 消息时间精度未初始化；请执行 agentscope_sql.py init"
                    )
            for table in metadata.tables.values():
                columns = await connection.run_sync(
                    lambda sync, name=table.name: {
                        c["name"] for c in inspect(sync).get_columns(name)
                    }
                )
                if not set(table.columns.keys()).issubset(columns):
                    raise RuntimeError(
                        "AgentScope schema 不完整；请使用新的空数据库初始化"
                    )
    except RuntimeError:
        raise
    except (SQLAlchemyError, OSError):
        raise RuntimeError(
            "无法检查 AgentScope SQL 数据库；请检查连接与数据库权限"
        ) from None
    finally:
        await engine.dispose()


async def message_precision(connection):
    from sqlalchemy import text

    return (
        await connection.execute(
            text(
                "SELECT DATETIME_PRECISION FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='messages' "
                "AND COLUMN_NAME='created_at'"
            )
        )
    ).scalar_one()


async def configure_mysql_precision() -> None:
    """官方 2.0.10 用 DATETIME(0)；部署时保留其消息排序需要的微秒。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url())
    try:
        if engine.dialect.name == "mysql":
            async with engine.begin() as connection:
                if await message_precision(connection) != 6:
                    await connection.execute(
                        text(
                            "ALTER TABLE messages MODIFY COLUMN created_at DATETIME(6) NOT NULL"
                        )
                    )
    finally:
        await engine.dispose()


async def initialize_database() -> None:
    from alembic import command
    from alembic.util.exc import CommandError
    from sqlalchemy.exc import SQLAlchemyError

    await check_schema(allow_empty=True)
    try:
        await asyncio.to_thread(command.upgrade, migration_config(), "head")
        await configure_mysql_precision()
    except (SQLAlchemyError, CommandError, OSError):
        raise RuntimeError(
            "AgentScope 官方 SQL 初始化失败；请检查数据库和部署日志"
        ) from None
    await check_schema()


def guard_lifespan(app) -> None:
    """先校验数据库，进入/退出存储仍交给官方应用生命周期。"""
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(scope):
        await check_schema()
        async with original(scope):
            yield

    app.router.lifespan_context = lifespan


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AgentScope 官方 SQL 数据库初始化/预检"
    )
    parser.add_argument("action", choices=["init", "check"])
    arguments = parser.parse_args()
    try:
        asyncio.run(
            initialize_database() if arguments.action == "init" else check_schema()
        )
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")
    print(
        "AgentScope SQL 初始化完成"
        if arguments.action == "init"
        else "AgentScope SQL schema 已就绪"
    )


if __name__ == "__main__":
    main()

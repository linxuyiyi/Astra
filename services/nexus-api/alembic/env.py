import asyncio
from logging.config import fileConfig

from sqlalchemy import inspect, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

# Register all business tables before autogenerate/upgrade.
import app.gateway.models  # noqa: E402,F401
import nexus_api.db_models  # noqa: E402,F401
from alembic import context
from app.config.settings import get_settings
from app.models import Base
from app.sql_storage import assert_current_schema

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
database_url = get_settings().sqlalchemy_database_url
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    tables = set(inspect(connection).get_table_names())
    if "alembic_version" in tables:
        versions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        if versions and versions != ["0001_init"]:
            raise RuntimeError("不支持历史数据库升级；请使用新的空数据库执行初始化")
        if versions == ["0001_init"]:
            assert_current_schema(connection)
        if not versions and tables - {"alembic_version"}:
            raise RuntimeError("数据库已有未标记的表；请使用新的空数据库执行初始化")
    elif tables:
        raise RuntimeError("数据库已有未标记的表；请使用新的空数据库执行初始化")
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(database_url, poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
            await connection.commit()
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())

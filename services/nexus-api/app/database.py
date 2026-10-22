from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from anyio import CancelScope
from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import get_settings

settings = get_settings()
database_url = settings.sqlalchemy_database_url
if database_url.startswith("sqlite"):
    settings.resolved_data_dir.mkdir(parents=True, exist_ok=True)

engine_options: dict = {"pool_pre_ping": True, "pool_reset_on_return": "rollback"}
if database_url.startswith("mysql"):
    engine_options["pool_recycle"] = settings.database_pool_recycle

engine = create_async_engine(database_url, **engine_options)


if database_url.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        # SSE readers and independent control/agent workers share the local DB.
        # WAL prevents long-lived readers from blocking runtime projection writes.
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()


if database_url.startswith("mysql"):

    @event.listens_for(engine.sync_engine, "connect")
    def configure_mysql_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET time_zone = '+00:00'")
            cursor.execute(f"SET NAMES {settings.database_charset}")
            cursor.execute(
                f"SET SESSION innodb_lock_wait_timeout = {int(settings.database_lock_wait_timeout)}"
            )
        finally:
            cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def _close_session(session: AsyncSession) -> None:
    # Always return pooled connections transaction-clean, including cancellation.
    with CancelScope(shield=True):
        if session.in_transaction():
            await session.rollback()
        await session.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    session = SessionLocal()
    try:
        yield session
    finally:
        # ASGI disconnects use level cancellation; shield rollback/close so the
        # pooled DB connection is not abandoned while a page or SSE closes.
        await _close_session(session)


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncSession]:
    """Celery-safe session scope with explicit rollback on every exit path."""

    session = SessionLocal()
    try:
        yield session
    except BaseException:
        with CancelScope(shield=True):
            if session.in_transaction():
                await session.rollback()
        raise
    finally:
        await _close_session(session)


async def init_database() -> None:
    from app.sql_storage import CURRENT_REVISION, VERSION, assert_current_schema, metadata

    def create_unversioned_schema(connection) -> None:
        if inspect(connection).get_table_names():
            assert_current_schema(connection, check_columns=False)
            return
        metadata().create_all(connection)
        VERSION.create(connection)
        connection.execute(VERSION.insert().values(version_num=CURRENT_REVISION))

    async with engine.begin() as connection:
        await connection.run_sync(create_unversioned_schema)


async def validate_database() -> None:
    from app.sql_storage import assert_current_schema

    async with engine.connect() as connection:
        await connection.run_sync(assert_current_schema)

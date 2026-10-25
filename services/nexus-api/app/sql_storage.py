"""共享数据库配置、同步目录连接与全新 schema 校验。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from sqlalchemy import Column, MetaData, String, Table, create_engine, event, inspect, select
from sqlalchemy.engine import URL, Connection, Engine, make_url

from app.config.settings import get_settings

CURRENT_REVISION = "0005_dsh_runtime"
VERSION = Table("alembic_version", MetaData(), Column("version_num", String(32), primary_key=True))
NEW_DATABASE_MESSAGE = (
    "数据库 schema 未就绪；请执行 alembic upgrade head。早期历史库请使用新的空数据库，不支持导入"
)


def metadata():
    import app.gateway.models  # noqa: F401
    import nexus_api.db_models  # noqa: F401
    from app.models import Base

    return Base.metadata


def sync_url(url: str | URL | Path | None = None) -> URL:
    if isinstance(url, Path):
        return make_url(f"sqlite:///{url.resolve().as_posix()}")
    parsed = make_url(url or get_settings().sqlalchemy_database_url)
    backend = parsed.get_backend_name()
    if backend not in {"mysql", "sqlite"}:
        raise ValueError("数据库只支持 MySQL 或 SQLite")
    return parsed.set(drivername="mysql+pymysql" if backend == "mysql" else "sqlite")


@lru_cache(maxsize=32)
def _engine(url: URL) -> Engine:
    settings = get_settings()
    options = {"pool_pre_ping": True, "pool_reset_on_return": "rollback"}
    if url.get_backend_name() == "mysql":
        options["pool_recycle"] = settings.database_pool_recycle
    else:
        options["connect_args"] = {"check_same_thread": False, "timeout": 10}
    engine = create_engine(url, **options)

    @event.listens_for(engine, "connect")
    def configure_connection(connection, _record):
        cursor = connection.cursor()
        try:
            if engine.dialect.name == "mysql":
                cursor.execute("SET time_zone = '+00:00'")
                cursor.execute("SET NAMES utf8mb4")
                cursor.execute(
                    f"SET SESSION innodb_lock_wait_timeout = {settings.database_lock_wait_timeout}"
                )
            else:
                cursor.execute("PRAGMA busy_timeout=10000")
                cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    return engine


def connect(url: str | URL | Path | None = None) -> Connection:
    """目录与任务使用同一 URL；Path 仅用于显式隔离的 SQLite 测试库。"""
    return _engine(sync_url(url)).connect()


def assert_current_schema(connection: Connection, *, check_columns: bool = True) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "alembic_version" not in tables:
        raise RuntimeError(NEW_DATABASE_MESSAGE)
    versions = connection.execute(select(VERSION.c.version_num)).scalars().all()
    if versions != [CURRENT_REVISION]:
        raise RuntimeError(NEW_DATABASE_MESSAGE)
    for table in metadata().tables.values():
        if table.name not in tables:
            raise RuntimeError(NEW_DATABASE_MESSAGE)
        if check_columns:
            columns = {column["name"] for column in inspector.get_columns(table.name)}
            if not set(table.columns.keys()).issubset(columns):
                raise RuntimeError(NEW_DATABASE_MESSAGE)


def init_schema() -> None:
    """服务启动只验证 schema；DDL 由部署阶段的 Alembic 执行。"""
    with connect() as connection:
        assert_current_schema(connection)


def create_test_schema(url: str | URL | Path) -> None:
    """显式创建隔离测试库。生产初始化使用固化的 Alembic init。"""
    with _engine(sync_url(url)).begin() as connection:
        tables = set(inspect(connection).get_table_names())
        if tables:
            assert_current_schema(connection)
            return
        metadata().create_all(connection)
        VERSION.create(connection)
        connection.execute(VERSION.insert().values(version_num=CURRENT_REVISION))

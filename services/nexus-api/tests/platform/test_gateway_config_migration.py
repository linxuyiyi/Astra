"""0032 删除版本历史，同时保留已保存配置和永久吊销记录。"""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import MetaData, Table, create_engine, inspect, select

from app.gateway.models import (
    GatewayAuditEvent,
    GatewayHealthCheck,
    GatewayKey,
    GatewayNode,
    GatewayPool,
    GatewayRequest,
    GatewayRoute,
    utcnow,
)
from app.gateway.security import gateway_key_digest

BACKEND = Path(__file__).resolve().parents[2]
RETAINED_MODELS = (
    GatewayPool,
    GatewayNode,
    GatewayRoute,
    GatewayKey,
    GatewayHealthCheck,
    GatewayRequest,
    GatewayAuditEvent,
)


def upgrade(database_path, revision):
    environment = {
        **os.environ,
        "ASTRA_DATABASE_URL": f"sqlite+aiosqlite:///{database_path.as_posix()}",
        "ASTRA_ENVIRONMENT": "development",
        "ASTRA_SEED_DEMO": "false",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def legacy_table(model, connection=None):
    """历史 revision 的表还没有 t_nexus_ 前缀。

    复制 ORM 列时保留 Python 默认值（旧表的 NOT NULL 列由默认值填充），
    但在提供连接时剔除该 revision 之后才新增的列。
    """

    table = model.__table__.to_metadata(
        MetaData(), name=model.__tablename__.removeprefix("t_nexus_")
    )
    if connection is not None:
        existing = {column["name"] for column in inspect(connection).get_columns(table.name)}
        for column in list(table.columns):
            if column.name not in existing:
                table._columns.remove(column)
    return table


def legacy_reflected(model, connection):
    """历史 revision 的列可能少于当前 ORM，按数据库实际结构反射。"""
    return Table(model.__tablename__.removeprefix("t_nexus_"), MetaData(), autoload_with=connection)


def retained_rows(connection):
    return {
        model.__tablename__: [
            dict(row)
            for row in connection.execute(select(legacy_reflected(model, connection))).mappings()
        ]
        for model in RETAINED_MODELS
    }


def test_0032_drops_history_without_applying_old_snapshot_or_changing_saved_data(tmp_path):
    database_path = tmp_path / "gateway-upgrade.db"
    upgrade(database_path, "0031_api_gateway_control")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.begin() as connection:
            connection.execute(
                legacy_table(GatewayPool, connection).insert().values(id="gp_keep", name="保留资源池")
            )
            connection.execute(
                legacy_table(GatewayNode, connection).insert().values(
                    id="gn_keep",
                    name="保留节点",
                    pool_id="gp_keep",
                    base_url="https://saved.example.com",
                    models=["saved-model"],
                )
            )
            connection.execute(
                legacy_table(GatewayRoute, connection).insert().values(
                    id="gr_keep", model="saved-model", pool_id="gp_keep"
                )
            )
            connection.execute(
                legacy_table(GatewayKey, connection).insert().values(
                    id="gk_revoked",
                    name="永久吊销 Key",
                    key_prefix="agw_revoked",
                    key_digest=gateway_key_digest("agw_revoked"),
                    enabled=False,
                    revoked_at=utcnow(),
                )
            )
            connection.execute(
                legacy_table(GatewayHealthCheck, connection).insert().values(
                    node_id="gn_keep",
                    probe_type="NETWORK",
                    success=True,
                )
            )
            connection.execute(
                legacy_table(GatewayRequest, connection).insert().values(
                    id="req_keep",
                    key_id="gk_revoked",
                    node_id="gn_keep",
                    status_code=200,
                    total_tokens=9,
                )
            )
            connection.execute(
                legacy_table(GatewayAuditEvent, connection).insert().values(
                    actor="astra",
                    actor_role="system",
                    action="key.revoke",
                    resource_type="key",
                    resource_id="gk_revoked",
                )
            )
            history = Table("gateway_config_versions", MetaData(), autoload_with=connection)
            connection.execute(
                history.insert().values(
                    version=1,
                    status="CURRENT",
                    note="过期的发布配置",
                    published_by="legacy-admin",
                    published_at=utcnow(),
                    created_at=utcnow(),
                    snapshot={
                        "nodes": [{"id": "gn_keep", "base_url": "https://obsolete.example.com"}],
                        "keys": [{"id": "gk_revoked", "enabled": True, "revoked_at": None}],
                    },
                )
            )
            before = retained_rows(connection)
    finally:
        engine.dispose()

    upgrade(database_path, "0032_gateway_saved_config")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            assert "gateway_config_versions" not in inspect(connection).get_table_names()
            assert retained_rows(connection) == before
            node = legacy_reflected(GatewayNode, connection)
            assert connection.scalar(select(node.c.base_url)) == "https://saved.example.com"
            key_table = legacy_reflected(GatewayKey, connection)
            key = connection.execute(select(key_table.c.enabled, key_table.c.revoked_at)).one()
            assert key.enabled is False
            assert key.revoked_at is not None
    finally:
        engine.dispose()


def test_0034_adds_deletion_marker_without_changing_existing_keys(tmp_path):
    database_path = tmp_path / "key-deletion-upgrade.db"
    upgrade(database_path, "0033_employee_character")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.begin() as connection:
            keys = Table("gateway_keys", MetaData(), autoload_with=connection)
            connection.execute(keys.insert().values(
                id="gk_keep", name="已有 Key", key_prefix="agw_keep", key_digest="keep-digest",
                allowed_models=[], max_concurrency=0, enabled=True,
                created_at=utcnow(), updated_at=utcnow(),
            ))
            before = dict(connection.execute(select(keys)).mappings().one())
    finally:
        engine.dispose()

    upgrade(database_path, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.begin() as connection:
            keys = Table("t_nexus_gateway_keys", MetaData(), autoload_with=connection)
            upgraded = dict(connection.execute(select(keys)).mappings().one())
            assert upgraded.pop("deleted_at") is None
            assert upgraded == before
            connection.execute(keys.update().values(enabled=False, revoked_at=utcnow(), deleted_at=utcnow()))
            deleted = dict(connection.execute(select(keys)).mappings().one())
    finally:
        engine.dispose()

    environment = {
        **os.environ, "ASTRA_DATABASE_URL": f"sqlite+aiosqlite:///{database_path.as_posix()}",
        "ASTRA_ENVIRONMENT": "development", "ASTRA_SEED_DEMO": "false",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0033_employee_character"],
        cwd=BACKEND, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            keys = Table("gateway_keys", MetaData(), autoload_with=connection)
            assert "deleted_at" not in keys.c
            deleted.pop("deleted_at")
            assert dict(connection.execute(select(keys)).mappings().one()) == deleted
    finally:
        engine.dispose()

"""0032 删除版本历史，同时保留已保存配置和永久吊销记录。"""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

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
            # 0031 的池表还有 weight 列，而当前 ORM 已删除；用历史结构直接写入。
            connection.execute(
                text(
                    "INSERT INTO gateway_pools "
                    "(id, name, description, weight, enabled, sort_order, created_at, updated_at) "
                    "VALUES ('gp_keep', '保留资源池', '', 1, 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
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
                text(
                    "INSERT INTO gateway_routes "
                    "(id, model, pool_id, description, enabled, weight, "
                    "created_at, updated_at) "
                    "VALUES ('gr_keep', 'saved-model', 'gp_keep', '', 1, 1, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
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


def test_0057_normalizes_node_weight_and_adds_default_pool(tmp_path):
    from sqlalchemy import text

    database_path = tmp_path / "gateway-weight.db"
    upgrade(database_path, "0056_gateway_probe_strategy")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO t_nexus_gateway_pools "
                "(id, name, description, weight, enabled, sort_order, created_at, updated_at) "
                "VALUES ('gp_keep', '保留池', '', 1, 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        node_sql = (
            "INSERT INTO t_nexus_gateway_nodes "
            "(id, name, pool_id, base_url, auth_type, auth_header, health_path, probe_strategy, "
            "models, weight, max_concurrency, quota_policy, config_state, scheduling_state, "
            "health_state, success_streak, failure_streak, health_detail, sort_order, "
            "created_at, updated_at) VALUES (:id, :name, :pool, 'https://a.example.com', 'BEARER', "
            "'Authorization', '/health', 'SSE', '[]', :weight, 0, 'STOP', 'DISABLED', 'DRAINING', "
            "'UNKNOWN', 0, 0, '{}', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.execute(
            text(node_sql),
            {"id": "gn_zero", "name": "零权重节点", "pool": "gp_keep", "weight": 0},
        )
        connection.execute(
            text(node_sql),
            {"id": "gn_three", "name": "权重三节点", "pool": "gp_keep", "weight": 3},
        )
        connection.execute(
            text(node_sql),
            {"id": "gn_unassigned", "name": "未分配节点", "pool": None, "weight": 1},
        )

    upgrade(database_path, "head")

    with engine.connect() as connection:
        nodes = {
            row.id: (row.weight, row.pool_id)
            for row in connection.execute(
                text("SELECT id, weight, pool_id FROM t_nexus_gateway_nodes")
            )
        }
        pools = {
            row.id: row.name
            for row in connection.execute(text("SELECT id, name FROM t_nexus_gateway_pools"))
        }

    # 零值迁移到 1，其余权重不变；节点归属不被改写，未分配节点仍保持 NULL。
    assert nodes == {
        "gn_zero": (1, "gp_keep"),
        "gn_three": (3, "gp_keep"),
        "gn_unassigned": (1, None),
    }
    assert len(nodes) == 3
    # 只新增一个默认池，原有池保留，行数核对：1 + 1。
    assert pools == {"gp_keep": "保留池", "gp_default": "默认资源池"}

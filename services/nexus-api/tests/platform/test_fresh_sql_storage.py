"""全新 schema 初始化、重复执行和旧库拒绝。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from app.sql_storage import CURRENT_REVISION, metadata

SERVICE = Path(__file__).resolve().parents[2]


def upgrade(path: Path):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=SERVICE,
        env={**os.environ, "ASTRA_DATABASE_URL": f"sqlite+aiosqlite:///{path}"},
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_initial_schema_is_complete_and_repeatable(tmp_path):
    database = tmp_path / "fresh.db"
    first = upgrade(database)
    assert first.returncode == 0, first.stderr
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
                == CURRENT_REVISION
            )
            assert not compare_metadata(MigrationContext.configure(connection), metadata())
            connection.execute(
                text("INSERT INTO t_nexus_settings (`key`, value) VALUES ('init-test', '保留')")
            )
        second = upgrade(database)
        assert second.returncode == 0, second.stderr
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT value FROM t_nexus_settings WHERE `key` = 'init-test'")
                ).scalar()
                == "保留"
            )
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM t_nexus_gateway_pools WHERE id = 'gp_default'")
                ).scalar()
                == 1
            )
    finally:
        engine.dispose()


def test_published_init_upgrades_without_losing_routes(tmp_path):
    database = tmp_path / "published.db"
    environment = {**os.environ, "ASTRA_DATABASE_URL": f"sqlite+aiosqlite:///{database}"}
    first = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0001_init"],
        cwd=SERVICE, env=environment, text=True, capture_output=True, timeout=30,
    )
    assert first.returncode == 0, first.stderr
    with sqlite3.connect(database) as connection:
        connection.execute("""
            INSERT INTO t_nexus_gateway_routes
            (id, model, pool_id, description, enabled, created_at, updated_at)
            VALUES ('route-retained', 'retained-model', 'gp_default', '', 1, datetime('now'), datetime('now'))
        """)
    from app.gateway.models import GatewayNode, GatewayPool

    old_engine = create_engine(f"sqlite:///{database}")
    with old_engine.begin() as connection:
        connection.execute(GatewayPool.__table__.insert().values(id="embed-pool", name="Embedding"))
        connection.execute(GatewayNode.__table__.insert().values(
            id="embed-node", name="Embedding", pool_id="embed-pool",
            base_url="http://embedding.test", probe_strategy="EMBEDDING",
        ))
        connection.execute(text("""
            INSERT INTO t_nexus_gateway_routes
            (id, model, pool_id, description, enabled, created_at, updated_at)
            VALUES ('embed-route', 'embedding', 'embed-pool', '', 1, datetime('now'), datetime('now'))
        """))
    old_engine.dispose()
    result = upgrade(database)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT model, model_type FROM t_nexus_gateway_routes WHERE id='route-retained'"
        ).fetchone() == ("retained-model", "CHAT")
        assert connection.execute(
            "SELECT model_type FROM t_nexus_gateway_routes WHERE id='embed-route'"
        ).fetchone() == ("EMBEDDING",)


@pytest.mark.parametrize("version", [None, "0058_drop_pool_route_weight"])
def test_old_or_unversioned_database_is_untouched(tmp_path, version):
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE t_nexus_old_data (value TEXT)")
        connection.execute("INSERT INTO t_nexus_old_data VALUES ('保留旧数据')")
        if version:
            connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
            connection.execute("INSERT INTO alembic_version VALUES (?)", (version,))
    outcome = upgrade(database)
    assert outcome.returncode != 0
    assert "新的空数据库" in outcome.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM t_nexus_old_data").fetchone()[0] == "保留旧数据"
        if version:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == version


def test_connection_exit_rolls_back_uncommitted_changes(tmp_path):
    from app.sql_storage import connect, create_test_schema

    database = tmp_path / "rollback.db"
    create_test_schema(database)
    with connect(database) as connection:
        connection.execute(text("INSERT INTO t_nexus_settings (`key`, value) VALUES ('rollback', '临时')"))
    with connect(database) as connection:
        assert (
            connection.execute(text("SELECT value FROM t_nexus_settings WHERE `key`='rollback'")).first()
            is None
        )


def test_incomplete_current_schema_is_not_silently_accepted(tmp_path):
    database = tmp_path / "incomplete.db"
    assert upgrade(database).returncode == 0
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE t_nexus_settings")
    outcome = upgrade(database)
    assert outcome.returncode != 0
    assert "新的空数据库" in outcome.stderr


def test_local_auth_cleanup_preserves_business_owner_ids(tmp_path):
    database = tmp_path / "local-auth.db"
    first = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0003_gateway_speech"],
        cwd=SERVICE,
        env={**os.environ, "ASTRA_DATABASE_URL": f"sqlite+aiosqlite:///{database}"},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert first.returncode == 0, first.stderr
    with sqlite3.connect(database) as connection:
        connection.execute("""INSERT INTO t_nexus_users
            (id,username,display_name,password_hash,permissions_json,enabled,created_at)
            VALUES ('old-uid','local','临时用户','removed-secret','["*"]',1,'2026-10-24')""")
        connection.execute("INSERT INTO t_nexus_auth_sessions VALUES ('old-token','old-uid','2999-01-01')")
        connection.execute("""INSERT INTO t_nexus_conversations
            (id,employee_id,owner_id,name,created_at,updated_at)
            VALUES ('conv-retained','emp','old-uid','保留','2026-10-24','2026-10-24')""")
        connection.execute("""INSERT INTO t_nexus_channel_members
            (channel_id,member_type,member_id,role)
            VALUES ('group-retained','human','old-uid','owner')""")
    result = upgrade(database)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {"t_nexus_users", "t_nexus_auth_sessions"} & tables
        assert connection.execute("SELECT * FROM t_nexus_identity_profiles").fetchall() == []
        assert connection.execute("SELECT owner_id FROM t_nexus_conversations").fetchone() == ("old-uid",)
        assert connection.execute("SELECT member_id,role FROM t_nexus_channel_members").fetchone() == (
            "old-uid",
            "owner",
        )


def test_released_init_upgrade_preserves_opencode_binding(tmp_path):
    database = tmp_path / "released.db"
    initial = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0001_init"], cwd=SERVICE,
        env={**os.environ, "ASTRA_DATABASE_URL": f"sqlite+aiosqlite:///{database}"},
        text=True, capture_output=True, timeout=30,
    )
    assert initial.returncode == 0, initial.stderr
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO t_nexus_opencode_execution_environments "
            "(id,employee_id,mode,name,description,port,opencode_server_port,opencode_path,"
            "workspace_root,status,status_detail,labels,created_at,updated_at) "
            "VALUES (1,'worker','LINUX_REMOTE','原环境','',22,4096,'opencode',"
            "'/agentwork','READY','{}','{}','2026-10-23','2026-10-23')"
        )
        connection.execute(
            "INSERT INTO t_nexus_runtime_sessions "
            "(id,task_id,employee_id,workspace,environment,status,tool_usage,usage_by_day,"
            "model_usage,created_at,total_tokens) VALUES "
            "('ses_original','task','worker','/agentwork/task','{}','SUCCEEDED','{}','{}',"
            "'{}','2026-10-23',123)"
        )
    outcome = upgrade(database)
    assert outcome.returncode == 0, outcome.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT runtime_engine FROM t_nexus_opencode_execution_environments WHERE id=1"
        ).fetchone() == ("opencode",)
        assert connection.execute(
            "SELECT id,native_session_id,runtime_engine,fencing_token,status,total_tokens,workspace "
            "FROM t_nexus_runtime_sessions"
        ).fetchone() == (
            "ses_original", "ses_original", "opencode", 0, "SUCCEEDED", 123, "/agentwork/task"
        )
    assert upgrade(database).returncode == 0

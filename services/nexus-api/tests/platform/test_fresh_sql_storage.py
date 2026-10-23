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

from app.sql_storage import SCHEMA_REVISION, metadata

SERVICE = Path(__file__).resolve().parents[2]


def upgrade(path: Path, revision: str = "head"):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
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
                == SCHEMA_REVISION
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


def test_released_init_upgrade_preserves_opencode_binding(tmp_path):
    database = tmp_path / "released.db"
    initial = upgrade(database, "0001_init")
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
    assert "schema" in outcome.stderr

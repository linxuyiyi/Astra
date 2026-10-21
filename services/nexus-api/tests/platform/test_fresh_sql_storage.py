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

from app.sql_storage import INITIAL_REVISION, metadata

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
                == INITIAL_REVISION
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

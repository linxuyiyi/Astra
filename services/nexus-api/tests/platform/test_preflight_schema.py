"""启动预检的表清单必须跟随迁移，而不是手写退役表名。

背景：0052 给业务表加了 ``t_nexus_`` 前缀，0053 又删除了总结表；预检仍按
``agent_workers/tasks/task_summaries`` 等旧名字判断，导致 ``upgrade head``
成功的空库在启动预检里被判为缺表并退出 1。
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
BACKEND_ROOT = ROOT / "services" / "nexus-api"
PREFLIGHT_SCRIPT = ROOT / "ops" / "dev" / "preflight.py"


def _load_preflight_module():
    spec = importlib.util.spec_from_file_location("preflight_schema_under_test", PREFLIGHT_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = _load_preflight_module()

RETIRED_TABLES = {"agent_workers", "tasks", "events", "runtime_sessions", "task_summaries"}


def test_required_tables_come_from_current_models():
    required = preflight.required_table_names()
    assert required == preflight.astra_table_names()
    assert required
    assert not required & RETIRED_TABLES
    assert all(name.startswith("t_nexus_") for name in required)


def _upgrade_empty_database(database_url: str) -> None:
    env = dict(os.environ)
    env["ASTRA_DATABASE_URL"] = database_url
    env["ASTRA_SEED_DEMO"] = "false"
    env["ASTRA_LLM_WIKI_ENABLED"] = "false"
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]


async def test_preflight_passes_right_after_empty_database_upgrade(tmp_path):
    database_file = tmp_path / "nexus.db"
    database_url = f"sqlite+aiosqlite:///{database_file.as_posix()}"
    _upgrade_empty_database(database_url)

    tables = {
        row[0]
        for row in sqlite3.connect(database_file).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert preflight.required_table_names() <= tables

    settings = SimpleNamespace(
        sqlalchemy_database_url=database_url,
        database_description=f"sqlite {database_file}",
        database_pool_recycle=3600,
    )
    checks = await preflight.check_database(settings)

    migration = next(check for check in checks if check.item == "数据库表与迁移")
    assert migration.passed, migration.reason
    assert "0054_experience_review" in migration.current
    assert not [check.item for check in checks if not check.passed and check.blocking]

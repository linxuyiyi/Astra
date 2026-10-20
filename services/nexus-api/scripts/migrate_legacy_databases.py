#!/usr/bin/env python3
"""把旧的 task-platform 库和 nexus-api 库合并成统一 nexus.db。

用法（在新仓库根目录）::

    services/nexus-api/.venv/bin/python services/nexus-api/scripts/migrate_legacy_databases.py \
        --task-db services/task-platform/data/nexus.db \
        --chat-db services/nexus-api/data/nexus.sqlite3 \
        --target services/nexus-api/data/nexus.db

一次性脚本：先备份旧库，再执行。目标库已存在时会保留现有数据，只补齐缺失
的 t_nexus_* 聊天表。任务表由 Alembic 0052 迁移改成 t_nexus_* 前缀。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from nexus_api.db import LEGACY_TABLES, init_db  # noqa: E402


def checkpoint(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def copy_task_database(source: Path, target: Path) -> None:
    if not source.exists():
        print(f"[skip] 任务库不存在: {source}")
        return
    if target.exists() and target.stat().st_size > 0:
        print(f"[skip] 目标库已存在，不覆盖: {target}")
        return
    checkpoint(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    print(f"[copy] {source} -> {target}")


def upgrade_task_tables(target: Path) -> None:
    environment = {
        **os.environ,
        "ASTRA_DATABASE_URL": f"sqlite+aiosqlite:///{target.resolve().as_posix()}",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=SERVICE_ROOT,
        env=environment,
        check=True,
    )
    print("[migrate] Alembic 已升级到 head")


def copy_chat_tables(chat_database: Path, target: Path) -> None:
    if not chat_database.exists():
        print(f"[skip] 聊天库不存在: {chat_database}")
        return
    with sqlite3.connect(target) as connection:
        connection.execute("ATTACH DATABASE ? AS legacy", (str(chat_database),))
        legacy_tables = {
            row[0] for row in connection.execute("SELECT name FROM legacy.sqlite_master WHERE type='table'")
        }
        for old, new in LEGACY_TABLES.items():
            if old not in legacy_tables:
                continue
            old_columns = [row[1] for row in connection.execute(f"PRAGMA legacy.table_info('{old}')")]
            new_columns = [row[1] for row in connection.execute(f"PRAGMA main.table_info('{new}')")]
            columns = [column for column in old_columns if column in new_columns]
            if not columns:
                continue
            joined = ", ".join(f'"{column}"' for column in columns)
            connection.execute(
                f'INSERT OR REPLACE INTO main."{new}" ({joined}) '
                f'SELECT {joined} FROM legacy."{old}"'
            )
            print(f"[merge] {old} -> {new} ({len(columns)} columns)")
        connection.commit()
        connection.execute("DETACH DATABASE legacy")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-db", type=Path, default=Path("services/task-platform/data/nexus.db"))
    parser.add_argument("--chat-db", type=Path, default=Path("services/nexus-api/data/nexus.sqlite3"))
    parser.add_argument("--target", type=Path, default=Path("services/nexus-api/data/nexus.db"))
    args = parser.parse_args()

    target = args.target.resolve()
    copy_task_database(args.task_db.resolve(), target)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    upgrade_task_tables(target)
    init_db(target)
    copy_chat_tables(args.chat_db.resolve(), target)
    print(f"[done] 统一数据库: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""真实 MySQL 8.0 并发验收；仅创建并删除随机命名的隔离测试数据库。"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import AgentWorker, Task, TaskCategory, WorkerMaintenanceOperation
from app.runtime.dispatcher import Dispatcher
from app.runtime.maintenance import submit_operation

pytestmark = pytest.mark.skipif(not os.environ.get("ASTRA_TEST_MYSQL_URL"), reason="需要独立 MySQL 测试实例")


@pytest.fixture
async def mysql_case():
    url = make_url(os.environ["ASTRA_TEST_MYSQL_URL"])
    assert url.get_backend_name() == "mysql" and "test" in (url.database or "")
    name = "nexus_v150_test_" + uuid4().hex
    admin = create_async_engine(url.set(drivername="mysql+asyncmy", database="mysql"))
    async with admin.begin() as connection:
        await connection.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"))
    scoped = url.set(drivername="mysql+asyncmy", database=name)
    engine = create_async_engine(scoped)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "ASTRA_DATABASE_URL": scoped.render_as_string(hide_password=False)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        async with factory() as db:
            db.add(TaskCategory(id="cat", name="cat"))
            db.add(
                AgentWorker(
                    id="worker",
                    name="worker",
                    status="IDLE",
                    mode="LINUX_REMOTE",
                    host="worker.invalid",
                    username="root",
                    ssh_password="encrypted",
                    workspace_root="/workspace",
                    task_types=["ISSUE"],
                    category_ids=["cat"],
                )
            )
            await db.commit()
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f"DROP DATABASE `{name}`"))
        await admin.dispose()


async def test_concurrent_claims_keep_one_task_per_worker(mysql_case):
    async with mysql_case() as db:
        for index in range(8):
            db.add(
                Task(id=f"T{index}", title="task", task_type="ISSUE", category_id="cat", priority="MEDIUM")
            )
        await db.commit()

    async def claim():
        async with mysql_case() as db:
            result = await Dispatcher(db).claim_once()
            return result.id if result else None

    await asyncio.gather(*(claim() for _ in range(6)))
    async with mysql_case() as db:
        assert await db.scalar(select(func.count()).select_from(Task).where(Task.status == "RUNNING")) == 1
        assert (await db.get(AgentWorker, "worker")).status == "WORKING"


async def test_concurrent_maintenance_is_exclusive(mysql_case):
    async def submit():
        async with mysql_case() as db:
            try:
                return (await submit_operation(db, "worker", "check")).id
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 409
                return None

    results = await asyncio.gather(*(submit() for _ in range(4)))
    assert sum(item is not None for item in results) == 1
    async with mysql_case() as db:
        assert await db.scalar(select(func.count()).select_from(WorkerMaintenanceOperation)) == 1






async def test_empty_mysql_schema_matches_final_models_exactly(mysql_case):
    from sqlalchemy import inspect

    from app.sql_storage import metadata

    async with mysql_case() as db:
        connection = await db.connection()

        def verify(sync):
            inspector = inspect(sync)
            tables = metadata().tables
            assert set(inspector.get_table_names()) == set(tables) | {"alembic_version"}
            for name, table in tables.items():
                assert {column["name"] for column in inspector.get_columns(name)} == set(table.columns.keys())
            assert "t_nexus_worker_commands" not in tables
            assert "t_nexus_worker_http_slots" not in tables

        await connection.run_sync(verify)


async def test_ssh_health_disconnect_keeps_task_occupancy_and_paused_state(mysql_case, monkeypatch):
    from unittest.mock import AsyncMock

    from app.runtime import worker_connection
    from app.runtime.occupancy import occupying_clause

    monkeypatch.setattr(worker_connection, "SessionLocal", mysql_case)
    probe = AsyncMock(side_effect=[ConnectionError("SSH disconnected"), {"version": "1.18.35"}])
    monkeypatch.setattr(worker_connection.connections, "probe", probe)
    async with mysql_case() as db:
        worker = await db.get(AgentWorker, "worker")
        worker.opencode_port = 14096
        db.add(Task(id="active", title="task", task_type="ISSUE", agent_worker_id="worker", status="RUNNING"))
        await db.commit()
        db.expunge(worker)
    assert not await worker_connection.check_worker(worker)
    async with mysql_case() as db:
        assert (await db.get(AgentWorker, "worker")).status == "OFFLINE"
        assert (await db.get(Task, "active")).status == "RUNNING"
        assert await db.scalar(select(Task.id).where(occupying_clause("worker"))) == "active"
    assert await worker_connection.check_worker(worker)
    async with mysql_case() as db:
        fresh = await db.get(AgentWorker, "worker")
        assert fresh.status == "WORKING" and fresh.last_checked_at
        fresh.status = "PAUSED"
        await db.commit()
    probe.side_effect = ConnectionError("SSH disconnected again")
    assert not await worker_connection.check_worker(worker)
    async with mysql_case() as db:
        assert (await db.get(AgentWorker, "worker")).status == "PAUSED"
        assert await db.scalar(select(Task.id).where(occupying_clause("worker"))) == "active"

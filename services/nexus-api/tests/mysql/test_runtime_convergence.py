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
from app.worker_gateway.bus import MemoryRouteBus
from app.worker_gateway.protocol import envelope
from app.worker_gateway.service import WorkerGateway

pytestmark = pytest.mark.skipif(not os.environ.get("ASTRA_TEST_MYSQL_URL"), reason="需要独立 MySQL 测试实例")


@pytest.fixture
async def mysql_case():
    url = make_url(os.environ["ASTRA_TEST_MYSQL_URL"])
    assert url.get_backend_name() == "mysql" and "test" in (url.database or "")
    name = "nexus_v150_test_" + uuid4().hex
    admin = create_async_engine(url.set(drivername="mysql+asyncmy", database=None))
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


async def test_gateway_old_epoch_cannot_disconnect_or_reply(mysql_case):
    gateway = WorkerGateway(bus=MemoryRouteBus())
    async with mysql_case() as db:
        enrolled = await gateway.enroll(db, "worker")
        auth = envelope(
            request_id="auth",
            agent_worker_id="worker",
            session_id=None,
            message_type="command",
            payload={"name": "authenticate"},
        )
        first = await gateway.authenticate(db, auth, enrolled.token)
    async with mysql_case() as db:
        second = await gateway.authenticate(db, auth, enrolled.token)
        assert second.epoch > first.epoch
    async with mysql_case() as db:
        await gateway.disconnect(db, "worker", first.epoch)
    async with mysql_case() as db:
        assert (await db.get(AgentWorker, "worker")).status == "IDLE"
        assert not await gateway.epoch_is_current(db, "worker", first.epoch)


async def test_worker_deletion_respects_mysql_foreign_keys(mysql_case):
    from app.api.worker_routes import delete_worker
    from app.models import Event, WorkerCommand, WorkerHttpSlot

    async with mysql_case() as db:
        db.add(
            Task(
                id="finished",
                title="已完成任务",
                task_type="ISSUE",
                category_id="cat",
                status="SUCCEEDED",
                agent_worker_id="worker",
            )
        )
        db.add(
            WorkerCommand(
                request_id="http_finished", agent_worker_id="worker", name="http", status="succeeded", epoch=2
            )
        )
        db.add(WorkerHttpSlot(agent_worker_id="worker", epoch=2, inflight=0))
        await db.commit()
        db.add(
            Event(
                task_id="finished",
                employee_id="worker",
                event_type="TASK_SUCCEEDED",
                phase="work",
                message="已确认",
            )
        )
        await db.commit()
        await delete_worker("worker", db)
        assert await db.get(AgentWorker, "worker") is None
        assert (await db.get(Task, "finished")).agent_worker_id is None
        assert (await db.scalar(select(Event).where(Event.task_id == "finished"))).employee_id is None
        assert await db.scalar(select(WorkerCommand)) is None
        assert await db.scalar(select(WorkerHttpSlot)) is None

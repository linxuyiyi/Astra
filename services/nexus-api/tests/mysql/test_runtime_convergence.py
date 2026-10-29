"""真实 MySQL 并发回归；ASTRA_TEST_MYSQL_URL 必须是隔离测试实例。

测试用户需要在该实例创建/删除临时测试库的权限。每个用例独立迁移，
只删除自己生成的 nexus_runtime_test_<uuid> 数据库。
"""

import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Update

from app.models import (
    AgentWorker,
    CodeRepository,
    OpenCodeExecutionEnvironment,
    Task,
    TaskCategory,
    WorkerInstance,
    WorkerMaintenanceOperation,
)
from app.runtime import maintenance, remote_provisioning, worker_health
from app.runtime.dispatcher import Dispatcher
from app.runtime.lifecycle import RuntimeService

pytestmark = pytest.mark.skipif(not os.environ.get("ASTRA_TEST_MYSQL_URL"), reason="需要独立 MySQL 测试实例")


@pytest.fixture
async def mysql_case(monkeypatch):
    url = make_url(os.environ["ASTRA_TEST_MYSQL_URL"])
    assert url.get_backend_name() == "mysql" and "test" in (url.database or "")
    name = "nexus_runtime_test_" + uuid4().hex
    admin = create_async_engine(url.set(drivername="mysql+asyncmy", database=None))
    async with admin.begin() as connection:
        await connection.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"))
    scoped = url.set(drivername="mysql+asyncmy", database=name)
    engine = create_async_engine(scoped)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        migrated = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "ASTRA_DATABASE_URL": scoped.render_as_string(hide_password=False)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert migrated.returncode == 0, migrated.stderr
        async with factory() as session:
            session.add(CodeRepository(id="repo", url="https://example.invalid/repo"))
            session.add(TaskCategory(id="cat", name="cat"))
            for index in (1, 2):
                session.add(
                    AgentWorker(
                        id=f"E{index}",
                        name=f"E{index}",
                        role="build",
                        status="IDLE",
                        task_types=["ISSUE"],
                        category_ids=["cat"],
                    )
                )
            await session.flush()
            for index in (1, 2):
                session.add(
                    OpenCodeExecutionEnvironment(
                        id=index,
                        employee_id=f"E{index}",
                        repository_id="repo",
                        mode="LOCAL",
                        status="AVAILABLE",
                        workspace_root="/tmp/unused-test-workspace",
                        status_detail={"capability": {"runtime_status": "applied"}},
                    )
                )
                session.add(
                    Task(
                        id=f"T{index}",
                        title=f"T{index}",
                        task_type="ISSUE",
                        category_id="cat",
                        repository_id="repo",
                        status="PENDING",
                    )
                )
            await session.commit()
        monkeypatch.setattr(maintenance, "SessionLocal", factory)

        async def probe(*args, **kwargs):
            return {"error": "controlled probe"}

        monkeypatch.setattr(worker_health, "probe_equipment", probe)
        yield SimpleNamespace(engine=engine, factory=factory)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f"DROP DATABASE `{name}`"))
        await admin.dispose()


async def test_json_environment_lock_and_unchanged_observation(mysql_case):
    async with mysql_case.factory() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, 1)
        await remote_provisioning.guard_environment_write(session, environment)
        await session.commit()
    result = await maintenance.observe_environment(1, persist=True)
    assert result["stale"] is False
    async with mysql_case.factory() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, 1)
        assert environment.status == "UNAVAILABLE"
        assert environment.last_checked_at is not None
        assert environment.status_detail["observed"]["error"] == "controlled probe"


@pytest.mark.parametrize("task_status", ["RUNNING", "WAITING", "PENDING"])
async def test_occupied_worker_skips_probe_and_keeps_health(mysql_case, monkeypatch, task_status):
    async with mysql_case.factory() as session:
        await session.execute(
            update(Task).where(Task.id == "T1")
            .values(employee_id="E1", status=task_status, claimed_at=datetime.now(UTC))
        )
        await session.commit()

    async def unexpected_probe(*args, **kwargs):
        raise AssertionError("occupied Worker must not be probed")

    monkeypatch.setattr(worker_health, "probe_equipment", unexpected_probe)
    result = await maintenance.observe_environment(1, persist=True)
    assert result["skipped"] is True
    assert result["reason"] == "occupied_task"
    async with mysql_case.factory() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, 1)
        assert environment.status == "AVAILABLE"
        assert environment.last_checked_at is None


async def test_idle_probe_cannot_downgrade_worker_that_started_work(mysql_case, monkeypatch):
    async def start_work(*args, **kwargs):
        async with mysql_case.factory() as session:
            await session.execute(
                update(Task).where(Task.id == "T1").values(status="RUNNING", employee_id="E1")
            )
            await session.commit()
        return {"error": "late probe failure"}

    monkeypatch.setattr(worker_health, "probe_equipment", start_work)
    result = await maintenance.observe_environment(1, persist=True)
    assert result["stale"] is True
    assert result["skipped"] is True
    async with mysql_case.factory() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, 1)
        assert environment.status == "AVAILABLE"
        assert environment.last_checked_at is None


async def test_manual_check_records_skip_without_changing_busy_worker(mysql_case, monkeypatch):
    async with mysql_case.factory() as session:
        await session.execute(update(Task).where(Task.id == "T1").values(status="RUNNING", employee_id="E1"))
        await session.commit()
        operation = await maintenance.submit_operation(session, 1, "check")
        operation_id = operation.id

    async def unexpected_probe(*args, **kwargs):
        raise AssertionError("occupied Worker must not be probed")

    monkeypatch.setattr(worker_health, "probe_equipment", unexpected_probe)
    assert await maintenance.run_maintenance_once("test-owner") is True
    async with mysql_case.factory() as session:
        operation = await session.get(WorkerMaintenanceOperation, operation_id)
        assert operation.state == "SUCCEEDED"
        assert operation.stage == "skipped"
        assert operation.after_snapshot == {"skipped": True, "reason": "occupied_task"}
        environment = await session.get(OpenCodeExecutionEnvironment, 1)
        assert environment.status == "AVAILABLE"
        assert environment.last_checked_at is None


async def test_environment_guard_uses_current_read_after_an_older_snapshot(mysql_case):
    async with mysql_case.factory() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, 1)
        async with mysql_case.factory() as other:
            await other.execute(
                update(OpenCodeExecutionEnvironment)
                .where(OpenCodeExecutionEnvironment.id == 1)
                .values(status_detail={"new": "configuration"})
            )
            await other.commit()
        await remote_provisioning.guard_environment_write(session, environment)
        assert environment.status_detail == {"new": "configuration"}
        await session.rollback()


@pytest.mark.parametrize("change", ["configuration", "epoch"])
async def test_late_observation_cannot_overwrite_new_facts(mysql_case, monkeypatch, change):
    if change == "epoch":
        async with mysql_case.factory() as session:
            await session.execute(
                update(OpenCodeExecutionEnvironment)
                .where(OpenCodeExecutionEnvironment.id == 1)
                .values(mode="LINUX_REMOTE")
            )
            session.add(
                WorkerInstance(id="w1", environment_id=1, token_digest="test", status="online", epoch=1)
            )
            await session.commit()

    async def changed_during_probe(*args, **kwargs):
        async with mysql_case.factory() as session:
            if change == "configuration":
                await session.execute(
                    update(OpenCodeExecutionEnvironment)
                    .where(OpenCodeExecutionEnvironment.id == 1)
                    .values(status_detail={"new": "configuration"})
                )
            else:
                await session.execute(update(WorkerInstance).where(WorkerInstance.id == "w1").values(epoch=2))
            await session.commit()
        return {"error": "old observation"}

    monkeypatch.setattr(worker_health, "probe_equipment", changed_during_probe)
    result = await maintenance.observe_environment(1, persist=True)
    assert result["stale"] is True
    async with mysql_case.factory() as session:
        environment = await session.get(OpenCodeExecutionEnvironment, 1)
        assert environment.status == "AVAILABLE"
        assert environment.last_checked_at is None


async def test_assignment_does_not_lock_unrelated_task_or_worker(mysql_case, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = remote_provisioning.guard_environment_write

    async def hold_first(session, environment):
        if environment.id == 1:
            entered.set()
            await release.wait()
        return await original(session, environment)

    monkeypatch.setattr(remote_provisioning, "guard_environment_write", hold_first)

    async def claim():
        async with mysql_case.factory() as session:
            task = await Dispatcher(session).claim_once()
            return (task.id, task.employee_id, task.base_commit) if task else None

    first = asyncio.create_task(claim())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with mysql_case.factory() as session:
            await session.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
            await session.execute(
                update(AgentWorker).where(AgentWorker.id == "E2").values(last_heartbeat=datetime.now(UTC))
            )
            await session.commit()
        assert await asyncio.wait_for(claim(), 3) == ("T2", "E2", None)
    finally:
        release.set()
        outcome = await asyncio.wait_for(first, 5)
    assert outcome == ("T1", "E1", None)


async def test_two_claimers_cannot_occupy_one_worker(mysql_case):
    async with mysql_case.factory() as session:
        await session.execute(update(AgentWorker).where(AgentWorker.id == "E2").values(enabled=False))
        await session.commit()

    async def claim():
        async with mysql_case.factory() as session:
            task = await Dispatcher(session).claim_once()
            return task.id if task else None

    await asyncio.wait_for(asyncio.gather(claim(), claim()), 5)
    async with mysql_case.factory() as session:
        assigned = (await session.scalars(select(Task.id).where(Task.claimed_at.is_not(None)))).all()
        assert len(assigned) == 1


async def test_maintained_worker_does_not_block_another_available_worker(mysql_case):
    async with mysql_case.factory() as session:
        session.add(
            WorkerMaintenanceOperation(
                id="op", environment_id=1, action="check", state="QUEUED", stage="queued",
                attempt=1, active_environment_id=1,
            )
        )
        await session.commit()
    async with mysql_case.factory() as session:
        task = await Dispatcher(session).claim_once()
        assert task is not None
        assert task.employee_id == "E2"


@pytest.mark.parametrize("control_status", ["PAUSED", "ERROR"])
async def test_heartbeat_preserves_concurrent_control_status(mysql_case, monkeypatch, control_status):
    async with mysql_case.factory() as session:
        await session.execute(update(AgentWorker).where(AgentWorker.id == "E1").values(status="OFFLINE"))
        await session.execute(update(Task).where(Task.id == "T1").values(status="RUNNING", employee_id="E1"))
        await session.commit()

    async with mysql_case.factory() as session:
        cached = await session.get(AgentWorker, "E1")
        await session.commit()
        async with mysql_case.factory() as other:
            await other.execute(
                update(AgentWorker).where(AgentWorker.id == "E1").values(status=control_status)
            )
            await other.commit()
        assert cached.status == "OFFLINE"

        @asynccontextmanager
        async def connect(*args, **kwargs):
            raise AssertionError("occupied Worker must not be probed")
            yield

        monkeypatch.setattr("app.runtime.lifecycle.connect", connect)
        await RuntimeService(session).refresh_employee_heartbeats()
    async with mysql_case.factory() as session:
        employee = await session.get(AgentWorker, "E1")
        assert employee.status == control_status
        assert employee.current_task_count == 1
        assert employee.last_heartbeat is None


async def test_finish_and_observe_share_environment_first_order(mysql_case, monkeypatch):
    async with mysql_case.factory() as session:
        session.add(
            WorkerMaintenanceOperation(
                id="op",
                environment_id=1,
                action="check",
                state="RUNNING",
                stage="running",
                attempt=1,
                active_environment_id=1,
                lease_owner="owner",
                lease_until=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        await session.commit()
    locked, release, observer_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class FinishingSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if (
                isinstance(statement, Update)
                and statement.table.name == "t_nexus_worker_maintenance_operations"
            ):
                locked.set()
                await release.wait()
            return result

    async def finish():
        async with FinishingSession(mysql_case.engine, expire_on_commit=False) as session:
            done = await maintenance._finish_in_session(
                session,
                "op",
                "owner",
                1,
                "SUCCEEDED",
                stage="finished",
                error_code=None,
                error_message=None,
                after_snapshot={},
                release_active=True,
                environment_id=1,
                environment_values={"last_checked_at": datetime.now(UTC)},
            )
            await session.commit()
            return done

    class ObservingSession(AsyncSession):
        async def scalar(self, statement, *args, **kwargs):
            if statement.get_execution_options().get("populate_existing"):
                observer_started.set()
            return await super().scalar(statement, *args, **kwargs)

    finishing = asyncio.create_task(finish())
    await asyncio.wait_for(locked.wait(), 5)
    monkeypatch.setattr(
        maintenance, "SessionLocal",
        async_sessionmaker(mysql_case.engine, class_=ObservingSession, expire_on_commit=False),
    )
    observing = asyncio.create_task(maintenance.observe_environment(1, persist=True))
    try:
        await asyncio.wait_for(observer_started.wait(), 5)
    finally:
        release.set()
    result, observation = await asyncio.wait_for(asyncio.gather(finishing, observing), 5)
    assert result is True
    assert observation["stale"] is False


async def test_finish_event_does_not_reverse_employee_environment_order(mysql_case):
    async with mysql_case.factory() as session:
        session.add(
            WorkerMaintenanceOperation(
                id="op", environment_id=1, action="check", state="RUNNING", stage="running",
                attempt=1, active_environment_id=1, lease_owner="owner",
                lease_until=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        await session.commit()
    locked, release = asyncio.Event(), asyncio.Event()

    class FinishingSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if (
                isinstance(statement, Update)
                and statement.table.name == "t_nexus_worker_maintenance_operations"
            ):
                locked.set()
                await release.wait()
            return result

    async def finish():
        async with FinishingSession(mysql_case.engine, expire_on_commit=False) as session:
            done = await maintenance._finish_in_session(
                session, "op", "owner", 1, "SUCCEEDED", stage="finished",
                error_code=None, error_message=None, after_snapshot={}, release_active=True,
                environment_id=1, environment_values={"last_checked_at": datetime.now(UTC)},
                event_message="维护完成",
            )
            await session.commit()
            return done

    finishing = asyncio.create_task(finish())
    try:
        await asyncio.wait_for(locked.wait(), 5)
        async with mysql_case.factory() as session:
            # A task start must not take Employee while finish holds Environment:
            # inserting its Event later also takes an Employee foreign-key lock.
            available = await session.scalar(
                select(AgentWorker.id).where(AgentWorker.id == "E1").with_for_update(skip_locked=True)
            )
            assert available is None
    finally:
        release.set()
        done = await asyncio.wait_for(finishing, 5)
    assert done is True

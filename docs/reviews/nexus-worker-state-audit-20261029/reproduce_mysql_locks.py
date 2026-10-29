"""在专用空 MySQL 测试库运行；先通过 Alembic 建表。

ASTRA_DATABASE_URL 必须指向名字以 nexus_state_audit 开头的专用库。
调用实际 Dispatcher、guard_environment_write、_finish_in_session；只替换远端 I/O
为可控等待，并在两次 SQL 之间设置屏障来复现合法并发交错。
"""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "services/nexus-api"))

from app.database import SessionLocal, database_url, engine
from app.models import (
    AgentWorker,
    CodeRepository,
    OpenCodeExecutionEnvironment,
    Task,
    TaskCategory,
    WorkerMaintenanceOperation,
)
from app.runtime import dispatcher, maintenance, worker_health
from app.runtime.maintenance import _finish_in_session
from app.runtime.remote_provisioning import guard_environment_write
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update


async def seed():
    async with SessionLocal() as session:
        session.add(
            CodeRepository(id="audit-repo", url="https://example.invalid/audit")
        )
        session.add(TaskCategory(id="audit-category", name="audit-category"))
        for i in (1, 2):
            session.add(
                AgentWorker(
                    id=f"audit-E{i}",
                    name=f"audit-E{i}",
                    role="build",
                    status="IDLE",
                    task_types=["ISSUE"],
                    category_ids=["audit-category"],
                )
            )
        await session.flush()
        for i in (1, 2):
            session.add(
                OpenCodeExecutionEnvironment(
                    employee_id=f"audit-E{i}",
                    repository_id="audit-repo",
                    mode="LOCAL",
                    workspace_root="/tmp/audit",
                    status="AVAILABLE",
                )
            )
            session.add(
                Task(
                    id=f"audit-T{i}",
                    title=f"audit-T{i}",
                    task_type="ISSUE",
                    category_id="audit-category",
                    repository_id="audit-repo",
                    status="PENDING",
                )
            )
        await session.commit()


async def dispatcher_locks():
    entered, resume = asyncio.Event(), asyncio.Event()
    original_uses, original_head = (
        dispatcher.environment_uses_worker,
        dispatcher.workspace_head_commit,
    )

    async def uses_worker(environment):
        return True

    async def slow_head(environment, workspace):
        entered.set()
        await resume.wait()
        return "a" * 40

    dispatcher.environment_uses_worker, dispatcher.workspace_head_commit = (
        uses_worker,
        slow_head,
    )

    async def claim():
        async with SessionLocal() as session:
            task = await dispatcher.Dispatcher(session).claim_once()
            return task.id if task else None

    job = asyncio.create_task(claim())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        observations = {}
        async with SessionLocal() as other:
            unlocked = await other.scalar(
                select(Task.id)
                .where(Task.id == "audit-T2")
                .with_for_update(skip_locked=True)
            )
            observations["unrelated_pending_task_skipped"] = unlocked is None
            await other.rollback()
            await other.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
            try:
                await other.execute(
                    update(AgentWorker)
                    .where(AgentWorker.id == "audit-E2")
                    .values(last_heartbeat=datetime.now(UTC))
                )
                observations["other_worker_heartbeat"] = "unexpectedly writable"
            except OperationalError as exc:
                observations["other_worker_heartbeat"] = str(exc.orig.args[0])
            finally:
                await other.rollback()
        assert observations == {
            "unrelated_pending_task_skipped": True,
            "other_worker_heartbeat": "1205",
        }, observations
        print(json.dumps({"dispatcher": observations}))
    finally:
        resume.set()
        await asyncio.wait_for(job, 10)
        dispatcher.environment_uses_worker, dispatcher.workspace_head_commit = (
            original_uses,
            original_head,
        )


async def json_guard_mismatch():
    async with SessionLocal() as session:
        environment = await session.scalar(
            select(OpenCodeExecutionEnvironment).where(
                OpenCodeExecutionEnvironment.employee_id == "audit-E2"
            )
        )
        equality = (
            await session.execute(
                text(
                    "SELECT status_detail = '{}', status_detail = CAST('{}' AS JSON) "
                    "FROM t_nexus_opencode_execution_environments WHERE id = :id"
                ),
                {"id": environment.id},
            )
        ).one()
        assert tuple(equality) == (0, 1)
        noop = await session.execute(
            update(OpenCodeExecutionEnvironment)
            .where(OpenCodeExecutionEnvironment.id == environment.id)
            .values(updated_at=OpenCodeExecutionEnvironment.updated_at)
        )
        assert noop.rowcount == 1
        await session.commit()
        try:
            await guard_environment_write(session, environment)
        except HTTPException as exc:
            assert exc.status_code == 409
            print(
                json.dumps(
                    {
                        "json_guard": {
                            "plain_string_equal": equality[0],
                            "cast_json_equal": equality[1],
                            "time_noop_matched_rows": noop.rowcount,
                            "http_status": exc.status_code,
                        }
                    },
                    ensure_ascii=False,
                )
            )
        else:
            raise AssertionError("预期复现 JSON 比较导致的环境锁误拒绝")


async def finish_observe_deadlock():
    async with SessionLocal() as session:
        environment = await session.scalar(
            select(OpenCodeExecutionEnvironment).where(
                OpenCodeExecutionEnvironment.employee_id == "audit-E1"
            )
        )
        environment_id = environment.id
        session.add(
            WorkerMaintenanceOperation(
                id="audit-op",
                environment_id=environment_id,
                action="check",
                state="RUNNING",
                stage="running",
                lease_owner="audit-owner",
                attempt=1,
                active_environment_id=environment_id,
                lease_until=datetime.now(UTC) + timedelta(minutes=2),
            )
        )
        await session.commit()
    operation_locked, environment_locked = asyncio.Event(), asyncio.Event()

    class GateSession(AsyncSession):
        async def scalar(self, statement, *args, **kwargs):
            result = await super().scalar(statement, *args, **kwargs)
            if (
                self.info["role"] == "observe"
                and getattr(statement, "_for_update_arg", None) is not None
            ):
                descriptions = getattr(statement, "column_descriptions", [])
                if (
                    descriptions
                    and descriptions[0].get("entity") is OpenCodeExecutionEnvironment
                ):
                    environment_locked.set()
            return result

        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if isinstance(statement, Update):
                table = statement.table.name
                if (
                    self.info["role"] == "finish"
                    and table == "t_nexus_worker_maintenance_operations"
                ):
                    operation_locked.set()
                    await asyncio.wait_for(environment_locked.wait(), 5)
            if (
                self.info["role"] == "observe"
                and getattr(statement, "_for_update_arg", None) is not None
            ):
                descriptions = getattr(statement, "column_descriptions", [])
                if (
                    descriptions
                    and descriptions[0].get("entity") is OpenCodeExecutionEnvironment
                ):
                    environment_locked.set()
            return result

    async def finish():
        async with GateSession(
            engine, expire_on_commit=False, info={"role": "finish"}
        ) as session:
            await _finish_in_session(
                session,
                "audit-op",
                "audit-owner",
                1,
                "SUCCEEDED",
                stage="finished",
                error_code=None,
                error_message=None,
                after_snapshot={},
                release_active=True,
                environment_id=environment_id,
                environment_values={"last_checked_at": datetime.now(UTC)},
            )
            await session.commit()
            return "finished"

    async def observe():
        await asyncio.wait_for(operation_locked.wait(), 5)
        return await maintenance.observe_environment(environment_id, persist=True)

    async def probe(*args, **kwargs):
        return {"error": "controlled offline probe"}

    original_factory, original_probe = (
        maintenance.SessionLocal,
        worker_health.probe_equipment,
    )
    maintenance.SessionLocal = lambda: GateSession(
        engine, expire_on_commit=False, info={"role": "observe"}
    )
    worker_health.probe_equipment = probe
    try:
        results = await asyncio.wait_for(
            asyncio.gather(finish(), observe(), return_exceptions=True), 10
        )
    finally:
        maintenance.SessionLocal, worker_health.probe_equipment = (
            original_factory,
            original_probe,
        )
    codes = [str(item.orig.args[0]) for item in results if hasattr(item, "orig")]
    assert "1213" in codes, [repr(item) for item in results]
    print(json.dumps({"op_environment_lock_order": {"mysql_error_codes": codes}}))


async def main():
    url = make_url(database_url)
    assert url.get_backend_name() == "mysql" and (url.database or "").startswith(
        "nexus_state_audit"
    )
    try:
        async with engine.connect() as connection:
            version, isolation = (
                await connection.execute(
                    text("SELECT VERSION(), @@transaction_isolation")
                )
            ).one()
            print(json.dumps({"mysql": version, "isolation": isolation}))
        await seed()
        await dispatcher_locks()
        await json_guard_mismatch()
        await finish_observe_deadlock()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

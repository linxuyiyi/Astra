"""Worker occupancy stays until success or a matching human release."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import EmployeeStatus, EnvironmentStatus, TaskStatus
from app.models import Base, DigitalEmployee, OpenCodeExecutionEnvironment, RuntimeSession, Task
from app.runtime.dispatcher import Dispatcher
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.runtime.maintenance import submit_operation
from app.runtime.occupancy import task_occupies


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def _employee(status=EmployeeStatus.IDLE, count=0):
    return DigitalEmployee(
        id="E1",
        name="Engineer",
        role="build",
        status=status,
        enabled=True,
        auto_claim=True,
        auto_execute=False,
        task_types=["ISSUE"],
        category_ids=["testing"],
        current_task_count=count,
    )


def _environment():
    return OpenCodeExecutionEnvironment(
        employee_id="E1",
        mode="LOCAL",
        name="local",
        workspace_root="/tmp/workspaces",
        repository_id="repo-1",
        status=EnvironmentStatus.AVAILABLE,
    )


def _task(**overrides):
    values = dict(
        id="T1",
        title="occupied",
        task_type="ISSUE",
        category_id="testing",
        status=TaskStatus.CANCELED,
        employee_id="E1",
        claimed_at=datetime.now(UTC),
        current_session_id="ses_1",
        pending_attempt=1,
        repository_id="repo-1",
    )
    values.update(overrides)
    return Task(**values)


def test_unclaimed_pending_does_not_occupy():
    task = _task(
        status=TaskStatus.PENDING,
        claimed_at=None,
        current_session_id=None,
        pending_message_id=None,
        employee_id="E1",
    )
    assert task_occupies(task) is False


def test_failed_and_unconfirmed_cancel_occupy():
    assert task_occupies(_task(status=TaskStatus.FAILED)) is True
    assert task_occupies(_task(status=TaskStatus.CANCELED)) is True
    running = _task(status=TaskStatus.RUNNING, execution_stop_confirmed_at=datetime.now(UTC))
    running.execution_stop_session_id = "ses_1"
    running.execution_stop_attempt = 1
    assert task_occupies(running) is True


@pytest.mark.asyncio
async def test_unclaimed_pending_does_not_block_dispatch(session):
    session.add(_employee())
    session.add(_environment())
    session.add(
        _task(
            status=TaskStatus.PENDING,
            claimed_at=None,
            current_session_id=None,
            pending_message_id=None,
            employee_id="E1",
        )
    )
    await session.commit()

    claimed = await Dispatcher(session).claim_once()

    assert claimed is not None
    assert claimed.id == "T1"
    employee = await session.get(DigitalEmployee, "E1")
    assert employee.current_task_count == 1


@pytest.mark.asyncio
async def test_stale_count_does_not_dispatch_over_unconfirmed_cancel(session):
    session.add(_employee(count=0))
    session.add(_environment())
    session.add(_task())
    session.add(
        Task(
            id="T2",
            title="next",
            task_type="ISSUE",
            category_id="testing",
            status=TaskStatus.PENDING,
        )
    )
    await session.commit()

    assert await Dispatcher(session).claim_once() is None
    employee = await session.get(DigitalEmployee, "E1")
    assert employee.current_task_count == 0


@pytest.mark.asyncio
async def test_human_release_then_dispatch(session):
    session.add(_employee(status=EmployeeStatus.WORKING, count=1))
    session.add(_environment())
    session.add(_task())
    session.add(
        Task(
            id="T2",
            title="next",
            task_type="ISSUE",
            category_id="testing",
            status=TaskStatus.PENDING,
            repository_id="repo-1",
        )
    )
    await session.commit()

    released = await RuntimeService(session).confirm_execution_stop(
        "T1", operator="ada", reason="进程已退出"
    )
    assert released.status == TaskStatus.CANCELED
    assert released.execution_stop_operator == "ada"
    assert released.execution_stop_session_id == "ses_1"
    assert released.execution_stop_attempt == 1
    employee = await session.get(DigitalEmployee, "E1")
    assert employee.current_task_count == 0
    assert employee.status == EmployeeStatus.IDLE

    claimed = await Dispatcher(session).claim_once()
    assert claimed is not None
    assert claimed.id == "T2"


@pytest.mark.asyncio
async def test_release_rejects_running_and_mismatched_session(session):
    session.add(_employee(status=EmployeeStatus.WORKING, count=1))
    session.add(_task(status=TaskStatus.RUNNING))
    await session.commit()
    with pytest.raises(RuntimeStateError, match="只能确认"):
        await RuntimeService(session).confirm_execution_stop("T1", operator="ada", reason="不行")

    task = await session.get(Task, "T1")
    task.status = TaskStatus.CANCELED
    task.execution_stop_confirmed_at = datetime.now(UTC)
    task.execution_stop_session_id = "ses_other"
    task.execution_stop_attempt = 1
    task.execution_stop_operator = "ada"
    task.execution_stop_reason = "旧会话"
    await session.commit()
    await RuntimeService(session).project(task, TaskStatus.CANCELED, detail="仍占用")
    employee = await session.get(DigitalEmployee, "E1")
    assert employee.current_task_count == 1


@pytest.mark.asyncio
async def test_heartbeat_does_not_clear_occupied_worker(session, monkeypatch):
    session.add(_employee(count=0))
    session.add(_environment())
    session.add(_task(status=TaskStatus.FAILED))
    await session.commit()
    probes = []

    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        probes.append(True)

        class Client:
            async def health(self):
                return {"version": "1.18.35"}

        yield Client()

    monkeypatch.setattr("app.runtime.lifecycle.connect", connect)
    healthy = await RuntimeService(session).refresh_employee_heartbeats()
    employee = await session.get(DigitalEmployee, "E1")
    assert probes
    assert healthy == ["E1"]
    assert employee.current_task_count == 1
    assert employee.status == EmployeeStatus.WORKING


@pytest.mark.asyncio
async def test_maintenance_blocks_until_release_matches_unknown_session(session, monkeypatch):
    settings = SimpleNamespace(
        remote_deploy_timeout_seconds=30,
        mcp_transport="streamable-http",
        worker_maintenance_lease_seconds=60,
        remote_opencode_version="1.18.35",
    )
    monkeypatch.setattr("app.runtime.remote_provisioning.get_settings", lambda: settings)
    monkeypatch.setattr("app.runtime.maintenance.get_settings", lambda: settings)
    employee = DigitalEmployee(
        id="E1",
        name="Remote",
        role="build",
        status=EmployeeStatus.WORKING,
        current_task_count=1,
    )
    environment = OpenCodeExecutionEnvironment(
        employee_id="E1",
        mode="LINUX_REMOTE",
        name="linux",
        host="10.0.0.8",
        port=22,
        username="root",
        ssh_password="secret",
        workspace_root="/srv/project",
        status="UNKNOWN",
        status_detail={},
    )
    task = _task()
    session.add(employee)
    session.add(environment)
    await session.flush()
    session.add(task)
    session.add(
        RuntimeSession(
            id="ses_1",
            task_id="T1",
            employee_id="E1",
            status="UNKNOWN",
            workspace="/srv/project",
            environment={},
        )
    )
    await session.commit()
    environment_id = environment.id

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as blocked:
        await submit_operation(
            session, environment_id, "deploy", requested_by="test"
        )
    assert blocked.value.status_code == 409

    await RuntimeService(session).confirm_execution_stop("T1", operator="ada", reason="已停止")
    operation = await submit_operation(
        session, environment_id, "deploy", requested_by="test"
    )
    assert operation.state == "QUEUED"
    await session.refresh(environment)
    assert environment.status == "UNKNOWN"

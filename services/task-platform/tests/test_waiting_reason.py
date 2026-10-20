"""waiting_reason 只跟着 Session 投影写。人工继续不新建 Session。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import EmployeeStatus, EnvironmentStatus, TaskStatus
from app.models import AgentWorker, Base, OpenCodeExecutionEnvironment, RuntimeSession, Task
from app.runtime.dispatcher import Dispatcher
from app.runtime.lifecycle import RuntimeService
from app.runtime.session_state import SessionState, derive_session_state


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def _task(**overrides):
    values = dict(
        id="T1",
        title="wait",
        task_type="ISSUE",
        category_id="testing",
        status=TaskStatus.RUNNING,
        employee_id="E1",
        repository_id="repo-1",
        claimed_at=datetime.now(UTC),
        current_session_id="ses",
        workspace="/tmp/workspaces/E1",
        current_progress="执行中",
    )
    values.update(overrides)
    return Task(**values)


async def _ready(session):
    session.add(
        AgentWorker(
            id="E1",
            name="Engineer",
            role="build",
            status=EmployeeStatus.WORKING,
            enabled=True,
            auto_claim=True,
            auto_execute=False,
            task_types=["ISSUE"],
            category_ids=["testing"],
            current_task_count=1,
        )
    )
    session.add(
        OpenCodeExecutionEnvironment(
            employee_id="E1",
            mode="LOCAL",
            name="local",
            workspace_root="/tmp/workspaces/E1",
            repository_id="repo-1",
            status=EnvironmentStatus.AVAILABLE,
        )
    )
    task = _task()
    session.add(task)
    session.add(
        RuntimeSession(
            id="ses",
            task_id="T1",
            employee_id="E1",
            workspace="/tmp/workspaces/E1",
            environment={},
            status="RUNNING",
        )
    )
    await session.commit()
    return task


@pytest.mark.asyncio
async def test_projection_writes_waiting_reason_without_closing_or_retrying(session):
    task = await _ready(session)
    service = RuntimeService(session)

    await service.apply_session_state(
        task,
        SessionState(status="WAITING", hitl="question"),
    )
    assert task.status == TaskStatus.WAITING
    assert task.waiting_reason == "HITL_QUESTION"

    await service.apply_session_state(
        task,
        SessionState(status="WAITING", hitl="permission"),
    )
    assert task.waiting_reason == "HITL_PERMISSION"
    assert task.status == TaskStatus.WAITING

    await service.apply_session_state(
        task,
        SessionState(status="SUCCEEDED", terminal_reason="completed", finish_reason="stop"),
    )
    assert task.status == TaskStatus.WAITING
    assert task.waiting_reason == "EXECUTION_RESULT_REVIEW"
    assert task.status != TaskStatus.FAILED

    await service.apply_session_state(
        task,
        SessionState(status="FAILED", terminal_reason="assistant_error"),
    )
    assert task.status == TaskStatus.WAITING
    assert task.waiting_reason == "RETRY_EXHAUSTED"

    await service.apply_session_state(
        task,
        SessionState(status="INTERRUPTED", terminal_reason="aborted"),
    )
    assert task.waiting_reason == "MANUAL_INTERVENTION"
    assert task.status == TaskStatus.WAITING

    task.status = TaskStatus.RUNNING
    await session.commit()
    await service.apply_session_state(
        task,
        SessionState(status="INTERRUPTED", terminal_reason="timeout"),
    )
    assert task.status == TaskStatus.WAITING
    assert task.waiting_reason == "TIMEOUT"

    task.status = TaskStatus.RUNNING
    task.waiting_reason = None
    await session.commit()
    await service.apply_session_state(
        task,
        SessionState(status="INTERRUPTED", terminal_reason="unconfirmed"),
    )
    assert task.status == TaskStatus.RUNNING
    assert task.waiting_reason == "TRANSPORT_RECOVERY"

    await service.apply_session_state(task, SessionState(status="UNKNOWN"))
    assert task.status == TaskStatus.RUNNING
    assert task.waiting_reason == "TRANSPORT_RECOVERY"

    await service.apply_session_state(task, SessionState(status="RUNNING"))
    assert task.status == TaskStatus.RUNNING
    assert task.waiting_reason is None


class _Idle:
    async def status(self, *_args):
        return "idle"

    async def children(self, *_args):
        return []

    async def questions(self, *_args):
        return []

    async def permissions(self, *_args):
        return []


@pytest.mark.asyncio
async def test_recovered_transport_recomputes_waiting_reason(session):
    task = await _ready(session)
    task.status = TaskStatus.WAITING
    task.waiting_reason = "TRANSPORT_RECOVERY"
    task.human_required_reason = "执行结束，待确认"
    task.current_progress = "执行结束，待确认"
    binding = await session.get(RuntimeSession, "ses")
    binding.status = "SUCCEEDED"
    binding.terminal_reason = "completed"
    await session.commit()

    refreshed = await RuntimeService(session).refresh(task.id, client=_Idle())
    assert refreshed.status == TaskStatus.WAITING
    assert refreshed.waiting_reason == "EXECUTION_RESULT_REVIEW"

    refreshed.waiting_reason = "TRANSPORT_RECOVERY"
    binding.status = "FAILED"
    binding.terminal_reason = "assistant_error"
    await session.commit()
    refreshed = await RuntimeService(session).refresh(task.id, client=_Idle())
    assert refreshed.status == TaskStatus.WAITING
    assert refreshed.waiting_reason == "RETRY_EXHAUSTED"


@pytest.mark.asyncio
async def test_transport_recovery_projects_execution_review_and_keeps_finished_at(session):
    completed = datetime(2026, 10, 18, 1, 0, tzinfo=UTC)
    task = await _ready(session)
    task.status = TaskStatus.WAITING
    task.waiting_reason = "TRANSPORT_RECOVERY"
    task.human_required_reason = "执行结束，待确认"
    task.current_progress = "执行结束，待确认"
    binding = await session.get(RuntimeSession, "ses")
    binding.status = "SUCCEEDED"
    binding.terminal_reason = "completed"
    binding.finished_at = completed
    await session.commit()

    service = RuntimeService(session)
    refreshed = await service.refresh(task.id, client=_Idle())
    assert refreshed.status == TaskStatus.WAITING
    assert refreshed.waiting_reason == "EXECUTION_RESULT_REVIEW"
    await service.refresh(task.id, client=_Idle())
    stored = await session.get(RuntimeSession, "ses")
    assert stored.finished_at == completed
    reloaded = await session.get(Task, "T1")
    assert reloaded.waiting_reason == "EXECUTION_RESULT_REVIEW"
    assert reloaded.status == TaskStatus.WAITING


@pytest.mark.asyncio
async def test_recovered_refresh_keeps_finished_at(session):
    completed = datetime(2026, 10, 18, 1, 0, tzinfo=UTC)
    task = await _ready(session)
    task.status = TaskStatus.WAITING
    task.waiting_reason = "TRANSPORT_RECOVERY"
    task.human_required_reason = "执行结束，待确认"
    task.current_progress = "执行结束，待确认"
    binding = await session.get(RuntimeSession, "ses")
    binding.status = "SUCCEEDED"
    binding.terminal_reason = "completed"
    binding.finished_at = completed
    await session.commit()

    service = RuntimeService(session)
    await service.refresh(task.id, client=_Idle())
    await service.refresh(task.id, client=_Idle())
    stored = await session.get(RuntimeSession, "ses")
    assert stored.finished_at == completed

    stored.status = "FAILED"
    stored.terminal_reason = "assistant_error"
    stored.finished_at = completed
    task.waiting_reason = "TRANSPORT_RECOVERY"
    await session.commit()
    await service.refresh(task.id, client=_Idle())
    await service.refresh(task.id, client=_Idle())
    assert stored.finished_at == completed


@pytest.mark.asyncio
async def test_missing_transport_and_completed_set_waiting_reason(session):
    task = await _ready(session)
    service = RuntimeService(session)
    await service.apply_session_state(
        task,
        SessionState(status="FAILED", terminal_reason="session_missing"),
    )
    assert task.status == TaskStatus.WAITING
    assert task.waiting_reason == "SESSION_MISSING"
    task.status = TaskStatus.RUNNING
    await session.commit()
    await service.apply_session_state(
        task,
        SessionState(status="INTERRUPTED", terminal_reason="transport_unknown"),
    )
    assert task.status == TaskStatus.WAITING
    assert task.waiting_reason == "TRANSPORT_RECOVERY"
    task.status = TaskStatus.RUNNING
    await session.commit()
    await service.apply_session_state(
        task,
        SessionState(status="FAILED", terminal_reason="completed"),
    )
    assert task.status == TaskStatus.WAITING
    assert task.waiting_reason == "EXECUTION_RESULT_REVIEW"
    assert task.status != TaskStatus.FAILED


def test_retry_stays_running_and_timeout_does_not_choose_retry_exhausted():
    running = derive_session_state(
        messages=[],
        server_status="retry",
        has_pending_question=False,
        has_pending_permission=False,
        pending_ack=False,
    )
    assert running.status == "RUNNING"
    permission = derive_session_state(
        messages=[],
        server_status="idle",
        has_pending_question=False,
        has_pending_permission=True,
        pending_ack=False,
    )
    assert permission.hitl == "permission"


@pytest.mark.asyncio
async def test_continue_reuses_the_worker_worktree_and_session(session):
    task = await _ready(session)
    task.status = TaskStatus.WAITING
    session.add(
        AgentWorker(
            id="E2",
            name="Other",
            role="build",
            status=EmployeeStatus.IDLE,
            enabled=True,
            auto_claim=True,
            auto_execute=False,
            task_types=["ISSUE"],
            category_ids=["testing"],
        )
    )
    session.add(
        OpenCodeExecutionEnvironment(
            employee_id="E2",
            mode="LOCAL",
            name="other",
            workspace_root="/tmp/workspaces/E2",
            repository_id="repo-1",
            status=EnvironmentStatus.AVAILABLE,
        )
    )
    session.add(
        Task(
            id="T2",
            title="other",
            task_type="ISSUE",
            category_id="testing",
            status=TaskStatus.PENDING,
            repository_id="repo-1",
        )
    )
    await session.commit()

    continued = await RuntimeService(session).continue_task("T1")
    assert continued.current_progress == "继续当前 Session"
    assert continued.current_session_id == "ses"
    assert continued.workspace == "/tmp/workspaces/E1"
    assert continued.employee_id == "E1"
    assert await session.get(RuntimeSession, "ses") is not None

    claimed = await Dispatcher(session).claim_once()
    assert claimed is not None
    assert claimed.id == "T2"
    stored = await session.get(Task, "T1")
    assert stored.employee_id == "E1"
    assert stored.current_session_id == "ses"

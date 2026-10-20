"""未生效的能力绑定不参与分配，发现失败不改正在运行的任务。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import EmployeeStatus, EnvironmentStatus, TaskStatus
from app.models import AgentWorker, Base, OpenCodeExecutionEnvironment, Task
from app.runtime.capability import (
    binding_revision,
    discover_worker,
    dispatch_allowed,
    observe_bindings,
    write_capability,
)
from app.runtime.dispatcher import Dispatcher
from app.runtime.environment import environment_snapshot


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def test_worker_discovery_uses_skill_md_and_mcp_names(tmp_path):
    root = tmp_path / "skills"
    (root / "review").mkdir(parents=True)
    (root / "review" / "SKILL.md").write_text("# review", encoding="utf-8")
    (tmp_path / ".opencode" / "skills" / "hidden").mkdir(parents=True)
    (tmp_path / ".opencode" / "skills" / "hidden" / "SKILL.md").write_text("no", encoding="utf-8")
    config = '{"mcp": {"nexus-library": {"url": "http://127.0.0.1/mcp"}}}\n'
    assert discover_worker([root], config) == {"review", "nexus-library"}
    assert "hidden" not in discover_worker([root], config)


def test_failed_discovery_keeps_the_previous_applied_revision():
    desired = binding_revision(["review"])
    record = observe_bindings(
        {"applied_revision": "old", "runtime_status": "applied"},
        desired=desired,
        found=set(),
        expected=["review"],
        error="",
        checked_at="2026-10-17T00:00:00Z",
    )
    assert record["runtime_status"] == "error"
    assert "review" in record["error"]
    assert record["applied_revision"] == "old"
    assert record["desired_revision"] == desired
    detail = write_capability({"operation": {"id": "keep"}}, record)
    assert detail["operation"]["id"] == "keep"


def _employee():
    return AgentWorker(
        id="E1",
        name="Engineer",
        role="build",
        status=EmployeeStatus.IDLE,
        enabled=True,
        auto_claim=True,
        auto_execute=False,
        task_types=["ISSUE"],
        category_ids=["testing"],
    )


def _task(**overrides):
    values = dict(
        id="T1",
        title="bind",
        task_type="ISSUE",
        category_id="testing",
        status=TaskStatus.PENDING,
        employee_id=None,
        claimed_at=None,
        current_session_id=None,
        repository_id="repo-1",
    )
    values.update(overrides)
    return Task(**values)


@pytest.mark.asyncio
async def test_unapplied_binding_is_not_dispatched_and_a_running_task_stays(session):
    desired = binding_revision(["review"])
    blocked = observe_bindings(
        None,
        desired=desired,
        found=set(),
        expected=["review"],
        error="目录不存在",
        checked_at="2026-10-17T00:00:00Z",
    )
    detail = write_capability({}, blocked)
    environment = OpenCodeExecutionEnvironment(
        employee_id="E1",
        mode="LOCAL",
        name="local",
        workspace_root="/tmp/workspaces",
        repository_id="repo-1",
        status=EnvironmentStatus.AVAILABLE,
        status_detail=detail,
    )
    running = Task(
        id="T-run",
        title="running",
        task_type="ISSUE",
        category_id="testing",
        status=TaskStatus.RUNNING,
        employee_id="E1",
        claimed_at=datetime.now(UTC),
        current_session_id="ses",
    )
    session.add(_employee())
    session.add(environment)
    session.add(running)
    session.add(_task())
    await session.commit()

    assert dispatch_allowed(detail) is False
    assert await Dispatcher(session).claim_once() is None
    stored = await session.get(Task, "T-run")
    assert stored is not None
    assert stored.status == TaskStatus.RUNNING
    stored.status = TaskStatus.SUCCEEDED

    ready = observe_bindings(
        blocked,
        desired=desired,
        found={"review"},
        expected=["review"],
        error="",
        checked_at="2026-10-17T00:01:00Z",
    )
    environment.status_detail = write_capability(detail, ready)
    await session.commit()
    claimed = await Dispatcher(session).claim_once()
    assert claimed is not None
    assert claimed.execution_environment_snapshot["capability_revision"] == desired
    assert environment_snapshot(environment)["capability_revision"] == desired

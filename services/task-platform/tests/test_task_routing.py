"""单仓分配记下 base commit。未指定仓库不派单，占用中的 Worker 不被另一条任务领走。"""

import subprocess
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import get_settings
from app.domain import EmployeeStatus, EnvironmentStatus, TaskStatus
from app.main import app
from app.models import Base, DigitalEmployee, OpenCodeExecutionEnvironment, Task
from app.runtime.capability import binding_revision, observe_bindings, write_capability
from app.runtime.dispatcher import Dispatcher


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def _employee():
    return DigitalEmployee(
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


def _environment(workspace: str, repository_id: str = "repo-1"):
    desired = binding_revision(["review"])
    ready = observe_bindings(
        None,
        desired=desired,
        found={"review"},
        expected=["review"],
        error="",
        checked_at="2026-10-17T00:00:00Z",
    )
    return OpenCodeExecutionEnvironment(
        employee_id="E1",
        mode="LOCAL",
        name="local",
        workspace_root=workspace,
        repository_id=repository_id,
        model_name="nexus-gateway/demo",
        status=EnvironmentStatus.AVAILABLE,
        status_detail=write_capability({}, ready),
    )


def _pending(task_id: str, repository_id: str | None):
    return Task(
        id=task_id,
        title=task_id,
        task_type="ISSUE",
        category_id="testing",
        status=TaskStatus.PENDING,
        repository_id=repository_id,
    )


def _commit(workspace) -> str:
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-q", "--allow-empty", "-m", "base"],
        check=True,
    )
    completed = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.mark.asyncio
async def test_dispatch_stays_on_one_repository_and_records_the_base_commit(session, tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    head = _commit(workspace)
    session.add(_employee())
    session.add(_environment(str(workspace)))
    session.add(
        Task(
            id="T-run",
            title="running",
            task_type="ISSUE",
            category_id="testing",
            status=TaskStatus.RUNNING,
            employee_id="E1",
            repository_id="repo-1",
            claimed_at=datetime.now(UTC),
            current_session_id="ses",
        )
    )
    session.add(_pending("T-same", "repo-1"))
    session.add(_pending("T-other", "repo-2"))
    session.add(_pending("T-none", None))
    session.add(_pending("T-next", "repo-1"))
    await session.commit()

    assert await Dispatcher(session).claim_once() is None
    running = await session.get(Task, "T-run")
    assert running.status == TaskStatus.RUNNING

    running.status = TaskStatus.SUCCEEDED
    await session.commit()
    claimed = await Dispatcher(session).claim_once()
    assert claimed is not None
    assert claimed.id == "T-same"
    assert claimed.base_commit == head
    assert claimed.execution_environment_snapshot["base_commit"] == head
    assert claimed.execution_environment_snapshot["capability_revision"] == binding_revision(["review"])
    assert claimed.execution_environment_snapshot["model_name"] == "nexus-gateway/demo"
    assert await Dispatcher(session).claim_once() is None
    waiting = await session.get(Task, "T-next")
    assert waiting.employee_id is None


def test_related_tasks_keep_one_repository_each_and_summarize_the_dependency(monkeypatch):
    monkeypatch.setattr(get_settings(), "gateway_master_key", Fernet.generate_key().decode())
    with TestClient(app) as client:
        first = client.post(
            "/api/repositories",
            json={"url": "https://git.example/org/one.git", "branch": "main", "token": "token-one"},
        )
        second = client.post(
            "/api/repositories",
            json={"url": "https://git.example/org/two.git", "branch": "main", "token": "token-two"},
        )
        assert first.status_code == 201
        assert second.status_code == 201
        created = client.post(
            "/api/tasks/related",
            json={
                "task_type": "ISSUE",
                "category_id": "testing-platform",
                "title": "跨仓",
                "description": "拆成两条任务",
                "acceptance_criteria": [{"content": "各自完成"}],
                "parts": [
                    {"repository_id": first.json()["id"], "title": "仓一"},
                    {"repository_id": second.json()["id"], "title": "仓二", "depends_on": 0},
                ],
            },
        )
        assert created.status_code == 201, created.text
        rows = created.json()
        assert rows[0]["repository_id"] == first.json()["id"]
        assert rows[1]["repository_id"] == second.json()["id"]
        assert rows[1]["related_task_id"] == rows[0]["id"]
        assert "token-one" not in created.text
        summary = client.get(f"/api/tasks/{rows[1]['id']}/related")
        assert summary.status_code == 200
        assert [item["id"] for item in summary.json()] == [rows[0]["id"], rows[1]["id"]]
        assert [item["repository_id"] for item in summary.json()] == [
            first.json()["id"],
            second.json()["id"],
        ]

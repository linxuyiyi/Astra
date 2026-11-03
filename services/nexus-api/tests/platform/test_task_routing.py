"""按任务分类承接，任务不绑定仓库；Worker 使用自己的 Git 配置。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import EmployeeStatus, EnvironmentStatus, TaskStatus
from app.main import app
from app.models import AgentWorker, Base, OpenCodeExecutionEnvironment, Task
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


async def test_dispatch_selects_category_worker_without_task_repository(session):
    for worker_id, category, url in (
        ("automation-worker", "automation", "https://git.example/automation.git"),
        ("system-worker", "testing-system", "https://git.example/testing-system.git"),
    ):
        session.add(
            AgentWorker(
                id=worker_id,
                name=worker_id,
                role="开发",
                status=EmployeeStatus.IDLE,
                auto_claim=True,
                auto_execute=False,
                task_types=["ISSUE"],
                category_ids=[category],
            )
        )
        session.add(
            OpenCodeExecutionEnvironment(
                employee_id=worker_id,
                workspace_root=f"/tmp/{worker_id}",
                repository_url=url,
                branch="main",
                status=EnvironmentStatus.AVAILABLE,
            )
        )
    session.add(
        Task(
            id="category-task",
            title="开发 MCP",
            task_type="ISSUE",
            category_id="testing-system",
            status=TaskStatus.PENDING,
        )
    )
    await session.commit()
    task = await Dispatcher(session).claim_once()
    assert task is not None and task.id == "category-task"
    assert task.employee_id == "system-worker"
    assert not hasattr(task, "repository_id")
    assert task.execution_environment_snapshot["repository_url"] == "https://git.example/testing-system.git"
    assert await Dispatcher(session).claim_once() is None


def test_related_tasks_keep_dependency_without_repository_parameters():
    with TestClient(app) as client:
        category = client.post("/api/task-categories", json={"name": "分类依赖任务"}).json()["id"]
        result = client.post(
            "/api/tasks/related",
            json={
                "task_type": "REQUIREMENT",
                "category_id": category,
                "title": "测试系统需求",
                "description": "分步完成",
                "parts": [{"title": "开发"}, {"title": "验证", "depends_on": 0}],
            },
        )
        assert result.status_code == 201, result.text
        rows = result.json()
        assert rows[1]["related_task_id"] == rows[0]["id"]
        assert all("repository_id" not in row for row in rows)
        related = client.get(f"/api/tasks/{rows[1]['id']}/related").json()
        assert [row["id"] for row in related] == [rows[0]["id"], rows[1]["id"]]

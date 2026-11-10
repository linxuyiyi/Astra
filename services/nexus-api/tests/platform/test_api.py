import asyncio

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.domain import TaskStatus
from app.main import app
from app.models import Task


def task_payload(**overrides):
    payload = {
        "task_type": "ISSUE",
        "category_id": "testing-platform",
        "title": "API 集成任务",
        "description": "验证 V0.4 任务事实、状态和结果契约",
        "acceptance_criteria": [{"content": "任务结果可追溯"}],
        "priority": "HIGH",
    }
    payload.update(overrides)
    return payload


def test_task_api_uses_session_business_contract():
    with TestClient(app) as client:
        created = client.post("/api/tasks", json=task_payload())
        assert created.status_code == 201
        task = created.json()
        assert task["status"] == "PENDING"
        assert task["task_type"] == "ISSUE"
        assert task["category_id"] == "testing-platform"
        assert task["acceptance_criteria"] == [{"id": "AC-01", "content": "任务结果可追溯"}]
        assert task["workspace"] is None
        assert task["current_session_id"] is None
        assert "current_phase" not in task
        assert "progress" not in task

        filtered = client.get(
            "/api/tasks",
            params={"q": "API 集成", "status": "PENDING", "task_type": "ISSUE"},
        )
        assert filtered.status_code == 200
        assert [item["id"] for item in filtered.json()] == [task["id"]]

        stopped = client.post(f"/api/tasks/{task['id']}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "CANCELED"
        resumed = client.post(f"/api/tasks/{task['id']}/resume")
        assert resumed.status_code == 409
        assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "CANCELED"


def test_task_list_filters_by_source_and_origin():
    with TestClient(app) as client:
        private = client.post(
            "/api/tasks",
            json=task_payload(source="private_chat", external_task_id="conv-a", origin_employee_id="emp-a"),
        ).json()
        group = client.post(
            "/api/tasks",
            json=task_payload(source="group_chat", external_task_id="group-a", origin_employee_id="emp-b"),
        ).json()
        result = client.get(
            "/api/tasks",
            params={"source": "private_chat", "external_task_id": "conv-a", "origin_employee_id": "emp-a"},
        ).json()
        assert [row["id"] for row in result] == [private["id"]]
        assert group["id"] != private["id"]


def test_task_patch_updates_open_task_and_refuses_concluded():
    with TestClient(app) as client:
        created = client.post("/api/tasks", json=task_payload(title="可编辑任务")).json()
        updated = client.patch(
            f"/api/tasks/{created['id']}",
            json={
                "title": "已改标题",
                "description": "已改描述",
                "acceptance_criteria": [{"content": "新的验收标准"}],
                "origin_employee_id": "emp-owner",
            },
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["title"] == "已改标题"
        assert body["description"] == "已改描述"
        assert body["acceptance_criteria"][0]["content"] == "新的验收标准"
        assert body["origin_employee_id"] == "emp-owner"

        stopped = client.post(f"/api/tasks/{created['id']}/stop")
        assert stopped.status_code == 200
        refused = client.patch(f"/api/tasks/{created['id']}", json={"title": "不能再改"})
        assert refused.status_code == 409


def test_task_delete_only_allows_pending_or_terminal_states():
    def set_status(task_id: str, status: TaskStatus) -> None:
        async def _update() -> None:
            async with SessionLocal() as session:
                task = await session.get(Task, task_id)
                task.status = status
                await session.commit()

        asyncio.run(_update())

    with TestClient(app) as client:
        pending = client.post("/api/tasks", json=task_payload(title="待执行任务删除")).json()
        running = client.post("/api/tasks", json=task_payload(title="执行中任务删除")).json()
        set_status(running["id"], TaskStatus.RUNNING)

        blocked = client.delete(f"/api/tasks/{running['id']}")
        assert blocked.status_code == 409
        assert client.get(f"/api/tasks/{running['id']}").status_code == 200

        deleted = client.delete(f"/api/tasks/{pending['id']}")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": pending["id"]}
        assert client.get(f"/api/tasks/{pending['id']}").status_code == 404

        missing = client.delete("/api/tasks/T-DOES-NOT-EXIST")
        assert missing.status_code == 404


def test_worker_crud_uses_flat_config_and_releases_historical_references():
    from uuid import uuid4

    worker_id = "crud_" + uuid4().hex[:12]
    payload = {
        "id": worker_id,
        "name": "资源配置验收",
        "host": "worker.invalid",
        "username": "nexus",
        "workspace_root": "/srv/project",
        "category_ids": ["testing-platform"],
    }
    with TestClient(app) as client:
        created = client.post("/api/workers", json=payload)
        assert created.status_code == 201
        assert not {"token_digest", "ssh_password", "git_token_ciphertext"} & set(created.json())
        config = {key: value for key, value in payload.items() if key != "id"}
        config.update(model_name="", repository_url="", branch="", name="改名后的资源")
        updated = client.put(f"/api/workers/{worker_id}", json=config)
        assert (
            updated.status_code == 200 and updated.json()["opencode_port"] == created.json()["opencode_port"]
        )
        task = client.post("/api/tasks", json=task_payload(agent_worker_id=worker_id)).json()

        async def occupy():
            async with SessionLocal() as session:
                row = await session.get(Task, task["id"])
                row.status = "WAITING"
                await session.commit()

        asyncio.run(occupy())
        assert (
            client.put(f"/api/workers/{worker_id}", json={**config, "host": "moved.invalid"}).status_code
            == 409
        )
        assert client.delete(f"/api/workers/{worker_id}").status_code == 409
        assert client.post(f"/api/tasks/{task['id']}/stop").status_code == 200
        assert client.delete(f"/api/workers/{worker_id}").status_code == 200
        assert client.get(f"/api/tasks/{task['id']}").json()["agent_worker_id"] is None
        assert client.get(f"/api/workers/{worker_id}").status_code == 404

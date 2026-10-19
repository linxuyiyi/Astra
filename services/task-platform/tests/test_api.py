import asyncio
import sys

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.domain import TaskStatus
from app.main import app
from app.models import DigitalEmployee, OpenCodeExecutionEnvironment, RuntimeSession, Task


def task_payload(**overrides):
    payload = {
        "task_type": "ISSUE",
        "category_id": "testing-platform",
        "title": "API 集成任务",
        "description": "验证 V0.4 任务事实、状态和结果契约",
        "acceptance_criteria": [{"content": "任务结果可追溯"}],
        "priority": "HIGH",
        "completion_policy": "AUTO",
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


def test_task_list_filters_by_exact_source_session_and_conversation():
    with TestClient(app) as client:
        conversation = client.post(
            "/api/tasks",
            json=task_payload(
                title="私聊来源任务",
                source_session_id="ses-private-a",
                source_conversation_id="conv-private-a",
            ),
        ).json()
        channel = client.post(
            "/api/tasks",
            json=task_payload(
                title="群组来源任务",
                source_session_id="ses-group-a",
                source_channel_id="ch-group-a",
            ),
        ).json()
        other = client.post(
            "/api/tasks",
            json=task_payload(
                title="其它会话任务",
                source_session_id="ses-private-b",
                source_conversation_id="conv-private-b",
            ),
        ).json()

        by_session = client.get("/api/tasks", params={"source_session_id": "ses-private-a"}).json()
        assert [item["id"] for item in by_session] == [conversation["id"]]
        by_conversation = client.get(
            "/api/tasks", params={"source_conversation_id": "conv-private-a"}
        ).json()
        assert [item["id"] for item in by_conversation] == [conversation["id"]]
        by_channel = client.get("/api/tasks", params={"source_channel_id": "ch-group-a"}).json()
        assert [item["id"] for item in by_channel] == [channel["id"]]
        assert other["id"] not in {item["id"] for item in by_session + by_conversation + by_channel}


def test_task_patch_updates_open_task_and_refuses_concluded():
    with TestClient(app) as client:
        created = client.post("/api/tasks", json=task_payload(title="可编辑任务")).json()
        updated = client.patch(
            f"/api/tasks/{created['id']}",
            json={
                "title": "已改标题",
                "description": "已改描述",
                "acceptance_criteria": [{"content": "新的验收标准"}],
                "responsible_employee_id": "emp-owner",
            },
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["title"] == "已改标题"
        assert body["description"] == "已改描述"
        assert body["acceptance_criteria"][0]["content"] == "新的验收标准"
        assert body["responsible_employee_id"] == "emp-owner"

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


def test_task_delete_rejects_terminal_business_state_with_active_runtime():
    async def prepare(task_id: str) -> None:
        async with SessionLocal() as session:
            task = await session.get(Task, task_id)
            session.add(DigitalEmployee(id="E-delete-guard", name="Guard", role="build", status="IDLE"))
            await session.flush()
            task.employee_id = "E-delete-guard"
            task.status = TaskStatus.FAILED
            task.pending_message_id = "msg_pendingdelete01"
            task.current_session_id = "ses_pending_delete"
            session.add(
                RuntimeSession(
                    id="ses_pending_delete",
                    task_id=task.id,
                    employee_id="E-delete-guard",
                    workspace=task.workspace or ".",
                    environment=task.execution_environment_snapshot or {},
                    status="RUNNING",
                )
            )
            await session.commit()

    with TestClient(app) as client:
        task = client.post(
            "/api/tasks",
            json=task_payload(title="终态但执行仍活动"),
        ).json()
        asyncio.run(prepare(task["id"]))

        blocked = client.delete(f"/api/tasks/{task['id']}")
        assert blocked.status_code == 409
        assert "待确认提交" in blocked.json()["detail"]


def test_home_dashboard_exposes_only_aggregated_business_sections():
    with TestClient(app) as client:
        overview = client.get("/api/dashboard/overview")
        assert overview.status_code == 200
        assert set(overview.json()) == {
            "employee_total",
            "team_count",
            "working_employees",
            "running_tasks",
            "waiting_tasks",
            "failed_tasks",
            "online_employees",
            "completed_today",
            "attention_total",
        }

        employees = client.get("/api/dashboard/employees")
        assert employees.status_code == 200
        for team in employees.json()["teams"]:
            for employee in team["employees"]:
                assert set(employee) == {
                    "id",
                    "name",
                    "role",
                    "character_id",
                    "status",
                    "enabled",
                    "state",
                    "state_label",
                    "runtime_state", "runtime_status_label", "runtime_detail", "failure",
                    "current_task",
                }
                assert "execution_environment" not in employee

        attention = client.get("/api/dashboard/attention")
        assert attention.status_code == 200
        assert len(attention.json()["items"]) <= 5

        tasks = client.get("/api/dashboard/tasks")
        assert tasks.status_code == 200
        assert len(tasks.json()["trend"]) == 7
        assert len(tasks.json()["recent"]) <= 6
        expected_recent_task_fields = {
            "runtime_state", "runtime_status_label", "runtime_detail", "failure",
            "id",
            "title",
            "status",
            "employee_id",
            "employee_name",
            "updated_at",
        }
        assert all(set(item) == expected_recent_task_fields for item in tasks.json()["recent"])

        activity = client.get("/api/dashboard/activity")
        assert activity.status_code == 200
        assert len(activity.json()["items"]) <= 12
        assert all("message" not in item and "detail" not in item for item in activity.json()["items"])

        knowledge = client.get(
            "/api/dashboard/knowledge",
            headers={"X-Astra-User-Id": "dashboard-viewer", "X-Astra-Role": "viewer"},
        )
        assert knowledge.status_code == 200
        assert set(knowledge.json()) == {"today_added", "candidate_experiences", "total", "recent"}
        assert knowledge.json()["candidate_experiences"] == 0
        assert all(set(item) == {"id", "title"} for item in knowledge.json()["recent"])






def test_employee_scope_environment_and_category_configuration(tmp_path):
    with TestClient(app) as client:
        category = client.post(
            "/api/task-categories",
            json={"name": "接口治理", "description": "接口治理类任务", "sort_order": 40},
        )
        assert category.status_code == 201
        category_id = category.json()["id"]

        created = client.post(
            "/api/employees",
            json={
                "id": "astra-api-v03",
                "name": "Astra API V0.4",
                "role": "接口治理员工",
                "default_work_area": "integration",
                "task_types": ["ISSUE", "REQUIREMENT"],
                "category_ids": [category_id],
                "auto_claim": True,
                "auto_execute": False,
                "execution_environment": {
                    "mode": "LOCAL",
                    "name": "API 本地工作空间",
                    "opencode_server_port": 54199,
                    "opencode_path": sys.executable,
                    "model_name": "deepseek/deepseek-v4-flash",
                    "workspace_root": str(tmp_path / "employee-workspace"),
                },
            },
        )
        assert created.status_code == 201
        employee = created.json()
        assert employee["task_types"] == ["ISSUE", "REQUIREMENT"]
        assert employee["category_ids"] == [category_id]
        assert employee["default_work_area"] == "integration"
        assert employee["execution_environment"]["workspace_root"].endswith("employee-workspace")
        assert employee["execution_environment"]["model_name"] == "deepseek/deepseek-v4-flash"
        assert "skills" not in employee
        assert "capabilities" not in employee

        area_updated = client.patch(
            "/api/employees/astra-api-v03", json={"default_work_area": "meeting"}
        )
        assert area_updated.status_code == 200
        assert area_updated.json()["default_work_area"] == "meeting"
        invalid_area = client.patch(
            "/api/employees/astra-api-v03", json={"default_work_area": "unknown-area"}
        )
        assert invalid_area.status_code == 422

def test_employee_without_task_history_can_be_deleted(tmp_path):
    with TestClient(app) as client:
        category = client.post(
            "/api/task-categories",
            json={"name": "员工删除测试", "description": "删除无任务员工"},
        )
        assert category.status_code == 201
        created = client.post(
            "/api/employees",
            json={
                "id": "astra-api-deletable",
                "name": "可删除员工",
                "role": "删除测试",
                "category_ids": [category.json()["id"]],
                "execution_environment": {
                    "mode": "LOCAL",
                    "name": "删除测试环境",
                    "opencode_server_port": 55199,
                    "opencode_path": sys.executable,
                    "workspace_root": str(tmp_path / "deletable-workspace"),
                },
            },
        )
        assert created.status_code == 201
        assert client.delete("/api/employees/astra-api-deletable").json() == {
            "deleted": "astra-api-deletable"
        }
        assert client.get("/api/employees/astra-api-deletable").status_code == 404
        assert client.delete("/api/employees/astra-api-deletable").status_code == 404


def test_remote_ssh_password_is_never_returned_and_blank_update_keeps_value():
    with TestClient(app) as client:
        category = client.post(
            "/api/task-categories",
            json={"name": "远程环境测试", "description": "验证 SSH 密码边界"},
        )
        assert category.status_code == 201
        created = client.post(
            "/api/employees",
            json={
                "id": "astra-api-remote-password",
                "name": "远程密码员工",
                "role": "远程执行测试",
                "category_ids": [category.json()["id"]],
                "execution_environment": {
                    "mode": "LINUX_REMOTE",
                    "name": "远程 Agent 环境",
                    "host": "example.internal",
                    "port": 22,
                    "username": "nexus",
                    "ssh_password": "database-only-secret",
                    "opencode_server_port": 4196,
                    "opencode_path": "/opt/agent/start-agent.sh",
                    "workspace_root": "/srv/workspace",
                },
            },
        )

        assert created.status_code == 201
        environment = created.json()["execution_environment"]
        assert environment["has_ssh_password"] is True
        assert environment["opencode_server_port"] == 4196
        assert "ssh_password" not in environment

        updated = client.put(
            "/api/employees/astra-api-remote-password/environment",
            json={
                "mode": "LINUX_REMOTE",
                "name": "远程 Agent 环境（已更新）",
                "host": "example.internal",
                "port": 22,
                "username": "nexus",
                "ssh_password": "",
                "opencode_server_port": 4296,
                "opencode_path": "/opt/agent/start-agent.sh",
                "workspace_root": "/srv/workspace",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["has_ssh_password"] is True
        assert updated.json()["opencode_server_port"] == 4296
        assert "ssh_password" not in updated.json()

        async def load_password():
            async with SessionLocal() as session:
                row = await session.get(OpenCodeExecutionEnvironment, environment["id"])
                return row.ssh_password

        assert asyncio.run(load_password()) == "database-only-secret"

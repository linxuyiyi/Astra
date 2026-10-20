from fastapi.testclient import TestClient

from app.main import app


def test_category_prompts_persist_and_patch_independently():
    with TestClient(app) as client:
        created = client.post(
            "/api/task-categories",
            json={
                "name": "提示词持久化测试",
                "enabled": False,
                "task_prompt": "  Task 123\n任务说明 {{input}}  ",
                "question_prompt": "问题说明\nQuestion?",
            },
        )
        assert created.status_code == 201
        category = created.json()
        category_id = category["id"]
        assert category["task_prompt"] == "  Task 123\n任务说明 {{input}}  "
        assert category["question_prompt"] == "问题说明\nQuestion?"
        updated = client.patch(
            f"/api/task-categories/{category_id}",
            json={
                "task_prompt": "修改任务\nUpdated task",
                "question_prompt": "修改问题\nUpdated question",
            },
        )
        assert updated.status_code == 200
        metadata = client.patch(f"/api/task-categories/{category_id}", json={"description": "只修改描述"})
        assert metadata.json()["task_prompt"] == "修改任务\nUpdated task"
        assert metadata.json()["question_prompt"] == "修改问题\nUpdated question"
    with TestClient(app) as client:
        rows = client.get("/api/task-categories?include_disabled=true").json()
        saved = next(row for row in rows if row["id"] == category_id)
        assert saved["task_prompt"] == "修改任务\nUpdated task"
        assert saved["question_prompt"] == "修改问题\nUpdated question"
        cleared = client.patch(
            f"/api/task-categories/{category_id}", json={"task_prompt": "", "question_prompt": None}
        )
        assert cleared.status_code == 200
        assert cleared.json()["task_prompt"] == ""
        assert cleared.json()["question_prompt"] is None

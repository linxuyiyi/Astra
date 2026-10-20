"""HTTP contract for review visibility and retry without reopening the business task."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.assistant.review import review_id
from app.database import SessionLocal
from app.main import app
from app.models import AgentWorker, RuntimeSession, Task, TaskReview, TaskSummary


def test_review_api_exposes_failure_and_retries_same_record():
    suffix = uuid4().hex[:8]
    employee_id, task_id, session_id = f"E-R-{suffix}", f"T-R-{suffix}", f"ses_R_{suffix}"

    async def seed():
        async with SessionLocal() as session:
            task = Task(id=task_id, title="复盘任务", task_type="ISSUE", employee_id=employee_id,
                        status="SUCCEEDED", current_session_id=session_id, last_message_id="turn_1")
            binding = RuntimeSession(id=session_id, task_id=task_id, employee_id=employee_id,
                                     workspace="/test", environment={}, status="SUCCEEDED")
            session.add_all([AgentWorker(id=employee_id, name="复盘员工", role="build"), task, binding])
            await session.flush()
            session.add(TaskReview(id=review_id(task, binding), task_id=task_id,
                                   source_session_id=session_id, source_status="SUCCEEDED",
                                   status="failed", attempts=1, error="会话不可达",
                                   retry_at=datetime.now(UTC) + timedelta(minutes=1)))
            session.add(TaskSummary(id=f"TS-{suffix}", task_id=task_id, primary_session_id=session_id,
                                    content_md="## 任务目标\n\n处理缓存问题。", source_status="SUCCEEDED",
                                    source_ref_snapshot={"turn_anchor_id": "turn_1"}))
            await session.commit()

    async def state():
        async with SessionLocal() as session:
            return (await session.get(Task, task_id)).status, await session.scalar(
                select(func.count()).select_from(TaskReview).where(TaskReview.task_id == task_id)
            )

    with TestClient(app) as client:
        asyncio.run(seed())
        detail = client.get(f"/api/tasks/{task_id}/review")
        assert detail.status_code == 200
        assert detail.json()["status"] == "failed" and detail.json()["can_retry"]
        assert detail.json()["summary_current"] and "任务目标" in detail.json()["summary"]
        retry = client.post(f"/api/tasks/{task_id}/review/retry")
        assert retry.status_code == 200 and retry.json()["status"] == "pending"
        assert client.post(f"/api/tasks/{task_id}/review/retry").status_code == 200
        assert asyncio.run(state()) == ("SUCCEEDED", 1)
        assert client.get("/api/tasks/unknown-review-task/review").status_code == 404

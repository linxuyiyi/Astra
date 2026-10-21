"""Maintenance actor contract: server-resolved UID, audit trail, no body override."""

from __future__ import annotations

from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from app.database import SessionLocal, init_database
from app.main import app
from app.models import (
    AgentWorker,
    OpenCodeExecutionEnvironment,
    WorkerMaintenanceEvent,
    WorkerMaintenanceOperation,
)
from app.runtime.maintenance import submit_operation


async def make_environment(*, status: str = "AVAILABLE") -> tuple[int, str]:
    await init_database()
    employee_id = "actor-" + uuid4().hex[:10]
    async with SessionLocal() as db:
        db.add(AgentWorker(id=employee_id, name=employee_id, role="build", current_task_count=0))
        await db.flush()
        environment = OpenCodeExecutionEnvironment(
            employee_id=employee_id,
            mode="LINUX_REMOTE",
            name="Linux",
            host="test.invalid",
            port=22,
            username="root",
            ssh_password="ssh-secret",
            workspace_root="/srv/project",
            opencode_path="/usr/local/bin/opencode",
            status=status,
            status_detail={},
            worker_channel="ssh",
        )
        db.add(environment)
        await db.commit()
        return environment.id, employee_id


async def make_operation(environment_id: int, *, requested_by: str = "alice"):
    async with SessionLocal() as db:
        return await submit_operation(
            db, environment_id, "check", requested_by=requested_by
        )


async def read_operation(operation_id: str) -> WorkerMaintenanceOperation | None:
    async with SessionLocal() as db:
        operation = await db.get(WorkerMaintenanceOperation, operation_id)
        if operation is not None:
            db.expunge(operation)
        return operation


async def events_of(operation_id: str) -> list[WorkerMaintenanceEvent]:
    async with SessionLocal() as db:
        rows = list(
            (
                await db.scalars(
                    select(WorkerMaintenanceEvent)
                    .where(WorkerMaintenanceEvent.operation_id == operation_id)
                    .order_by(WorkerMaintenanceEvent.id)
                )
            ).all()
        )
        for row in rows:
            db.expunge(row)
        return rows


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_submit_without_identity_is_rejected_and_writes_nothing():
    environment_id, _ = await make_environment()
    async with client() as http:
        response = await http.post(
            f"/api/agent-workers/{environment_id}/maintenance",
            json={"action": "check"},
        )
    assert response.status_code == 401
    async with SessionLocal() as db:
        count = await db.scalar(
            select(WorkerMaintenanceOperation.id).where(
                WorkerMaintenanceOperation.environment_id == environment_id
            )
        )
    assert count is None


@pytest.mark.asyncio
async def test_submit_uses_trusted_header_and_ignores_body_requester():
    environment_id, _ = await make_environment()
    async with client() as http:
        response = await http.post(
            f"/api/agent-workers/{environment_id}/maintenance",
            json={"action": "check", "requested_by": "console-admin"},
            headers={"X-Verified-UID": "alice"},
        )
    assert response.status_code == 202
    assert response.json()["requested_by"] == "alice"
    stored = await read_operation(response.json()["id"])
    assert stored is not None and stored.requested_by == "alice"


@pytest.mark.asyncio
async def test_submit_accepts_cookie_in_manual_uid_mode():
    environment_id, _ = await make_environment()
    async with client() as http:
        response = await http.post(
            f"/api/agent-workers/{environment_id}/maintenance",
            json={"action": "check"},
            headers={"Cookie": "nexus_uid=bob"},
        )
    assert response.status_code == 202
    assert response.json()["requested_by"] == "bob"


@pytest.mark.asyncio
async def test_cookie_uid_round_trips_browser_encoding():
    environment_id, _ = await make_environment()
    uid = "验收用户/team+1"
    async with client() as http:
        response = await http.post(
            f"/api/agent-workers/{environment_id}/maintenance",
            json={"action": "check"},
            headers={"Cookie": f"nexus_uid={quote(uid, safe='')}"},
        )
    assert response.status_code == 202
    assert response.json()["requested_by"] == uid


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-Verified-UID": "proxy", "X-Owner-Id": "owner", "Cookie": "nexus_uid=cookie"}, "proxy"),
        ({"X-Owner-Id": "owner%2Fraw", "Cookie": "nexus_uid=cookie"}, "owner%2Fraw"),
    ],
)
async def test_identity_priority_and_literal_headers(headers, expected):
    environment_id, _ = await make_environment()
    async with client() as http:
        response = await http.post(
            f"/api/agent-workers/{environment_id}/maintenance",
            json={"action": "check", "requested_by": "forged"},
            headers=headers,
        )
    assert response.status_code == 202
    assert response.json()["requested_by"] == expected


@pytest.mark.asyncio
async def test_cancel_records_actor_and_keeps_original_requester():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id, requested_by="alice")
    async with client() as http:
        response = await http.post(
            f"/api/maintenance/{operation.id}/cancel",
            headers={"X-Owner-Id": "bob"},
        )
    assert response.status_code == 200
    assert response.json()["requested_by"] == "alice"
    stored = await read_operation(operation.id)
    assert stored is not None and stored.requested_by == "alice"
    canceled = [event for event in await events_of(operation.id) if event.type == "canceled"]
    assert canceled and canceled[-1].detail == {"reason": "canceled", "requested_by": "bob"}


@pytest.mark.asyncio
async def test_cancel_without_identity_is_rejected():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    async with client() as http:
        response = await http.post(f"/api/maintenance/{operation.id}/cancel")
    assert response.status_code == 401
    stored = await read_operation(operation.id)
    assert stored is not None and stored.state == "QUEUED"


@pytest.mark.asyncio
async def test_retry_records_actor_and_keeps_original_requester():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id, requested_by="alice")
    async with SessionLocal() as db:
        await db.execute(
            update(WorkerMaintenanceOperation)
            .where(WorkerMaintenanceOperation.id == operation.id)
            .values(
                state="FAILED",
                stage="failed",
                error_code="remote_state_unknown",
                error_message="测试失败",
            )
        )
        await db.commit()
    async with client() as http:
        response = await http.post(
            f"/api/maintenance/{operation.id}/retry",
            headers={"X-Verified-UID": "carol"},
        )
    assert response.status_code == 200
    assert response.json()["requested_by"] == "alice"
    stored = await read_operation(operation.id)
    assert stored is not None and stored.requested_by == "alice"
    retries = [event for event in await events_of(operation.id) if event.type == "retry"]
    assert retries and retries[-1].detail["requested_by"] == "carol"


@pytest.mark.asyncio
async def test_reconcile_records_actor_without_replacing_initiator():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id, requested_by="alice")
    async with SessionLocal() as db:
        await db.execute(
            update(WorkerMaintenanceOperation)
            .where(WorkerMaintenanceOperation.id == operation.id)
            .values(state="INTERRUPTED", stage="interrupted", reconcile_required=True)
        )
        await db.commit()
    async with client() as http:
        response = await http.post(
            f"/api/maintenance/{operation.id}/reconcile",
            headers={"X-Owner-Id": "reviewer"},
        )
    assert response.status_code == 200
    assert response.json()["requested_by"] == "alice"
    retries = [event for event in await events_of(operation.id) if event.type == "retry"]
    assert retries[-1].detail["requested_by"] == "reviewer"
    assert retries[-1].detail["reconcile_only"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["retry", "reconcile"])
async def test_followup_actions_require_identity(action):
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    async with client() as http:
        response = await http.post(f"/api/maintenance/{operation.id}/{action}")
    assert response.status_code == 401
    stored = await read_operation(operation.id)
    assert stored is not None and stored.state == "QUEUED"

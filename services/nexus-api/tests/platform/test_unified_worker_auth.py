"""统一服务必须保留 Worker 协议鉴权，而非要求浏览器 Cookie。"""

import httpx
import pytest

from app.database import SessionLocal
from app.models import AgentWorker, OpenCodeExecutionEnvironment
from app.worker_gateway.bus import MemoryRouteBus
from app.worker_gateway.service import WorkerGateway
from nexus_api.service import create_service


@pytest.mark.asyncio
async def test_worker_and_admin_protocol_auth_without_cookie(monkeypatch):
    import app.worker_gateway.service as service

    gateway = WorkerGateway(bus=MemoryRouteBus())
    monkeypatch.setattr(service, "_gateway", gateway)
    async with SessionLocal() as db:
        worker = AgentWorker(id="qa-unified-worker-auth", name="QA", role="test")
        db.add(worker)
        await db.flush()
        environment = OpenCodeExecutionEnvironment(employee_id=worker.id)
        db.add(environment)
        await db.commit()
        environment_id = environment.id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_service()), base_url="http://test"
    ) as client:
        rejected = await client.post("/api/worker-gateway/instances", json={"environment_id": environment_id})
        assert rejected.status_code == 401
        assert rejected.json()["detail"]["code"] == "unauthorized"
        enrolled = await client.post(
            "/api/worker-gateway/instances",
            json={"environment_id": environment_id},
            headers={"X-Astra-Worker-Gateway-Key": "wgk_test"},
        )
        assert enrolled.status_code == 201, enrolled.text
        headers = {
            "Authorization": "Bearer " + enrolled.json()["token"],
            "X-Astra-Worker-Epoch": "0",
        }
        missing = await client.get("/api/worker-gateway/data/no-such-request/request", headers=headers)
        assert missing.status_code == 404, missing.text
        headers["Authorization"] = "Bearer invalid"
        invalid = await client.get("/api/worker-gateway/data/no-such-request/request", headers=headers)
        assert invalid.status_code == 401
        assert invalid.json()["detail"]["code"] == "unauthorized"
        assert (await client.get("/api/agent-workers")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("spoof", [False, True])
async def test_maintenance_records_temporary_uid(monkeypatch, spoof):
    from uuid import uuid4

    uid = "qa-" + uuid4().hex
    async with SessionLocal() as db:
        worker = AgentWorker(id=uid, name="QA", role="test")
        db.add(worker)
        await db.flush()
        environment = OpenCodeExecutionEnvironment(employee_id=uid)
        db.add(environment)
        await db.commit()
        environment_id = environment.id
    headers = {"X-Owner-Id": uid}
    if spoof:
        headers.update({"X-Verified-UID": "forged", "Cookie": "nexus_session=obsolete"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_service()), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/agent-workers/{environment_id}/maintenance",
            json={"action": "check", "requested_by": "forged"},
            headers=headers,
        )
        assert response.status_code == 202, response.text
        assert response.json()["requested_by"] == uid
        assert (
            await client.post(f"/api/agent-workers/{environment_id}/maintenance", json={"action": "check"})
        ).status_code == 401


@pytest.mark.asyncio
async def test_task_wake_lookup_authenticates_as_service(monkeypatch):
    from app.config.settings import get_settings
    from app.models import TaskWakeOutbox
    from app.runtime.task_wakeup import HttpWakeSender, WakeRetry

    monkeypatch.setenv("NEXUS_API_BASE", "http://test")
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "qa-internal-key")
    get_settings.cache_clear()
    row = TaskWakeOutbox(responsible_employee_id="no-such-employee", source_session_id="no-session")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_service()), base_url="http://test"
        ) as client:
            # The source lookup must pass service auth before reporting a missing source.
            with pytest.raises(WakeRetry):
                await HttpWakeSender(client).send(row)
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_library_audit_identity_records_temporary_uid():
    from tests.nexus.clients import session_headers

    headers = session_headers("qa-library-reviewer")
    headers.update({"X-Astra-User-Id": "forged", "X-Astra-Role": "admin"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_service()), base_url="http://test"
    ) as client:
        response = await client.get("/api/library/permissions", headers=headers)
        assert response.status_code == 200
        assert response.json()["user_id"] == "qa-library-reviewer"


@pytest.mark.asyncio
@pytest.mark.parametrize("spoof", [False, True])
async def test_stop_release_uses_temporary_uid_and_exposes_canonical_occupancy(spoof):
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.models import Task
    from tests.nexus.clients import session_headers

    uid = "qa-stop-" + uuid4().hex
    headers = session_headers(uid)
    if spoof:
        headers["X-Astra-User-Id"] = "forged"
    async with SessionLocal() as db:
        db.add(AgentWorker(id=uid, name="QA", role="QA"))
        task = Task(
            id="T-" + uid,
            title="QA cancellation",
            task_type="ISSUE",
            status="CANCELED",
            employee_id=uid,
            claimed_at=datetime.now(UTC),
        )
        db.add(task)
        await db.commit()
        task_id = task.id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_service()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/tasks/" + task_id + "/confirm-execution-stop",
            json={"reason": "已核对执行停止"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        async with SessionLocal() as db:
            current = await db.get(Task, task_id)
            assert current.execution_stop_operator == uid
        assert response.json()["execution_released"] is True

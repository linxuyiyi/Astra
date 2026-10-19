"""P0 worker maintenance: persistence, leases, guards, disconnect, SSE resume."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from app.config.settings import get_settings
from app.database import SessionLocal, init_database
from app.domain import EnvironmentStatus
from app.gateway.security import gateway_key_digest
from app.main import app
from app.models import (
    DigitalEmployee,
    OpenCodeExecutionEnvironment,
    Task,
    WorkerInstance,
    WorkerMaintenanceOperation,
)
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.runtime.maintenance import (
    MaintenanceLeaseLost,
    OperationLease,
    append_event,
    cancel_operation,
    claim_next,
    environment_health,
    event_stream,
    finish_operation,
    observe_environment,
    recover_expired,
    retry_operation,
    run_maintenance_once,
    submit_operation,
)
from app.runtime.remote_provisioning import RemoteProvisioner
from app.runtime.worker_channel import mark_worker_channel


@pytest.fixture(autouse=True)
def healthy_observation(monkeypatch):
    async def fake(environment_id, *, persist=False, **_kwargs):
        now = datetime.now(UTC)
        return {
            "stale": False,
            "status": EnvironmentStatus.AVAILABLE,
            "desired": {},
            "observation": {"source": "test"},
            "equipment": [],
            "repository": None,
            "checked_at": now,
            "values": {
                "status": EnvironmentStatus.AVAILABLE,
                "last_checked_at": now,
                "status_detail": {},
            },
        }

    monkeypatch.setattr("app.runtime.maintenance.observe_environment", fake)


@pytest.fixture(autouse=True)
def maintenance_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mcp_transport", "streamable-http")
    monkeypatch.setattr(settings, "remote_gateway_local_url", "http://10.0.0.8:8110")
    monkeypatch.setattr(settings, "mcp_resource_server_url", "http://10.0.0.8:8201/mcp")
    monkeypatch.setattr(
        settings, "worker_agent_gateway_url", "ws://10.0.0.8:8100/api/worker-gateway/connect"
    )


async def make_environment(**overrides):
    await init_database()
    employee_id = "maint-" + uuid4().hex[:10]
    async with SessionLocal() as db:
        db.add(
            DigitalEmployee(
                id=employee_id,
                name=employee_id,
                role="build",
                current_task_count=0,
            )
        )
        await db.flush()
        values = dict(
            employee_id=employee_id,
            mode="LINUX_REMOTE",
            name="Linux",
            host="test.invalid",
            port=22,
            username="root",
            ssh_password="ssh-secret",
            workspace_root="/srv/project",
            opencode_path="/usr/local/bin/opencode",
            status="AVAILABLE",
            status_detail={},
            worker_channel="ssh",
        )
        values.update(overrides)
        environment = OpenCodeExecutionEnvironment(**values)
        db.add(environment)
        await db.commit()
        return environment.id, employee_id


async def make_operation(environment_id, action="deploy"):
    async with SessionLocal() as db:
        return await submit_operation(db, environment_id, action, requested_by="test")


async def read_operation(operation_id):
    async with SessionLocal() as db:
        operation = await db.get(WorkerMaintenanceOperation, operation_id)
        if operation is not None:
            db.expunge(operation)
        return operation


async def expire_lease(operation_id):
    async with SessionLocal() as db:
        await db.execute(
            update(WorkerMaintenanceOperation)
            .where(WorkerMaintenanceOperation.id == operation_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()


@pytest.mark.asyncio
async def test_closing_event_stream_does_not_cancel_operation(monkeypatch):
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(self, emit):
        await self.lease.set_stage("running")
        await emit(
            {"type": "step", "name": "测试阶段", "state": "running", "detail": "已开始"}
        )
        started.set()
        await release.wait()
        await self._finish(True, "", None)
        await emit({"type": "result", "success": True, "message": "完成", "status": "AVAILABLE"})

    monkeypatch.setattr(RemoteProvisioner, "run", fake_run)
    runner = asyncio.create_task(run_maintenance_once("test-owner"))
    await asyncio.wait_for(started.wait(), timeout=5)

    stream = event_stream(operation.id, 0)
    first = await anext(stream)
    assert first["type"] in {"started", "step"}
    await stream.aclose()

    still = await read_operation(operation.id)
    assert still.state in {"PREFLIGHT", "RUNNING"}
    assert still.lease_owner == "test-owner"

    release.set()
    await asyncio.wait_for(runner, timeout=5)
    final = await read_operation(operation.id)
    assert final.state == "SUCCEEDED"


@pytest.mark.asyncio
async def test_sse_resume_uses_last_event_id():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    async with SessionLocal() as db:
        first = await append_event(db, operation.id, 1, name="一", message="a")
        await append_event(db, operation.id, 1, name="二", message="b")
        await append_event(db, operation.id, 1, name="三", message="c")

    async with SessionLocal() as db:
        await cancel_operation(db, operation.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with client.stream(
            "GET",
            f"/api/maintenance/{operation.id}/events",
            headers={"Last-Event-ID": str(first.id)},
        ) as response:
            assert response.status_code == 200
            ids: list[int] = []
            async for line in response.aiter_lines():
                if line.startswith("id: "):
                    ids.append(int(line[4:]))
                if len(ids) == 2:
                    break
            assert ids == [first.id + 1, first.id + 2]


@pytest.mark.asyncio
async def test_expired_lease_is_interrupted_and_stale_attempt_cannot_write():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    assert await claim_next("owner-a") == operation.id
    lease = OperationLease(operation.id, 1, "owner-a", 60)
    await expire_lease(operation.id)

    assert await recover_expired("owner-b") == 1
    recovered = await read_operation(operation.id)
    assert recovered.state == "INTERRUPTED"
    assert recovered.error_code == "lease_expired"
    assert recovered.active_environment_id == environment_id
    assert not await lease.finish(
        "SUCCEEDED", stage="verify", after_snapshot={"stale": True}
    )
    assert (await read_operation(operation.id)).state == "INTERRUPTED"

    async with SessionLocal() as db:
        retried = await retry_operation(db, operation.id)
    assert retried.attempt == 2
    assert retried.state == "QUEUED"
    assert retried.reconcile_required is True
    assert await claim_next("owner-b") == operation.id
    assert not await lease.finish(
        "SUCCEEDED", stage="verify", after_snapshot={"stale": True}
    )
    current = await read_operation(operation.id)
    assert current.attempt == 2
    assert current.state == "PREFLIGHT"
    await finish_operation(operation.id, "owner-b", 2, "CANCELED")


@pytest.mark.asyncio
async def test_two_concurrent_submissions_only_one_wins():
    environment_id, _ = await make_environment()

    async def submit():
        try:
            async with SessionLocal() as db:
                return await submit_operation(db, environment_id, "restart")
        except HTTPException as exc:
            return exc

    results = await asyncio.gather(submit(), submit())
    wins = [item for item in results if isinstance(item, WorkerMaintenanceOperation)]
    losses = [item for item in results if isinstance(item, HTTPException)]
    assert len(wins) == 1
    assert len(losses) == 1
    assert losses[0].status_code == 409
    async with SessionLocal() as db:
        await cancel_operation(db, wins[0].id)


@pytest.mark.asyncio
async def test_waiting_task_and_maintenance_block_task_start():
    environment_id, employee_id = await make_environment()
    async with SessionLocal() as db:
        db.add(
            Task(
                id="task-" + uuid4().hex,
                title="waiting",
                task_type="ISSUE",
                category_id="testing-platform",
                employee_id=employee_id,
                status="WAITING",
                pending_message_id="msg_waiting",
            )
        )
        await db.commit()
    with pytest.raises(HTTPException) as error:
        await make_operation(environment_id)
    assert error.value.status_code == 409

    async with SessionLocal() as db:
        task = await db.scalar(select(Task).where(Task.employee_id == employee_id))
        task.status = "PENDING"
        task.pending_message_id = None
        await db.commit()
        operation = await submit_operation(db, environment_id, "restart")
        operation_id = operation.id
        with pytest.raises(RuntimeStateError, match="维护"):
            await RuntimeService(db).start(task.id)
    async with SessionLocal() as db:
        await cancel_operation(db, operation_id)


@pytest.mark.asyncio
async def test_native_busy_blocks_offline_and_online_restart(monkeypatch):
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    assert await claim_next("owner-native") == operation.id
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        db.expunge(environment)
    lease = OperationLease(operation.id, 1, "owner-native", 60)
    worker = RemoteProvisioner(environment, operation, lease=lease)

    async def run_command(_command, timeout=30):
        return 0, '{"ses_busy": {"type": "busy"}}', ""

    worker.ssh = type("SSH", (), {"run_command": staticmethod(run_command)})()
    with pytest.raises(RuntimeError, match="正在执行"):
        await worker._native_idle(strict=True)

    async def busy_request(*_args, **_kwargs):
        return {"ses_busy": {"type": "busy"}}

    monkeypatch.setattr(
        "app.runtime.worker_client.WorkerOpenCodeClient.request", busy_request
    )
    with pytest.raises(RuntimeError, match="正在执行"):
        await worker._native_idle_online()
    await finish_operation(operation.id, "owner-native", 1, "CANCELED")


@pytest.mark.asyncio
async def test_cancel_during_danger_keeps_interrupt_and_lock(monkeypatch):
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    danger = asyncio.Event()
    proceed = asyncio.Event()

    async def fake_run(self, emit):
        await self.lease.mark_danger()
        await self.lease.set_stage("restart")
        danger.set()
        await proceed.wait()
        await self.lease.check_canceled()

    monkeypatch.setattr(RemoteProvisioner, "run", fake_run)
    runner = asyncio.create_task(run_maintenance_once("owner-cancel"))
    await asyncio.wait_for(danger.wait(), timeout=5)
    async with SessionLocal() as db:
        await cancel_operation(db, operation.id)
    proceed.set()
    await asyncio.wait_for(runner, timeout=5)

    current = await read_operation(operation.id)
    assert current.state == "INTERRUPTED"
    assert current.error_code == "canceled_after_remote_change"
    assert current.active_environment_id == environment_id

    async with SessionLocal() as db:
        await cancel_operation(db, operation.id) if current.state == "QUEUED" else None
    # The interrupted lock can only be released after verification or an explicit
    # safe cancellation. Finish it here for test isolation.
    async with SessionLocal() as db:
        await finish_operation(operation.id, "owner-cancel", 1, "CANCELED")


@pytest.mark.asyncio
async def test_cancel_before_danger_releases_lock(monkeypatch):
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    entered = asyncio.Event()
    proceed = asyncio.Event()

    async def fake_run(self, emit):
        await self.lease.set_stage("preflight")
        entered.set()
        await proceed.wait()
        await self.lease.check_canceled()

    monkeypatch.setattr(RemoteProvisioner, "run", fake_run)
    runner = asyncio.create_task(run_maintenance_once("owner-safe-cancel"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    async with SessionLocal() as db:
        await cancel_operation(db, operation.id)
    proceed.set()
    await asyncio.wait_for(runner, timeout=5)
    current = await read_operation(operation.id)
    assert current.state == "CANCELED"
    assert current.active_environment_id is None


@pytest.mark.asyncio
async def test_failure_before_danger_keeps_healthy_environment(monkeypatch):
    environment_id, _ = await make_environment(status="AVAILABLE")
    operation = await make_operation(environment_id)

    async def fake_run(self, emit):
        raise RuntimeError("preflight failed")

    monkeypatch.setattr(RemoteProvisioner, "run", fake_run)
    await run_maintenance_once("owner-fail")
    current = await read_operation(operation.id)
    assert current.state == "FAILED"
    assert current.active_environment_id is None
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        assert environment.status == "AVAILABLE"


@pytest.mark.asyncio
async def test_failure_after_danger_marks_unknown_and_keeps_lock(monkeypatch):
    environment_id, _ = await make_environment(status="AVAILABLE")
    operation = await make_operation(environment_id)

    async def fake_run(self, emit):
        await self.lease.mark_danger()
        await self.lease.set_stage("install")
        await self._finish(False, "install failed")

    monkeypatch.setattr(RemoteProvisioner, "run", fake_run)
    await run_maintenance_once("owner-danger-fail")
    current = await read_operation(operation.id)
    assert current.state == "INTERRUPTED"
    assert current.reconcile_required is False
    assert current.active_environment_id == environment_id
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        assert environment.status == "UNKNOWN"
    await finish_operation(operation.id, "owner-danger-fail", 1, "CANCELED")


@pytest.mark.asyncio
async def test_health_separates_maintenance_connection_and_scheduling():
    now = datetime.now(UTC)
    environment_id, employee_id = await make_environment(
        status="AVAILABLE",
        last_checked_at=now,
        status_detail={
            "observed_at": now.isoformat(),
            "observed_epoch": 1,
            "observed_online": True,
        },
    )
    async with SessionLocal() as db:
        db.add(
            WorkerInstance(
                id="wrk_health",
                environment_id=environment_id,
                token_digest=gateway_key_digest("wtk_health"),
                status="online",
                epoch=1,
            )
        )
        await db.commit()
    async with SessionLocal() as db:
        health = await environment_health(db, environment_id)
    assert health["runtime_health"]["status"] == "AVAILABLE"
    assert health["connection"]["state"] == "online"
    assert health["maintenance"] is None
    assert health["schedulable"] is True

    operation = await make_operation(environment_id, action="check")
    async with SessionLocal() as db:
        health = await environment_health(db, environment_id)
    assert health["maintenance"]["state"] == "QUEUED"
    assert health["schedulable"] is False
    assert any(item["code"] == "maintenance_active" for item in health["blocking"])
    async with SessionLocal() as db:
        await cancel_operation(db, operation.id)

    async with SessionLocal() as db:
        db.add(
            Task(
                id="task-" + uuid4().hex,
                title="human wait",
                task_type="ISSUE",
                category_id="testing-platform",
                employee_id=employee_id,
                status="WAITING",
                pending_message_id="msg_waiting",
            )
        )
        await db.commit()
        health = await environment_health(db, environment_id)
    assert health["schedulable"] is False
    assert any(item["code"] == "occupied_task" for item in health["blocking"])


@pytest.mark.asyncio
async def test_worker_channel_respects_maintenance_lock(monkeypatch):
    monkeypatch.setattr("app.runtime.worker_channel._mismatch", lambda _result: None)
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id, action="restart")
    async with SessionLocal() as db:
        with pytest.raises(HTTPException) as error:
            await mark_worker_channel(db, environment_id, switch=lambda: None)
        assert error.value.status_code == 409
    async with SessionLocal() as db:
        await cancel_operation(db, operation.id)

    async def switch():
        return {"reloaded": True}

    async with SessionLocal() as db:
        stored = await mark_worker_channel(db, environment_id, switch=switch)
        assert stored.worker_channel == "worker"


@pytest.mark.asyncio
async def test_existing_worker_token_is_reused_when_digest_matches():
    environment_id, _ = await make_environment()
    token = "wtk_existing_reuse"
    async with SessionLocal() as db:
        db.add(
            WorkerInstance(
                id="wrk_reuse",
                environment_id=environment_id,
                token_digest=gateway_key_digest(token),
                status="online",
                epoch=3,
            )
        )
        await db.commit()
    environment = SimpleNamespace(
        id=environment_id,
        ssh_password="ssh-secret",
        model_name="",
        opencode_path="/usr/local/bin/opencode",
    )
    worker = RemoteProvisioner(environment, {"id": "op-token", "action": "deploy"})

    class SSH:
        async def read_file(self, _path):
            return (
                f"NEXUS_WORKER_TOKEN={token}\n"
                "NEXUS_WORKER_INSTANCE_ID=wrk_reuse\n"
            ).encode()

    worker.ssh = SSH()
    assert await worker._existing_worker_credentials("/etc/nexus/worker.env") == (
        token,
        "wrk_reuse",
    )

    class WrongToken(SSH):
        async def read_file(self, _path):
            return b"NEXUS_WORKER_TOKEN=wtk_wrong\nNEXUS_WORKER_INSTANCE_ID=wrk_reuse\n"

    worker.ssh = WrongToken()
    with pytest.raises(RuntimeError, match="不一致"):
        await worker._existing_worker_credentials("/etc/nexus/worker.env")


@pytest.mark.asyncio
async def test_enrolled_token_ciphertext_recovers_after_controller_crash(monkeypatch):
    from app.gateway.security import CredentialCipher

    settings = get_settings()
    key = "9Xr3mYq0mJ4W8n2z5v7b1c3d6f8g0h2j4k6l8m0n2p4="
    monkeypatch.setattr(settings, "gateway_master_key", key)
    cipher = CredentialCipher(key)
    token = "wtk_crash_window"
    environment_id, _ = await make_environment()
    async with SessionLocal() as db:
        db.add(
            WorkerInstance(
                id="wrk_crash",
                environment_id=environment_id,
                token_digest=gateway_key_digest(token),
                token_ciphertext=cipher.encrypt(token),
                status="offline",
                epoch=1,
            )
        )
        await db.commit()
    environment = SimpleNamespace(
        id=environment_id,
        ssh_password="ssh-secret",
        model_name="",
        opencode_path="/usr/local/bin/opencode",
    )
    worker = RemoteProvisioner(environment, {"id": "op-crash", "action": "deploy"})

    class SSH:
        async def read_file(self, _path):
            return None

    worker.ssh = SSH()
    assert await worker._existing_worker_credentials("/etc/nexus/worker.env") == (
        token,
        "wrk_crash",
    )


@pytest.mark.asyncio
async def test_failed_apply_does_not_revoke_key_already_written(monkeypatch):
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    assert await claim_next("owner-key") == operation.id
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        db.expunge(environment)
    lease = OperationLease(operation.id, 1, "owner-key", 60)
    revoked: list[str] = []

    class Service:
        async def revoke_key(self, key_id, **_kwargs):
            revoked.append(key_id)

    class Runtime:
        async def close(self):
            return None

    monkeypatch.setattr(RemoteProvisioner, "_gateway_service", lambda *_args: Service())
    monkeypatch.setattr("app.runtime.remote_provisioning.RedisRuntimeStore", lambda _url: Runtime())
    async def healthy(_self):
        return EnvironmentStatus.AVAILABLE

    monkeypatch.setattr(RemoteProvisioner, "_probe_after_failure", healthy)
    worker = RemoteProvisioner(environment, operation, lease=lease)
    worker.new_key_id = "new-key"
    worker.old_key_id = "old-key"
    worker.key_applied = True
    await lease.mark_danger()
    await worker._finish(False, "install failed")
    assert revoked == []




@pytest.mark.asyncio
async def test_late_maintenance_submit_blocks_waiting_task_start():
    environment_id, employee_id = await make_environment()
    async with SessionLocal() as db:
        task = Task(
            id="task-" + uuid4().hex,
            title="locks",
            task_type="ISSUE",
            category_id="testing-platform",
            employee_id=employee_id,
            status="PENDING",
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    # Hold the environment row lock and insert the maintenance operation in the
    # same uncommitted transaction, mimicking a submit that is about to commit.
    holder = SessionLocal()
    environment = await holder.get(OpenCodeExecutionEnvironment, environment_id)
    from app.runtime.maintenance import lock_environment_row

    await lock_environment_row(holder, environment)
    holder.add(
        WorkerMaintenanceOperation(
            id="op_" + uuid4().hex,
            environment_id=environment_id,
            action="restart",
            state="QUEUED",
            stage="queued",
            requested_by="race",
            attempt=1,
            target_revision={},
            before_snapshot={},
            after_snapshot={},
            active_environment_id=environment_id,
            requested_at=datetime.now(UTC),
        )
    )
    await holder.flush()

    async def start_task():
        async with SessionLocal() as db:
            try:
                await RuntimeService(db).start(task_id)
            except RuntimeStateError as exc:
                return str(exc)
            return None

    starter = asyncio.create_task(start_task())
    await asyncio.sleep(0.1)
    await holder.commit()
    await holder.close()
    result = await asyncio.wait_for(starter, timeout=10)
    # The waiting Task start acquires the row lock only after the maintenance
    # fact commits, so it must see the operation and refuse.
    assert result is not None
    assert "维护" in result


@pytest.mark.asyncio
async def test_expired_lease_cannot_renew_stage_danger_or_finish():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    assert await claim_next("owner-expired") == operation.id
    lease = OperationLease(operation.id, 1, "owner-expired", 60)
    await expire_lease(operation.id)

    assert await lease.renew() is False
    with pytest.raises(MaintenanceLeaseLost):
        await lease.set_stage("danger")
    with pytest.raises(MaintenanceLeaseLost):
        await lease.mark_danger()
    assert (
        await lease.finish("SUCCEEDED", stage="verify", after_snapshot={"stale": True})
        is False
    )
    current = await read_operation(operation.id)
    assert current.state == "PREFLIGHT"
    await recover_expired("other-owner")
    assert (await read_operation(operation.id)).state == "INTERRUPTED"


@pytest.mark.asyncio
async def test_stale_attempt_event_is_rejected_after_retry():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    assert await claim_next("owner-stale") == operation.id
    stale = OperationLease(operation.id, 1, "owner-stale", 60)
    await expire_lease(operation.id)
    await recover_expired("other")
    async with SessionLocal() as db:
        await retry_operation(db, operation.id)
    assert await claim_next("owner-new") == operation.id
    with pytest.raises(MaintenanceLeaseLost):
        await stale.event(event_type="step", name="stale", message="old")
    current = await read_operation(operation.id)
    assert current.attempt == 2
    await finish_operation(operation.id, "owner-new", 2, "CANCELED")


@pytest.mark.asyncio
async def test_cancel_after_retry_keeps_lock_until_reconcile():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    assert await claim_next("owner-unknown") == operation.id
    async with SessionLocal() as db:
        await db.execute(
            update(WorkerMaintenanceOperation)
            .where(WorkerMaintenanceOperation.id == operation.id)
            .values(danger_dispatched=True)
        )
        await db.commit()
    await expire_lease(operation.id)
    await recover_expired("other")
    async with SessionLocal() as db:
        interrupted = await db.get(WorkerMaintenanceOperation, operation.id)
        with pytest.raises(HTTPException):
            await cancel_operation(db, operation.id)
        assert interrupted.state == "INTERRUPTED"

    async with SessionLocal() as db:
        await retry_operation(db, operation.id)
    async with SessionLocal() as db:
        cancelled = await cancel_operation(db, operation.id)
    assert cancelled.state == "INTERRUPTED"
    assert cancelled.active_environment_id == environment_id
    assert cancelled.error_code == "cancel_requires_reconcile"


@pytest.mark.asyncio
async def test_reconcile_checks_remote_facts_before_release(monkeypatch):
    environment_id, _ = await make_environment()
    async with SessionLocal() as db:
        db.add(
            WorkerInstance(
                id="wrk_reconcile",
                environment_id=environment_id,
                token_digest=gateway_key_digest("wtk_reconcile"),
                status="online",
                epoch=1,
            )
        )
        await db.commit()
    operation = await make_operation(environment_id)
    assert await claim_next("owner-danger") == operation.id
    async with SessionLocal() as db:
        await db.execute(
            update(WorkerMaintenanceOperation)
            .where(WorkerMaintenanceOperation.id == operation.id)
            .values(danger_dispatched=True)
        )
        await db.commit()
    await expire_lease(operation.id)
    await recover_expired("other")
    async with SessionLocal() as db:
        await retry_operation(db, operation.id, reconcile_only=True)

    async def free_locks(*_args):
        return True

    async def health(_self):
        return {"healthy": True, "version": "1.18.35"}

    async def status(_self, *_args, **_kwargs):
        return {"ses_idle": {"type": "idle"}}

    monkeypatch.setattr("app.runtime.maintenance._installer_locks_free", free_locks)
    monkeypatch.setattr(
        "app.runtime.worker_client.WorkerOpenCodeClient.health", health
    )
    monkeypatch.setattr(
        "app.runtime.worker_client.WorkerOpenCodeClient.request", status
    )
    await run_maintenance_once("owner-reconcile")
    current = await read_operation(operation.id)
    assert current.state == "FAILED"
    assert current.error_code == "reconciled"
    assert current.active_environment_id is None
    assert current.reconcile_required is False
    assert current.after_snapshot["installer_lock"] == "free"
    assert current.after_snapshot["health"] == "ok"
    assert current.after_snapshot["native"] == "idle"
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        assert environment.status == "AVAILABLE"


@pytest.mark.asyncio
async def test_event_stream_drains_more_than_one_page_and_ends_interrupted():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    async with SessionLocal() as db:
        for index in range(250):
            await append_event(db, operation.id, 1, name=f"e{index}", message=str(index))
        await db.commit()
    collected = []
    async for event in event_stream(operation.id, 0):
        collected.append(event)
        if len(collected) == 250:
            break
    assert len(collected) == 250
    assert collected[0]["id"] < collected[-1]["id"]

    async with SessionLocal() as db:
        await cancel_operation(db, operation.id)
    terminal = [event async for event in event_stream(operation.id, 0)]
    assert terminal[-1]["type"] == "canceled"

    # INTERRUPTED already ended execution; its stream must finish as well.
    environment_id, _ = await make_environment()
    interrupted = await make_operation(environment_id)
    assert await claim_next("owner-sse") == interrupted.id
    async with SessionLocal() as db:
        await db.execute(
            update(WorkerMaintenanceOperation)
            .where(WorkerMaintenanceOperation.id == interrupted.id)
            .values(
                state="INTERRUPTED",
                lease_owner=None,
                lease_until=None,
                active_environment_id=environment_id,
            )
        )
        await db.commit()
    assert [event async for event in event_stream(interrupted.id, 0)] == []


@pytest.mark.asyncio
async def test_failed_channel_switch_keeps_interrupted_lock(monkeypatch):
    environment_id, _ = await make_environment()
    async with SessionLocal() as db:
        with pytest.raises(HTTPException):
            await mark_worker_channel(
                db,
                environment_id,
                switch=_raising_switch,
            )
    async with SessionLocal() as db:
        operation = await db.scalar(
            select(WorkerMaintenanceOperation)
            .where(WorkerMaintenanceOperation.environment_id == environment_id)
            .order_by(WorkerMaintenanceOperation.requested_at.desc())
        )
    assert operation.state == "INTERRUPTED"
    assert operation.active_environment_id == environment_id
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        assert environment.worker_channel == "ssh"


async def _raising_switch():
    raise RuntimeError("reload timeout")


@pytest.mark.asyncio
async def test_executor_error_after_danger_is_interrupted(monkeypatch):
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)

    async def fake_run(self, emit):
        await self.lease.mark_danger()
        raise RuntimeError("unexpected after dispatch")

    monkeypatch.setattr(RemoteProvisioner, "run", fake_run)
    await run_maintenance_once("owner-generic-danger")
    current = await read_operation(operation.id)
    assert current.state == "INTERRUPTED"
    assert current.active_environment_id == environment_id
    assert current.error_code == "remote_state_unknown"


@pytest.mark.asyncio
async def test_disable_only_pauses_dispatch_without_stopping_work():
    environment_id, employee_id = await make_environment()
    async with SessionLocal() as db:
        task = Task(
            id="task-" + uuid4().hex,
            title="running",
            task_type="ISSUE",
            category_id="testing-platform",
            employee_id=employee_id,
            status="RUNNING",
            current_session_id="ses_running",
        )
        db.add(task)
        await db.commit()
    operation = await make_operation(environment_id, action="disable")
    await run_maintenance_once("owner-disable")
    current = await read_operation(operation.id)
    assert current.state == "SUCCEEDED"
    async with SessionLocal() as db:
        employee = await db.get(DigitalEmployee, employee_id)
        assert employee.enabled is False
        stored = await db.get(Task, task.id)
        assert stored.status == "RUNNING"
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        assert environment.status == "AVAILABLE"


@pytest.mark.asyncio
async def test_restore_requires_real_health_then_enables(monkeypatch):
    environment_id, employee_id = await make_environment()
    async with SessionLocal() as db:
        employee = await db.get(DigitalEmployee, employee_id)
        employee.enabled = False
        await db.commit()
    operation = await make_operation(environment_id, action="restore")
    await run_maintenance_once("owner-restore")
    current = await read_operation(operation.id)
    assert current.state == "SUCCEEDED"
    async with SessionLocal() as db:
        employee = await db.get(DigitalEmployee, employee_id)
        assert employee.enabled is True


@pytest.mark.asyncio
async def test_restore_keeps_disabled_when_health_fails(monkeypatch):
    environment_id, employee_id = await make_environment()
    async with SessionLocal() as db:
        employee = await db.get(DigitalEmployee, employee_id)
        employee.enabled = False
        await db.commit()

    async def unhealthy(environment_id, *, persist=False):
        now = datetime.now(UTC)
        return {
            "stale": False,
            "status": EnvironmentStatus.UNAVAILABLE,
            "desired": {},
            "observation": {"error": "openCode down"},
            "equipment": [],
            "repository": None,
            "checked_at": now,
            "values": {
                "status": EnvironmentStatus.UNAVAILABLE,
                "last_checked_at": now,
                "status_detail": {},
            },
        }

    monkeypatch.setattr("app.runtime.maintenance.observe_environment", unhealthy)
    operation = await make_operation(environment_id, action="restore")
    await run_maintenance_once("owner-restore-fail")
    current = await read_operation(operation.id)
    assert current.state == "FAILED"
    assert current.error_code == "health_not_available"
    async with SessionLocal() as db:
        employee = await db.get(DigitalEmployee, employee_id)
        assert employee.enabled is False


@pytest.mark.asyncio
async def test_check_is_allowed_while_occupied_and_uses_real_probe():
    environment_id, employee_id = await make_environment()
    async with SessionLocal() as db:
        db.add(
            Task(
                id="task-" + uuid4().hex,
                title="busy",
                task_type="ISSUE",
                category_id="testing-platform",
                employee_id=employee_id,
                status="WAITING",
                pending_message_id="msg_busy",
            )
        )
        await db.commit()
    operation = await make_operation(environment_id, action="check")
    await run_maintenance_once("owner-check")
    current = await read_operation(operation.id)
    assert current.state == "SUCCEEDED"
    assert current.action == "check"


@pytest.mark.asyncio
async def test_event_page_returns_cursor_items():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    async with SessionLocal() as db:
        await append_event(db, operation.id, 1, name="a", message="1")
        await append_event(db, operation.id, 1, name="b", message="2")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/maintenance/{operation.id}/events/page",
            params={"after_id": 0, "limit": 1},
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["next_cursor"] == body["items"][0]["id"]
    async with SessionLocal() as db:
        await cancel_operation(db, operation.id)


@pytest.mark.asyncio
async def test_health_json_contract_and_stale_blocking():
    environment_id, employee_id = await make_environment(
        status="AVAILABLE", last_checked_at=datetime.now(UTC)
    )
    async with SessionLocal() as db:
        health = await environment_health(db, environment_id)
    for key in {
        "environment_id",
        "employee_id",
        "enabled",
        "connection",
        "maintenance",
        "runtime_health",
        "repository",
        "schedulable",
        "blocking",
    }:
        assert key in health
    assert isinstance(health["blocking"], list)

    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        environment.last_checked_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()
        stale = await environment_health(db, environment_id)
    assert stale["runtime_health"]["status"] == "UNKNOWN"
    assert any(item["code"] == "health_stale" for item in stale["blocking"])


@pytest.mark.asyncio
async def test_stale_health_blocks_manual_task_start():
    environment_id, employee_id = await make_environment(
        status="AVAILABLE",
        last_checked_at=datetime.now(UTC) - timedelta(hours=1),
        status_detail={
            "observed_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "observed_epoch": 1,
            "observed_online": True,
        },
    )
    async with SessionLocal() as db:
        db.add(
            WorkerInstance(
                id="wrk_stale",
                environment_id=environment_id,
                token_digest=gateway_key_digest("wtk_stale"),
                status="online",
                epoch=1,
            )
        )
        await db.commit()
    async with SessionLocal() as db:
        task = Task(
            id="task-" + uuid4().hex,
            title="stale",
            task_type="ISSUE",
            category_id="testing-platform",
            employee_id=employee_id,
            status="PENDING",
        )
        db.add(task)
        await db.commit()
        with pytest.raises(RuntimeStateError, match="健康观测"):
            await RuntimeService(db).start(task.id)


def test_agent_wheel_is_reproducible_and_named_for_nexus():
    from app.runtime.remote_provisioning import agent_wheel

    first_name, first = agent_wheel()
    second_name, second = agent_wheel()
    assert first_name == second_name == "nexus_worker_agent-1.4.5-py3-none-any.whl"
    assert first == second


@pytest.mark.asyncio
async def test_update_requires_bound_repository():
    environment_id, _ = await make_environment()
    with pytest.raises(HTTPException) as error:
        await make_operation(environment_id, action="update")
    assert error.value.status_code == 409
    assert "仓库" in str(error.value.detail)


@pytest.mark.asyncio
async def test_observe_does_not_persist_stale_worker_epoch(monkeypatch):
    from app.domain import EnvironmentStatus as Status
    from app.runtime import worker_health

    now = datetime.now(UTC)
    environment_id, _ = await make_environment(
        status="AVAILABLE",
        status_detail={"observed_at": now.isoformat(), "observed_epoch": 1},
    )
    async with SessionLocal() as db:
        db.add(
            WorkerInstance(
                id="wrk_epoch",
                environment_id=environment_id,
                token_digest=gateway_key_digest("wtk_epoch"),
                status="online",
                epoch=1,
            )
        )
        await db.commit()

    async def probe(environment, desired, settings, **_kwargs):
        async with SessionLocal() as db:
            await db.execute(
                update(WorkerInstance)
                .where(WorkerInstance.environment_id == environment_id)
                .values(epoch=2)
            )
            await db.commit()
        return {"source": "worker", "observed_epoch": 1}

    monkeypatch.setattr(worker_health, "probe_equipment", probe)
    monkeypatch.setattr(worker_health, "health_status", lambda *_args: Status.AVAILABLE)
    result = await observe_environment(environment_id, persist=True)
    assert result["stale"] is True
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        assert environment.status_detail.get("observed_epoch") != 2
        assert "observed_online" not in environment.status_detail


@pytest.mark.asyncio
async def test_observe_does_not_overwrite_new_configuration_or_maintenance(monkeypatch):
    from app.domain import EnvironmentStatus as Status
    from app.runtime import worker_health

    now = datetime.now(UTC)
    environment_id, employee_id = await make_environment(
        status="AVAILABLE",
        status_detail={"observed_at": now.isoformat(), "observed_epoch": 1},
    )
    async with SessionLocal() as db:
        db.add(
            WorkerInstance(
                id="wrk_cas",
                environment_id=environment_id,
                token_digest=gateway_key_digest("wtk_cas"),
                status="online",
                epoch=1,
            )
        )
        await db.commit()

    async def changed_config(environment, desired, settings, **_kwargs):
        async with SessionLocal() as db:
            row = await db.get(OpenCodeExecutionEnvironment, environment_id)
            row.status_detail = {"changed": True}
            await db.commit()
        return {"source": "worker"}

    monkeypatch.setattr(worker_health, "probe_equipment", changed_config)
    monkeypatch.setattr(worker_health, "health_status", lambda *_args: Status.AVAILABLE)
    result = await observe_environment(environment_id, persist=True)
    assert result["stale"] is True
    async with SessionLocal() as db:
        row = await db.get(OpenCodeExecutionEnvironment, environment_id)
        assert row.status_detail == {"changed": True}

    async def maintenance_started(environment, desired, settings, **_kwargs):
        async with SessionLocal() as db:
            db.add(
                WorkerMaintenanceOperation(
                    id="op_race_" + uuid4().hex,
                    environment_id=environment_id,
                    action="check",
                    state="RUNNING",
                    stage="checking",
                    requested_by="race",
                    attempt=1,
                    lease_owner="race",
                    lease_until=datetime.now(UTC) + timedelta(minutes=1),
                    target_revision={},
                    before_snapshot={},
                    after_snapshot={},
                    active_environment_id=environment_id,
                    requested_at=datetime.now(UTC),
                )
            )
            await db.commit()
        return {"source": "worker"}

    monkeypatch.setattr(worker_health, "probe_equipment", maintenance_started)
    result = await observe_environment(environment_id, persist=True)
    assert result["stale"] is True


@pytest.mark.asyncio
async def test_remote_channel_ssh_still_requires_online_worker_for_health():
    now = datetime.now(UTC)
    environment_id, _ = await make_environment(
        status="AVAILABLE",
        worker_channel="ssh",
        last_checked_at=now,
        status_detail={
            "observed_at": now.isoformat(),
            "observed_epoch": 1,
            "observed_online": False,
        },
    )
    async with SessionLocal() as db:
        health = await environment_health(db, environment_id)
    assert health["runtime_health"]["status"] == "UNKNOWN"
    assert health["schedulable"] is False
    assert any(item["code"] == "worker_offline" for item in health["blocking"])


@pytest.mark.asyncio
async def test_check_does_not_promote_applied_revision(monkeypatch):
    from app.domain import EnvironmentStatus as Status
    from app.runtime import worker_health

    now = datetime.now(UTC)
    environment_id, _ = await make_environment(
        status="AVAILABLE",
        status_detail={
            "capability": {
                "desired_revision": "old",
                "applied_revision": "old",
                "runtime_status": "applied",
            },
            "observed_at": now.isoformat(),
            "observed_epoch": 1,
        },
    )
    async with SessionLocal() as db:
        db.add(
            WorkerInstance(
                id="wrk_applied",
                environment_id=environment_id,
                token_digest=gateway_key_digest("wtk_applied"),
                status="online",
                epoch=1,
            )
        )
        await db.commit()

    async def probe(_environment, _desired, _settings, **_kwargs):
        return {"source": "worker"}

    monkeypatch.setattr(worker_health, "probe_equipment", probe)
    monkeypatch.setattr(worker_health, "health_status", lambda *_args: Status.AVAILABLE)
    result = await observe_environment(environment_id, persist=False)
    capability = result["values"]["status_detail"]["capability"]
    assert capability["applied_revision"] == "old"
    assert result["values"]["status_detail"]["equipment"]


@pytest.mark.asyncio
async def test_retry_preserves_previous_attempt_snapshot_in_event_history(monkeypatch):
    from app.models import WorkerMaintenanceEvent

    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    assert await claim_next("owner-history") == operation.id
    async with SessionLocal() as db:
        await db.execute(
            update(WorkerMaintenanceOperation)
            .where(WorkerMaintenanceOperation.id == operation.id)
            .values(
                after_snapshot={"equipment": [{"key": "opencode"}]},
                error_code="remote_state_unknown",
                error_message="old attempt",
            )
        )
        await db.commit()
    await expire_lease(operation.id)
    await recover_expired("other")
    async with SessionLocal() as db:
        await retry_operation(db, operation.id)
        events = list(
            (
                await db.scalars(
                    select(WorkerMaintenanceEvent).where(
                        WorkerMaintenanceEvent.operation_id == operation.id,
                        WorkerMaintenanceEvent.type == "attempt",
                    )
                )
            ).all()
        )
    assert len(events) == 1
    assert events[0].attempt == 1
    assert events[0].detail["after_snapshot"] == {"equipment": [{"key": "opencode"}]}
    assert events[0].detail["error_code"] == "lease_expired"
    assert events[0].detail["target_revision"]
    assert events[0].detail["before_snapshot"]
    async with SessionLocal() as db:
        await cancel_operation(db, operation.id)


@pytest.mark.asyncio
async def test_terminal_result_event_is_written_with_the_terminal_state(monkeypatch):
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    assert await claim_next("owner-result") == operation.id
    await finish_operation(
        operation.id,
        "owner-result",
        1,
        "SUCCEEDED",
        stage="verify",
        after_snapshot={"checks": [{"name": "x", "passed": True}]},
        event_message="维护完成",
    )
    events = [event async for event in event_stream(operation.id, 0)]
    assert events[-1]["type"] == "result"
    assert events[-1]["detail"]["success"] is True


@pytest.mark.asyncio
async def test_only_successful_deploy_marks_applied_revision(monkeypatch):
    from app.domain import EnvironmentStatus as Status
    from app.runtime import worker_health

    now = datetime.now(UTC)
    environment_id, _ = await make_environment(
        status="AVAILABLE",
        status_detail={
            "observed_at": now.isoformat(),
            "observed_epoch": 1,
            "observed_online": True,
        },
    )
    async with SessionLocal() as db:
        db.add(
            WorkerInstance(
                id="wrk_deploy_rev",
                environment_id=environment_id,
                token_digest=gateway_key_digest("wtk_deploy_rev"),
                status="online",
                epoch=1,
            )
        )
        await db.commit()

    async def probe(_environment, _desired, _settings, **_kwargs):
        return {"source": "worker"}

    monkeypatch.setattr(worker_health, "probe_equipment", probe)
    monkeypatch.setattr(worker_health, "health_status", lambda *_args: Status.AVAILABLE)
    checked = await observe_environment(environment_id, persist=False)
    desired = checked["desired"]["revision"]
    assert checked["values"]["status_detail"]["capability"]["applied_revision"] != desired
    deployed = await observe_environment(
        environment_id,
        persist=False,
        apply_desired=True,
        mark_deployed=True,
    )
    assert deployed["values"]["status_detail"]["capability"]["applied_revision"] == desired
    assert deployed["values"]["status_detail"]["deployed"]["opencode"]["version"]


@pytest.mark.asyncio
async def test_observe_persists_only_with_current_online_epoch(monkeypatch):
    from app.domain import EnvironmentStatus as Status
    from app.runtime import worker_health

    environment_id, _ = await make_environment(status="UNKNOWN")
    async with SessionLocal() as db:
        db.add(
            WorkerInstance(
                id="wrk_persist",
                environment_id=environment_id,
                token_digest=gateway_key_digest("wtk_persist"),
                status="online",
                epoch=4,
            )
        )
        await db.commit()

    async def probe(_environment, _desired, _settings, **_kwargs):
        return {"source": "worker"}

    monkeypatch.setattr(worker_health, "probe_equipment", probe)
    monkeypatch.setattr(worker_health, "health_status", lambda *_args: Status.AVAILABLE)
    result = await observe_environment(environment_id, persist=True)
    assert result["stale"] is False
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        assert environment.status == "AVAILABLE"
        assert environment.status_detail["observed_epoch"] == 4
        assert environment.status_detail["observed_online"] is True


@pytest.mark.asyncio
async def test_health_and_scheduling_share_pending_equipment_rules():
    from app.runtime.maintenance import scheduling_block

    now = datetime.now(UTC)
    pending_detail = {
        "observed_at": now.isoformat(),
        "observed_epoch": 1,
        "observed_online": True,
        "capability": {
            "desired_revision": "new",
            "applied_revision": "old",
            "runtime_status": "pending",
        },
        "equipment": [
            {
                "key": "skills",
                "label": "Skill",
                "configured": True,
                "deployed": False,
                "verified": False,
                "diff": ["内容不一致"],
            }
        ],
    }
    environment_id, _ = await make_environment(
        status="AVAILABLE", status_detail=pending_detail
    )
    async with SessionLocal() as db:
        db.add(
            WorkerInstance(
                id="wrk_pending",
                environment_id=environment_id,
                token_digest=gateway_key_digest("wtk_pending"),
                status="online",
                epoch=1,
            )
        )
        await db.commit()
    async with SessionLocal() as db:
        health = await environment_health(db, environment_id)
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        message = await scheduling_block(db, environment)
        assert health["schedulable"] is False
        codes = {item["code"] for item in health["blocking"]}
        assert "equipment_pending" in codes
        assert message is not None
        assert "装备" in message


@pytest.mark.asyncio
async def test_checking_status_is_never_schedulable_and_matches_scheduling_block():
    from app.runtime.maintenance import scheduling_block

    environment_id, _ = await make_environment(status="CHECKING")
    async with SessionLocal() as db:
        health = await environment_health(db, environment_id)
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        message = await scheduling_block(db, environment)
    assert health["schedulable"] is False
    assert any(item["code"] == "runtime_checking" for item in health["blocking"])
    assert message in {item["message"] for item in health["blocking"]}


@pytest.mark.asyncio
async def test_install_units_skips_unnecessary_restarts_when_unchanged(monkeypatch):
    import hashlib

    from app.runtime.remote_provisioning import agent_wheel

    environment_id, _ = await make_environment(workspace_root="/srv/project")
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        db.expunge(environment)
    provisioner = RemoteProvisioner(environment, {"id": "op-idem", "action": "deploy"})
    provisioner.home = "/root"
    provisioner.config_path = f"/etc/nexus/opencode/{environment_id}/opencode.jsonc"
    provisioner.target_binary = (
        f"/root/.local/share/nexus/workers/{environment_id}/bin/opencode"
    )
    provisioner.installer_meta = {
        "changed": {
            "runtime": False,
            "configuration": False,
            "skills": False,
            "repository": False,
        }
    }
    wheel_digest = hashlib.sha256(agent_wheel()[1]).hexdigest()
    calls: list[str] = []

    async def command(text, timeout=30):
        calls.append(text)
        if "wheel.sha256" in text and text.lstrip().startswith("cat "):
            return wheel_digest
        if "test -x" in text and text.rstrip().endswith("&& echo yes || true"):
            return "yes"
        return ""

    async def unchanged(*_args, **_kwargs):
        return False

    async def safe(*_args, **_kwargs):
        return None

    async def writable(**_kwargs):
        return True

    async def credentials(_path):
        return "wtk_existing", "wrk_existing"

    monkeypatch.setattr(provisioner, "_command", command)
    monkeypatch.setattr(provisioner, "_write_if_changed", unchanged)
    monkeypatch.setattr(provisioner, "_ensure_safe", safe)
    monkeypatch.setattr(provisioner, "_native_idle", writable)
    monkeypatch.setattr(provisioner, "_existing_worker_credentials", credentials)

    async def emit(_event):
        return None

    await provisioner._install_units(emit)
    assert not any("pip install" in item for item in calls)
    systemd = next(item for item in calls if "systemctl enable" in item)
    assert "daemon-reload" not in systemd
    assert f"systemctl start nexus-opencode-{environment_id}.service" in systemd
    assert f"systemctl start nexus-worker-{environment_id}.service" in systemd
    assert "restart nexus-opencode" not in systemd
    assert "restart nexus-worker" not in systemd


@pytest.mark.asyncio
async def test_target_revision_uses_worker_agent_version_source():
    from app.runtime.maintenance import _target_revision
    from app.worker_agent import __version__

    environment_id, _ = await make_environment()
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        revision = _target_revision(environment)
    assert revision["worker_agent_version"] == __version__ == "1.4.5"


@pytest.mark.asyncio
async def test_provisioner_steps_advance_operation_state_and_stage_atomically():
    from app.models import WorkerMaintenanceEvent
    from app.runtime.maintenance import _emit_event

    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    assert await claim_next("owner-steps") == operation.id
    lease = OperationLease(operation.id, 1, "owner-steps", 60)

    stage = "preflight"
    for name, expected_state, expected_stage in [
        ("SSH 连接与执行检查", "PREFLIGHT", "preflight"),
        ("运行状态检查", "PREFLIGHT", "preflight"),
        ("系统依赖", "RUNNING", "system"),
        ("Node.js 与 npm", "RUNNING", "runtime"),
        ("Git 工作树", "RUNNING", "repository"),
        ("OpenCode 安装", "RUNNING", "opencode"),
        ("写入前状态检查", "RUNNING", "write_check"),
        ("工程目录", "RUNNING", "repository"),
        ("全局 OpenCode 配置", "RUNNING", "configure"),
        ("安装 Worker Agent", "RUNNING", "install"),
        ("切换到 Worker", "RUNNING", "channel"),
        ("重启 OpenCode Serve", "RUNNING", "restart"),
        ("模型中转连接", "VERIFYING", "verify"),
        ("图书馆 MCP", "VERIFYING", "verify"),
    ]:
        stage = await _emit_event(
            lease,
            {"type": "step", "name": name, "state": "running", "detail": f"{name} 开始"},
            stage,
        )
        current = await read_operation(operation.id)
        assert current.state == expected_state, name
        assert current.stage == expected_stage, name
        async with SessionLocal() as db:
            event = await db.scalar(
                select(WorkerMaintenanceEvent)
                .where(WorkerMaintenanceEvent.operation_id == operation.id)
                .order_by(WorkerMaintenanceEvent.id.desc())
            )
        assert event.stage == expected_stage

    # A late preflight-looking event must not downgrade RUNNING/VERIFYING.
    stage = await _emit_event(
        lease,
        {"type": "step", "name": "SSH 连接与执行检查", "state": "running"},
        stage,
    )
    current = await read_operation(operation.id)
    assert current.state == "VERIFYING"
    assert current.stage == "verify"
    await finish_operation(operation.id, "owner-steps", 1, "FAILED")

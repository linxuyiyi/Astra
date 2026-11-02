"""Worker 5 态无锁维护执行器、离线快速唤醒、就绪投影与列表/配置 API 契约测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

import app.gateway.models  # noqa: F401 - 注册 GatewayRoute/GatewayKey 表
from app.config.settings import get_settings
from app.database import SessionLocal, init_database
from app.gateway.models import GatewayKey, GatewayRoute
from app.gateway.security import CredentialCipher, gateway_key_digest
from app.main import app
from app.models import (
    AgentWorker,
    CodeRepository,
    OpenCodeExecutionEnvironment,
    Task,
    TaskCategory,
    WorkerInstance,
    WorkerMaintenanceOperation,
)
from app.runtime.dispatcher import Dispatcher
from app.runtime.environment import ExecutionEnvironmentRuntime
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.runtime.maintenance import (
    OperationLease,
    append_event,
    cancel_operation,
    claim_next,
    environment_health,
    event_stream,
    finish_operation,
    interrupt_owned,
    list_operations,
    recover_expired,
    retry_operation,
    run_maintenance_once,
    submit_operation,
)
from app.runtime.remote_provisioning import RemoteProvisioner, agent_wheel, worker_ctl_path
from app.runtime.worker_readiness import build_desired_spec, compute_target_digest
from app.worker_agent import __version__ as WORKER_AGENT_VERSION

ACTOR_HEADERS = {"X-Owner-Id": "admin-test"}


@pytest.fixture(autouse=True)
def maintenance_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mcp_transport", "streamable-http")
    monkeypatch.setattr(settings, "gateway_openai_base", "http://10.0.0.8:8110")
    monkeypatch.setattr(settings, "mcp_resource_server_url", "http://10.0.0.8:8201/mcp")
    monkeypatch.setattr(
        settings, "worker_agent_gateway_url", "ws://10.0.0.8:8100/api/worker-gateway/connect"
    )


async def make_environment(
    *, with_repo: bool = True, employee_prefix: str = "maint", **overrides
) -> tuple[int, str]:
    await init_database()
    suffix = uuid4().hex[:10]
    employee_id = f"{employee_prefix}-{suffix}"
    repo_id = f"repo-{suffix}"
    route_id = f"route-{suffix}"
    async with SessionLocal() as db:
        db.add(
            AgentWorker(
                id=employee_id,
                name=employee_id,
                role="build",
                status="IDLE",
                enabled=True,
                auto_claim=True,
                auto_execute=True,
                current_task_count=0,
                task_types=["ISSUE"],
            )
        )
        if with_repo:
            db.add(
                CodeRepository(
                    id=repo_id,
                    url="https://example.test/org/repo.git",
                    branch="main",
                )
            )
        db.add(GatewayRoute(id=route_id, model=route_id, pool_id="pool-1", enabled=True))
        await db.flush()
        values = dict(
            employee_id=employee_id,
            repository_id=repo_id if with_repo else None,
            mode="LINUX_REMOTE",
            name="Linux Worker",
            host="10.0.0.9",
            port=22,
            username="root",
            ssh_password="ssh-secret-value",
            workspace_root="/srv/project",
            opencode_path="/usr/local/bin/opencode",
            opencode_server_port=4096,
            model_name=f"nexus-gateway/{route_id}",
            skill_refs=[],
            mcp_refs=[],
            status="AVAILABLE",
            status_detail={},
        )
        values.update(overrides)
        environment = OpenCodeExecutionEnvironment(**values)
        db.add(environment)
        await db.commit()
        return environment.id, employee_id


async def enroll_ready_worker(
    environment_id: int,
    *,
    online: bool = True,
    opencode_healthy: bool = True,
    last_error: str | None = None,
    epoch: int = 1,
) -> WorkerInstance:
    settings = get_settings()
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, environment_id)
        desired = await build_desired_spec(db, environment, settings)
        endpoints = {
            item["name"]: item["url"]
            for item in desired["mcp"]["items"]
            if isinstance(item, dict) and item.get("name")
        }
        skills = [
            item["name"]
            for item in desired["skills"]["items"]
            if isinstance(item, dict) and item.get("name")
        ]
        skill_hashes = {
            item["name"]: item["content_hash"]
            for item in desired["skills"]["items"]
            if isinstance(item, dict) and item.get("name") and item.get("content_hash")
        }
        repo = desired.get("repository") or {}
        snapshot = {
            "spec_revision": desired["revision"],
            "opencode": {
                "status": "healthy" if opencode_healthy else "unhealthy",
                "version": desired["opencode"]["version"],
                "error": None if opencode_healthy else (last_error or "OpenCode 进程异常"),
                "last_error": last_error,
            },
            "model": {
                "status": "healthy",
                "configured": True,
                "model": desired["model"]["name"],
                "error": None,
            },
            "mcp": {
                "status": "healthy",
                "connected": True,
                "missing": [],
                "servers": {name: {"status": "connected"} for name in endpoints},
                "endpoints": endpoints,
                "error": None,
            },
            "skills": {
                "status": "healthy",
                "discovered": skills,
                "missing": [],
                "content_hashes": skill_hashes,
                "error": None,
            },
            "repository": {
                "status": "healthy" if repo else "unknown",
                "url": repo.get("url"),
                "branch": repo.get("target_branch"),
                "commit": "a" * 40 if repo else None,
                "dirty": False,
                "credential_revision": repo.get("credential_revision"),
                "error": None if repo else "未绑定仓库",
            },
            "checked_at": now.isoformat(),
            "worker_agent_version": WORKER_AGENT_VERSION,
            "observed_epoch": epoch,
            "source": "worker",
            "error": None,
        }
        raw_token = "wtk_" + uuid4().hex
        existing = await db.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == environment_id)
        )
        digest = compute_target_digest(environment)
        if existing is None:
            cipher = CredentialCipher(settings.gateway_master_key)
            instance = WorkerInstance(
                id="wrk_" + uuid4().hex[:16],
                environment_id=environment_id,
                token_digest=gateway_key_digest(raw_token),
                token_ciphertext=cipher.encrypt(raw_token),
                status="online" if online else "offline",
                epoch=epoch,
                agent_version=WORKER_AGENT_VERSION,
                target_digest=digest,
                observed_snapshot=snapshot,
                observed_at=now,
                connected_at=now if online else None,
                last_heartbeat_at=now if online else None,
            )
            db.add(instance)
        else:
            existing.status = "online" if online else "offline"
            existing.epoch = epoch
            existing.agent_version = WORKER_AGENT_VERSION
            existing.target_digest = digest
            existing.observed_snapshot = snapshot
            existing.observed_at = now
            existing.last_heartbeat_at = now if online else existing.last_heartbeat_at
            instance = existing
        await db.commit()
        await db.refresh(instance)
        db.expunge(instance)
        return instance


async def make_operation(environment_id: int, action: str = "deploy") -> WorkerMaintenanceOperation:
    async with SessionLocal() as db:
        return await submit_operation(db, environment_id, action, requested_by="test")


async def read_operation(operation_id: str) -> WorkerMaintenanceOperation | None:
    async with SessionLocal() as db:
        operation = await db.get(WorkerMaintenanceOperation, operation_id)
        if operation is not None:
            db.expunge(operation)
        return operation


async def expire_lease(operation_id: str) -> None:
    async with SessionLocal() as db:
        await db.execute(
            update(WorkerMaintenanceOperation)
            .where(WorkerMaintenanceOperation.id == operation_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()


# ---------------------------------------------------------------------------
# 1. 5 态状态机、无锁超时回收、取消、重试与 SSE 恢复
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_action", ["disable", "restore", "switch_channel", "reconcile"])
async def test_legacy_maintenance_actions_are_rejected(legacy_action: str):
    environment_id, _ = await make_environment()
    with pytest.raises(HTTPException) as exc_info:
        await make_operation(environment_id, legacy_action)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_closing_event_stream_does_not_cancel_running_operation(monkeypatch):
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id, "deploy")
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(self, emit):
        await emit({"name": "测试阶段", "state": "running", "detail": "已开始"})
        started.set()
        await release.wait()
        await self._finish(True, "")

    monkeypatch.setattr(RemoteProvisioner, "run", fake_run)
    runner = asyncio.create_task(run_maintenance_once("test-owner"))
    await asyncio.wait_for(started.wait(), timeout=5)

    stream = event_stream(operation.id, 0)
    first = await anext(stream)
    assert first["type"] in {"queued", "started", "step"}
    await stream.aclose()

    still = await read_operation(operation.id)
    assert still is not None
    assert still.state == "RUNNING"
    assert still.lease_owner == "test-owner"

    release.set()
    await asyncio.wait_for(runner, timeout=5)
    final = await read_operation(operation.id)
    assert final is not None and final.state == "SUCCEEDED"
    assert final.active_environment_id is None


@pytest.mark.asyncio
async def test_sse_resume_uses_last_event_id_and_drains_terminal_events():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    async with SessionLocal() as db:
        first = await append_event(db, operation.id, 1, name="一", message="a")
        await append_event(db, operation.id, 1, name="二", message="b")
        await append_event(db, operation.id, 1, name="三", message="c")
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
            assert ids[:2] == [first.id + 1, first.id + 2]

        page1 = await client.get(
            f"/api/maintenance/{operation.id}/events/page", params={"limit": 2}
        )
        assert page1.status_code == 200
        body1 = page1.json()
        assert len(body1["items"]) == 2
        assert body1["next_cursor"] is not None

        page2 = await client.get(
            f"/api/maintenance/{operation.id}/events/page",
            params={"after": body1["next_cursor"], "limit": 10},
        )
        assert page2.status_code == 200
        assert page2.json()["next_cursor"] is None


@pytest.mark.asyncio
async def test_expired_lease_recovers_to_failed_and_releases_lock_immediately():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id)
    assert await claim_next("owner-a") == operation.id
    stale_lease = OperationLease(operation.id, 1, "owner-a", 60)
    await expire_lease(operation.id)

    assert await recover_expired() == 1
    recovered = await read_operation(operation.id)
    assert recovered is not None
    assert recovered.state == "FAILED"
    assert recovered.error_code == "lease_expired"
    assert recovered.active_environment_id is None
    assert recovered.lease_owner is None
    assert not await stale_lease.finish("SUCCEEDED", after_snapshot={"stale": True})
    assert not await stale_lease.renew()

    # 锁已自动释放，管理员既可直接重试原记录，也可直接提交新操作
    async with SessionLocal() as db:
        retried = await retry_operation(db, operation.id, actor="ops")
    assert retried.attempt == 2
    assert retried.retry_of_attempt == 1
    assert retried.state == "QUEUED"
    assert retried.active_environment_id == environment_id
    assert await claim_next("owner-b") == operation.id
    assert not await stale_lease.finish("SUCCEEDED", after_snapshot={"stale": True})
    current = await read_operation(operation.id)
    assert current is not None and current.attempt == 2 and current.state == "RUNNING"
    await finish_operation(operation.id, "owner-b", 2, "CANCELED")


@pytest.mark.asyncio
async def test_controller_shutdown_marks_owned_operations_failed_and_releases_lock():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id, "deploy")
    assert await claim_next("owner-stop") == operation.id
    assert await interrupt_owned("owner-stop", "控制面停机") == 1
    stopped = await read_operation(operation.id)
    assert stopped is not None
    assert stopped.state == "FAILED"
    assert stopped.error_code == "controller_stopped"
    assert stopped.active_environment_id is None


@pytest.mark.asyncio
async def test_cancel_during_running_aborts_and_releases_lock(monkeypatch):
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id, "deploy")
    started = asyncio.Event()

    async def fake_run(self, emit):
        started.set()
        while True:
            await self.lease.check_canceled()
            await asyncio.sleep(0.05)

    monkeypatch.setattr(RemoteProvisioner, "run", fake_run)
    runner = asyncio.create_task(run_maintenance_once("owner-cancel"))
    await asyncio.wait_for(started.wait(), timeout=5)

    async with SessionLocal() as db:
        pending = await cancel_operation(db, operation.id, actor="alice")
    assert pending.cancel_requested is True

    await asyncio.wait_for(runner, timeout=5)
    final = await read_operation(operation.id)
    assert final is not None
    assert final.state == "CANCELED"
    assert final.active_environment_id is None


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


@pytest.mark.asyncio
async def test_occupancy_blocks_disruptive_actions_but_allows_check(monkeypatch):
    environment_id, employee_id = await make_environment()
    instance = await enroll_ready_worker(environment_id, online=True)
    async with SessionLocal() as db:
        task = Task(
            id="task-" + uuid4().hex[:8],
            title="in-flight",
            task_type="ISSUE",
            employee_id=employee_id,
            status="WAITING_INPUT",
        )
        db.add(task)
        await db.commit()

    with pytest.raises(HTTPException) as blocked:
        await make_operation(environment_id, "restart")
    assert blocked.value.status_code == 409

    # check 为只读动作，占用期间允许提交并执行深度诊断
    captured_calls: list[dict] = []

    async def fake_probe(environment, desired, settings, *, worker_instance=None, deep=False):
        captured_calls.append({"deep": deep, "worker_id": worker_instance.id})
        return dict(instance.observed_snapshot)

    monkeypatch.setattr("app.runtime.maintenance.probe_online_worker", fake_probe)
    check_op = await make_operation(environment_id, "check")
    assert await run_maintenance_once("owner-check") is True
    finished = await read_operation(check_op.id)
    assert finished is not None
    assert finished.state == "SUCCEEDED"
    assert captured_calls == [{"deep": True, "worker_id": instance.id}]


@pytest.mark.asyncio
async def test_active_maintenance_blocks_manual_task_start():
    environment_id, employee_id = await make_environment()
    await enroll_ready_worker(environment_id, online=True)
    operation = await make_operation(environment_id, "deploy")
    async with SessionLocal() as db:
        task = Task(
            id="task-" + uuid4().hex[:8],
            title="start-guard",
            task_type="ISSUE",
            employee_id=employee_id,
            status="PENDING",
        )
        db.add(task)
        await db.commit()
        with pytest.raises(RuntimeStateError, match="维护"):
            await RuntimeService(db).start(task.id)
        await cancel_operation(db, operation.id)


# ---------------------------------------------------------------------------
# 2. restart 在线快路径 vs 离线轻量 SSH ctl.sh start 快速唤醒 & update 路由
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_fails_when_worker_never_deployed():
    environment_id, _ = await make_environment()
    operation = await make_operation(environment_id, "restart")
    assert await run_maintenance_once("owner-restart") is True
    finished = await read_operation(operation.id)
    assert finished is not None
    assert finished.state == "FAILED"
    assert finished.error_code == "not_deployed"
    assert finished.active_environment_id is None


@pytest.mark.asyncio
async def test_restart_online_worker_uses_gateway_without_ssh(monkeypatch):
    environment_id, _ = await make_environment()
    instance = await enroll_ready_worker(environment_id, online=True)
    commands: list[tuple[str, dict]] = []

    async def forbid_ssh(self, _env):
        raise AssertionError("在线 restart 不得打开 SSH")

    class FakeWorkerClient:
        def __init__(self, settings, *, environment_id):
            self.environment_id = environment_id

        async def run_command(self, name, data):
            commands.append((name, data))
            return {"restarted": True, "snapshot": dict(instance.observed_snapshot)}

    monkeypatch.setattr(ExecutionEnvironmentRuntime, "open_session", forbid_ssh)
    monkeypatch.setattr("app.runtime.maintenance.WorkerOpenCodeClient", FakeWorkerClient)

    operation = await make_operation(environment_id, "restart")
    assert await run_maintenance_once("owner-online-restart") is True
    finished = await read_operation(operation.id)
    assert finished is not None
    assert finished.state == "SUCCEEDED"
    assert finished.execution_channel == "worker"
    assert commands and commands[0][0] == "restart_opencode"


@pytest.mark.asyncio
async def test_restart_offline_worker_fails_fast_when_ctl_missing(monkeypatch):
    environment_id, _ = await make_environment()
    await enroll_ready_worker(environment_id, online=False)
    ssh_cmds: list[str] = []

    class FakeSSH:
        async def run_command(self, cmd: str):
            ssh_cmds.append(cmd)
            return 1, "", ""

        async def close(self):
            return None

    monkeypatch.setattr(
        ExecutionEnvironmentRuntime, "open_session", AsyncMock(return_value=FakeSSH())
    )
    operation = await make_operation(environment_id, "restart")
    assert await run_maintenance_once("owner-offline-missing") is True
    finished = await read_operation(operation.id)
    assert finished is not None
    assert finished.state == "FAILED"
    assert finished.error_code == "ctl_missing"
    assert finished.execution_channel == "ssh"
    assert any(worker_ctl_path(environment_id) in cmd for cmd in ssh_cmds)


@pytest.mark.asyncio
async def test_restart_offline_worker_wakes_daemon_via_ctl_start(monkeypatch):
    environment_id, _ = await make_environment()
    await enroll_ready_worker(environment_id, online=False)
    ctl = worker_ctl_path(environment_id)
    ssh_cmds: list[str] = []

    class FakeSSH:
        async def run_command(self, cmd: str):
            ssh_cmds.append(cmd)
            if cmd.endswith(" start"):
                await enroll_ready_worker(environment_id, online=True, opencode_healthy=True, epoch=2)
            return 0, "started", ""

        async def close(self):
            return None

    monkeypatch.setattr(
        ExecutionEnvironmentRuntime, "open_session", AsyncMock(return_value=FakeSSH())
    )
    operation = await make_operation(environment_id, "restart")
    assert await run_maintenance_once("owner-offline-wake") is True
    finished = await read_operation(operation.id)
    assert finished is not None
    assert finished.state == "SUCCEEDED"
    assert finished.execution_channel == "ssh"
    assert any(f"{ctl} start" in cmd or cmd.endswith("ctl.sh start") for cmd in ssh_cmds)


@pytest.mark.asyncio
async def test_restart_offline_worker_surfaces_opencode_last_error_when_unhealthy(monkeypatch):
    environment_id, _ = await make_environment()
    await enroll_ready_worker(environment_id, online=False)

    class FakeSSH:
        async def run_command(self, cmd: str):
            if cmd.endswith(" start"):
                await enroll_ready_worker(
                    environment_id,
                    online=True,
                    opencode_healthy=False,
                    last_error="Error: EADDRINUSE 127.0.0.1:4096",
                    epoch=2,
                )
            return 0, "started", ""

        async def close(self):
            return None

    monkeypatch.setattr(
        ExecutionEnvironmentRuntime, "open_session", AsyncMock(return_value=FakeSSH())
    )
    operation = await make_operation(environment_id, "restart")
    assert await run_maintenance_once("owner-offline-broken") is True
    finished = await read_operation(operation.id)
    assert finished is not None
    assert finished.state == "FAILED"
    assert finished.error_code == "opencode_unhealthy"
    assert "EADDRINUSE" in str(finished.error_message)


@pytest.mark.asyncio
async def test_update_requires_bound_repository_and_uses_worker_gateway(monkeypatch):
    unbound_env_id, _ = await make_environment(with_repo=False)
    with pytest.raises(HTTPException) as unbound_err:
        await make_operation(unbound_env_id, "update")
    assert unbound_err.value.status_code == 409

    env_id, _ = await make_environment(with_repo=True)
    instance = await enroll_ready_worker(env_id, online=True)

    class FakeWorkerClient:
        def __init__(self, settings, *, environment_id):
            self.environment_id = environment_id

        async def run_command(self, name, data):
            assert name == "update_workspace"
            assert data["branch"] == "main"
            return {"commit": "b" * 40, "snapshot": dict(instance.observed_snapshot)}

    monkeypatch.setattr("app.runtime.maintenance.WorkerOpenCodeClient", FakeWorkerClient)
    op = await make_operation(env_id, "update")
    assert await run_maintenance_once("owner-update") is True
    finished = await read_operation(op.id)
    assert finished is not None
    assert finished.state == "SUCCEEDED"
    assert finished.after_snapshot["commit"] == "b" * 40


# ---------------------------------------------------------------------------
# 3. 部署幂等复用凭据、密钥轮换保护与 Wheel 可复现性
# ---------------------------------------------------------------------------


def test_agent_wheel_is_reproducible_and_named_for_nexus():
    name1, bytes1 = agent_wheel()
    name2, bytes2 = agent_wheel()
    assert name1 == name2
    assert name1.startswith("nexus_worker_agent-")
    assert bytes1 == bytes2


@pytest.mark.asyncio
async def test_existing_worker_token_is_reused_when_target_digest_matches():
    environment_id, _ = await make_environment()
    settings = get_settings()
    raw_token = "wtk_reusable_" + uuid4().hex
    async with SessionLocal() as db:
        env = await db.get(OpenCodeExecutionEnvironment, environment_id)
        digest = compute_target_digest(env)
        cipher = CredentialCipher(settings.gateway_master_key)
        instance = WorkerInstance(
            id="wrk_" + uuid4().hex[:12],
            environment_id=environment_id,
            token_digest=gateway_key_digest(raw_token),
            token_ciphertext=cipher.encrypt(raw_token),
            status="online",
            target_digest=digest,
        )
        db.add(instance)
        await db.commit()
        db.expunge(env)
        instance_id = instance.id

    provisioner = RemoteProvisioner(env, {"id": "op-reuse", "action": "deploy"})
    env_bytes = (
        f"NEXUS_WORKER_TOKEN={raw_token}\n"
        f"NEXUS_WORKER_INSTANCE_ID={instance_id}\n"
        f"NEXUS_WORKER_ENVIRONMENT_ID={environment_id}\n"
    ).encode()
    provisioner.ssh = SimpleNamespace(read_file=AsyncMock(return_value=env_bytes))
    reused = await provisioner._existing_worker_credentials("/etc/worker.env", target_digest=digest)
    assert reused == (raw_token, instance_id)

    # 目标摘要变化时拒绝复用旧 token，强制重新 enroll
    mismatch = await provisioner._existing_worker_credentials(
        "/etc/worker.env", target_digest="different-digest"
    )
    assert mismatch is None


@pytest.mark.asyncio
async def test_failed_deploy_does_not_revoke_key_already_written_to_worker():
    environment_id, employee_id = await make_environment()
    operation = await make_operation(environment_id, "deploy")
    assert await claim_next("owner-key") == operation.id
    lease = OperationLease(operation.id, 1, "owner-key", 60)

    raw_key = "agw_" + uuid4().hex
    key_id = "key-" + uuid4().hex[:8]
    async with SessionLocal() as db:
        env = await db.get(OpenCodeExecutionEnvironment, environment_id)
        db.add(
            GatewayKey(
                id=key_id,
                name="worker-key",
                owner_id=employee_id,
                key_prefix=raw_key[:12],
                key_digest=gateway_key_digest(raw_key),
                enabled=True,
                allowed_models=[],
            )
        )
        await db.commit()
        db.expunge(env)

    provisioner = RemoteProvisioner(env, operation, lease=lease)
    provisioner.new_key_id = key_id
    provisioner.key_applied = True
    await provisioner._finish(False, "就绪验收超时")

    async with SessionLocal() as db:
        stored_key = await db.get(GatewayKey, key_id)
        assert stored_key is not None and stored_key.revoked_at is None


# ---------------------------------------------------------------------------
# 4. 列表内嵌 worker_health、PATCH enabled、PUT environment 与 Dispatcher 跳过异常节点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_workers_embeds_health_and_patch_enabled_plus_put_environment():
    env_id, employee_id = await make_environment(with_repo=True)
    await enroll_ready_worker(env_id, online=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1) GET /api/agent-workers 直接内嵌同源 worker_health
        list_resp = await client.get("/api/agent-workers")
        assert list_resp.status_code == 200
        item = next(row for row in list_resp.json() if row["id"] == employee_id)
        assert item["worker_health"]["readiness"] == "READY"
        assert item["worker_health"]["schedulable"] is True

        detail_health = await client.get(f"/api/agent-workers/{env_id}/health")
        assert detail_health.status_code == 200
        assert detail_health.json()["readiness"] == item["worker_health"]["readiness"]
        assert detail_health.json()["schedulable"] == item["worker_health"]["schedulable"]

        # 2) PATCH enabled=False 同步生效，不产生异步维护记录
        patch_off = await client.patch(
            f"/api/agent-workers/{employee_id}", json={"enabled": False}
        )
        assert patch_off.status_code == 200
        assert patch_off.json()["enabled"] is False
        assert patch_off.json()["worker_health"]["schedulable"] is False
        assert any(
            b["code"] == "disabled"
            for b in patch_off.json()["worker_health"]["blocking"]
        )
        async with SessionLocal() as db:
            ops = await list_operations(db, env_id)
            assert ops == []

        patch_on = await client.patch(
            f"/api/agent-workers/{employee_id}", json={"enabled": True}
        )
        assert patch_on.status_code == 200
        assert patch_on.json()["worker_health"]["schedulable"] is True

        # 3) 模拟员工有在途任务时，保存纯展示字段保持 READY；保存模型/装备变为 DRIFTED 且不中断在途任务
        async with SessionLocal() as db:
            emp = await db.get(AgentWorker, employee_id)
            emp.current_task_count = 1
            await db.commit()

        env_data = item["execution_environment"]
        display_only = await client.put(
            f"/api/agent-workers/{employee_id}/environment",
            json={
                "mode": "LINUX_REMOTE",
                "name": "Renamed Display Name",
                "description": "Updated note",
                "host": env_data["host"],
                "port": env_data["port"],
                "username": env_data["username"],
                "workspace_root": env_data["workspace_root"],
                "opencode_server_port": env_data["opencode_server_port"],
                "opencode_path": env_data["opencode_path"],
                "model_name": env_data["model_name"],
                "repository_id": env_data["repository_id"],
                "labels": ["canary"],
                "skill_refs": [],
                "mcp_refs": [],
            },
        )
        assert display_only.status_code == 200
        async with SessionLocal() as db:
            after_display = await environment_health(db, env_id)
            assert after_display["readiness"] == "READY"

        spec_drift = await client.put(
            f"/api/agent-workers/{employee_id}/environment",
            json={
                "mode": "LINUX_REMOTE",
                "name": "Renamed Display Name",
                "host": env_data["host"],
                "port": env_data["port"],
                "username": env_data["username"],
                "workspace_root": env_data["workspace_root"],
                "opencode_server_port": env_data["opencode_server_port"],
                "opencode_path": env_data["opencode_path"],
                "model_name": env_data["model_name"],
                "repository_id": env_data["repository_id"],
                "skill_refs": ["new-skill-pending-sync"],
                "mcp_refs": [],
            },
        )
        assert spec_drift.status_code == 200
        assert spec_drift.json()["status_detail"]["applies_to"] == "future_tasks"
        async with SessionLocal() as db:
            after_spec = await environment_health(db, env_id)
            assert after_spec["readiness"] == "DRIFTED"
            assert after_spec["schedulable"] is False

        # 4) 修改 host 改变 target_digest，立即判定 target_changed (DRIFTED)
        target_drift = await client.put(
            f"/api/agent-workers/{employee_id}/environment",
            json={
                "mode": "LINUX_REMOTE",
                "name": "Renamed Display Name",
                "host": "10.0.0.99",
                "port": env_data["port"],
                "username": env_data["username"],
                "workspace_root": env_data["workspace_root"],
                "opencode_server_port": env_data["opencode_server_port"],
                "opencode_path": env_data["opencode_path"],
                "model_name": env_data["model_name"],
                "repository_id": env_data["repository_id"],
                "skill_refs": [],
                "mcp_refs": [],
            },
        )
        assert target_drift.status_code == 200
        async with SessionLocal() as db:
            after_target = await environment_health(db, env_id)
            assert after_target["readiness"] == "DRIFTED"
            assert any(b["code"] == "target_changed" for b in after_target["blocking"])


async def test_local_check_uses_local_inspection_signature_and_finishes(monkeypatch):
    env_id, _ = await make_environment(with_repo=True)
    async with SessionLocal() as db:
        environment = await db.get(OpenCodeExecutionEnvironment, env_id)
        environment.mode = "LOCAL"
        await db.commit()

    calls = []

    def inspect_local(opencode_url, workspace_root, *, deep=False, **_expected):
        calls.append((opencode_url, workspace_root, deep))
        return {"opencode": {"status": "unhealthy", "error": "connection refused"}}

    monkeypatch.setattr("app.runtime.maintenance.inspect_runtime", inspect_local)
    operation = await make_operation(env_id, "check")
    await run_maintenance_once("owner-local-inspection")
    result = await read_operation(operation.id)
    assert result.state == "FAILED"
    assert result.error_code == "runtime_degraded"
    assert result.active_environment_id is None
    assert len(calls) == 1
    assert calls[0][0].startswith("http://127.0.0.1:")
    assert calls[0][2] is True


@pytest.mark.asyncio
async def test_dispatcher_skips_drifted_worker_and_assigns_healthy_colleague():
    cat_id = "cat-" + uuid4().hex[:8]
    # 按 AgentWorker.id 字典序排序，000-drifted 排在 zzz-ready 之前
    drifted_env_id, drifted_emp_id = await make_environment(
        with_repo=True, employee_prefix="000-drifted"
    )
    ready_env_id, ready_emp_id = await make_environment(
        with_repo=True, employee_prefix="zzz-ready"
    )

    async with SessionLocal() as db:
        drifted_env = await db.get(OpenCodeExecutionEnvironment, drifted_env_id)
        ready_env = await db.get(OpenCodeExecutionEnvironment, ready_env_id)
        ready_env.repository_id = drifted_env.repository_id
        shared_repo_id = drifted_env.repository_id
        await db.commit()

    await enroll_ready_worker(drifted_env_id, online=True)
    await enroll_ready_worker(ready_env_id, online=True)

    task_id = "T-disp-" + uuid4().hex[:8]
    async with SessionLocal() as db:
        db.add(TaskCategory(id=cat_id, name=cat_id, enabled=True))
        drifted_emp = await db.get(AgentWorker, drifted_emp_id)
        ready_emp = await db.get(AgentWorker, ready_emp_id)
        drifted_emp.category_ids = [cat_id]
        ready_emp.category_ids = [cat_id]
        drifted_env = await db.get(OpenCodeExecutionEnvironment, drifted_env_id)
        # 修改排第一节点的期望分支使其变为 DRIFTED
        drifted_env.branch = "feature/unapplied-branch"
        db.add(
            Task(
                id=task_id,
                title="dispatch-skip-drifted",
                task_type="ISSUE",
                category_id=cat_id,
                repository_id=shared_repo_id,
                status="PENDING",
            )
        )
        await db.commit()

    async with SessionLocal() as db:
        claimed = await Dispatcher(db).claim_once()
        assert claimed is not None
        assert claimed.id == task_id
        assert claimed.employee_id == ready_emp_id

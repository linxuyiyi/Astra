"""验证期望配置与运行观测物理隔离、单一就绪纯函数及网关心跳快照原子更新。"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import Settings
from app.domain import EmployeeStatus, TaskStatus, WorkerReadiness
from app.gateway.models import GatewayPool, GatewayRoute
from app.models import (
    AgentWorker,
    Base,
    CodeRepository,
    OpenCodeExecutionEnvironment,
    Task,
    WorkerInstance,
)
from app.runtime.dispatcher import Dispatcher
from app.runtime.worker_readiness import (
    build_desired_spec,
    compute_target_digest,
    evaluate_worker_readiness,
)
from app.worker_agent import __version__ as WORKER_AGENT_VERSION
from app.worker_agent.inspection import skill_content_hash
from app.worker_gateway.protocol import WorkerGatewayError, envelope
from app.worker_gateway.service import WorkerGateway


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _settings(tmp_path) -> Settings:
    skills_dir = tmp_path / "worker-skills" / "code-audit"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: code-audit\ndescription: audit\n---\n检查关键路径。\n",
        encoding="utf-8",
    )
    return Settings(
        workspace_root=str(tmp_path),
        remote_opencode_version="1.18.35",
        mcp_resource_server_url="http://nexus.internal:18000/mcp",
        gateway_master_key=Fernet.generate_key().decode(),
    )


def _matching_snapshot(tmp_path, *, model: str = "nexus-gateway/gpt-5.4", branch: str = "main") -> dict:
    skill_text = (tmp_path / "worker-skills" / "code-audit" / "SKILL.md").read_text(encoding="utf-8")
    return {
        "opencode": {"status": "healthy", "version": "1.18.35", "last_error": None},
        "model": {"status": "healthy", "model": model, "configured": True},
        "mcp": {
            "status": "healthy",
            "missing": [],
            "endpoints": {
                "nexus-library": "http://nexus.internal:18000/mcp",
                "jira-mcp": "http://mcp.internal:9100/sse",
            },
        },
        "skills": {
            "status": "healthy",
            "missing": [],
            "discovered": ["code-audit"],
            "content_hashes": {"code-audit": skill_content_hash(skill_text)},
        },
        "repository": {
            "status": "healthy",
            "url": "https://git.internal/org/app.git",
            "branch": branch,
            "commit": "abc1234567890def",
            "dirty": False,
        },
        "checked_at": "2026-11-02T00:00:00+00:00",
    }


async def _seed_ready_remote_worker(session, tmp_path, *, worker_id: str = "emp-1", host: str = "10.0.0.11"):
    pool = await session.get(GatewayPool, "pool-1")
    if pool is None:
        session.add(
            GatewayPool(
                id="pool-1",
                name="primary",
                enabled=True,
            )
        )
        session.add(
            GatewayRoute(
                id="route-1",
                model="gpt-5.4",
                model_type="CHAT",
                pool_id="pool-1",
                enabled=True,
            )
        )
        session.add(
            GatewayRoute(
                id="route-2",
                model="deepseek-v3",
                model_type="CHAT",
                pool_id="pool-1",
                enabled=True,
            )
        )
        session.add(
            CodeRepository(
                id="repo-1",
                url="https://git.internal/org/app.git",
                branch="main",
            )
        )
    employee = AgentWorker(
        id=worker_id,
        name=f"Worker {worker_id}",
        role="后端工程",
        status=EmployeeStatus.IDLE,
        enabled=True,
        auto_claim=True,
        auto_execute=False,
        task_types=["ISSUE"],
        category_ids=["backend"],
    )
    session.add(employee)
    await session.flush()
    environment = OpenCodeExecutionEnvironment(
        employee_id=employee.id,
        mode="LINUX_REMOTE",
        name="生产节点 A",
        description="初始描述",
        host=host,
        port=22,
        username="nexus",
        workspace_root=f"/srv/workspaces/{worker_id}",
        opencode_server_port=14096,
        model_name="nexus-gateway/gpt-5.4",
        repository_id="repo-1",
        branch="main",
        skill_refs=["code-audit"],
        mcp_refs=[{"name": "jira-mcp", "url": "http://mcp.internal:9100/sse"}],
        labels=["linux", "prod"],
    )
    session.add(environment)
    await session.commit()
    await session.refresh(environment)

    settings = _settings(tmp_path)
    gateway = WorkerGateway(settings=settings)
    enrolled = await gateway.enroll(session, environment.id)
    snapshot = _matching_snapshot(tmp_path)
    snapshot["spec_revision"] = (await build_desired_spec(session, environment, settings))["revision"]
    auth_msg = envelope(
        request_id="auth-1",
        worker_instance_id=enrolled.worker_instance_id,
        environment_id=environment.id,
        session_id=None,
        message_type="command",
        payload={
            "name": "authenticate",
            "token": enrolled.token,
            "agent_version": WORKER_AGENT_VERSION,
            "snapshot": snapshot,
        },
    )
    auth_ok = await gateway.authenticate(session, auth_msg, enrolled.token)
    instance = await session.get(WorkerInstance, enrolled.worker_instance_id)
    return settings, gateway, employee, environment, instance, auth_ok


@pytest.mark.asyncio
async def test_display_fields_stay_ready_while_functional_or_host_edits_mark_drifted(db_session, tmp_path):
    settings, _gateway, employee, environment, instance, _auth = await _seed_ready_remote_worker(
        db_session, tmp_path
    )

    desired_initial = await build_desired_spec(db_session, environment, settings)
    ready_state = evaluate_worker_readiness(
        desired_initial, environment, instance, employee_enabled=employee.enabled
    )
    assert ready_state["readiness"] == WorkerReadiness.READY.value
    assert ready_state["schedulable"] is True

    # 场景 1：修改名称、描述、标签等展示字段，保持 READY 且可调度，无需重部署
    environment.name = "生产节点 A（改名）"
    environment.description = "新的备注说明"
    environment.labels = ["linux", "prod", "team-alpha"]
    await db_session.commit()

    desired_after_display = await build_desired_spec(db_session, environment, settings)
    after_display = evaluate_worker_readiness(
        desired_after_display, environment, instance, employee_enabled=employee.enabled
    )
    assert after_display["readiness"] == WorkerReadiness.READY.value
    assert after_display["schedulable"] is True
    assert after_display["target_matched"] is True

    # 场景 2：修改模型、Skill、MCP 或分支，仅标记 DRIFTED（待同步），不改动观测快照
    environment.model_name = "nexus-gateway/deepseek-v3"
    environment.branch = "release/v1.5"
    await db_session.commit()

    desired_after_spec = await build_desired_spec(db_session, environment, settings)
    after_spec = evaluate_worker_readiness(
        desired_after_spec, environment, instance, employee_enabled=employee.enabled
    )
    assert after_spec["readiness"] == WorkerReadiness.DRIFTED.value
    assert after_spec["runtime_health"]["status"] == "DRIFTED"
    assert after_spec["schedulable"] is False
    assert any(item["code"] == "config_drifted" for item in after_spec["blocking"])
    equipment_by_key = {row["key"]: row for row in after_spec["runtime_health"]["equipment"]}
    assert equipment_by_key["model"]["state"] == "drifted"
    assert equipment_by_key["git"]["state"] == "drifted"

    # 场景 3：修改目标主机地址（旧主机连接仍 online），目标指纹不匹配立即阻断调度
    environment.model_name = "nexus-gateway/gpt-5.4"
    environment.branch = "main"
    environment.host = "10.0.0.99"
    await db_session.commit()

    assert compute_target_digest(environment) != instance.target_digest
    desired_after_host = await build_desired_spec(db_session, environment, settings)
    after_host = evaluate_worker_readiness(
        desired_after_host, environment, instance, employee_enabled=employee.enabled
    )
    assert instance.status == "online"
    assert after_host["target_matched"] is False
    assert after_host["readiness"] == WorkerReadiness.DRIFTED.value
    assert after_host["schedulable"] is False
    assert after_host["blocking"][0]["code"] == "target_changed"


@pytest.mark.asyncio
async def test_gateway_heartbeat_snapshot_is_fenced_by_epoch(db_session, tmp_path):
    settings, gateway, _employee, environment, instance, auth_ok = await _seed_ready_remote_worker(
        db_session, tmp_path
    )

    degraded_snapshot = dict(instance.observed_snapshot)
    degraded_snapshot["opencode"] = {
        "status": "unhealthy",
        "version": "1.18.35",
        "error": "connect ECONNREFUSED 127.0.0.1:14096",
        "last_error": "Error: port 14096 bind failed",
    }

    # 旧 epoch 的迟到心跳不得覆盖当前 epoch 的观测快照
    stale_ok = await gateway.note_heartbeat(
        db_session,
        instance.id,
        auth_ok.epoch - 1,
        snapshot=degraded_snapshot,
    )
    assert stale_ok is False
    await db_session.refresh(instance)
    assert instance.observed_snapshot["opencode"]["status"] == "healthy"

    # 当前 epoch 的增量上报立即原子更新快照并反映 last_error
    current_ok = await gateway.note_heartbeat(
        db_session,
        instance.id,
        auth_ok.epoch,
        snapshot=degraded_snapshot,
    )
    assert current_ok is True
    await db_session.refresh(instance)
    desired = await build_desired_spec(db_session, environment, settings)
    degraded_eval = evaluate_worker_readiness(desired, environment, instance)
    assert degraded_eval["readiness"] == WorkerReadiness.DEGRADED.value
    assert degraded_eval["schedulable"] is False
    assert "port 14096 bind failed" in (degraded_eval["runtime_health"]["last_error"] or "")

    # reuse_token 幂等重部署可复用现有有效 token 而不打断在线 epoch
    reused = await gateway.enroll(db_session, environment.id, reuse_token=True)
    assert reused.rotated is False
    assert reused.epoch == auth_ok.epoch
    with pytest.raises(WorkerGatewayError):
        await gateway.enroll(db_session, 999999)


async def test_reconnect_without_snapshot_cannot_reuse_previous_connection_readiness(db_session, tmp_path):
    settings, gateway, _employee, environment, instance, auth = await _seed_ready_remote_worker(
        db_session, tmp_path
    )
    reconnect = envelope(
        request_id="auth-reconnect",
        worker_instance_id=instance.id,
        environment_id=environment.id,
        session_id=None,
        message_type="command",
        payload={"name": "authenticate", "agent_version": WORKER_AGENT_VERSION},
    )
    enrolled = await gateway.enroll(db_session, environment.id, reuse_token=True)
    new_auth = await gateway.authenticate(db_session, reconnect, enrolled.token)
    assert new_auth.epoch > auth.epoch
    await db_session.refresh(instance)
    assert instance.observed_snapshot is None
    assert instance.observed_at is None
    desired = await build_desired_spec(db_session, environment, settings)
    evaluated = evaluate_worker_readiness(desired, environment, instance)
    assert evaluated["readiness"] == "UNKNOWN"
    assert evaluated["schedulable"] is False


@pytest.mark.parametrize("binding", ["skill_refs", "mcp_refs"])
async def test_removing_managed_bindings_stays_drifted_until_redeploy(db_session, tmp_path, binding):
    settings, _gateway, employee, environment, instance, _auth = await _seed_ready_remote_worker(
        db_session, tmp_path
    )
    original_snapshot = dict(instance.observed_snapshot)
    setattr(environment, binding, [])
    await db_session.commit()
    desired = await build_desired_spec(db_session, environment, settings)
    evaluated = evaluate_worker_readiness(desired, environment, instance)
    assert evaluated["readiness"] == "DRIFTED"
    assert evaluated["schedulable"] is False
    assert instance.observed_snapshot == original_snapshot
    assert employee.enabled is True


async def test_redeploy_to_new_host_rotates_token_and_fences_old_connection(db_session, tmp_path):
    _settings, gateway, _employee, environment, instance, auth = await _seed_ready_remote_worker(
        db_session, tmp_path
    )
    previous = await gateway.enroll(db_session, environment.id, reuse_token=True)
    environment.host = "10.0.0.99"
    await db_session.commit()
    moved = await gateway.enroll(db_session, environment.id, reuse_token=True)
    assert moved.rotated is True
    assert moved.token != previous.token
    await db_session.refresh(instance)
    assert instance.target_digest == compute_target_digest(environment)
    assert instance.observed_snapshot is None
    assert instance.status == "offline"
    assert not await gateway.note_heartbeat(
        db_session, instance.id, auth.epoch, snapshot=_matching_snapshot(tmp_path)
    )


@pytest.mark.parametrize("startup_error", [None, "OpenCode bind failed"])
async def test_deploy_acceptance_reports_snapshot_blocker_without_command_probe(
    db_session, tmp_path, monkeypatch, startup_error
):
    from unittest.mock import AsyncMock

    from app.runtime import remote_provisioning

    settings, _gateway, _employee, environment, instance, _auth = await _seed_ready_remote_worker(
        db_session, tmp_path
    )
    monkeypatch.setattr(remote_provisioning, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(settings, "remote_deploy_timeout_seconds", 0.05)
    snapshot = _matching_snapshot(tmp_path)
    if startup_error:
        snapshot["opencode"] = {"status": "unhealthy", "last_error": startup_error}
    else:
        environment.branch = "pending-config"
    instance.observed_snapshot = snapshot
    await db_session.commit()
    provisioner = remote_provisioning.RemoteProvisioner(environment, {"id": "acceptance"})
    provisioner.settings = settings
    monkeypatch.setattr(provisioner, "_ensure_safe", AsyncMock())
    with pytest.raises(RuntimeError, match=startup_error or "待执行一键部署同步"):
        await provisioner._converge_readiness(AsyncMock())


@pytest.mark.asyncio
async def test_dispatcher_skips_drifted_or_offline_worker_and_assigns_healthy_peer(
    db_session, tmp_path, monkeypatch
):
    settings, _gw1, emp1, env1, inst1, _auth1 = await _seed_ready_remote_worker(
        db_session, tmp_path, worker_id="emp-01", host="10.0.0.11"
    )
    monkeypatch.setattr("app.runtime.worker_readiness.get_settings", lambda: settings)

    # emp-01 排在字典序最前，但修改了模型处于 DRIFTED（待同步）状态
    env1.model_name = "nexus-gateway/deepseek-v3"
    await db_session.commit()

    # emp-02 处于完全就绪状态
    _s2, _gw2, emp2, _env2, _inst2, _auth2 = await _seed_ready_remote_worker(
        db_session, tmp_path, worker_id="emp-02", host="10.0.0.12"
    )
    assert inst1.status == "online"

    task = Task(
        id="T-skip-drifted-01",
        title="修复调度队头阻塞",
        task_type="ISSUE",
        category_id="backend",
        status=TaskStatus.PENDING,
        repository_id="repo-1",
    )
    db_session.add(task)
    await db_session.commit()

    claimed = await Dispatcher(db_session).claim_once()
    assert claimed is not None
    assert claimed.id == "T-skip-drifted-01"
    assert claimed.employee_id == emp2.id
    await db_session.refresh(emp1)
    assert emp1.current_task_count == 0

"""装备期望、节点观测对比与就绪投影契约测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.gateway.models  # noqa: F401 - 注册 GatewayRoute 表
from app.domain import EnvironmentStatus
from app.gateway.models import GatewayRoute
from app.models import AgentWorker, Base, OpenCodeExecutionEnvironment, WorkerInstance
from app.runtime.worker_health import probe_online_worker
from app.runtime.worker_readiness import (
    _skills_content_revision,
    compute_target_digest,
    evaluate_worker_readiness,
    mcp_endpoints_revision,
)
from app.runtime.worker_readiness import (
    build_desired_spec as desired_equipment,
)
from app.runtime.worker_readiness import (
    build_equipment_rows as equipment_rows,
)
from app.runtime.worker_readiness import (
    build_repository_summary as repository_payload,
)
from app.worker_agent import __version__ as WORKER_AGENT_VERSION

CANARY = "sk-canary-secret-value"


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        workspace_root=tmp_path,
        remote_opencode_version="1.18.33",
        mcp_resource_server_url="https://mcp.example.test/mcp?token=query-canary",
    )


async def _seed(session, tmp_path: Path) -> OpenCodeExecutionEnvironment:
    skill = tmp_path / "worker-skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: review code\n---\n# Review\n",
        encoding="utf-8",
    )
    session.add(AgentWorker(id="emp-1", name="Employee", role="build"))
    session.add(GatewayRoute(id="route-1", model="route-1", pool_id="pool-1", enabled=True))
    environment = OpenCodeExecutionEnvironment(
        employee_id="emp-1",
        repository_url="https://user:pass@example.test/org/repo.git?token=query-canary",
        git_token_ciphertext="cipher-canary-value",
        mode="LINUX_REMOTE",
        name="Linux",
        host="worker.test",
        username="root",
        ssh_password="ssh-secret",
        workspace_root="/srv/project",
        model_name=None,
        skill_refs=["review"],
        mcp_refs=[{"name": "extra", "url": "https://extra.example.test/mcp"}],
        status_detail={"desired": {"model": "stale-model-must-not-win"}},
    )
    session.add(environment)
    await session.commit()
    return environment


def _desired() -> dict:
    skill_items = [
        {
            "name": "review",
            "content_hash": "skill-hash",
            "fullfile_hash": "skill-fullfile-hash",
        }
    ]
    mcp_items = [{"name": "nexus-library", "url": "https://mcp.example.test/mcp", "required": True}]
    return {
        "opencode": {"version": "1.18.33"},
        "worker_agent": {"version": WORKER_AGENT_VERSION},
        "model": {"name": "nexus-gateway/route-1", "route": "route-1", "provider": "nexus-gateway"},
        "repository": {
                "url": "https://example.test/org/repo.git",
            "target_branch": "main",
            "authentication": "none",
            "credential_revision": None,
        },
        "skills": {
            "items": skill_items,
            "missing_sources": [],
            "unverifiable_sources": [],
            "revision": _skills_content_revision(skill_items),
            "content_revision": _skills_content_revision(skill_items),
        },
        "mcp": {
            "items": mcp_items,
            "required": ["nexus-library"],
            "revision": mcp_endpoints_revision(mcp_items),
        },
        "revision": "desired-revision",
    }


def _observation(desired: dict | None = None) -> dict:
    want = desired or _desired()
    skills = [item["name"] for item in want["skills"]["items"]]
    content_hashes = {item["name"]: item["content_hash"] for item in want["skills"]["items"] if item["name"]}
    endpoints = {item["name"]: item["url"] for item in want["mcp"]["items"] if item["name"]}
    return {
        "opencode": {"status": "healthy", "version": want["opencode"]["version"], "error": None},
        "model": {
            "status": "healthy",
            "configured": True,
            "authenticated": True,
            "model": want["model"]["name"],
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
            "content_hashes": content_hashes,
            "error": None,
        },
        "repository": {
            "status": "healthy",
            "url": want["repository"]["url"],
            "branch": want["repository"]["target_branch"],
            "commit": "a" * 40,
            "dirty": False,
            "credential_revision": want["repository"].get("credential_revision"),
            "error": None,
        },
        "checked_at": "2026-10-19T00:00:00+00:00",
        "worker_agent_version": want["worker_agent"]["version"],
        "observed_epoch": 7,
        "source": "worker",
        "error": None,
    }


def health_status(observation: dict, desired: dict) -> EnvironmentStatus:
    environment = OpenCodeExecutionEnvironment(
        id=1,
        mode="LINUX_REMOTE",
        host="fixture.invalid",
        port=22,
        username="nexus",
        workspace_root="/srv/project",
        opencode_server_port=14096,
    )
    digest = compute_target_digest(environment)
    worker = WorkerInstance(
        id="fixture",
        environment_id=1,
        status="online",
        epoch=1,
        target_digest=digest,
        agent_version=WORKER_AGENT_VERSION,
        observed_snapshot={**observation, "spec_revision": desired.get("revision")},
    )
    result = evaluate_worker_readiness({**desired, "target_digest": digest}, environment, worker)
    return EnvironmentStatus(result["runtime_health"]["status"])


@pytest.mark.asyncio
async def test_desired_uses_db_and_skill_content_changes_revision(session, tmp_path: Path):
    environment = await _seed(session, tmp_path)
    settings = _settings(tmp_path)

    first = await desired_equipment(session, environment, settings)

    assert first["model"]["name"] == "nexus-gateway/route-1"
    assert first["worker_agent"]["version"] == WORKER_AGENT_VERSION
    assert first["repository"]["url"] == "https://example.test/org/repo.git"
    assert first["repository"]["target_branch"] == "main"
    assert first["repository"]["authentication"] == "token"
    assert first["repository"]["credential_revision"]
    assert first["skills"]["items"][0]["content_hash"]
    assert first["mcp"]["required"] == ["extra", "nexus-library"]
    serialized = json.dumps(first, ensure_ascii=False)
    for secret in ("cipher-canary-value", "query-canary", "user:pass", "ssh-secret"):
        assert secret not in serialized
    assert "stale-model-must-not-win" not in serialized

    (tmp_path / "worker-skills" / "review" / "SKILL.md").write_text("# Review\n\nchanged\n", encoding="utf-8")
    second = await desired_equipment(session, environment, settings)

    assert second["skills"]["items"][0]["content_hash"] != first["skills"]["items"][0]["content_hash"]
    assert second["skills"]["revision"] != first["skills"]["revision"]
    assert second["model"] == first["model"]


def test_healthy_projection_and_fixed_shapes():
    desired = _desired()
    observation = _observation(desired)

    rows = equipment_rows(desired, observation)

    assert [row["key"] for row in rows] == [
        "opencode",
        "worker_agent",
        "model",
        "mcp",
        "skills",
        "git",
    ]
    for row in rows:
        expected_keys = {
            "key",
            "label",
            "state",
            "configured",
            "deployed",
            "verified",
            "desired_revision",
            "actual_revision",
            "diff",
            "error",
            "detail",
        }
        if row["key"] == "mcp":
            expected_keys.add("warnings")
            assert row["warnings"] == []
        assert set(row) == expected_keys
        assert row["state"] == "ready"
        assert row["verified"] is True
        assert row["deployed"] is True
        assert row["error"] is None
    assert health_status(observation, desired) == EnvironmentStatus.AVAILABLE

    payload = repository_payload(desired, observation)
    assert set(payload) == {
        "url",
        "target_branch",
        "branch",
        "commit",
        "dirty",
        "authentication",
        "error",
    }
    assert payload == {
        "url": "https://example.test/org/repo.git",
        "target_branch": "main",
        "branch": "main",
        "commit": "a" * 40,
        "dirty": False,
        "authentication": "none",
        "error": None,
    }


def test_missing_probe_is_unknown_and_not_schedulable():
    desired = _desired()
    observation = {
        key: {"status": "unknown", "error": "未探测"}
        for key in ("opencode", "model", "mcp", "skills", "repository")
    }
    observation.update(
        {
            "checked_at": "2026-10-19T00:00:00+00:00",
            "worker_agent_version": None,
            "observed_epoch": None,
            "source": "none",
            "error": None,
        }
    )

    rows = equipment_rows(desired, observation)

    assert health_status(observation, desired) == EnvironmentStatus.UNKNOWN
    assert all(row["verified"] is None for row in rows)
    assert all(row["deployed"] is False for row in rows)


def test_mcp_disconnected_is_reported_per_item():
    desired = _desired()
    observation = _observation(desired)
    observation["mcp"] = {
        "status": "unhealthy",
        "connected": False,
        "missing": ["nexus-library"],
        "servers": {"nexus-library": {"status": "failed"}},
        "endpoints": {item["name"]: item["url"] for item in desired["mcp"]["items"]},
        "error": "MCP 未连接：nexus-library",
    }

    row = {item["key"]: item for item in equipment_rows(desired, observation)}["mcp"]

    assert row["verified"] is False
    assert row["state"] == "degraded"
    assert row["error"] == "MCP 未连接：nexus-library"
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


def test_skill_missing_marks_drifted_pending_deploy():
    desired = _desired()
    observation = _observation(desired)
    observation["skills"] = {
        "status": "healthy",
        "discovered": [],
        "missing": ["review"],
        "content_hashes": {},
        "error": None,
    }

    row = {item["key"]: item for item in equipment_rows(desired, observation)}["skills"]

    assert row["verified"] is False
    assert row["state"] == "drifted"
    assert health_status(observation, desired) == EnvironmentStatus.DRIFTED


def test_model_mismatch_marks_drifted_while_missing_provider_marks_degraded():
    desired = _desired()
    observation = _observation(desired)
    observation["model"] = {
        "status": "healthy",
        "configured": True,
        "model": "nexus-gateway/other",
        "error": None,
    }
    drifted_row = {item["key"]: item for item in equipment_rows(desired, observation)}["model"]
    assert drifted_row["state"] == "drifted"
    assert health_status(observation, desired) == EnvironmentStatus.DRIFTED

    observation["model"] = {
        "status": "unhealthy",
        "configured": False,
        "model": "nexus-gateway/route-1",
        "error": "模型 provider 缺少网关地址或凭据",
    }
    degraded_row = {item["key"]: item for item in equipment_rows(desired, observation)}["model"]
    assert degraded_row["state"] == "degraded"
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


def test_model_configuration_does_not_depend_on_gateway_authentication():
    desired = _desired()
    observation = _observation(desired)
    observation["model"].pop("authenticated")
    assert health_status(observation, desired) == EnvironmentStatus.AVAILABLE
    desired["model"]["resolved"] = False
    assert health_status(observation, desired) != EnvironmentStatus.AVAILABLE
    model = next(row for row in equipment_rows(desired, observation) if row["key"] == "model")
    assert model["verified"] is False


@pytest.mark.asyncio
async def test_online_worker_probe_uses_inspect_runtime_with_deep_flag(monkeypatch):
    desired = _desired()
    calls: list[tuple[str, dict]] = []
    broken = _observation(desired)
    broken["opencode"] = {
        "status": "unhealthy",
        "version": None,
        "error": "OpenCode session/status 不可用：HTTP 503",
    }

    class FakeClient:
        def __init__(self, settings, *, environment_id):
            self.environment_id = environment_id

        async def run_command(self, name, data, **_kwargs):
            calls.append((name, data))
            return broken

    monkeypatch.setattr("app.runtime.worker_health.WorkerOpenCodeClient", FakeClient)
    environment = SimpleNamespace(
        runtime_engine="opencode",
        id=7,
        host="worker.test",
        username="root",
        ssh_password="ssh-secret",
    )

    observation = await probe_online_worker(
        environment,
        desired,
        _settings(Path("/tmp")),
        worker_instance=SimpleNamespace(status="online", agent_version=WORKER_AGENT_VERSION, epoch=11),
        deep=True,
    )

    assert calls == [
        (
            "inspect_runtime",
            {
                "expected_skills": ["review"],
                "expected_mcp": ["nexus-library"],
                "expected_model": "nexus-gateway/route-1",
                "expected_opencode_version": "1.18.33",
                "deep": True,
            },
        )
    ]
    assert observation["source"] == "worker"
    assert observation["observed_epoch"] == 11
    assert observation["worker_agent_version"] == WORKER_AGENT_VERSION
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_offline_worker_probe_never_opens_ssh():
    desired = _desired()
    environment = SimpleNamespace(
        runtime_engine="opencode",
        id=9,
        host="worker.test",
        username="root",
        ssh_password="ssh-secret",
        workspace_root="/srv/project",
    )

    observation = await probe_online_worker(
        environment,
        desired,
        _settings(Path("/tmp")),
        worker_instance=SimpleNamespace(status="offline", agent_version="1.4.5", epoch=2),
    )

    assert "离线" in str(observation["error"])
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


def test_dirty_git_only_blocks_updates_not_runtime_health():
    desired = _desired()
    observation = _observation(desired)
    observation["repository"]["dirty"] = True

    rows = {item["key"]: item for item in equipment_rows(desired, observation)}
    row = rows["git"]

    assert row["verified"] is True
    assert row["error"] is None
    assert health_status(observation, desired) == EnvironmentStatus.AVAILABLE
    assert repository_payload(desired, observation)["dirty"] is True


def test_repository_payload_sanitizes_url_and_redacts_errors():
    desired = _desired()
    observation = _observation(desired)
    observation["repository"]["url"] = (
        "https://user:pass@example.test/org/repo.git?token=query-canary#fragment"
    )
    observation["repository"]["status"] = "unknown"
    observation["repository"]["error"] = f"读取失败 Bearer {CANARY}"

    payload = repository_payload(desired, observation)

    assert payload["url"] == "https://example.test/org/repo.git"
    assert payload["error"] == "读取失败 Bearer [已隐藏]"
    serialized = json.dumps(payload, ensure_ascii=False)
    for secret in ("user:pass", "query-canary", "fragment", CANARY):
        assert secret not in serialized


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("url", "https://wrong.example.test/org/repo.git", "仓库地址"),
        ("branch", "release", "分支"),
    ],
)
def test_wrong_git_identity_marks_drifted(field, value, fragment):
    desired = _desired()
    observation = _observation(desired)
    observation["repository"][field] = value

    row = {item["key"]: item for item in equipment_rows(desired, observation)}["git"]

    assert row["verified"] is False
    assert row["state"] == "drifted"
    assert any(fragment in item for item in row["diff"])
    assert health_status(observation, desired) == EnvironmentStatus.DRIFTED


def test_same_skill_name_with_old_content_marks_drifted():
    desired = _desired()
    desired["skills"]["items"][0]["content_hash"] = "new-content-hash"
    observation = _observation(desired)
    observation["skills"]["content_hashes"]["review"] = "old-content-hash"

    row = {item["key"]: item for item in equipment_rows(desired, observation)}["skills"]

    assert row["verified"] is False
    assert row["state"] == "drifted"
    assert any("内容待同步" in item for item in row["diff"])
    assert health_status(observation, desired) == EnvironmentStatus.DRIFTED


def test_managed_skill_revision_ignores_unmanaged_extra_skills():
    desired = _desired()
    expected = _skills_content_revision([{"name": "review", "content_hash": "skill-hash"}])
    desired["skills"]["content_revision"] = expected
    observation = _observation(desired)
    observation["skills"]["content_hashes"]["extra-unmanaged"] = "extra-hash-v1"

    row = {item["key"]: item for item in equipment_rows(desired, observation)}["skills"]

    assert row["desired_revision"] == expected
    assert row["actual_revision"] == expected
    assert row["verified"] is True
    assert row["diff"] == []

    observation["skills"]["content_hashes"]["extra-unmanaged"] = "extra-hash-v2"
    unchanged = {item["key"]: item for item in equipment_rows(desired, observation)}["skills"]
    assert unchanged["actual_revision"] == expected
    assert unchanged["verified"] is True

    observation["skills"]["content_hashes"]["review"] = "skill-hash-drifted"
    drifted = {item["key"]: item for item in equipment_rows(desired, observation)}["skills"]
    assert drifted["actual_revision"] != expected
    assert drifted["verified"] is False


def test_same_mcp_name_with_wrong_url_marks_drifted():
    desired = _desired()
    observation = _observation(desired)
    observation["mcp"]["endpoints"]["nexus-library"] = "https://wrong.example.test/mcp"

    row = {item["key"]: item for item in equipment_rows(desired, observation)}["mcp"]

    assert row["verified"] is False
    assert row["state"] == "drifted"
    assert any("地址待同步" in item for item in row["diff"])
    assert health_status(observation, desired) == EnvironmentStatus.DRIFTED


def test_private_credential_change_marks_drifted_until_deployed():
    desired = _desired()
    desired["repository"]["authentication"] = "token"
    desired["repository"]["credential_revision"] = "new-credential"
    observation = _observation(desired)
    observation["repository"]["credential_revision"] = "old-credential"

    row = {item["key"]: item for item in equipment_rows(desired, observation)}["git"]

    assert row["verified"] is False
    assert row["state"] == "drifted"
    assert health_status(observation, desired) == EnvironmentStatus.DRIFTED


def test_unbound_repository_is_explicitly_unconfigured():
    desired = _desired()
    desired["repository"] = None
    observation = _observation()

    row = {item["key"]: item for item in equipment_rows(desired, observation)}["git"]

    assert row["configured"] is False
    assert row["deployed"] is False
    assert row["verified"] is None
    assert row["error"] == "未绑定 Git 仓库"

"""R02/R06/R07：装备期望、真实只读探测与 canonical 投影。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.gateway.models  # noqa: F401 - 注册 GatewayRoute 表
from app.domain import EnvironmentStatus
from app.gateway.models import GatewayRoute
from app.models import (
    AgentWorker,
    Base,
    CodeRepository,
    GitCredential,
    OpenCodeExecutionEnvironment,
)
from app.runtime.worker_health import (
    WORKER_AGENT_VERSION,
    _skills_content_revision,
    desired_equipment,
    equipment_rows,
    health_status,
    probe_equipment,
    repository_payload,
)

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
        remote_opencode_version="1.18.35",
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
    session.add(
        CodeRepository(
            id="repo-1",
            url="https://user:pass@example.test/org/repo.git?token=query-canary",
            branch="main",
        )
    )
    session.add(GitCredential(repository_id="repo-1", token_ciphertext="cipher-canary-value"))
    session.add(GatewayRoute(id="route-1", model="route-1", pool_id="pool-1", enabled=True))
    environment = OpenCodeExecutionEnvironment(
        employee_id="emp-1",
        repository_id="repo-1",
        mode="LINUX_REMOTE",
        name="Linux",
        host="worker.test",
        username="root",
        ssh_password="ssh-secret",
        workspace_root="/srv/project",
        model_name=None,
        skill_refs=["review"],
        mcp_refs=[{"name": "extra", "url": "https://extra.example.test/mcp"}],
        worker_channel="ssh",
        status_detail={"desired": {"model": "stale-model-must-not-win"}},
    )
    session.add(environment)
    await session.commit()
    return environment


def _desired() -> dict:
    return {
        "opencode": {"version": "1.18.35"},
        "worker_agent": {"version": WORKER_AGENT_VERSION},
        "model": {"name": "nexus-gateway/route-1", "route": "route-1", "provider": "nexus-gateway"},
        "repository": {
            "id": "repo-1",
            "url": "https://example.test/org/repo.git",
            "target_branch": "main",
            "authentication": "none",
            "credential_revision": None,
        },
        "skills": {
            "items": [
                {
                    "name": "review",
                    "content_hash": "skill-hash",
                    "fullfile_hash": "skill-fullfile-hash",
                }
            ],
            "missing_sources": [],
            "unverifiable_sources": [],
            "revision": "skill-revision",
            "content_revision": "skill-content-revision",
        },
        "mcp": {
            "items": [{"name": "nexus-library", "url": "https://mcp.example.test/mcp", "required": True}],
            "required": ["nexus-library"],
            "revision": "mcp-revision",
        },
        "revision": "desired-revision",
    }


def _observation(desired: dict | None = None) -> dict:
    want = desired or _desired()
    skills = [item["name"] for item in want["skills"]["items"]]
    content_hashes = {
        item["name"]: item["content_hash"] for item in want["skills"]["items"] if item["name"]
    }
    endpoints = {
        item["name"]: item["url"] for item in want["mcp"]["items"] if item["name"]
    }
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
            "error": None,
        },
        "checked_at": "2026-10-19T00:00:00+00:00",
        "worker_agent_version": want["worker_agent"]["version"],
        "observed_epoch": 7,
        "source": "worker",
        "error": None,
    }


def _snapshot(desired: dict) -> dict:
    snapshot = copy.deepcopy(desired)
    snapshot["repository"]["commit"] = "a" * 40
    return snapshot


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

    (tmp_path / "worker-skills" / "review" / "SKILL.md").write_text(
        "# Review\n\nchanged\n", encoding="utf-8"
    )
    second = await desired_equipment(session, environment, settings)

    assert second["skills"]["items"][0]["content_hash"] != first["skills"]["items"][0]["content_hash"]
    assert second["skills"]["revision"] != first["skills"]["revision"]
    assert second["model"] == first["model"]

    rows = {row["key"]: row for row in equipment_rows(second, {}, first)}
    assert rows["skills"]["deployed"] is False
    assert any("Skill 部署 revision" in item for item in rows["skills"]["diff"])


def test_healthy_projection_and_fixed_shapes():
    desired = _desired()
    observation = _observation(desired)
    snapshot = _snapshot(desired)

    rows = equipment_rows(desired, observation, snapshot)

    assert [row["key"] for row in rows] == [
        "opencode",
        "worker_agent",
        "model",
        "mcp",
        "skills",
        "git",
    ]
    for row in rows:
        assert set(row) == {
            "key",
            "label",
            "configured",
            "deployed",
            "verified",
            "desired_revision",
            "actual_revision",
            "diff",
            "error",
            "detail",
        }
        assert row["verified"] is True
        assert row["deployed"] is True
        assert row["error"] is None
    assert health_status(observation, desired) == EnvironmentStatus.AVAILABLE

    payload = repository_payload(desired, observation)
    assert set(payload) == {
        "id",
        "url",
        "target_branch",
        "branch",
        "commit",
        "dirty",
        "authentication",
        "error",
    }
    assert payload == {
        "id": "repo-1",
        "url": "https://example.test/org/repo.git",
        "target_branch": "main",
        "branch": "main",
        "commit": "a" * 40,
        "dirty": False,
        "authentication": "none",
        "error": None,
    }


def test_missing_probe_is_unverified_and_not_schedulable():
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

    rows = equipment_rows(desired, observation, None)

    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE
    assert all(row["verified"] is None for row in rows)
    assert all(row["deployed"] is False for row in rows)


def test_mcp_disconnected_is_reported_per_item():
    desired = _desired()
    observation = _observation(desired)
    observation["mcp"] = {
        "status": "unhealthy",
        "connected": False,
        "missing": ["nexus-library"],
        "error": "MCP 未连接：nexus-library",
    }

    row = {item["key"]: item for item in equipment_rows(desired, observation, _snapshot(desired))}["mcp"]

    assert row["verified"] is False
    assert row["error"] == "MCP 未连接：nexus-library"
    assert any("MCP 未连接" in item for item in row["diff"])
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


def test_skill_missing_is_reported_separately_from_content_revision():
    desired = _desired()
    observation = _observation(desired)
    observation["skills"] = {
        "status": "unhealthy",
        "discovered": [],
        "missing": ["review"],
        "error": "Skill 未发现：review",
    }

    row = {item["key"]: item for item in equipment_rows(desired, observation, _snapshot(desired))}[
        "skills"
    ]

    assert row["verified"] is False
    assert any("Skill 未发现" in item for item in row["diff"])
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


@pytest.mark.parametrize(
    ("actual", "configured", "error"),
    [
        ("nexus-gateway/other", True, "实际模型与期望模型不一致"),
        ("nexus-gateway/route-1", False, "模型 provider 缺少网关地址或凭据"),
    ],
)
def test_model_mismatch_and_missing_configuration_are_unverified(actual, configured, error):
    desired = _desired()
    observation = _observation(desired)
    observation["model"] = {
        "status": "unhealthy",
        "configured": configured,
        "model": actual,
        "error": error,
    }

    row = {item["key"]: item for item in equipment_rows(desired, observation, _snapshot(desired))}[
        "model"
    ]

    assert row["verified"] is False
    assert row["error"] == error
    assert any("模型" in item for item in row["diff"])
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


def test_model_configuration_does_not_depend_on_gateway_authentication():
    desired = _desired()
    observation = _observation(desired)
    observation["model"].pop("authenticated")
    assert health_status(observation, desired) == EnvironmentStatus.AVAILABLE
    desired["model"]["resolved"] = False
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE
    model = next(
        row for row in equipment_rows(desired, observation, _snapshot(desired)) if row["key"] == "model"
    )
    assert model["verified"] is False


@pytest.mark.asyncio
async def test_online_worker_uses_inspect_runtime_even_when_channel_is_ssh(monkeypatch):
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
        worker_channel="ssh",
        host="worker.test",
        username="root",
        ssh_password="ssh-secret",
    )

    observation = await probe_equipment(
        environment,
        desired,
        _settings(Path("/tmp")),
        worker_instance=SimpleNamespace(status="online", agent_version=WORKER_AGENT_VERSION, epoch=3),
        observed_epoch=11,
    )

    assert calls == [
        (
            "inspect_runtime",
            {
                "expected_skills": ["review"],
                "expected_mcp": ["nexus-library"],
                "expected_model": "nexus-gateway/route-1",
                "expected_opencode_version": "1.18.35",
            },
        )
    ]
    assert all(name == "inspect_runtime" for name, _data in calls)
    assert observation["source"] == "worker"
    assert observation["observed_epoch"] == 11
    assert observation["worker_agent_version"] == WORKER_AGENT_VERSION
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_offline_ssh_probe_is_read_only_and_redacts_canary(monkeypatch):
    desired = _desired()
    commands: list[str] = []
    inspection = _observation(desired)
    inspection["model"]["status"] = "unhealthy"
    inspection["model"]["error"] = f"模型网关认证失败 Bearer {CANARY}"

    class FakeSession:
        async def run_command(self, command):
            commands.append(command)
            return 0, json.dumps(inspection, ensure_ascii=False), ""

        async def close(self):
            return None

    class FakeRuntime:
        def __init__(self, settings):
            self.settings = settings

        async def open_session(self, environment):
            return FakeSession()

    monkeypatch.setattr("app.runtime.worker_health.ExecutionEnvironmentRuntime", FakeRuntime)
    environment = SimpleNamespace(
        runtime_engine="opencode",
        id=9,
        host="worker.test",
        username="root",
        ssh_password="ssh-secret",
        workspace_root="/srv/project",
    )

    observation = await probe_equipment(
        environment,
        desired,
        _settings(Path("/tmp")),
        worker_instance=SimpleNamespace(status="offline", agent_version="1.4.4", epoch=2),
        observed_epoch=4,
        perenv_python="/opt/nexus/workers/9/venv/bin/python",
    )

    command = commands[0]
    assert "/opt/nexus/workers/9/venv/bin/python" in command
    assert "/etc/nexus/worker-9.env" in command
    assert "from app.worker_agent.inspection import inspect_runtime" in command
    assert "expected_model" in command
    assert "ASTRA_WORKER" not in command
    assert "NEXUS_WORKER_OPENCODE_URL" in command
    assert "NEXUS_WORKER_WORKSPACE_ROOT" in command
    for forbidden in ("pip install", "systemctl", " restart", "git clone", "rm -rf"):
        assert forbidden not in command
    assert CANARY not in command
    assert CANARY not in json.dumps(observation, ensure_ascii=False)
    assert observation["source"] == "ssh"
    assert observation["observed_epoch"] == 4
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_missing_per_environment_artifact_is_explicitly_not_deployed(monkeypatch):
    class FakeSession:
        async def run_command(self, _command):
            return 127, "", "python: no such file"

        async def close(self):
            return None

    class FakeRuntime:
        def __init__(self, settings):
            self.settings = settings

        async def open_session(self, environment):
            return FakeSession()

    monkeypatch.setattr("app.runtime.worker_health.ExecutionEnvironmentRuntime", FakeRuntime)
    environment = SimpleNamespace(
        runtime_engine="opencode",
        id=3,
        host="worker.test",
        username="root",
        ssh_password="ssh-secret",
        workspace_root="/srv/project",
    )

    observation = await probe_equipment(
        environment,
        _desired(),
        _settings(Path("/tmp")),
        worker_instance=SimpleNamespace(status="offline", agent_version=None, epoch=1),
        observed_epoch=1,
    )

    assert "未部署" in observation["error"]
    assert observation["source"] == "ssh"
    assert health_status(observation) == EnvironmentStatus.UNAVAILABLE


def test_dirty_git_only_blocks_updates_not_runtime_health():
    desired = _desired()
    observation = _observation(desired)
    observation["repository"]["dirty"] = True

    rows = {item["key"]: item for item in equipment_rows(desired, observation, _snapshot(desired))}
    row = rows["git"]

    assert row["verified"] is True
    assert row["error"] is None
    assert any("仅阻止更新" in item for item in row["diff"])
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
def test_raw_healthy_but_wrong_git_identity_is_unavailable(field, value, fragment):
    desired = _desired()
    observation = _observation(desired)
    observation["repository"][field] = value

    row = {item["key"]: item for item in equipment_rows(desired, observation, _snapshot(desired))}[
        "git"
    ]

    assert row["verified"] is False
    assert any(fragment in item for item in row["diff"])
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


def test_same_skill_name_with_old_content_is_not_verified():
    desired = _desired()
    desired["skills"]["items"][0]["content_hash"] = "new-content-hash"
    observation = _observation(desired)
    observation["skills"]["content_hashes"]["review"] = "old-content-hash"

    row = {item["key"]: item for item in equipment_rows(desired, observation, _snapshot(desired))}[
        "skills"
    ]

    assert row["verified"] is False
    assert any("内容不一致" in item for item in row["diff"])
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


def test_managed_skill_revision_ignores_unmanaged_extra_skills():
    desired = _desired()
    expected = _skills_content_revision(
        [{"name": "review", "content_hash": "skill-hash"}]
    )
    desired["skills"]["content_revision"] = expected
    observation = _observation(desired)
    observation["skills"]["content_hashes"]["extra-unmanaged"] = "extra-hash-v1"
    snapshot = _snapshot(desired)

    row = {item["key"]: item for item in equipment_rows(desired, observation, snapshot)}[
        "skills"
    ]

    assert row["desired_revision"] == expected
    assert row["actual_revision"] == expected
    assert row["verified"] is True
    assert row["diff"] == []

    observation["skills"]["content_hashes"]["extra-unmanaged"] = "extra-hash-v2"
    unchanged = {
        item["key"]: item
        for item in equipment_rows(desired, observation, snapshot)
    }["skills"]
    assert unchanged["actual_revision"] == expected
    assert unchanged["verified"] is True

    observation["skills"]["content_hashes"]["review"] = "skill-hash-drifted"
    drifted = {
        item["key"]: item
        for item in equipment_rows(desired, observation, snapshot)
    }["skills"]
    assert drifted["actual_revision"] != expected
    assert drifted["verified"] is False

    observation["skills"]["content_hashes"].pop("review")
    unknown = {
        item["key"]: item
        for item in equipment_rows(desired, observation, snapshot)
    }["skills"]
    assert unknown["actual_revision"] is None


def test_skill_actual_revision_uses_observed_semantic_content_hashes():
    desired = _desired()
    observation = _observation(desired)
    snapshot = _snapshot(desired)

    row = {item["key"]: item for item in equipment_rows(desired, observation, snapshot)}["skills"]

    assert row["desired_revision"] == "skill-content-revision"
    assert row["actual_revision"] is not None
    assert row["actual_revision"] != "skill-revision"
    baseline = row["actual_revision"]

    observation["skills"]["content_hashes"]["review"] = "drifted-content-hash"
    drifted = {
        item["key"]: item
        for item in equipment_rows(desired, observation, snapshot)
    }["skills"]
    assert drifted["actual_revision"] != baseline
    assert drifted["verified"] is False
    assert any("内容不一致" in item for item in drifted["diff"])

    observation["skills"]["content_hashes"] = None
    unknown = {
        item["key"]: item
        for item in equipment_rows(desired, observation, snapshot)
    }["skills"]
    assert unknown["actual_revision"] is None
    assert unknown["verified"] is None


def test_same_mcp_name_with_wrong_url_is_not_verified():
    desired = _desired()
    observation = _observation(desired)
    observation["mcp"]["endpoints"]["nexus-library"] = "https://wrong.example.test/mcp"

    row = {item["key"]: item for item in equipment_rows(desired, observation, _snapshot(desired))}[
        "mcp"
    ]

    assert row["verified"] is False
    assert any("URL 不一致" in item for item in row["diff"])
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


def test_unreadable_config_and_skill_content_are_unknown_not_verified():
    desired = _desired()
    observation = _observation(desired)
    observation["mcp"]["endpoints"] = None
    observation["skills"]["content_hashes"] = None
    observation["skills"]["discovered"] = ["review"]

    rows = {item["key"]: item for item in equipment_rows(desired, observation, _snapshot(desired))}

    assert rows["mcp"]["verified"] is None
    assert any("/config" in item for item in rows["mcp"]["diff"])
    assert rows["skills"]["verified"] is None
    assert any("内容未知" in item for item in rows["skills"]["diff"])
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE


def test_private_credential_change_does_not_reuse_previous_auth_verification():
    desired = _desired()
    desired["repository"]["authentication"] = "token"
    desired["repository"]["credential_revision"] = "new-credential"
    snapshot = _snapshot(desired)
    snapshot["repository"]["credential_revision"] = "old-credential"
    snapshot["repository"]["authentication_verified"] = True
    observation = _observation(desired)

    row = {item["key"]: item for item in equipment_rows(desired, observation, snapshot)}["git"]

    assert row["verified"] is None
    assert row["deployed"] is False
    assert any("认证未单独探测" in item for item in row["diff"])
    assert any("配置 revision" in item for item in row["diff"])


def test_unbound_repository_is_explicitly_unconfigured():
    desired = _desired()
    desired["repository"] = None
    observation = _observation()

    row = {item["key"]: item for item in equipment_rows(desired, observation, None)}["git"]

    assert row["configured"] is False
    assert row["deployed"] is False
    assert row["verified"] is None
    assert row["error"] == "未绑定 Git 仓库"
    assert any("不能调度" in item for item in row["diff"])
    assert health_status(observation, desired) == EnvironmentStatus.UNAVAILABLE

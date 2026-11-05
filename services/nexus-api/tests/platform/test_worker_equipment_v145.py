"""装备期望、节点观测对比与就绪投影契约测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.gateway.models  # noqa: F401 - 注册 GatewayRoute 表
from app.models import Base
from app.runtime.worker_readiness import (
    _skills_content_revision,
    mcp_endpoints_revision,
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


def test_unbound_repository_is_explicitly_unconfigured():
    desired = _desired()
    desired["repository"] = None
    observation = _observation()

    row = {item["key"]: item for item in equipment_rows(desired, observation)}["git"]

    assert row["configured"] is False
    assert row["deployed"] is False
    assert row["verified"] is None
    assert row["error"] == "未绑定 Git 仓库"

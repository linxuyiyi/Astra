"""Worker 直接保存 Git 配置，不创建或绑定独立仓库对象。"""

import asyncio

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import OpenCodeExecutionEnvironment
from app.runtime.repository import cipher


def worker_body(worker_id, category_id, **git):
    return {
        "id": worker_id,
        "name": "开发 Worker",
        "role": "开发",
        "task_types": ["ISSUE"],
        "category_ids": [category_id],
        "execution_environment": {
            "mode": "LOCAL",
            "name": "开发 Worker",
            "workspace_root": f"/tmp/workspaces/{worker_id}",
            "opencode_server_port": 19991,
            **git,
        },
    }


def test_worker_direct_git_configuration_and_encrypted_credential(monkeypatch):
    monkeypatch.setattr(get_settings(), "gateway_master_key", Fernet.generate_key().decode())
    token = "worker-git-test-token"
    with TestClient(app) as client:
        category = client.post("/api/task-categories", json={"name": "直接 Git 配置"}).json()["id"]
        body = worker_body(
            "worker-direct-git",
            category,
            repository_url="https://git.example/org/project.git",
            branch="release",
            git_token=token,
        )
        created = client.post("/api/agent-workers", json=body)
        assert created.status_code == 201, created.text
        env = created.json()["execution_environment"]
        assert env["repository_url"] == "https://git.example/org/project.git"
        assert env["branch"] == "release" and env["has_git_token"] is True
        assert "repository_id" not in env
        assert token not in created.text
        assert "git_token_ciphertext" not in created.text
        assert client.get("/api/repositories").status_code == 404
        assert client.post("/api/repositories", json={}).status_code == 404

        async def stored_token():
            async with SessionLocal() as session:
                stored = await session.get(OpenCodeExecutionEnvironment, env["id"])
                assert stored.git_token_ciphertext != token
                assert cipher().decrypt(stored.git_token_ciphertext) == token

        asyncio.run(stored_token())
        updated = client.put(
            "/api/agent-workers/worker-direct-git/environment",
            json={**body["execution_environment"], "git_token": None, "branch": "main"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["branch"] == "main" and updated.json()["has_git_token"]
        assert token not in updated.text
        asyncio.run(stored_token())
        cleared = client.put(
            "/api/agent-workers/worker-direct-git/environment",
            json={**body["execution_environment"], "repository_url": None, "git_token": None},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["repository_url"] is None
        assert cleared.json()["has_git_token"] is False


def test_worker_public_git_does_not_require_token_and_copy_has_own_workspace():
    with TestClient(app) as client:
        category = client.post("/api/task-categories", json={"name": "公共 Git 配置"}).json()["id"]
        created = client.post(
            "/api/agent-workers",
            json=worker_body(
                "worker-public-git",
                category,
                repository_url="https://git.example/public.git",
                branch="main",
            ),
        )
        assert created.status_code == 201, created.text
        env = created.json()["execution_environment"]
        assert env["has_git_token"] is False
        copied = client.post(
            "/api/agent-workers/worker-public-git/copy", json={"id": "worker-public-copy", "name": "副本"}
        )
        assert copied.status_code == 201, copied.text
        copied_env = copied.json()["employee"]["execution_environment"]
        assert copied_env["repository_url"] == env["repository_url"]
        assert copied_env["branch"] == "main"
        assert copied_env["workspace_root"] == "/tmp/workspaces/worker-public-copy"
        assert copied_env["opencode_server_port"] != 19991
        assert copied.json()["copied_task_count"] == 0

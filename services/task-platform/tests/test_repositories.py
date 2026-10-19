"""仓库 PAT 不进入 URL 和错误文本。复制 Worker 不带走密钥、端口、任务和工作区。"""

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import GitCredential, OpenCodeExecutionEnvironment
from app.runtime.repository import CloneError, clone_repository

PAT = "ghp_supersecretPAT1234567890"


def test_clone_keeps_the_token_out_of_the_command_and_the_askpass_script(tmp_path):
    seen: dict[str, object] = {}

    def runner(command, env):
        seen["command"] = command
        seen["script"] = Path(env["GIT_ASKPASS"]).read_text(encoding="utf-8")
        return 0, "", ""

    clone_repository(
        "https://git.example/org/repo.git",
        "main",
        str(tmp_path / "checkout"),
        PAT,
        runner=runner,
    )
    command = " ".join(seen["command"])
    assert PAT not in command
    assert PAT not in seen["script"]
    assert "git init" not in command
    assert "credential.helper=" in command
    assert "https://git.example/org/repo.git" in command

    def fail_if_called(_command, _env):
        raise AssertionError("含凭据的地址不应该发起 git")

    with pytest.raises(CloneError):
        clone_repository(
            f"https://{PAT}@git.example/org/repo.git",
            "main",
            str(tmp_path / "other"),
            PAT,
            runner=fail_if_called,
        )


def test_failed_git_clone_error_omits_the_pat(tmp_path):
    with pytest.raises(CloneError) as caught:
        clone_repository(
            "https://127.0.0.1:1/no/such.git",
            "main",
            str(tmp_path / "missing"),
            PAT,
        )
    assert PAT not in str(caught.value)


def test_copy_keeps_the_repository_and_drops_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "gateway_master_key", Fernet.generate_key().decode())
    with TestClient(app) as client:
        created = client.post(
            "/api/employees",
            json={
                "id": "worker-source",
                "name": "来源",
                "role": "build",
                "task_types": ["ISSUE"],
                "category_ids": ["testing-platform"],
                "execution_environment": {
                    "mode": "LOCAL",
                    "name": "本地",
                    "port": 2222,
                    "opencode_server_port": 19991,
                    "ssh_password": "ssh-secret-value",
                    "model_name": "nexus-gateway/demo",
                    "workspace_root": "/tmp/workspaces/source-worker",
                    "labels": ["runtime"],
                },
            },
        )
        assert created.status_code == 201, created.text
        leaked = client.post(
            "/api/repositories",
            json={"url": f"https://{PAT}@git.example/org/repo.git", "branch": "main", "token": PAT},
        )
        assert leaked.status_code == 422
        assert PAT not in leaked.text
        registered = client.post(
            "/api/repositories",
            json={"url": "https://git.example/org/repo.git", "branch": "release", "token": PAT},
        )
        assert registered.status_code == 201, registered.text
        assert PAT not in registered.text
        repository_id = registered.json()["id"]
        bound = client.post(
            "/api/employees/worker-source/repository",
            json={"repository_id": repository_id},
        )
        assert bound.status_code == 200, bound.text
        assert bound.json()["channel_health"]["repository_bound"] is True
        task = client.post(
            "/api/tasks",
            json={
                "task_type": "ISSUE",
                "category_id": "testing-platform",
                "title": "只留在来源",
                "description": "复制时不能带走",
                "acceptance_criteria": [{"content": "仍在来源"}],
                "employee_id": "worker-source",
                "repository_id": repository_id,
            },
        )
        assert task.status_code == 201, task.text
        assert task.json()["repository_id"] == repository_id
        assert PAT not in task.text

    import asyncio

    async def mark_refs():
        async with SessionLocal() as session:
            environment_id = bound.json()["execution_environment"]["id"]
            environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
            environment.skill_refs = ["review"]
            environment.mcp_refs = [{"name": "library", "url": "http://127.0.0.1/mcp"}]
            await session.commit()

    asyncio.run(mark_refs())

    with TestClient(app) as client:
        copied = client.post(
            "/api/employees/worker-source/copy",
            json={"id": "worker-copy", "name": "副本"},
        )
        assert copied.status_code == 201, copied.text
        assert PAT not in copied.text
        assert "ssh-secret-value" not in copied.text
        body = copied.json()
        assert body["source_task_count"] == 1
        assert body["copied_task_count"] == 0
        environment = body["employee"]["execution_environment"]
        assert environment["repository_id"] == repository_id
        assert environment["branch"] == "release"
        assert environment["model_name"] == "nexus-gateway/demo"
        assert environment["skill_refs"] == ["review"]
        assert environment["mcp_refs"] == [{"name": "library", "url": "http://127.0.0.1/mcp"}]
        assert environment["labels"] == ["runtime"]
        assert environment["workspace_root"] == "/tmp/workspaces/worker-copy"
        assert environment["port"] == 22
        assert environment["opencode_server_port"] != 19991
        assert environment["has_ssh_password"] is False
        other = client.post(
            "/api/repositories",
            json={"url": "https://git.example/org/other.git", "branch": "main", "token": "another-token"},
        )
        refused = client.post(
            "/api/employees/worker-source/repository",
            json={"repository_id": other.json()["id"]},
        )
        assert refused.status_code == 409
        again = client.post(
            "/api/employees/worker-source/repository",
            json={"repository_id": repository_id},
        )
        assert again.status_code == 200
        failed = client.post(
            f"/api/repositories/{repository_id}/clone",
            json={"destination": str(tmp_path / "missing")},
        )
        assert failed.status_code == 502
        assert PAT not in failed.text

    async def ciphertext():
        async with SessionLocal() as session:
            from sqlalchemy import select

            stored = await session.scalar(
                select(GitCredential).where(GitCredential.repository_id == repository_id)
            )
            return stored.token_ciphertext

    assert PAT not in asyncio.run(ciphertext())

"""Worker Git 凭据、工作区事实与 Worker 复制。

Git 凭据仅提供加解密入口；实际 Git 操作由 Worker 的部署和运行逻辑处理。
"""

from __future__ import annotations

import subprocess

from sqlalchemy import select

from app.config.settings import get_settings
from app.gateway.security import CredentialCipher
from nexus_api.db_models import skills
from nexus_api.resources import ResourceStore, data_dir


class WorkerCopyError(RuntimeError):
    pass


def cipher() -> CredentialCipher:
    return CredentialCipher(get_settings().gateway_master_key)


def read_base_commit(workspace: str) -> str:
    """读取已有仓库的 HEAD。不是仓库时返回空字符串，不执行 git init。"""

    if not workspace:
        return ""
    completed = subprocess.run(
        ["git", "-C", workspace, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def fresh_workspace(source: str, employee_id: str) -> str:
    if source.startswith("/"):
        parent = source.rsplit("/", 1)[0] or "/"
        return f"{parent}/{employee_id}"
    return f"./workspaces/{employee_id}"


async def load_worker_skills(session, names, *, include_assets=False) -> list[dict]:
    """Resolve enabled Worker bindings against the platform's active Skill versions."""
    if not names:
        return []
    store = ResourceStore(data_dir())
    rows = await session.execute(
        select(skills.c.id, skills.c.name, skills.c.version).where(
            skills.c.enabled == 1, skills.c.name.in_(names)
        )
    )
    result = []
    for row in rows:
        directory = store.version_dir(row.id, row.version)
        main = directory / "SKILL.md"
        if main.is_file():
            files = store.read_version_files(row.id, row.version) if include_assets else {}
            body = files["SKILL.md"] if include_assets else main.read_bytes()
            result.append({
                "name": row.name, "body": body.decode("utf-8"), "version": row.version, "files": files,
            })
    return result

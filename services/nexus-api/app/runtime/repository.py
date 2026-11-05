"""Worker Git 凭据、工作区事实与 Worker 复制。

Git 凭据仅提供加解密入口；实际 Git 操作由 Worker 的部署和运行逻辑处理。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.config.settings import get_settings
from app.gateway.security import CredentialCipher


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


def collect_skill_files(names: list[str], source: Path) -> list[dict[str, str]]:
    """从平台技能目录复制引用。不读取仓库里的 `.opencode/`。"""

    files: list[dict[str, str]] = []
    for name in names:
        path = source / name / "SKILL.md"
        if path.is_file():
            files.append({"name": name, "body": path.read_text(encoding="utf-8")})
    return files

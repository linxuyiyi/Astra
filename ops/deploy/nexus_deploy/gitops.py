from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import DeployConfig
from .errors import PrepareError
from .records import atomic_json, read_json, utcnow
from .runner import CommandResult, Runner


@dataclass
class Release:
    path: Path
    commit: str
    manifest: dict

    @property
    def python(self) -> Path:
        return self.path / "services" / "task-platform" / ".venv" / "bin" / "python"

    @property
    def nexus_api_python(self) -> Path:
        return self.path / "services" / "nexus-api" / ".venv" / "bin" / "python"

    @property
    def agent_python(self) -> Path:
        return self.path / "services" / "agent-service" / ".venv-agentscope" / "bin" / "python"

    @property
    def opencode_binary(self) -> Path:
        return self.path / "tools" / "node_modules" / ".bin" / "opencode"


class ReleaseBuilder:
    def __init__(
        self,
        config: DeployConfig,
        runner: Runner,
        *,
        python: str = "python3",
        npm: str = "npm",
        skip_web_build: bool = False,
        log: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.runner = runner
        self.python = python
        self.npm = npm
        self.skip_web_build = skip_web_build
        self.log = log

    def prepare(self) -> Release:
        release = self.config.release_dir()
        if self._is_complete(release):
            self.log(f"[prepare] 复用已完成的发布 {self.config.commit[:12]}")
            return Release(release, self.config.commit, read_json(release / "release.json"))

        if release.exists():
            if self._is_current(release):
                raise PrepareError(
                    f"当前发布目录不完整或被修改，拒绝原地重建：{release}；请部署新的 commit"
                )
            self.log(f"[prepare] 清理未完成发布 {release}")
            shutil.rmtree(release)
        release.parent.mkdir(parents=True, exist_ok=True)
        release.mkdir(mode=0o755)

        try:
            manifest = self._checkout(release)
            self._build(release)
        except Exception:
            if not self._is_current(release):
                shutil.rmtree(release, ignore_errors=True)
            raise
        manifest["prepared_at"] = utcnow()
        manifest["status"] = "complete"
        atomic_json(release / "release.json", manifest)
        return Release(release, self.config.commit, manifest)

    def _is_current(self, release: Path) -> bool:
        link = self.config.current_link
        try:
            return link.exists() and link.resolve() == release.resolve()
        except OSError:
            return False

    def _is_complete(self, release: Path) -> bool:
        if not (release / ".nexus-release-complete").is_file():
            return False
        manifest = read_json(release / "release.json")
        if manifest.get("commit") != self.config.commit:
            return False
        head = self.runner.run(["git", "-C", str(release), "rev-parse", "HEAD"])
        if head.returncode != 0 or head.stdout.strip() != self.config.commit:
            return False
        remote = self.runner.run(["git", "-C", str(release), "remote", "get-url", "origin"])
        if remote.returncode != 0 or remote.stdout.strip() != self.config.repo_url:
            self._reject_polluted(release, "远端地址不匹配")
            return False
        status = self.runner.run(["git", "-C", str(release), "status", "--porcelain"])
        if status.returncode != 0 or status.stdout.strip():
            self._reject_polluted(release, "工作树被修改")
            return False
        if manifest.get("prepared_at") and not self._artifacts_ok(release, manifest):
            self._reject_polluted(release, "构建产物不完整或版本不匹配")
            return False
        return True

    def _reject_polluted(self, release: Path, reason: str) -> None:
        if self._is_current(release):
            raise PrepareError(f"当前发布目录不可复用（{reason}）：{release}")

    def _artifacts_ok(self, release: Path, manifest: dict) -> bool:
        if not (release / "services" / "task-platform" / ".venv" / "bin" / "python").is_file():
            return False
        if manifest.get("web_built"):
            dist = release / "web" / "nexus" / "dist"
            if not (dist / "index.html").is_file():
                return False
            if not any((dist / "assets").glob("*.js")):
                return False
        return self._opencode_version_ok(release)

    def _opencode_version_ok(self, release: Path) -> bool:
        binary = self.config.effective("ASTRA_OPENCODE_BINARY", "").strip()
        path = Path(binary) if binary else release.opencode_binary
        if not path.is_file():
            return False
        result = self.runner.run([str(path), "--version"], timeout=30)
        if result.returncode != 0:
            return False
        match = re.search(r"(\d+\.\d+\.\d+)", result.stdout + result.stderr)
        if not match:
            return False
        expected = self.config.opencode_version.strip().removeprefix("v")
        return match.group(1) == expected

    def _checkout(self, release: Path) -> dict:
        commit = self.config.commit
        repo = self.config.repo_url
        commands = [
            ["git", "-C", str(release), "init", "-q"],
            ["git", "-C", str(release), "remote", "add", "origin", repo],
            ["git", "-C", str(release), "fetch", "-q", "--depth=1", "origin", commit],
            ["git", "-C", str(release), "checkout", "-q", "--detach", "FETCH_HEAD"],
        ]
        for command in commands:
            result = self.runner.run(command)
            self._require_git(result, f"Git 准备失败：{' '.join(command[:4])}")

        remote = self.runner.run(["git", "-C", str(release), "remote", "get-url", "origin"])
        if remote.returncode != 0 or remote.stdout.strip() != repo:
            raise PrepareError(
                f"部署仓库不匹配：配置 {repo!r}，实际 {remote.stdout.strip()!r}"
            )
        head = self.runner.run(["git", "-C", str(release), "rev-parse", "HEAD"])
        if head.returncode != 0 or head.stdout.strip() != commit:
            raise PrepareError(
                f"HEAD 与目标 commit 不一致：期望 {commit}，实际 {head.stdout.strip() or 'unknown'}"
            )
        exclude = release / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        with exclude.open("a", encoding="utf-8") as handle:
            for name in (
                "release.json",
                ".nexus-release-complete",
                "tools/package.json",
                "tools/package-lock.json",
                "tools/node_modules/",
            ):
                if name not in existing.splitlines():
                    handle.write(name + "\n")
        dirty = self.runner.run(["git", "-C", str(release), "status", "--porcelain"])
        if dirty.returncode != 0 or dirty.stdout.strip():
            raise PrepareError("发布工作树不干净，拒绝继续")

        metadata = read_json(release / "ops" / "deploy" / "release.json")
        compatible = metadata.get("database_compatible_with_previous", False) is True
        return {
            "product": "nexus",
            "version": self.config.product_version,
            "commit": commit,
            "repo_url": repo,
            "database_compatible_with_previous": compatible,
            "web_built": not self.skip_web_build,
        }

    def _build(self, release: Path) -> None:
        task_root = release / "services" / "task-platform"
        nexus_api_root = release / "services" / "nexus-api"
        agent_root = release / "services" / "agent-service"
        web_root = release / "web" / "nexus"

        self._ensure_venv(release, task_root / ".venv")
        self._run(
            [
                str(task_root / ".venv" / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "-q",
                "-e",
                str(task_root),
            ],
            task_root,
            "安装 Task Platform 依赖",
        )
        self._ensure_venv(release, nexus_api_root / ".venv")
        self._run(
            [
                str(nexus_api_root / ".venv" / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "-q",
                "-r",
                str(nexus_api_root / "requirements.txt"),
            ],
            nexus_api_root,
            "安装 Nexus API 依赖",
        )
        self._ensure_venv(release, agent_root / ".venv-agentscope")
        self._run(
            [
                str(agent_root / ".venv-agentscope" / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "-q",
                "-r",
                str(agent_root / "requirements.txt"),
            ],
            agent_root,
            "安装 Agent Service 依赖",
        )
        if not self.skip_web_build:
            self._run([self.npm, "ci", "--no-audit", "--no-fund"], web_root, "安装 Web 依赖")
            self._run([self.npm, "run", "build"], web_root, "构建 Web")
            dist = web_root / "dist"
            if not (dist / "index.html").is_file():
                raise PrepareError("Web 构建完成但 dist/index.html 不存在")
            if not any((dist / "assets").glob("*.js")):
                raise PrepareError("Web 构建完成但 dist/assets 缺少 JS 产物")
        self._install_opencode(release)
        if not self._opencode_version_ok(release):
            raise PrepareError(
                f"OpenCode 版本不是要求的 {self.config.opencode_version}，请检查安装或 ASTRA_OPENCODE_BINARY"
            )
        (release / ".nexus-release-complete").write_text(utcnow() + "\n", encoding="utf-8")

    def _install_opencode(self, release: Path) -> None:
        configured = self.config.effective("ASTRA_OPENCODE_BINARY", "").strip()
        if configured:
            self.log(f"[prepare] 使用已配置 OpenCode：{configured}")
            return
        tools = release / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                self.npm,
                "install",
                "--prefix",
                str(tools),
                "--no-audit",
                "--no-fund",
                f"opencode-ai@{self.config.opencode_version}",
            ],
            tools,
            "安装 OpenCode",
        )

    def _ensure_venv(self, release: Path, venv: Path) -> None:
        if venv.is_dir():
            return
        self._run([self.python, "-m", "venv", str(venv)], release, f"创建虚拟环境 {venv.name}")

    def _run(self, command: list[str], cwd: Path, title: str) -> None:
        self.log(f"[prepare] {title}")
        result = self.runner.run(command, cwd=cwd)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            tail = detail[-1] if detail else f"exit={result.returncode}"
            raise PrepareError(f"{title} 失败：{tail}")

    @staticmethod
    def _require_git(result: CommandResult, title: str) -> None:
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            tail = detail[-1] if detail else f"exit={result.returncode}"
            raise PrepareError(f"{title}：{tail}")

    def read_compatibility(self, release: Path) -> bool:
        value: object = read_json(release / "ops" / "deploy" / "release.json").get(
            "database_compatible_with_previous", False
        )
        return value is True


def verify_git_environment(config: DeployConfig, runner: Runner) -> None:
    """Preflight the repository and commit without touching the running release."""
    version = runner.run(["git", "--version"])
    if version.returncode != 0:
        raise PrepareError("缺少 git")
    with tempfile.TemporaryDirectory(prefix="nexus-git-probe-") as directory:
        probe_dir = Path(directory)
        commands = [
            ["git", "-C", str(probe_dir), "init", "-q"],
            ["git", "-C", str(probe_dir), "remote", "add", "origin", config.repo_url],
            ["git", "-C", str(probe_dir), "fetch", "-q", "--depth=1", "origin", config.commit],
            ["git", "-C", str(probe_dir), "checkout", "-q", "--detach", "FETCH_HEAD"],
        ]
        for command in commands:
            result = runner.run(command)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip().splitlines()
                tail = detail[-1] if detail else f"exit={result.returncode}"
                raise PrepareError(f"无法获取指定 commit {config.commit}：{tail}")
        head = runner.run(["git", "-C", str(probe_dir), "rev-parse", "HEAD"])
        if head.returncode != 0 or head.stdout.strip() != config.commit:
            raise PrepareError(f"仓库 HEAD 与目标 commit 不一致：{config.commit}")

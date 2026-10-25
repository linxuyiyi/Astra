import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.config.settings import Settings, get_settings
from app.domain import EnvironmentStatus, ExecutionMode
from app.executors.opencode_local_server import SERVE_LOCK_DIR
from app.executors.process import (
    local_command,
    process_group_options,
    resolve_executable,
)
from app.models import OpenCodeExecutionEnvironment
from app.runtime.ssh_client import SSHSession


@dataclass(slots=True)
class CheckItem:
    name: str
    passed: bool
    detail: str


def _capability_revision(environment: OpenCodeExecutionEnvironment) -> str:
    detail = environment.status_detail if isinstance(environment.status_detail, dict) else {}
    record = detail.get("capability") if isinstance(detail, dict) else None
    if not isinstance(record, dict):
        return ""
    return str(record.get("applied_revision") or "")


def environment_snapshot(environment: OpenCodeExecutionEnvironment) -> dict:
    return {
        "environment_id": environment.id,
        "runtime_engine": environment.runtime_engine or "opencode",
        "worker_channel": environment.worker_channel,
        "mode": environment.mode,
        "name": environment.name,
        "host": environment.host,
        "port": environment.port,
        "username": environment.username,
        "has_ssh_password": environment.has_ssh_password,
        "opencode_server_port": environment.opencode_server_port,
        "opencode_path": environment.opencode_path,
        "model_name": environment.model_name,
        "workspace_root": environment.workspace_root,
        "repository_id": environment.repository_id,
        "labels": list(environment.labels or []),
        "skill_refs": list(environment.skill_refs or []),
        "mcp_names": [str(x.get("name")) for x in environment.mcp_refs or []],
        "capability_revision": _capability_revision(environment),
    }


def environment_from_snapshot(snapshot: dict, employee_id: str) -> OpenCodeExecutionEnvironment:
    return OpenCodeExecutionEnvironment(
        id=snapshot.get("environment_id"),
        runtime_engine=snapshot.get("runtime_engine", "opencode"),
        worker_channel=snapshot.get("worker_channel", "ssh"),
        employee_id=employee_id,
        mode=snapshot["mode"],
        name=snapshot["name"],
        host=snapshot.get("host"),
        port=snapshot.get("port", 22),
        username=snapshot.get("username"),
        ssh_password=None,
        opencode_server_port=snapshot.get("opencode_server_port", 14096),
        opencode_path=snapshot["opencode_path"],
        model_name=snapshot.get("model_name"),
        workspace_root=snapshot["workspace_root"],
        labels=snapshot.get("labels", []),
        status=EnvironmentStatus.AVAILABLE,
    )


class ExecutionEnvironmentRuntime:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    async def open_session(self, environment: OpenCodeExecutionEnvironment) -> SSHSession:
        if not environment.host or not environment.username:
            raise RuntimeError("Linux 远程环境缺少 Host 或 Username")
        if not environment.ssh_password:
            raise RuntimeError("Linux 远程环境尚未配置 SSH 密码")
        session = SSHSession(
            host=environment.host,
            port=environment.port,
            username=environment.username,
            password=environment.ssh_password,
            connect_timeout=self.settings.ssh_connect_timeout_seconds,
        )
        await session.connect()
        return session

    async def run_remote(
        self,
        environment: OpenCodeExecutionEnvironment,
        command: str,
        *,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        session: SSHSession | None = None
        try:
            session = await self.open_session(environment)
            return await asyncio.wait_for(
                session.run_command(command),
                timeout=timeout or self.settings.opencode_timeout_seconds,
            )
        except TimeoutError:
            return 124, "", "远程命令执行超时"
        except Exception as exc:
            return 255, "", str(exc)
        finally:
            if session:
                await session.close()

    async def test(self, environment: OpenCodeExecutionEnvironment) -> list[CheckItem]:
        if environment.runtime_engine == "dsh":
            from app.runtime.worker_transport import WorkerCommandTransport

            if environment.mode != ExecutionMode.LINUX_REMOTE or environment.worker_channel != "worker":
                return [CheckItem("DSH Worker", False, "DSH 必须使用已连接的远程 Worker 通道")]
            try:
                client = WorkerCommandTransport(environment_id=environment.id)
                result = await client.run_command("dsh_health", {})
                detail = dict(environment.status_detail or {})
                detail["dsh"] = result
                environment.status_detail = detail
                return [CheckItem("DSH ACP", result.get("healthy") is True, "固定版本与装配已核对")]
            except Exception:
                return [CheckItem("DSH ACP", False, "DSH 健康检查失败，请核对 Worker 固定安装和私有配置")]
        if environment.mode == ExecutionMode.LOCAL:
            return await self._test_local(environment)
        return await self._test_remote(environment)

    async def _local_serve_identity(self, client, environment, port: int, health: dict) -> CheckItem:
        """Read /path and the owner file. Do not write the owner or start a server."""

        expected = self.settings.employee_workspace_path(environment.workspace_root)
        try:
            paths = await client.request("GET", "/path")
        except Exception as exc:
            return CheckItem("OpenCode Server", False, f"无法读取 Serve Workspace：{exc}")
        actual = Path(str((paths or {}).get("directory") or "")).resolve()
        if actual != expected:
            return CheckItem("OpenCode Server", False, "Serve 的启动 Workspace 与员工配置不一致")
        owner_path = Path(tempfile.gettempdir()) / SERVE_LOCK_DIR / f"{port}.json"
        if owner_path.is_file():
            try:
                owner = json.loads(owner_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return CheckItem("OpenCode Server", False, f"无法读取 Serve 归属：{exc}")
            if owner.get("employee_id") != environment.employee_id:
                return CheckItem("OpenCode Server", False, "该 Serve 属于另一员工或工作空间")
        version = health.get("version") if isinstance(health, dict) else None
        return CheckItem("OpenCode Server", True, f"健康：{port} · {version} · Workspace 匹配")

    async def _test_local(self, environment: OpenCodeExecutionEnvironment) -> list[CheckItem]:
        if self.settings.executor_type == "worker":
            return [
                CheckItem(
                    "执行环境",
                    False,
                    "当前部署只支持远程 Worker；请在 Worker 机器上创建 LINUX_REMOTE 环境",
                )
            ]
        binary = environment.opencode_path
        resolved = resolve_executable(binary)
        exists = bool(resolved)
        checks = [
            CheckItem(
                "Agent 命令存在",
                exists,
                f"已找到 {resolved}" if exists else f"未找到 {binary}",
            )
        ]
        if exists:
            try:
                process = await asyncio.create_subprocess_exec(
                    *local_command(str(resolved), ["--version"]),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    **process_group_options(),
                )
                output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
                started = process.returncode == 0
                detail = output.decode("utf-8", errors="replace").strip() or "命令执行完成"
            except (OSError, TimeoutError) as exc:
                started, detail = False, str(exc)
            checks.append(CheckItem("Agent 命令可启动", started, detail[:500]))
        else:
            checks.append(CheckItem("Agent 命令可启动", False, "未执行：Agent 命令不存在"))

        root = self.settings.employee_workspace_path(environment.workspace_root)
        try:
            if not root.is_dir():
                raise FileNotFoundError(f"Workspace 不存在：{root}")
            probe = root / f".astra-write-check-{uuid4().hex}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            workspace_ok, workspace_detail = True, f"可写：{root.resolve()}"
        except OSError as exc:
            workspace_ok, workspace_detail = False, str(exc)
        checks.append(CheckItem("Workspace 可用", workspace_ok, workspace_detail))
        if self.settings.executor_type == "opencode_server":
            from app.executors.opencode_server import OpenCodeServerClient

            port = environment.opencode_server_port
            if port in {self.settings.opencode_server_port}:
                checks.append(
                    CheckItem("OpenCode Server", False, "端口与平台公共/助手 Serve 冲突，请配置独立端口")
                )
            else:
                client = OpenCodeServerClient(self.settings, base_url=f"http://127.0.0.1:{port}")
                try:
                    health = await client.health()
                    checks.append(await self._local_serve_identity(client, environment, port, health))
                except Exception as exc:
                    import socket

                    with socket.socket() as probe:
                        probe.settimeout(1)
                        occupied = probe.connect_ex(("127.0.0.1", port)) == 0
                    checks.append(
                        CheckItem(
                            "OpenCode Server",
                            not occupied,
                            f"端口冲突或 Serve 异常：{exc}" if occupied else "未启动；任务执行时将自动启动",
                        )
                    )
        return checks

    async def _test_remote(self, environment: OpenCodeExecutionEnvironment) -> list[CheckItem]:
        """Worker-local health. This check does not open SSH or read the password."""

        from app.database import SessionLocal
        from app.runtime.worker_channel import read_worker_channel
        from app.runtime.worker_client import WorkerOpenCodeClient

        async with SessionLocal() as session:
            channel = await read_worker_channel(session, environment.id)
        if channel != "worker":
            return [CheckItem("OpenCode Server", False, "远程环境尚未安装 Worker")]
        try:
            health = await WorkerOpenCodeClient(self.settings, environment_id=environment.id).health()
        except Exception as exc:
            return [CheckItem("OpenCode Server", False, f"Worker 离线或 OpenCode 不可用：{exc}")]
        version = health.get("version") if isinstance(health, dict) else None
        return [
            CheckItem(
                "OpenCode Server",
                True,
                f"健康：{environment.opencode_server_port} · {version}",
            )
        ]


def checks_status(checks: list[CheckItem]) -> EnvironmentStatus:
    return (
        EnvironmentStatus.AVAILABLE
        if checks and all(item.passed for item in checks)
        else EnvironmentStatus.UNAVAILABLE
    )

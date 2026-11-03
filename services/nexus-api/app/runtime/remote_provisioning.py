"""远程 Worker 一键部署与轻量唤醒（无 systemd 单守护模型）。

保留 Python 3.11 / Node / OpenCode 制品引导（``remote_bootstrap``、``remote_artifacts``），
统一由 ``/opt/nexus/workers/{id}/ctl.sh`` 与 ``/opt/nexus/workers/start-all.sh`` 拉起单实例
``nexus-worker-agent`` 守护，并在上线推送就绪快照后直接宣告部署成功，不再做收尾通道切换。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import shlex
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.config.settings import get_settings
from app.config.worker_deployment import deployment_manifest, pip_install_options, pip_trusted_host
from app.database import SessionLocal
from app.domain import EnvironmentStatus, MaintenanceState
from app.gateway.models import GatewayKey, GatewayRoute
from app.gateway.probe import HttpGatewayProbeClient
from app.gateway.runtime import RedisRuntimeStore
from app.gateway.schemas import GatewayKeyCreate
from app.gateway.security import CredentialCipher, gateway_key_digest
from app.gateway.service import GatewayControlService
from app.gateway.urls import client_gateway_base
from app.models import (
    OpenCodeExecutionEnvironment,
    WorkerInstance,
)
from app.runtime.direct_urls import require_direct_url
from app.runtime.environment import ExecutionEnvironmentRuntime
from app.runtime.opencode_model import route_model_id
from app.runtime.redaction import redact_data, redact_text
from app.runtime.remote_artifacts import install_node, install_opencode
from app.runtime.remote_bootstrap import managed_python, prepare_python
from app.runtime.remote_package_config import configure_sources
from app.runtime.remote_workspace import configure_runtime, git_workspace
from app.runtime.repository import cipher, collect_skill_files
from app.runtime.worker_readiness import (
    build_desired_spec,
    compute_target_digest,
    evaluate_worker_readiness,
)
from app.worker_agent import __version__ as WORKER_AGENT_VERSION
from app.worker_agent.jsonc import loads_jsonc

PROVIDER_ID = "nexus-gateway"
WORKER_SECRET_DIR = "/etc/nexus"
START_ALL_PATH = "/opt/nexus/workers/start-all.sh"


def worker_install_dir(environment_id: int) -> str:
    return f"/opt/nexus/workers/{environment_id}"


def worker_state_dir(environment_id: int) -> str:
    return f"{worker_install_dir(environment_id)}/state"


def worker_log_dir(environment_id: int) -> str:
    return f"{worker_install_dir(environment_id)}/logs"


def worker_ctl_path(environment_id: int) -> str:
    return f"{worker_install_dir(environment_id)}/ctl.sh"


def agent_venv(environment_id: int) -> str:
    return f"{worker_install_dir(environment_id)}/venv"


def agent_python(environment_id: int) -> str:
    return f"{agent_venv(environment_id)}/bin/python"


def git_token_path(environment_id: int) -> str:
    return f"{WORKER_SECRET_DIR}/git-{environment_id}.token"


def git_askpass_path(environment_id: int) -> str:
    return f"{WORKER_SECRET_DIR}/git-{environment_id}-askpass"


def credential_revision(ciphertext: str) -> str:
    return hashlib.sha256(ciphertext.encode("utf-8")).hexdigest()[:16]


def safe_git_environment(environment_id: int) -> dict[str, str]:
    """The only safe Git variables shared by OpenCode and Worker Agent."""

    return {
        "GIT_ASKPASS": git_askpass_path(environment_id),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
    }


def git_askpass_script(environment_id: int) -> str:
    return (
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  *Username*) printf '%s\\n' git ;;\n"
        f"  *) cat '{git_token_path(environment_id)}' ;;\n"
        "esac\n"
    )


def render_worker_ctl_script(environment_id: int, *, port: int) -> str:
    """无 systemd 标准单入口脚本：通过 Python fcntl.flock 管理 worker.lock 生命周期。"""

    install_dir = worker_install_dir(environment_id)
    python_bin = agent_python(environment_id)
    env_file = f"{WORKER_SECRET_DIR}/worker-{environment_id}.env"
    state_dir = worker_state_dir(environment_id)
    log_dir = worker_log_dir(environment_id)
    lock_file = f"{state_dir}/worker.lock"
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f"INSTALL_DIR={shlex.quote(install_dir)}\n"
        f"PYTHON={shlex.quote(python_bin)}\n"
        f"ENV_FILE={shlex.quote(env_file)}\n"
        f"STATE_DIR={shlex.quote(state_dir)}\n"
        f"LOG_DIR={shlex.quote(log_dir)}\n"
        f"LOCK_FILE={shlex.quote(lock_file)}\n"
        f"ENV_ID={int(environment_id)}\n"
        f"PORT={int(port)}\n"
        'ACTION="${1:-status}"\n'
        'mkdir -p -m 700 "$STATE_DIR" "$LOG_DIR"\n'
        "\n"
        "is_locked() {\n"
        '  "$PYTHON" -I - "$LOCK_FILE" <<\'PY\'\n'
        "import errno, fcntl, os, sys\n"
        "path = sys.argv[1]\n"
        "if not os.path.exists(path):\n"
        "    raise SystemExit(1)\n"
        "fd = os.open(path, os.O_RDWR)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError as exc:\n"
        "    if exc.errno in (errno.EACCES, errno.EAGAIN):\n"
        "        raise SystemExit(0)\n"
        "    raise\n"
        "finally:\n"
        "    os.close(fd)\n"
        "raise SystemExit(1)\n"
        "PY\n"
        "}\n"
        "\n"
        "stop_daemon() {\n"
        "  if ! is_locked; then\n"
        '  "$PYTHON" -c "from app.worker_agent.supervisor import cleanup_orphan_opencode; '
        'cleanup_orphan_opencode($ENV_ID)" >/dev/null 2>&1 || true\n'
        "    printf 'already_stopped\\n'\n"
        "    return 0\n"
        "  fi\n"
        '  "$PYTHON" -I - "$LOCK_FILE" "$ENV_ID" <<\'PY\'\n'
        "import errno, fcntl, os, pathlib, signal, sys, time\n"
        "from app.worker_agent.supervisor import cleanup_orphan_opencode\n"
        "lock_path, env_id = pathlib.Path(sys.argv[1]), int(sys.argv[2])\n"
        "\n"
        "def locked() -> bool:\n"
        "    if not lock_path.exists():\n"
        "        return False\n"
        "    fd = os.open(lock_path, os.O_RDWR)\n"
        "    try:\n"
        "        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "        return False\n"
        "    except OSError as exc:\n"
        "        if exc.errno in (errno.EACCES, errno.EAGAIN):\n"
        "            return True\n"
        "        raise\n"
        "    finally:\n"
        "        os.close(fd)\n"
        "\n"
        "def holder_pids() -> list[int]:\n"
        "    found: list[int] = []\n"
        "    try:\n"
        "        target_stat = lock_path.stat()\n"
        "    except OSError:\n"
        "        return found\n"
        "    proc = pathlib.Path('/proc')\n"
        "    if proc.is_dir():\n"
        "        for entry in proc.iterdir():\n"
        "            if not entry.name.isdigit() or int(entry.name) == os.getpid():\n"
        "                continue\n"
        "            fd_dir = entry / 'fd'\n"
        "            try:\n"
        "                for desc in fd_dir.iterdir():\n"
        "                    st = desc.stat()\n"
        "                    if st.st_dev == target_stat.st_dev and st.st_ino == target_stat.st_ino:\n"
        "                        found.append(int(entry.name))\n"
        "                        break\n"
        "            except OSError:\n"
        "                continue\n"
        "    return found\n"
        "\n"
        "pids = holder_pids()\n"
        "for pid in pids:\n"
        "    try:\n"
        "        os.kill(pid, signal.SIGTERM)\n"
        "    except OSError:\n"
        "        pass\n"
        "deadline = time.monotonic() + 8.0\n"
        "while locked() and time.monotonic() < deadline:\n"
        "    time.sleep(0.1)\n"
        "if locked():\n"
        "    for pid in holder_pids() or pids:\n"
        "        try:\n"
        "            os.kill(pid, signal.SIGKILL)\n"
        "        except OSError:\n"
        "            pass\n"
        "    deadline2 = time.monotonic() + 2.0\n"
        "    while locked() and time.monotonic() < deadline2:\n"
        "        time.sleep(0.05)\n"
        "cleanup_orphan_opencode(env_id)\n"
        "if locked():\n"
        "    raise SystemExit('守护进程未能在时限内释放 worker.lock')\n"
        "PY\n"
        "  printf 'stopped\\n'\n"
        "}\n"
        "\n"
        "start_daemon() {\n"
        "  if is_locked; then\n"
        "    printf 'already_running\\n'\n"
        "    return 0\n"
        "  fi\n"
        '  nohup "$PYTHON" -m app.worker_agent --env-file "$ENV_FILE" '
        '>> "$LOG_DIR/agent.log" 2>&1 < /dev/null &\n'
        '  "$PYTHON" -I - "$LOCK_FILE" "$PORT" "$LOG_DIR/agent.log" <<\'PY\'\n'
        "import errno, fcntl, json, os, pathlib, sys, time, urllib.request\n"
        "lock_path = pathlib.Path(sys.argv[1])\n"
        "port = int(sys.argv[2])\n"
        "log_path = pathlib.Path(sys.argv[3])\n"
        "\n"
        "def locked() -> bool:\n"
        "    if not lock_path.exists():\n"
        "        return False\n"
        "    fd = os.open(lock_path, os.O_RDWR)\n"
        "    try:\n"
        "        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "        return False\n"
        "    except OSError as exc:\n"
        "        return exc.errno in (errno.EACCES, errno.EAGAIN)\n"
        "    finally:\n"
        "        os.close(fd)\n"
        "\n"
        "def healthy() -> bool:\n"
        "    try:\n"
        "        with urllib.request.urlopen(\n"
        "            f'http://127.0.0.1:{port}/global/health', timeout=0.8\n"
        "        ) as resp:\n"
        "            data = json.loads(resp.read(65536).decode('utf-8'))\n"
        "            return isinstance(data, dict) and data.get('healthy') is True\n"
        "    except Exception:\n"
        "        return False\n"
        "\n"
        "lock_deadline = time.monotonic() + 3.0\n"
        "while not locked() and time.monotonic() < lock_deadline:\n"
        "    time.sleep(0.05)\n"
        "if not locked():\n"
        "    tail = ''\n"
        "    if log_path.is_file():\n"
        "        lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()[-20:]\n"
        "        tail = '\\n'.join(lines)\n"
        "    raise SystemExit('Worker Agent 守护启动后未能持有排他锁' + (f'：{tail}' if tail else ''))\n"
        "\n"
        "health_deadline = time.monotonic() + 12.0\n"
        "while time.monotonic() < health_deadline:\n"
        "    if not locked():\n"
        "        raise SystemExit('Worker Agent 守护在启动期间退出')\n"
        "    if healthy():\n"
        "        print('started')\n"
        "        raise SystemExit(0)\n"
        "    time.sleep(0.2)\n"
        "print('started_pending_health')\n"
        "PY\n"
        "}\n"
        "\n"
        'case "$ACTION" in\n'
        "  start)\n"
        "    start_daemon\n"
        "    ;;\n"
        "  stop)\n"
        "    stop_daemon\n"
        "    ;;\n"
        "  restart)\n"
        "    stop_daemon >/dev/null\n"
        "    start_daemon\n"
        "    ;;\n"
        "  status)\n"
        "    if is_locked; then\n"
        "      printf 'running\\n'\n"
        "      exit 0\n"
        "    fi\n"
        "    printf 'stopped\\n'\n"
        "    exit 1\n"
        "    ;;\n"
        "  *)\n"
        "    printf 'Unsupported action: %s\\n' \"$ACTION\" >&2\n"
        "    exit 2\n"
        "    ;;\n"
        "esac\n"
    )


def render_start_all_script() -> str:
    """容器/宿主机无 systemd 启动钩子可调用的幂等聚合拉起脚本。"""

    return (
        "#!/bin/sh\n"
        "set -u\n"
        "for ctl in /opt/nexus/workers/*/ctl.sh; do\n"
        '  if [ -x "$ctl" ]; then\n'
        '    "$ctl" start || true\n'
        "  fi\n"
        "done\n"
    )


async def guard_environment_write(session, environment):
    """锁定环境行并拒绝在活跃维护期间并发修改/删除环境配置。"""

    if environment is None:
        return
    from app.models import WorkerMaintenanceOperation
    from app.runtime.maintenance import ACTIVE_STATES, lock_environment_row

    try:
        await lock_environment_row(session, environment)
    except OperationalError as exc:
        await session.rollback()
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise HTTPException(409, "环境正在被其它事务更新，请稍后重试") from exc
        raise
    active = await session.scalar(
        select(WorkerMaintenanceOperation.id)
        .where(
            WorkerMaintenanceOperation.environment_id == environment.id,
            WorkerMaintenanceOperation.state.in_(ACTIVE_STATES),
        )
        .with_for_update()
        .limit(1)
    )
    if active is not None:
        await session.rollback()
        raise HTTPException(409, "远程环境正在维护，请等待完成或先取消当前操作")


def _wheel_digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def agent_wheel() -> tuple[str, bytes]:
    """Reproducible Worker Agent wheel: same source always yields the same bytes."""

    dist = f"nexus_worker_agent-{WORKER_AGENT_VERSION}"
    app_root = Path(__file__).resolve().parents[1]
    names = [
        "app/__init__.py",
        "app/worker_gateway/__init__.py",
        "app/worker_gateway/protocol.py",
        "app/worker_gateway/heartbeat.py",
        "app/runtime/__init__.py",
        "app/runtime/redaction.py",
    ]
    names.extend(
        str(path.relative_to(app_root.parent))
        for path in sorted((app_root / "worker_agent").rglob("*.py"))
    )
    files = sorted((name, (app_root.parent / name).read_bytes()) for name in set(names))
    info = f"{dist}.dist-info"
    metadata = (
        "Metadata-Version: 2.1\n"
        "Name: nexus-worker-agent\n"
        f"Version: {WORKER_AGENT_VERSION}\n"
        "Summary: Nexus Worker Agent\n"
        "Requires-Python: >=3.11\n"
        "Requires-Dist: httpx>=0.27,<1\n"
        "Requires-Dist: websockets>=12\n"
    ).encode()
    wheel_meta = b"Wheel-Version: 1.0\nGenerator: nexus-provision\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files.extend([(f"{info}/METADATA", metadata), (f"{info}/WHEEL", wheel_meta)])
    record = "".join(f"{name},{_wheel_digest(data)},{len(data)}\n" for name, data in files)
    record += f"{info}/RECORD,,\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            info_entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info_entry.compress_type = zipfile.ZIP_DEFLATED
            info_entry.external_attr = 0o644 << 16
            archive.writestr(info_entry, data)
        info_entry = zipfile.ZipInfo(f"{info}/RECORD", date_time=(2020, 1, 1, 0, 0, 0))
        info_entry.compress_type = zipfile.ZIP_DEFLATED
        info_entry.external_attr = 0o644 << 16
        archive.writestr(info_entry, record.encode())
    return f"{dist}-py3-none-any.whl", buffer.getvalue()


def managed_config(
    *,
    config_path,
    model,
    models,
    gateway_url,
    gateway_key,
    mcp_url,
    skills=None,
    mcp_refs=None,
):
    mcp = {"type": "remote", "url": mcp_url, "enabled": True, "oauth": False}
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Nexus API 中转",
        "options": {"baseURL": client_gateway_base(gateway_url), "apiKey": gateway_key},
        "models": {name: {"name": name} for name in models},
    }
    return {
        "config_path": config_path,
        "model": f"{PROVIDER_ID}/{route_model_id(model)}",
        "provider": provider,
        "mcp": mcp,
        "mcp_refs": list(mcp_refs or []),
        "skills": list(skills or []),
    }


class RemoteProvisioner:
    def __init__(self, environment, operation, *, runtime=None, lease=None):
        self.environment = environment
        self.operation = operation
        self.operation_id = operation["id"] if isinstance(operation, dict) else operation.id
        self.action = (
            operation.get("action", "deploy")
            if isinstance(operation, dict)
            else operation.action
        )
        self.lease = lease
        self.settings = get_settings()
        self.runtime = runtime or ExecutionEnvironmentRuntime(self.settings)
        self.steps: list[dict] = []
        self.secrets = [environment.ssh_password] if getattr(environment, "ssh_password", None) else []
        self.ssh = None
        self.new_key_id = None
        self.old_key_id = None
        self.key_applied = False
        self.target_model = environment.model_name
        self.target_binary = environment.opencode_path
        self.config_path = None
        self.home: str | None = None
        self.git_token: str | None = None
        self.git_credential_revision: str | None = None
        self.installer_meta: dict = {}
        self.install_lock = None
        self.verified_evaluation: dict | None = None

    def _agent_interpreter(self) -> str:
        if getattr(self, "python_interpreter", None):
            return self.python_interpreter
        configured = (self.settings.remote_python or "").strip()
        return configured or managed_python(self.environment.id)

    def _probe_interpreter(self) -> str:
        return (
            agent_python(self.environment.id)
            if self.action == "restart"
            else self._agent_interpreter()
        )

    def _gateway_service(self, db, runtime=None):
        return GatewayControlService(
            db,
            cipher=CredentialCipher(self.settings.gateway_master_key),
            probe_client=HttpGatewayProbeClient(self.settings.gateway_probe_timeout_seconds),
            runtime=runtime,
        )

    def redact(self, value):
        text = str(value)
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[已隐藏]")
        text = re.sub(r"agw_[A-Za-z0-9_-]+", "[已隐藏]", text)
        text = re.sub(r"Bearer\s+\S+", "Bearer [已隐藏]", text)
        return text[-1500:]

    async def step(self, emit, name, state, detail=""):
        item = {
            "name": name,
            "state": state,
            "passed": state == "passed",
            "detail": self.redact(detail),
        }
        self.steps = [s for s in self.steps if s["name"] != name] + [item]
        await emit({"type": "step", **item})

    async def _acquire_install_lock(self):
        if self.install_lock is not None:
            return
        root = "/opt/nexus/workers/.locks"
        await self._command(shlex.join(["install", "-d", "-m", "700", root]))
        canonical = await self._command(
            shlex.join(["realpath", "-m", self.environment.workspace_root])
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:24]
        self.install_lock = await self.ssh.hold_lock(
            [
                f"{root}/deploy-env-{self.environment.id}.lock",
                f"{root}/workspace-{digest}.lock",
            ]
        )

    async def _ensure_safe(self, *, allow_occupied: bool = False):
        if self.install_lock is not None and not self.install_lock.is_active():
            raise RuntimeError("远端部署锁已失效，停止修改")
        if self.lease is None:
            return
        from app.runtime.maintenance import MaintenanceCanceled, MaintenanceLeaseLost

        await self.lease.check_canceled()
        await self.lease.assert_safe_to_disrupt(
            self.environment.id,
            self.environment.employee_id,
            allow_occupied=allow_occupied,
        )
        operation = await self.lease.snapshot()
        if operation is None or operation.attempt != self.lease.attempt:
            raise MaintenanceLeaseLost("维护租约已失效")
        if operation.cancel_requested:
            raise MaintenanceCanceled("操作已请求取消")

    async def _command(self, command, timeout=30):
        rc, out, err = await asyncio.wait_for(self.ssh.run_command(command), timeout)
        if rc:
            raise RuntimeError(
                self.redact(err.strip() or out.strip() or f"远程命令退出码 {rc}")
            )
        return out.strip()

    @staticmethod
    def _assert_native_idle(states) -> None:
        if not isinstance(states, dict):
            raise RuntimeError("无法确认 OpenCode 原生 Session 状态，不能执行危险维护")
        for item in states.values():
            if not isinstance(item, dict) or item.get("type") not in {
                "idle",
                "busy",
                "retry",
            }:
                raise RuntimeError("无法确认 OpenCode 原生 Session 状态，不能执行危险维护")
            if item["type"] in {"busy", "retry"}:
                raise RuntimeError("OpenCode 存在正在执行的 Session，请先停止执行再维护环境")

    async def _native_idle(self, *, strict: bool = False, interpreter: str | None = None):
        port = int(self.environment.opencode_server_port)
        query = urlencode({"directory": self.environment.workspace_root})
        url = f"http://127.0.0.1:{port}/session/status?{query}"
        script = (
            "import socket,urllib.request\n"
            f"port={port}\n"
            "probe=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
            "probe.settimeout(1.0)\n"
            "try:\n"
            "    probe.connect(('127.0.0.1',port))\n"
            "except ConnectionRefusedError:\n"
            "    print('NEXUS_STOPPED')\n"
            "    raise SystemExit(0)\n"
            "except OSError:\n"
            "    print('NEXUS_UNKNOWN')\n"
            "    raise SystemExit(0)\n"
            "finally:\n"
            "    probe.close()\n"
            f"url={json.dumps(url)}\n"
            "try:\n"
            "    print(urllib.request.urlopen(url, timeout=3).read().decode())\n"
            "except Exception:\n"
            "    print('NEXUS_UNKNOWN')\n"
        )
        out = (
            await self._command(
                f"{shlex.quote(interpreter or self._probe_interpreter())} -c {shlex.quote(script)}"
            )
        ).strip()
        if out == "NEXUS_STOPPED":
            return False
        if out == "NEXUS_UNKNOWN" or not out:
            if strict:
                raise RuntimeError("无法确认 OpenCode 原生 Session 状态，不能重启")
            return False
        try:
            states = json.loads(out)
        except json.JSONDecodeError:
            if strict:
                raise RuntimeError("无法确认 OpenCode 原生 Session 状态，不能重启") from None
            return False
        self._assert_native_idle(states)
        return True

    async def _bootstrap_idle(self, *, interpreter_ready: bool):
        if interpreter_ready:
            readable = await self._native_idle(strict=True)
            if readable:
                await self._require_owned_listener(interpreter=self._agent_interpreter())
            return
        installed = agent_python(self.environment.id)
        if await self._command(f"test -x {shlex.quote(installed)} && printf yes || true") == "yes":
            readable = await self._native_idle(strict=True, interpreter=installed)
            if readable:
                await self._require_owned_listener(interpreter=installed)
            return
        await self._ensure_safe()
        probe = r"""
port="$1"
files=(/proc/net/tcp)
if test -e /proc/net/tcp6; then files+=(/proc/net/tcp6); fi
inodes=$(awk -v port="$port" '$4 == "0A" && $2 ~ ":" port "$" {print $10}' "${files[@]}") || exit 1
if test -z "$inodes"; then printf CLEAR; exit 0; fi
pids=()
starts=()
for proc in /proc/[0-9]*; do
  found=false
  for fd in "$proc"/fd/*; do
    target=$(readlink "$fd" 2>/dev/null) || continue
    for inode in $inodes; do
      if test "$target" = "socket:[$inode]"; then found=true; break; fi
    done
    if $found; then break; fi
  done
  if ! $found; then continue; fi
  exe=$(readlink "$proc/exe") || exit 1
  mapfile -d '' -t args < "$proc/cmdline"
  name=${exe##*/}
  is_opencode=false
  case "$name" in
    opencode|opencode.exe|opencode-*) is_opencode=true ;;
    node|bun)
      for arg in "${args[@]}"; do
        if test "${arg##*/}" = opencode; then is_opencode=true; fi
      done ;;
  esac
  serve=false
  for arg in "${args[@]}"; do if test "$arg" = serve; then serve=true; fi; done
  if ! $is_opencode || ! $serve; then printf FOREIGN; exit 0; fi
  pids+=("${proc##*/}")
  starts+=("$(awk '{print $22}' "$proc/stat")")
done
if test "${#pids[@]}" = 0; then printf UNKNOWN; exit 0; fi
for i in "${!pids[@]}"; do
  pid=${pids[$i]}
  if test -e "/proc/$pid/stat"; then
    test "$(awk '{print $22}' "/proc/$pid/stat")" = "${starts[$i]}" || exit 1
    kill -TERM "$pid" || exit 1
  fi
done
for attempt in {1..50}; do
  if ! awk -v port="$port" '$4 == "0A" && $2 ~ ":" port "$" {found=1}
    END {exit found ? 0 : 1}' "${files[@]}"; then printf STOPPED; exit 0; fi
  sleep 0.1
done
printf STILL_LISTENING
"""
        port = f"{int(self.environment.opencode_server_port):04X}"
        state = await self._command("bash -c " + shlex.quote(probe) + " -- " + shlex.quote(port))
        if state not in {"CLEAR", "STOPPED"}:
            raise RuntimeError("端口监听进程不是可停止的历史 OpenCode，或停止后端口仍被占用")

    async def _require_owned_listener(self, *, interpreter: str):
        lock_file = f"{worker_state_dir(self.environment.id)}/worker.lock"
        env_id = str(int(self.environment.id))
        script = (
            "import errno,fcntl,os,pathlib,sys\n"
            "lock_path,env_id=pathlib.Path(sys.argv[1]),sys.argv[2]\n"
            "if lock_path.exists():\n"
            "    fd=os.open(lock_path,os.O_RDWR)\n"
            "    try:\n"
            "        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
            "    except OSError as exc:\n"
            "        if exc.errno in (errno.EACCES,errno.EAGAIN):\n"
            "            print('OWNED');raise SystemExit(0)\n"
            "    finally:\n"
            "        os.close(fd)\n"
            "proc=pathlib.Path('/proc')\n"
            "if proc.is_dir():\n"
            "    for entry in proc.iterdir():\n"
            "        if not entry.name.isdigit():continue\n"
            "        try:\n"
            "            raw=(entry/'environ').read_bytes()\n"
            "        except OSError:\n"
            "            continue\n"
            "        items=set(raw.split(b'\\x00'))\n"
            "        if (f'NEXUS_WORKER_ENV_ID={env_id}'.encode() in items\n"
            "            or f'NEXUS_WORKER_ENVIRONMENT_ID={env_id}'.encode() in items):\n"
            "            print('OWNED');raise SystemExit(0)\n"
            "print('FOREIGN')\n"
        )
        verdict = await self._command(
            shlex.join([interpreter, "-I", "-c", script, lock_file, env_id])
        )
        if verdict.strip() != "OWNED":
            raise RuntimeError("OpenCode 端口已被非当前环境的未知进程占用，拒绝覆盖部署")

    async def _gateway_key(self, config_path, models):
        content = await self.ssh.read_file(config_path)
        raw = None
        if content:
            text = content.decode() if isinstance(content, bytes) else content
            try:
                config = loads_jsonc(text)
                raw = (
                    config.get("provider", {})
                    .get(PROVIDER_ID, {})
                    .get("options", {})
                    .get("apiKey")
                )
            except (ValueError, AttributeError):
                raise RuntimeError("已有全局配置不是有效 JSON，请人工合并后重试") from None
        async with SessionLocal() as db:
            if raw and isinstance(raw, str):
                key = await db.scalar(
                    select(GatewayKey).where(
                        GatewayKey.key_digest == gateway_key_digest(raw),
                        GatewayKey.owner_id == self.environment.employee_id,
                        GatewayKey.deleted_at.is_(None),
                    )
                )
                if key:
                    self.old_key_id = key.id
                    if (
                        key.enabled
                        and not key.revoked_at
                        and (not key.expires_at or key.expires_at > datetime.now(UTC))
                    ):
                        if not key.allowed_models or set(models).issubset(key.allowed_models):
                            self.secrets.append(raw)
                            return raw
            key, raw = await self._gateway_service(db).create_key(
                GatewayKeyCreate(
                    name=f"Agent Worker {self.environment.employee_id}",
                    owner_id=self.environment.employee_id,
                ),
                actor="employee-deployment",
                actor_role="admin",
            )
            self.new_key_id = key.id
        self.secrets.append(raw)
        return raw

    async def _install(self, emit):
        await self.step(emit, "部署配置", "running")
        await self._ensure_safe()
        async with SessionLocal() as db:
            models = list(
                await db.scalars(
                    select(GatewayRoute.model)
                    .where(GatewayRoute.enabled.is_(True), GatewayRoute.model_type == "CHAT")
                    .order_by(GatewayRoute.model)
                )
            )
        if not self.settings.gateway_enabled or not models:
            raise RuntimeError("请先在 API 中转配置至少一个启用的模型路由")
        selected = models[0]
        if self.environment.model_name:
            selected = route_model_id(self.environment.model_name)
        if selected not in models:
            raise RuntimeError("员工模型未匹配启用的中转路由，请留空或选择 nexus-gateway/<路由模型>")
        home = (await self._command('printf "%s" "$HOME"')).splitlines()[0]
        if not home.startswith("/"):
            raise RuntimeError("无法确定远程用户目录")
        self.home = home
        config_path = f"/etc/nexus/opencode/{self.environment.id}/opencode.jsonc"
        self.config_path = config_path
        raw = await self._gateway_key(config_path, models)
        gateway_url, mcp_url = self._direct_endpoints()
        self.target_model = f"{PROVIDER_ID}/{selected}"
        self.target_binary = f"{home}/.local/share/nexus/workers/{self.environment.id}/bin/opencode"
        cache = f"{home}/.cache/nexus-provision"
        await self._command(f"install -d -m 700 {shlex.quote(cache)}")
        stem = f"{cache}/{self.operation_id}"
        skill_source = Path(self.settings.workspace_root) / "worker-skills"
        payload = managed_config(
            config_path=config_path,
            model=selected,
            models=models,
            gateway_url=gateway_url,
            gateway_key=raw,
            mcp_url=mcp_url,
            skills=collect_skill_files(list(self.environment.skill_refs or []), skill_source),
            mcp_refs=list(self.environment.mcp_refs or []),
        )
        payload["deployment"] = deployment_manifest(self.settings)
        payload.update(
            {
                "action": "update" if self.action == "update" else "deploy",
                "environment_id": self.environment.id,
                "workspace_root": self.environment.workspace_root,
                "opencode_binary": self.target_binary,
                "opencode_server_port": int(self.environment.opencode_server_port),
                "repository": {"url": "", "branch": self.environment.branch or "main"},
            }
        )
        token_file = ""
        if self.environment.repository_url:
            ciphertext = self.environment.git_token_ciphertext
            if ciphertext:
                token = cipher().decrypt(ciphertext)
                self.secrets.append(token)
                self.git_token = token
                self.git_credential_revision = credential_revision(ciphertext)
                token_file = stem + ".token"
            payload["repository"] = {
                "url": self.environment.repository_url,
                "branch": self.environment.branch or "main",
            }
        manifest = deployment_manifest(self.settings)
        await self.step(emit, "部署配置", "passed", "已准备员工专属网关与图书馆配置")
        await self.step(emit, "Node.js 运行环境", "running")
        facts = self.remote_facts
        arch = "x64" if facts["arch"] == "x86_64" else "arm64"
        node_root, node_changed = await install_node(
            self, manifest, home, arch=arch, libc=facts["libc"]
        )
        self.node_binary = node_root + "/bin/node"
        await self.step(emit, "Node.js 运行环境", "passed", manifest["node_version"])
        await self.step(emit, "包管理器全局镜像", "running")
        await configure_sources(self, manifest, home, node_root)
        await self.step(emit, "包管理器全局镜像", "passed", "已保留或写入部署用户 pip/npm 配置")
        await self.step(emit, "OpenCode 安装", "running")
        prefix = f"{home}/.local/share/nexus/workers/{self.environment.id}/opencode"
        package_changed = await install_opencode(
            self, manifest, prefix, arch=arch, musl=facts["libc"] == "musl"
        )
        await self._command(shlex.join(["mkdir", "-p", str(Path(self.target_binary).parent)]))
        current_link = await self._command(
            shlex.join(["readlink", self.target_binary]) + " || true"
        )
        link_changed = current_link != prefix + "/bin/opencode"
        if link_changed:
            backup = self.target_binary + ".nexus-backup-" + self.operation_id
            await self._ensure_safe()
            await self._command(
                f"if test -e {shlex.quote(self.target_binary)} "
                f"&& ! test -L {shlex.quote(self.target_binary)}; "
                f"then test ! -e {shlex.quote(backup)} "
                f"&& mv {shlex.quote(self.target_binary)} {shlex.quote(backup)} "
                f"|| exit 1; fi; "
                + shlex.join(["ln", "-sfn", prefix + "/bin/opencode", self.target_binary])
            )
        await self.step(emit, "OpenCode 安装", "passed", manifest["opencode_version"])
        await self._native_idle(strict=True)
        await self.step(emit, "Git 工作区", "running")
        try:
            if token_file:
                await self.ssh.write_file(token_file, self.git_token.encode(), mode=0o600)
            repository, repository_changed = await git_workspace(
                self,
                url=payload["repository"]["url"],
                branch=payload["repository"]["branch"],
                token_path=token_file,
            )
            await self.step(emit, "Git 工作区", "passed", repository["commit"])
            await self.step(emit, "OpenCode 配置", "running")
            await self._ensure_safe()
            self.key_applied = True
            changed = await configure_runtime(self, payload)
            await self.step(emit, "OpenCode 配置", "passed")
            self.installer_meta = {
                "repository": repository,
                "opencode": {
                    "version": manifest["opencode_version"],
                    "binary": self.target_binary,
                },
                "worker_agent": {"version": WORKER_AGENT_VERSION},
                "changed": {
                    "runtime": node_changed or package_changed or link_changed,
                    "repository": repository_changed,
                    **changed,
                },
            }
        finally:
            if token_file:
                await self._command(
                    shlex.join(["rm", "-f", "--", token_file, token_file + ".askpass"])
                )

    def _direct_endpoints(self) -> tuple[str, str]:
        return (
            client_gateway_base(require_direct_url(self.settings.gateway_openai_base, "Gateway")),
            require_direct_url(self.settings.mcp_resource_server_url, "图书馆 MCP"),
        )

    async def _probe_gateway(self, emit):
        await self.step(emit, "模型中转连接", "running")
        if not self.config_path:
            raise RuntimeError("尚未准备 OpenCode 配置，无法验收网关")
        source = (
            "import json,sys,urllib.request,urllib.error\n"
            "config=json.load(open(sys.argv[1]));provider=config['provider']['nexus-gateway']\n"
            "request=urllib.request.Request(provider['options']['baseURL']+'/models',"
            "headers={'Authorization':'Bearer '+provider['options']['apiKey']})\n"
            "try:\n"
            " response=urllib.request.urlopen(request,timeout=5)\n"
            " print(json.dumps({'status':response.status,'body':json.load(response)}))\n"
            "except urllib.error.HTTPError as error:print(json.dumps({'status':error.code}))\n"
        )
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            await self._ensure_safe()
            result = json.loads(
                await self._command(
                    shlex.join([self._agent_interpreter(), "-I", "-c", source, self.config_path]),
                    timeout=8,
                )
            )
            if result["status"] == 200:
                model = route_model_id(self.target_model)
                if not any(item.get("id") == model for item in result["body"].get("data", [])):
                    raise RuntimeError("中转未提供当前 Worker 模型")
                await self.step(emit, "模型中转连接", "passed", "模型中转认证与模型路由正常")
                return
            if result["status"] != 401:
                raise RuntimeError(f"模型中转 HTTP {result['status']}")
            await asyncio.sleep(2)
        raise RuntimeError("网关 Key 未在时限内生效")

    async def _read_optional(self, path: str) -> bytes | None:
        try:
            return await self.ssh.read_file(path)
        except Exception:
            return None

    async def _write_atomic_if_changed(
        self, path: str, data: bytes, mode: int, *, backup: bool = False
    ) -> bool:
        existing = await self.ssh.read_file(path)
        quoted = shlex.quote(path)
        if existing == data:
            await self._command(f"chmod {mode:o} {quoted}")
            return False
        if existing is None:
            await self._command(shlex.join(["mkdir", "-p", "-m", "700", "--", str(Path(path).parent)]))
        temp = f"{path}.tmp-{uuid4().hex}"
        temp_quoted = shlex.quote(temp)
        try:
            if backup and existing is not None:
                backup_path = shlex.quote(path + ".nexus-backup")
                await self._command(f"cp -p {quoted} {backup_path} && chmod 600 {backup_path}")
            await self.ssh.write_file(temp, data, mode=mode)
            if await self.ssh.read_file(temp) != data:
                raise RuntimeError(f"临时文件完整性校验失败：{path}")
            await self._command(f"chmod {mode:o} {temp_quoted}")
            await self._command(f"mv -f {temp_quoted} {quoted}")
        finally:
            try:
                await self._command(f"rm -f {temp_quoted}")
            except Exception:
                pass
        return True

    async def _existing_worker_credentials(
        self, token_path: str, *, target_digest: str | None = None
    ):
        content = await self._read_optional(token_path)
        remote_token = None
        if content:
            text = content.decode() if isinstance(content, bytes) else content
            values: dict[str, str] = {}
            for line in text.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
            remote_token = values.get("NEXUS_WORKER_TOKEN")
        async with SessionLocal() as session:
            instance = await session.scalar(
                select(WorkerInstance).where(
                    WorkerInstance.environment_id == self.environment.id
                )
            )
            if instance is not None and target_digest != instance.target_digest:
                return None
        if remote_token and instance is not None:
            if gateway_key_digest(remote_token) == instance.token_digest:
                return remote_token, instance.id
            raise RuntimeError("远端 Worker token 与数据库登记不一致，需人工核对后再部署，不会自动轮换")
        if instance is None:
            return None
        if instance.token_ciphertext:
            try:
                token_cipher = CredentialCipher(self.settings.gateway_master_key)
                recovered = token_cipher.decrypt(instance.token_ciphertext)
            except Exception as exc:
                raise RuntimeError(
                    f"数据库中的 Worker token 无法恢复：{redact_text(exc)}；需人工修复"
                ) from exc
            if gateway_key_digest(recovered) == instance.token_digest:
                return recovered, instance.id
        raise RuntimeError("无法证明现有 Worker token 有效，需人工修复或在数据库中重新登记，不会盲目轮换")

    async def _enroll_worker(self, target_digest: str):
        from app.worker_gateway.service import get_gateway

        gateway = get_gateway()
        async with SessionLocal() as session:
            return await gateway.enroll(session, self.environment.id, target_digest=target_digest)

    async def _run_ctl(self, action: str) -> str:
        ctl_path = worker_ctl_path(self.environment.id)
        return await self._command(shlex.join([ctl_path, action]), timeout=45)

    async def _install_worker(self, emit):
        await self.step(emit, "安装 Worker Agent", "running")
        await self._ensure_safe()
        gateway_url, mcp_url = self._direct_endpoints()
        agent_gateway = require_direct_url(
            self.settings.worker_agent_gateway_url, "Worker Gateway"
        )
        port = int(self.environment.opencode_server_port)
        home = self.home
        if not home:
            raise RuntimeError("部署尚未探测远端 HOME")
        env_root = f"{home}/.local/share/nexus/workers/{self.environment.id}"
        node_binary = getattr(self, "node_binary", None) or self.settings.remote_node_binary
        node_bin = (
            str(Path(node_binary).parent)
            if node_binary
            else (f"{env_root}/node-v{self.settings.remote_node_version}/bin")
        )
        path_value = ":".join(
            [node_bin, f"{env_root}/opencode/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        )
        install_dir, python = (
            worker_install_dir(self.environment.id),
            agent_python(self.environment.id),
        )
        state_dir, log_dir = (
            worker_state_dir(self.environment.id),
            worker_log_dir(self.environment.id),
        )
        await self._command(shlex.join(["install", "-d", "-m", "755", install_dir]))
        await self._command(shlex.join(["install", "-d", "-m", "700", state_dir, log_dir]))
        await self._command(shlex.join(["install", "-d", "-m", "700", WORKER_SECRET_DIR]))
        token_path_git, askpass_git = (
            git_token_path(self.environment.id),
            git_askpass_path(self.environment.id),
        )
        git_secret_changed = False
        if self.git_token:
            tok_changed = await self._write_atomic_if_changed(
                token_path_git, self.git_token.encode(), 0o600
            )
            ask_changed = await self._write_atomic_if_changed(
                askpass_git, git_askpass_script(self.environment.id).encode(), 0o700
            )
            git_secret_changed = tok_changed or ask_changed
        else:
            await self._command(shlex.join(["rm", "-f", token_path_git, askpass_git]))

        wheel_name, wheel_bytes = agent_wheel()
        wheel_digest = hashlib.sha256(wheel_bytes).hexdigest()
        wheel_path, marker_path = f"{install_dir}/{wheel_name}", f"{install_dir}/wheel.sha256"
        marker = (
            await self._command(shlex.join(["cat", marker_path]) + " 2>/dev/null || true")
        ).strip()
        venv_ok = (
            marker == wheel_digest
            and await self._command(f"test -x {shlex.quote(python)} && printf yes || true")
            == "yes"
        )
        if not venv_ok:
            await self._ensure_safe()
            await self._write_atomic_if_changed(wheel_path, wheel_bytes, 0o644)
            index = deployment_manifest(self.settings)["pip_index_url"]
            pip = (
                "env -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_TRUSTED_HOST -u PIP_NO_INDEX "
                "-u PIP_TARGET -u PIP_PREFIX -u PIP_USER PIP_CONFIG_FILE=/dev/null "
                + "PIP_INDEX_URL="
                + shlex.quote(index)
                + " "
                + "PIP_TRUSTED_HOST="
                + shlex.quote(pip_trusted_host(index))
                + " "
                + shlex.join(
                    [
                        python,
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--upgrade",
                        "--force-reinstall",
                        *pip_install_options(index),
                        wheel_path,
                    ]
                )
            )
            await self._command(
                shlex.join(
                    [self._agent_interpreter(), "-m", "venv", agent_venv(self.environment.id)]
                )
                + " && "
                + pip,
                timeout=300,
            )
            await self._write_atomic_if_changed(marker_path, wheel_digest.encode(), 0o600)

        target_digest = compute_target_digest(self.environment)
        token_path = f"{WORKER_SECRET_DIR}/worker-{self.environment.id}.env"
        existing = await self._existing_worker_credentials(
            token_path, target_digest=target_digest
        )
        if existing is not None:
            token, worker_instance_id = existing
        else:
            enrolled = await self._enroll_worker(target_digest)
            token, worker_instance_id = enrolled.token, enrolled.worker_instance_id
        self.secrets.append(token)

        async with SessionLocal() as session:
            deployed_spec = await build_desired_spec(session, self.environment, self.settings)
        worker_env = {
            "HOME": home,
            "PATH": path_value,
            "OPENCODE_CONFIG": self.config_path,
            "OPENCODE_CONFIG_DIR": str(Path(self.config_path).parent),
            **safe_git_environment(self.environment.id),
            "NEXUS_GIT_CREDENTIAL_REVISION": self.git_credential_revision or "",
            "NEXUS_WORKER_STATE_DIR": state_dir,
            "NEXUS_WORKER_SPEC_REVISION": deployed_spec["revision"],
            "NEXUS_WORKER_TOKEN": token,
            "NEXUS_WORKER_INSTANCE_ID": worker_instance_id,
            "NEXUS_WORKER_ENVIRONMENT_ID": str(self.environment.id),
            "NEXUS_WORKER_GATEWAY_URL": agent_gateway,
            "NEXUS_REMOTE_GATEWAY_URL": gateway_url,
            "NEXUS_MCP_RESOURCE_URL": mcp_url,
            "NEXUS_WORKER_OPENCODE_URL": f"http://127.0.0.1:{port}",
            "NEXUS_WORKER_OPENCODE_BIN": self.target_binary,
            "NEXUS_WORKER_WORKSPACE_ROOT": self.environment.workspace_root,
            "NEXUS_OPENCODE_CONTROL_TIMEOUT_SECONDS": str(
                self.settings.opencode_control_timeout_seconds
            ),
        }

        def serialize(env):
            if any("\n" in str(value) or "\r" in str(value) for value in env.values()):
                raise RuntimeError("进程环境变量含换行，拒绝写入")
            return "".join(f"{key}={value}\n" for key, value in env.items()).encode()

        worker_changed = await self._write_atomic_if_changed(
            token_path, serialize(worker_env), 0o600, backup=True
        )
        ctl_changed = await self._write_atomic_if_changed(
            worker_ctl_path(self.environment.id),
            render_worker_ctl_script(self.environment.id, port=port).encode(),
            0o755,
        )
        await self._write_atomic_if_changed(
            START_ALL_PATH,
            render_start_all_script().encode(),
            0o755,
        )

        await self._ensure_safe()
        await self._native_idle(strict=True)
        changes = self.installer_meta.get("changed", {})
        should_restart = bool(
            worker_changed
            or ctl_changed
            or git_secret_changed
            or not venv_ok
            or changes.get("runtime")
            or changes.get("configuration")
            or changes.get("skills")
            or changes.get("repository")
        )
        await self._run_ctl("restart" if should_restart else "start")
        await self.step(emit, "安装 Worker Agent", "passed", worker_instance_id)

    async def _converge_readiness(self, emit) -> dict:
        await self.step(emit, "就绪验收", "running")
        deadline = asyncio.get_running_loop().time() + min(
            30.0, float(self.settings.remote_deploy_timeout_seconds)
        )
        last_blocking: list[str] = ["Worker 尚未建立在线握手"]
        while asyncio.get_running_loop().time() < deadline:
            await self._ensure_safe()
            async with SessionLocal() as session:
                env_row = await session.get(
                    OpenCodeExecutionEnvironment, self.environment.id
                )
                if env_row is None:
                    raise RuntimeError("执行环境已不存在")
                instance = await session.scalar(
                    select(WorkerInstance).where(
                        WorkerInstance.environment_id == self.environment.id
                    )
                )
                if instance is None or instance.status != "online":
                    await asyncio.sleep(0.25)
                    continue
                desired = await build_desired_spec(session, env_row, self.settings)
                evaluated = evaluate_worker_readiness(desired, env_row, instance)
            if evaluated["readiness"] == "READY":
                self.verified_evaluation = evaluated
                await self.step(emit, "就绪验收", "passed", "网关在线且装备快照已对齐")
                return evaluated

            opencode_err = evaluated["runtime_health"].get("last_error")
            if evaluated["readiness"] == "DEGRADED" and opencode_err:
                raise RuntimeError(f"远端 OpenCode 启动或健康异常：{opencode_err}")
            last_blocking = [item["message"] for item in evaluated["blocking"]] or ["装备尚未收敛就绪"]
            await asyncio.sleep(0.3)

        raise RuntimeError("就绪验收未通过：" + "；".join(last_blocking))

    async def _finish(self, success: bool, error: str = "") -> bool:
        now = datetime.now(UTC)
        if self.lease is None:
            if not success:
                return False
            async with SessionLocal() as db:
                environment = await db.get(
                    OpenCodeExecutionEnvironment, self.environment.id
                )
                if environment is None:
                    return False
                environment.status = EnvironmentStatus.AVAILABLE
                environment.last_checked_at = now
                if self.target_model:
                    environment.model_name = self.target_model
                if self.target_binary:
                    environment.opencode_path = self.target_binary
                await db.commit()
            return True

        evaluated = self.verified_evaluation
        if evaluated is None:
            try:
                async with SessionLocal() as db:
                    env_row = await db.get(
                        OpenCodeExecutionEnvironment, self.environment.id
                    )
                    instance = await db.scalar(
                        select(WorkerInstance).where(
                            WorkerInstance.environment_id == self.environment.id
                        )
                    )
                    if env_row is not None:
                        desired = await build_desired_spec(db, env_row, self.settings)
                        evaluated = evaluate_worker_readiness(desired, env_row, instance)
            except Exception:
                evaluated = None

        if success:
            state = MaintenanceState.SUCCEEDED.value
            stage = "completed"
            error_code = None
            env_status = EnvironmentStatus.AVAILABLE
        else:
            state = MaintenanceState.FAILED.value
            stage = "failed"
            error_code = "maintenance_failed"
            raw_status = (
                ((evaluated or {}).get("runtime_health") or {}).get("status")
                if isinstance(evaluated, dict)
                else None
            )
            env_status = (
                EnvironmentStatus(raw_status)
                if raw_status in {item.value for item in EnvironmentStatus}
                else EnvironmentStatus.UNAVAILABLE
            )

        installer_opencode = (
            self.installer_meta.get("opencode")
            if isinstance(self.installer_meta.get("opencode"), dict)
            else {}
        )
        installer_worker = (
            self.installer_meta.get("worker_agent")
            if isinstance(self.installer_meta.get("worker_agent"), dict)
            else {}
        )
        installer_repository = (
            self.installer_meta.get("repository")
            if isinstance(self.installer_meta.get("repository"), dict)
            and self.installer_meta.get("repository")
            else None
        )
        after = {
            "checks": self.steps,
            "message": redact_text(error, self.secrets),
            "probe_status": env_status.value,
            "readiness": (evaluated or {}).get("readiness"),
            "opencode_version": installer_opencode.get("version")
            or ((evaluated or {}).get("opencode") or {}).get("version"),
            "worker_agent_version": installer_worker.get("version") or WORKER_AGENT_VERSION,
            "model_name": self.target_model,
            "repository": installer_repository or (evaluated or {}).get("repository"),
            "changed": self.installer_meta.get("changed") or {},
            "equipment": ((evaluated or {}).get("runtime_health") or {}).get("equipment") or [],
        }
        values: dict = {
            "status": env_status,
            "last_checked_at": now,
        }
        if success:
            if self.target_model:
                values["model_name"] = self.target_model
            if self.target_binary:
                values["opencode_path"] = self.target_binary

        applied = await self.lease.finish(
            state,
            stage=stage,
            error_code=error_code,
            error_message=redact_text(error, self.secrets) if error else None,
            after_snapshot=redact_data(after, self.secrets),
            environment_id=self.environment.id,
            environment_values=values,
            event_message="远程环境部署完成" if success else "远程环境部署失败",
        )
        if not applied:
            return False

        key_to_revoke = None
        if success and self.new_key_id:
            key_to_revoke = self.old_key_id
        elif not success and self.new_key_id and not self.key_applied:
            key_to_revoke = self.new_key_id
        if key_to_revoke:
            runtime = RedisRuntimeStore(self.settings.redis_url)
            try:
                async with SessionLocal() as db:
                    await self._gateway_service(db, runtime).revoke_key(
                        key_to_revoke, actor="employee-deployment", actor_role="admin"
                    )
            finally:
                await runtime.close()
        return True

    async def run(self, emit):
        from app.runtime.maintenance import (
            MaintenanceBlocked,
            MaintenanceCanceled,
            MaintenanceLeaseLost,
        )

        success, error = False, ""
        skip_finish = False
        try:
            async with asyncio.timeout(self.settings.remote_deploy_timeout_seconds):
                self._direct_endpoints()
                await self.step(emit, "SSH 连接与执行检查", "running")
                self.ssh = await self.runtime.open_session(self.environment)
                await prepare_python(self, emit)
                await self._native_idle(strict=True)
                await self.step(emit, "SSH 连接与执行检查", "passed")
                if self.lease is not None:
                    await self.lease.set_execution_route("ssh", "Nexus 通过 SSH 执行一键部署")
                await self._ensure_safe()
                await self._install(emit)
                await self._probe_gateway(emit)
                await self._install_worker(emit)
                await self._converge_readiness(emit)
                success = True
        except (MaintenanceCanceled, MaintenanceLeaseLost, MaintenanceBlocked):
            skip_finish = True
            raise
        except asyncio.CancelledError:
            skip_finish = True
            raise
        except TimeoutError:
            error = "部署超时；远端锁已释放，可直接重试一键部署"
        except Exception as exc:
            error = self.redact(exc)
        finally:
            if not success and error and not skip_finish:
                current = next(
                    (s["name"] for s in reversed(self.steps) if s["state"] == "running"),
                    "环境部署",
                )
                await self.step(emit, current, "failed", error)
            if self.install_lock is not None:
                await self.install_lock.close()
                self.install_lock = None
            if self.ssh:
                await self.ssh.close()
                self.ssh = None
            if not skip_finish:
                applied = await self._finish(success, error)
                if not applied:
                    success = False
                    error = "环境配置或操作记录已变化，旧结果未写入；请刷新重试"
                    skip_finish = True
        if not skip_finish:
            await emit(
                {
                    "type": "result",
                    "success": success,
                    "message": "部署完成" if success else error,
                    "status": "AVAILABLE" if success else "UNAVAILABLE",
                    "checks": self.steps,
                }
            )

"""Explicit environment maintenance; OpenCode remains the execution fact source."""

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
from app.config.worker_deployment import deployment_manifest
from app.database import SessionLocal
from app.domain import EnvironmentStatus
from app.gateway.models import GatewayKey, GatewayRoute
from app.gateway.probe import HttpGatewayProbeClient
from app.gateway.runtime import RedisRuntimeStore
from app.gateway.schemas import GatewayKeyCreate
from app.gateway.security import CredentialCipher, gateway_key_digest
from app.gateway.service import GatewayControlService
from app.gateway.urls import gateway_base
from app.models import CodeRepository, GitCredential, OpenCodeExecutionEnvironment
from app.runtime.capability import (
    binding_entries,
    binding_names,
    binding_revision,
    discover_names,
    loads_jsonc,
    observe_bindings,
    stored_capability,
)
from app.runtime.direct_urls import require_direct_url
from app.runtime.environment import ExecutionEnvironmentRuntime
from app.runtime.opencode_model import route_model_id
from app.runtime.remote_artifacts import install_node, install_opencode
from app.runtime.remote_bootstrap import managed_python, prepare_python
from app.runtime.remote_package_config import configure_sources
from app.runtime.remote_workspace import configure_runtime, git_workspace
from app.runtime.repository import cipher, collect_skill_files

PROVIDER_ID = "nexus-gateway"
WORKER_SECRET_DIR = "/etc/nexus"


def worker_install_dir(environment_id: int) -> str:
    return f"/opt/nexus/workers/{environment_id}"


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


async def guard_environment_write(session, environment):
    """Lock the environment row first, then read the current maintenance fact.

    Environment row -> operation row ordering matches submit/retry. The caller's
    transaction keeps the row lock until it commits, so a maintenance submit
    that is waiting on the lock cannot slip a Task start through afterwards.
    """

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
    # lock_environment_row already refreshed with a current locking read.


def _wheel_digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def agent_wheel() -> tuple[str, bytes]:
    """Reproducible Worker Agent wheel: same source always yields the same bytes."""

    from app.worker_agent import __version__ as version

    dist = f"nexus_worker_agent-{version}"
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
        str(path.relative_to(app_root.parent)) for path in sorted((app_root / "worker_agent").rglob("*.py"))
    )
    files = sorted((name, (app_root.parent / name).read_bytes()) for name in set(names))
    files.append(
        (
            "app/worker_agent/dsh_acp/dependency-lock.json",
            (app_root.parents[2] / "ops/runtime/dsh/package-lock.json").read_bytes(),
        )
    )
    info = f"{dist}.dist-info"
    metadata = (
        "Metadata-Version: 2.1\n"
        "Name: nexus-worker-agent\n"
        f"Version: {version}\n"
        "Summary: Nexus Worker Agent\n"
        "Requires-Python: >=3.11\n"
        "Requires-Dist: httpx>=0.27,<1\n"
        "Requires-Dist: websockets>=12\n"
        "Provides-Extra: dsh\n"
        "Requires-Dist: agent-client-protocol==0.8.1; extra == 'dsh'\n"
        "Requires-Dist: psutil==7.2.2; extra == 'dsh'\n"
        "Requires-Dist: PyYAML>=6,<7; extra == 'dsh'\n"
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
        "options": {"baseURL": gateway_base(gateway_url), "apiKey": gateway_key},
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
        self.action = operation.get("action", "deploy") if isinstance(operation, dict) else operation.action
        self.lease = lease
        self.settings = get_settings()
        self.runtime = runtime or ExecutionEnvironmentRuntime(self.settings)
        self.steps: list[dict] = []
        self.secrets = [environment.ssh_password]
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
        self.git_repository_id: str | None = None
        self.installer_meta: dict = {}
        self.install_lock = None

    def _agent_interpreter(self) -> str:
        if getattr(self, "python_interpreter", None):
            return self.python_interpreter
        configured = (self.settings.remote_python or "").strip()
        return configured or managed_python(self.environment.id)

    def _probe_interpreter(self) -> str:
        return agent_python(self.environment.id) if self.action == "restart" else self._agent_interpreter()

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
        item = {"name": name, "state": state, "passed": state == "passed", "detail": self.redact(detail)}
        self.steps = [s for s in self.steps if s["name"] != name] + [item]
        await emit({"type": "step", **item})

    async def _acquire_install_lock(self):
        if self.install_lock is not None:
            return
        root = "/opt/nexus/workers/.locks"
        await self._command(shlex.join(["install", "-d", "-m", "700", root]))
        canonical = await self._command(shlex.join(["realpath", "-m", self.environment.workspace_root]))
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:24]
        self.install_lock = await self.ssh.hold_lock(
            [
                f"{root}/environment-{self.environment.id}.lock",
                f"{root}/workspace-{digest}.lock",
            ]
        )

    async def _ensure_safe(self, *, allow_occupied: bool = False):
        if self.install_lock is not None and not self.install_lock.is_active():
            raise RuntimeError("远端部署锁已失效，停止修改")
        if self.lease is None:
            return
        from app.runtime.maintenance import MaintenanceCanceled

        await self.lease.check_canceled()
        await self.lease.assert_safe_to_disrupt(
            self.environment.id,
            self.environment.employee_id,
            allow_occupied=allow_occupied,
        )
        operation = await self.lease.snapshot()
        if operation is None or operation.attempt != self.lease.attempt:
            from app.runtime.maintenance import MaintenanceLeaseLost

            raise MaintenanceLeaseLost("维护租约已失效")
        if operation.cancel_requested:
            raise MaintenanceCanceled("操作已请求取消")

    async def _command(self, command, timeout=30):
        rc, out, err = await asyncio.wait_for(self.ssh.run_command(command), timeout)
        if rc:
            raise RuntimeError(self.redact(err.strip() or out.strip() or f"远程命令退出码 {rc}"))
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
            # Only an explicit refused connection proves the service is stopped.
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
                await self._require_owned_listener()
            return
        installed = agent_python(self.environment.id)
        if await self._command(f"test -x {shlex.quote(installed)} && printf yes || true") == "yes":
            readable = await self._native_idle(strict=True, interpreter=installed)
            if readable:
                await self._require_owned_listener()
            return
        # Before Python exists, only absence of a kernel TCP listener permits
        # bootstrap. A listener cannot be treated as idle without native facts.
        probe = r"""
files=(/proc/net/tcp)
if test -e /proc/net/tcp6; then files+=(/proc/net/tcp6); fi
for file in "${files[@]}"; do
  test -r "$file" || { echo '无法确认 OpenCode 监听状态，停止环境引导' >&2; exit 1; }
done
if awk -v port="$1" '$4 == "0A" && $2 ~ ":" port "$" { found=1 }
  END { exit found ? 1 : 0 }' "${files[@]}"; then
  printf CLEAR
else
  printf BLOCKED
fi
"""
        port = f"{int(self.environment.opencode_server_port):04X}"
        state = await self._command("bash -c " + shlex.quote(probe) + " -- " + shlex.quote(port))
        if state != "CLEAR":
            raise RuntimeError("OpenCode 端口已有监听且尚无可用解释器确认原生状态，停止环境引导")

    async def _require_owned_listener(self):
        from app.runtime.remote_processes import identity

        path = f"{worker_install_dir(self.environment.id)}/opencode-process.json"
        raw = await self.ssh.read_file(path)
        if not raw:
            raise RuntimeError("OpenCode 已在运行但缺少受管身份记录，请管理员确认停止旧部署后重新部署")
        try:
            expected = json.loads(raw)["identity"]
            pid = expected["pid"]
            if not isinstance(pid, int) or pid <= 1 or expected.get("pgid") != pid:
                raise ValueError("invalid identity")
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("已有进程身份记录无效，未修改环境") from exc
        if await identity(self, pid) != expected:
            raise RuntimeError("已有进程身份已变化，未修改环境")

    async def _manage_process(self, name: str, *, restart: bool) -> None:
        from app.runtime.remote_processes import manage

        await manage(self, name, restart=restart)

    async def _gateway_key(self, config_path, models):
        content = await self.ssh.read_file(config_path)
        raw = None
        if content:
            text = content.decode() if isinstance(content, bytes) else content
            try:
                config = loads_jsonc(text)
                raw = config.get("provider", {}).get(PROVIDER_ID, {}).get("options", {}).get("apiKey")
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
                    name=f"Agent Worker {self.environment.employee_id}", owner_id=self.environment.employee_id
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
        gateway_url, mcp_url = await self._channel_endpoints()
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
        if self.environment.repository_id:
            async with SessionLocal() as db:
                repository = await db.get(CodeRepository, self.environment.repository_id)
                credential = await db.scalar(
                    select(GitCredential).where(GitCredential.repository_id == self.environment.repository_id)
                )
            if repository is None or credential is None:
                raise RuntimeError("绑定的仓库没有凭据")
            token = cipher().decrypt(credential.token_ciphertext)
            self.secrets.append(token)
            self.git_token = token
            self.git_credential_revision = credential_revision(credential.token_ciphertext)
            self.git_repository_id = repository.id
            token_file = stem + ".token"
            payload["repository"] = {
                "url": repository.url,
                "branch": self.environment.branch or repository.branch,
            }
        manifest = deployment_manifest(self.settings)
        await self.step(emit, "部署配置", "passed", "已准备员工专属网关与图书馆配置")
        await self.step(emit, "Node.js 运行环境", "running")
        facts = self.remote_facts
        arch = "x64" if facts["arch"] == "x86_64" else "arm64"
        node_root, node_changed = await install_node(self, manifest, home, arch=arch, libc=facts["libc"])
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
        current_link = await self._command(shlex.join(["readlink", self.target_binary]) + " || true")
        link_changed = current_link != prefix + "/bin/opencode"
        if link_changed:
            backup = self.target_binary + ".nexus-backup-" + self.operation_id
            await self._ensure_safe()
            await self._command(
                f"if test -e {shlex.quote(self.target_binary)} "
                f"&& ! test -L {shlex.quote(self.target_binary)}; "
                f"then test ! -e {shlex.quote(backup)} "
                f"&& mv {shlex.quote(self.target_binary)} {shlex.quote(backup)} "
                f"|| exit 1; fi; " + shlex.join(["ln", "-sfn", prefix + "/bin/opencode", self.target_binary])
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
            if self.lease is not None:
                await self.lease.mark_danger()
            self.key_applied = True
            changed = await configure_runtime(self, payload)
            await self.step(emit, "OpenCode 配置", "passed")
            self.installer_meta = {
                "repository": repository,
                "opencode": {"version": manifest["opencode_version"], "binary": self.target_binary},
                "changed": {
                    "runtime": node_changed or package_changed or link_changed,
                    "repository": repository_changed,
                    **changed,
                },
            }
            if self.action == "update" and self.lease is not None and repository["remote_commit"]:
                current = await self.lease.snapshot()
                target = dict(current.target_revision or {}) if current is not None else {}
                target["repository_commit"] = repository["remote_commit"]
                await self.lease.set_target_revision(target)
        finally:
            if token_file:
                await self._command(shlex.join(["rm", "-f", "--", token_file, token_file + ".askpass"]))

    def _direct_endpoints(self) -> tuple[str, str]:
        return (
            gateway_base(require_direct_url(self.settings.remote_gateway_local_url, "Gateway")),
            require_direct_url(self.settings.mcp_resource_server_url, "图书馆 MCP"),
        )

    async def _channel_endpoints(self) -> tuple[str, str]:
        return self._direct_endpoints()

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
        """Atomically replace one remote file without exposing its content.

        The temporary file lives in the target directory, is read back through
        SFTP before rename, and the previous env/unit is kept as a backup. Any
        failure leaves the old target in place; commands only carry paths.
        """

        existing = await self.ssh.read_file(path)
        quoted = shlex.quote(path)
        if existing == data:
            await self._command(f"chmod {mode:o} {quoted}")
            return False
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

    async def _existing_worker_credentials(self, token_path: str):
        from app.models import WorkerInstance
        from app.runtime.redaction import redact_text

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
                select(WorkerInstance).where(WorkerInstance.environment_id == self.environment.id)
            )
        if remote_token and instance is not None:
            if gateway_key_digest(remote_token) == instance.token_digest:
                return remote_token, instance.id
            raise RuntimeError("远端 Worker token 与数据库登记不一致，需人工核对后再部署，不会自动轮换")
        if instance is None:
            return None
        if instance.token_ciphertext:
            try:
                from app.gateway.security import CredentialCipher

                cipher = CredentialCipher(self.settings.gateway_master_key)
                recovered = cipher.decrypt(instance.token_ciphertext)
            except Exception as exc:
                raise RuntimeError(
                    f"数据库中的 Worker token 无法恢复：{redact_text(exc)}；需人工修复"
                ) from exc
            if gateway_key_digest(recovered) == instance.token_digest:
                return recovered, instance.id
        raise RuntimeError("无法证明现有 Worker token 有效，需人工修复或在数据库中重新登记，不会盲目轮换")

    async def _install_worker(self, emit):
        await self.step(emit, "安装 Worker Agent", "running")
        await self._ensure_safe()
        gateway_url, mcp_url = self._direct_endpoints()
        agent_gateway = require_direct_url(self.settings.worker_agent_gateway_url, "Worker Gateway")
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
        path_value = ":".join([node_bin, f"{env_root}/opencode/bin", "/usr/local/bin", "/usr/bin", "/bin"])
        install_dir, python = worker_install_dir(self.environment.id), agent_python(self.environment.id)
        await self._command(shlex.join(["install", "-d", "-m", "755", install_dir]))
        await self._command(shlex.join(["install", "-d", "-m", "700", WORKER_SECRET_DIR]))
        token_path_git, askpass_git = (
            git_token_path(self.environment.id),
            git_askpass_path(self.environment.id),
        )
        if self.git_token:
            await self._write_atomic_if_changed(token_path_git, self.git_token.encode(), 0o600)
            await self._write_atomic_if_changed(
                askpass_git, git_askpass_script(self.environment.id).encode(), 0o700
            )
        else:
            await self._command(shlex.join(["rm", "-f", token_path_git, askpass_git]))
        wheel_name, wheel_bytes = agent_wheel()
        wheel_digest = hashlib.sha256(wheel_bytes).hexdigest()
        wheel_path, marker_path = f"{install_dir}/{wheel_name}", f"{install_dir}/wheel.sha256"
        marker = (await self._command(shlex.join(["cat", marker_path]) + " 2>/dev/null || true")).strip()
        venv_ok = (
            marker == wheel_digest
            and await self._command(f"test -x {shlex.quote(python)} && printf yes || true") == "yes"
        )
        if not venv_ok:
            await self._ensure_safe()
            await self._write_atomic_if_changed(wheel_path, wheel_bytes, 0o644)
            index = deployment_manifest(self.settings)["pip_index_url"]
            pip = (
                "env -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_TRUSTED_HOST -u PIP_NO_INDEX "
                "-u PIP_TARGET -u PIP_PREFIX -u PIP_USER PIP_CONFIG_FILE=/dev/null "
                + shlex.join(
                    [
                        python,
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--upgrade",
                        "--force-reinstall",
                        "--index-url",
                        index,
                        wheel_path,
                    ]
                )
            )
            await self._command(
                shlex.join([self._agent_interpreter(), "-m", "venv", agent_venv(self.environment.id)])
                + " && "
                + pip,
                timeout=300,
            )
            await self._write_atomic_if_changed(marker_path, wheel_digest.encode(), 0o600)
        token_path = f"{WORKER_SECRET_DIR}/worker-{self.environment.id}.env"
        existing = await self._existing_worker_credentials(token_path)
        if existing is not None:
            token, worker_instance_id = existing
        else:
            from app.worker_gateway.service import get_gateway

            async with SessionLocal() as session:
                enrolled = await get_gateway().enroll(session, self.environment.id)
            token, worker_instance_id = enrolled.token, enrolled.worker_instance_id
        self.secrets.append(token)
        common = {
            "HOME": home,
            "PATH": path_value,
            "OPENCODE_CONFIG": self.config_path,
            "OPENCODE_CONFIG_DIR": str(Path(self.config_path).parent),
            **safe_git_environment(self.environment.id),
        }
        worker_env = {
            **common,
            "NEXUS_WORKER_TOKEN": token,
            "NEXUS_WORKER_INSTANCE_ID": worker_instance_id,
            "NEXUS_WORKER_ENVIRONMENT_ID": str(self.environment.id),
            "NEXUS_WORKER_GATEWAY_URL": agent_gateway,
            "NEXUS_REMOTE_GATEWAY_URL": gateway_url,
            "NEXUS_MCP_RESOURCE_URL": mcp_url,
            "NEXUS_WORKER_OPENCODE_URL": f"http://127.0.0.1:{port}",
            "NEXUS_WORKER_OPENCODE_BIN": self.target_binary,
            "NEXUS_WORKER_WORKSPACE_ROOT": self.environment.workspace_root,
            "NEXUS_WORKER_OPENCODE_PROCESS_RECORD": f"{install_dir}/opencode-process.json",
            "NEXUS_OPENCODE_CONTROL_TIMEOUT_SECONDS": str(self.settings.opencode_control_timeout_seconds),
        }

        def serialize(env):
            if any("\n" in str(value) or "\r" in str(value) for value in env.values()):
                raise RuntimeError("进程环境变量含换行，拒绝写入")
            return "".join(f"{key}={value}\n" for key, value in env.items()).encode()

        worker_changed = await self._write_atomic_if_changed(
            token_path, serialize(worker_env), 0o600, backup=True
        )
        opencode_changed = await self._write_atomic_if_changed(
            f"{WORKER_SECRET_DIR}/opencode-{self.environment.id}.env",
            serialize(common),
            0o600,
            backup=True,
        )
        await self._ensure_safe()
        await self._native_idle(strict=True)
        changes = self.installer_meta.get("changed", {})
        if self.lease is not None:
            await self.lease.mark_danger()
        await self._manage_process(
            "opencode",
            restart=bool(
                opencode_changed
                or changes.get("runtime")
                or changes.get("configuration")
                or changes.get("skills")
            ),
        )
        await self._wait_opencode_ready()
        await self._manage_process("worker", restart=worker_changed or not venv_ok)
        await self.step(emit, "安装 Worker Agent", "passed", worker_instance_id)

    async def _wait_until_online(self):
        from app.models import WorkerInstance

        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            async with SessionLocal() as session:
                instance = await session.scalar(
                    select(WorkerInstance).where(WorkerInstance.environment_id == self.environment.id)
                )
                if instance is not None and instance.status == "online":
                    return
            await asyncio.sleep(0.2)
        raise RuntimeError("Worker 未上线，通道保持未安装")

    async def _mark_inside_operation(self, emit):
        await self.step(emit, "切换到 Worker", "running")
        await self._wait_until_online()
        from app.runtime.worker_channel import complete_worker_mark

        async def probe():
            await self._probe_mcp(emit)

        async with SessionLocal() as session:
            await complete_worker_mark(
                session,
                self.environment.id,
                self.operation_id,
                lease=self.lease,
                before_write=probe,
            )
        await self.step(emit, "切换到 Worker", "passed", "生效配置与内网地址一致")

    async def _probe_mcp(self, emit):
        await self.step(emit, "图书馆 MCP", "running")
        from app.runtime.worker_client import WorkerOpenCodeClient

        client = WorkerOpenCodeClient(self.settings, environment_id=self.environment.id)
        mcp = await client.request("GET", "/mcp", directory=Path(self.environment.workspace_root))
        if not isinstance(mcp, dict) or mcp.get("nexus-library", {}).get("status") != "connected":
            raise RuntimeError("数字图书馆 MCP 未连接")
        await self.step(emit, "图书馆 MCP", "passed", "数字图书馆已连接")

    async def _opencode_health_ready(self) -> tuple[bool, str]:
        port = int(self.environment.opencode_server_port)
        url = f"http://127.0.0.1:{port}/global/health"
        script = (
            "import json,urllib.error,urllib.request\n"
            f"url={json.dumps(url)}\n"
            "try:\n"
            "    response=urllib.request.urlopen(url, timeout=2)\n"
            "    payload=json.loads(response.read().decode('utf-8'))\n"
            "    ok=isinstance(payload, dict) and payload.get('healthy') is True\n"
            "    print('READY' if ok else 'NOT_READY:invalid')\n"
            "except urllib.error.HTTPError as exc:\n"
            "    print('NOT_READY:HTTP %d' % exc.code)\n"
            "except Exception as exc:\n"
            "    print('NOT_READY:%s' % exc.__class__.__name__)\n"
        )
        out = (
            await self._command(f"{shlex.quote(self._probe_interpreter())} -c {shlex.quote(script)}")
        ).strip()
        return out == "READY", out

    async def _wait_opencode_ready(self) -> None:
        """Bounded read-only readiness wait after an SSH unit restart."""

        deadline = asyncio.get_running_loop().time() + self.settings.opencode_server_start_timeout_seconds
        last = "NOT_READY"
        while asyncio.get_running_loop().time() < deadline:
            await self._ensure_safe()
            ready, last = await self._opencode_health_ready()
            if ready:
                return
            await asyncio.sleep(0.5)
        raise RuntimeError(f"OpenCode 重启后健康检查未通过：{last[-200:]}")

    async def _restart_processes(self, emit):
        await self.step(emit, "重启 OpenCode Serve", "running")
        await self._ensure_safe()
        await self._native_idle(strict=True)
        await self._ensure_safe()
        if self.lease is not None:
            await self.lease.mark_danger()
        await self._manage_process("opencode", restart=True)
        await self._wait_opencode_ready()
        await self._manage_process("worker", restart=True)
        await self._wait_until_online()
        await self.step(
            emit,
            "重启 OpenCode Serve",
            "passed",
            "进程重启、OpenCode 健康与 Worker 上线均已确认",
        )

    async def _observe_capability(self) -> dict:
        text = ""
        skill_names: list[str] = []
        error = ""
        try:
            if self.config_path and self.ssh is not None:
                content = await self.ssh.read_file(self.config_path)
                text = content.decode() if isinstance(content, bytes) else (content or "")
                skill_root = str(Path(self.config_path).parent / "skill")
                listing = await self._command(
                    "find "
                    + shlex.quote(skill_root)
                    + " -mindepth 2 -maxdepth 2 -name SKILL.md -printf '%h\\n' || true"
                )
                skill_names = [Path(line.strip()).name for line in listing.splitlines() if line.strip()]
            found = discover_names(skill_names, text)
        except Exception as exc:
            found = set()
            error = self.redact(exc)
        expected = binding_names(self.environment.skill_refs, self.environment.mcp_refs)
        entries = binding_entries(self.environment.skill_refs, self.environment.mcp_refs)
        detail = self.environment.status_detail if isinstance(self.environment.status_detail, dict) else {}
        current = detail.get("capability") if isinstance(detail.get("capability"), dict) else None
        return observe_bindings(
            current,
            desired=binding_revision(entries),
            found=found,
            expected=expected,
            error=error,
            checked_at=datetime.now(UTC).isoformat(),
        )

    async def _probe_after_failure(self):
        """A real read-only health probe; unknown never becomes AVAILABLE."""

        try:
            if await self._worker_is_online():
                from app.runtime.worker_client import WorkerOpenCodeClient

                try:
                    await WorkerOpenCodeClient(self.settings, environment_id=self.environment.id).health()
                    return EnvironmentStatus.AVAILABLE
                except Exception:
                    return EnvironmentStatus.UNAVAILABLE
        except Exception:
            pass
        return EnvironmentStatus.UNKNOWN

    async def _finish(self, success, error="", capability=None):
        from app.domain import MaintenanceState
        from app.runtime.redaction import redact_data, redact_text

        now = datetime.now(UTC)
        if self.lease is None:
            # Direct callers without a persisted operation may only record success.
            # The API and executor always use a persisted operation with a lease.
            if not success:
                return False
            async with SessionLocal() as db:
                environment = await db.get(OpenCodeExecutionEnvironment, self.environment.id)
                if environment is None:
                    return False
                environment.status = EnvironmentStatus.AVAILABLE
                environment.last_checked_at = now
                environment.model_name = self.target_model
                environment.opencode_path = self.target_binary
                await db.commit()
            return True

        operation = await self.lease.snapshot()
        danger = bool(operation and operation.danger_dispatched)
        probe = None
        if success:
            try:
                from app.runtime.maintenance import observe_environment

                probe = await observe_environment(
                    self.environment.id,
                    persist=False,
                    apply_desired=self.action in {"deploy", "update"},
                    mark_deployed=self.action in {"deploy", "update"},
                )
                if probe.get("skipped"):
                    error = "员工存在占用任务，跳过部署后的可用性探测"
                    probe = None
            except Exception as exc:
                probe = None
                error = f"部署完成但装备核验失败：{exc}"
        probe_ok = bool(probe and not probe["stale"] and probe["status"] == EnvironmentStatus.AVAILABLE)
        if success and probe_ok:
            state = MaintenanceState.SUCCEEDED.value
            stage = "verify"
            error_code = None
        elif success and probe is not None and not probe["stale"]:
            state = MaintenanceState.FAILED.value
            stage = "verify_failed"
            error_code = "equipment_verification_failed"
            error = error or "环境装备真实核验未通过"
        elif success:
            state = MaintenanceState.INTERRUPTED.value
            stage = "interrupted"
            error_code = "health_unknown"
            error = error or "无法确认环境真实健康"
        elif danger:
            state = MaintenanceState.INTERRUPTED.value
            stage = "interrupted"
            error_code = "remote_state_unknown"
        else:
            state = MaintenanceState.FAILED.value
            stage = "failed"
            error_code = "maintenance_failed"
        if capability is None:
            capability = stored_capability(self.environment.status_detail)
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
        observation = (
            probe.get("observation")
            if probe is not None and isinstance(probe.get("observation"), dict)
            else {}
        )
        observed_opencode = (
            observation.get("opencode") if isinstance(observation.get("opencode"), dict) else {}
        )
        observed_model = observation.get("model") if isinstance(observation.get("model"), dict) else {}
        installer_repository = (
            self.installer_meta.get("repository")
            if isinstance(self.installer_meta.get("repository"), dict)
            and self.installer_meta.get("repository")
            else None
        )
        after = {
            "checks": self.steps,
            "message": redact_text(error, self.secrets),
            "probe_status": None,
            "opencode_version": installer_opencode.get("version") or observed_opencode.get("version"),
            "worker_agent_version": installer_worker.get("version")
            or observation.get("worker_agent_version"),
            "model_name": observed_model.get("model"),
            "repository": installer_repository or (probe["repository"] if probe is not None else None),
            "changed": self.installer_meta.get("changed") or {},
            "equipment": probe["equipment"] if probe else [],
        }
        previous_detail = (
            dict(self.environment.status_detail) if isinstance(self.environment.status_detail, dict) else {}
        )
        if probe is not None and not probe["stale"]:
            values = dict(probe["values"])
            detail = dict(values.get("status_detail") or previous_detail)
            if self.action in {"deploy", "update"}:
                deployed = detail.get("deployed")
                if isinstance(deployed, dict):
                    if self.git_repository_id:
                        repository_snapshot = deployed.get("repository")
                        if isinstance(repository_snapshot, dict):
                            repository_snapshot = dict(repository_snapshot)
                            repository_snapshot["credential_revision"] = self.git_credential_revision
                            deployed["repository"] = repository_snapshot
                    else:
                        deployed["repository"] = None
            values["status_detail"] = detail
            after["probe_status"] = probe["status"].value
            if state == MaintenanceState.SUCCEEDED.value:
                after["capability_revision"] = (detail.get("capability") or {}).get("applied_revision") or ""
        else:
            values = {"last_checked_at": now}
            if not success and danger:
                env_status = await self._probe_after_failure()
                after["probe_status"] = env_status.value
                # A basic health probe cannot prove model, MCP, skill and Git
                # equipment are complete. Keep the lock and force a full probe.
                values["status"] = EnvironmentStatus.UNKNOWN
        if state == MaintenanceState.SUCCEEDED.value:
            values.update(model_name=self.target_model, opencode_path=self.target_binary)
        elif state == MaintenanceState.FAILED.value and probe is not None and not probe["stale"]:
            values.update(model_name=self.target_model, opencode_path=self.target_binary)
        if self.action == "update" and self.lease is not None:
            repository = (
                self.installer_meta.get("repository")
                if isinstance(self.installer_meta.get("repository"), dict)
                else {}
            )
            remote_commit = repository.get("remote_commit")
            if remote_commit:
                current = await self.lease.snapshot()
                target = dict(current.target_revision or {}) if current is not None else {}
                if target.get("repository_commit") != remote_commit:
                    target["repository_commit"] = remote_commit
                    await self.lease.set_target_revision(target)
        applied = await self.lease.finish(
            state,
            stage=stage,
            error_code=error_code,
            error_message=redact_text(error, self.secrets) if error else None,
            after_snapshot=redact_data(after, self.secrets),
            environment_id=self.environment.id,
            environment_values=values,
            event_message="远程环境维护完成" if success else "远程环境维护未完成",
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
        capability = None
        skip_finish = False
        try:
            async with asyncio.timeout(self.settings.remote_deploy_timeout_seconds):
                self._direct_endpoints()
                await self.step(emit, "SSH 连接与执行检查", "running")
                self.ssh = await self.runtime.open_session(self.environment)
                if self.action in {"deploy", "update"}:
                    await prepare_python(self, emit)
                else:
                    await self._acquire_install_lock()
                await self._native_idle(strict=True)
                await self.step(emit, "SSH 连接与执行检查", "passed")
                if self.lease is not None:
                    await self.lease.set_execution_route("ssh", "Nexus 通过 SSH 管理 Worker 进程")
                await self._ensure_safe()
                if self.action in {"deploy", "update"}:
                    await self._install(emit)
                    await self._probe_gateway(emit)
                    await self._install_worker(emit)
                    await self._mark_inside_operation(emit)
                    capability = await self._observe_capability()
                else:
                    await self._restart_processes(emit)
                success = True
        except (MaintenanceCanceled, MaintenanceLeaseLost, MaintenanceBlocked):
            skip_finish = True
            raise
        except asyncio.CancelledError:
            skip_finish = True
            error = "操作已取消或连接中断；请核对远端状态后重试"
            raise
        except TimeoutError:
            error = "操作超时，远端残留安装受超时及互斥保护；请核对后重试"
        except Exception as exc:
            error = self.redact(exc)
        finally:
            if not success and error and not skip_finish:
                current = next(
                    (s["name"] for s in reversed(self.steps) if s["state"] == "running"),
                    "环境维护",
                )
                await self.step(emit, current, "failed", error)
            if self.install_lock is not None:
                await self.install_lock.close()
                self.install_lock = None
            if self.ssh:
                await self.ssh.close()
                self.ssh = None
            if not skip_finish:
                applied = await self._finish(success, error, capability if success else None)
                if not applied:
                    success = False
                    error = "环境配置或操作记录已变化，旧结果未写入；请刷新后核对"
                    skip_finish = True
        if not skip_finish:
            await emit(
                {
                    "type": "result",
                    "success": success,
                    "message": (
                        "部署完成"
                        if self.action == "deploy"
                        else "更新完成"
                        if self.action == "update"
                        else "重启完成"
                    )
                    if success
                    else error,
                    "status": "AVAILABLE" if success else "INTERRUPTED",
                    "checks": self.steps,
                }
            )

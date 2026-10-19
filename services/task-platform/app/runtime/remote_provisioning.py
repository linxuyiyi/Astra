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

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.config.settings import get_settings
from app.database import SessionLocal
from app.domain import EnvironmentStatus
from app.gateway.models import GatewayKey, GatewayRoute
from app.gateway.probe import HttpGatewayProbeClient
from app.gateway.runtime import RedisRuntimeStore
from app.gateway.schemas import GatewayKeyCreate
from app.gateway.security import CredentialCipher, gateway_key_digest
from app.gateway.service import GatewayControlService
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
from app.runtime.repository import cipher, collect_skill_files

PROVIDER_ID = "nexus-gateway"
E2E_MODEL = "opencode/deepseek-v4.1-flash-free"
WORKER_SECRET_DIR = "/etc/nexus"


def worker_install_dir(environment_id: int) -> str:
    return f"/opt/nexus/workers/{environment_id}"


def agent_venv(environment_id: int) -> str:
    return f"{worker_install_dir(environment_id)}/venv"


def agent_python(environment_id: int) -> str:
    return f"{agent_venv(environment_id)}/bin/python"


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
    # Re-read inside the lock so an identity-map copy cannot hide a new fact.
    await session.refresh(environment)


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
    ]
    names.extend(f"app/worker_agent/{path.name}" for path in sorted((app_root / "worker_agent").glob("*.py")))
    files = sorted((name, (app_root.parent / name).read_bytes()) for name in set(names))
    info = f"{dist}.dist-info"
    metadata = (
        "Metadata-Version: 2.1\n"
        "Name: nexus-worker-agent\n"
        f"Version: {version}\n"
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
    mcp_key,
    e2e=False,
    skills=None,
    mcp_refs=None,
):
    mcp = {"type": "remote", "url": mcp_url, "enabled": True, "oauth": False}
    if mcp_key:
        mcp["headers"] = {"Authorization": f"Bearer {mcp_key}"}
    provider = None
    if not e2e:
        provider = {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Nexus API 中转",
            "options": {"baseURL": gateway_url, "apiKey": gateway_key},
            "models": {name: {"name": name} for name in models},
        }
    return {
        "config_path": config_path,
        "model": E2E_MODEL if e2e else f"{PROVIDER_ID}/{model}",
        "provider": provider,
        "mcp": mcp,
        "mcp_refs": list(mcp_refs or []),
        "skills": list(skills or []),
        "e2e": e2e,
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
        self.secrets = [environment.ssh_password, self.settings.mcp_api_key]
        self.ssh = None
        self.new_key_id = None
        self.old_key_id = None
        self.key_applied = False
        self.target_model = environment.model_name
        self.target_binary = environment.opencode_path
        self.config_path = None
        self.home: str | None = None
        self.installer_meta: dict = {}

    def _agent_interpreter(self) -> str:
        configured = (self.settings.remote_python or "").strip()
        return configured or "python3"

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

    async def _ensure_safe(self, *, allow_occupied: bool = False):
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

    async def _native_idle(self, *, strict: bool = False):
        port = int(self.environment.opencode_server_port)
        query = urlencode({"directory": self.environment.workspace_root})
        url = f"http://127.0.0.1:{port}/session/status?{query}"
        script = (
            "import json,urllib.request\n"
            f"url={json.dumps(url)}\n"
            "try:\n"
            "    print(urllib.request.urlopen(url, timeout=3).read().decode())\n"
            "except Exception:\n"
            "    pass\n"
        )
        out = await self._command(f"python3 -c {shlex.quote(script)} || true")
        if not out:
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

    async def _opencode_unit_active(self) -> bool:
        unit = f"nexus-opencode-{self.environment.id}.service"
        try:
            state = await self._command(f"systemctl is-active {shlex.quote(unit)} || true")
        except Exception:
            return True
        return state.strip() == "active"

    async def _native_idle_online(self):
        from app.runtime.worker_client import WorkerOpenCodeClient

        client = WorkerOpenCodeClient(self.settings, environment_id=self.environment.id)
        try:
            states = await client.request(
                "GET", "/session/status", directory=Path(self.environment.workspace_root)
            )
        except Exception as exc:
            raise RuntimeError(f"无法确认 OpenCode 原生 Session 状态，不能重启：{exc}") from exc
        self._assert_native_idle(states)

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
                    name=f"数字员工 {self.environment.employee_id}", owner_id=self.environment.employee_id
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
        e2e = (self.environment.model_name or "").strip() == E2E_MODEL
        if e2e:
            models: list[str] = []
            selected = E2E_MODEL
        else:
            async with SessionLocal() as db:
                models = list(
                    await db.scalars(
                        select(GatewayRoute.model)
                        .where(GatewayRoute.enabled.is_(True))
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
        raw = "" if e2e else await self._gateway_key(config_path, models)
        gateway_url, mcp_url = await self._channel_endpoints()
        self.target_model = E2E_MODEL if e2e else f"{PROVIDER_ID}/{selected}"
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
            mcp_key=self.settings.mcp_api_key,
            e2e=e2e,
            skills=collect_skill_files(list(self.environment.skill_refs or []), skill_source),
            mcp_refs=list(self.environment.mcp_refs or []),
        )
        payload.update(
            {
                "action": "update" if self.action == "update" else "deploy",
                "environment_id": self.environment.id,
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
            token_file = stem + ".token"
            await self.ssh.write_file(token_file, token.encode(), mode=0o600)
            payload["repository"] = {
                "url": repository.url,
                "branch": self.environment.branch or repository.branch,
            }
        jsonc_file = stem + ".jsonc.py"
        await self.ssh.write_file(stem + ".json", json.dumps(payload, ensure_ascii=False).encode())
        await self.ssh.write_file(
            jsonc_file, Path(__file__).resolve().parents[1].joinpath("worker_agent", "jsonc.py").read_bytes()
        )
        await self.ssh.write_file(stem + ".sh", Path(__file__).with_name("remote_install.sh").read_bytes())
        await self.step(emit, "部署配置", "passed", "已准备员工专属中转、数字图书馆与安全连接")
        args = [
            self.environment.workspace_root,
            self.target_binary,
            self.settings.remote_node_version,
            self.settings.remote_opencode_version,
            self.settings.remote_npm_registry,
            stem + ".json",
            self.operation_id,
            self._agent_interpreter(),
            jsonc_file,
            token_file,
        ]
        command = (
            f"timeout -k 10 {self.settings.remote_deploy_timeout_seconds} bash {shlex.quote(stem + '.sh')} "
            + shlex.join(args)
        )
        # The remote script may merge the new key into the config even when it
        # later fails, so never revoke the new key after this point.
        self.key_applied = True
        if self.lease is not None:
            await self.lease.mark_danger()
        channel = await self.ssh.open_exec(command)
        stderr_task = asyncio.create_task(channel.stderr.read())
        tail = ""
        try:
            while line := await channel.stdout.readline():
                text = line.decode(errors="replace").strip()
                if text.startswith("NEXUS_STEP\t"):
                    _, name, state = text.split("\t", 2)
                    await self.step(emit, name, state)
                elif text.startswith("NEXUS_META\t"):
                    try:
                        self.installer_meta = json.loads(text.split("\t", 1)[1])
                    except json.JSONDecodeError:
                        self.installer_meta = {}
                else:
                    tail = (tail + "\n" + text)[-2000:]
            rc = await channel.wait()
            err = (await stderr_task).decode(errors="replace")
            if rc:
                raise RuntimeError(self.redact(err or tail or f"安装退出码 {rc}"))
        finally:
            await channel.close()
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            leftovers = [stem + ".sh", stem + ".json", jsonc_file]
            if token_file:
                leftovers.append(token_file)
            await self._command("rm -f " + " ".join(shlex.quote(path) for path in leftovers))

    def _direct_endpoints(self) -> tuple[str, str]:
        return (
            require_direct_url(self.settings.remote_gateway_local_url, "Gateway"),
            require_direct_url(self.settings.mcp_resource_server_url, "图书馆 MCP"),
        )

    async def _channel_endpoints(self) -> tuple[str, str]:
        return self._direct_endpoints()

    async def _restart_via_agent(self, emit):
        await self.step(emit, "重启 OpenCode Serve", "running")
        await self._ensure_safe()
        from app.runtime.worker_client import WorkerOpenCodeClient

        if self.lease is not None:
            await self.lease.mark_danger()
        client = WorkerOpenCodeClient(self.settings, environment_id=self.environment.id)
        attempt = self.lease.attempt if self.lease is not None else 0
        await client.run_command(
            "restart_opencode",
            {
                "workspace": self.environment.workspace_root,
                "port": self.environment.opencode_server_port,
            },
            request_id=f"maint:{self.operation_id}:{attempt}",
        )
        health = await client.health()
        await self.step(emit, "重启 OpenCode Serve", "passed", f"健康 · OpenCode {health.get('version')}")

    async def _probe_gateway(self, emit):
        await self.step(emit, "模型中转连接", "running")
        probe = (
            "const fs=require('fs');const c=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));"
            "const p=c.provider['nexus-gateway'];"
            "async function main(){for(let i=0;i<15;i++){"
            "const r=await fetch(p.options.baseURL+'/models',{headers:{Authorization:'Bearer '+"
            "p.options.apiKey},signal:AbortSignal.timeout(5000)});"
            "if(r.ok){const d=await r.json();const id=c.model.slice('nexus-gateway/'.length);"
            "if(!d.data?.some(m=>m.id===id))throw Error('中转未提供员工模型');"
            "console.log('模型中转认证与模型路由正常');return;}"
            "if(r.status!==401)throw Error('中转 HTTP '+r.status);"
            "await new Promise(r=>setTimeout(r,2000));}throw Error('中转 Key 未生效');}"
            "main().catch(e=>{console.error(e.message);process.exit(1)})"
        )
        detail = await self._command(
            'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"; '
            f"node -e {shlex.quote(probe)} {shlex.quote(self.config_path)}",
            timeout=50,
        )
        await self.step(emit, "模型中转连接", "passed", detail)

    async def _read_optional(self, path: str) -> bytes | None:
        try:
            return await self.ssh.read_file(path)
        except Exception:
            return None

    async def _write_if_changed(self, path: str, data: bytes, mode: int = 0o644) -> bool:
        existing = await self._read_optional(path)
        if existing == data:
            return False
        await self.ssh.write_file(path, data, mode=mode)
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

    async def _install_units(self, emit):
        await self.step(emit, "安装 Worker Agent", "running")
        await self._ensure_safe()
        gateway_url, mcp_url = self._direct_endpoints()
        agent_gateway = require_direct_url(self.settings.worker_agent_gateway_url, "Worker Gateway")
        port = int(self.environment.opencode_server_port)
        home = self.home or "/opt/nexus"
        env_root = f"{home}/.local/share/nexus/workers/{self.environment.id}"
        node_root = f"{env_root}/node-v{self.settings.remote_node_version}"
        npm_bin = f"{env_root}/npm/bin"
        install_dir = worker_install_dir(self.environment.id)
        venv = agent_venv(self.environment.id)
        python = agent_python(self.environment.id)
        opencode_unit = f"nexus-opencode-{self.environment.id}.service"
        worker_unit = f"nexus-worker-{self.environment.id}.service"
        token_path = f"{WORKER_SECRET_DIR}/worker-{self.environment.id}.env"
        opencode_url = f"http://127.0.0.1:{port}"
        timeout = int(self.settings.opencode_control_timeout_seconds)
        opencode_config = self.config_path or (f"/etc/nexus/opencode/{self.environment.id}/opencode.jsonc")
        opencode_dir = str(Path(opencode_config).parent)
        path_value = ":".join([f"{node_root}/bin", npm_bin, "/usr/local/bin", "/usr/bin", "/bin"])
        opencode_unit_text = (
            "[Unit]\nDescription=Nexus OpenCode\nAfter=network-online.target\n"
            "[Service]\n"
            f"WorkingDirectory={self.environment.workspace_root}\n"
            f"Environment=PATH={path_value}\n"
            f"Environment=OPENCODE_CONFIG={opencode_config}\n"
            f"Environment=OPENCODE_CONFIG_DIR={opencode_dir}\n"
            f"ExecStart={self.target_binary} serve --hostname 127.0.0.1 --port {port}\n"
            "Restart=on-failure\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        agent_unit_text = (
            "[Unit]\nDescription=Nexus Worker Agent\nAfter=network-online.target\n"
            "[Service]\n"
            f"WorkingDirectory={self.environment.workspace_root}\n"
            f"Environment=PATH={path_value}\n"
            f"EnvironmentFile={token_path}\n"
            f"ExecStart={python} -m app.worker_agent\n"
            "Restart=on-failure\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        wheel_name, wheel_bytes = agent_wheel()
        wheel_digest = hashlib.sha256(wheel_bytes).hexdigest()
        wheel_path = f"{install_dir}/{wheel_name}"
        marker_path = f"{install_dir}/wheel.sha256"
        await self._command(f"install -d -m 755 {shlex.quote(install_dir)} {shlex.quote(env_root)}")
        await self._command(f"install -d -m 700 {WORKER_SECRET_DIR} /etc/systemd/system")
        marker = (await self._command(f"cat {shlex.quote(marker_path)} || true")).strip()
        interpreter = shlex.quote(self._agent_interpreter())
        venv_python = f"{venv}/bin/python"
        venv_ok = (
            marker == wheel_digest
            and (await self._command(f"test -x {shlex.quote(venv_python)} && echo yes || true")).strip()
            == "yes"
        )
        wheel_changed = False
        if not venv_ok:
            await self._ensure_safe()
            await self._write_if_changed(wheel_path, wheel_bytes, mode=0o644)
            index = (self.settings.remote_pip_index_url or "").strip()
            pip_index = f"-i {shlex.quote(index)} " if index else ""
            pip = (
                f"{python} -m pip install --disable-pip-version-check --upgrade --force-reinstall "
                f"{pip_index}{shlex.quote(wheel_path)}"
            )
            await self._command(
                f"{interpreter} -m venv {shlex.quote(venv)} && {pip}",
                timeout=300,
            )
            await self._command(f"printf '%s' {shlex.quote(wheel_digest)} > {shlex.quote(marker_path)}")
            wheel_changed = True

        from app.worker_gateway.service import get_gateway

        await self._ensure_safe()
        existing = await self._existing_worker_credentials(token_path)
        if existing is not None:
            token, worker_instance_id = existing
        else:
            async with SessionLocal() as session:
                enrolled = await get_gateway().enroll(session, self.environment.id)
            token, worker_instance_id = enrolled.token, enrolled.worker_instance_id
        self.secrets.append(token)
        env = (
            "\n".join(
                [
                    f"NEXUS_WORKER_TOKEN={token}",
                    f"NEXUS_WORKER_GATEWAY_URL={agent_gateway}",
                    f"NEXUS_WORKER_INSTANCE_ID={worker_instance_id}",
                    f"NEXUS_WORKER_ENVIRONMENT_ID={self.environment.id}",
                    f"NEXUS_REMOTE_GATEWAY_URL={gateway_url}",
                    f"NEXUS_MCP_RESOURCE_URL={mcp_url}",
                    f"NEXUS_WORKER_OPENCODE_URL={opencode_url}",
                    f"NEXUS_WORKER_OPENCODE_UNIT={opencode_unit}",
                    f"NEXUS_WORKER_OPENCODE_BIN={self.target_binary}",
                    f"OPENCODE_CONFIG={opencode_config}",
                    f"OPENCODE_CONFIG_DIR={opencode_dir}",
                    f"NEXUS_WORKER_WORKSPACE_ROOT={self.environment.workspace_root}",
                    f"NEXUS_OPENCODE_CONTROL_TIMEOUT_SECONDS={timeout}",
                ]
            )
            + "\n"
        )
        await self._ensure_safe()
        env_changed = await self._write_if_changed(token_path, env.encode(), mode=0o600)
        opencode_unit_changed = await self._write_if_changed(
            f"/etc/systemd/system/{opencode_unit}", opencode_unit_text.encode(), mode=0o644
        )
        worker_unit_changed = await self._write_if_changed(
            f"/etc/systemd/system/{worker_unit}", agent_unit_text.encode(), mode=0o644
        )
        units_changed = opencode_unit_changed or worker_unit_changed
        await self._ensure_safe()
        readable = await self._native_idle(strict=False)
        if not readable and await self._opencode_unit_active():
            raise RuntimeError("OpenCode 服务正在运行但原生状态不可读，不能继续重启")
        await self._ensure_safe()
        changed = self.installer_meta.get("changed") if isinstance(self.installer_meta, dict) else {}
        runtime_changed = bool(isinstance(changed, dict) and changed.get("runtime"))
        configuration_changed = bool(
            isinstance(changed, dict) and (changed.get("configuration") or changed.get("skills"))
        )
        # Restart OpenCode only when its own unit, runtime, or configuration
        # changed. A Worker-Agent wheel/env/unit update must not bounce it.
        restart_opencode = (
            opencode_unit_changed or runtime_changed or configuration_changed
        )
        restart_worker = worker_unit_changed or env_changed or wheel_changed
        if self.lease is not None:
            await self.lease.mark_danger()
        commands = [
            "systemctl daemon-reload" if (units_changed or wheel_changed) else None,
            f"systemctl enable {shlex.quote(opencode_unit)} {shlex.quote(worker_unit)}",
        ]
        if restart_opencode:
            commands.append(f"systemctl restart {shlex.quote(opencode_unit)}")
        else:
            commands.append(f"systemctl start {shlex.quote(opencode_unit)}")
        if restart_worker:
            commands.append(f"systemctl restart {shlex.quote(worker_unit)}")
        else:
            commands.append(f"systemctl start {shlex.quote(worker_unit)}")
        await self._command(" && ".join(item for item in commands if item))
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

    async def _restart_offline(self, emit):
        await self.step(emit, "重启 OpenCode Serve", "running")
        await self._ensure_safe()
        await self._native_idle(strict=True)
        await self._ensure_safe()
        unit = f"nexus-opencode-{self.environment.id}.service"
        worker_unit = f"nexus-worker-{self.environment.id}.service"
        if self.lease is not None:
            await self.lease.mark_danger()
        await self._command(f"systemctl restart {shlex.quote(unit)} {shlex.quote(worker_unit)}")
        await self.step(emit, "重启 OpenCode Serve", "passed", "已通过 SSH 重启单元")

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
        after = {
            "checks": self.steps,
            "message": redact_text(error, self.secrets),
            "probe_status": None,
            "opencode_version": (self.installer_meta.get("opencode") or {}).get("version")
            or self.settings.remote_opencode_version,
            "worker_agent_version": __import__("app.worker_agent", fromlist=["__version__"]).__version__,
            "model_name": self.target_model,
            "repository": self.installer_meta.get("repository") or {},
            "changed": self.installer_meta.get("changed") or {},
            "equipment": probe["equipment"] if probe else [],
        }
        previous_detail = (
            dict(self.environment.status_detail) if isinstance(self.environment.status_detail, dict) else {}
        )
        if probe is not None and not probe["stale"]:
            values = dict(probe["values"])
            detail = dict(values.get("status_detail") or previous_detail)
            deployed = detail.get("deployed") if isinstance(detail.get("deployed"), dict) else {}
            deployed["opencode"] = {"version": after["opencode_version"]}
            deployed["worker_agent"] = {"version": after["worker_agent_version"]}
            deployed["model"] = {"name": self.target_model}
            deployed["repository"] = after["repository"]
            deployed["changed"] = after["changed"]
            detail["deployed"] = deployed
            values["status_detail"] = detail
            after["probe_status"] = probe["status"].value
            if state == MaintenanceState.SUCCEEDED.value:
                after["capability_revision"] = (detail.get("capability") or {}).get("applied_revision") or ""
        else:
            values = {"last_checked_at": now}
            if not success and danger:
                env_status = await self._probe_after_failure()
                after["probe_status"] = env_status.value
                values["status"] = env_status
        if state == MaintenanceState.SUCCEEDED.value:
            values.update(model_name=self.target_model, opencode_path=self.target_binary)
        elif state == MaintenanceState.FAILED.value and probe is not None and not probe["stale"]:
            values.update(model_name=self.target_model, opencode_path=self.target_binary)
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

    async def _worker_is_online(self) -> bool:
        from app.models import WorkerInstance

        async with SessionLocal() as session:
            instance = await session.scalar(
                select(WorkerInstance).where(WorkerInstance.environment_id == self.environment.id)
            )
            return instance is not None and instance.status == "online"

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
                if self.action == "restart" and await self._worker_is_online():
                    await self._ensure_safe()
                    await self._native_idle_online()
                    await self._ensure_safe()
                    await self._restart_via_agent(emit)
                    success = True
                else:
                    await self.step(emit, "SSH 连接与执行检查", "running")
                    self.ssh = await self.runtime.open_session(self.environment)
                    readable = await self._native_idle(strict=self.action == "restart")
                    if (
                        not readable
                        and self.action in {"deploy", "update"}
                        and await self._opencode_unit_active()
                    ):
                        raise RuntimeError("OpenCode 服务正在运行但原生状态不可读，不能当作未运行继续部署")
                    await self.step(emit, "SSH 连接与执行检查", "passed")
                    await self._ensure_safe()
                    if self.action in {"deploy", "update"}:
                        await self._install(emit)
                        await self._probe_gateway(emit)
                        await self._install_units(emit)
                        await self._mark_inside_operation(emit)
                        capability = await self._observe_capability()
                    else:
                        await self._restart_offline(emit)
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

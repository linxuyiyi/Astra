"""Maintenance switch from the SSH tunnels to the intranet Gateway and MCP.

The Worker reads the two addresses from its own environment. A command payload
cannot supply them. OpenCode is reloaded only from this operation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from app.worker_agent.jsonc import loads_jsonc
from app.worker_agent.process_manager import manage_process, managed_running

_GATEWAY_ENV = "NEXUS_REMOTE_GATEWAY_URL"
_MCP_ENV = "NEXUS_MCP_RESOURCE_URL"
_PROVIDER = "nexus-gateway"
_DEFAULT_GATEWAY = "http://127.0.0.1:8110"
_DEFAULT_MCP = "http://127.0.0.1:8201/mcp"
_HEALTH_WAIT_SECONDS = 15
_EXIT_WAIT_SECONDS = 5
_UNIT_NAME = re.compile(r"^[A-Za-z0-9_:.@-]+$")

Probe = Callable[[str], bool]
Reload = Callable[[], None]
ReadEffective = Callable[[], dict]
Gate = Callable[[], None]


def global_config_path(environ: dict[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    explicit = (env.get("OPENCODE_CONFIG") or "").strip()
    if explicit:
        return Path(explicit)
    home = env.get("XDG_CONFIG_HOME")
    if home:
        root = Path(home) / "opencode"
    else:
        root = Path(env.get("HOME") or str(Path.home())) / ".config" / "opencode"
    jsonc = root / "opencode.jsonc"
    legacy = root / "opencode.json"
    if jsonc.is_file() or not legacy.is_file():
        return jsonc
    return legacy


def direct_urls(environ: dict[str, str] | None = None) -> tuple[str, str]:
    env = environ if environ is not None else os.environ
    return env.get(_GATEWAY_ENV) or _DEFAULT_GATEWAY, env.get(_MCP_ENV) or _DEFAULT_MCP


def probe_url(url: str, *, timeout: float = 3) -> bool:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
            return True
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _read_config(path: Path) -> dict:
    try:
        config = loads_jsonc(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("全局 OpenCode 配置不是有效 JSON，请人工处理，未覆盖配置") from exc
    if not isinstance(config, dict):
        raise RuntimeError("全局配置必须是 JSON 对象")
    return config


def _config_target(path: Path) -> Path | None:
    jsonc = path if path.suffix == ".jsonc" else path.with_suffix(".jsonc")
    if jsonc.is_file():
        return jsonc
    if path.is_file():
        return path
    return None


def config_addresses(path: Path) -> tuple[str, str] | None:
    target = _config_target(path)
    if target is None:
        return None
    try:
        config = _read_config(target)
    except RuntimeError:
        return None
    gateway = _mapping(_mapping(_mapping(config, "provider"), _PROVIDER), "options")
    library = _mapping(_mapping(config, "mcp"), "nexus-library")
    base = gateway.get("baseURL") if isinstance(gateway, dict) else None
    mcp = library.get("url") if isinstance(library, dict) else None
    if not isinstance(base, str) or not isinstance(mcp, str):
        return None
    return base, mcp


def write_direct_config(path: Path, *, gateway_url: str, mcp_url: str) -> bool:
    """Replace only the two direct addresses. Keep the Gateway key and MCP token.

    An existing opencode.jsonc is merged. It is not a reason to stop.
    """

    jsonc = path if path.suffix == ".jsonc" else path.with_suffix(".jsonc")
    if jsonc.is_file():
        config = _read_config(jsonc)
        target = jsonc
        gateway = _mapping(_mapping(_mapping(config, "provider"), _PROVIDER), "options")
        legacy_path = jsonc.with_suffix(".json")
        if gateway is None and legacy_path.is_file():
            legacy = _read_config(legacy_path)
            provider = _mapping(_mapping(legacy, "provider"), _PROVIDER)
            if isinstance(provider, dict):
                providers = config.get("provider")
                if not isinstance(providers, dict):
                    providers = {}
                    config["provider"] = providers
                providers[_PROVIDER] = provider
            library = _mapping(_mapping(legacy, "mcp"), "nexus-library")
            if isinstance(library, dict):
                mcp = config.get("mcp")
                if not isinstance(mcp, dict):
                    mcp = {}
                    config["mcp"] = mcp
                mcp.setdefault("nexus-library", library)
    elif path.is_file():
        config = _read_config(path)
        target = path
    else:
        raise RuntimeError("全局 OpenCode 配置不存在，未重载")
    options = _mapping(_mapping(_mapping(config, "provider"), _PROVIDER), "options")
    library = _mapping(_mapping(config, "mcp"), "nexus-library")
    if options is None or "apiKey" not in options or "baseURL" not in options:
        raise RuntimeError("全局配置缺少 nexus-gateway，未重载")
    if library is None or "url" not in library:
        raise RuntimeError("全局配置缺少 nexus-library，未重载")
    if options.get("baseURL") == gateway_url and library.get("url") == mcp_url:
        return False
    backup = target.with_name(target.name + ".nexus-backup")
    shutil.copy2(target if target.is_file() else path, backup)
    os.chmod(backup, 0o600)
    options["baseURL"] = gateway_url
    library["url"] = mcp_url
    temporary = target.with_name(target.name + ".tmp-switch")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    return True


def read_effective_config(port: int, workspace: str, *, timeout: float = 5) -> dict:
    """Read the running merge. A project opencode.json can override the global file."""

    query = urllib.parse.urlencode({"directory": workspace})
    request = urllib.request.Request(f"http://127.0.0.1:{port}/config?{query}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("读取生效配置失败") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("生效配置不是 JSON 对象")
    return payload


def _listener_pid(port: int) -> int | None:
    needle = f"{_ipv4_hex('127.0.0.1')}:{port:04X}"
    tcp = Path("/proc/net/tcp")
    try:
        lines = tcp.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    inode = None
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[1].upper() != needle or fields[3] != "0A":
            continue
        inode = fields[9]
        break
    if inode is None:
        return None
    wanted = f"socket:[{inode}]"
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            descriptors = list(fd_dir.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target == wanted:
                return int(entry.name)
    return None


def _check_gate(gate: Gate | None) -> None:
    if gate is not None:
        gate()


def _probe_listener(port: int, *, timeout: float = 1.0) -> bool:
    """只有明确的 ECONNREFUSED 才能证明端口上没有监听者。"""

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(("127.0.0.1", port))
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        raise RuntimeError("无法确认 OpenCode 监听状态，拒绝执行维护") from exc
    finally:
        probe.close()
    return True


def _read_native_status(port: int, workspace: str) -> None:
    query = urllib.parse.urlencode({"directory": workspace})
    url = f"http://127.0.0.1:{port}/session/status?{query}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            body = response.read(65536)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise RuntimeError("无法确认 OpenCode 原生 Session 状态，拒绝执行维护") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("无法确认 OpenCode 原生 Session 状态，拒绝执行维护") from exc
    try:
        states = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("无法确认 OpenCode 原生 Session 状态，拒绝执行维护") from exc
    if not isinstance(states, dict):
        raise RuntimeError("无法确认 OpenCode 原生 Session 状态，拒绝执行维护")
    busy = False
    malformed = False
    for item in states.values():
        if not isinstance(item, dict):
            malformed = True
            continue
        kind = item.get("type")
        if kind in {"busy", "retry"}:
            busy = True
        elif kind != "idle":
            malformed = True
    if busy:
        raise RuntimeError("OpenCode 存在正在执行的 Session，请先停止执行再维护环境")
    if malformed:
        raise RuntimeError("无法确认 OpenCode 原生 Session 状态，拒绝执行维护")


def assert_native_idle(workspace: str, port: int, *, gate: Gate | None = None) -> bool:
    """只读检查本机原生 Session。返回端口上是否已有监听者。

    空对象表示空闲；只有明确的连接拒绝才视为没有运行中的 OpenCode。
    Timeout 或其它 OSError 都保守拒绝。
    """

    _check_gate(gate)
    if not _probe_listener(port):
        return False
    _read_native_status(port, workspace)
    return True


def restart_local_opencode(
    workspace: str,
    port: int,
    *,
    environ: dict[str, str] | None = None,
    gate: Gate | None = None,
) -> None:
    folder = Path(workspace)
    if not folder.is_dir():
        raise RuntimeError("Workspace 不存在，未重载 OpenCode")
    if port < 1 or port > 65535:
        raise RuntimeError("OpenCode 端口无效")
    listener = assert_native_idle(workspace, port, gate=gate)
    env = environ if environ is not None else os.environ
    record = env.get("NEXUS_WORKER_OPENCODE_PROCESS_RECORD")
    if record:
        path = Path(record)
        if listener and not managed_running(path):
            raise RuntimeError("监听进程缺少受管记录，未重载 OpenCode")
        _check_gate(gate)
        binary = env.get("NEXUS_WORKER_OPENCODE_BIN") or "opencode"
        manage_process(
            path,
            command=[binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
            workspace=workspace,
            log_path=path.with_suffix(".log"),
            environ=dict(env),
            restart=True,
            opencode=True,
            gate=gate,
        )
        _wait_until_healthy(port, gate=gate)
        return
    raise RuntimeError("Worker 缺少受管进程记录，请先通过 Nexus 重新部署，不接管未知进程")


def _wait_until_healthy(port: int, *, gate: Gate | None = None) -> None:
    """GET /global/health 必须是 2xx JSON 且 healthy=true，401/404 不算健康。"""

    health = f"http://127.0.0.1:{port}/global/health"
    deadline = time.monotonic() + _HEALTH_WAIT_SECONDS
    while time.monotonic() < deadline:
        _check_gate(gate)
        payload: object = None
        try:
            request = urllib.request.Request(health, method="GET")
            with urllib.request.urlopen(request, timeout=0.5) as response:
                body = response.read(65536)
            payload = json.loads(body.decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            payload = None
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("healthy") is True:
            return
        time.sleep(0.2)
    raise RuntimeError("OpenCode 重载后健康检查失败")


def apply_channel_switch(
    *,
    workspace: str,
    port: int,
    config_path: Path | None = None,
    environ: dict[str, str] | None = None,
    probe: Probe | None = None,
    reload: Reload | None = None,
    read_effective: ReadEffective | None = None,
    gate: Gate | None = None,
) -> dict[str, str | bool]:
    _check_gate(gate)
    if port < 1 or port > 65535:
        raise RuntimeError("OpenCode 端口无效")
    gateway_url, mcp_url = direct_urls(environ)
    probe_fn = probe or probe_url
    for url in (gateway_url, mcp_url):
        if not probe_fn(url):
            raise RuntimeError("内网 Gateway 或 MCP 探测失败")
    path = config_path or global_config_path(environ)
    read_running = read_effective or (lambda: read_effective_config(port, workspace))

    def read_addresses() -> tuple[dict | None, tuple[str, str] | None]:
        try:
            effective = read_running()
        except RuntimeError:
            return None, None
        try:
            return effective, (
                _address(effective, ("provider", _PROVIDER, "options", "baseURL")),
                _address(effective, ("mcp", "nexus-library", "url")),
            )
        except RuntimeError:
            return effective, None

    file_addresses = config_addresses(path)
    _initial_effective, running_addresses = read_addresses()
    if file_addresses == (gateway_url, mcp_url) and running_addresses == (
        gateway_url,
        mcp_url,
    ):
        # Already applied on disk and in the running process: no write, no reload.
        return {
            "gateway_url": gateway_url,
            "mcp_url": mcp_url,
            "reloaded": False,
            "verified": True,
        }

    # Any write/reload stays behind the existing native-idle and owner gate.
    _check_gate(gate)
    assert_native_idle(workspace, port, gate=gate)
    _check_gate(gate)
    write_direct_config(path, gateway_url=gateway_url, mcp_url=mcp_url)
    if reload is None:
        restart_local_opencode(workspace, port, environ=environ, gate=gate)
    else:
        _check_gate(gate)
        reload()
    _check_gate(gate)
    effective, running = read_addresses()
    verified = running == (gateway_url, mcp_url)
    if effective is None:
        raise RuntimeError("读取生效配置失败")
    return {
        "gateway_url": _address(effective, ("provider", _PROVIDER, "options", "baseURL")),
        "mcp_url": _address(effective, ("mcp", "nexus-library", "url")),
        "reloaded": True,
        "verified": verified,
    }


def _mapping(value: object, key: str) -> dict | None:
    if not isinstance(value, dict):
        return None
    child = value.get(key)
    return child if isinstance(child, dict) else None


def _address(config: dict, path: tuple[str, ...]) -> str:
    current: object = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            raise RuntimeError("生效配置缺少 Gateway 或 MCP 地址")
        current = current[key]
    if not isinstance(current, str) or not current:
        raise RuntimeError("生效配置缺少 Gateway 或 MCP 地址")
    return current


def _ipv4_hex(address: str) -> str:
    parts = [int(part) for part in address.split(".")]
    return "".join(f"{part:02X}" for part in reversed(parts))

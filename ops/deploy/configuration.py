"""平台部署配置：不执行 dotenv 内容；统一诊断、选择、备份和原子发布。"""

from __future__ import annotations

import base64
import datetime
import getpass
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
from pathlib import Path
from urllib.parse import urlsplit


class DeploymentError(RuntimeError):
    pass


def hidden(key: str, value: str) -> str:
    if any(
        word in key.upper()
        for word in (
            "PASSWORD",
            "TOKEN",
            "SECRET",
            "KEY",
            "SQL_URL",
            "DATABASE_URL",
            "REDIS_URL",
        )
    ):
        return "<已配置，已脱敏>" if value else "<空>"
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query:
            return f"{parsed.scheme}://{parsed.hostname or '<地址>'}/<已脱敏>"
    return value[:180]


class Dialog:
    def __init__(self, interactive=True):
        self.interactive = interactive

    def ask(self, label, default="", secret=False):
        if not self.interactive:
            raise DeploymentError(
                f"需要交互补齐或确认：{label}；请通过 --env-file 提供正确值"
            )
        prompt = label + (f" [{default}]" if default and not secret else "") + ": "
        return (getpass.getpass(prompt) if secret else input(prompt)).strip() or default

    def resolve(self, key, current, proposed, reason, *, regenerable=False):
        print(
            f"[配置诊断] {key}: {reason}；当前={hidden(key, current)}；建议={hidden(key, proposed)}"
        )
        choice = self.ask("保留(k)、修改(m)、重新生成/采用建议(r)", "k")
        if choice == "k":
            return current
        if choice == "m":
            return self.ask(key, secret=hidden(key, current).startswith("<"))
        if choice == "r" and (proposed or regenerable):
            return proposed
        raise DeploymentError("未选择有效的配置处理方式")


def parse_env(path: Path, dialog: Dialog) -> dict[str, str]:
    result = {}
    if not path.is_file():
        return result
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):  # noqa: FURB188 - bootstrap supports Python 3.8
            line = line[7:]
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not match:
            raise DeploymentError(
                f"{path.name}:{number} 不是 KEY=VALUE；配置不会作为 Shell 执行"
            )
        key, value = match.groups()
        try:
            quoted = value.strip()
            if quoted.startswith('"'):
                decoded, end = json.JSONDecoder().raw_decode(quoted)
                if not isinstance(decoded, str) or (
                    quoted[end:].strip() and not quoted[end:].lstrip().startswith("#")
                ):
                    raise ValueError()
                tokens = [decoded]
            else:
                tokens = shlex.split(value, comments=True)
        except ValueError:
            raise DeploymentError(f"{path.name}:{number} 引号不完整") from None
        value = " ".join(tokens)
        if "$'" in match.group(2) or "\x00" in value or "\n" in value:
            raise DeploymentError(f"{path.name}:{number} 包含不支持的值，请重新输入")
        if key in result:
            selected = dialog.resolve(
                key, result[key], value, f"{path.name}:{number} 重复定义"
            )
            if selected == result[key] and result[key] != value:
                raise DeploymentError(f"{key} 重复值未解决")
            value = selected
        result[key] = value
    return result


def load_config(paths, environ, dialog):
    result = {}
    for path in paths:
        for key, value in parse_env(path, dialog).items():
            if key in result and result[key] != value:
                value = dialog.resolve(key, result[key], value, f"与 {path.name} 冲突")
            result[key] = value
    for key, value in environ.items():
        if key.startswith(("NEXUS_", "ASTRA_", "AGENTSCOPE_")):
            if key in result and result[key] != value:
                value = dialog.resolve(key, result[key], value, "与当前进程环境冲突")
            result[key] = value
    return result


def atomic_write(path: Path, content: str, *, mode=0o600):
    if path.is_file() and path.read_text() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        backup = path.with_name(path.name + ".backup-" + stamp)
        shutil.copy2(path, backup)
        backup.chmod(0o600)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as stream:
        os.chmod(temp, mode)
        stream.write(content)
    temp.replace(path)
    return True


def save_env(path, values):
    return atomic_write(
        path,
        "# Nexus 部署器生成；唯一运行配置，禁止提交密钥\n"
        + "".join(
            f"{key}={json.dumps(str(value), ensure_ascii=False)}\n"
            for key, value in sorted(values.items())
        ),
    )


def ipv4_addresses():
    import subprocess

    try:
        data = json.loads(
            subprocess.check_output(
                ["ip", "-j", "-4", "address", "show", "scope", "global"], timeout=10
            )
        )
        found = [
            entry["local"]
            for interface in data
            for entry in interface.get("addr_info", [])
            if entry.get("family") == "inet"
        ]
    except (OSError, ValueError, subprocess.SubprocessError):
        found = socket.gethostbyname_ex(socket.gethostname())[2]
    candidates = [
        value for value in found if not ipaddress.ip_address(value).is_loopback
    ]
    return sorted(
        set(candidates), key=lambda value: (not value.startswith("7."), value)
    )


def usable_address(value):
    try:
        address = ipaddress.ip_address(value)
        if (
            address.version != 4
            or address.is_loopback
            or address.is_unspecified
            or address.is_multicast
        ):
            return False
        with socket.socket() as sock:
            sock.bind((value, 0))
        return True
    except (ValueError, OSError):
        return False


def initialize(values, root, state, dialog, addresses=None):
    values = dict(values)
    # 无法判断的旧参数必须处理，不能因为文件存在就继续使用。
    obsolete = {
        "NEXUS_BIND_HOST",
        "NEXUS_REMOTE_DOWNLOAD_BASE_URL",
        "NEXUS_BRANCH",
        "NEXUS_REPO_URL",
        "ASTRA_DATABASE_PASSWORD",
        "ASTRA_DATABASE_HOST",
        "ASTRA_DATABASE_PORT",
        "ASTRA_DATABASE_USERNAME",
        "ASTRA_DATABASE_NAME",
        "ASTRA_DATABASE_CHARSET",
    }
    for key in sorted(obsolete & values.keys()):
        if key == "NEXUS_REMOTE_DOWNLOAD_BASE_URL" and values[key]:
            raise DeploymentError(
                "旧 Worker 源 NEXUS_REMOTE_DOWNLOAD_BASE_URL 已停用，请先完成独立源配置迁移"
            )
        choice = dialog.resolve(
            key,
            values[key],
            "",
            "旧部署参数，当前使用明确 URL 和当前 Git 工作区；r 删除",
            regenerable=True,
        )
        if choice:
            raise DeploymentError(f"过期配置 {key} 尚未处理")
        values.pop(key)
    candidates = addresses if addresses is not None else ipv4_addresses()
    suggested = next((ip for ip in candidates if usable_address(ip)), "")
    current = values.get("NEXUS_DEPLOY_PUBLIC_IP", "")
    if not current:
        current = suggested or dialog.ask("服务器可跨机器访问的 IPv4")
    if not usable_address(current):
        current = dialog.resolve(
            "NEXUS_DEPLOY_PUBLIC_IP", current, suggested, "不是本机可绑定的非回环 IPv4"
        )
    if not usable_address(current):
        raise DeploymentError("服务器地址不可用")
    values["NEXUS_DEPLOY_PUBLIC_IP"] = current
    for key in ("ASTRA_DATABASE_URL", "AGENTSCOPE_SQL_URL"):
        if not values.get(key) or "REPLACE" in values[key]:
            values[key] = dialog.ask(key + "（独立 MySQL 异步连接 URL）", secret=True)
    if values["ASTRA_DATABASE_URL"] == values["AGENTSCOPE_SQL_URL"]:
        raise DeploymentError("Nexus 与 AgentScope 必须使用独立数据库")
    if not values.get("ASTRA_REDIS_URL"):
        with socket.socket() as probe:
            probe.settimeout(1)
            local_redis = probe.connect_ex(("127.0.0.1", 6379)) == 0
        values["ASTRA_REDIS_URL"] = (
            "redis://127.0.0.1:6379/1"
            if local_redis
            else dialog.ask(
                "Redis URL（未发现本机服务，请提供已有服务连接）", secret=True
            )
        )

    # URL 能表达身份、DB，AgentScope 的分散字段全部由同一 URL 生成。
    redis = urlsplit(values["ASTRA_REDIS_URL"])
    if (
        redis.scheme != "redis"
        or not redis.hostname
        or redis.username not in (None, "default")
    ):
        raise DeploymentError(
            "AgentScope 当前仅支持 redis:// 和 default 用户，请配置可用 Redis URL"
        )
    from urllib.parse import unquote

    values.update(
        AGENTSCOPE_REDIS_HOST=redis.hostname,
        AGENTSCOPE_REDIS_PORT=str(redis.port or 6379),
        AGENTSCOPE_REDIS_DB=redis.path.lstrip("/") or "0",
        AGENTSCOPE_REDIS_PASSWORD=unquote(redis.password or ""),
    )
    defaults = {
        "NEXUS_API_PORT": "18120",
        "AGENTSCOPE_PORT": "18000",
        "ASTRA_SERVICE_GATEWAY_PORT": "18110",
        "NEXUS_MCP_PORT": "18201",
        "NEXUS_TASK_MCP_PORT": "18202",
        "NEXUS_SCHEDULE_MCP_PORT": "18203",
        "NEXUS_DEPLOY_HTTPS_PORT": "443",
        "NEXUS_DEPLOY_WORKER_PORT": "18130",
        "NEXUS_DEFAULT_MODEL": "default",
        "NEXUS_DEPLOY_PUBLIC_HOST": current,
        "NEXUS_DEPLOY_TLS_CERT": str(state / "tls/server.crt"),
        "NEXUS_DEPLOY_NGINX_CACHE": "/var/cache/nexus-nginx/"
        + hashlib.sha256(str(state).encode()).hexdigest()[:12],
        "NEXUS_DEPLOY_TLS_KEY": str(state / "tls/server.key"),
        "NEXUS_DEPLOY_WORKER_ALLOW": "",
        "ASTRA_DATA_DIR": str(state / "data"),
        "NEXUS_DATA_DIR": str(state / "data/nexus"),
        "ASTRA_CONFIG_DIR": str(state / "configs"),
        "ASTRA_WORKSPACE_ROOT": str(state / "workspaces"),
        "ASTRA_LOG_DIR": str(state / "logs"),
        "ASTRA_RUNTIME_DIR": str(state / "runtime"),
        "AGENTSCOPE_WORKSPACE_DIR": str(state / "agentscope-workspaces"),
    }
    for key, value in defaults.items():
        values.setdefault(key, value)
    for key in (
        "ASTRA_DATA_DIR",
        "NEXUS_DATA_DIR",
        "ASTRA_CONFIG_DIR",
        "ASTRA_WORKSPACE_ROOT",
        "ASTRA_LOG_DIR",
        "ASTRA_RUNTIME_DIR",
        "AGENTSCOPE_WORKSPACE_DIR",
    ):
        if not Path(values[key]).is_absolute():
            proposed = str((root / values[key]).resolve())
            selected = dialog.resolve(
                key,
                values[key],
                proposed,
                "相对路径会随发布目录改变，必须固定已有数据原位置",
            )
            if selected != proposed:
                raise DeploymentError(key + " 必须明确保留原数据绝对路径")
            values[key] = proposed
    ports = []
    for key, default_value in defaults.items():
        if key.endswith("PORT"):
            try:
                port = int(values[key])
                assert 0 < port < 65536 and port not in ports
            except (ValueError, AssertionError):
                replacement = dialog.resolve(
                    key, values[key], default_value, "端口无效或与其他组件重复"
                )
                values[key] = replacement
                try:
                    port = int(replacement)
                    assert 0 < port < 65536 and port not in ports
                except (ValueError, AssertionError):
                    raise DeploymentError(f"{key} 仍不合法") from None
            ports.append(port)
    # 受管拓扑只有一套，已有错误地址需要明确确认，不能悄悄覆盖。
    expected = {
        "ASTRA_EXECUTOR_TYPE": "worker",
        "ASTRA_ENVIRONMENT": "production",
        "ASTRA_SEED_DEMO": "false",
        "ASTRA_GATEWAY_ENABLED": "true",
        "ASTRA_SERVICE_GATEWAY_HOST": "127.0.0.1",
        "AGENTSCOPE_HOST": "127.0.0.1",
        "NEXUS_MCP_HOST": "127.0.0.1",
        "NEXUS_TASK_MCP_HOST": "127.0.0.1",
        "NEXUS_SCHEDULE_MCP_HOST": "127.0.0.1",
        "NEXUS_MCP_TRANSPORT": "streamable-http",
        "NEXUS_AGENTSCOPE_USER_ID": "nexus",
        "ASTRA_AGENTSCOPE_USER_ID": "nexus",
        "NEXUS_API_BASE": "http://127.0.0.1:" + values["NEXUS_API_PORT"],
        "NEXUS_AGENTSCOPE_BASE_URL": "http://127.0.0.1:" + values["AGENTSCOPE_PORT"],
        "NEXUS_GATEWAY_OPENAI_BASE": "http://127.0.0.1:"
        + values["ASTRA_SERVICE_GATEWAY_PORT"]
        + "/v1",
        "NEXUS_TASK_MCP_URL": "http://127.0.0.1:"
        + values["NEXUS_TASK_MCP_PORT"]
        + "/mcp",
        "NEXUS_SCHEDULE_MCP_URL": "http://127.0.0.1:"
        + values["NEXUS_SCHEDULE_MCP_PORT"]
        + "/mcp",
        "NEXUS_RUNTIME_ENV_FILE": str(state / "nexus.env"),
    }
    expected["ASTRA_NEXUS_API_BASE"] = expected["NEXUS_API_BASE"]
    expected["ASTRA_AGENTSCOPE_BASE_URL"] = expected["NEXUS_AGENTSCOPE_BASE_URL"]
    worker_base = "http://" + current + ":" + values["NEXUS_DEPLOY_WORKER_PORT"]
    expected.update(
        NEXUS_REMOTE_GATEWAY_LOCAL_URL=worker_base + "/v1",
        NEXUS_MCP_RESOURCE_SERVER_URL=worker_base + "/mcp",
        NEXUS_WORKER_AGENT_GATEWAY_URL=worker_base.replace("http:", "ws:")
        + "/api/worker-gateway/connect",
    )
    for key, value in expected.items():
        if key in values and values[key] != value:
            chosen = dialog.resolve(key, values[key], value, "与单机受管服务拓扑不一致")
            if chosen != value:
                raise DeploymentError(f"{key} 尚未修正，部署停止")
        values[key] = value
    for key in (
        "NEXUS_MCP_API_KEY",
        "NEXUS_TASK_MCP_KEY",
        "NEXUS_SCHEDULE_MCP_KEY",
        "NEXUS_INTERNAL_KEY",
        "NEXUS_WORKER_GATEWAY_ADMIN_KEY",
        "NEXUS_GATEWAY_API_KEY",
    ):
        if not values.get(key):
            values[key] = (
                "agw_" if key == "NEXUS_GATEWAY_API_KEY" else ""
            ) + secrets.token_urlsafe(32)
    if not values.get("ASTRA_GATEWAY_MASTER_KEY"):
        values["ASTRA_GATEWAY_MASTER_KEY"] = base64.urlsafe_b64encode(
            secrets.token_bytes(32)
        ).decode()
    else:
        try:
            assert (
                len(base64.urlsafe_b64decode(values["ASTRA_GATEWAY_MASTER_KEY"])) == 32
            )
        except (ValueError, AssertionError):
            # 不提供自动再生，避免数据库中已有密文无法解密。
            raise DeploymentError(
                "ASTRA_GATEWAY_MASTER_KEY 无效；须恢复原有效密钥，不能自动再生"
            ) from None
    if not re.fullmatch(r"agw_[A-Za-z0-9_-]+", values["NEXUS_GATEWAY_API_KEY"]):
        raise DeploymentError(
            "NEXUS_GATEWAY_API_KEY 格式无效；请明确提供有效平台网关密钥"
        )
    if not values["NEXUS_DEPLOY_WORKER_ALLOW"]:
        values["NEXUS_DEPLOY_WORKER_ALLOW"] = dialog.ask(
            "允许 Worker HTTP 入口访问的网段（CIDR，逗号分隔；不能自动猜测公司边界）"
        )
    for cidr in values["NEXUS_DEPLOY_WORKER_ALLOW"].split(","):
        try:
            network = ipaddress.ip_network(cidr.strip(), strict=False)
            assert network.version == 4 and network.prefixlen > 0
        except (ValueError, AssertionError):
            raise DeploymentError("Worker 入口允许网段无效，禁止 0.0.0.0/0") from None
    return values

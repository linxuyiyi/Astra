from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from pathlib import Path

from .config import DeployConfig, parse_env_file
from .errors import ConfigError
from .records import atomic_json, atomic_write

SECRET_KEYS = {
    "ASTRA_TASK_MCP_KEY",
    "NEXUS_TASK_MCP_KEY",
    "ASTRA_MCP_API_KEY",
    "ASTRA_GATEWAY_MASTER_KEY",
    "ASTRA_WORKER_GATEWAY_ADMIN_KEY",
    "ASTRA_DATABASE_PASSWORD",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ASTRA_LLM_WIKI_API_TOKEN",
    "ASTRA_LLM_WIKI_PROJECT_ID",
    "NEXUS_GATEWAY_PROBE_KEY",
    "ASTRA_OPENCODE_SERVER_PASSWORD",
    "ASTRA_ASSISTANT_OPENCODE_PASSWORD",
}
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_NAME = re.compile(r"(KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL)", re.IGNORECASE)
URL_NAME = re.compile(r"(URL|URI|DSN)", re.IGNORECASE)
URL_USERINFO = re.compile(r"(://)[^/@\s]+@")
URL_SECRET_QUERY = re.compile(r"(?i)([?&](?:token|key|password|secret|api_key)=)[^&\s]+")


SAFE_ENV_VALUE = re.compile(r"^[A-Za-z0-9_./:@%+,=-]*$")


def env_line(key: str, value: str) -> str:
    """Render a systemd EnvironmentFile line without changing the served value."""
    if "\n" in value or "\r" in value:
        raise ConfigError(f"{key} 不能包含换行")
    if value == "":
        return f"{key}=\n"
    if SAFE_ENV_VALUE.match(value):
        return f"{key}={value}\n"
    if "'" not in value:
        return f"{key}='{value}'\n"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"\n'


def own_generated_file(config: DeployConfig, path: Path, mode: int = 0o600) -> None:
    """Generated runtime files must be readable by the service account, not only root."""
    path.chmod(mode)
    if os.geteuid() != 0 or config.run_user == "root":
        return
    import grp
    import pwd

    try:
        uid = pwd.getpwnam(config.run_user).pw_uid
        gid = grp.getgrnam(config.run_group).gr_gid
    except KeyError:
        return
    os.chown(path, uid, gid)


def _token() -> str:
    return secrets.token_urlsafe(32)


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def ensure_user_config(path: Path) -> bool:
    if path.exists():
        return False
    template = (
        "# Nexus 生产配置。部署不会覆盖本文件中的已有值。\n"
        "# 完整模板见 ops/env/nexus-production.env.example。\n"
        "NEXUS_REPO_URL=https://github.com/linxuyiyi/Astra\n"
        "NEXUS_COMMIT=\n"
        "NEXUS_BIND_HOST=127.0.0.1\n"
        "ASTRA_DATABASE_BACKEND=sqlite\n"
        "ASTRA_GATEWAY_ENABLED=true\n"
        "ASTRA_MCP_TRANSPORT=streamable-http\n"
        "OPENAI_BASE_URL=https://api.deepseek.com\n"
        "OPENAI_MODEL=deepseek-flash\n"
    )
    atomic_write(path, template, mode=0o600)
    return True


def _shared_task_key(user: dict[str, str], existing: dict[str, str]) -> str:
    for source in (user, existing):
        value = (source.get("ASTRA_TASK_MCP_KEY") or source.get("NEXUS_TASK_MCP_KEY") or "").strip()
        if value:
            return value
    return _token()


def ensure_secrets(config: DeployConfig, user: dict[str, str]) -> tuple[dict[str, str], bool]:
    existing = parse_env_file(config.secrets_file)
    values = dict(existing)
    changed = False
    task_key = _shared_task_key(user, existing)
    for key in ("ASTRA_TASK_MCP_KEY", "NEXUS_TASK_MCP_KEY"):
        if values.get(key) != task_key:
            values[key] = task_key
            changed = True
    if config.library_mcp_enabled and not values.get("ASTRA_MCP_API_KEY"):
        values["ASTRA_MCP_API_KEY"] = _token()
        changed = True
    if config.gateway_enabled and not values.get("ASTRA_GATEWAY_MASTER_KEY"):
        values["ASTRA_GATEWAY_MASTER_KEY"] = _fernet_key()
        changed = True
    if not values.get("ASTRA_WORKER_GATEWAY_ADMIN_KEY"):
        values["ASTRA_WORKER_GATEWAY_ADMIN_KEY"] = _token()
        changed = True
    for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ASTRA_DATABASE_PASSWORD"):
        supplied = user.get(key, "").strip()
        if supplied and values.get(key) != supplied:
            values[key] = supplied
            changed = True
    if not values.get("OPENAI_API_KEY") and values.get("DEEPSEEK_API_KEY"):
        values["OPENAI_API_KEY"] = values["DEEPSEEK_API_KEY"]
        changed = True
    if changed or not config.secrets_file.exists():
        payload = "".join(env_line(key, value) for key, value in sorted(values.items()))
        atomic_write(config.secrets_file, payload, mode=0o600)
    own_generated_file(config, config.secrets_file, 0o600)
    return values, changed


def _pick(user: dict[str, str], key: str, default: str) -> str:
    value = user.get(key, "")
    return value if value != "" else default


def _url_with_v1(base: str) -> str:
    cleaned = base.rstrip("/")
    return cleaned if cleaned.endswith("/v1") else f"{cleaned}/v1"


def generate_runtime_env(config: DeployConfig) -> dict[str, str]:
    user = config.user_values
    current = str(config.current_link)
    task_api = str(config.ports["task_api"])
    agent = str(config.ports["agent_service"])
    nexus_api = str(config.ports["nexus_api"])
    task_mcp = str(config.ports["task_mcp"])
    library_mcp = str(config.ports["library_mcp"])
    gateway = str(config.ports["gateway"])
    provider = _pick(user, "NEXUS_OPENCODE_PROVIDER", "nexus")
    model = _pick(user, "OPENAI_MODEL", config.openai_model)
    database = {
        "ASTRA_ENVIRONMENT": "production",
        "ASTRA_SEED_DEMO": "false",
        "ASTRA_DATABASE_BACKEND": config.database_backend,
        "ASTRA_DATABASE_HOST": config.database_host,
        "ASTRA_DATABASE_PORT": str(config.database_port),
        "ASTRA_DATABASE_NAME": config.database_name,
        "ASTRA_DATABASE_USERNAME": config.database_username,
        "ASTRA_DATABASE_CHARSET": _pick(user, "ASTRA_DATABASE_CHARSET", "utf8mb4"),
        "ASTRA_DATA_DIR": str(config.task_data_dir),
        "ASTRA_WORKSPACE_ROOT": str(config.task_workspace_root),
        "ASTRA_RUNTIME_DIR": str(config.task_runtime_dir),
        "ASTRA_CONFIG_DIR": str(config.task_config_dir),
        "ASTRA_LOG_DIR": str(config.task_log_dir),
        "ASTRA_REDIS_URL": config.redis_url,
        "ASTRA_SERVICE_BACKEND_HOST": config.bind_host,
        "ASTRA_SERVICE_BACKEND_PORT": task_api,
        "ASTRA_SERVICE_GATEWAY_HOST": config.bind_host,
        "ASTRA_SERVICE_GATEWAY_PORT": gateway,
        "ASTRA_GATEWAY_ENABLED": "true" if config.gateway_enabled else "false",
        "ASTRA_MCP_TRANSPORT": "streamable-http" if config.library_mcp_enabled else "stdio",
        "ASTRA_MCP_HOST": config.bind_host,
        "ASTRA_MCP_PORT": library_mcp,
        "ASTRA_TASK_MCP_HOST": config.bind_host,
        "ASTRA_TASK_MCP_PORT": task_mcp,
        "ASTRA_NEXUS_API_BASE": f"http://127.0.0.1:{nexus_api}",
        "ASTRA_AGENTSCOPE_BASE_URL": f"http://127.0.0.1:{agent}",
        "ASTRA_AGENTSCOPE_USER_ID": "nexus",
        "ASTRA_OPENCODE_BINARY": _pick(
            user, "ASTRA_OPENCODE_BINARY", f"{current}/tools/node_modules/.bin/opencode"
        ),
        "ASTRA_OPENCODE_MODEL": _pick(user, "ASTRA_OPENCODE_MODEL", f"{provider}/{model}"),
        "ASTRA_OPENCODE_SERVER_HOSTNAME": _pick(user, "ASTRA_OPENCODE_SERVER_HOSTNAME", "127.0.0.1"),
        "ASTRA_OPENCODE_SERVER_PORT": str(config.ports["opencode"]),
        "ASTRA_ASSISTANT_OPENCODE_PORT": str(config.ports["assistant_opencode"]),
        "ASTRA_OUTBOX_INTERVAL": _pick(user, "ASTRA_OUTBOX_INTERVAL", "2"),
        "ASTRA_OUTBOX_RETRY_SECONDS": _pick(user, "ASTRA_OUTBOX_RETRY_SECONDS", "5"),
        "NEXUS_DATA_DIR": str(config.nexus_api_data_dir),
        "NEXUS_API_BASE": f"http://127.0.0.1:{nexus_api}",
        "NEXUS_AGENTSCOPE_BASE_URL": f"http://127.0.0.1:{agent}",
        "NEXUS_AGENTSCOPE_USER_ID": "nexus",
        "NEXUS_TASK_MCP_URL": f"http://127.0.0.1:{task_mcp}/mcp",
        "NEXUS_CHAT_PROVIDER": _pick(user, "NEXUS_CHAT_PROVIDER", "openai"),
        "NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG": _pick(
            user, "NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG", ""
        ),
        "AGENTSCOPE_HOST": config.bind_host,
        "AGENTSCOPE_PORT": agent,
        "AGENTSCOPE_REDIS_HOST": _pick(user, "AGENTSCOPE_REDIS_HOST", "127.0.0.1"),
        "AGENTSCOPE_REDIS_PORT": _pick(user, "AGENTSCOPE_REDIS_PORT", "6379"),
        "AGENTSCOPE_REDIS_DB": _pick(user, "AGENTSCOPE_REDIS_DB", "1"),
        "AGENTSCOPE_WORKSPACE_DIR": str(config.agent_workspace_dir),
        "AGENTSCOPE_QDRANT_PATH": str(config.agent_qdrant_dir),
        "AGENTSCOPE_BLOB_DIR": str(config.agent_blob_dir),
        "OPENAI_BASE_URL": config.openai_base_url,
        "OPENAI_MODEL": model,
        "OPENCODE_CONFIG": str(config.opencode_config_file),
        "OPENCODE_CONFIG_DIR": str(config.opencode_config_file.parent),
        "NEXUS_OPENCODE_CONFIG": str(config.opencode_config_file),
    }
    generated = {
        key: value
        for key, value in database.items()
        if key not in user or key in {"ASTRA_ENVIRONMENT", "ASTRA_SEED_DEMO"}
    }
    if not generated.get("NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG"):
        generated.pop("NEXUS_AGENTSCOPE_CHAT_MODEL_CONFIG", None)
    if not config.library_mcp_enabled:
        generated["ASTRA_MCP_TRANSPORT"] = "stdio"
    if _pick(user, "ASTRA_LLM_WIKI_ENABLED", "false").lower() in {"1", "true", "yes", "on"}:
        generated["ASTRA_LLM_WIKI_ENABLED"] = "true"
        generated["ASTRA_LLM_WIKI_API_BASE_URL"] = _pick(
            user,
            "ASTRA_LLM_WIKI_API_BASE_URL",
            f"http://127.0.0.1:{config.effective('LLM_WIKI_LOCAL_PORT', '19828')}/api/v1",
        )
        generated["ASTRA_LLM_WIKI_TIMEOUT"] = _pick(user, "ASTRA_LLM_WIKI_TIMEOUT", "30")
    return generated


def write_runtime_env(config: DeployConfig) -> tuple[dict[str, str], dict[str, str]]:
    generated = generate_runtime_env(config)
    previous = parse_env_file(config.runtime_env_file)
    changed = {
        key: {"before": previous.get(key, ""), "after": value}
        for key, value in generated.items()
        if previous.get(key) != value or (key in previous) != (key in generated)
    }
    payload = "".join(env_line(key, value) for key, value in sorted(generated.items()))
    atomic_write(config.runtime_env_file, payload, mode=0o600)
    own_generated_file(config, config.runtime_env_file, 0o600)
    return generated, changed


def write_opencode_config(config: DeployConfig) -> dict:
    model = _pick(config.user_values, "OPENAI_MODEL", config.openai_model)
    provider = _pick(config.user_values, "NEXUS_OPENCODE_PROVIDER", "nexus")
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{provider}/{model}",
        "provider": {
            provider: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Nexus",
                "options": {
                    "baseURL": _url_with_v1(config.openai_base_url),
                    "apiKey": "{env:OPENAI_API_KEY}",
                },
                "models": {model: {"name": model}},
            }
        },
    }
    atomic_json(config.opencode_config_file, payload, mode=0o600)
    own_generated_file(config, config.opencode_config_file, 0o600)
    return payload


def persist_secret(config: DeployConfig, key: str, value: str) -> None:
    values = parse_env_file(config.secrets_file)
    values[key] = value
    payload = "".join(env_line(name, item) for name, item in sorted(values.items()))
    atomic_write(config.secrets_file, payload, mode=0o600)
    own_generated_file(config, config.secrets_file, 0o600)


def config_digest(config: DeployConfig) -> str:
    digest = hashlib.sha256()
    try:
        digest.update(config.user_env_file.read_bytes())
    except OSError:
        digest.update(b"<missing-user-env>")
    try:
        secret_names = sorted(parse_env_file(config.secrets_file))
    except OSError:
        secret_names = []
    digest.update(("\n".join(secret_names)).encode("utf-8"))
    try:
        digest.update(config.runtime_env_file.read_bytes())
    except OSError:
        digest.update(b"<missing-runtime-env>")
    return digest.hexdigest()


def ensure_directories(config: DeployConfig) -> None:
    for path in (
        config.config_dir,
        config.secrets_file.parent,
        config.runtime_env_file.parent,
        config.opencode_config_file.parent,
        config.chat_model_file.parent,
        config.state_dir,
        config.releases_dir,
        config.data_dir,
        config.log_dir,
        config.backup_dir,
        *config.data_paths().values(),
        config.task_log_dir,
        config.task_runtime_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    for path in config.data_paths().values():
        resolved = path.resolve()
        if config.releases_dir.resolve() in resolved.parents:
            raise ConfigError(f"数据目录不能位于发布目录内：{resolved}")
    _chown_runtime_dirs(config)


def _chown_runtime_dirs(config: DeployConfig) -> None:
    if os.geteuid() != 0 or config.run_user == "root":
        return
    import grp
    import pwd

    try:
        uid = pwd.getpwnam(config.run_user).pw_uid
        gid = grp.getgrnam(config.run_group).gr_gid
    except KeyError:
        return
    targets = [
        config.config_dir,
        config.state_dir,
        config.data_dir,
        config.log_dir,
        config.backup_dir,
        config.task_log_dir,
        *config.data_paths().values(),
    ]
    for base in targets:
        try:
            os.chown(base, uid, gid)
        except OSError:
            continue
        for root, dirs, files in os.walk(base):
            for name in dirs:
                try:
                    os.chown(Path(root) / name, uid, gid)
                except OSError:
                    continue
            for name in files:
                path = Path(root) / name
                if path in {config.user_env_file, config.secrets_file}:
                    continue
                try:
                    os.chown(path, uid, gid)
                except OSError:
                    continue


def bootstrap_task_config(config: DeployConfig, release_path: Path) -> int:
    """Copy repository default Task configs only when the external file is absent."""
    source = release_path / "services" / "task-platform" / "configs"
    target = config.task_config_dir
    if not source.is_dir():
        return 0
    copied = 0
    for path in source.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(source)
        destination = target / relative
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
        copied += 1
        own_generated_file(config, destination, 0o644)
    return copied


def safe_value(key: str, value: str) -> str:
    if not value:
        return "<unset>"
    if key in SECRET_KEYS or SECRET_NAME.search(key):
        return "<hidden>"
    if URL_NAME.search(key) or "://" in value:
        redacted = URL_USERINFO.sub(r"\1***@", value)
        return URL_SECRET_QUERY.sub(r"\1***", redacted)
    return value


def secret_values(config: DeployConfig) -> list[str]:
    values: list[str] = []
    for key, value in config.effective_values.items():
        if value and (key in SECRET_KEYS or SECRET_NAME.search(key)):
            values.append(value)
        elif value:
            match = re.search(r"://[^/@\s:]+:([^/@\s]+)@", value)
            if match:
                values.append(match.group(1))
    values.extend(value for value in (config.database_password, config.gateway_probe_key) if value)
    for key, value in parse_env_file(config.secrets_file).items():
        if value and (key in SECRET_KEYS or SECRET_NAME.search(key)):
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact_text(text: str, secrets: list[str]) -> str:
    result = text
    for value in secrets:
        if value and len(value) >= 4:
            result = result.replace(value, "***")
    result = URL_USERINFO.sub(r"\1***@", result)
    return URL_SECRET_QUERY.sub(r"\1***", result)


def sanitize_payload(value: object, secrets: list[str]) -> object:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [sanitize_payload(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_payload(item, secrets) for key, item in value.items()}
    return value


def non_secret_summary(changed: dict[str, dict[str, str]]) -> list[str]:
    lines = []
    for key in sorted(changed):
        before = safe_value(key, changed[key].get("before", ""))
        after = safe_value(key, changed[key].get("after", ""))
        lines.append(f"{key}: {before} -> {after}")
    return lines


def validate_env_keys(values: dict[str, str], source: Path) -> None:
    for key in values:
        if not ENV_KEY.match(key):
            raise ConfigError(f"{source} 包含非法变量名：{key!r}")


def merged_environment(config: DeployConfig) -> dict[str, str]:
    merged = dict(os.environ)
    merged.update(parse_env_file(config.user_env_file))
    merged.update(parse_env_file(config.secrets_file))
    merged.update(parse_env_file(config.runtime_env_file))
    return merged


def redact(value: str) -> str:
    if not value:
        return ""
    return "<set>" if len(value) <= 8 else f"{value[:2]}***{value[-2:]}"


def json_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)

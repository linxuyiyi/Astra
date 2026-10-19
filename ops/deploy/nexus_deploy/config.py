from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from .errors import ConfigError

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
ENV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
TRUE = {"1", "true", "yes", "on"}

DEFAULT_PORTS = {
    "redis": 6379,
    "task_api": 18100,
    "gateway": 18110,
    "nexus_api": 18120,
    "library_mcp": 18201,
    "task_mcp": 18202,
    "agent_service": 18000,
    "web": 15174,
    "opencode": 14096,
    "assistant_opencode": 14097,
}


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines without executing shell syntax or interpolating."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_RE.match(line)
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = value.replace('\\"', '"').replace("\\\\", "\\")
        values[key] = value
    return values


def env_bool(values: dict[str, str], key: str, default: bool = False) -> bool:
    raw = values.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in TRUE


def env_int(values: dict[str, str], key: str, default: int) -> int:
    raw = values.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是整数，当前为 {raw!r}") from exc


def _path(values: dict[str, str], key: str, default: Path) -> Path:
    raw = values.get(key, "").strip()
    path = Path(raw).expanduser() if raw else default
    return path


@dataclass(frozen=True)
class DeployConfig:
    repo_url: str
    commit: str
    user_env_file: Path
    config_dir: Path
    secrets_file: Path
    runtime_env_file: Path
    opencode_config_file: Path
    releases_dir: Path
    current_link: Path
    state_dir: Path
    data_dir: Path
    log_dir: Path
    backup_dir: Path
    unit_dir: Path
    nginx_conf: Path
    nginx_port: int
    bind_host: str
    entry_url: str
    run_user: str
    run_group: str
    ports: dict[str, int]
    user_values: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, str] = field(default_factory=dict)
    effective_values: dict[str, str] = field(default_factory=dict)
    database_url: str = ""
    gateway_probe_key: str = ""
    gateway_enabled: bool = True
    library_mcp_enabled: bool = True
    redis_enabled: bool = True
    nginx_enabled: bool = True
    database_backend: str = "sqlite"
    database_host: str = "127.0.0.1"
    database_port: int = 3306
    database_name: str = "nexus"
    database_username: str = "nexus"
    database_password: str = ""
    openai_base_url: str = "https://api.deepseek.com"
    openai_model: str = "deepseek-flash"
    deepseek_api_key: str = ""
    redis_url: str = "redis://127.0.0.1:6379/1"
    opencode_version: str = "1.18.35"
    keep_releases: int = 5
    database_compatible_with_previous: bool = False
    mysql_backup_acknowledged: bool = False

    @property
    def chat_model_file(self) -> Path:
        return self.config_dir / "chat-model.env"

    @property
    def optional_env_file(self) -> Path:
        return self.config_dir / "optional.env"

    @property
    def task_data_dir(self) -> Path:
        return self._user_path("ASTRA_DATA_DIR", self.data_dir / "task-platform")

    @property
    def task_workspace_root(self) -> Path:
        return self._user_path("ASTRA_WORKSPACE_ROOT", self.data_dir / "workspaces")

    @property
    def task_runtime_dir(self) -> Path:
        return self._user_path("ASTRA_RUNTIME_DIR", self.data_dir / "runtime")

    @property
    def task_config_dir(self) -> Path:
        return self._user_path("ASTRA_CONFIG_DIR", self.data_dir / "config")

    @property
    def task_log_dir(self) -> Path:
        return self._user_path("ASTRA_LOG_DIR", self.log_dir / "task-platform")

    @property
    def nexus_api_data_dir(self) -> Path:
        return self._user_path("NEXUS_DATA_DIR", self.data_dir / "nexus-api")

    @property
    def nexus_api_sqlite_path(self) -> Path:
        return self.nexus_api_data_dir / "nexus.sqlite3"

    @property
    def agent_workspace_dir(self) -> Path:
        return self._user_path(
            "AGENTSCOPE_WORKSPACE_DIR", self.data_dir / "agent-service" / "workspaces"
        )

    @property
    def agent_qdrant_dir(self) -> Path:
        return self._user_path("AGENTSCOPE_QDRANT_PATH", self.data_dir / "agent-service" / "qdrant")

    @property
    def agent_blob_dir(self) -> Path:
        return self._user_path("AGENTSCOPE_BLOB_DIR", self.data_dir / "agent-service" / "blobs")

    @property
    def celery_schedule(self) -> Path:
        return self.task_runtime_dir / "celerybeat-schedule"

    @property
    def sqlite_path(self) -> Path:
        if self.database_url.startswith("sqlite") and self.database_url:
            return _sqlite_path_from_url(self.database_url)
        return self.task_data_dir / "nexus.db"

    @property
    def database_description(self) -> str:
        if self.database_backend == "sqlite":
            return f"sqlite:{self.sqlite_path}"
        return f"mysql:{self.database_host}:{self.database_port}/{self.database_name}"

    @property
    def product_version(self) -> str:
        return "1.4.4"

    def effective(self, key: str, default: str = "") -> str:
        return self.effective_values.get(key, default)

    def _user_path(self, key: str, default: Path) -> Path:
        raw = self.effective(key, "").strip()
        return Path(raw).expanduser() if raw else default

    def all_service_ports(self) -> dict[str, int]:
        ports = {
            "task_api": self.ports["task_api"],
            "agent_service": self.ports["agent_service"],
            "nexus_api": self.ports["nexus_api"],
            "task_mcp": self.ports["task_mcp"],
        }
        if self.library_mcp_enabled:
            ports["library_mcp"] = self.ports["library_mcp"]
        if self.gateway_enabled:
            ports["gateway"] = self.ports["gateway"]
        return ports

    def validate(self) -> None:
        if not COMMIT_RE.match(self.commit):
            raise ConfigError("NEXUS_COMMIT 必须是完整的 40 位小写 Git commit")
        if not self.repo_url.strip():
            raise ConfigError("NEXUS_REPO_URL 不能为空")
        if self.database_backend not in {"sqlite", "mysql"}:
            raise ConfigError("ASTRA_DATABASE_BACKEND 只支持 sqlite 或 mysql")
        if self.database_backend == "mysql" and not self.database_name.strip():
            raise ConfigError("MySQL 部署需要 ASTRA_DATABASE_NAME")
        if self.database_backend == "sqlite":
            sqlite = self.sqlite_path
            if not sqlite.is_absolute():
                raise ConfigError(f"SQLite 数据库必须是绝对路径：{sqlite}")
            if self.releases_dir.resolve() in sqlite.resolve().parents:
                raise ConfigError(f"SQLite 数据库不能位于发布目录内：{sqlite}")
        if not self.run_user.strip():
            raise ConfigError("NEXUS_RUN_USER 不能为空")
        for label, path in {
            "发布目录": self.releases_dir,
            "current 链接": self.current_link,
            "配置目录": self.config_dir,
            "状态目录": self.state_dir,
            "数据根目录": self.data_dir,
            "日志目录": self.log_dir,
            "备份目录": self.backup_dir,
            "密钥文件": self.secrets_file,
            "运行配置": self.runtime_env_file,
            "OpenCode 配置": self.opencode_config_file,
            "systemd unit 目录": self.unit_dir,
            "Nginx 配置": self.nginx_conf,
            **self.data_paths(),
        }.items():
            if not path.is_absolute():
                raise ConfigError(f"{label} 必须是绝对路径：{path}")
        releases = self.releases_dir.resolve()
        for label, path in self.data_paths().items():
            resolved = path.resolve()
            if resolved == releases or releases in resolved.parents:
                raise ConfigError(f"{label} 不能位于发布目录内：{resolved}")
            if resolved == self.current_link.resolve():
                raise ConfigError(f"{label} 不能指向 current 发布目录")

    def data_paths(self) -> dict[str, Path]:
        return {
            "Task 数据目录": self.task_data_dir,
            "Task workspaces": self.task_workspace_root,
            "Task runtime": self.task_runtime_dir,
            "Task config": self.task_config_dir,
            "Nexus API 数据目录": self.nexus_api_data_dir,
            "Agent Service workspace": self.agent_workspace_dir,
            "Agent Service qdrant": self.agent_qdrant_dir,
            "Agent Service blob": self.agent_blob_dir,
        }

    def release_dir(self, commit: str | None = None) -> Path:
        return self.releases_dir / (commit or self.commit)


def _sqlite_path_from_url(url: str) -> Path:
    parsed = urlparse(url)
    path = unquote(parsed.path)
    if url.startswith("sqlite+aiosqlite:///"):
        path = url.split("sqlite+aiosqlite:///", 1)[1]
    elif url.startswith("sqlite:///"):
        path = url.split("sqlite:///", 1)[1]
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ConfigError(f"SQLite 路径必须是绝对路径：{candidate}")
    return candidate


def _parse_database_url(url: str) -> tuple[str, str, int, str, str, str]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme.startswith("sqlite"):
        path = _sqlite_path_from_url(url)
        return "sqlite", "127.0.0.1", 3306, path.stem, "", ""
    if not scheme.startswith("mysql"):
        raise ConfigError(f"不支持的 ASTRA_DATABASE_URL 协议：{scheme}")
    if not parsed.hostname or not parsed.path.lstrip("/"):
        raise ConfigError("ASTRA_DATABASE_URL 缺少主机或数据库名")
    return (
        "mysql",
        parsed.hostname,
        parsed.port or 3306,
        parsed.path.lstrip("/"),
        unquote(parsed.username or "nexus"),
        unquote(parsed.password or ""),
    )


def load_config(
    *,
    env_file: Path | None = None,
    overrides: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
    require_commit: bool = True,
    include_runtime: bool = False,
) -> DeployConfig:
    environ = dict(os.environ if environ is None else environ)
    base_dir = Path(environ.get("NEXUS_CONFIG_DIR", "/etc/nexus")).expanduser()
    user_file = (env_file or base_dir / "nexus.env").expanduser()
    user_values = parse_env_file(user_file)
    process_overrides = {
        k: v for k, v in environ.items() if k.startswith("NEXUS_") or k.startswith("ASTRA_")
    }
    merged_overrides = {**process_overrides, **(overrides or {})}
    initial = {**user_values, **merged_overrides}
    initial_config_dir = _path(initial, "NEXUS_CONFIG_DIR", base_dir)
    secret_file = _path(initial, "NEXUS_SECRETS_FILE", initial_config_dir / "secrets.env")
    secret_values = parse_env_file(secret_file)
    runtime_file = _path(initial, "NEXUS_RUNTIME_ENV_FILE", initial_config_dir / "runtime.env")
    runtime_values = parse_env_file(runtime_file) if include_runtime else {}
    values = {**user_values, **secret_values, **runtime_values, **merged_overrides}

    repo_url = values.get("NEXUS_REPO_URL", "").strip()
    commit = values.get("NEXUS_COMMIT", "").strip().lower()
    if not commit and require_commit:
        raise ConfigError("必须显式设置 NEXUS_COMMIT（完整 40 位 commit），不能部署浮动分支")
    if not commit:
        commit = "0" * 40

    releases_dir = _path(values, "NEXUS_RELEASES_DIR", Path("/opt/nexus/releases"))
    current_link = _path(values, "NEXUS_CURRENT_LINK", Path("/opt/nexus/current"))
    state_dir = _path(values, "NEXUS_STATE_DIR", Path("/var/lib/nexus"))
    data_dir = _path(values, "NEXUS_DATA_ROOT", state_dir / "data")
    log_dir = _path(values, "NEXUS_LOG_DIR", Path("/var/log/nexus"))
    config_dir = _path(values, "NEXUS_CONFIG_DIR", base_dir)
    secrets_file = _path(values, "NEXUS_SECRETS_FILE", config_dir / "secrets.env")
    runtime_env_file = _path(values, "NEXUS_RUNTIME_ENV_FILE", config_dir / "runtime.env")
    opencode_config = _path(
        values, "NEXUS_OPENCODE_CONFIG", config_dir / "opencode" / "opencode.json"
    )
    backup_dir = _path(values, "NEXUS_BACKUP_DIR", state_dir / "backups")
    unit_dir = _path(values, "NEXUS_SYSTEMD_UNIT_DIR", Path("/etc/systemd/system"))
    nginx_conf = _path(values, "NEXUS_NGINX_CONF", Path("/etc/nginx/conf.d/nexus.conf"))
    bind_host = values.get("NEXUS_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    nginx_port = env_int(values, "NEXUS_NGINX_PORT", 80)
    entry_default = f"http://127.0.0.1:{nginx_port}/" if nginx_port != 80 else "http://127.0.0.1/"
    entry_url = values.get("NEXUS_ENTRY_URL", entry_default).rstrip("/") + "/"

    run_user = values.get("NEXUS_RUN_USER", environ.get("SUDO_USER") or os.environ.get("USER", "root"))
    run_group = values.get("NEXUS_RUN_GROUP", run_user)
    ports = {
        name: env_int(values, f"NEXUS_{name.upper()}_PORT", default)
        for name, default in DEFAULT_PORTS.items()
    }
    database_backend = values.get("ASTRA_DATABASE_BACKEND", "sqlite").strip().lower() or "sqlite"
    database_host = values.get("ASTRA_DATABASE_HOST", "127.0.0.1")
    database_port = env_int(values, "ASTRA_DATABASE_PORT", 3306)
    database_name = values.get("ASTRA_DATABASE_NAME", "nexus")
    database_username = values.get("ASTRA_DATABASE_USERNAME", "nexus")
    database_password = values.get("ASTRA_DATABASE_PASSWORD", "")
    database_url = values.get("ASTRA_DATABASE_URL", "").strip()
    if database_url:
        (
            database_backend,
            database_host,
            database_port,
            database_name,
            database_username,
            database_password,
        ) = _parse_database_url(database_url)
    config = DeployConfig(
        repo_url=repo_url,
        commit=commit,
        user_env_file=user_file,
        config_dir=config_dir,
        secrets_file=secrets_file,
        runtime_env_file=runtime_env_file,
        opencode_config_file=opencode_config,
        releases_dir=releases_dir,
        current_link=current_link,
        state_dir=state_dir,
        data_dir=data_dir,
        log_dir=log_dir,
        backup_dir=backup_dir,
        unit_dir=unit_dir,
        nginx_conf=nginx_conf,
        nginx_port=nginx_port,
        bind_host=bind_host,
        entry_url=entry_url,
        run_user=run_user,
        run_group=run_group,
        ports=ports,
        user_values=user_values,
        overrides=merged_overrides,
        effective_values=values,
        database_url=database_url,
        gateway_probe_key=values.get("NEXUS_GATEWAY_PROBE_KEY", "").strip(),
        gateway_enabled=env_bool(values, "ASTRA_GATEWAY_ENABLED", True),
        library_mcp_enabled=values.get("ASTRA_MCP_TRANSPORT", "stdio").strip() == "streamable-http",
        nginx_enabled=env_bool(values, "NEXUS_NGINX_ENABLED", True),
        database_backend=database_backend,
        database_host=database_host,
        database_port=database_port,
        database_name=database_name,
        database_username=database_username,
        database_password=database_password,
        openai_base_url=values.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
        openai_model=values.get("OPENAI_MODEL", "deepseek-flash"),
        deepseek_api_key=values.get("DEEPSEEK_API_KEY", ""),
        redis_url=values.get("ASTRA_REDIS_URL", "redis://127.0.0.1:6379/1"),
        opencode_version=values.get("ASTRA_OPENCODE_SERVER_VERSION", "1.18.35"),
        keep_releases=env_int(values, "NEXUS_KEEP_RELEASES", 5),
        mysql_backup_acknowledged=env_bool(values, "NEXUS_MYSQL_BACKUP_ACKNOWLEDGED", False),
    )
    config.validate()
    return config

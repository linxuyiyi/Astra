import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.worker_deployment import (
    DEFAULT_NODE_BASE,
    DEFAULT_NPM_REGISTRY,
    DEFAULT_PIP_INDEX,
    DEFAULT_PYTHON_SOURCE,
)

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
PROJECT_ROOT = REPO_ROOT
BACKEND_ROOT = SERVICE_ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ASTRA_",
        env_file=os.environ.get("NEXUS_RUNTIME_ENV_FILE") or REPO_ROOT / "ops" / "env" / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "Nexus"
    environment: str = "development"
    timezone: str = "Asia/Shanghai"

    # Local development uses SQLite without an external service. Production examples
    # use the explicit MySQL fields below. DATABASE_URL remains as a compatibility
    # escape hatch and takes precedence when supplied.
    database_url: str | None = None
    database_backend: str = "sqlite"
    database_host: str = "127.0.0.1"
    database_port: int = 3306
    database_name: str = "nexus"
    database_username: str = "nexus"
    database_password: str = ""
    database_charset: str = "utf8mb4"
    database_pool_recycle: int = 1800
    database_lock_wait_timeout: int = Field(default=10, ge=1, le=120)

    data_dir: Path = BACKEND_ROOT / "data"
    workspace_root: Path = BACKEND_ROOT / "workspaces"
    log_dir: Path = PROJECT_ROOT / ".astra" / "logs"
    runtime_dir: Path = PROJECT_ROOT / ".astra" / "run"
    config_dir: Path = SERVICE_ROOT / "configs"
    seed_demo: bool = True
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:15174", "http://127.0.0.1:18120"]
    )

    redis_url: str = "redis://localhost:6379/1"
    # auto：redis_url 非空时用 Redis 共享连接所有者；URL 为空才退回 memory。
    # memory / redis 可显式覆盖。memory 会在健康接口提示仅单进程可用。
    worker_gateway_bus: Literal["auto", "memory", "redis"] = Field(
        default="auto", validation_alias="NEXUS_WORKER_GATEWAY_BUS"
    )
    worker_gateway_ping_seconds: int = Field(
        default=20, ge=1, le=300, validation_alias="NEXUS_WORKER_GATEWAY_PING_SECONDS"
    )
    worker_gateway_ping_timeout_seconds: int = Field(
        default=10, ge=1, le=300, validation_alias="NEXUS_WORKER_GATEWAY_PING_TIMEOUT_SECONDS"
    )
    worker_gateway_reconnect_min_seconds: float = Field(
        default=1, ge=0.1, le=60, validation_alias="NEXUS_WORKER_GATEWAY_RECONNECT_MIN_SECONDS"
    )
    worker_gateway_reconnect_max_seconds: float = Field(
        default=30, ge=1, le=300, validation_alias="NEXUS_WORKER_GATEWAY_RECONNECT_MAX_SECONDS"
    )
    worker_gateway_max_frame_bytes: int = Field(
        default=262144, ge=1024, le=1048576, validation_alias="NEXUS_WORKER_GATEWAY_MAX_FRAME_BYTES"
    )
    worker_gateway_control_queue_size: int = Field(
        default=64, ge=1, le=10000, validation_alias="NEXUS_WORKER_GATEWAY_CONTROL_QUEUE_SIZE"
    )
    worker_gateway_event_queue_size: int = Field(
        default=64, ge=1, le=10000, validation_alias="NEXUS_WORKER_GATEWAY_EVENT_QUEUE_SIZE"
    )
    worker_gateway_heartbeat_misses: int = Field(
        default=2, ge=1, le=10, validation_alias="NEXUS_WORKER_GATEWAY_HEARTBEAT_MISSES"
    )
    worker_gateway_command_timeout_seconds: int = Field(
        default=30, ge=1, le=600, validation_alias="NEXUS_WORKER_GATEWAY_COMMAND_TIMEOUT_SECONDS"
    )
    worker_gateway_auth_timeout_seconds: int = Field(
        default=10, ge=1, le=60, validation_alias="NEXUS_WORKER_GATEWAY_AUTH_TIMEOUT_SECONDS"
    )
    worker_gateway_dispatch_lease_seconds: int = Field(
        default=10, ge=1, le=600, validation_alias="NEXUS_WORKER_GATEWAY_DISPATCH_LEASE_SECONDS"
    )
    # 空值时由启动流程生成并写入 ops/env/.env。管理接口校验这把密钥，WebSocket 不使用它。
    worker_gateway_admin_key: str = Field(default="", validation_alias="NEXUS_WORKER_GATEWAY_ADMIN_KEY")
    # How long an open event stream waits for a disconnected Worker before it
    # emits a gap and ends. A new epoch inside this window resumes the stream.
    worker_event_offline_grace_seconds: float = Field(
        default=30, ge=1, le=600, validation_alias="NEXUS_WORKER_EVENT_OFFLINE_GRACE_SECONDS"
    )
    # Extra environment names copied into OpenCode, comma-separated. ASTRA_* and
    # token/key/secret names are never copied, even when listed here.
    worker_child_env_allow: str = Field(default="", validation_alias="NEXUS_WORKER_CHILD_ENV_ALLOW")
    scout_interval: int = 30
    dispatcher_interval: int = 5
    heartbeat_interval: int = 10
    heartbeat_timeout: int = 60
    inactivity_timeout: int = 900
    outbox_interval: int = Field(default=2, ge=1, le=3600)
    outbox_retry_seconds: int = Field(default=5, ge=1, le=3600)
    outbox_retry_cap_seconds: int = Field(default=300, ge=1, le=86400)
    outbox_lease_seconds: int = Field(default=30, ge=5, le=3600)
    runtime_startup_timeout: int = Field(default=120, ge=10, le=86400)
    runtime_heartbeat_timeout: int = Field(default=90, ge=10, le=86400)
    executor_type: str = "opencode_server"
    opencode_binary: str = "opencode"
    opencode_agent: str | None = None
    # 受管 Runtime 使用网关别名；Worker model_name 优先于这个默认值。
    opencode_model: str | None = "nexus-gateway/default"
    nexus_api_base: str = "http://127.0.0.1:18120"
    agentscope_base_url: str = "http://127.0.0.1:18000"
    agentscope_user_id: str = "nexus"
    task_mcp_host: str = Field(default="127.0.0.1", validation_alias="NEXUS_TASK_MCP_HOST")
    task_mcp_port: int = Field(default=18202, ge=1, le=65535, validation_alias="NEXUS_TASK_MCP_PORT")
    # Shared with Nexus API (NEXUS_TASK_MCP_KEY). Empty rejects every Task MCP call.
    task_mcp_key: str | None = Field(default=None, validation_alias="NEXUS_TASK_MCP_KEY")
    opencode_timeout_seconds: int = 3600
    # Control-plane requests (status/messages/session/abort/prompt) must never
    # inherit the long execution budget; a stalled control call has to fail fast
    # so one slow Session cannot block supervision.
    opencode_control_timeout_seconds: int = Field(
        default=60, ge=5, le=600, validation_alias="NEXUS_OPENCODE_CONTROL_TIMEOUT_SECONDS"
    )
    opencode_server_url: str | None = None
    opencode_server_hostname: str = "127.0.0.1"
    opencode_server_port: int = Field(default=14096, ge=1, le=65535)
    opencode_server_username: str = "opencode"
    opencode_server_password: str | None = None
    # Exactly one company OpenCode release is supported. A custom build can
    # disable the check explicitly; never encode "off" as 0.0.0.
    opencode_server_version_check_enabled: bool = True
    opencode_server_version: str = "1.18.35"
    opencode_server_start_timeout_seconds: int = Field(default=30, ge=1, le=300)
    # A turn with no assistant reply is only a failure after this window; a
    # freshly submitted prompt must never be classified as failed.
    opencode_no_reply_grace_seconds: int = Field(default=15, ge=1, le=3600)
    # 确认完成时用量采集的有界读取；超过后按已读消息计算，避免拖慢人工确认。
    opencode_final_usage_max_messages: int = Field(
        default=5000, ge=100, validation_alias="NEXUS_FINAL_USAGE_MAX_MESSAGES"
    )
    opencode_reconcile_interval_seconds: int = Field(default=10, ge=2, le=300)
    # restart must never start a second Session while the old one can still
    # write to the shared task workspace.
    opencode_restart_abort_timeout_seconds: int = Field(default=10, ge=1, le=300)
    ssh_connect_timeout_seconds: int = Field(default=10, validation_alias="NEXUS_SSH_CONNECT_TIMEOUT_SECONDS")

    remote_opencode_version: str = Field(default="1.18.35", validation_alias="NEXUS_REMOTE_OPENCODE_VERSION")
    remote_node_version: str = Field(default="22.23.3", validation_alias="NEXUS_REMOTE_NODE_VERSION")
    remote_npm_registry: str = Field(
        default=DEFAULT_NPM_REGISTRY, validation_alias="NEXUS_REMOTE_NPM_REGISTRY"
    )
    remote_download_base_url: str = Field(
        default="", validation_alias="NEXUS_REMOTE_DOWNLOAD_BASE_URL"
    )
    remote_python_source_url: str = Field(
        default=DEFAULT_PYTHON_SOURCE, validation_alias="NEXUS_REMOTE_PYTHON_SOURCE_URL"
    )
    remote_node_base_url: str = Field(
        default=DEFAULT_NODE_BASE, validation_alias="NEXUS_REMOTE_NODE_BASE_URL"
    )
    remote_node_binary: str = Field(default="", validation_alias="NEXUS_REMOTE_NODE_BINARY")
    remote_npm_artifact_hosts: str = Field(default="", validation_alias="NEXUS_REMOTE_NPM_ARTIFACT_HOSTS")
    # Empty prefers a verified existing interpreter before building private Python.
    remote_python: str = Field(default="", validation_alias="NEXUS_REMOTE_PYTHON")
    remote_pip_index_url: str = Field(
        default=DEFAULT_PIP_INDEX,
        validation_alias="NEXUS_REMOTE_PIP_INDEX_URL",
    )
    remote_deploy_timeout_seconds: int = Field(
        default=1200, ge=60, le=3600, validation_alias="NEXUS_REMOTE_DEPLOY_TIMEOUT_SECONDS"
    )
    # 独立维护 executor 的租约与轮询；租约必须短于部署超时并由执行器持续续租。
    worker_maintenance_lease_seconds: int = Field(
        default=60, ge=15, le=600, validation_alias="NEXUS_WORKER_MAINTENANCE_LEASE_SECONDS"
    )
    worker_maintenance_poll_seconds: float = Field(
        default=1.0, ge=0.05, le=60, validation_alias="NEXUS_WORKER_MAINTENANCE_POLL_SECONDS"
    )
    worker_health_interval_seconds: int = Field(
        default=30, ge=0, le=3600, validation_alias="NEXUS_WORKER_HEALTH_INTERVAL_SECONDS"
    )
    worker_health_max_age_seconds: int = Field(
        default=90, ge=5, le=86400, validation_alias="NEXUS_WORKER_HEALTH_MAX_AGE_SECONDS"
    )
    remote_gateway_local_url: str = Field(
        default="http://127.0.0.1:18110/v1", validation_alias="NEXUS_REMOTE_GATEWAY_LOCAL_URL"
    )
    remote_gateway_public_url: str | None = Field(
        default=None, validation_alias="NEXUS_REMOTE_GATEWAY_PUBLIC_URL"
    )
    # WebSocket the remote Worker Agent dials. Deploy rejects an empty or loopback value.
    worker_agent_gateway_url: str = Field(default="", validation_alias="NEXUS_WORKER_AGENT_GATEWAY_URL")

    mcp_transport: Literal["stdio", "streamable-http"] = Field(
        default="stdio", validation_alias="NEXUS_MCP_TRANSPORT"
    )
    mcp_host: str = Field(default="127.0.0.1", validation_alias="NEXUS_MCP_HOST")
    mcp_port: int = Field(default=18201, ge=1, le=65535, validation_alias="NEXUS_MCP_PORT")
    mcp_streamable_http_path: str = Field(default="/mcp", validation_alias="NEXUS_MCP_STREAMABLE_HTTP_PATH")
    mcp_api_key: str | None = Field(default=None, validation_alias="NEXUS_MCP_API_KEY")
    # Optional public MCP URL. Deploy uses mcp_resource_server_url and rejects loopback.
    mcp_public_url: str | None = Field(default=None, validation_alias="NEXUS_MCP_PUBLIC_URL")
    mcp_issuer_url: str = Field(default="http://127.0.0.1:18201", validation_alias="NEXUS_MCP_ISSUER_URL")
    mcp_resource_server_url: str = Field(
        default="http://127.0.0.1:18201/mcp", validation_alias="NEXUS_MCP_RESOURCE_SERVER_URL"
    )

    llm_wiki_enabled: bool = False
    llm_wiki_api_base_url: str = "http://127.0.0.1:19828/api/v1"
    llm_wiki_api_token: str = Field(default="", repr=False)
    llm_wiki_project_id: str = ""
    llm_wiki_timeout: float = Field(default=10, gt=0, le=300)

    # 图书馆同步到 AgentScope 知识库时使用的 embedding 路由。
    # 只配置网关路由和维度；凭据复用指向网关的内部 credential。
    kb_embedding_json: str = Field(default="", validation_alias="NEXUS_KB_EMBEDDING_JSON", repr=False)

    @field_validator("kb_embedding_json")
    @classmethod
    def validate_kb_embedding_json(cls, value: str) -> str:
        raw = value.strip()
        if not raw:
            return ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("NEXUS_KB_EMBEDDING_JSON 不是 JSON") from None
        if (
            not isinstance(data, dict) or set(data) != {"model_route_id", "dimensions"}
            or not isinstance(data.get("model_route_id"), str) or not data["model_route_id"].strip()
            or type(data.get("dimensions")) is not int or data["dimensions"] <= 0
        ):
            raise ValueError(
                "NEXUS_KB_EMBEDDING_JSON 只接受非空 model_route_id 和正整数 dimensions"
            )
        return raw

    @field_validator("llm_wiki_api_base_url")
    @classmethod
    def validate_llm_wiki_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/api/v1"
        ):
            raise ValueError("知识服务地址必须为 http(s)://<host>[:port]/api/v1")
        return value.rstrip("/")

    # 任务经验复盘：独立后台推理，不创建 AgentScope 长期 Agent。
    experience_review_enabled: bool = Field(
        default=False, validation_alias="NEXUS_EXPERIENCE_REVIEW_ENABLED"
    )
    experience_review_shadow_mode: bool = Field(
        default=True, validation_alias="NEXUS_EXPERIENCE_REVIEW_SHADOW_MODE"
    )
    experience_review_gateway_url: str = Field(
        default="http://127.0.0.1:18110/v1/chat/completions",
        validation_alias="NEXUS_EXPERIENCE_REVIEW_GATEWAY_URL",
    )
    experience_review_model: str = Field(
        default="default", validation_alias="NEXUS_EXPERIENCE_REVIEW_MODEL"
    )
    experience_review_gateway_key: str | None = Field(
        default=None, validation_alias="NEXUS_EXPERIENCE_REVIEW_GATEWAY_KEY"
    )
    experience_review_context_tokens: int = Field(
        default=32768, ge=4096, validation_alias="NEXUS_EXPERIENCE_REVIEW_CONTEXT_TOKENS"
    )
    experience_review_map_input_tokens: int = Field(
        default=8000, ge=1024, validation_alias="NEXUS_EXPERIENCE_REVIEW_MAP_INPUT_TOKENS"
    )
    experience_review_reduce_input_tokens: int = Field(
        default=8000, ge=1024, validation_alias="NEXUS_EXPERIENCE_REVIEW_REDUCE_INPUT_TOKENS"
    )
    experience_review_max_attempts: int = Field(
        default=3, ge=1, le=10, validation_alias="NEXUS_EXPERIENCE_REVIEW_MAX_ATTEMPTS"
    )
    experience_review_max_candidates: int = Field(
        default=3, ge=1, le=10, validation_alias="NEXUS_EXPERIENCE_REVIEW_MAX_CANDIDATES"
    )
    experience_review_max_messages: int = Field(
        default=50000, ge=100, validation_alias="NEXUS_EXPERIENCE_REVIEW_MAX_MESSAGES"
    )
    experience_review_max_source_bytes: int = Field(
        default=536870912, ge=1048576, validation_alias="NEXUS_EXPERIENCE_REVIEW_MAX_SOURCE_BYTES"
    )
    experience_review_max_concurrency: int = Field(
        default=2, ge=1, le=16, validation_alias="NEXUS_EXPERIENCE_REVIEW_MAX_CONCURRENCY"
    )
    experience_review_scan_interval: int = Field(
        default=30, ge=5, validation_alias="NEXUS_EXPERIENCE_REVIEW_SCAN_INTERVAL"
    )
    experience_review_lease_seconds: int = Field(
        default=600, ge=30, validation_alias="NEXUS_EXPERIENCE_REVIEW_LEASE_SECONDS"
    )
    experience_review_page_size: int = Field(
        default=20, ge=1, le=200, validation_alias="NEXUS_EXPERIENCE_REVIEW_PAGE_SIZE"
    )

    nr_test_system_endpoint: str = "http://localhost:8000"
    nr_test_system_discover_path: str = "/api/astra/tasks"
    nr_test_system_result_path: str = "/api/astra/tasks/{external_task_id}/result"
    nr_test_system_token: str | None = None

    # API 中转：控制面直接管理，Gateway Key 仍用于数据面与 Key 自助鉴权。
    # 主密钥（Fernet）只用于加密节点下游凭据，缺失时拒绝相关写操作；生产环境启动强制校验。
    gateway_enabled: bool = True
    gateway_master_key: str | None = Field(default=None, repr=False)
    gateway_probe_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    gateway_health_path: str = "/health"
    gateway_probe_interval_seconds: int = Field(default=300, ge=10, le=86400)
    # 数据面独立进程（app.gateway.server:app）：实例心跳、租约 TTL、配置刷新与
    # 运行态健康检查。实例 ID 缺省时按主机名与进程号生成。
    gateway_instance_id: str | None = None
    gateway_heartbeat_interval: int = Field(default=10, ge=1, le=3600)
    gateway_heartbeat_ttl: int = Field(default=60, ge=5, le=86400)
    gateway_lease_ttl: int = Field(default=900, ge=10, le=86400)
    gateway_config_poll_interval: int = Field(default=10, ge=1, le=3600)
    gateway_runtime_health_interval: int = Field(default=5, ge=1, le=3600)
    # 已吊销 Key 摘要的后台追平周期：Redis 清空/重启后据此从 DB 重建阻断列表。
    gateway_revocation_sync_interval: int = Field(default=60, ge=5, le=86400)

    @model_validator(mode="after")
    def reject_legacy_worker_env_names(self):
        """Worker/remote/MCP use only NEXUS_*; old ASTRA_* names are rejected."""

        legacy = []
        for field in type(self).model_fields.values():
            alias = field.validation_alias
            if isinstance(alias, str) and alias.startswith("NEXUS_"):
                old = "ASTRA_" + alias.removeprefix("NEXUS_")
                if old in os.environ:
                    legacy.append(old)
        env_file = type(self).model_config.get("env_file")
        if env_file:
            try:
                for line in Path(env_file).read_text(encoding="utf-8").splitlines():
                    key = line.split("=", 1)[0].strip()
                    if not key.startswith("ASTRA_"):
                        continue
                    alias = "NEXUS_" + key.removeprefix("ASTRA_")
                    if any(field.validation_alias == alias for field in type(self).model_fields.values()):
                        legacy.append(key)
            except OSError:
                pass
        if legacy:
            raise ValueError(
                "以下 Worker/远程/MCP 环境变量已迁移到 NEXUS_*，不再接受旧名："
                + ", ".join(sorted(set(legacy)))
            )
        return self

    @field_validator("database_backend")
    @classmethod
    def validate_database_backend(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"mysql", "sqlite"}:
            raise ValueError("ASTRA_DATABASE_BACKEND 只支持 mysql 或 sqlite")
        return normalized

    @field_validator("database_charset")
    @classmethod
    def validate_database_charset(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != "utf8mb4":
            raise ValueError("Astra MySQL 必须使用 utf8mb4 字符集")
        return normalized

    @field_validator("gateway_master_key")
    @classmethod
    def validate_gateway_master_key(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        from app.gateway.security import CredentialCipher

        CredentialCipher(value)
        return value.strip()

    @field_validator("gateway_health_path")
    @classmethod
    def validate_gateway_health_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/"):
            raise ValueError("ASTRA_GATEWAY_HEALTH_PATH 必须以 / 开头")
        return normalized

    @field_validator("executor_type")
    @classmethod
    def validate_executor_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"opencode_server", "opencode_cli_fallback", "worker"}:
            raise ValueError(
                "ASTRA_EXECUTOR_TYPE 只支持 opencode_server、opencode_cli_fallback 或 worker"
            )
        return normalized

    @field_validator("opencode_server_version")
    @classmethod
    def validate_opencode_server_version(cls, value: str) -> str:
        normalized = value.strip().removeprefix("v")
        if not normalized:
            raise ValueError("ASTRA_OPENCODE_SERVER_VERSION 不能为空")
        if normalized == "0.0.0":
            raise ValueError(
                "不能用 0.0.0 关闭版本校验；如需关闭请设置 ASTRA_OPENCODE_SERVER_VERSION_CHECK_ENABLED=false"
            )
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", normalized):
            raise ValueError("ASTRA_OPENCODE_SERVER_VERSION 必须是完整版本号")
        return normalized

    @model_validator(mode="after")
    def validate_opencode_server_security(self):
        parsed_url = urlparse(self.opencode_server_url) if self.opencode_server_url else None
        if parsed_url and (parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname):
            raise ValueError("ASTRA_OPENCODE_SERVER_URL 必须是完整的 HTTP(S) URL")
        configured_host = parsed_url.hostname if parsed_url else self.opencode_server_hostname
        if (
            self.executor_type == "opencode_server"
            and configured_host not in {"127.0.0.1", "localhost", "::1"}
            and not self.opencode_server_password
        ):
            raise ValueError("OpenCode Server 非 loopback 监听时必须配置密码")
        if (
            self.mcp_transport == "streamable-http"
            and self.mcp_host not in {"127.0.0.1", "localhost", "::1"}
            and not self.mcp_api_key
        ):
            raise ValueError("数字图书馆 MCP 非 loopback 监听时必须配置 NEXUS_MCP_API_KEY")
        if not self.mcp_streamable_http_path.startswith("/"):
            raise ValueError("NEXUS_MCP_STREAMABLE_HTTP_PATH 必须以 / 开头")
        if self.gateway_enabled and self.environment == "production":
            if not self.gateway_master_key:
                raise ValueError("生产模式启用 API 中转时必须配置 ASTRA_GATEWAY_MASTER_KEY")
        return self

    def absolute_path(self, value: Path) -> Path:
        expanded = Path(os.path.expandvars(str(value))).expanduser()
        if expanded.is_absolute():
            return expanded.resolve()
        return (PROJECT_ROOT / expanded).resolve()

    def employee_workspace_path(self, value: str | Path) -> Path:
        expanded = Path(os.path.expandvars(str(value))).expanduser()
        if expanded.is_absolute():
            return expanded.resolve()
        # Historical Astra services run from the service root. Keep existing relative
        # employee workspace values stable while removing current-directory reliance.
        return (BACKEND_ROOT / expanded).resolve()

    @property
    def resolved_data_dir(self) -> Path:
        return self.absolute_path(self.data_dir)

    @property
    def resolved_workspace_root(self) -> Path:
        return self.absolute_path(self.workspace_root)

    @property
    def resolved_log_dir(self) -> Path:
        return self.absolute_path(self.log_dir)

    @property
    def resolved_runtime_dir(self) -> Path:
        return self.absolute_path(self.runtime_dir)

    @property
    def resolved_config_dir(self) -> Path:
        return self.absolute_path(self.config_dir)

    @property
    def resolved_opencode_server_url(self) -> str:
        if self.opencode_server_url:
            return self.opencode_server_url.rstrip("/")
        host = (
            "127.0.0.1"
            if self.opencode_server_hostname in {"0.0.0.0", "::"}
            else self.opencode_server_hostname
        )
        return f"http://{host}:{self.opencode_server_port}"

    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url:
            if self.database_url.startswith("sqlite+aiosqlite:///./"):
                relative = self.database_url.removeprefix("sqlite+aiosqlite:///./")
                # Before configuration was centralized, Astra services ran from
                # services/nexus-api/. Preserve that location for existing relative URLs.
                path = (BACKEND_ROOT / relative).resolve().as_posix()
                return f"sqlite+aiosqlite:///{path}"
            return self.database_url
        if self.database_backend == "sqlite":
            path = (self.resolved_data_dir / "nexus.db").as_posix()
            return f"sqlite+aiosqlite:///{path}"

        username = quote(self.database_username, safe="")
        password = quote(self.database_password, safe="")
        credentials = username if not password else f"{username}:{password}"
        database = quote(self.database_name, safe="")
        return (
            f"mysql+asyncmy://{credentials}@{self.database_host}:{self.database_port}/"
            f"{database}?charset={self.database_charset}"
        )

    @property
    def database_description(self) -> str:
        if self.database_url:
            scheme, _, remainder = self.database_url.partition("://")
            if "@" in remainder:
                remainder = remainder.split("@", 1)[1]
            return f"{scheme}://{remainder}"
        if self.database_backend == "mysql":
            return (
                f"mysql://{self.database_host}:{self.database_port}/"
                f"{self.database_name}?charset={self.database_charset}"
            )
        return str(self.resolved_data_dir / "nexus.db")

    @property
    def redis_description(self) -> str:
        scheme, _, remainder = self.redis_url.partition("://")
        if "@" in remainder:
            remainder = remainder.split("@", 1)[1]
        return f"{scheme}://{remainder}"


@lru_cache
def get_settings() -> Settings:
    return Settings()

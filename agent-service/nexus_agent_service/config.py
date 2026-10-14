# -*- coding: utf-8 -*-
"""Nexus AgentScope Service 的部署配置。

配置来源只有两处：进程环境变量，以及仓库根目录的 ``.env``（存在时）。
不读取用户 Home 下任何凭据文件，也绝不把模型密钥写入日志或响应。
"""
from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# ``agent-service/nexus_agent_service/config.py`` → 仓库根目录
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_INTERNAL_USER_ID = "nexus-service"
DEFAULT_MODEL_NAME = "deepseek-flash"
DEFAULT_MODEL_BASE_URL = "https://api.deepseek.com"
# 运行 guard 默认回调的 Nexus 后端地址（仅回环）。仅在配置了内部秘密时启用。
DEFAULT_GUARD_URL = "http://127.0.0.1:8765"
# 固定凭据 ID 让每次启动都命中同一记录（upsert 幂等），不会重复新建凭据。
DEFAULT_CREDENTIAL_ID = "nexus-default-deepseek-credential"
DEFAULT_CREDENTIAL_NAME = "nexus-default"
# 默认模型绑定类型，与 DeepSeekChatModel.type 对应。
DEFAULT_CHAT_MODEL_TYPE = "deepseek_chat"
# 原生 DeepSeekCredential 的类型标识，用于复用已持久化凭据时校验。
DEFAULT_CREDENTIAL_TYPE = "deepseek_credential"

MIN_PORT = 1
MAX_PORT = 65535


def load_project_env(root: Path = PROJECT_ROOT) -> None:
    """加载仓库根目录的 ``.env``（如存在）。

    使用 ``override=False``，显式环境变量优先于 ``.env``，例如由
    ``scripts/services.py`` 或部署环境注入的值不会被文件覆盖。
    """
    env_path = root / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


def _env_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _validate_host(host: str) -> str:
    """只允许回环监听地址；``localhost`` 显式放行。

    与 README 约定一致：AgentScope 服务仅监听回环，不直接对外暴露。
    """
    if host == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(
            f"AGENTSCOPE_SERVICE_HOST 必须是回环地址，实际为 {host!r}",
        ) from exc
    if not address.is_loopback:
        raise ValueError(
            f"AGENTSCOPE_SERVICE_HOST 必须是回环地址，实际为 {host!r}",
        )
    return host


def _validate_port(port: int) -> int:
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValueError(
            f"AGENTSCOPE_SERVICE_PORT 必须在 {MIN_PORT}..{MAX_PORT}，"
            f"实际为 {port}",
        )
    return port


def _parse_bool(raw: str | None) -> bool:
    """解析布尔开关；未配置或非真值文本返回 ``False``。"""
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_model_parameters(raw: str | None) -> dict:
    """解析 ``AGENTSCOPE_MODEL_PARAMETERS``（JSON 对象）。

    未配置时返回空对象，保持官方默认参数；配置非法时明确报错，而不是
    静默忽略或写入猜测值。
    """
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "AGENTSCOPE_MODEL_PARAMETERS 必须是合法 JSON 对象，"
            f"实际为 {raw!r}",
        ) from exc
    if not isinstance(value, dict):
        raise ValueError(
            "AGENTSCOPE_MODEL_PARAMETERS 必须是 JSON 对象，"
            f"实际为 {type(value).__name__}",
        )
    return value


@dataclass(frozen=True)
class ServiceConfig:
    """运行期服务配置。"""

    host: str
    port: int
    data_dir: Path
    internal_user_id: str
    model_api_key: str | None = field(repr=False)
    model_base_url: str
    model_name: str
    credential_id: str
    credential_name: str
    model_parameters: dict = field(default_factory=dict)
    # 原生运行 guard：内部秘密与后端回调地址。秘密仅服务端配置，绝不下发
    # 前端、不写日志；为空表示不启用 guard。
    nexus_internal_token: str | None = field(default=None, repr=False)
    nexus_guard_url: str = DEFAULT_GUARD_URL
    # 可选：知识库功能开关与持久化目录。默认关闭（不配置 embedding 凭据时
    # 目录接口返回 503 feature-disabled，不伪造空目录）。
    knowledge_enabled: bool = False
    knowledge_vector_dir: Path | None = None
    knowledge_blob_dir: Path | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "agentscope.db"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path}"

    @property
    def workspace_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def base_url(self) -> str:
        host = self.host
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @property
    def model_configured(self) -> bool:
        """是否已提供模型密钥。密钥不参与返回体。"""
        return bool(self.model_api_key)

    def default_chat_model_config(self) -> dict:
        """返回供 Nexus 创建原生 Session 的默认 ChatModelConfig。

        只含 credential_id/type/model/parameters，绝不包含密钥。
        ``parameters`` 来自 ``AGENTSCOPE_MODEL_PARAMETERS``（JSON 对象）；
        未配置时保持官方合理默认值（空对象），不擅自添加额外模型。
        """
        return {
            "type": DEFAULT_CHAT_MODEL_TYPE,
            "credential_id": self.credential_id,
            "model": self.model_name,
            "parameters": dict(self.model_parameters),
        }


def load_config(env: dict | None = None) -> ServiceConfig:
    """从环境（及根 ``.env``）构建 :class:`ServiceConfig`。"""
    if env is None:
        load_project_env()
        env = os.environ

    def get(key: str, default: str | None = None) -> str | None:
        value = env.get(key)
        if value is None:
            return default
        value = value.strip()
        return value or default

    raw_port = get("AGENTSCOPE_SERVICE_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"AGENTSCOPE_SERVICE_PORT 必须是整数，实际为 {raw_port!r}",
        ) from exc
    port = _validate_port(port)

    host = _validate_host(get("AGENTSCOPE_SERVICE_HOST", DEFAULT_HOST))

    data_dir = _env_path(get("AGENTSCOPE_DATA_DIR", "data/agentscope"))

    return ServiceConfig(
        host=host,
        port=port,
        data_dir=data_dir,
        internal_user_id=get(
            "AGENTSCOPE_INTERNAL_USER_ID",
            DEFAULT_INTERNAL_USER_ID,
        ),
        model_api_key=get("AGENTSCOPE_MODEL_API_KEY"),
        model_base_url=get(
            "AGENTSCOPE_MODEL_BASE_URL",
            DEFAULT_MODEL_BASE_URL,
        ),
        model_name=get("AGENTSCOPE_MODEL_NAME", DEFAULT_MODEL_NAME),
        credential_id=get("AGENTSCOPE_CREDENTIAL_ID", DEFAULT_CREDENTIAL_ID),
        credential_name=get(
            "AGENTSCOPE_CREDENTIAL_NAME",
            DEFAULT_CREDENTIAL_NAME,
        ),
        model_parameters=_parse_model_parameters(
            get("AGENTSCOPE_MODEL_PARAMETERS"),
        ),
        nexus_internal_token=get("NEXUS_INTERNAL_TOKEN"),
        nexus_guard_url=get("NEXUS_GUARD_URL", DEFAULT_GUARD_URL),
        knowledge_enabled=_parse_bool(
            get("AGENTSCOPE_KNOWLEDGE_ENABLED"),
        ),
        knowledge_vector_dir=_env_path(
            get(
                "AGENTSCOPE_KNOWLEDGE_VECTOR_DIR",
                "data/agentscope/knowledge/vector",
            ),
        ),
        knowledge_blob_dir=_env_path(
            get(
                "AGENTSCOPE_KNOWLEDGE_BLOB_DIR",
                "data/agentscope/knowledge/blobs",
            ),
        ),
    )

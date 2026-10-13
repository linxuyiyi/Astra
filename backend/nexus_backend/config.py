# -*- coding: utf-8 -*-
"""Nexus 业务后端的部署配置。

配置来源只有两处：进程环境变量，以及仓库根目录的 ``.env``（存在时）。
后端只保存到 AgentScope 服务的连接配置（服务 URL、内部租户），不保存任何
模型密钥或模型参数；模型配置由 AgentScope 服务统一管理，后端仅通过
``/nexus/defaults`` 读取。
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# ``backend/nexus_backend/config.py`` → 仓库根目录
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_DATA_DIR = "data/nexus-backend"
DEFAULT_AGENTSCOPE_SERVICE_URL = "http://127.0.0.1:8766"
DEFAULT_INTERNAL_USER_ID = "nexus-service"
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 5.0

# 后端健康检查要求的上游官方服务固定版本。
EXPECTED_AGENTSCOPE_VERSION = "2.0.10"

MIN_PORT = 1
MAX_PORT = 65535


def load_project_env(root: Path = PROJECT_ROOT) -> None:
    """加载仓库根目录的 ``.env``（如存在）。

    使用 ``override=False``，显式环境变量优先于 ``.env``。与 AgentScope
    服务读取同一份根 ``.env``，但只关心后端自己的变量。
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
    """后端只允许监听回环地址，与 AgentScope 服务边界一致。"""
    if host == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(
            f"NEXUS_BACKEND_HOST 必须是回环地址，实际为 {host!r}",
        ) from exc
    if not address.is_loopback:
        raise ValueError(
            f"NEXUS_BACKEND_HOST 必须是回环地址，实际为 {host!r}",
        )
    return host


def _validate_port(port: int) -> int:
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValueError(
            f"NEXUS_BACKEND_PORT 必须在 {MIN_PORT}..{MAX_PORT}，实际为 {port}",
        )
    return port


def _validate_service_url(url: str) -> str:
    """校验上游服务 URL 为回环 http(s)，避免后端连接任意外部地址。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"AGENTSCOPE_SERVICE_URL 必须是 http(s) 地址，实际为 {url!r}",
        )
    host = parsed.hostname
    if not host:
        raise ValueError(f"AGENTSCOPE_SERVICE_URL 缺少主机名：{url!r}")
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError(
                "AGENTSCOPE_SERVICE_URL 必须指向回环地址，"
                f"实际为 {host!r}",
            ) from exc
        if not address.is_loopback:
            raise ValueError(
                f"AGENTSCOPE_SERVICE_URL 必须指向回环地址，实际为 {host!r}",
            )
    return url.rstrip("/")


def _duration(value: str | None, default: float, key: str) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是秒数，实际为 {value!r}") from exc
    if seconds <= 0:
        raise ValueError(f"{key} 必须为正数，实际为 {seconds}")
    return seconds


@dataclass(frozen=True)
class BackendConfig:
    """Nexus 后端运行期配置（不含任何模型密钥）。"""

    host: str
    port: int
    data_dir: Path
    agentscope_service_url: str
    internal_user_id: str
    request_timeout: float
    connect_timeout: float

    @property
    def base_url(self) -> str:
        host = self.host
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @property
    def expected_agentscope_version(self) -> str:
        return EXPECTED_AGENTSCOPE_VERSION


def load_config(env: dict | None = None) -> BackendConfig:
    """从环境（及根 ``.env``）构建 :class:`BackendConfig`。"""
    if env is None:
        load_project_env()
        env = os.environ

    def get(key: str, default: str | None = None) -> str | None:
        value = env.get(key)
        if value is None:
            return default
        value = value.strip()
        return value or default

    raw_port = get("NEXUS_BACKEND_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"NEXUS_BACKEND_PORT 必须是整数，实际为 {raw_port!r}",
        ) from exc
    port = _validate_port(port)

    host = _validate_host(get("NEXUS_BACKEND_HOST", DEFAULT_HOST))

    data_dir = _env_path(get("NEXUS_BACKEND_DATA_DIR", DEFAULT_DATA_DIR))

    service_url = _validate_service_url(
        get("AGENTSCOPE_SERVICE_URL", DEFAULT_AGENTSCOPE_SERVICE_URL),
    )

    return BackendConfig(
        host=host,
        port=port,
        data_dir=data_dir,
        agentscope_service_url=service_url,
        internal_user_id=get(
            "AGENTSCOPE_INTERNAL_USER_ID",
            DEFAULT_INTERNAL_USER_ID,
        ),
        request_timeout=_duration(
            get("NEXUS_BACKEND_REQUEST_TIMEOUT"),
            DEFAULT_REQUEST_TIMEOUT,
            "NEXUS_BACKEND_REQUEST_TIMEOUT",
        ),
        connect_timeout=_duration(
            get("NEXUS_BACKEND_CONNECT_TIMEOUT"),
            DEFAULT_CONNECT_TIMEOUT,
            "NEXUS_BACKEND_CONNECT_TIMEOUT",
        ),
    )

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000/api/v1"
DEFAULT_TIMEOUT_SECONDS = 15.0
SESSION_COOKIE_ENV = "NR_MCP_SESSION_COOKIE"
API_BASE_URL_ENV = "NR_MCP_API_BASE_URL"
TIMEOUT_ENV = "NR_MCP_TIMEOUT_SECONDS"


class ConfigurationError(Exception):
    """MCP 运行配置缺失或不合法。"""

    code = "configuration_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    session_cookie: str = field(repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        raw_base_url = (env.get(API_BASE_URL_ENV) or DEFAULT_API_BASE_URL).strip()
        base_url = raw_base_url.rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError(
                f"{API_BASE_URL_ENV} 必须是合法的 http(s) 基地址，当前为 {raw_base_url!r}"
            )

        session_cookie = (env.get(SESSION_COOKIE_ENV) or "").strip()
        if not session_cookie:
            raise ConfigurationError(
                f"缺少 {SESSION_COOKIE_ENV}，请注入既有会话 Cookie（只读），例如 sessionid=..."
            )

        raw_timeout = (env.get(TIMEOUT_ENV) or str(DEFAULT_TIMEOUT_SECONDS)).strip()
        try:
            timeout_seconds = float(raw_timeout)
        except ValueError as exc:
            raise ConfigurationError(f"{TIMEOUT_ENV} 必须是数字，当前为 {raw_timeout!r}") from exc
        if timeout_seconds <= 0:
            raise ConfigurationError(f"{TIMEOUT_ENV} 必须大于 0，当前为 {timeout_seconds}")

        return cls(
            api_base_url=base_url,
            session_cookie=session_cookie,
            timeout_seconds=timeout_seconds,
        )

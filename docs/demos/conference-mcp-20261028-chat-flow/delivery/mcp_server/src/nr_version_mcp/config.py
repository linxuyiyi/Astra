from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

STATUS_CHOICES = ("DRAFT", "RELEASED", "ARCHIVED")
DEFAULT_BASE_URL = "http://127.0.0.1:8000/api/v1"
DEFAULT_STATUSES = ("RELEASED",)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigError(ValueError):
    """配置缺失或不满足安全边界。"""


@dataclass(frozen=True)
class Settings:
    base_url: str
    session_cookie: str = field(repr=False)
    default_statuses: tuple[str, ...] = DEFAULT_STATUSES
    timeout: float = 15.0
    max_page_size: int = 100
    max_fetch_pages: int = 50


def parse_statuses(value: str | None, *, source: str) -> tuple[str, ...]:
    raw = (value or "").strip()
    if not raw:
        raise ConfigError(f"{source} 不能为空，可选值：{', '.join(STATUS_CHOICES)}")
    statuses: list[str] = []
    for part in raw.split(","):
        status = part.strip().upper()
        if not status:
            continue
        if status not in STATUS_CHOICES:
            raise ConfigError(
                f"{source} 包含未知状态 {part!r}，可选值：{', '.join(STATUS_CHOICES)}"
            )
        if status not in statuses:
            statuses.append(status)
    if not statuses:
        raise ConfigError(f"{source} 不能为空，可选值：{', '.join(STATUS_CHOICES)}")
    return tuple(statuses)


def _loopback_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        raise ConfigError("NR_API_BASE_URL 必须使用 http 或 https")
    if parts.hostname not in LOOPBACK_HOSTS:
        raise ConfigError(
            "NR_API_BASE_URL 只允许本机回环地址（127.0.0.1、localhost 或 ::1），禁止访问生产环境"
        )
    if parts.query or parts.fragment:
        raise ConfigError("NR_API_BASE_URL 不能包含查询参数或片段")
    if not parts.path:
        raise ConfigError("NR_API_BASE_URL 必须包含 API 路径，例如 /api/v1")
    return value


def _session_cookie(raw: str, cookie_name: str) -> str:
    value = raw.strip()
    if not value:
        raise ConfigError("必须通过环境变量 NR_SESSION_COOKIE 注入登录 Cookie")
    if "\r" in value or "\n" in value:
        raise ConfigError("NR_SESSION_COOKIE 不能包含换行符")
    if "=" not in value:
        value = f"{cookie_name}={value}"
    return value


def _positive_float(raw: str | None, *, name: str, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字") from exc
    if value <= 0:
        raise ConfigError(f"{name} 必须大于 0")
    return value


def _positive_int(raw: str | None, *, name: str, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数") from exc
    if value <= 0:
        raise ConfigError(f"{name} 必须大于 0")
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    base_url = _loopback_base_url(env.get("NR_API_BASE_URL", DEFAULT_BASE_URL))
    cookie_name = (env.get("NR_SESSION_COOKIE_NAME") or "sessionid").strip()
    if not cookie_name or "=" in cookie_name or ";" in cookie_name:
        raise ConfigError("NR_SESSION_COOKIE_NAME 必须是合法的 Cookie 名称")
    session_cookie = _session_cookie(env.get("NR_SESSION_COOKIE", ""), cookie_name)
    raw_default_statuses = env.get("NR_DEFAULT_STATUSES")
    if raw_default_statuses and raw_default_statuses.strip():
        default_statuses = parse_statuses(raw_default_statuses, source="NR_DEFAULT_STATUSES")
    else:
        default_statuses = DEFAULT_STATUSES
    return Settings(
        base_url=base_url,
        session_cookie=session_cookie,
        default_statuses=default_statuses,
        timeout=_positive_float(
            env.get("NR_REQUEST_TIMEOUT"), name="NR_REQUEST_TIMEOUT", default=15.0
        ),
        max_page_size=_positive_int(
            env.get("NR_MAX_PAGE_SIZE"), name="NR_MAX_PAGE_SIZE", default=100
        ),
        max_fetch_pages=_positive_int(
            env.get("NR_MAX_FETCH_PAGES"), name="NR_MAX_FETCH_PAGES", default=50
        ),
    )

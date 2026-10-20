"""Intranet addresses the Worker dials. Loopback is the control host, not the Worker."""

from urllib.parse import urlparse

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def require_direct_url(url: str, name: str) -> str:
    host = (urlparse(url).hostname or "").strip().lower()
    if host in _LOOPBACK or not host:
        raise RuntimeError(f"{name} 必须是 Worker 能访问的内网地址")
    return url.rstrip("/")

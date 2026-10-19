"""Environment handed to processes the Worker Agent starts.

The Agent's own token and Gateway address stay in the Agent process. A model
shell tool inherits the OpenCode process environment, so that process must not
see Worker or Gateway credentials.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

_EXACT = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "TZ",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SHELL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "NODE_EXTRA_CA_CERTS",
    }
)
_PREFIXES = ("LC_", "XDG_", "SSL_CERT_")
_BLOCKED_PARTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
_BLOCKED_PREFIXES = ("ASTRA_", "NEXUS_")
_ALLOW_ENV = "NEXUS_WORKER_CHILD_ENV_ALLOW"


def _blocked(name: str) -> bool:
    upper = name.upper()
    return any(upper.startswith(prefix) for prefix in _BLOCKED_PREFIXES)


def _extra_allowed(name: str) -> bool:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
    if not name or any(character not in letters for character in name):
        return False
    upper = name.upper()
    if _blocked(upper):
        return False
    return not any(part in upper for part in _BLOCKED_PARTS)


def _configured_allow(source: Mapping[str, str]) -> list[str]:
    raw = source.get(_ALLOW_ENV)
    if raw is None:
        raw = os.environ.get(_ALLOW_ENV, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def child_environ(source: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ if source is None else source
    allowed = set(_EXACT)
    for name in _configured_allow(env):
        if _extra_allowed(name):
            allowed.add(name)
    return {
        key: value
        for key, value in env.items()
        if not _blocked(key) and (key in allowed or key.startswith(_PREFIXES))
    }

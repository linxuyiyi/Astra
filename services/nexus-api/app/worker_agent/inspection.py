"""Worker 本地只读运行态探测。

只通过 loopback HTTP 和只读 Git 命令观察 OpenCode，不拉起进程、不改配置、
不依赖端口监听或 systemd active 宣告健康。命令 payload 只能提供期望的装备
名称/版本，不能提供路径、URL 或 shell。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.worker_agent.jsonc import loads_jsonc
from app.worker_agent.process_env import child_environ

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"

_DEFAULT_MCP = "nexus-library"
_TIMEOUT_SECONDS = 3.0
_MAX_BODY_BYTES = 4 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 5.0
_MAX_NAME_LENGTH = 200
_BEARER = re.compile(r"(?i)bearer\s+\S+")
_KEY_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|secret|token|authorization|oauth)\b"
    r"\s*[:=]\s*[^\s,;\"'}]+"
)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+")
_FRONTMATTER = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)", re.DOTALL)


class _ProbeError(Exception):
    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def normalized_skill_body(content: Any) -> str:
    """返回 OpenCode ``/skill`` 与控制器共用的语义 body。

    OpenCode 1.18.35 使用 markdown.content（不含 frontmatter）；控制器读取的是
    完整 SKILL.md。这里统一去掉 frontmatter、换行符和首尾空白，避免两个不同
    语义的 hash 被直接比较。
    """

    if not isinstance(content, str):
        return ""
    text = content.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    match = _FRONTMATTER.match(text)
    if match is not None:
        text = text[match.end() :]
    return text.strip()


def skill_content_hash(content: Any) -> str | None:
    """语义 body 的 sha256；内容缺失或为空时返回 None，不伪造空内容。"""

    body = normalized_skill_body(content)
    if not body:
        return None
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def inspect_runtime_from_payload(
    opencode_url: str,
    workspace_root: str,
    data: Any,
) -> dict[str, Any]:
    """命令入口。只读取白名单字段，其余 payload（path/url/shell）一律忽略。"""

    options = data if isinstance(data, dict) else {}
    return inspect_runtime(
        opencode_url,
        workspace_root,
        expected_skills=_names(options.get("expected_skills")),
        expected_mcp=_names(options.get("expected_mcp")),
        expected_model=_text(options.get("expected_model")),
        expected_opencode_version=_text(options.get("expected_opencode_version")),
    )


def inspect_runtime(
    opencode_url: str,
    workspace_root: str,
    *,
    expected_skills: list[str] | None = None,
    expected_mcp: list[str] | None = None,
    expected_model: str = "",
    expected_opencode_version: str = "",
) -> dict[str, Any]:
    """返回稳定结构；每个环节独立探测，失败只影响该环节。"""

    base = str(opencode_url or "").rstrip("/")
    workspace = str(workspace_root or "")
    skills_expected = _names(expected_skills)
    mcp_expected = _names([_DEFAULT_MCP, *(expected_mcp or [])])
    local_config = _read_local_config()
    effective_config = _read_effective_config(base, workspace)
    effective_confirmed = effective_config is not None
    config = effective_config if effective_confirmed else local_config
    secrets = _secrets(config) | _secrets(local_config)
    endpoints = _config_mcp_endpoints(effective_config, effective_confirmed)

    return {
        "opencode": _inspect_opencode(base, workspace, expected_opencode_version, secrets),
        "model": _inspect_model(
            config,
            local_config,
            expected_model,
            effective_confirmed=effective_confirmed,
        ),
        "mcp": _inspect_mcp(base, workspace, mcp_expected, endpoints, secrets),
        "skills": _inspect_skills(base, workspace, skills_expected, secrets),
        "repository": _inspect_repository(workspace, secrets),
        "checked_at": datetime.now(UTC).isoformat(),
    }


def status_summary(opencode_url: str) -> dict[str, Any]:
    """轻量状态：只探一次 /global/health，供 status 命令使用。"""

    base = str(opencode_url or "").rstrip("/")
    managed_by = _managed_by()
    if not base:
        return {
            "opencode": UNKNOWN,
            "version": None,
            "error": "未配置 OpenCode 地址",
            "managed_by": managed_by,
        }
    try:
        payload = _get_json(base + "/global/health")
    except _ProbeError as exc:
        return {
            "opencode": UNHEALTHY,
            "version": None,
            "error": _redact(exc.message),
            "managed_by": managed_by,
        }
    if not isinstance(payload, dict) or payload.get("healthy") is not True:
        return {
            "opencode": UNHEALTHY,
            "version": None,
            "error": "OpenCode 健康接口未通过",
            "managed_by": managed_by,
        }
    version = payload.get("version")
    return {
        "opencode": HEALTHY,
        "version": str(version) if version is not None else None,
        "error": None,
        "managed_by": managed_by,
    }


def _managed_by() -> str:
    """只有部署时写入 OpenCode unit 的环境才声明 systemd 管理。"""

    return "systemd" if (os.environ.get("NEXUS_WORKER_OPENCODE_UNIT") or "").strip() else "external"


def _inspect_opencode(
    base: str, workspace: str, expected_version: str, secrets: set[str]
) -> dict[str, Any]:
    if not base:
        return {"status": UNKNOWN, "version": None, "error": "未配置 OpenCode 地址"}
    try:
        payload = _get_json(base + "/global/health")
    except _ProbeError as exc:
        return {"status": UNHEALTHY, "version": None, "error": _redact(exc.message, secrets)}
    if not isinstance(payload, dict) or payload.get("healthy") is not True:
        return {"status": UNHEALTHY, "version": None, "error": "OpenCode 健康接口未通过"}
    version = payload.get("version")
    version_text = str(version) if version is not None else None
    if expected_version and _version_key(version_text) != _version_key(expected_version):
        return {
            "status": UNHEALTHY,
            "version": version_text,
            "error": f"OpenCode 版本不匹配，期望 {_redact(expected_version, secrets)}",
        }
    try:
        _get_json(base + "/session/status", {"directory": workspace} if workspace else None)
    except _ProbeError as exc:
        return {
            "status": UNHEALTHY,
            "version": version_text,
            "error": _redact(f"OpenCode session/status 不可用：{exc.message}", secrets),
        }
    return {"status": HEALTHY, "version": version_text, "error": None}


def _inspect_model(
    config: Any,
    local_config: Any,
    expected_model: str,
    *,
    effective_confirmed: bool,
) -> dict[str, Any]:
    """只核对生效模型与网关配置；网关可用性不属于 Worker 健康事实。"""
    effective = config if isinstance(config, dict) else {}
    local = local_config if isinstance(local_config, dict) else {}
    if not effective_confirmed:
        # 本地文件只够取凭据，不能替代 /config 宣告实际模型已验证。
        return {
            "status": UNKNOWN,
            "configured": False,
            "model": None,
            "error": "OpenCode 生效配置未确认，无法确认实际模型",
        }
    actual = _text(effective.get("model"))
    desired = _text(expected_model)
    if not actual:
        return {
            "status": UNHEALTHY,
            "configured": False,
            "model": None,
            "error": "生效配置未配置模型",
        }
    if desired and desired != actual:
        return {
            "status": UNHEALTHY,
            "configured": True,
            "model": actual,
            "error": "实际模型与期望模型不一致",
        }
    provider_name, _, model_id = actual.partition("/")
    options = _credential_options(effective, local, provider_name)
    base_url = _text(options.get("baseURL")) if isinstance(options, dict) else ""
    api_key = _text(options.get("apiKey")) if isinstance(options, dict) else ""
    if not provider_name or not model_id or not options:
        return {
            "status": UNHEALTHY,
            "configured": False,
            "model": actual,
            "error": "生效配置缺少模型 provider",
        }
    if not base_url or not api_key:
        return {
            "status": UNHEALTHY,
            "configured": False,
            "model": actual,
            "error": "模型 provider 缺少网关地址或凭据",
        }

    return {
        "status": HEALTHY,
        "configured": True,
        "model": actual,
        "error": None,
    }


def _config_mcp_endpoints(config: Any, confirmed: bool) -> dict[str, str] | None:
    """生效 /config 的 MCP URL。配置不可读时未知，不用本地文件替代。"""

    if not confirmed or not isinstance(config, dict):
        return None
    raw = config.get("mcp")
    if not isinstance(raw, dict):
        return {}
    endpoints: dict[str, str] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue
        endpoints[name] = _sanitize_url(entry.get("url")) if _text(entry.get("url")) else ""
    return endpoints


def _inspect_mcp(
    base: str,
    workspace: str,
    expected: list[str],
    endpoints: dict[str, str] | None,
    secrets: set[str],
) -> dict[str, Any]:
    try:
        payload = _get_json(base + "/mcp", {"directory": workspace} if workspace else None)
    except _ProbeError as exc:
        return {
            "status": UNKNOWN,
            "connected": False,
            "missing": expected,
            "endpoints": endpoints,
            "error": _redact(f"MCP 状态不可读：{exc.message}", secrets),
        }
    if not isinstance(payload, dict):
        return {
            "status": UNKNOWN,
            "connected": False,
            "missing": expected,
            "endpoints": endpoints,
            "error": "MCP 状态返回结构无法识别",
        }
    missing = [
        name
        for name in expected
        if not (
            isinstance(payload.get(name), dict)
            and payload[name].get("status") == "connected"
        )
    ]
    if missing:
        return {
            "status": UNHEALTHY,
            "connected": False,
            "missing": missing,
            "endpoints": endpoints,
            "error": "MCP 未连接：" + "、".join(missing),
        }
    return {
        "status": HEALTHY,
        "connected": True,
        "missing": [],
        "endpoints": endpoints,
        "error": None,
    }


def _inspect_skills(
    base: str, workspace: str, expected: list[str], secrets: set[str]
) -> dict[str, Any]:
    try:
        payload = _get_json(base + "/skill", {"directory": workspace} if workspace else None)
    except _ProbeError as exc:
        return {
            "status": UNKNOWN,
            "discovered": [],
            "missing": expected,
            "content_hashes": None,
            "error": _redact(f"Skill 列表不可读：{exc.message}", secrets),
        }
    if not isinstance(payload, list):
        return {
            "status": UNKNOWN,
            "discovered": [],
            "missing": expected,
            "content_hashes": None,
            "error": "Skill 列表返回结构无法识别",
        }
    content_hashes: dict[str, str | None] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if not name:
            continue
        content_hashes[name] = skill_content_hash(item.get("content"))
    discovered = sorted(content_hashes)
    missing = [name for name in expected if name not in content_hashes]
    if missing:
        return {
            "status": UNHEALTHY,
            "discovered": discovered,
            "missing": missing,
            "content_hashes": content_hashes,
            "error": "Skill 未发现：" + "、".join(missing),
        }
    return {
        "status": HEALTHY,
        "discovered": discovered,
        "missing": [],
        "content_hashes": content_hashes,
        "error": None,
    }


def _inspect_repository(workspace: str, secrets: set[str]) -> dict[str, Any]:
    def result(
        status: str,
        *,
        url: str | None = None,
        branch: str | None = None,
        commit: str | None = None,
        dirty: bool | None = None,
        error: str | None = None,
        authentication_verified: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "url": url,
            "branch": branch,
            "commit": commit,
            "dirty": dirty,
            "authentication_verified": authentication_verified,
            "verification_scope": "refs:read",
            "error": error,
        }

    root = Path(workspace) if workspace else None
    if root is None or not root.is_dir():
        return result(UNKNOWN, error="工作区不存在，未检测到 Git 仓库")
    inside = _git(workspace, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != "true":
        return result(UNKNOWN, error="未检测到 Git 仓库")
    toplevel = _git(workspace, "rev-parse", "--show-toplevel")
    try:
        same_root = (
            toplevel is not None
            and Path(toplevel.strip()).resolve() == Path(workspace).resolve()
        )
    except OSError:
        same_root = False
    if not same_root:
        return result(UNHEALTHY, error="工作区不是 Git 仓库根目录")
    url = _git(workspace, "config", "--get", "remote.origin.url")
    branch = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    commit = _git(workspace, "rev-parse", "HEAD")
    changes = _git(workspace, "status", "--porcelain")
    if branch is None or commit is None or changes is None:
        return result(
            UNHEALTHY,
            url=_sanitize_url(url) if url else None,
            branch=branch.strip() if branch else None,
            commit=commit.strip() if commit else None,
            error="Git 只读探测失败",
        )
    if not url or not url.strip():
        return result(
            UNHEALTHY,
            branch=branch.strip(),
            commit=commit.strip(),
            dirty=bool(changes.strip()),
            error="Git 仓库缺少 remote.origin.url",
        )
    authenticated, auth_error = _verify_remote_read(
        workspace, branch.strip(), secrets
    )
    return result(
        HEALTHY,
        url=_sanitize_url(url),
        branch=branch.strip(),
        commit=commit.strip(),
        dirty=bool(changes.strip()),
        error=auth_error,
        authentication_verified=authenticated,
    )


def _verify_remote_read(
    workspace: str, branch: str, secrets: set[str]
) -> tuple[bool | None, str | None]:
    """Read-only refs check. Never fetches, never proves push access."""

    if not branch:
        return None, "无法确定当前分支，未验证远端读取"
    env = child_environ(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    command = [
        "git",
        "-C",
        workspace,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        "-c",
        "credential.helper=",
        "ls-remote",
        "--exit-code",
        "origin",
        f"refs/heads/{branch}",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "Git 远端读取验证超时或无法启动"
    if completed.returncode == 0:
        return True, None
    if completed.returncode == 2:
        return False, "远端不存在目标分支或无法读取引用"
    return False, "Git 远端认证或读取失败（仅验证读取，不代表 push 权限）"


def _git(workspace: str, *args: str) -> str | None:
    command = [
        "git",
        "-C",
        workspace,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        *args,
    ]
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _get_json(
    url: str,
    params: dict[str, str] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> Any:
    target = url
    if params:
        target += ("&" if "?" in target else "?") + urllib.parse.urlencode(params)
    request = urllib.request.Request(target, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read(_MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise _ProbeError(f"HTTP {exc.code}", http_status=exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _ProbeError(f"请求失败（{exc.__class__.__name__}）") from None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _ProbeError("响应不是有效 JSON") from None


def _read_local_config() -> dict[str, Any]:
    path = _local_config_path()
    if path is None or not path.is_file():
        return {}
    try:
        payload = loads_jsonc(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _local_config_path() -> Path | None:
    """Worker wheel 只带 worker_agent，不能依赖控制端的 app.runtime。"""

    from app.worker_agent.channel_switch import global_config_path

    return global_config_path()


def _read_effective_config(base: str, workspace: str) -> dict[str, Any] | None:
    if not base:
        return None
    try:
        payload = _get_json(base + "/config", {"directory": workspace} if workspace else None)
    except _ProbeError:
        return None
    return payload if isinstance(payload, dict) else None


def _provider_options(config: Any, provider: str) -> dict[str, Any] | None:
    if not provider or not isinstance(config, dict):
        return None
    providers = config.get("provider")
    if not isinstance(providers, dict):
        return None
    entry = providers.get(provider)
    if not isinstance(entry, dict):
        return None
    options = entry.get("options")
    return options if isinstance(options, dict) else None


def _credential_options(
    effective: dict[str, Any], local: dict[str, Any], provider: str
) -> dict[str, Any] | None:
    """有效配置优先；缺失地址/凭据时只从本地文件补凭据。"""

    options = _provider_options(effective, provider)
    if options is not None and _text(options.get("baseURL")) and _text(options.get("apiKey")):
        return options
    fallback = _provider_options(local, provider)
    if fallback is None:
        return options
    merged = dict(fallback)
    if options:
        merged.update(options)
    return merged


def _secrets(config: Any) -> set[str]:
    found: set[str] = set()
    if not isinstance(config, dict):
        return found
    providers = config.get("provider")
    if isinstance(providers, dict):
        for entry in providers.values():
            if not isinstance(entry, dict):
                continue
            options = entry.get("options")
            if not isinstance(options, dict):
                continue
            for key, value in options.items():
                if isinstance(value, str) and _secret_key(str(key)) and len(value) >= 4:
                    found.add(value)
    mcp = config.get("mcp")
    if isinstance(mcp, dict):
        for entry in mcp.values():
            headers = entry.get("headers") if isinstance(entry, dict) else None
            if not isinstance(headers, dict):
                continue
            for key, value in headers.items():
                if isinstance(value, str) and _secret_key(str(key)) and len(value) >= 4:
                    found.add(value)
    return found


def _secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in ("key", "token", "secret", "password", "authorization"))


def _names(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ValueError("期望名称必须是数组")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("期望名称必须是字符串")
        name = value.strip()
        if not name or len(name) > _MAX_NAME_LENGTH or any(ord(char) < 32 for char in name):
            raise ValueError("期望名称无效")
        if name not in result:
            result.append(name)
    return result


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _version_key(value: str | None) -> str:
    return str(value or "").strip().lower().removeprefix("v")


def _sanitize_url(value: str) -> str:
    """去掉 userinfo、query 和 fragment，失败时返回空串。"""

    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        return re.sub(r"^[^/@\s]+@", "", text.split("?", 1)[0].split("#", 1)[0])
    try:
        parts = urllib.parse.urlsplit(text)
        if parts.scheme == "file":
            return urllib.parse.urlunsplit(("file", "", parts.path, "", ""))
        host = parts.hostname or ""
        if not host:
            return ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            port = ""
        return urllib.parse.urlunsplit((parts.scheme, host + port, parts.path, "", ""))
    except ValueError:
        return ""


def _redact(value: Any, secrets: set[str] | None = None) -> str:
    text = str(value or "")
    for secret in secrets or set():
        if secret:
            text = text.replace(secret, "[已隐藏]")
    text = _BEARER.sub("Bearer [已隐藏]", text)
    text = _KEY_VALUE.sub(lambda match: f"{match.group(1)}=[已隐藏]", text)
    text = _URL.sub(lambda match: _sanitize_url(match.group(0)) or "[已隐藏]", text)
    return text[-1500:]

"""A task-bound native protocol gateway, not a second OpenCode API or UI implementation."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path, PurePosixPath
from typing import Annotated, Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.routes import require_task
from app.config.settings import REPO_ROOT
from app.database import get_session
from app.domain import ExecutionMode, TaskStatus
from app.executors.errors import OpenCodeSessionNotFound
from app.models import RuntimeSession
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.runtime.session_connection import connect, resolve_environment, session_tree
from app.runtime.workspace_changes import collect_workspace_changes, workspace_diff
from app.runtime.workspace_files import (
    MAX_UPLOAD,
    check_request_path,
    same_workspace_path,
    upload,
    validate_path,
)
from app.schemas import ExecutionStopConfirm, RetrySubmission, TaskRead

router = APIRouter(prefix="/api/tasks/{task_id}")


class _Upstream:
    def __init__(self, status_code: int, content: bytes, headers: dict[str, str]) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self):
        return json.loads(self.content) if self.content else None
# Task Workspace capability model: a task-bound, read-only window onto the
# employee's OpenCode server plus the native conversation/reply writes below.
# GET-only paths must be non-mutating and must not create PTYs, processes or
# sessions; anything capable of executing work is denied here and only reachable
# through an explicit task action. `/pty/shells` only lists shells for the native
# Settings page, so it stays read-only while `/pty` (PTY lifecycle) is faked empty.
READ_PATHS = {
    # Server / workspace observation
    "/global/health",
    "/project",
    "/project/current",
    "/path",
    "/vcs",
    "/vcs/diff",
    "/vcs/diff/raw",
    "/file",
    "/file/content",
    "/file/status",
    "/find",
    "/find/file",
    "/find/symbol",
    "/lsp",
    "/formatter",
    "/experimental/resource",
    "/experimental/worktree",
    # Native Settings (read-only)
    "/global/config",
    "/config",
    "/config/providers",
    "/provider",
    "/provider/auth",
    "/agent",
    "/command",
    "/skill",
    "/mcp",
    "/api/reference",
    "/pty/shells",
    # Conversation observation
    "/session",
    "/session/status",
    "/permission",
    "/question",
    "/pty",
}
SESSION_READ = re.compile(r"^/session/([^/]+)(/(children|todo|diff|message)(/[^/]+)?)?$")
# OpenCode 1.18 的 v2 客户端用绝对路径 /api/...。只放行观察类 GET。
API_READ = re.compile(
    r"^/api/(?:health|provider|model|config|project|path|agent|command|skill|mcp|location|"
    r"global|file|find|vcs|lsp|formatter|experimental|permission|question|pty|session)(?:/.*)?$"
)
API_SESSION = re.compile(r"^/api/session/([^/]+)(/.*)?$")
API_SESSION_COLLECTIONS = {"active", "status"}
SESSION_WRITE = re.compile(
    r"^/session/([^/]+)/(prompt_async|message|abort|revert|unrevert|summarize|command)$"
)
REPLY = re.compile(r"^/(question|permission)/([^/]+)/(reply|reject)$")
# 原生 Workspace UI 直接按 Session 回答权限请求，与 /permission/{id}/reply 等价。
PERMISSION_REPLY = re.compile(r"^/session/([^/]+)/permissions/([^/]+)$")
OPENCODE_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
RETIRED_WORKSPACE_UI = "版本未知/界面已退役"
_SECRET_SEGMENTS = frozenset({"provider", "model", "config"})
# Exact names after '-' / '_' normalization. `key` stays exact so fields such as
# setCacheKey are left for the Workspace UI.
_EXACT_SECRET_FIELDS = frozenset({"key", "credentials", "bearer"})
# Whole-name matches after normalization. `setCacheKey` stays a normal option.
_COMPOUND_SECRET_FIELDS = frozenset({"private_key", "access_key"})
# Segment match (case-insensitive). `tokens` / `*_tokens` are usage counters.
_SECRET_WORDS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "cookie",
        "cookies",
        "apikey",
        "credentials",
        "bearer",
    }
)
_TOKEN_USAGE_PARTS = frozenset(
    {
        "count",
        "counts",
        "total",
        "input",
        "output",
        "prompt",
        "completion",
        "reasoning",
        "cache",
        "cached",
        "max",
        "limit",
        "used",
        "usage",
    }
)
_REDACTED = "[redacted]"
_REMOTE_SECRET_TTL_SECONDS = 30.0
# Config reads are only for redaction. Two serial control timeouts (60s) made
# the first diff hang; bound each read and run them together.
_SECRET_FETCH_TIMEOUT_SECONDS = 5.0
_DIFF_UNAVAILABLE = "OpenCode 不可用，暂时无法安全显示 Diff"
# Compact form after lowercasing and removing '-' / '_'. Exact match only, so
# tokens, inputtokens, setcachekey, cachekey, publickey, and authorizationurl stay.
_CONCAT_SECRET_FIELDS = frozenset(
    {
        "apikey",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "privatekey",
        "accesskey",
        "xapikey",
        "xauthtoken",
        "xgoogapikey",
        "authorization",
        "password",
        "passwd",
        "secret",
        "token",
        "cookie",
        "cookies",
        "credentials",
        "bearer",
    }
)
# (environment id, worker epoch, workspace) -> (monotonic time, secret values). Not logged.
_REMOTE_SECRET_CACHE: dict[tuple[int, int, str], tuple[float, frozenset[str]]] = {}
_CONFIG_FILES = (
    Path("opencode.json"),
    Path("opencode.jsonc"),
    Path(".opencode/opencode.json"),
    Path(".opencode/opencode.jsonc"),
)


def normalize_proxy_path(path: str) -> str:
    """Collapse duplicate and trailing slashes before allowlist and filter checks."""

    return "/" + "/".join(part for part in path.split("/") if part)


def _workspace_file_body(path: str) -> bool:
    """Proxied responses whose body is workspace diff or file content."""

    shape = path.removeprefix("/api") if path.startswith("/api/") else path
    if shape in {"/file/content", "/vcs/diff", "/vcs/diff/raw", "/find"}:
        return True
    return re.fullmatch(r"/session/[^/]+/diff(?:/.*)?", shape) is not None


def carries_provider_secrets(path: str) -> bool:
    return bool(_SECRET_SEGMENTS.intersection(normalize_proxy_path(path).split("/")))


def _normalize_field_name(name: str) -> str:
    """Lower-case and treat '-' / '_' / camelCase as the same field."""

    text = str(name).strip()
    chars: list[str] = []
    for index, char in enumerate(text):
        if char in "-_":
            if chars and chars[-1] != "_":
                chars.append("_")
            continue
        if char.isupper() and index and (text[index - 1].islower() or text[index - 1].isdigit()):
            chars.append("_")
        chars.append(char.lower())
    return "".join(chars).strip("_")


def _is_secret_field(name: str) -> bool:
    normalized = _normalize_field_name(name)
    if not normalized or normalized == "tokens" or normalized.endswith("_tokens"):
        return False
    parts = [part for part in normalized.split("_") if part]
    if "token" in parts and parts != ["token"] and _TOKEN_USAGE_PARTS.intersection(parts):
        return False
    if normalized.replace("_", "") in _CONCAT_SECRET_FIELDS:
        return True
    if normalized in _EXACT_SECRET_FIELDS or normalized in _COMPOUND_SECRET_FIELDS:
        return True
    if "api" in parts:
        for index, part in enumerate(parts[:-1]):
            if part == "api" and parts[index + 1] == "key":
                return True
    if "authorization" in parts and "url" not in parts:
        return True
    return any(part in _SECRET_WORDS for part in parts)


def _collect_configured_secrets(value: Any, found: set[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_configured_secrets(item, found)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if _is_secret_field(key) and isinstance(item, str) and len(item) >= 8:
            found.add(item)
            continue
        _collect_configured_secrets(item, found)


def configured_secret_values(workspace: str) -> set[str]:
    """Secret values written in this workspace's OpenCode config. Missing files add nothing."""

    from app.worker_agent.jsonc import loads_jsonc

    found: set[str] = set()
    root = Path(workspace)
    for name in _CONFIG_FILES:
        path = root / name
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
                continue
            data = loads_jsonc(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        _collect_configured_secrets(data, found)
    return found


def _needs_remote_secrets(environment, workspace: str) -> bool:
    """Remote Worker files are not on this host. A missing directory is the same gap."""

    mode = getattr(environment, "mode", None)
    if mode is not None:
        value = mode.value if hasattr(mode, "value") else str(mode)
        return value != ExecutionMode.LOCAL.value
    try:
        return not Path(workspace).is_dir()
    except OSError:
        return True


def _platform_written_secrets() -> set[str]:
    """Secrets this platform writes into opencode.jsonc and still holds in config.

    The gateway provider apiKey is not stored in plaintext. OpenCode's merged
    `/config`, reached through the connected client, supplies that value.
    """

    from app.config.settings import get_settings

    found: set[str] = set()
    key = get_settings().mcp_api_key
    if isinstance(key, str) and len(key) >= 8:
        found.add(key)
    return found


def _secret_cache_workspace(workspace: str) -> str:
    """Lexical path identity. A trailing slash shares the directory's cache entry."""

    return str(PurePosixPath(str(workspace).replace("\\", "/")))


async def _secrets_from_opencode(client, workspace: str) -> tuple[set[str], bool] | None:
    """Read merged OpenCode config through the client already talking to the Worker.

    ``None`` means no read parsed. The bool is true only when merged ``/config``
    parsed. Values are not logged.
    """

    exchange = getattr(client, "exchange", None)
    if exchange is None:
        return set(), False
    found: set[str] = set()
    paths = ("/config", "/global/config")

    async def read(path: str):
        try:
            return await asyncio.wait_for(
                exchange("GET", path, params={"directory": workspace}),
                timeout=_SECRET_FETCH_TIMEOUT_SECONDS,
            )
        except Exception:
            return None

    config_parsed = False
    any_parsed = False
    results = await asyncio.gather(*(read(path) for path in paths))
    for path, result in zip(paths, results, strict=True):
        if result is None:
            continue
        status, content, _headers = result
        if isinstance(content, str):
            content = content.encode()
        if not isinstance(content, (bytes, bytearray)) or status >= 400 or len(content) > 1_000_000:
            continue
        try:
            data = json.loads(content)
        except (UnicodeError, ValueError):
            continue
        any_parsed = True
        if path == "/config":
            config_parsed = True
        _collect_configured_secrets(data, found)
    if not any_parsed:
        return None
    # The gateway key is in merged /config. /global/config alone does not make it known.
    return found, config_parsed


async def _worker_epoch(environment) -> int:
    """Current Worker epoch for this environment. Missing rows stay at 0."""

    explicit = getattr(environment, "worker_epoch", None)
    if isinstance(explicit, int):
        return explicit
    environment_id = getattr(environment, "id", None)
    if not isinstance(environment_id, int):
        return 0
    try:
        from sqlalchemy import select

        from app.database import SessionLocal
        from app.models import WorkerInstance

        async with SessionLocal() as session:
            epoch = await session.scalar(
                select(WorkerInstance.epoch).where(WorkerInstance.environment_id == environment_id)
            )
    except Exception:
        return 0
    return int(epoch or 0)


def clear_proxy_secret_cache() -> None:
    _REMOTE_SECRET_CACHE.clear()


async def _remote_secret_cache_key(environment, workspace: str) -> tuple[int, int, str] | None:
    environment_id = getattr(environment, "id", None)
    if not isinstance(environment_id, int):
        return None
    epoch = await _worker_epoch(environment)
    return int(environment_id), epoch, _secret_cache_workspace(workspace)


def _cached_remote_secrets(key: tuple[int, int, str] | None) -> set[str]:
    if key is None:
        return set()
    cached = _REMOTE_SECRET_CACHE.get(key)
    if cached is None:
        return set()
    return set(cached[1])


async def _remote_configured_secrets(environment, client, workspace: str) -> tuple[set[str], bool]:
    """Worker config values, reused per environment, epoch, and workspace for 30 seconds.

    Known requires a parsed merged ``/config``, or a cache entry from one.
    ``/global/config`` alone is not stored and is not known on a cold cache.
    A later miss keeps a previous known entry and refreshes its timestamp.
    """

    key = await _remote_secret_cache_key(environment, workspace)
    now = time.monotonic()
    cached = _REMOTE_SECRET_CACHE.get(key) if key is not None else None
    if cached is not None and now - cached[0] < _REMOTE_SECRET_TTL_SECONDS:
        return set(cached[1]), True
    try:
        fetched = await _secrets_from_opencode(client, workspace)
    except Exception:
        fetched = None
    if fetched is None:
        if cached is not None:
            return set(cached[1]), True
        return set(), False
    values, config_parsed = fetched
    if cached is not None:
        values = set(cached[1]) | values
    if config_parsed or cached is not None:
        if key is not None:
            _REMOTE_SECRET_CACHE[key] = (now, frozenset(values))
        return set(values), True
    return set(values), False


async def _load_proxy_secrets(workspace: str, environment=None, client=None) -> tuple[set[str], bool]:
    """Local config files, plus remote Worker config when this host does not have them.

    For a remote environment the bool is false when no secret values are known.
    """

    try:
        found = configured_secret_values(workspace)
        if not _needs_remote_secrets(environment, workspace):
            return found, True
        found |= _platform_written_secrets()
        remote, known = await _remote_configured_secrets(environment, client, workspace)
        return found | remote, known
    except Exception:
        found = configured_secret_values(workspace)
        if not _needs_remote_secrets(environment, workspace):
            return found, True
        key = await _remote_secret_cache_key(environment, workspace)
        if key is not None and key in _REMOTE_SECRET_CACHE:
            return found | _cached_remote_secrets(key), True
        return found, False


async def load_proxy_secrets(workspace: str, environment=None, client=None) -> set[str]:
    values, _known = await _load_proxy_secrets(workspace, environment, client)
    return values


async def _diff_proxy_secrets(environment, workspace: str) -> tuple[set[str], bool]:
    """Secrets for a platform-built diff.

    ``start=False`` skips OpenCode health so a dead server still reaches the
    cache. Remote diffs stay withheld when that cache is empty and the fetch fails.
    """

    if not _needs_remote_secrets(environment, workspace):
        return await load_proxy_secrets(workspace, environment), True
    try:
        async with connect(environment, start=False) as client:
            return await _load_proxy_secrets(workspace, environment, client)
    except Exception:
        found = configured_secret_values(workspace)
        key = await _remote_secret_cache_key(environment, workspace)
        if key is not None and key in _REMOTE_SECRET_CACHE:
            return found | _cached_remote_secrets(key), True
        return found, False


def redact_configured_text(text: str, configured: set[str] | None = None) -> str:
    secrets = [item for item in (configured or set()) if isinstance(item, str) and len(item) >= 8]
    for secret in sorted(secrets, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, _REDACTED)
    return text


def redact_secret_fields(value: Any, configured: set[str] | None = None) -> Any:
    """Drop secret-named fields, and replace configured secret text inside every string."""

    secrets = configured or set()
    if isinstance(value, str):
        return redact_configured_text(value, secrets)

    def walk(node: Any) -> None:
        if isinstance(node, list):
            kept: list[Any] = []
            for item in node:
                if isinstance(item, str):
                    if len(item) >= 8 and item in secrets:
                        continue
                    kept.append(redact_configured_text(item, secrets))
                    continue
                walk(item)
                kept.append(item)
            node[:] = kept
            return
        if not isinstance(node, dict):
            return
        for key in [name for name in node if _is_secret_field(name)]:
            node.pop(key, None)
        for key, item in list(node.items()):
            if isinstance(item, str):
                node[key] = redact_configured_text(item, secrets)
                continue
            walk(item)

    walk(value)
    return value


def redact_upstream_body(content: bytes, configured: set[str] | None = None) -> bytes:
    """Redact JSON error bodies in place, and configured values inside other text."""

    if not content:
        return content
    secrets = configured or set()
    try:
        data = json.loads(content)
    except (UnicodeError, ValueError):
        data = None
    if isinstance(data, (dict, list)):
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        redact_secret_fields(data, secrets)
        if json.dumps(data, ensure_ascii=False, sort_keys=True) == before:
            return content
        return json.dumps(data, ensure_ascii=False).encode()
    if isinstance(data, str):
        replaced = redact_configured_text(data, secrets)
        if replaced == data:
            return content
        return json.dumps(replaced, ensure_ascii=False).encode()
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        return content
    replaced = redact_configured_text(text, secrets)
    if replaced == text:
        return content
    return replaced.encode()


def _api_route_missing(status: int, headers: dict[str, str]) -> bool:
    """v1 OpenCode serves its SPA document for an unknown /api path."""

    if status == 404:
        return True
    media = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    return status == 200 and media == "text/html"


def _scope_session_list(data, allowed: set[str]):
    """v1 is an array. v2 is {data, cursor}; keep the envelope and filter data."""

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict) and item.get("id") in allowed]
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        visible = [item for item in data["data"] if isinstance(item, dict) and item.get("id") in allowed]
        return {**data, "data": visible}
    return data


def _scope_session_active(data, allowed: set[str]):
    """v2 wraps the status map in data. A bare map is filtered the same way."""

    if not isinstance(data, dict):
        return data
    if isinstance(data.get("data"), dict):
        return {**data, "data": {key: value for key, value in data["data"].items() if key in allowed}}
    return {key: value for key, value in data.items() if key in allowed}


def workspace_ui_bundle(version: str) -> Path:
    root = REPO_ROOT / "services" / "nexus-api" / "web" / "public" / "opencode-workspace"
    return root / version / "index.html"


_present_bundles: set[str] = set()


def bundle_installed(version: str) -> bool:
    """已安装的版本包不变，命中后不再查磁盘。缺失不缓存，装上后下一次就能看见。"""

    if version in _present_bundles:
        return True
    if workspace_ui_bundle(version).is_file():
        _present_bundles.add(version)
        return True
    return False


def workspace_ui_for(snapshot: dict | None) -> tuple[str, str | None, str | None]:
    """Select the UI for the version already stored on the session.

    Missing or unreadable versions stay unknown. A recorded version is never
    replaced with the current release; a retired bundle is reported as such.
    """
    if (snapshot or {}).get("runtime_engine") == "dsh":
        return "", None, None
    raw = (snapshot or {}).get("opencode_version")
    if raw is None or not str(raw).strip():
        return "unknown", None, RETIRED_WORKSPACE_UI
    version = str(raw).strip().removeprefix("v")
    if not OPENCODE_VERSION.fullmatch(version):
        return version or "unknown", None, RETIRED_WORKSPACE_UI
    if not bundle_installed(version):
        return version, None, f"{RETIRED_WORKSPACE_UI}（{version}）"
    return version, f"/opencode-workspace/{version}/index.html", None


async def runtime_worker_online(session, snapshot: dict | None) -> bool | None:
    """Worker 通道上的在线事实。非 Worker 通道不在这里判断。"""

    if not isinstance(snapshot, dict):
        return None
    environment_id = snapshot.get("environment_id")
    if environment_id is None:
        return None
    from app.models import OpenCodeExecutionEnvironment, WorkerInstance

    stored = await session.get(OpenCodeExecutionEnvironment, environment_id)
    if stored is None or stored.worker_channel != "worker":
        return None
    instance = await session.scalar(
        select(WorkerInstance).where(WorkerInstance.environment_id == stored.id)
    )
    return bool(instance is not None and instance.status == "online")


def error_response(exc):
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, OpenCodeSessionNotFound):
        return HTTPException(404, "Session 不存在；请显式选择恢复或新建 Session")
    if isinstance(exc, PermissionError):
        return HTTPException(403, str(exc))
    if isinstance(exc, (ValueError, RuntimeStateError)):
        return HTTPException(409, str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(409, f"Workspace 不存在：{exc}")
    return HTTPException(503, f"当前执行 Runtime 不可连接：{exc}")


@router.post("/start", response_model=TaskRead)
async def start_task(task_id: str, session=Depends(get_session)):
    await require_task(task_id, session)
    try:
        return await RuntimeService(session).start(task_id)
    except Exception as exc:
        raise error_response(exc) from exc


@router.post("/restart", response_model=TaskRead)
async def restart_task(task_id: str, session=Depends(get_session)):
    await require_task(task_id, session)
    try:
        return await RuntimeService(session).start(task_id, restart=True)
    except Exception as exc:
        raise error_response(exc) from exc


@router.post("/pause", response_model=TaskRead)
async def pause_task(task_id: str, session=Depends(get_session)):
    task = await require_task(task_id, session)
    if task.status != TaskStatus.RUNNING.value:
        raise HTTPException(409, "只有执行中的任务可以暂停")
    try:
        return await RuntimeService(session).stop(task_id, cancel_task=False)
    except Exception as exc:
        raise error_response(exc) from exc


@router.post("/resume", response_model=TaskRead)
async def resume_task(task_id: str, session=Depends(get_session)):
    task = await require_task(task_id, session)
    if task.status != TaskStatus.WAITING.value:
        raise HTTPException(409, "只有等待中的任务可以恢复")
    try:
        if task.current_session_id:
            return await RuntimeService(session).continue_task(task_id)
        return await RuntimeService(session).start(task_id)
    except Exception as exc:
        raise error_response(exc) from exc


@router.post("/stop", response_model=TaskRead)
async def stop_task(task_id: str, session=Depends(get_session)):
    await require_task(task_id, session)
    try:
        return await RuntimeService(session).stop(task_id)
    except Exception as exc:
        raise error_response(exc) from exc


def execution_stop_actor(
    user_id: Annotated[str, Header(alias="X-Astra-User-Id")] = "",
) -> str:
    return user_id


@router.post("/confirm-execution-stop", response_model=TaskRead)
async def confirm_execution_stop(
    task_id: str,
    body: ExecutionStopConfirm,
    session=Depends(get_session),
    user_id: str = Depends(execution_stop_actor),
):
    await require_task(task_id, session)
    try:
        return await RuntimeService(session).confirm_execution_stop(
            task_id, operator=user_id, reason=body.reason
        )
    except Exception as exc:
        raise error_response(exc) from exc


@router.post("/reconnect", response_model=TaskRead)
async def reconnect_task(task_id: str, session=Depends(get_session)):
    await require_task(task_id, session)
    try:
        return await RuntimeService(session).refresh(task_id)
    except OpenCodeSessionNotFound as exc:
        # refresh() already records SESSION_MISSING; do not overwrite it with a
        # transport-unknown marker.
        raise error_response(exc) from exc
    except Exception as exc:
        await session.rollback()
        await RuntimeService(session).disconnected(task_id, exc)
        raise error_response(exc) from exc


@router.get("/workspace")
async def task_workspace(task_id: str, session_id: str | None = None, session=Depends(get_session)):
    task = await require_task(task_id, session)
    bindings = (
        await session.scalars(
            select(RuntimeSession)
            .where(
                RuntimeSession.task_id == task.id,
            )
            .order_by(RuntimeSession.created_at.desc())
        )
    ).all()
    selected = next((item for item in bindings if item.id == (session_id or task.current_session_id)), None)
    if session_id and not selected:
        raise HTTPException(404, "Session 不属于此任务")

    selected_snapshot = selected.environment if selected else task.execution_environment_snapshot
    selected_version, selected_ui, selected_ui_error = workspace_ui_for(selected_snapshot)
    worker_online = await runtime_worker_online(session, selected_snapshot)

    def session_item(item: RuntimeSession):
        version, workspace_ui, workspace_ui_error = workspace_ui_for(item.environment)
        return {
            "id": item.id,
            "runtime_engine": item.runtime_engine,
            "native_session_id": item.native_session_id,
            "engine_metadata": item.engine_metadata,
            "workspace": item.workspace,
            "created_at": item.created_at,
            "status": item.status,
            "superseded_by": item.superseded_by,
            "finished_at": item.finished_at,
            "current": item.id == task.current_session_id,
            "opencode_version": version,
            "workspace_ui": workspace_ui,
            "workspace_ui_error": workspace_ui_error,
        }

    return {
        "task_id": task.id,
        "task_status": task.status,
        "runtime": {
            "runtime_engine": (selected.runtime_engine if selected
                               else (selected_snapshot or {}).get("runtime_engine", "opencode")),
            "native_session_id": selected.native_session_id if selected else None,
            "engine_metadata": selected.engine_metadata if selected else None,
            "server": f"/api/tasks/{task.id}/opencode",
            "session_id": selected.id if selected else None,
            "workspace": selected.workspace if selected else task.workspace,
            "connection": task.runtime_connection,
            "error": task.runtime_error,
            "opencode_version": selected_version,
            "workspace_ui": selected_ui,
            "workspace_ui_error": selected_ui_error,
            "worker_online": worker_online,
        },
        "sessions": [session_item(item) for item in bindings],
    }


@router.post("/files/{filename}")
async def upload_file(task_id: str, filename: str, request: Request, session=Depends(get_session)):
    task = await require_task(task_id, session)
    if not task.workspace:
        raise HTTPException(409, "任务尚未准备 Workspace")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_UPLOAD:
            raise HTTPException(413, "文件不能超过 20 MB")
    try:
        environment = await resolve_environment(session, task)
        await session.commit()
        path = await upload(environment, task.workspace, filename, bytes(content))
    except Exception as exc:
        raise error_response(exc) from exc
    return {"path": path}


def event_session(event):
    props = event.get("properties", {})
    info = props.get("info", {})
    return (
        props.get("sessionID")
        or props.get("part", {}).get("sessionID")
        or info.get("sessionID")
        or (info.get("id") if event.get("type", "").startswith("session.") else None)
    )


def _sse_data(value, secrets: set[str]) -> str:
    redacted = redact_secret_fields(value, secrets)
    return f"data: {json.dumps(redacted, ensure_ascii=False)}\n\n"


async def native_events(environment, workspace, root, global_stream):
    async with connect(environment, start=True) as client:
        allowed = await session_tree(client, Path(workspace), root)
        secrets = await load_proxy_secrets(str(workspace), environment, client)
        if getattr(client, "records_fetch", False):
            stream = client.events(Path(workspace), consumer=f"browser:{uuid4().hex}")
        else:
            stream = client.events(Path(workspace))
        async for event in stream:
            props = event.get("properties", {})
            info = props.get("info", {})
            if event.get("type") == "session.created" and info.get("parentID") in allowed:
                allowed.add(info["id"])
            if event.get("type") == "astra.keepalive":
                yield ": keepalive\n\n"
                continue
            if event.get("type") == "astra.event_gap":
                value = {"directory": workspace, "payload": event} if global_stream else event
                yield _sse_data(value, secrets)
                continue
            sid = event_session(event)
            if sid and sid not in allowed:
                continue
            if not sid and event.get("type") not in {
                "server.connected",
                "server.heartbeat",
                "file.edited",
                "file.watcher.updated",
                "vcs.branch.updated",
                "lsp.updated",
                "instance.disposed",
            }:
                continue
            value = {"directory": workspace, "payload": event} if global_stream else event
            yield _sse_data(value, secrets)


async def persist_attachments(body, environment, workspace):
    for part in body.get("parts", []):
        if part.get("type") != "file":
            continue
        url = part.get("url", "")
        if url.startswith("data:"):
            header, encoded = url.split(",", 1)
            if ";base64" not in header or len(encoded) > MAX_UPLOAD * 4 // 3 + 8:
                raise ValueError("附件格式或大小无效")
            path = await upload(
                environment,
                workspace,
                part.get("filename") or "attachment",
                base64.b64decode(encoded, validate=True),
            )
            part["url"] = (
                Path(path).as_uri()
                if environment.mode == ExecutionMode.LOCAL
                else PurePosixPath(path).as_uri()
            )
        elif url.startswith("file:"):
            parsed = urlparse(url)
            if parsed.netloc:
                raise PermissionError("不允许外部文件地址")
            value = unquote(parsed.path)
            if environment.mode == ExecutionMode.LOCAL:
                value = url2pathname(value)
            await validate_path(environment, workspace, value)
        else:
            raise PermissionError("附件必须写入任务 Workspace")
    return body


@router.api_route("/opencode/{session_id}/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def native_proxy(
    task_id: str, session_id: str, path: str, request: Request, session=Depends(get_session)
):
    task = await require_task(task_id, session)
    binding = await session.get(RuntimeSession, session_id)
    if not binding or binding.task_id != task.id:
        raise HTTPException(404, "Session 不属于此任务")
    if binding.runtime_engine != "opencode":
        raise HTTPException(409, "此 Session 使用 DSH，请使用通用工作台")
    # Environment and directory are bound in the DB, never selected by browser-supplied paths.
    from app.models import OpenCodeExecutionEnvironment
    from app.runtime.environment import environment_from_snapshot

    environment = environment_from_snapshot(binding.environment, binding.employee_id)
    stored = await session.get(OpenCodeExecutionEnvironment, binding.environment["environment_id"])
    if stored:
        environment.ssh_password = stored.ssh_password
        environment.worker_channel = stored.worker_channel
    workspace = binding.workspace
    # Release the read transaction before path validation, SSH/OpenCode startup,
    # SSE or upstream HTTP. Native Workspace requests can be long-lived.
    await session.commit()
    if "workspace" in request.query_params or "x-opencode-workspace" in request.headers:
        raise HTTPException(403, "不允许覆盖任务绑定")
    for key, value in request.query_params.multi_items():
        if key.startswith("location") and (
            key != "location[directory]"
            or not same_workspace_path(environment, workspace, unquote(value))
        ):
            raise HTTPException(403, "不允许覆盖任务绑定")
    requested = request.query_params.get("directory") or request.headers.get("x-opencode-directory")
    requested_path = unquote(requested) if requested else None
    native_path = normalize_proxy_path(path)
    directory = workspace
    if requested_path and not same_workspace_path(environment, workspace, requested_path):
        if request.method != "GET" or native_path not in {"/vcs/diff", "/vcs/diff/raw"}:
            raise HTTPException(403, "Workspace 不属于此任务")
        try:
            await validate_path(environment, workspace, requested_path)
        except Exception as exc:
            raise error_response(exc) from exc
        directory = requested_path
    method = request.method
    if method == "GET" and native_path in {"/vcs/diff", "/vcs/diff/raw"}:
        try:
            # `git` is the native uncommitted view; the explicit task view also
            # includes commits since this workspace's first Session baseline.
            # This path reads Git itself and must withhold the diff when remote
            # secrets are unknown. Other file-body routes use the same gate below.
            mode = request.query_params.get("mode", "task")
            if mode not in {"git", "task"}:
                raise ValueError("不支持的 Diff 模式")
            secrets, known = await _diff_proxy_secrets(environment, workspace)
            if not known:
                raise HTTPException(503, _DIFF_UNAVAILABLE)
            diffs = await workspace_diff(
                session, task, cumulative=mode == "task", directory=directory,
                environment=environment, workspace=workspace,
            )
            if native_path.endswith("/raw"):
                raw = redact_configured_text("".join(item["patch"] for item in diffs), secrets)
                return Response(raw, media_type="text/x-diff")
            return redact_secret_fields(diffs, secrets)
        except Exception as exc:
            raise error_response(exc) from exc
    match = SESSION_READ.fullmatch(native_path) if method == "GET" else SESSION_WRITE.fullmatch(native_path)
    reply = REPLY.fullmatch(native_path) if method == "POST" else None
    permission_reply = PERMISSION_REPLY.fullmatch(native_path) if method == "POST" else None
    if permission_reply and permission_reply[1] != session_id:
        raise HTTPException(403, "历史 Session 只读")
    pending = (reply[1], reply[2]) if reply else (
        ("permission", permission_reply[2]) if permission_reply else None
    )
    if method != "GET" and task.status in {"SUCCEEDED", "CANCELED"}:
        raise HTTPException(409, "任务已结束；请显式重新开始")
    if method != "GET" and session_id != task.current_session_id:
        raise HTTPException(403, "历史 Session 只读")
    if method == "GET" and native_path in {"/global/event", "/event"}:
        await session.commit()
        return StreamingResponse(
            native_events(environment, workspace, session_id, native_path == "/global/event"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    api_read = method == "GET" and API_READ.fullmatch(native_path) is not None
    if not match and not pending and not (method == "GET" and native_path in READ_PATHS) and not api_read:
        raise HTTPException(403, "此接口不属于 Task Workspace 的权限范围")
    gated: tuple[set[str], bool] | None = None
    if _workspace_file_body(native_path) and _needs_remote_secrets(environment, workspace):
        # Decide before health/connect/session_tree: a stopped OpenCode would
        # otherwise hold the request for the full Worker exchange timeout.
        gated = await _diff_proxy_secrets(environment, workspace)
        if not gated[1]:
            raise HTTPException(503, _DIFF_UNAVAILABLE)
    try:
        await check_request_path(
            environment, workspace, request.query_params.get("path", "."), native_path=native_path
        )
        async with connect(environment, start=True) as client:
            allowed = await session_tree(client, Path(workspace), session_id)
            # Global reads such as /session/status are not per-session resources;
            # their payloads are filtered to `allowed` below instead of rejected here.
            if match and native_path not in READ_PATHS and match[1] not in allowed:
                raise PermissionError("Session 不属于此任务")
            api_session = API_SESSION.fullmatch(native_path)
            if (
                api_session
                and api_session[1] not in API_SESSION_COLLECTIONS
                and api_session[1] not in allowed
            ):
                raise PermissionError("Session 不属于此任务")
            if method != "GET" and match and match[1] != session_id:
                raise PermissionError("子 Session 只读")
            body = json.loads(await request.body() or b"{}") if method != "GET" else None
            if body and {"directory", "sessionID", "workspace", "location"}.intersection(body):
                raise PermissionError("不允许覆盖任务绑定")
            if pending:
                items = await client.request(
                    "GET",
                    f"/{pending[0]}",
                    directory=Path(workspace),
                    session_id=session_id,
                    task_id=task.id,
                )
                if not any(item["id"] == pending[1] and item.get("sessionID") in allowed for item in items):
                    raise PermissionError("请求不属于此任务 Session")
            if native_path.endswith(("/prompt_async", "/message", "/command")) and method == "POST":
                await persist_attachments(body, environment, workspace)
                await RuntimeService(session).send(
                    task.id,
                    body,
                    expected_session_id=session_id,
                    native_action="command" if native_path.endswith("/command") else "prompt_async",
                )
                return Response(status_code=204)
            if native_path.endswith("/abort") and method == "POST":
                await RuntimeService(session).stop(task.id, cancel_task=False)
                return True
            if native_path == "/pty":
                return []  # Arbitrary terminals are outside the task-scoped API boundary.
            params = dict(request.query_params)
            params["directory"] = directory
            # v2 resolves /api/model and friends from location[directory]. Always pin it
            # to this task; a missing value follows OpenCode's last directory.
            if native_path.startswith("/api/") or "location[directory]" in params:
                params["location[directory]"] = workspace
            for key in ("sessionID", "sessionId", "session_id"):
                if key in params and params[key] not in allowed:
                    raise PermissionError("Session 不属于此任务")
            forward_path = "/project/current" if native_path == "/project" else native_path
            status, content, headers = await client.exchange(
                method,
                forward_path,
                params=params,
                body=body,
                session_id=session_id,
                task_id=task.id,
            )
            if forward_path.startswith("/api/") and _api_route_missing(status, headers):
                # v1 OpenCode answers an unknown /api path with the SPA document (200
                # text/html) or 404. Retry the unprefixed route once.
                alternate = forward_path.removeprefix("/api") or "/"
                status, content, headers = await client.exchange(
                    method,
                    alternate,
                    params=params,
                    body=body,
                    session_id=session_id,
                    task_id=task.id,
                )
            upstream = _Upstream(status, content, headers)
            # Reuse the gate's read so one request never pays the config timeout twice.
            secrets, known = gated or await _load_proxy_secrets(workspace, environment, client)
            if (
                not known
                and _needs_remote_secrets(environment, workspace)
                and _workspace_file_body(native_path)
            ):
                raise HTTPException(503, _DIFF_UNAVAILABLE)
            if upstream.is_error:
                return Response(
                    content=redact_upstream_body(upstream.content, secrets),
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"),
                )
            data = upstream.json() if upstream.content else None
            data = redact_secret_fields(data, secrets)
            shape = native_path.removeprefix("/api") if native_path.startswith("/api/") else native_path
            if shape == "/session":
                data = _scope_session_list(data, allowed)
            elif shape == "/session/active":
                data = _scope_session_active(data, allowed)
            elif shape == "/session/status":
                data = {key: value for key, value in data.items() if key in allowed}
            elif shape in {"/question", "/permission"}:
                data = [item for item in data if item.get("sessionID") in allowed]
            elif shape in {"/project", "/project/current"}:
                data = {**data, "worktree": workspace, "sandboxes": []}
                if shape == "/project":
                    data = [data]
            elif shape == "/path":
                data = {
                    **data,
                    "directory": workspace,
                    "worktree": workspace,
                    "home": workspace,
                    "config": workspace,
                    "state": workspace,
                }
            elif shape in {"/global/config", "/config"}:
                data = {
                    key: value
                    for key, value in data.items()
                    if key
                    in {
                        "snapshot",
                        "model",
                        "small_model",
                        "default_agent",
                        "username",
                        "enabled_providers",
                        "disabled_providers",
                    }
                }
                if environment.model_name:
                    data["model"] = environment.model_name
            elif shape in {"/provider", "/config/providers"} and isinstance(data, dict):
                for provider in data.get("all", data.get("providers", [])):
                    for key in ("key", "options", "headers"):
                        provider.pop(key, None)
                    for model in provider.get("models", {}).values():
                        model["options"] = {}
                        model["headers"] = {}
            elif shape == "/experimental/worktree":
                data = []
            if reply or method != "GET":
                await RuntimeService(session).refresh(task.id, client=client)
            return Response(
                content=json.dumps(data, ensure_ascii=False),
                media_type="application/json",
                headers={
                    key: value
                    for key, value in upstream.headers.items()
                    if key in {"link", "x-next-cursor", "x-has-more"}
                },
            )
    except Exception as exc:
        raise error_response(exc) from exc


@router.post("/retry-submission", response_model=TaskRead)
async def retry_submission(
    task_id: str,
    body: RetrySubmission | None = None,
    session=Depends(get_session),
):
    await require_task(task_id, session)
    try:
        return await RuntimeService(session).retry_submission(
            task_id, confirm_unknown=bool(body and body.confirm_unknown)
        )
    except Exception as exc:
        raise error_response(exc) from exc


@router.get("/repo")
async def task_repo(task_id: str, session=Depends(get_session)):
    import asyncio

    from app.domain import ExecutionMode

    task = await require_task(task_id, session)
    if not task.workspace:
        raise HTTPException(409, "Workspace 尚未准备")
    environment = await resolve_environment(session, task)
    await session.commit()
    try:
        await validate_path(environment, task.workspace, ".")
        from app.runtime.worker_files import WorkerFileError, environment_uses_worker, worker_git

        worker = await environment_uses_worker(environment)
        status_overflow = False

        async def git(*args):
            nonlocal status_overflow
            if worker:
                try:
                    code, output = await worker_git(environment, task.workspace, args)
                except WorkerFileError as exc:
                    if args[:1] == ("status",) and exc.code == "too_large":
                        status_overflow = True
                        return ""
                    raise
                return output.strip() if code == 0 else ""
            if environment.mode == ExecutionMode.LOCAL:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "-C",
                    task.workspace,
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                output, _ = await proc.communicate()
                return output.decode().strip() if proc.returncode == 0 else ""
            raise RuntimeError("远程环境尚未安装 Worker")

        commit = await git("rev-parse", "HEAD")
        branch = await git("symbolic-ref", "--quiet", "--short", "HEAD")
        changes = await git("status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all")
        origin = await git("config", "--get", "remote.origin.url")
        common_dir = await git("rev-parse", "--git-common-dir")
        if common_dir and not Path(common_dir).is_absolute():
            common_dir = str(Path(task.workspace) / common_dir)
        name = (
            origin.rstrip("/").split("/")[-1].removesuffix(".git")
            if origin
            else Path(common_dir).parent.name if common_dir else Path(task.workspace).name
        )
        task_changes = await collect_workspace_changes(session, task)
        return {
            "name": name,
            "branch": branch,
            "commit": commit,
            "dirty": status_overflow or bool(changes),
            "changes": sum(bool(entry) for entry in changes.split("\0")),
            "task_changes": task_changes,
        }
    except Exception as exc:
        raise error_response(exc) from exc

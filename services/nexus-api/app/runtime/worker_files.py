"""File, read-only Git, and upload calls for an environment marked worker.

The browser still uses the Workspace routes. These helpers only move the work
onto the Worker and never read the SSH password copied onto the environment.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.database import SessionLocal
from app.models import OpenCodeExecutionEnvironment
from app.runtime.worker_client import WorkerCommandError, WorkerOpenCodeClient
from app.worker_gateway.data_channel import DataChannel, request_key

_PATH_CODES = frozenset({"path_forbidden", "path_outside", "symlink_escape"})
_VALUE_CODES = frozenset(
    {"invalid_name", "too_large", "checksum_mismatch", "invalid_revision", "unsupported_operation"}
)


class WorkerPathError(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class WorkerFileError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def environment_uses_worker(environment) -> bool:
    """Live rows already carry the mark. Snapshots do not, so re-read that column only."""

    if getattr(environment, "worker_channel", None) == "worker":
        return True
    environment_id = getattr(environment, "id", None)
    if not isinstance(environment_id, int):
        return False
    async with SessionLocal() as session:
        channel = await session.scalar(
            select(OpenCodeExecutionEnvironment.worker_channel).where(
                OpenCodeExecutionEnvironment.id == environment_id
            )
        )
    return channel == "worker"


def git_command_payload(args: tuple[str, ...]) -> dict[str, Any]:
    """Map one existing read-only Git invocation onto the closed operation set."""

    if args == ("rev-parse", "HEAD"):
        return {"operation": "head"}
    if args == ("rev-parse", "--is-inside-work-tree"):
        return {"operation": "inside_work_tree"}
    if args == ("rev-parse", "--git-common-dir"):
        return {"operation": "git_common_dir"}
    if args == ("symbolic-ref", "--quiet", "--short", "HEAD"):
        return {"operation": "branch"}
    if args == ("status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all"):
        return {"operation": "status_porcelain"}
    if args == ("config", "--get", "remote.origin.url"):
        return {"operation": "origin_url"}
    if (
        len(args) == 4
        and args[:3] == ("rev-list", "--reverse", "--max-count=200")
        and args[3].endswith("..HEAD")
    ):
        return {"operation": "rev_list", "baseline": args[3].removesuffix("..HEAD")}
    if (
        len(args) == 5
        and args[:4] == ("diff", "--name-status", "--no-renames", "-z")
        and args[4].endswith("..HEAD")
    ):
        return {"operation": "diff_name_status_range", "baseline": args[4].removesuffix("..HEAD")}
    if (
        len(args) == 9
        and args[:6] == ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z")
        and args[7] == "--"
    ):
        return {"operation": "diff_numstat", "revision": args[6], "path": args[8]}
    if (
        len(args) == 6
        and args[:4] == ("ls-files", "--others", "--exclude-standard", "-z")
        and args[4] == "--"
    ):
        return {"operation": "ls_files_others", "path": args[5]}
    if len(args) == 2 and args[0] == "show" and ":" in args[1]:
        revision, file_name = args[1].split(":", 1)
        return {"operation": "show_file", "revision": revision, "file": file_name}
    if (
        len(args) == 8
        and args[:5] == ("diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames")
        and args[6] == "--"
    ):
        return {"operation": "diff_patch", "revision": args[5], "file": args[7]}
    if (
        len(args) == 7
        and args[:4] == ("diff", "--no-renames", "--name-status", "-z")
        and args[5] == "--"
    ):
        return {"operation": "diff_name_status_file", "revision": args[4], "file": args[6]}
    raise WorkerFileError("unsupported_operation", "不支持的 Git 操作")


async def worker_validate_path(environment, workspace: str, value: str) -> str:
    result = await _run(environment, "validate_path", {"workspace": workspace, "path": value})
    relative = result.get("relative")
    if not isinstance(relative, str):
        raise RuntimeError("路径校验没有返回相对路径")
    return relative


async def worker_read_text(environment, workspace: str, path: str) -> str:
    result = await _run(environment, "read_file", {"workspace": workspace, "path": path})
    if result.get("missing"):
        return ""
    return _text(result)


async def worker_git(environment, workspace: str, args: tuple[str, ...]) -> tuple[int, str]:
    payload = git_command_payload(args)
    payload["workspace"] = workspace
    result = await _run(environment, "git_readonly", payload)
    return int(result.get("code") or 0), _text(result)


async def worker_upload(environment, workspace: str, filename: str, content: bytes) -> str:
    client = WorkerOpenCodeClient(environment_id=environment.id)
    request_id = f"upl_{uuid4().hex}"
    payload = {
        "workspace": workspace,
        "filename": filename,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    channel = DataChannel(client.gateway)
    ttl = float(
        client.settings.worker_gateway_command_timeout_seconds
        + client.settings.worker_gateway_dispatch_lease_seconds
    )
    await channel.stage_upload(request_id, content, ttl)
    try:
        result = await client.run_command("upload_file", payload, request_id=request_id)
    finally:
        await client.gateway.bus.delete_text(request_key(request_id))
    path = result.get("path")
    if not isinstance(path, str) or not path:
        raise RuntimeError("上传没有返回路径")
    return path


async def _run(environment, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = WorkerOpenCodeClient(environment_id=environment.id)
    try:
        return await client.run_command(name, payload)
    except WorkerCommandError as exc:
        raise _mapped(exc) from exc


def _mapped(exc: WorkerCommandError) -> Exception:
    message = str(exc)
    if exc.code in _PATH_CODES:
        return WorkerPathError(exc.code, message)
    if exc.code in _VALUE_CODES:
        return WorkerFileError(exc.code, message)
    return RuntimeError(message)


def _text(result: dict[str, Any]) -> str:
    raw = result.get("body")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        decoded = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise RuntimeError("响应体无法按原始字节还原") from exc
    return decoded.decode("utf-8", errors="replace")

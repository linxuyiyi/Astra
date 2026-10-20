"""Task-scoped filesystem access for uploads and native file API requests."""

import asyncio
import base64
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from app.domain import ExecutionMode

MAX_UPLOAD = 20 * 1024 * 1024
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def _windows_style(value: str) -> bool:
    return bool(_WINDOWS_ABSOLUTE.match(value or "")) or "\\" in (value or "")


def same_workspace_path(environment, workspace: str, value: str) -> bool:
    """Compare task-bound paths using the target environment's path semantics."""

    if environment.mode == ExecutionMode.LOCAL:
        if _windows_style(workspace) or _windows_style(value):
            return PureWindowsPath(workspace) == PureWindowsPath(value)
        try:
            return Path(workspace).resolve(strict=False) == Path(value).resolve(strict=False)
        except OSError:
            return False
    return PurePosixPath(workspace) == PurePosixPath(value)


def relative_path(value: str, workspace: str, *, local: bool = False) -> str:
    value = value or "."
    if "\x00" in value or "\\" in value:
        # A backslash is a Windows path separator; treat it as an escape attempt on
        # every host so LOCAL classification cannot bypass the workspace boundary.
        raise PermissionError("不允许访问此路径")
    if local:
        root = Path(workspace).resolve(strict=True)
        candidate = Path(value)
        target = (
            candidate.resolve(strict=False)
            if candidate.is_absolute()
            else (root / candidate).resolve(strict=False)
        )
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise PermissionError("路径不属于此任务 Workspace") from exc
        if any(part.casefold() == ".git" for part in relative.parts):
            raise PermissionError("不允许访问此路径")
        return relative.as_posix()

    path = PurePosixPath(value)
    root = PurePosixPath(workspace)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as exc:
            raise PermissionError("路径不属于此任务 Workspace") from exc
    if ".." in path.parts or ".git" in path.parts:
        raise PermissionError("不允许访问此路径")
    return str(path)


_FILE_API = {"/file", "/file/content", "/file/status", "/find/file"}


async def check_request_path(environment, workspace: str, value: str, *, native_path: str = "") -> str:
    """Worker send and session calls stay lexical. File and Git routes still use SSH."""

    worker = getattr(environment, "worker_channel", "ssh") == "worker"
    if worker and native_path not in _FILE_API:
        return relative_path(value or ".", workspace, local=environment.mode == ExecutionMode.LOCAL)
    return await validate_path(environment, workspace, value)


async def validate_path(environment, workspace: str, value: str) -> str:
    from app.runtime.worker_files import environment_uses_worker, worker_validate_path

    if await environment_uses_worker(environment):
        relative_path(value, workspace, local=False)
        return await worker_validate_path(environment, workspace, value)
    local = environment.mode == ExecutionMode.LOCAL
    relative = relative_path(value, workspace, local=local)
    if local:
        root = Path(workspace).resolve(strict=True)
        target = (root / relative).resolve(strict=False)
        if not target.is_relative_to(root):
            raise PermissionError("符号链接指向任务 Workspace 之外")
        return relative
    raise RuntimeError("远程环境尚未安装 Worker")


async def upload(environment, workspace: str, filename: str, content: bytes) -> str:
    if (
        not filename
        or filename != PurePosixPath(filename).name
        or "\\" in filename
        or "\x00" in filename
    ):
        raise ValueError("文件名无效")
    if len(content) > MAX_UPLOAD:
        raise ValueError("文件不能超过 20 MB")
    from app.runtime.worker_files import environment_uses_worker, worker_upload

    if await environment_uses_worker(environment):
        return await worker_upload(environment, workspace, filename, content)
    relative = f"uploads/{uuid4().hex[:12]}-{filename}"
    if environment.mode == ExecutionMode.LOCAL:
        root = Path(workspace).resolve(strict=True)
        folder = root / "uploads"
        folder.mkdir(exist_ok=True)
        if folder.is_symlink() or not folder.resolve().is_relative_to(root):
            raise PermissionError("上传目录越界")
        target = folder / Path(relative).name

        def write():
            if os.name == "nt":
                # Windows has no O_DIRECTORY/O_NOFOLLOW/dir_fd equivalent.
                # The target name is random and the parent was checked above;
                # exclusive creation prevents overwrite/race with an existing file.
                with target.open("xb") as stream:
                    stream.write(content)
                return

            # Unix keeps the stronger no-follow + directory-fd boundary.
            directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fd = os.open(
                    target.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
            finally:
                os.close(directory)

        await asyncio.to_thread(write)
    else:
        raise RuntimeError("远程环境尚未安装 Worker")
    if environment.mode == ExecutionMode.LOCAL:
        return str(Path(workspace) / relative)
    return f"{workspace}/{relative}"


async def materialize_task_attachments(environment, workspace: str, attachments: list[dict]) -> list[dict]:
    """Write Task creation attachments into the task Workspace once."""

    materialized: list[dict] = []
    for attachment in attachments or []:
        item = dict(attachment)
        if item.get("workspace_path"):
            item.pop("content", None)
            materialized.append(item)
            continue
        raw = item.get("content")
        if not raw:
            materialized.append(item)
            continue
        if not isinstance(raw, str) or not raw.startswith("data:") or "," not in raw:
            raise ValueError(f"附件格式无效：{item.get('name') or 'attachment'}")
        header, encoded = raw.split(",", 1)
        if ";base64" not in header:
            raise ValueError(f"附件必须使用 base64：{item.get('name') or 'attachment'}")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError(f"附件内容无效：{item.get('name') or 'attachment'}") from exc
        path = await upload(
            environment,
            workspace,
            str(item.get("name") or "attachment"),
            content,
        )
        item["workspace_path"] = path
        item["size"] = len(content)
        item.pop("content", None)
        materialized.append(item)
    return materialized

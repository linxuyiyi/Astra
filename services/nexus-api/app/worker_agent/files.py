"""Local file and read-only Git commands for a migrated Worker.

Paths are opened from the configured workspace root with openat2. The browser
still talks to Nexus; this module only runs on the Worker host.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import platform
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from app.worker_agent.process_env import child_environ

MAX_FILE_BYTES = 20 * 1024 * 1024
FILE_COMMANDS = frozenset({"validate_path", "read_file", "git_readonly", "upload_file"})
FILE_COMMAND_LIMIT = 4
# linux/openat2.h. 0x04 is RESOLVE_NO_SYMLINKS and would reject an in-workspace
# relative symlink that sftp.normalize still accepts.
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_BENEATH = 0x08
_RESOLVE = _RESOLVE_BENEATH | _RESOLVE_NO_MAGICLINKS
_RENAME_NOREPLACE = 1
_OPENAT2 = 437
_HEX = set("0123456789abcdefABCDEF")
_PARTIAL_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")


class FileCommandError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _OpenHow(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


def openat2(dir_fd: int, path: str, flags: int, *, mode: int = 0) -> int:
    """Open path beneath dir_fd. ENOSYS and seccomp EPERM stay OSError."""

    how = _OpenHow(flags | os.O_CLOEXEC, mode, _RESOLVE)
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(
        ctypes.c_long(_OPENAT2),
        ctypes.c_int(dir_fd),
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)
    return int(fd)


def map_open_error(exc: OSError) -> FileCommandError:
    if exc.errno in {errno.EXDEV, errno.ELOOP}:
        return FileCommandError("symlink_escape", "符号链接指向任务 Workspace 之外")
    if exc.errno in {errno.ENOSYS, errno.EPERM}:
        return FileCommandError("io", exc.strerror or "openat2 不可用")
    if exc.errno == errno.ENOENT:
        return FileCommandError("io", "路径不存在")
    return FileCommandError("io", exc.strerror or "打开路径失败")


def lexical_relative(workspace: str, value: str) -> str:
    """Same remote lexical rules as workspace_files.relative_path."""

    value = value or "."
    if "\x00" in value or "\\" in value:
        raise FileCommandError("path_forbidden", "不允许访问此路径")
    path = PurePosixPath(value)
    root = PurePosixPath(workspace)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as exc:
            raise FileCommandError("path_outside", "路径不属于此任务 Workspace") from exc
    if ".." in path.parts or ".git" in path.parts:
        raise FileCommandError("path_forbidden", "不允许访问此路径")
    text = path.as_posix()
    return "." if text in {"", "."} else text


def sweep_partials(allowed_root: str) -> None:
    """Drop leftover upload partials when the Agent starts. Do not follow links."""

    root = os.path.abspath(allowed_root)
    _sweep_level(root)
    tasks = os.path.join(os.path.dirname(root), ".astra-tasks")
    _sweep_level(tasks)
    # Task worktrees are .astra-tasks/{employee}/{task}/uploads, one level deeper
    # than the published one-star glob. Still bounded, still not following links.
    _sweep_level(tasks, depth=2)


def _sweep_level(base: str, *, depth: int = 1) -> None:
    if depth < 1 or os.path.islink(base) or not os.path.isdir(base):
        return
    try:
        children = list(os.scandir(base))
    except OSError:
        return
    for child in children:
        if child.is_symlink():
            continue
        try:
            is_dir = child.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if not is_dir:
            continue
        if depth > 1:
            _sweep_level(child.path, depth=depth - 1)
            continue
        uploads = os.path.join(child.path, "uploads")
        if os.path.islink(uploads) or not os.path.isdir(uploads):
            continue
        try:
            names = os.listdir(uploads)
        except OSError:
            continue
        for name in names:
            if not name.startswith(".partial-"):
                continue
            target = os.path.join(uploads, name)
            if os.path.islink(target):
                continue
            try:
                os.unlink(target)
            except OSError:
                pass


def open_workspace(allowed_root: str, workspace: str) -> int:
    """Directory fd for workspace, opened from an allowed-root fd."""

    if not allowed_root:
        raise FileCommandError("io", "未配置工作区根，拒绝准备目录")
    root = os.path.abspath(allowed_root)
    target = os.path.abspath(workspace)
    tasks = os.path.join(os.path.dirname(root), ".astra-tasks")
    if _is_under(target, root):
        base, relative = root, os.path.relpath(target, root)
    elif _is_under(target, tasks):
        base, relative = tasks, os.path.relpath(target, tasks)
    else:
        raise FileCommandError("path_outside", "路径不属于此任务 Workspace")
    try:
        base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise FileCommandError("symlink_escape", "符号链接指向任务 Workspace 之外") from exc
        raise FileCommandError("path_outside", "路径不属于此任务 Workspace") from exc
    try:
        if relative == ".":
            return os.dup(base_fd)
        return openat2(base_fd, relative, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise map_open_error(exc) from exc
    finally:
        os.close(base_fd)


def validate_inside(workspace_fd: int, relative: str) -> str:
    """Open the full path. Only a missing final component may fall back to its parent."""

    relative = relative or "."
    try:
        fd = openat2(workspace_fd, relative, os.O_PATH)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise map_open_error(exc) from exc
        parent = PurePosixPath(relative).parent.as_posix()
        if parent in {"", "."}:
            parent = "."
        try:
            parent_fd = openat2(workspace_fd, parent, os.O_PATH | os.O_DIRECTORY)
        except OSError as parent_exc:
            raise map_open_error(parent_exc) from parent_exc
        os.close(parent_fd)
        return relative
    os.close(fd)
    return relative


def read_inside(workspace_fd: int, relative: str) -> tuple[bytes | None, dict[str, Any]]:
    empty = hashlib.sha256(b"").hexdigest()
    try:
        fd = openat2(workspace_fd, relative, os.O_RDONLY)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None, {"size": 0, "sha256": empty, "missing": True}
        raise map_open_error(exc) from exc
    try:
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_FILE_BYTES:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            total += len(block)
            chunks.append(block)
            if total > MAX_FILE_BYTES:
                raise FileCommandError("too_large", "Diff 文件不能超过 20 MB")
    finally:
        os.close(fd)
    data = b"".join(chunks)
    return data, {
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "missing": False,
    }


def build_git_argv(data: dict[str, Any]) -> list[str]:
    if any(key in data for key in ("args", "config", "c", "-c")):
        raise FileCommandError("unsupported_operation", "不支持的 Git 操作")
    operation = str(data.get("operation") or "")
    tail = _git_tail(operation, data)
    return ["-c", "core.fsmonitor=false", *tail]


def run_git(workspace: str, argv: list[str], deadline: float) -> tuple[int, bytes]:
    remaining = deadline - time.time()
    if remaining <= 0:
        return 124, b""
    try:
        process = subprocess.Popen(
            ["git", "-C", workspace, *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_environ(),
        )
    except OSError as exc:
        raise FileCommandError("io", exc.strerror or "git 无法启动") from exc
    try:
        output, _error = process.communicate(timeout=remaining)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return 124, b""
    if len(output) > MAX_FILE_BYTES:
        raise FileCommandError("too_large", "文件不能超过 20 MB")
    return int(process.returncode or 0), output


def upload_bytes(
    workspace_fd: int,
    workspace: str,
    filename: str,
    content: bytes,
    request_id: str,
    gate: Callable[[], None],
) -> str:
    if (
        not filename
        or filename != PurePosixPath(filename).name
        or "\\" in filename
        or "\x00" in filename
    ):
        raise FileCommandError("invalid_name", "文件名无效")
    if len(content) > MAX_FILE_BYTES:
        raise FileCommandError("too_large", "文件不能超过 20 MB")
    gate()
    uploads = _uploads_fd(workspace_fd)
    partial = _partial_name(request_id)
    final = f"{uuid4().hex[:12]}-{filename}"
    try:
        _unlink_partial(uploads, partial)
        fd = os.open(
            partial,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=uploads,
        )
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        gate()
        _rename_noreplace(uploads, partial, uploads, final)
    except FileCommandError:
        _unlink_partial(uploads, partial)
        raise
    except OSError as exc:
        _unlink_partial(uploads, partial)
        if exc.errno == errno.ELOOP:
            raise FileCommandError("symlink_escape", "上传目录越界") from exc
        raise FileCommandError("io", exc.strerror or "上传失败") from exc
    finally:
        os.close(uploads)
    return f"{workspace.rstrip('/')}/uploads/{final}"


def execute_file_command(
    data: dict[str, Any],
    *,
    allowed_root: str,
    request_id: str,
    deadline: float,
    content: bytes | None = None,
    gate: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], bytes | None]:
    name = str(data.get("name") or "")
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(payload, dict):
        payload = {}
    workspace = str(payload.get("workspace") or "")
    if name == "git_readonly":
        argv = build_git_argv(payload)
        directory = open_workspace(allowed_root, workspace)
        os.close(directory)
        code, output = run_git(workspace, argv, deadline)
        digest = hashlib.sha256(output).hexdigest()
        result = {"code": code, "size": len(output), "sha256": digest}
        return result, output or None
    if name == "upload_file":
        if content is None:
            raise FileCommandError("io", "上传正文不存在")
        expected = str(payload.get("sha256") or "")
        size = int(payload.get("size") or 0)
        actual = hashlib.sha256(content).hexdigest()
        if size != len(content) or actual != expected:
            raise FileCommandError("checksum_mismatch", "上传校验和不匹配")
        directory = open_workspace(allowed_root, workspace)
        try:
            path = upload_bytes(
                directory,
                workspace,
                str(payload.get("filename") or ""),
                content,
                request_id,
                gate or (lambda: None),
            )
        finally:
            os.close(directory)
        return {"path": path}, None
    relative = lexical_relative(workspace, str(payload.get("path") or "."))
    directory = open_workspace(allowed_root, workspace)
    try:
        if name == "validate_path":
            return {"relative": validate_inside(directory, relative)}, None
        if name == "read_file":
            body, meta = read_inside(directory, relative)
            return meta, body
    finally:
        os.close(directory)
    raise FileCommandError("unsupported_operation", "不支持的 Git 操作")


def connection_gate(closed: threading.Event, epoch_matches: Callable[[], bool]) -> Callable[[], None]:
    def gate() -> None:
        if closed.is_set() or not epoch_matches():
            raise FileCommandError("epoch_conflict", "epoch 已切换")

    return gate


def _git_tail(operation: str, data: dict[str, Any]) -> list[str]:
    revision = _revision(data.get("revision"), allow_head=True) if "revision" in data else None
    baseline = _revision(data.get("baseline"), allow_head=False) if "baseline" in data else None
    path = _optional_path(data.get("path")) if "path" in data else None
    file_name = _optional_path(data.get("file")) if "file" in data else None
    if operation == "head":
        return ["rev-parse", "HEAD"]
    if operation == "inside_work_tree":
        return ["rev-parse", "--is-inside-work-tree"]
    if operation == "git_common_dir":
        return ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    if operation == "branch":
        return ["branch", "--show-current"]
    if operation == "status_porcelain":
        return ["status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all"]
    if operation == "origin_url":
        return ["config", "--get", "remote.origin.url"]
    if operation == "rev_list":
        if baseline is None:
            raise FileCommandError("invalid_revision", "Git 修订无效")
        return ["rev-list", "--reverse", "--max-count=200", f"{baseline}..HEAD"]
    if operation == "diff_name_status_range":
        if baseline is None:
            raise FileCommandError("invalid_revision", "Git 修订无效")
        return ["diff", "--name-status", "--no-renames", "-z", f"{baseline}..HEAD"]
    if operation == "diff_numstat":
        if revision is None or path is None:
            raise FileCommandError("unsupported_operation", "不支持的 Git 操作")
        return [
            "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z",
            revision, "--", path,
        ]
    if operation == "ls_files_others":
        if path is None:
            raise FileCommandError("unsupported_operation", "不支持的 Git 操作")
        return ["ls-files", "--others", "--exclude-standard", "-z", "--", path]
    if operation == "show_file":
        if revision is None or file_name is None:
            raise FileCommandError("unsupported_operation", "不支持的 Git 操作")
        return ["show", f"{revision}:{file_name}"]
    if operation == "diff_patch":
        if revision is None or file_name is None:
            raise FileCommandError("unsupported_operation", "不支持的 Git 操作")
        return [
            "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
            revision, "--", file_name,
        ]
    if operation == "diff_name_status_file":
        if revision is None or file_name is None:
            raise FileCommandError("unsupported_operation", "不支持的 Git 操作")
        return ["diff", "--no-renames", "--name-status", "-z", revision, "--", file_name]
    raise FileCommandError("unsupported_operation", "不支持的 Git 操作")


def _revision(value: Any, *, allow_head: bool) -> str:
    text = str(value or "")
    if allow_head and text == "HEAD":
        return text
    if 4 <= len(text) <= 64 and all(char in _HEX for char in text):
        return text
    raise FileCommandError("invalid_revision", "Git 修订无效")


def _optional_path(value: Any) -> str:
    text = str(value or ".")
    if "\x00" in text or "\\" in text or text.startswith("/") or ".." in PurePosixPath(text).parts:
        raise FileCommandError("path_forbidden", "不允许访问此路径")
    if ".git" in PurePosixPath(text).parts:
        raise FileCommandError("path_forbidden", "不允许访问此路径")
    return "." if text in {"", "."} else text


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _uploads_fd(workspace_fd: int) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open("uploads", flags, dir_fd=workspace_fd)
    except FileNotFoundError:
        os.mkdir("uploads", 0o700, dir_fd=workspace_fd)
        try:
            return os.open("uploads", flags, dir_fd=workspace_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise FileCommandError("symlink_escape", "上传目录越界") from exc
            raise FileCommandError("io", exc.strerror or "上传目录不可用") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise FileCommandError("symlink_escape", "上传目录越界") from exc
        raise FileCommandError("io", exc.strerror or "上传目录不可用") from exc


def _partial_name(request_id: str) -> str:
    if _PARTIAL_ID.fullmatch(request_id):
        return f".partial-{request_id}"
    digest = hashlib.sha256(request_id.encode("utf-8", errors="replace")).hexdigest()[:32]
    return f".partial-{digest}"


def _unlink_partial(uploads: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=uploads)
    except FileNotFoundError:
        return
    except OSError:
        return


def _rename_noreplace(old_dir: int, old_name: str, new_dir: int, new_name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if platform.system() == "Linux" and hasattr(libc, "renameat2"):
        libc.renameat2.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
        ]
        libc.renameat2.restype = ctypes.c_int
        result = libc.renameat2(
            old_dir, os.fsencode(old_name), new_dir, os.fsencode(new_name), _RENAME_NOREPLACE,
        )
        if result == 0:
            return
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), new_name)
    raise OSError(errno.ENOSYS, "renameat2 不可用", new_name)

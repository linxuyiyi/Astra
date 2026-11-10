"""Worker-local path, Git, and upload commands."""

import errno
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.worker_tools.files import (
    FileCommandError,
    build_git_argv,
    execute_file_command,
    map_open_error,
    open_workspace,
    run_git,
    sweep_partials,
)

# 这些命令走 Linux openat2/O_PATH；在 Darwin 上调用 syscall 437 会直接触发 SIGSYS。
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Worker 文件命令依赖 Linux openat2/O_PATH")


def _run(root: Path, workspace: Path, name: str, **payload):
    data = {"workspace": str(workspace), **payload}
    return execute_file_command(
        {"name": name, "data": data},
        allowed_root=str(root),
        request_id="req-file",
        deadline=time.time() + 30,
        content=payload.get("content"),
        gate=payload.get("gate"),
    )


def test_external_final_symlink_is_rejected_and_deleted_parent_is_not(tmp_path: Path):
    root = tmp_path / "root"
    workspace = root / "ws"
    (workspace / "dir").mkdir(parents=True)
    (workspace / "link").symlink_to("/etc/passwd")
    with pytest.raises(FileCommandError) as escaped:
        _run(root, workspace, "validate_path", path="link")
    assert escaped.value.code == "symlink_escape"
    result, body = _run(root, workspace, "validate_path", path="dir/gone.txt")
    assert body is None
    assert result == {"relative": "dir/gone.txt"}


def test_absolute_inside_symlink_is_rejected_and_relative_symlink_is_readable(tmp_path: Path):
    root = tmp_path / "root"
    workspace = root / "ws"
    workspace.mkdir(parents=True)
    (workspace / "keep").write_bytes(b"inside")
    (workspace / "absolute").symlink_to(workspace / "keep")
    (workspace / "relative").symlink_to("keep")
    with pytest.raises(FileCommandError) as escaped:
        _run(root, workspace, "read_file", path="absolute")
    assert escaped.value.code == "symlink_escape"
    meta, body = _run(root, workspace, "read_file", path="relative")
    assert body == b"inside"
    assert meta["missing"] is False
    assert meta["size"] == len(body)


def test_open_errors_and_workspace_open_do_not_use_realpath(tmp_path: Path, monkeypatch):
    assert map_open_error(OSError(errno.EXDEV, "out")).code == "symlink_escape"
    assert map_open_error(OSError(errno.ELOOP, "loop")).code == "symlink_escape"
    assert map_open_error(OSError(errno.ENOSYS, "missing")).code == "io"
    assert map_open_error(OSError(errno.EPERM, "seccomp")).code == "io"
    root = tmp_path / "root"
    root.mkdir()

    def refuse_realpath(*_args, **_kwargs):
        raise AssertionError("realpath")

    monkeypatch.setattr(os.path, "realpath", refuse_realpath)
    directory = open_workspace(str(root), str(root))
    os.close(directory)

    def fail_open(*_args, **_kwargs):
        raise OSError(errno.ENOSYS, "openat2")

    monkeypatch.setattr("app.worker_tools.files.openat2", fail_open)
    with pytest.raises(FileCommandError) as missing:
        _run(root, root, "validate_path", path=".")
    assert missing.value.code == "io"
    monkeypatch.setattr(
        "app.worker_tools.files.openat2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(errno.EPERM, "blocked")),
    )
    with pytest.raises(FileCommandError) as blocked:
        _run(root, root, "validate_path", path=".")
    assert blocked.value.code == "io"


def test_git_argv_pins_fsmonitor_and_rejects_caller_config(tmp_path: Path, monkeypatch):
    argv = build_git_argv({"operation": "head"})
    assert argv[:2] == ["-c", "core.fsmonitor=false"]
    for key in ("-c", "c", "args", "config"):
        with pytest.raises(FileCommandError) as rejected:
            build_git_argv({key: "core.pager=cat", "operation": "head"})
        assert rejected.value.code == "unsupported_operation"
    seen: dict[str, list[str]] = {}

    class Process:
        returncode = 0

        def communicate(self, timeout=None):
            return b"abc\n", b""

    def popen(argv, **_kwargs):
        seen["argv"] = argv
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    code, output = run_git(str(tmp_path), argv, time.time() + 5)
    assert code == 0
    assert output == b"abc\n"
    assert seen["argv"][:5] == ["git", "-C", str(tmp_path), "-c", "core.fsmonitor=false"]
    stopped, empty = run_git(str(tmp_path), argv, time.time() - 1)
    assert (stopped, empty) == (124, b"")

    class Hanging:
        def __init__(self):
            self.killed = False
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("git", timeout or 1)
            return b"late", b""

        def kill(self):
            self.killed = True

    hanging = Hanging()
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: hanging)
    code, output = run_git(str(tmp_path), argv, time.time() + 5)
    assert hanging.killed is True
    assert (code, output) == (124, b"")




def test_checksum_mismatch_writes_nothing_and_rename_does_not_replace(tmp_path: Path):
    root = tmp_path / "root"
    workspace = root / "ws"
    workspace.mkdir(parents=True)
    with pytest.raises(FileCommandError) as mismatch:
        execute_file_command(
            {
                "name": "upload_file",
                "data": {
                    "workspace": str(workspace),
                    "filename": "a.txt",
                    "size": 2,
                    "sha256": "0" * 64,
                },
            },
            allowed_root=str(root),
            request_id="req-sum",
            deadline=time.time() + 5,
            content=b"hi",
        )
    assert mismatch.value.code == "checksum_mismatch"
    assert not (workspace / "uploads").exists()
    directory = open_workspace(str(root), str(workspace))
    os.close(directory)
    first = _run(
        root,
        workspace,
        "upload_file",
        filename="a.txt",
        size=2,
        sha256=hashlib.sha256(b"hi").hexdigest(),
        content=b"hi",
        gate=lambda: None,
    )
    assert first[0]["path"].endswith("-a.txt")
    from app.worker_tools.files import _rename_noreplace

    uploads = os.open(workspace / "uploads", os.O_RDONLY | os.O_DIRECTORY)
    try:
        source = os.open("source", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=uploads)
        os.write(source, b"src")
        os.close(source)
        destination = os.open("dest", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=uploads)
        os.close(destination)
        with pytest.raises(OSError):
            _rename_noreplace(uploads, "source", uploads, "dest")
        assert os.stat("source", dir_fd=uploads).st_size == 3
        assert os.stat("dest", dir_fd=uploads).st_size == 0
    finally:
        os.close(uploads)


def test_startup_sweep_removes_partials_without_following_links(tmp_path: Path):
    root = tmp_path / "root"
    uploads = root / "child" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / ".partial-one").write_text("partial")
    (uploads / "kept.txt").write_text("keep")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / ".partial-secret"
    secret.write_text("secret")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (uploads / ".partial-link").symlink_to(secret)
    deep = tmp_path / ".astra-tasks" / "emp" / "task" / "uploads"
    deep.mkdir(parents=True)
    (deep / ".partial-deep").write_text("deep")
    (deep / "done.txt").write_text("done")
    sweep_partials(str(root))
    assert not (uploads / ".partial-one").exists()
    assert (uploads / "kept.txt").read_text() == "keep"
    assert (uploads / ".partial-link").is_symlink()
    assert secret.read_text() == "secret"
    assert not (deep / ".partial-deep").exists()
    assert (deep / "done.txt").read_text() == "done"

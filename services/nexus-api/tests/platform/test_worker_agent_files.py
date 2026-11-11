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
    run_git,
    sweep_partials,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Worker 文件命令依赖 Linux O_PATH/renameat2")


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


def test_validate_path_allows_only_a_missing_final_component(tmp_path: Path):
    root = tmp_path / "root"
    workspace = root / "ws"
    (workspace / "dir" / "nested").mkdir(parents=True)
    result, body = _run(root, workspace, "validate_path", path="dir/nested/gone.txt")
    assert body is None
    assert result == {"relative": "dir/nested/gone.txt"}
    with pytest.raises(FileCommandError) as missing:
        _run(root, workspace, "validate_path", path="dir/missing/gone.txt")
    assert missing.value.code == "io"
    with pytest.raises(FileCommandError) as forbidden:
        _run(root, workspace, "validate_path", path="dir/../other")
    assert forbidden.value.code == "path_forbidden"


@pytest.mark.parametrize("command", ["validate_path", "read_file"])
def test_file_commands_reject_intermediate_and_final_symlinks(tmp_path: Path, command: str):
    root = tmp_path / "root"
    workspace = root / "ws"
    (workspace / "dir").mkdir(parents=True)
    (workspace / "dir" / "keep").write_bytes(b"inside")
    (workspace / "dir-link").symlink_to("dir", target_is_directory=True)
    (workspace / "file-link").symlink_to("dir/keep")
    (workspace / "outside-link").symlink_to(tmp_path / "missing")
    for path in ("dir-link/keep", "file-link", "outside-link"):
        with pytest.raises(FileCommandError) as escaped:
            _run(root, workspace, command, path=path)
        assert escaped.value.code == "symlink_escape"

    meta, body = _run(root, workspace, "read_file", path="dir/keep")
    assert body == b"inside"
    assert meta["missing"] is False
    assert meta["size"] == len(body)


def test_workspace_open_rejects_symlink_components(tmp_path: Path):
    root = tmp_path / "root"
    (root / "group" / "workspace").mkdir(parents=True)
    (root / "group-link").symlink_to("group", target_is_directory=True)
    (root / "workspace-link").symlink_to("group/workspace", target_is_directory=True)
    for workspace in (root / "group-link" / "workspace", root / "workspace-link"):
        with pytest.raises(FileCommandError) as escaped:
            _run(root, workspace, "git_readonly", operation="head")
        assert escaped.value.code == "symlink_escape"


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
    assert Path(first[0]["path"]).read_bytes() == b"hi"
    from app.worker_tools.files import _rename_noreplace

    uploads = os.open(workspace / "uploads", os.O_RDONLY | os.O_DIRECTORY)
    try:
        source = os.open("source", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=uploads)
        os.write(source, b"src")
        os.close(source)
        destination = os.open("dest", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=uploads)
        os.close(destination)
        with pytest.raises(FileExistsError) as collision:
            _rename_noreplace(uploads, "source", uploads, "dest")
        assert collision.value.errno == errno.EEXIST
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

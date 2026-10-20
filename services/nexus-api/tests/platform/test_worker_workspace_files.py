"""Migrated file, Git, and upload calls do not open SSH."""

import base64
import hashlib
import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.workspace_routes import persist_attachments, router, task_repo
from app.config.settings import get_settings
from app.database import SessionLocal, init_database
from app.domain import ExecutionMode
from app.models import AgentWorker, OpenCodeExecutionEnvironment, Task
from app.runtime.environment import ExecutionEnvironmentRuntime, environment_from_snapshot
from app.runtime.worker_files import WorkerFileError, git_command_payload, worker_git, worker_upload
from app.runtime.workspace_changes import workspace_diff
from app.runtime.workspace_files import check_request_path, upload, validate_path
from app.worker_gateway.bus import MemoryRouteBus
from app.worker_gateway.data_channel import request_key


class _SecretEnv:
    id = 1
    mode = ExecutionMode.LINUX_REMOTE
    worker_channel = "worker"

    @property
    def ssh_password(self):
        raise AssertionError("ssh password read")


def test_git_operations_match_the_existing_invocations():
    assert git_command_payload(("rev-parse", "HEAD")) == {"operation": "head"}
    assert git_command_payload(("rev-parse", "--is-inside-work-tree")) == {"operation": "inside_work_tree"}
    assert git_command_payload(("rev-parse", "--path-format=absolute", "--git-common-dir")) == {
        "operation": "git_common_dir"
    }
    assert git_command_payload(("branch", "--show-current")) == {"operation": "branch"}
    assert git_command_payload(
        ("status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all")
    ) == {"operation": "status_porcelain"}
    assert git_command_payload(("config", "--get", "remote.origin.url")) == {"operation": "origin_url"}
    assert git_command_payload(("rev-list", "--reverse", "--max-count=200", "abcd..HEAD")) == {
        "operation": "rev_list",
        "baseline": "abcd",
    }
    assert git_command_payload(("diff", "--name-status", "--no-renames", "-z", "abcd..HEAD")) == {
        "operation": "diff_name_status_range",
        "baseline": "abcd",
    }
    assert git_command_payload(
        ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", "HEAD", "--", "a")
    ) == {"operation": "diff_numstat", "revision": "HEAD", "path": "a"}
    assert git_command_payload(("ls-files", "--others", "--exclude-standard", "-z", "--", ".")) == {
        "operation": "ls_files_others",
        "path": ".",
    }
    assert git_command_payload(("show", "HEAD:a.txt")) == {
        "operation": "show_file",
        "revision": "HEAD",
        "file": "a.txt",
    }
    assert git_command_payload(
        ("diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "HEAD", "--", "a.txt")
    ) == {"operation": "diff_patch", "revision": "HEAD", "file": "a.txt"}
    assert git_command_payload(("diff", "--no-renames", "--name-status", "-z", "HEAD", "--", "a.txt")) == {
        "operation": "diff_name_status_file",
        "revision": "HEAD",
        "file": "a.txt",
    }
    with pytest.raises(WorkerFileError):
        git_command_payload(("push",))
    with pytest.raises(WorkerFileError):
        git_command_payload(("-c", "core.pager=cat", "status"))


def test_browser_workspace_routes_keep_their_urls_and_media_types():
    paths = " ".join(getattr(route, "path", "") for route in router.routes)
    assert "/files/{filename}" in paths
    assert "/repo" in paths
    assert "/opencode/{session_id}/{path:path}" in paths
    import app.api.workspace_routes as workspace_routes

    source = inspect.getsource(workspace_routes)
    assert "client.exchange" in source
    assert "check_request_path" in source
    assert 'media_type="text/event-stream"' in source
    assert 'media_type="text/x-diff"' in source
    assert '"Cache-Control": "no-cache"' in source
    # OpenCode still resolves /file* after validate_path, the same window SSH left open.


async def test_worker_channel_does_not_open_ssh_or_read_the_password(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def run_command(self, name, data, session_id=None, request_id=None):
            calls.append((name, data))
            if name == "validate_path":
                return {"relative": "a.txt"}
            if name == "read_file":
                return {"missing": True, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
            if name == "git_readonly":
                return {"code": 0, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
            raise AssertionError(name)

    async def opened(self, environment):
        raise AssertionError("ssh opened")

    async def remote(self, environment, command, **_kwargs):
        raise AssertionError("ssh remote")

    monkeypatch.setattr("app.runtime.worker_files.WorkerOpenCodeClient", FakeClient)
    monkeypatch.setattr(ExecutionEnvironmentRuntime, "open_session", opened)
    monkeypatch.setattr(ExecutionEnvironmentRuntime, "run_remote", remote)
    env = _SecretEnv()
    assert await validate_path(env, "/ws", "a.txt") == "a.txt"
    from app.runtime.workspace_changes import _git, _working_text

    assert await _git(env, "/ws", "rev-parse", "HEAD") == (0, "")
    assert await _working_text(env, "/ws", "a.txt") == ""
    assert [name for name, _data in calls] == ["validate_path", "git_readonly", "validate_path", "read_file"]
    assert calls[1][1]["operation"] == "head"
    assert "-c" not in calls[1][1]

    ssh = SimpleNamespace(mode=ExecutionMode.LINUX_REMOTE, worker_channel="ssh", id=None)
    with pytest.raises(RuntimeError, match="尚未安装 Worker"):
        await validate_path(ssh, "/ws", "a.txt")
    with pytest.raises(RuntimeError, match="尚未安装 Worker"):
        await _git(ssh, "/ws", "rev-parse", "HEAD")


async def test_snapshot_environment_follows_the_stored_channel(monkeypatch):
    await init_database()
    async with SessionLocal() as session:
        employee = AgentWorker(id=f"emp-{uuid4().hex[:8]}", name="远程员工", role="执行")
        session.add(employee)
        await session.flush()
        environment = OpenCodeExecutionEnvironment(
            employee_id=employee.id,
            mode="LINUX_REMOTE",
            name="云执行机",
            host="example.invalid",
            username="dev",
            ssh_password="secret",
            opencode_path="opencode",
            workspace_root="/work",
            worker_channel="worker",
        )
        session.add(environment)
        await session.commit()
        await session.refresh(environment)
        snapshot = environment_from_snapshot(
            {
                "environment_id": environment.id,
                "mode": "LINUX_REMOTE",
                "name": "云执行机",
                "opencode_path": "opencode",
                "workspace_root": "/work",
            },
            employee.id,
        )
        assert snapshot.worker_channel != "worker"

    seen = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def run_command(self, name, data, session_id=None, request_id=None):
            seen.append(name)
            return {"relative": "."}

    async def opened(self, environment):
        raise AssertionError("ssh opened")

    monkeypatch.setattr("app.runtime.worker_files.WorkerOpenCodeClient", FakeClient)
    monkeypatch.setattr(ExecutionEnvironmentRuntime, "open_session", opened)
    assert await validate_path(snapshot, "/work/task", ".") == "."
    assert seen == ["validate_path"]


async def test_upload_stages_bytes_and_deleted_diff_still_returns_the_file(monkeypatch):
    holder = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            self.settings = get_settings()
            self.gateway = SimpleNamespace(bus=MemoryRouteBus(), settings=self.settings)
            holder["client"] = self

        async def run_command(self, name, data, session_id=None, request_id=None):
            self.request_id = request_id
            stored = await self.gateway.bus.get_text(request_key(request_id))
            assert base64.b64decode(stored) == b"hello"
            assert "content" not in data and "body" not in data
            assert data["sha256"] == hashlib.sha256(b"hello").hexdigest()
            return {"path": "/ws/uploads/abc-a.txt"}

    monkeypatch.setattr("app.runtime.worker_files.WorkerOpenCodeClient", FakeClient)
    path = await worker_upload(_SecretEnv(), "/ws", "a.txt", b"hello")
    assert path == "/ws/uploads/abc-a.txt"
    staged = request_key(holder["client"].request_id)
    assert await holder["client"].gateway.bus.get_text(staged) is None

    async def fake_validate(_environment, _workspace, value):
        return value

    async def fake_git(_environment, _workspace, args):
        if args == ("rev-parse", "--is-inside-work-tree"):
            return 0, "true\n"
        if "--numstat" in args:
            return 0, "0\t3\tgone.txt\0"
        if args[0] == "ls-files":
            return 0, ""
        if args[0] == "show":
            return 0, "old\n"
        if "--no-color" in args:
            return 0, "diff --git a/gone.txt\n"
        if "--name-status" in args:
            return 0, "D\0gone.txt\0"
        return 0, ""

    async def fake_read(_environment, _workspace, _path):
        return ""

    monkeypatch.setattr("app.runtime.worker_files.worker_validate_path", fake_validate)
    monkeypatch.setattr("app.runtime.worker_files.worker_git", fake_git)
    monkeypatch.setattr("app.runtime.worker_files.worker_read_text", fake_read)

    class Session:
        async def scalar(self, *_args, **_kwargs):
            return None

        async def commit(self):
            return None

    class TaskRow:
        id = "t-gone"
        workspace = "/ws"

    # validate_path is imported into workspace_changes, so the worker branch calls
    # worker_validate_path. A missing final component still yields the relative path.
    diffs = await workspace_diff(
        Session(), TaskRow(), cumulative=False, environment=_SecretEnv(), workspace="/ws"
    )
    assert [item["file"] for item in diffs] == ["gone.txt"]
    assert diffs[0]["status"] == "deleted"
    assert set(diffs[0]) == {"file", "patch", "before", "after", "additions", "deletions", "status"}


async def test_file_api_still_validates_before_opencode_and_repo_overflow_is_dirty(monkeypatch):
    seen = []

    async def fake_validate(environment, workspace, value):
        seen.append(value)
        return "a.txt"

    monkeypatch.setattr("app.runtime.workspace_files.validate_path", fake_validate)
    env = _SecretEnv()
    assert await check_request_path(env, "/ws", "a.txt", native_path="/file") == "a.txt"
    assert await check_request_path(env, "/ws", "src/a.txt", native_path="/find") == "src/a.txt"
    assert seen == ["a.txt"]

    body = {"parts": [{"type": "file", "url": "file:///ws/a.txt"}]}
    monkeypatch.setattr("app.api.workspace_routes.validate_path", fake_validate)
    await persist_attachments(body, env, "/ws")
    assert seen[-1] == "/ws/a.txt"

    await init_database()
    async with SessionLocal() as session:
        employee = AgentWorker(id=f"emp-{uuid4().hex[:8]}", name="远程员工", role="执行")
        session.add(employee)
        await session.flush()
        environment = OpenCodeExecutionEnvironment(
            employee_id=employee.id,
            mode="LINUX_REMOTE",
            name="云执行机",
            opencode_path="opencode",
            workspace_root="/work",
            worker_channel="worker",
            ssh_password="secret",
        )
        session.add(environment)
        await session.flush()
        task = Task(
            id=f"T-{uuid4().hex[:8]}",
            title="repo",
            task_type="ISSUE",
            status="RUNNING",
            employee_id=employee.id,
            workspace="/work/task",
            execution_environment_snapshot={
                "environment_id": environment.id,
                "mode": "LINUX_REMOTE",
                "name": "云执行机",
                "opencode_path": "opencode",
                "workspace_root": "/work",
            },
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    async def fake_git(_environment, _workspace, args):
        if args[0] == "status":
            raise WorkerFileError("too_large", "文件不能超过 20 MB")
        if args == ("rev-parse", "HEAD"):
            return 0, "abc123\n"
        if args[0] == "branch":
            return 0, "main\n"
        if args[0] == "config":
            return 1, ""
        return 0, "/work/repo/.git\n"

    async def allow(*_args, **_kwargs):
        return "."

    async def empty(*_args, **_kwargs):
        return {}

    monkeypatch.setattr("app.runtime.worker_files.worker_git", fake_git)
    monkeypatch.setattr("app.api.workspace_routes.validate_path", allow)
    monkeypatch.setattr("app.api.workspace_routes.collect_workspace_changes", empty)
    async with SessionLocal() as session:
        payload = await task_repo(task_id, session)
    assert payload["dirty"] is True
    assert payload["commit"] == "abc123"
    assert payload["branch"] == "main"
    assert set(payload) == {"name", "branch", "commit", "dirty", "changes", "task_changes"}


async def test_worker_git_decodes_raw_bytes_with_replacement(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def run_command(self, name, data, session_id=None, request_id=None):
            return {
                "code": 0,
                "size": 1,
                "sha256": hashlib.sha256(b"\xff").hexdigest(),
                "body": base64.b64encode(b"\xff").decode("ascii"),
            }

    monkeypatch.setattr("app.runtime.worker_files.WorkerOpenCodeClient", FakeClient)
    code, text = await worker_git(_SecretEnv(), "/ws", ("show", "HEAD:a.bin"))
    assert code == 0
    assert text == "\ufffd"


async def test_upload_helper_is_what_the_route_calls(monkeypatch):
    async def fake_upload(environment, workspace, filename, content):
        assert environment.worker_channel == "worker"
        return f"{workspace}/uploads/abc-{filename}"

    monkeypatch.setattr("app.runtime.worker_files.worker_upload", fake_upload)
    # upload() imports the helper inside the function.
    path = await upload(_SecretEnv(), "/ws", "a.txt", b"hi")
    assert path == "/ws/uploads/abc-a.txt"


async def test_repo_route_turns_uninstalled_worker_into_503():
    await init_database()
    async with SessionLocal() as session:
        employee = AgentWorker(id=f"emp-{uuid4().hex[:8]}", name="远程员工", role="执行")
        session.add(employee)
        await session.flush()
        environment = OpenCodeExecutionEnvironment(
            employee_id=employee.id,
            mode="LINUX_REMOTE",
            name="云执行机",
            opencode_path="opencode",
            workspace_root="/work",
            worker_channel="ssh",
        )
        session.add(environment)
        await session.flush()
        task = Task(
            id=f"T-{uuid4().hex[:8]}",
            title="repo",
            task_type="ISSUE",
            status="RUNNING",
            employee_id=employee.id,
            workspace="/work/task",
            execution_environment_snapshot={
                "environment_id": environment.id,
                "mode": "LINUX_REMOTE",
                "name": "云执行机",
                "opencode_path": "opencode",
                "workspace_root": "/work",
            },
        )
        session.add(task)
        await session.commit()
        task_id = task.id
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as error:
            await task_repo(task_id, session)
    assert error.value.status_code == 503
    assert "不可连接" in error.value.detail

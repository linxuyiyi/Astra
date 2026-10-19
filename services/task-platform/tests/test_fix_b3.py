"""修订号门禁、远程克隆、分配检查和共用 JSONC。"""

import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.config.settings import get_settings
from app.domain import EmployeeStatus, EnvironmentStatus, TaskStatus
from app.main import app
from app.models import Base, DigitalEmployee, OpenCodeExecutionEnvironment, Task, TaskCategory
from app.runtime.capability import (
    binding_entries,
    binding_revision,
    discover_names,
    dispatch_allowed,
    loads_jsonc,
    observe_bindings,
    stamp_desired,
)
from app.runtime.dispatcher import Dispatcher, runtime_assignment_block
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.runtime.remote_provisioning import RemoteProvisioner
from app.runtime.repository import copy_worker
from app.schemas import EmployeeCreate, ExecutionEnvironmentWrite
from app.worker_agent.channel_switch import write_direct_config
from app.worker_agent.jsonc import loads_jsonc as parse_jsonc

PAT = "ghp_fixb3tokenvalue"


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def test_shared_jsonc_accepts_inline_comments_and_trailing_commas(tmp_path):
    text = """
    {
      // line
      "url": "http://example/a//b", /* block
      comment */
      "n": 1,
    }
    """
    assert parse_jsonc(text) == {"url": "http://example/a//b", "n": 1}
    assert loads_jsonc(text)["url"] == "http://example/a//b"
    assert loads_jsonc("[1, 2,]") == {}
    target = tmp_path / "opencode.jsonc"
    target.write_text('{ "skill": "review", }\n', encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "app/worker_agent/jsonc.py", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"skill": "review"}


def test_bindings_without_a_revision_record_are_not_applied():
    assert dispatch_allowed(None, bindings=["review"]) is False
    assert dispatch_allowed({}, bindings=["review"]) is False
    assert dispatch_allowed(None) is True
    assert dispatch_allowed({}, bindings=[]) is True
    desired = binding_revision(["review", "library"])
    detail = stamp_desired({}, ["review", "library"], checked_at="2026-10-17T00:00:00Z")
    assert detail["capability"]["desired_revision"] == desired
    assert "applied_revision" not in detail["capability"]
    assert dispatch_allowed(detail, bindings=["review", "library"]) is False
    found = discover_names(["review"], '{"mcp": {"library": {}}}')
    record = observe_bindings(
        detail["capability"],
        desired=desired,
        found=found,
        expected=["review", "library"],
        error="",
        checked_at="2026-10-17T00:01:00Z",
    )
    assert record["applied_revision"] == desired
    assert record["runtime_status"] == "applied"
    same = binding_entries(["review"], [{"name": "library", "url": "http://127.0.0.1/a"}])
    other = binding_entries(["review"], [{"name": "library", "url": "http://127.0.0.1/b"}])
    configured = binding_entries(
        ["review"],
        [{"name": "library", "url": "http://127.0.0.1/a", "config": {"timeout": 1}}],
    )
    assert binding_revision(same) != desired
    assert binding_revision(same) != binding_revision(other)
    assert binding_revision(same) == binding_revision(configured)


@pytest.mark.asyncio
async def test_binding_changes_stamp_desired_until_deploy_observes(session):
    session.add(TaskCategory(id="testing", name="测试"))
    await session.commit()
    from app.api.routes import create_employee, update_employee_environment

    created = await create_employee(
        EmployeeCreate(
            id="E-bind",
            name="绑定",
            role="build",
            category_ids=["testing"],
            execution_environment=ExecutionEnvironmentWrite(
                skill_refs=["review"],
                mcp_refs=[{"name": "library"}],
            ),
        ),
        session,
    )
    environment = created.execution_environment
    assert environment is not None
    names = ["review", "library"]
    assert environment.status_detail["capability"]["desired_revision"] == binding_revision(names)
    assert dispatch_allowed(environment.status_detail, bindings=names) is False

    updated = await update_employee_environment(
        "E-bind",
        ExecutionEnvironmentWrite(name="改名", workspace_root="./workspaces"),
        session,
    )
    assert updated.skill_refs == ["review"]
    assert updated.status_detail["capability"]["desired_revision"] == binding_revision(names)

    changed = await update_employee_environment(
        "E-bind",
        ExecutionEnvironmentWrite(skill_refs=["other"]),
        session,
    )
    assert changed.skill_refs == ["other"]
    assert changed.status_detail["capability"]["desired_revision"] == binding_revision(["other", "library"])
    assert changed.status_detail["capability"]["runtime_status"] == "pending"


@pytest.mark.asyncio
async def test_deploy_observe_writes_applied_revision():
    environment = OpenCodeExecutionEnvironment(
        employee_id="E1",
        mode="LINUX_REMOTE",
        name="remote",
        host="10.0.0.8",
        username="root",
        workspace_root="/srv/app",
        skill_refs=["review"],
        mcp_refs=[{"name": "library"}],
        opencode_path="opencode",
        status_detail=stamp_desired({}, ["review", "library"], checked_at="2026-10-17T00:00:00Z"),
    )
    provisioner = RemoteProvisioner(environment, {"id": "op", "action": "deploy"})

    class SSH:
        async def read_file(self, _path):
            return b'{ "mcp": { "library": {}, }, }'

        async def run_command(self, command):
            assert "SKILL.md" in command
            return 0, "/cfg/skill/review\n", ""

    provisioner.ssh = SSH()
    provisioner.config_path = "/cfg/opencode.jsonc"
    record = await provisioner._observe_capability()
    assert record["applied_revision"] == record["desired_revision"]
    assert record["runtime_status"] == "applied"
    assert dispatch_allowed(
        {"capability": record}, bindings=["review", "library"]
    )


def _employee(employee_id="E1", **overrides):
    values = dict(
        id=employee_id,
        name=employee_id,
        role="build",
        status=EmployeeStatus.IDLE,
        enabled=True,
        auto_claim=True,
        auto_execute=False,
        task_types=["ISSUE"],
        category_ids=["testing"],
    )
    values.update(overrides)
    return DigitalEmployee(**values)


def _environment(employee_id="E1", **overrides):
    values = dict(
        employee_id=employee_id,
        mode="LOCAL",
        name="local",
        workspace_root="/tmp/workspaces/" + employee_id,
        repository_id="repo-1",
        status=EnvironmentStatus.AVAILABLE,
        status_detail={},
    )
    values.update(overrides)
    return OpenCodeExecutionEnvironment(**values)


def _task(task_id, **overrides):
    values = dict(
        id=task_id,
        title=task_id,
        task_type="ISSUE",
        category_id="testing",
        status=TaskStatus.PENDING,
        repository_id="repo-1",
    )
    values.update(overrides)
    return Task(**values)


@pytest.mark.asyncio
async def test_child_waits_until_the_parent_succeeds(session, monkeypatch):
    async def uses(environment):
        return getattr(environment, "worker_channel", None) == "worker"

    monkeypatch.setattr("app.runtime.dispatcher.environment_uses_worker", uses)
    session.add(_employee())
    session.add(_environment())
    session.add(_task("parent", status=TaskStatus.RUNNING))
    session.add(_task("child", related_task_id="parent"))
    await session.commit()
    assert await Dispatcher(session).claim_once() is None
    parent = await session.get(Task, "parent")
    parent.status = TaskStatus.SUCCEEDED
    await session.commit()
    claimed = await Dispatcher(session).claim_once()
    assert claimed is not None
    assert claimed.id == "child"


@pytest.mark.asyncio
async def test_canceled_parent_skips_the_child_with_a_reason(session, monkeypatch):
    async def uses(environment):
        return getattr(environment, "worker_channel", None) == "worker"

    monkeypatch.setattr("app.runtime.dispatcher.environment_uses_worker", uses)
    session.add(_employee())
    session.add(_environment())
    session.add(_task("parent", status=TaskStatus.CANCELED))
    session.add(_task("child", related_task_id="parent"))
    await session.commit()
    assert await Dispatcher(session).claim_once() is None
    child = await session.get(Task, "child")
    assert child.status == TaskStatus.PENDING
    assert child.current_progress == "父任务已取消，阻塞中"
    assert child.runtime_error is None
    assert await Dispatcher(session).claim_once() is None


@pytest.mark.asyncio
async def test_worker_base_commit_comes_from_the_worker(session, monkeypatch):
    async def uses(_environment):
        return True

    async def head(_environment, _workspace):
        return "workerhead"

    monkeypatch.setattr("app.runtime.dispatcher.environment_uses_worker", uses)
    monkeypatch.setattr("app.runtime.dispatcher.workspace_head_commit", head)
    session.add(_employee())
    session.add(_environment(worker_channel="worker", workspace_root="/srv/missing"))
    session.add(_task("T-worker"))
    await session.commit()
    claimed = await Dispatcher(session).claim_once()
    assert claimed is not None
    assert claimed.base_commit == "workerhead"
    assert claimed.execution_environment_snapshot["base_commit"] == "workerhead"
    assert claimed.execution_environment_snapshot["repository_id"] == "repo-1"


@pytest.mark.asyncio
async def test_start_reuses_the_assignment_checks_and_merges_the_snapshot(session, monkeypatch):
    session.add(_employee())
    session.add(_environment(workspace_root="/tmp/does-not-matter"))
    session.add(
        _task(
            "T-start",
            repository_id=None,
            status=TaskStatus.WAITING,
            employee_id="E1",
            execution_environment_snapshot={"kept_marker": "stay", "model_name": "old"},
        )
    )
    await session.commit()
    with pytest.raises(RuntimeStateError, match="任务没有绑定仓库"):
        await RuntimeService(session).start("T-start")

    task = await session.get(Task, "T-start")
    task.repository_id = "repo-1"
    environment = await session.scalar(
        select(OpenCodeExecutionEnvironment).where(OpenCodeExecutionEnvironment.employee_id == "E1")
    )
    environment.repository_id = "repo-other"
    await session.commit()
    with pytest.raises(RuntimeStateError, match="任务仓库与 Worker 不一致"):
        await RuntimeService(session).start("T-start")

    environment.repository_id = "repo-1"
    environment.skill_refs = ["review"]
    await session.commit()
    blocked = runtime_assignment_block(environment, await session.get(Task, "T-start"))
    assert blocked == "Worker 能力绑定尚未应用"
    with pytest.raises(RuntimeStateError, match="Worker 能力绑定尚未应用"):
        await RuntimeService(session).start("T-start")

    environment.skill_refs = []
    await session.commit()

    class Client:
        async def health(self):
            return {"version": "1.2.3"}

        async def create_session(self, *_args, **_kwargs):
            return {"id": "ses-new"}

    @asynccontextmanager
    async def fake_connect(*_args, **_kwargs):
        yield Client()

    async def fake_prepare(_task, _environment, *, reuse_existing=True):
        return "/tmp/workspaces/E1/T-start"

    async def fake_head(_environment, _workspace):
        return "abc123"

    async def fake_deliver(self, task_id):
        return await self.session.get(Task, task_id)

    monkeypatch.setattr("app.runtime.lifecycle.connect", fake_connect)
    monkeypatch.setattr("app.runtime.lifecycle.prepare_workspace", fake_prepare)
    monkeypatch.setattr("app.runtime.lifecycle.workspace_head_commit", fake_head)
    monkeypatch.setattr(RuntimeService, "deliver_first_submission", fake_deliver)
    started = await RuntimeService(session).start("T-start")
    assert started.base_commit == "abc123"
    snapshot = started.execution_environment_snapshot
    assert snapshot["kept_marker"] == "stay"
    assert snapshot["repository_id"] == "repo-1"
    assert snapshot["base_commit"] == "abc123"
    assert snapshot["opencode_version"] == "1.2.3"
    assert started.current_session_id == "ses-new"


def test_direct_config_merges_the_gateway_into_jsonc(tmp_path):
    jsonc = tmp_path / "opencode.jsonc"
    legacy = tmp_path / "opencode.json"
    jsonc.write_text(
        '{ "theme": "dark", "mcp": { "custom": {"enabled": false}, }, }\n',
        encoding="utf-8",
    )
    legacy.write_text(
        json.dumps(
            {
                "theme": "overwrite-me",
                "provider": {"nexus-gateway": {"options": {"apiKey": "k", "baseURL": "http://old/v1"}}},
                "mcp": {"nexus-library": {"url": "http://old/mcp"}},
            }
        ),
        encoding="utf-8",
    )
    write_direct_config(jsonc, gateway_url="http://10.0.0.8:8110/v1", mcp_url="http://10.0.0.8:8201/mcp")
    written = json.loads(jsonc.read_text(encoding="utf-8"))
    assert written["theme"] == "dark"
    assert written["mcp"]["custom"] == {"enabled": False}
    assert written["provider"]["nexus-gateway"]["options"]["baseURL"] == "http://10.0.0.8:8110/v1"
    assert written["provider"]["nexus-gateway"]["options"]["apiKey"] == "k"
    assert written["mcp"]["nexus-library"]["url"] == "http://10.0.0.8:8201/mcp"
    assert "overwrite-me" not in jsonc.read_text(encoding="utf-8")
    assert json.loads(legacy.read_text(encoding="utf-8"))["theme"] == "overwrite-me"


@pytest.mark.asyncio
async def test_copy_picks_a_free_port_or_the_requested_one(session):
    source = _employee("src")
    environment = _environment(
        "src",
        host="10.9.9.9",
        opencode_server_port=19991,
        workspace_root="/tmp/workspaces/src",
        skill_refs=["review"],
        mcp_refs=[{"name": "library"}],
    )
    source.execution_environment = environment
    other = _employee("other")
    occupied = _environment("other", host="10.9.9.9", opencode_server_port=14096)
    other.execution_environment = occupied
    session.add_all([source, environment, other, occupied])
    await session.commit()
    loaded = await session.scalar(
        select(DigitalEmployee)
        .where(DigitalEmployee.id == "src")
        .options(selectinload(DigitalEmployee.execution_environment))
    )
    await copy_worker(session, loaded, new_id="copy-a", name="副本")
    copied = await session.scalar(
        select(OpenCodeExecutionEnvironment).where(OpenCodeExecutionEnvironment.employee_id == "copy-a")
    )
    assert copied.opencode_server_port == 14097
    assert copied.port == 22
    assert dispatch_allowed(copied.status_detail, bindings=["review", "library"]) is False
    loaded = await session.scalar(
        select(DigitalEmployee)
        .where(DigitalEmployee.id == "src")
        .options(selectinload(DigitalEmployee.execution_environment))
    )
    await copy_worker(session, loaded, new_id="copy-b", name="指定端口", port=15000)
    chosen = await session.scalar(
        select(OpenCodeExecutionEnvironment).where(OpenCodeExecutionEnvironment.employee_id == "copy-b")
    )
    assert chosen.opencode_server_port == 15000


def _git_stub() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json, os, sys
        from pathlib import Path
        log = Path(os.environ["GIT_LOG"])
        state_path = Path(os.environ["GIT_STATE"])
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        args = sys.argv[1:]
        workspace = None
        while args and args[0] in {"-C", "-c"}:
            if args[0] == "-C":
                workspace = args[1]
            args = args[2:]
        command = args[0] if args else ""
        rest = args[1:]
        record = {"argv": sys.argv[1:], "command": command, "token": os.environ.get("NEXUS_GIT_TOKEN", "")}
        askpass = os.environ.get("GIT_ASKPASS")
        if askpass and Path(askpass).is_file():
            record["askpass"] = Path(askpass).read_text()
        token_path = os.environ.get("TOKEN_PATH")
        if token_path and Path(token_path).is_file():
            record["mode"] = oct(Path(token_path).stat().st_mode & 0o777)
        lines = json.loads(log.read_text()) if log.exists() else []
        lines.append(record)
        log.write_text(json.dumps(lines))
        repo = state.get(workspace or "", {})
        if command == "rev-parse" and "--git-dir" in rest:
            sys.exit(0 if repo else 1)
        if command == "rev-parse" and "HEAD" in rest:
            sys.exit(0 if repo.get("head") else 1)
        if command == "config":
            sys.stdout.write(repo.get("origin", ""))
            sys.exit(0)
        if command == "branch":
            sys.stdout.write(repo.get("branch", ""))
            sys.exit(0)
        if command == "clone":
            branch = rest[rest.index("--branch") + 1]
            url, dest = rest[-2], rest[-1]
            state[dest] = {"origin": url, "branch": branch, "head": "cloned"}
        elif command == "init":
            state[workspace] = {"origin": "", "branch": "", "head": ""}
        elif command == "commit":
            repo["head"] = "init"
            state[workspace] = repo
        else:
            sys.exit(1)
        state_path.write_text(json.dumps(state))
        """
    )


def _run_workspace(tmp_path: Path, payload: dict, *, token: str | None, state: dict, workspace: Path):
    script = Path("app/runtime/remote_install.sh").read_text(encoding="utf-8")
    body = script[script.index("step '工程目录' running") : script.index("step '工程目录' passed")]
    trap = next(line for line in script.splitlines() if line.startswith("trap "))
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    git = bindir / "git"
    git.write_text(_git_stub(), encoding="utf-8")
    git.chmod(0o755)
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")
    token_file = tmp_path / "secret.token"
    if token is not None:
        fd = os.open(token_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
    log = tmp_path / "git.log"
    state_path = tmp_path / "git-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    wrapper = tmp_path / "workspace.sh"
    token_ref = str(token_file) if token is not None else ""
    wrapper.write_text(
        "\n".join(
            [
                "set -euo pipefail",
                "umask 077",
                "step() { printf 'ASTRA_STEP\\t%s\\t%s\\n' \"$1\" \"$2\"; }",
                f"python_bin={sys.executable}",
                f"payload={payload_file}",
                f"workspace={workspace}",
                f"token_file={token_ref}",
                "askpass=",
                trap,
                body,
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(PATH=f"{bindir}:{env.get('PATH', '')}", GIT_LOG=str(log), GIT_STATE=str(state_path))
    if token is not None:
        env["TOKEN_PATH"] = str(token_file)
    completed = subprocess.run(
        ["bash", str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    calls = json.loads(log.read_text(encoding="utf-8")) if log.exists() else []
    return completed, calls, token_file


def test_remote_install_clones_with_askpass_and_checks_an_existing_workspace(tmp_path):
    workspace = tmp_path / "srv" / "app"
    payload = {"repository": {"url": "https://git.example/org/repo.git", "branch": "dev"}}
    completed, calls, token_file = _run_workspace(
        tmp_path / "clone", payload, token=PAT, state={}, workspace=workspace
    )
    assert completed.returncode == 0, completed.stderr
    assert not token_file.exists()
    cloned = next(item for item in calls if item["command"] == "clone")
    assert PAT not in " ".join(cloned["argv"])
    assert cloned["token"] == PAT
    assert PAT not in cloned["askpass"]
    assert cloned["mode"] == oct(stat.S_IRUSR | stat.S_IWUSR)
    assert all(item["command"] != "init" for item in calls)
    stored = json.loads((tmp_path / "clone" / "git-state.json").read_text(encoding="utf-8"))
    assert stored[str(workspace)]["origin"] == "https://git.example/org/repo.git"
    assert stored[str(workspace)]["branch"] == "dev"

    mismatch, mismatch_calls, mismatch_token = _run_workspace(
        tmp_path / "mismatch",
        payload,
        token=PAT,
        state={str(workspace): {"origin": "https://git.example/org/other.git", "branch": "dev", "head": "x"}},
        workspace=workspace,
    )
    assert mismatch.returncode != 0
    assert "origin" in mismatch.stderr
    assert all(item["command"] != "clone" for item in mismatch_calls)
    assert not mismatch_token.exists()

    occupied = tmp_path / "dirty" / "app"
    occupied.mkdir(parents=True)
    (occupied / "README").write_text("x", encoding="utf-8")
    dirty, dirty_calls, _token = _run_workspace(
        tmp_path / "dirty-run", payload, token=PAT, state={}, workspace=occupied
    )
    assert dirty.returncode != 0
    assert "不为空" in dirty.stderr
    assert all(item["command"] != "clone" for item in dirty_calls)
    assert all(item["command"] != "init" for item in dirty_calls)

    empty, empty_calls, empty_token = _run_workspace(
        tmp_path / "empty", {}, token=None, state={}, workspace=tmp_path / "plain"
    )
    assert empty.returncode == 0, empty.stderr
    assert any(item["command"] == "init" for item in empty_calls)
    assert all(item["command"] != "clone" for item in empty_calls)
    assert not empty_token.exists()


def test_related_tasks_stay_in_one_transaction(monkeypatch):
    monkeypatch.setattr(get_settings(), "gateway_master_key", Fernet.generate_key().decode())
    with TestClient(app) as client:
        registered = client.post(
            "/api/repositories",
            json={"url": "https://git.example/org/fix-b3.git", "branch": "main", "token": "token-b3"},
        )
        assert registered.status_code == 201, registered.text
        failed = client.post(
            "/api/tasks/related",
            json={
                "task_type": "ISSUE",
                "category_id": "testing-platform",
                "title": "fix-b3-parent",
                "description": "必须一起回滚",
                "acceptance_criteria": [{"content": "没有残留"}],
                "parts": [
                    {"repository_id": registered.json()["id"], "title": "fix-b3-parent"},
                    {"repository_id": "missing-repo", "title": "fix-b3-child"},
                ],
            },
        )
        assert failed.status_code == 422
        listed = client.get("/api/tasks", params={"limit": 100})
        assert listed.status_code == 200
        assert "fix-b3-parent" not in [item["title"] for item in listed.json()]


def test_clone_route_runs_git_off_the_event_loop(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "gateway_master_key", Fernet.generate_key().decode())
    seen: dict[str, object] = {}

    def fake_clone(url, branch, destination, token, runner=None):
        seen["main"] = threading.current_thread() is threading.main_thread()
        seen["url"] = url
        seen["token"] = token
        seen["destination"] = destination

    monkeypatch.setattr("app.api.repository_routes.clone_repository", fake_clone)
    with TestClient(app) as client:
        registered = client.post(
            "/api/repositories",
            json={"url": "https://git.example/org/clone-b3.git", "branch": "main", "token": PAT},
        )
        assert registered.status_code == 201, registered.text
        cloned = client.post(
            f"/api/repositories/{registered.json()['id']}/clone",
            json={"destination": str(tmp_path / "checkout")},
        )
        assert cloned.status_code == 200, cloned.text
        assert PAT not in cloned.text
    assert seen["main"] is False
    assert seen["token"] == PAT
    assert seen["url"] == "https://git.example/org/clone-b3.git"


@pytest.mark.asyncio
async def test_cancel_clears_waiting_reason_and_unknown_failure_does_too(session):
    session.add(_employee())
    task = _task(
        "T-wait",
        status=TaskStatus.WAITING,
        employee_id="E1",
        waiting_reason="TIMEOUT",
        current_session_id="ses",
    )
    session.add(task)
    await session.commit()
    service = RuntimeService(session)
    from app.runtime.session_state import SessionState

    await service.apply_session_state(
        task,
        SessionState(status="FAILED", terminal_reason="session_missing"),
    )
    assert task.waiting_reason == "SESSION_MISSING"
    assert task.status == TaskStatus.WAITING
    task.waiting_reason = "TIMEOUT"
    task.status = TaskStatus.WAITING
    await session.commit()
    canceled = await service._write_canceled_task(
        task,
        cancel_task=True,
        usage_messages=None,
        native_session_count=None,
        forced_offline_cancel=False,
        stop_error=None,
    )
    assert canceled.status == TaskStatus.CANCELED
    assert canceled.waiting_reason is None

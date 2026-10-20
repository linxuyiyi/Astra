"""阶段 5：反向隧道和 ssh 执行没有残留调用方；超时和进程重启后 Task 仍绑定原来的 Session。"""

import asyncio
import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import EmployeeStatus, TaskStatus
from app.models import AgentWorker, Base, RuntimeSession, Task
from app.runtime import recovery
from app.runtime.lifecycle import RuntimeService
from app.runtime.session_connection import connect
from app.state_machine import TaskStateMachine

APP = Path(__file__).resolve().parents[2] / "app"
ROOT = Path(__file__).resolve().parents[4]
BANNED = (
    "ensure_remote_mcp_tunnel",
    "ensure_remote_gateway_tunnel",
    "close_remote_mcp_tunnels",
    "open_reverse_tunnel",
    "SSHReverseTunnel",
    "SSHTunnel",
)
SSH_CALLS = {
    ("app/runtime/environment.py", "SSHSession("),
    ("app/runtime/environment.py", ".open_session("),
    ("app/runtime/maintenance.py", ".open_session("),
    ("app/runtime/remote_provisioning.py", ".open_session("),
    ("app/runtime/ssh_client.py", ".open_session("),
    ("app/runtime/worker_health.py", ".open_session("),
    ("app/executors/opencode_remote.py", ".open_session("),
}


def test_deleted_tunnel_and_ssh_execution_have_no_callers():
    found = set()
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(APP.parent).as_posix()
        for name in BANNED:
            assert name not in text, f"{relative} still mentions {name}"
        assert ".run_remote(" not in text
        for needle in ("SSHSession(", ".open_session(", ".run_remote("):
            if needle in text:
                found.add((relative, needle))
        if path.name == "opencode_remote.py":
            continue
        assert "RemoteOpenCodeExecutor" not in text
        assert "from app.executors.opencode_remote import" not in text
        assert "import app.executors.opencode_remote" not in text
    assert found == SSH_CALLS
    connect_source = inspect.getsource(connect)
    start_source = inspect.getsource(RuntimeService.start)
    assert "open_session" not in connect_source
    assert "opencode_remote" not in connect_source
    assert "RemoteOpenCodeExecutor" not in start_source
    assert "open_session" not in start_source


def test_cli_fallback_agentscope_and_state_machine_stay():
    cli = (APP / "executors" / "opencode_remote.py").read_text(encoding="utf-8")
    assert "class RemoteOpenCodeExecutor(OpenCodeCLIExecutor)" in cli
    assert "await self.runtime.open_session(self.environment)" in cli

    assert "Runtime transport availability never changes task status." in TaskStateMachine.__doc__
    assert set(TaskStateMachine.transitions) == set(TaskStatus)

    agentscope = (ROOT / "services" / "nexus-api" / "nexus_api" / "agentscope.py").read_text(encoding="utf-8")
    assert "import agentscope" not in agentscope
    assert "from agentscope" not in agentscope
    launcher = (ROOT / "services" / "agent-service" / "agent_service.py").read_text(encoding="utf-8")
    head = launcher.split("def main()", 1)[0]
    assert "import agentscope" not in head
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import agentscope" not in text
        assert "from agentscope" not in text


class _TimeoutClient:
    def events(self, _directory):
        async def _gen():
            await asyncio.Event().wait()
            yield {}

        return _gen()

    async def status(self, *_args, **_kwargs):
        raise TimeoutError("network timeout")

    async def create_session(self, *_args, **_kwargs):
        raise AssertionError("超时和重启不能新建 Session")


async def _seed(factory, workspace: str):
    async with factory() as session:
        session.add(
            AgentWorker(
                id="E1",
                name="Engineer",
                role="build",
                status=EmployeeStatus.WORKING,
                enabled=True,
                auto_claim=True,
                auto_execute=False,
                task_types=["ISSUE"],
                category_ids=["testing"],
                current_task_count=1,
            )
        )
        session.add(
            Task(
                id="T1",
                title="running",
                task_type="ISSUE",
                category_id="testing",
                status=TaskStatus.RUNNING,
                employee_id="E1",
                claimed_at=datetime.now(UTC),
                current_session_id="ses",
                workspace=workspace,
            )
        )
        session.add(
            RuntimeSession(
                id="ses",
                task_id="T1",
                employee_id="E1",
                workspace=workspace,
                environment={},
                status="RUNNING",
            )
        )
        await session.commit()


async def test_network_timeout_and_process_restart_keep_the_same_session(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stage5.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _seed(factory, str(tmp_path))

    async def _resolve(_session, _task):
        return None

    @asynccontextmanager
    async def _connect(*_args, **_kwargs):
        yield _TimeoutClient()

    monkeypatch.setattr(recovery, "SessionLocal", factory)
    monkeypatch.setattr(recovery, "resolve_environment", _resolve)
    monkeypatch.setattr(recovery, "connect", _connect)

    watcher = asyncio.create_task(recovery.watch_session("T1"))
    recorded = None
    for _ in range(40):
        async with factory() as session:
            task = await session.get(Task, "T1")
            binding = await session.get(RuntimeSession, "ses")
            if task.runtime_connection == "UNAVAILABLE":
                recorded = (
                    task.status,
                    task.current_session_id,
                    binding.status,
                    binding.terminal_reason,
                )
                break
        await asyncio.sleep(0.05)
    watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)
    assert recorded == (TaskStatus.RUNNING, "ses", "UNKNOWN", "transport_unknown")

    connects = []

    @asynccontextmanager
    async def _restart_connect(*_args, **_kwargs):
        connects.append("connect")
        yield _TimeoutClient()

    monkeypatch.setattr(recovery, "connect", _restart_connect)
    monkeypatch.setattr(recovery, "SUPERVISOR_INTERVAL_SECONDS", 0.01)
    supervisor = asyncio.create_task(recovery.supervise_sessions())
    for _ in range(40):
        if connects:
            break
        await asyncio.sleep(0.05)
    supervisor.cancel()
    await asyncio.gather(supervisor, return_exceptions=True)

    async with factory() as session:
        task = await session.get(Task, "T1")
        rows = (await session.scalars(select(RuntimeSession))).all()
    assert connects
    assert task.status == TaskStatus.RUNNING
    assert task.current_session_id == "ses"
    assert [row.id for row in rows] == ["ses"]
    await engine.dispose()

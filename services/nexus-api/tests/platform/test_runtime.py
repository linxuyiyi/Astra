import io
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import SessionLocal, init_database
from app.domain import Priority, TaskStatus, TaskType
from app.executors.base import ToolUsageResult
from app.executors.process import windows_batch_command
from app.executors.usage import parse_execution_seconds, parse_opencode_usage
from app.models import Base, Event, Task, TaskCategory
from app.runtime.scout import TaskScout
from app.runtime.ssh_client import SSHSession
from app.schemas import TaskCreate
from app.services import TaskService
from app.sources.base import ExternalTask, TaskSource


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def new_task(**overrides) -> TaskCreate:
    payload = {
        "task_type": TaskType.ISSUE,
        "category_id": "testing-platform",
        "title": "修复接口问题",
        "description": "定位并解决接口异常",
        "acceptance_criteria": [{"content": "接口恢复正常"}],
        "priority": Priority.HIGH,
    }
    payload.update(overrides)
    return TaskCreate(**payload)


def fake_agent_launcher(tmp_path: Path, python_source: str, name: str) -> Path:
    script = tmp_path / f"{name}.py"
    script.write_text(python_source, encoding="utf-8")
    if os.name == "nt":
        launcher = tmp_path / f"{name}.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = tmp_path / name
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return launcher


async def test_create_task_records_ready_event(session):
    task = await TaskService(session).create(new_task())

    assert task.status == TaskStatus.PENDING
    assert task.current_progress == "等待领取"
    events = (
        (await session.execute(select(Event).where(Event.task_id == task.id).order_by(Event.id)))
        .scalars()
        .all()
    )
    assert [event.event_type for event in events] == ["TASK_CREATED"]


async def test_add_event_can_join_caller_transaction_without_implicit_commit(session):
    task = await TaskService(session).create(new_task())
    task_id = task.id

    await TaskService(session).add_event(
        task,
        "TASK_ASSIGNED",
        "work",
        "尚未提交的领取事件",
        commit=False,
    )
    await session.rollback()

    events = (
        (await session.execute(select(Event).where(Event.task_id == task_id).order_by(Event.id)))
        .scalars()
        .all()
    )
    assert [event.event_type for event in events] == ["TASK_CREATED"]


def test_dispatch_starts_claimed_task(monkeypatch):
    """已领取任务直接启动，不再有自动执行开关。"""
    from types import SimpleNamespace

    from app.runtime import celery_app

    started: list[str] = []

    class WorkerSession:
        async def get(self, model, key):
            return SimpleNamespace()

    @asynccontextmanager
    async def worker_scope():
        yield WorkerSession()

    async def no_init() -> None:
        return None

    async def claim_once(self):
        return SimpleNamespace(id="T-auto", employee_id="astra-dispatch")

    async def start(self, task_id, **kwargs):
        started.append(task_id)

    monkeypatch.setattr(celery_app, "validate_database", no_init)
    monkeypatch.setattr(celery_app, "worker_session", worker_scope)
    monkeypatch.setattr(celery_app.Dispatcher, "claim_once", claim_once)
    monkeypatch.setattr(celery_app.RuntimeService, "start", start)

    assert celery_app.dispatch() == "T-auto"
    assert started == ["T-auto"]


class FakeSource(TaskSource):
    id = "nr_test_system"

    async def discover_tasks(self):
        return [
            ExternalTask(
                external_task_id="EXT-1",
                title="外部需求",
                task_type="REQUIREMENT",
                category_id="testing-platform",
                description="处理外部系统需求",
                acceptance_criteria=["提供处理结果"],
            )
        ]

    async def get_task(self, external_task_id):
        return (await self.discover_tasks())[0]

    async def update_task(self, external_task_id, result):
        return None

    async def heartbeat(self):
        return True


async def test_scout_is_idempotent(session):
    session.add(
        TaskCategory(
            id="testing-platform",
            name="测试平台",
            description="测试平台相关任务",
        )
    )
    await session.commit()
    scout = TaskScout(session, [FakeSource()])
    first = await scout.poll_once()
    second = await scout.poll_once()

    assert len(first) == 1
    assert second == []
    assert await session.scalar(select(func.count(Task.id))) == 1


def test_opencode_usage_parser_normalizes_model_object():
    _tokens, _tools, model = parse_opencode_usage(
        [
            {
                "info": {
                    "role": "assistant",
                    "tokens": {"input": 1},
                    "model": {"providerID": "deepseek", "modelID": "deepseek-v4-flash"},
                }
            }
        ]
    )

    assert model == "deepseek/deepseek-v4-flash"


def test_parse_execution_seconds_accepts_milliseconds_and_seconds():
    assert (
        parse_execution_seconds(
            [
                {
                    "info": {
                        "role": "assistant",
                        "time": {"created": 1_700_000_000_000, "completed": 1_700_000_004_000},
                    }
                }
            ]
        )
        == 4
    )
    assert (
        parse_execution_seconds(
            [
                {
                    "info": {
                        "role": "assistant",
                        "time": {"created": 1_700_000_000, "completed": 1_700_000_002},
                    }
                }
            ]
        )
        == 2
    )
    assert parse_execution_seconds([{"info": {"role": "user"}}]) == 0


def test_opencode_usage_parser_uses_actual_json_events():
    tokens, tools, model = parse_opencode_usage(
        [
            {
                "type": "step_finish",
                "part": {
                    "modelID": "deepseek/deepseek-v4-flash",
                    "tokens": {
                        "input": 120,
                        "output": 30,
                        "cache": {"read": 400, "write": 5},
                    },
                },
            },
            {
                "type": "tool",
                "part": {
                    "id": "call-1",
                    "tool": "bash",
                    "state": {"status": "completed", "time": {"start": 100, "end": 145}},
                },
            },
        ]
    )

    assert tokens.total_tokens == 555
    assert tokens.llm_call_count == 1
    assert model == "deepseek/deepseek-v4-flash"
    assert tools == [
        ToolUsageResult(
            name="bash",
            call_count=1,
            success_count=1,
            failure_count=0,
            duration_ms=45,
        )
    ]


async def test_paramiko_exec_channel_closes_stdin_and_streams_output():
    class FakeChannel:
        closed = False

        def __init__(self):
            self.command = ""
            self.stdin_closed = False

        def exec_command(self, command):
            self.command = command

        def shutdown_write(self):
            self.stdin_closed = True

        def makefile(self, mode, size):
            return io.BytesIO(b'{"type":"text","text":"ok"}\n')

        def makefile_stderr(self, mode, size):
            return io.BytesIO(b"")

        def recv_exit_status(self):
            return 0

        def close(self):
            self.closed = True

    channel = FakeChannel()

    class FakeTransport:
        def is_active(self):
            return True

        def open_session(self, timeout):
            return channel

    class FakeClient:
        def get_transport(self):
            return FakeTransport()

        def close(self):
            return None

    ssh = SSHSession(
        host="example.internal",
        port=22,
        username="nexus",
        password="secret",
    )
    ssh._client = FakeClient()

    process = await ssh.open_exec("agent run")
    stdout = await process.stdout.readline()
    exit_code = await process.wait()

    assert channel.command == "agent run"
    assert channel.stdin_closed is True
    assert stdout.endswith(b"\n")
    assert exit_code == 0


def test_ssh_session_has_no_forwarding_tunnel():
    assert not hasattr(SSHSession, "open_tunnel")
    assert not hasattr(SSHSession, "open_reverse_tunnel")


def test_windows_batch_command_keeps_batch_path_and_arguments_separate():
    command = windows_batch_command(
        r"C:\Program Files\OpenCode\opencode.cmd",
        ["run", "--dir", r"D:\中文项目\workspace with spaces"],
        r"C:\Windows\System32\cmd.exe",
    )

    assert command == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/c",
        r"C:\Program Files\OpenCode\opencode.cmd",
        "run",
        "--dir",
        r"D:\中文项目\workspace with spaces",
    ]


def test_windows_batch_command_does_not_prequote_multiline_arguments():
    command = windows_batch_command(
        r"C:\OpenCode\opencode.cmd",
        ["run", "第一行\n第二行 & 由 subprocess 负责转义"],
    )

    assert command[-1] == "第一行\n第二行 & 由 subprocess 负责转义"


async def test_stream_poll_observes_external_commits_and_releases_connection():
    from app.api.routes import _stream_event_batch
    from app.database import engine

    await init_database()
    cursor, _ = await _stream_event_batch(None, None)
    assert engine.pool.checkedout() == 0
    async with SessionLocal() as writer:
        event = Event(event_type="STREAM_TEST", phase="runtime", message="new commit")
        writer.add(event)
        await writer.commit()
        event_id = event.id
    next_cursor, events = await _stream_event_batch(None, cursor)
    assert next_cursor >= event_id
    assert any(event.id == event_id for event in events)
    assert engine.pool.checkedout() == 0

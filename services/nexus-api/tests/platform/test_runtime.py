import io
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import SessionLocal, init_database
from app.domain import EmployeeStatus, EnvironmentStatus, Priority, TaskStatus, TaskType
from app.executors.base import ToolUsageResult
from app.executors.opencode_cli import OpenCodeCLIExecutor
from app.executors.process import windows_batch_command
from app.executors.usage import parse_execution_seconds, parse_opencode_usage
from app.models import (
    AgentWorker,
    Base,
    Event,
    OpenCodeExecutionEnvironment,
    Task,
    TaskCategory,
)
from app.runtime.dispatcher import Dispatcher
from app.runtime.environment import ExecutionEnvironmentRuntime
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
        "completion_policy": "AUTO",
        "repository_id": "repo-1",
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
    assert [event.event_type for event in events] == ["TASK_CREATED", "TASK_PENDING"]


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
    assert [event.event_type for event in events] == ["TASK_CREATED", "TASK_PENDING"]


async def test_dispatcher_matches_type_and_category_and_limits_one_task(session, tmp_path):
    workspace = tmp_path / "employee-workspace"
    workspace.mkdir()
    employee = AgentWorker(
        id="astra-test",
        name="Astra Test",
        role="测试平台员工",
        status=EmployeeStatus.IDLE,
        task_types=["ISSUE"],
        category_ids=["testing-platform"],
        auto_execute=False,
    )
    session.add(employee)
    session.add(
        OpenCodeExecutionEnvironment(
            employee_id=employee.id,
            mode="LOCAL",
            name="员工固定工作空间",
            opencode_path="opencode",
            model_name="deepseek/deepseek-v4-flash",
            workspace_root=str(workspace),
            repository_id="repo-1",
            status=EnvironmentStatus.AVAILABLE,
        )
    )
    await session.commit()
    first = await TaskService(session).create(new_task(title="匹配任务"))
    first_id = first.id
    await TaskService(session).create(new_task(title="第二个任务"))

    claimed = await Dispatcher(session).claim_once()
    claimed_id = claimed.id if claimed else None
    second_claim = await Dispatcher(session).claim_once()
    if claimed:
        await session.refresh(claimed)

    assert claimed_id == first_id
    assert claimed.status == TaskStatus.PENDING
    assert claimed.employee_id == "astra-test"
    assert claimed.claimed_at is not None
    assert claimed.execution_environment_snapshot["workspace_root"] == str(workspace)
    assert claimed.execution_environment_snapshot["model_name"] == "deepseek/deepseek-v4-flash"
    assert second_claim is None
    await session.refresh(employee)
    assert employee.current_task_count == 1


async def test_dispatcher_claims_task_assigned_to_employee_without_auto_claim(session, tmp_path):
    """人工指定员工后必须自动分配并启动，不受 auto_claim 影响。"""

    workspace = tmp_path / "employee-workspace"
    workspace.mkdir()
    employee = AgentWorker(
        id="astra-assigned",
        name="Astra Assigned",
        role="指定员工",
        status=EmployeeStatus.IDLE,
        task_types=["ISSUE"],
        category_ids=["testing-platform"],
        auto_claim=False,
        auto_execute=True,
    )
    session.add(employee)
    session.add(
        OpenCodeExecutionEnvironment(
            employee_id=employee.id,
            mode="LOCAL",
            name="员工固定工作空间",
            opencode_path="opencode",
            model_name="opencode/space-bunny-free",
            workspace_root=str(workspace),
            repository_id="repo-1",
            status=EnvironmentStatus.AVAILABLE,
        )
    )
    await session.commit()
    task = await TaskService(session).create(new_task(title="指定员工任务", employee_id="astra-assigned"))
    task_id = task.id

    claimed = await Dispatcher(session).claim_once()
    assert claimed is not None
    assert claimed.id == task_id
    assert claimed.employee_id == "astra-assigned"
    assert claimed.claimed_at is not None
    assert claimed.execution_environment_snapshot["workspace_root"] == str(workspace)
    assert claimed.execution_environment_snapshot["model_name"] == "opencode/space-bunny-free"
    # 下一轮只把同一任务作为待启动的已分配任务交回，不会改派他人。
    recovered = await Dispatcher(session).claim_once()
    assert recovered is not None
    assert recovered.id == task_id
    assert recovered.employee_id == "astra-assigned"


async def test_dispatcher_keeps_unclaimed_pool_on_auto_claim(session, tmp_path):
    """未指定员工的任务仍只由开启自动认领的员工领取。"""

    workspace = tmp_path / "employee-workspace"
    workspace.mkdir()
    session.add(
        AgentWorker(
            id="astra-manual",
            name="Astra Manual",
            role="手动员工",
            status=EmployeeStatus.IDLE,
            task_types=["ISSUE"],
            category_ids=["testing-platform"],
            auto_claim=False,
            auto_execute=True,
        )
    )
    session.add(
        OpenCodeExecutionEnvironment(
            employee_id="astra-manual",
            mode="LOCAL",
            name="员工固定工作空间",
            opencode_path="opencode",
            workspace_root=str(workspace),
            status=EnvironmentStatus.AVAILABLE,
        )
    )
    await session.commit()
    await TaskService(session).create(new_task(title="无人指定任务"))

    assert await Dispatcher(session).claim_once() is None


async def _assigned_task(session, tmp_path, *, auto_execute: bool, employee_id: str):
    workspace = tmp_path / "employee-workspace"
    workspace.mkdir(exist_ok=True)
    session.add(
        AgentWorker(
            id=employee_id,
            name="Dispatch Employee",
            role="调度员工",
            status=EmployeeStatus.IDLE,
            task_types=["ISSUE"],
            category_ids=["testing-platform"],
            auto_claim=False,
            auto_execute=auto_execute,
        )
    )
    session.add(
        OpenCodeExecutionEnvironment(
            employee_id=employee_id,
            mode="LOCAL",
            name="员工固定工作空间",
            opencode_path="opencode",
            workspace_root=str(workspace),
            repository_id="repo-1",
            status=EnvironmentStatus.AVAILABLE,
        )
    )
    await session.commit()
    return await TaskService(session).create(new_task(employee_id=employee_id))


@pytest.mark.parametrize(
    ("auto_execute", "expected_starts"),
    [(False, []), (True, ["T-auto"])],
)
def test_dispatch_respects_employee_auto_execute(monkeypatch, auto_execute, expected_starts):
    """人工指定员工即被领取；是否自动执行仍由员工的自动执行开关决定。"""

    from types import SimpleNamespace

    from app.runtime import celery_app

    started: list[str] = []

    class WorkerSession:
        async def get(self, model, key):
            return SimpleNamespace(auto_execute=auto_execute)

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
    assert started == expected_starts


async def test_claimed_task_without_session_is_offered_again_for_start_retry(session, tmp_path):
    """启动失败后任务仍无 Session，下一轮必须原样交回同一任务重试启动。"""

    task = await _assigned_task(session, tmp_path, auto_execute=True, employee_id="astra-retry")
    task_id = task.id
    claimed = await Dispatcher(session).claim_once()
    assert claimed is not None and claimed.id == task_id
    # 模拟 start() 失败：没有 Session、没有待确认输入、没有运行时错误。
    assert claimed.current_session_id is None
    assert claimed.pending_message_id is None
    assert claimed.runtime_error is None

    retried = await Dispatcher(session).claim_once()
    assert retried is not None
    assert retried.id == task_id
    assert retried.employee_id == "astra-retry"
    assert retried.execution_environment_snapshot["workspace_root"] == str(tmp_path / "employee-workspace")


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


async def test_local_environment_check_reports_binary_and_workspace(tmp_path: Path):
    binary_dir = tmp_path / "Agent Program Files"
    binary_dir.mkdir()
    binary = fake_agent_launcher(binary_dir, 'print("fake-opencode-1.0")\n', "fake-opencode")
    environment = OpenCodeExecutionEnvironment(
        employee_id="astra-check",
        opencode_server_port=54198,
        mode="LOCAL",
        name="本地检查",
        opencode_path=str(binary),
        workspace_root=str(tmp_path / "workspaces"),
    )

    (tmp_path / "workspaces").mkdir()
    checks = await ExecutionEnvironmentRuntime().test(environment)

    assert [item.name for item in checks] == [
        "Agent 命令存在",
        "Agent 命令可启动",
        "Workspace 可用",
        "OpenCode Server",
    ]
    assert all(item.passed for item in checks)


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


async def test_opencode_permission_rejection_is_blocked_even_with_zero_exit(tmp_path: Path):
    binary = fake_agent_launcher(
        tmp_path,
        "import sys\n"
        'print(\'{"type":"text","part":{"text":"无法写入目标文件"}}\')\n'
        'print("permission requested: external_directory; auto-rejecting", file=sys.stderr)\n',
        "permission-rejected-opencode",
    )
    task = Task(
        id="T-PERMISSION-REJECTED",
        source="manual",
        task_type="ISSUE",
        category_id="testing-platform",
        title="验证权限拒绝识别",
        description="创建文件",
        acceptance_criteria=[{"id": "AC-01", "content": "文件已创建"}],
        priority="LOW",
        completion_policy="AUTO",
        status=TaskStatus.RUNNING,
    )

    result = await OpenCodeCLIExecutor(opencode_binary=str(binary)).start(task, tmp_path)

    assert result.exit_code == 0
    assert result.status == "WAITING_INPUT"


def test_employee_model_is_passed_to_agent_command(tmp_path: Path):
    task = Task(
        id="T-EMPLOYEE-MODEL",
        source="manual",
        task_type="REQUIREMENT",
        category_id="testing-platform",
        title="验证员工模型配置",
        description="模型应作为 Agent 启动参数",
        acceptance_criteria=[{"id": "AC-01", "content": "使用指定模型"}],
        priority="LOW",
        completion_policy="AUTO",
        status=TaskStatus.RUNNING,
    )
    executor = OpenCodeCLIExecutor(
        opencode_binary="opencode",
        model_name="deepseek/deepseek-v4-flash",
    )

    command = executor._command(task, tmp_path)

    model_index = command.index("--model")
    assert command[model_index + 1] == "deepseek/deepseek-v4-flash"


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

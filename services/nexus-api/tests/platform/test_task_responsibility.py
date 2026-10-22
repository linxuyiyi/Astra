from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import RuntimeSessionStatus, TaskStatus
from app.models import AgentWorker, Base, Event, RuntimeSession, Task, TaskWakeOutbox
from app.runtime.lifecycle import RuntimeService
from app.runtime.opencode_model import direct_model, gateway_model, route_model_id
from app.schemas import TaskCreate
from app.services import TaskService
from app.task_tools import TaskTools
from tests.platform.test_runtime import new_task


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def _create(**overrides) -> TaskCreate:
    return new_task(**overrides)


async def test_same_idempotency_key_returns_the_existing_task(session):
    service = TaskService(session)
    first = await service.create(
        _create(responsible_employee_id="emp_1", source_session_id="ses_1", idempotency_key="same-request")
    )
    second = await service.create(
        _create(title="另一标题", responsible_employee_id="emp_1", idempotency_key="same-request")
    )

    assert second.id == first.id
    assert second.title == first.title
    events = (await session.scalars(select(Event).where(Event.task_id == first.id))).all()
    assert [event.event_type for event in events] == ["TASK_CREATED", "TASK_PENDING"]


async def test_failure_and_recovery_coalesce_pending_wakes(session):
    task = await TaskService(session).create(
        _create(responsible_employee_id="emp_1", source_session_id="ses_1")
    )
    await TaskService(session).transition(task, TaskStatus.FAILED)
    pending = list(
        (await session.scalars(select(TaskWakeOutbox).where(TaskWakeOutbox.status == "pending"))).all()
    )
    assert len(pending) == 1
    assert "nexus_task_get" in pending[0].hint

    await TaskService(session).transition(task, TaskStatus.RUNNING)
    rows = list((await session.scalars(select(TaskWakeOutbox))).all())
    assert sorted(row.status for row in rows) == ["pending", "superseded"]
    assert task.status == TaskStatus.RUNNING


async def test_task_without_responsible_employee_does_not_wake(session):
    task = await TaskService(session).create(_create())
    await TaskService(session).transition(task, TaskStatus.FAILED)
    rows = list((await session.scalars(select(TaskWakeOutbox))).all())
    assert rows == []


async def test_illegal_control_leaves_concluded_status(session):
    task = await TaskService(session).create(_create(responsible_employee_id="emp_1"))
    task.status = TaskStatus.SUCCEEDED.value
    await session.commit()
    tools = TaskTools(session)

    paused = await tools.pause(task.id)
    terminated = await tools.terminate(task.id)
    retried = await tools.retry(task.id)

    assert paused["ok"] is False
    assert terminated["ok"] is False
    assert retried["ok"] is False
    current = await session.get(Task, task.id)
    assert current.status == TaskStatus.SUCCEEDED.value


async def test_retry_pending_does_not_start(session):
    task = await TaskService(session).create(_create())
    result = await TaskTools(session).retry(task.id)
    assert result["ok"] is False
    current = await session.get(Task, task.id)
    assert current.status == TaskStatus.PENDING.value


async def test_refused_start_returns_ok_false_after_rollback(session):
    from app.task_tools import nexus_employee_id

    token = nexus_employee_id.set("emp_1")
    try:
        task = await TaskService(session).create(_create(responsible_employee_id="emp_1"))
        result = await TaskTools(session).start(task.id)
    finally:
        nexus_employee_id.reset(token)

    assert result["ok"] is False
    assert result["task_id"] == task.id
    assert "请先分配已配置运行环境的员工" in result["error"]
    current = await session.get(Task, task.id)
    assert current.status == TaskStatus.PENDING.value


async def test_paused_finished_turn_stays_waiting(session):
    started = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    session.add(AgentWorker(id="worker-1", name="执行员工", role="工程"))
    session.add(
        Task(
            id="T-paused",
            task_type="ISSUE",
            title="登录修复",
            status=TaskStatus.WAITING.value,
            employee_id="worker-1",
            workspace="/tmp/task-ws",
            current_session_id="ses_done",
            last_message_id="msg_done",
            turn_started_at=started,
        )
    )
    session.add(
        RuntimeSession(
            id="ses_done",
            task_id="T-paused",
            employee_id="worker-1",
            workspace="/tmp/task-ws",
            environment={},
            status=RuntimeSessionStatus.SUCCEEDED.value,
        )
    )
    await session.commit()

    class IdleClient:
        def __init__(self) -> None:
            self.history_reads = 0

        async def status(self, _directory, _session_id):
            return "idle"

        async def children(self, _directory, _session_id):
            return []

        async def questions(self, _directory):
            return []

        async def permissions(self, _directory):
            return []

        async def messages(self, _directory, _session_id):
            self.history_reads += 1
            raise AssertionError("暂停后的已完成回合不应再读历史")

    client = IdleClient()
    refreshed = await RuntimeService(session).refresh("T-paused", client=client)

    assert client.history_reads == 0
    assert refreshed.status == TaskStatus.WAITING.value


def test_task_mcp_structured_output_accepts_dict_annotations():
    from app.task_mcp import build_mcp_server

    server = build_mcp_server()
    tools = server._tool_manager.list_tools()
    assert {tool.name for tool in tools} >= {"nexus_task_create", "nexus_task_get", "nexus_task_start"}
    assert all(tool.output_schema for tool in tools)


async def test_task_tools_require_owner_and_hide_transport_errors(session, monkeypatch):
    from app.executors.errors import OpenCodeServerUnavailable
    from app.task_tools import nexus_employee_id

    mine = nexus_employee_id.set("emp_1")
    try:
        owned = await TaskService(session).create(_create(title="自己的", responsible_employee_id="emp_1"))
        other = await TaskService(session).create(_create(title="别人的", responsible_employee_id="emp_2"))
        listed = await TaskTools(session).list()
        assert listed["ok"] is True
        assert [item["task_id"] for item in listed["tasks"]] == [owned.id]
        hidden = await TaskTools(session).get(other.id)
        assert hidden == {"ok": False, "error": "任务不存在"}
        updated = await TaskTools(session).update(other.id, title="改掉")
        assert updated["ok"] is False
        current = await session.get(Task, other.id)
        assert current.title == "别人的"

        async def explode(_self, _task_id):
            raise OpenCodeServerUnavailable("ssh root@secret-host password=hunter2")

        monkeypatch.setattr("app.runtime.lifecycle.RuntimeService.start", explode)
        failed = await TaskTools(session).start(owned.id)
        assert failed["ok"] is False
        assert failed["failure_code"] == "SSH_AUTH_FAILED"
        assert failed["error"] == "SSH 认证失败"
        rendered = str(failed)
        assert "hunter2" not in rendered
        assert "secret-host" not in rendered

        async def time_out(_self, _task_id):
            raise TimeoutError("password=hunter2 at 10.1.2.3")

        monkeypatch.setattr("app.runtime.lifecycle.RuntimeService.start", time_out)
        timed = await TaskTools(session).start(owned.id)
        assert timed["failure_code"] == "EXECUTION_TIMEOUT"
        assert "hunter2" not in str(timed)

        async def conflict(_self, _task_id):
            raise RuntimeError("端口冲突：该 Serve 属于另一员工或工作空间 password=hunter2")

        monkeypatch.setattr("app.runtime.lifecycle.RuntimeService.start", conflict)
        blocked = await TaskTools(session).start(owned.id)
        assert blocked["failure_code"] == "WORKSPACE_CONFLICT"
        assert "hunter2" not in str(blocked)

        async def offline(_self, _task_id):
            raise OpenCodeServerUnavailable("connection refused")

        monkeypatch.setattr("app.runtime.lifecycle.RuntimeService.start", offline)
        unreachable = await TaskTools(session).start(owned.id)
        assert unreachable["failure_code"] == "ENVIRONMENT_UNREACHABLE"
        assert unreachable["hint"]

        async def unknown(_self, _task_id):
            raise RuntimeError("token=abc secret-host")

        monkeypatch.setattr("app.runtime.lifecycle.RuntimeService.start", unknown)
        generic = await TaskTools(session).start(owned.id)
        assert generic["failure_code"] == "ENVIRONMENT_UNAVAILABLE"
        assert generic["error"] == "执行环境暂时不可用"
        assert "abc" not in str(generic)
        assert "secret-host" not in str(generic)
    finally:
        nexus_employee_id.reset(mine)

    anonymous = await TaskTools(session).list()
    assert anonymous["ok"] is False
    assert anonymous["tasks"] == []


async def test_running_replaces_waiting_progress(session):
    task = await TaskService(session).create(_create())
    assert task.current_progress == "等待领取"
    await TaskService(session).transition(task, TaskStatus.RUNNING)
    assert task.current_progress == "执行中"


def test_opencode_model_names():
    assert route_model_id(None) == "default"
    assert route_model_id("deepseek/deepseek-flash") == "deepseek-flash"
    assert route_model_id("nexus-gateway/deepseek-flash") == "deepseek-flash"
    assert gateway_model(None) == "nexus-gateway/default"
    assert gateway_model("deepseek/deepseek-flash") == "nexus-gateway/deepseek-flash"
    assert direct_model("opencode/glm-5.1", None) == "opencode/glm-5.1"
    assert direct_model(None, "deepseek/deepseek-flash") == "deepseek/deepseek-flash"


async def test_group_task_cannot_override_server_side_source(session):
    from app.task_tools import nexus_channel_id, nexus_session_id

    channel = nexus_channel_id.set("ch_group")
    session_token = nexus_session_id.set("ses_group")
    try:
        for overrides in (
            {"source_channel_id": "ch_other"},
            {"source_session_id": "ses_other"},
            {"source_conversation_id": "conv-other"},
        ):
            result = await TaskTools(None).create(
                task_type="ENGINEERING",
                category_id="cat",
                title="标题",
                description="描述",
                **overrides,
            )
            assert result == {"ok": False, "error": "不能覆盖服务端群调用来源"}
        allowed = await TaskTools(session).create(
            task_type="ENGINEERING",
            category_id="cat",
            title="标题",
            description="描述",
            source_channel_id="ch_group",
            source_session_id="ses_group",
        )
    finally:
        nexus_session_id.reset(session_token)
        nexus_channel_id.reset(channel)
    # 允许的值继续进入分类校验，而不是被来源检查误伤。
    assert allowed == {"ok": False, "error": "请选择已启用的任务分类"}

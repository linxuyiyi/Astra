from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import TaskStatus
from app.models import Base, Event, Task
from app.runtime.opencode_model import direct_model, gateway_model, route_model_id
from app.runtime.presentation import TASK_STATUS
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
        from nexus_api.db_models import employees

        await value.execute(
            employees.insert().values(
                id="qa-owner",
                name="QA",
                domain="QA",
                description="",
                instructions="",
                avatar="robot",
                enabled=1,
                knowledge_json="[]",
                skills_json="[]",
                tools_json="[]",
                created_at="t",
                updated_at="t",
            )
        )
        await value.commit()
        yield value
    await engine.dispose()


def _create(**overrides) -> TaskCreate:
    return new_task(**overrides)


@pytest.mark.parametrize("state", list(TaskStatus))
async def test_employee_task_results_supply_chinese_labels_and_original_codes(session, state):

    task = await TaskService(session).create(_create(origin_employee_id="emp_1"))
    task.status = state.value
    await session.commit()
    tools = TaskTools(session)
    listed = await tools.list()
    responses = [listed["tasks"][0]]
    for method in (tools.get, tools.logs, tools.result, tools.summary):
        responses.append(await method(task.id))
    responses.append(tools._refuse(task, "测试拒绝"))
    for result in responses:
        assert result["status"] == state.value
        assert result["runtime_status_label"] == TASK_STATUS[state.value]["label"]


async def test_same_source_creates_distinct_tasks(session):
    service = TaskService(session)
    first = await service.create(
        _create(origin_employee_id="emp_1", source="group_chat", external_task_id="group")
    )
    second = await service.create(
        _create(origin_employee_id="emp_1", source="group_chat", external_task_id="group")
    )
    assert second.id != first.id


async def test_illegal_control_leaves_concluded_status(session):
    task = await TaskService(session).create(_create(origin_employee_id="emp_1"))
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

    task = await TaskService(session).create(_create(origin_employee_id="emp_1"))
    result = await TaskTools(session).start(task.id)

    assert result["ok"] is False
    assert result["task_id"] == task.id
    assert "Worker" in result["error"]
    current = await session.get(Task, task.id)
    assert current.status == TaskStatus.PENDING.value


def test_task_mcp_structured_output_accepts_dict_annotations():
    from app.task_mcp import build_mcp_server

    server = build_mcp_server()
    tools = server._tool_manager.list_tools()
    assert {tool.name for tool in tools} >= {"nexus_task_create", "nexus_task_get", "nexus_task_start"}
    assert all(tool.output_schema for tool in tools)


async def test_task_tools_use_explicit_filters_and_hide_transport_errors(session, monkeypatch):
    from app.executors.errors import OpenCodeServerUnavailable

    owned = await TaskService(session).create(_create(title="自己的", origin_employee_id="emp_1"))
    other = await TaskService(session).create(_create(title="别人的", origin_employee_id="emp_2"))
    listed = await TaskTools(session).list(origin_employee_id="emp_1")
    assert listed["ok"] is True
    assert [item["task_id"] for item in listed["tasks"]] == [owned.id]
    hidden = await TaskTools(session).get(other.id)
    assert hidden["ok"] is True
    updated = await TaskTools(session).update(other.id, title="改掉")
    assert updated["ok"] is True
    current = await session.get(Task, other.id)
    assert current.title == "改掉"

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

    anonymous = await TaskTools(session).list()
    assert anonymous["ok"] is True
    assert len(anonymous["tasks"]) == 2


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


async def test_task_categories_only_return_enabled_choices(session):
    from app.models import TaskCategory

    session.add_all(
        [
            TaskCategory(
                id="visible-test", name="可用测试分类", description="测试说明", enabled=True, sort_order=1
            ),
            TaskCategory(id="hidden-test", name="停用分类", enabled=False),
        ]
    )
    await session.commit()
    result = await TaskTools(session).categories()
    assert result["ok"] is True
    assert any(c["id"] == "visible-test" and c["description"] == "测试说明" for c in result["categories"])
    assert all(c["id"] != "hidden-test" and c["enabled"] for c in result["categories"])


async def test_task_mcp_exposes_categories_and_enum_types():
    from app.task_mcp import build_mcp_server

    tools = {t.name: t for t in await build_mcp_server().list_tools()}
    assert "nexus_task_categories" in tools
    assert tools["nexus_task_create"].input_schema["properties"]["task_type"]["enum"] == [
        "ISSUE",
        "REQUIREMENT",
    ]


async def test_summary_uses_real_review_for_the_requested_task(session):
    from app.models import ExperienceReviewJob, KnowledgeItem

    task = await TaskService(session).create(
        _create(origin_employee_id="emp_1", source="group_chat", external_task_id="group-a")
    )
    other = await TaskService(session).create(_create(origin_employee_id="emp_2"))
    tools = TaskTools(session)
    assert (await tools.summary(task.id))["review"] is None
    event = await session.scalar(select(Event).where(Event.task_id == task.id))
    now = datetime.now(UTC)
    session.add(
        ExperienceReviewJob(
            id="review-real",
            confirmation_event_id=event.id,
            review_key="review-real",
            task_id=task.id,
            task_id_snapshot=task.id,
            root_session_id="native-1",
            confirmed_at=now,
            status="SUCCEEDED",
            phase="WRITING",
            candidate_ids=["know-own", "know-other"],
            lease_token="never-expose-lease",
            source_snapshot={"runtime_context": "never-expose-context"},
        )
    )
    session.add_all(
        [
            KnowledgeItem(
                id="know-own",
                title="本任务经验",
                source_task_id=task.id,
                status="candidate",
                summary="尚待审核",
                created_by="tester",
                updated_by="tester",
            ),
            KnowledgeItem(
                id="know-other",
                title="其他任务经验",
                source_task_id=other.id,
                created_by="tester",
                updated_by="tester",
            ),
        ]
    )
    await session.commit()
    result = await tools.summary(task.id)
    assert result["review"]["status"] == "SUCCEEDED"
    assert [k["id"] for k in result["candidates"]] == ["know-own"]
    assert result["candidates"][0]["status"] == "candidate"
    assert result["candidates"][0]["url"] == "/library?knowledge=know-own"
    assert "never-expose" not in str(result)
    assert (await tools.summary(other.id))["ok"]


async def test_summary_is_registered_as_a_readonly_task_tool():
    from app.task_mcp import build_mcp_server

    tools = {t.name: t for t in await build_mcp_server().list_tools()}
    assert "nexus_task_summary" in tools
    assert set(tools["nexus_task_summary"].input_schema["properties"]) == {"task_id"}

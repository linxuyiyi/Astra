from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.dashboard import (
    dashboard_activity,
    dashboard_attention,
    dashboard_employees,
    dashboard_overview,
)
from app.models import AgentWorker, Base, Event, OpenCodeExecutionEnvironment, RuntimeSession, Task
from app.runtime.presentation import TASK_STATUS


@pytest.fixture
async def office_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def employee_task(session, *, employee_values=None, task_values=None, session_status="RUNNING"):
    employee = AgentWorker(
        id="E-office", name="办公室员工", role="开发", status="WORKING",
    )
    for key, value in (employee_values or {}).items():
        setattr(employee, key, value)
    task = Task(
        id="T-office", title="真实工作", task_type="ISSUE", status="RUNNING",
        employee_id=employee.id, runtime_connection="CONNECTED", current_session_id="ses_current",
    )
    for key, value in (task_values or {}).items():
        setattr(task, key, value)
    binding = RuntimeSession(
        id="ses_current", task_id=task.id, employee_id=employee.id, workspace="/tmp/office",
        environment={}, status=session_status,
    )
    session.add_all([employee, task, binding])
    await session.commit()
    return employee, task, binding


async def office_employee(session):
    result = await dashboard_employees(session)
    return result["teams"][0]["employees"][0]


@pytest.mark.parametrize(
    ("session_status", "state", "label"),
    [
        ("RUNNING", "working", TASK_STATUS["RUNNING"]["label"]),
        ("WAITING", "waiting", TASK_STATUS["WAITING"]["label"]),
        ("SUCCEEDED", "waiting", "执行结束，待确认"),
        ("FAILED", "waiting", "执行异常，待恢复"),
        ("INTERRUPTED", "waiting", "执行异常，待恢复"),
        ("UNKNOWN", "waiting", "连接异常/状态待确认"),
    ],
)
async def test_office_motion_uses_native_current_round_without_closing_task(
    office_session, session_status, state, label,
):
    _, task, _ = await employee_task(office_session, session_status=session_status)

    employee = await office_employee(office_session)
    overview = await dashboard_overview(office_session)

    assert employee["state"] == state
    assert employee["state_label"] == label
    assert employee["current_task"]["session_status"] == session_status
    assert employee["current_task"]["runtime_status_label"] == TASK_STATUS["RUNNING"]["label"]
    assert overview["working_employees"] == int(state == "working")
    assert overview["running_tasks"] == 1
    assert task.status == "RUNNING"
    assert not office_session.dirty


async def test_office_human_wait_includes_workspace_binding_and_reason(office_session):
    reason = "请在 Workspace 回答问题或权限请求"
    await employee_task(
        office_session,
        task_values={"status": "WAITING", "human_required_reason": reason, "current_progress": "等待选择"},
        session_status="WAITING",
    )

    employee = await office_employee(office_session)
    attention = (await dashboard_attention(office_session))["items"][0]
    overview = await dashboard_overview(office_session)

    assert employee["state"] == "waiting"
    assert employee["current_task"] == {
        "id": "T-office", "title": "真实工作", "status": "WAITING",
        "current_session_id": "ses_current", "session_status": "WAITING",
        "runtime_status_label": TASK_STATUS["WAITING"]["label"], "runtime_detail": "等待选择",
        "human_required_reason": reason, "runtime_connection": "CONNECTED",
    }
    assert attention["current_session_id"] == "ses_current"
    assert attention["session_status"] == "WAITING"
    assert attention["human_required_reason"] == reason
    assert attention["status_label"] == TASK_STATUS["WAITING"]["label"]
    assert overview["waiting_tasks"] == overview["attention_total"] == 1
    assert overview["failed_tasks"] == 0


@pytest.mark.parametrize("old_status", ["SUCCEEDED", "FAILED", "WAITING"])
async def test_office_old_round_cannot_override_current_session(office_session, old_status):
    await employee_task(office_session)
    office_session.add(RuntimeSession(
        id="ses_old", task_id="T-office", employee_id="E-office", workspace="/tmp/office",
        environment={}, status=old_status, superseded_by="ses_current",
    ))
    await office_session.commit()

    employee = await office_employee(office_session)

    assert employee["state"] == "working"
    assert employee["current_task"]["current_session_id"] == "ses_current"
    assert employee["current_task"]["session_status"] == "RUNNING"


async def test_office_superseded_current_pointer_is_not_execution_evidence(office_session):
    _, task, binding = await employee_task(
        office_session, task_values={"status": "WAITING"}, session_status="FAILED",
    )
    office_session.add(RuntimeSession(
        id="ses_new", task_id=task.id, employee_id=task.employee_id, workspace="/tmp/office",
        environment={}, status="RUNNING",
    ))
    binding.superseded_by = "ses_new"
    await office_session.commit()

    employee = await office_employee(office_session)
    attention = (await dashboard_attention(office_session))["items"][0]

    assert employee["state"] == "waiting"
    assert employee["current_task"]["session_status"] is None
    assert attention["session_status"] is None
    assert (await dashboard_overview(office_session))["working_employees"] == 0


@pytest.mark.parametrize("task_status", ["CANCELED", "SUCCEEDED"])
async def test_office_terminal_task_has_no_active_desk_even_with_stale_session(office_session, task_status):
    await employee_task(office_session, task_values={"status": task_status})

    employee = await office_employee(office_session)

    assert employee["state"] == "idle"
    assert employee["current_task"] is None
    assert (await dashboard_overview(office_session))["working_employees"] == 0
    assert (await dashboard_attention(office_session))["items"] == []


async def test_office_bound_work_is_selected_before_new_pending_task(office_session):
    _, task, _ = await employee_task(office_session)
    task.updated_at = datetime.now(UTC) - timedelta(days=1)
    office_session.add(Task(
        id="T-new-pending", title="尚未启动", task_type="ISSUE", employee_id="E-office", status="PENDING",
    ))
    await office_session.commit()

    employee = await office_employee(office_session)

    assert employee["current_task"]["id"] == "T-office"
    assert employee["state"] == "working"


async def test_office_new_pending_task_is_selected_before_old_bound_failure(office_session):
    _, task, _ = await employee_task(
        office_session, task_values={"status": "FAILED"}, session_status="FAILED",
    )
    task.updated_at = datetime.now(UTC) - timedelta(days=1)
    office_session.add(Task(
        id="T-new-pending", title="下一项工作", task_type="ISSUE", employee_id="E-office", status="PENDING",
    ))
    await office_session.commit()

    employee = await office_employee(office_session)

    assert employee["current_task"]["id"] == "T-new-pending"
    assert employee["current_task"]["session_status"] is None
    assert employee["state"] == "idle"
    assert employee["state_label"] == TASK_STATUS["PENDING"]["label"]
    assert {item["task_id"] for item in (await dashboard_attention(office_session))["items"]} == {"T-office"}


async def test_office_waiting_bound_task_has_priority_over_running_bound_task(office_session):
    await employee_task(office_session)
    office_session.add_all([
        Task(
            id="T-human", title="待回答", task_type="ISSUE", employee_id="E-office", status="WAITING",
            current_session_id="ses_human", runtime_connection="CONNECTED",
        ),
        RuntimeSession(
            id="ses_human", task_id="T-human", employee_id="E-office", workspace="/tmp/office",
            environment={}, status="WAITING",
        ),
    ])
    await office_session.commit()

    employee = await office_employee(office_session)

    assert employee["current_task"]["id"] == "T-human"
    assert employee["state"] == "waiting"
    assert (await dashboard_overview(office_session))["working_employees"] == 0


async def test_office_pending_task_without_session_is_available_to_open(office_session):
    _, task, binding = await employee_task(office_session, task_values={"status": "PENDING"})
    task.current_session_id = None
    await office_session.delete(binding)
    await office_session.commit()

    employee = await office_employee(office_session)

    assert employee["current_task"]["id"] == task.id
    assert employee["current_task"]["session_status"] is None
    assert employee["state"] == "idle"
    assert employee["state_label"] == TASK_STATUS["PENDING"]["label"]


@pytest.mark.parametrize(
    ("employee_values", "state", "label"),
    [
        ({"enabled": False}, "idle", "已停用"),
        ({"status": "OFFLINE"}, "idle", "离线"),
        ({"status": "PAUSED"}, "waiting", "已暂停"),
        ({"status": "ERROR"}, "error", "异常"),
    ],
)
async def test_office_unavailable_employee_does_not_animate(office_session, employee_values, state, label):
    await employee_task(office_session, employee_values=employee_values)

    employee = await office_employee(office_session)

    assert employee["state"] == state
    assert employee["state_label"] == label
    assert (await dashboard_overview(office_session))["working_employees"] == 0


@pytest.mark.parametrize(
    ("connection", "state", "label"),
    [("UNKNOWN", "idle", "状态待确认"), ("UNAVAILABLE", "waiting", "连接异常/状态待确认")],
)
async def test_office_unconfirmed_connection_does_not_animate(office_session, connection, state, label):
    await employee_task(office_session, task_values={"runtime_connection": connection})

    employee = await office_employee(office_session)

    assert employee["state"] == state
    assert employee["state_label"] == label
    assert (await dashboard_overview(office_session))["working_employees"] == 0


async def test_office_attention_remains_business_wait_failed_or_inactivity(office_session):
    await employee_task(office_session, session_status="SUCCEEDED")
    office_session.add_all([
        Task(id="T-wait", title="人工事项", task_type="ISSUE", status="WAITING"),
        Task(id="T-fail", title="失败事项", task_type="ISSUE", status="FAILED"),
        Task(id="T-inactive", title="活动提醒", task_type="ISSUE", status="RUNNING", inactivity_alert=True),
    ])
    await office_session.commit()

    items = (await dashboard_attention(office_session))["items"]
    overview = await dashboard_overview(office_session)

    assert {item["task_id"] for item in items} == {"T-wait", "T-fail", "T-inactive"}
    assert overview["attention_total"] == 3
    assert overview["waiting_tasks"] == overview["failed_tasks"] == 1
    assert overview["online_employees"] == 1
    assert overview["working_employees"] == 0


async def test_office_activity_returns_twelve_latest_business_events(office_session):
    await employee_task(office_session)
    now = datetime.now(UTC)
    for index in range(15):
        office_session.add(Event(
            task_id="T-office", employee_id="E-office", event_type="TASK_RUNNING",
            phase="work", message="原始消息", detail={"private": True},
            created_at=now + timedelta(seconds=index),
        ))
    office_session.add(Event(
        task_id="T-office", employee_id="E-office", event_type="TOOL_STARTED",
        phase="work", message="工具事件", created_at=now + timedelta(minutes=1),
    ))
    await office_session.commit()

    items = (await dashboard_activity(office_session))["items"]

    assert len(items) == 12
    assert items[0]["created_at"] == now + timedelta(seconds=14)
    assert items[-1]["created_at"] == now + timedelta(seconds=3)
    assert all(item["kind"] == "task_started" and item["target_id"] == "T-office" for item in items)
    assert all("message" not in item and "detail" not in item for item in items)


async def test_office_activity_excludes_deleted_targets_before_limit(office_session):
    await employee_task(office_session)
    now = datetime.now(UTC)
    valid = Event(
        task_id="T-office", employee_id="E-office", event_type="TASK_SUCCEEDED",
        message="完成任务", created_at=now - timedelta(minutes=1),
    )
    office_session.add(valid)
    for index in range(15):
        office_session.add(Event(
            task_id=f"T-deleted-{index}", event_type="TASK_RUNNING",
            message="历史任务", created_at=now + timedelta(seconds=index),
        ))
    office_session.add(Event(
        employee_id="E-deleted", event_type="EMPLOYEE_CREATED", message="历史员工", created_at=now,
    ))
    await office_session.commit()

    items = (await dashboard_activity(office_session))["items"]

    assert [item["id"] for item in items] == [valid.id]
    assert items[0]["description"] == "完成任务：真实工作"
    assert items[0]["target_id"] == "T-office"


async def test_worker_names_are_independent_of_the_digital_employee_name(office_session):
    worker, task, _ = await employee_task(
        office_session, task_values={"status": "WAITING"}, session_status="WAITING",
    )
    office_session.add(OpenCodeExecutionEnvironment(
        employee_id=worker.id, name="独立构建 Worker", mode="LOCAL",
    ))
    await office_session.commit()

    employee = await office_employee(office_session)
    attention = (await dashboard_attention(office_session))["items"][0]
    assert employee["name"] == "独立构建 Worker"
    assert attention["employee_name"] == "独立构建 Worker"
    assert employee["name"] != worker.name
    assert attention["task_id"] == task.id

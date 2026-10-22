"""仓库/分类不限制接单，各入口共享类型与数字员工资格。"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.api.routes import create_employee, create_task, patch_task
from app.domain import TaskStatus
from app.models import AgentWorker, Task, TaskCategory
from app.runtime.assignment import worker_assignment_block
from app.runtime.dispatcher import Dispatcher
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.schemas import EmployeeCreate, TaskCreate, TaskUpdate
from app.task_tools import TaskTools, nexus_employee_id
from tests.platform.test_task_routing import (
    _employee,
    _environment,
    _pending,
    session,  # noqa: F401
)


@pytest.mark.parametrize("owner,bound,allowed", [
    (None, None, True), (None, "emp-a", True), (None, "emp-b", True),
    ("emp-a", None, True), ("emp-a", "emp-a", True), ("emp-a", "emp-b", False),
])
@pytest.mark.parametrize("kind,supported", [("ISSUE", True), ("REQUIREMENT", False)])
def test_shared_assignment_matrix(owner, bound, allowed, kind, supported):
    worker = _employee()
    worker.digital_employee_id = bound
    result = worker_assignment_block(worker, task_type=kind, responsible_employee_id=owner)
    assert (result is None) == (allowed and supported)


@pytest.mark.parametrize("owner,bound,allowed", [
    (None, None, True), (None, "emp-b", True),
    ("emp-a", None, True), ("emp-a", "emp-a", True), ("emp-a", "emp-b", False),
])
async def test_dispatch_uses_employee_relation_not_repository_or_category(
    session, tmp_path, owner, bound, allowed,  # noqa: F811
):
    worker = _employee()
    worker.digital_employee_id = bound
    worker.category_ids = []
    task = _pending("T-routing", "task-repository")
    task.responsible_employee_id = owner
    session.add_all([worker, _environment(str(tmp_path), "worker-repository"), task])
    await session.commit()
    claimed = await Dispatcher(session).claim_once()
    assert (claimed is not None) == allowed
    task = await session.get(Task, "T-routing")
    assert task.repository_id == "task-repository"
    if claimed:
        assert claimed.execution_environment_snapshot["repository_id"] == "worker-repository"


@pytest.mark.parametrize("owner,kind,error", [
    ("emp-other", "ISSUE", "责任数字员工"), ("emp-a", "REQUIREMENT", "任务类型"),
])
async def test_manual_start_cannot_bypass_assignment_rules(
    session, tmp_path, owner, kind, error,  # noqa: F811
):
    worker = _employee()
    worker.digital_employee_id = "emp-a"
    task = _pending("T-start-invalid", None)
    task.employee_id = worker.id
    task.responsible_employee_id = owner
    task.task_type = kind
    session.add_all([worker, _environment(str(tmp_path)), task])
    await session.commit()
    with pytest.raises(RuntimeStateError, match=error):
        await RuntimeService(session).start(task.id)
    assert task.current_session_id is None


async def test_api_create_and_edit_share_rules_without_category_filter(session):  # noqa: F811
    worker = _employee()
    worker.digital_employee_id = "emp-a"
    worker.category_ids = []
    session.add_all([worker, TaskCategory(id="testing", name="测试")])
    await session.commit()
    payload = dict(
        task_type="ISSUE", category_id="testing", title="跨仓库接单",
        description="分类不限制 Worker", employee_id=worker.id, responsible_employee_id="emp-a",
    )
    task = await create_task(TaskCreate(**payload), session)
    assert task.employee_id == worker.id
    assert task.repository_id is None
    with pytest.raises(HTTPException) as error:
        await patch_task(task.id, TaskUpdate(responsible_employee_id="emp-b"), session)
    assert error.value.status_code == 409
    assert task.responsible_employee_id == "emp-a"
    cleared = await patch_task(task.id, TaskUpdate(responsible_employee_id=None), session)
    assert cleared.employee_id == worker.id and cleared.responsible_employee_id is None
    with pytest.raises(HTTPException):
        await create_task(TaskCreate(**{**payload, "responsible_employee_id": "emp-b"}), session)
    with pytest.raises(HTTPException):
        await create_task(TaskCreate(**{**payload, "task_type": "REQUIREMENT"}), session)


async def test_mcp_create_and_assign_share_owner_and_type_rules(session):  # noqa: F811
    worker = _employee()
    worker.digital_employee_id = "emp-a"
    worker.category_ids = []
    other = AgentWorker(id="other", name="other", role="build", digital_employee_id="emp-b")
    session.add_all([worker, other, TaskCategory(id="testing", name="测试")])
    await session.commit()
    token = nexus_employee_id.set("emp-a")
    try:
        tools = TaskTools(session)
        result = await tools.create(
            task_type="ISSUE", category_id="testing", title="MCP 创建",
            description="未绑定仓库也可执行", worker_id=worker.id,
        )
        assert result["ok"] is True
        task = await session.get(Task, result["task_id"])
        assert task.repository_id is None and task.responsible_employee_id == "emp-a"
        refused = await tools.assign(task.id, other.id)
        assert refused["ok"] is False
        assert "责任数字员工" in refused["error"]
        other.digital_employee_id = None
        await session.commit()
        accepted = await tools.assign(task.id, other.id)
        assert accepted["ok"] is True
        assert accepted["worker_id"] == other.id
        task.status = TaskStatus.SUCCEEDED
        await session.commit()
    finally:
        nexus_employee_id.reset(token)


async def test_public_worker_can_be_created_without_employee_or_category(session):  # noqa: F811
    created = await create_employee(
        EmployeeCreate(id="public-worker", name="公共 Worker", role="build", task_types=["ISSUE"]),
        session,
    )
    assert created.digital_employee_id is None
    assert created.category_ids == []
    assert created.task_types == ["ISSUE"]


async def test_recovery_skips_incompatible_assignment_without_starving_next(session):  # noqa: F811
    first = _employee()
    first.auto_execute = True
    first.digital_employee_id = "emp-a"
    second = AgentWorker(
        id="E2", name="second", role="build", enabled=True, auto_execute=True, task_types=["ISSUE"],
        digital_employee_id="emp-b",
    )
    early = _pending("assigned-incompatible", "one-repo")
    early.employee_id = first.id
    early.responsible_employee_id = "emp-b"
    early.claimed_at = datetime.now(UTC) - timedelta(seconds=1)
    later = _pending("assigned-compatible", "another-repo")
    later.employee_id = second.id
    later.responsible_employee_id = "emp-b"
    later.claimed_at = datetime.now(UTC)
    session.add_all([first, second, early, later])
    await session.commit()
    claimed = await Dispatcher(session).claim_once()
    assert claimed.id == later.id
    assert early.employee_id == first.id

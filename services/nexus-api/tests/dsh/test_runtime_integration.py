from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.workbench_routes import router
from app.database import get_session
from app.models import AgentWorker, Base, OpenCodeExecutionEnvironment, RuntimeSession, Task
from app.runtime.dsh_lifecycle import DshLifecycle, session_state
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.runtime.occupancy import task_occupies


def test_native_end_turn_is_not_business_completion():
    assert session_state({"state": "succeeded", "stop_reason": "end_turn"}).status == "SUCCEEDED"
    for snapshot in [
        {"state": "unknown"},
        {"state": "succeeded", "stop_reason": "max_tokens"},
        {"state": "prepared"},
    ]:
        assert session_state(snapshot).status == "UNKNOWN"


@pytest.mark.parametrize("verified", [None, False, True])
def test_worker_health_requires_explicit_file_channel_verification(monkeypatch, verified):
    from app.runtime import worker_health

    monkeypatch.setattr(worker_health, "_matches_repository", lambda *_: True)
    desired = {"runtime_engine": "dsh", "model": {"route": "qa"}}
    native = {"healthy": True, "protocol_initialized": True, "models": ["qa"], "mcp_names": []}
    if verified is not None:
        native["file_channel_verified"] = verified
    result = worker_health.health_status({"dsh": native}, desired)
    assert result == ("AVAILABLE" if verified is True else "UNAVAILABLE")


async def test_worker_health_exposes_file_channel_failure(monkeypatch):
    from app.runtime import worker_health
    from app.runtime.worker_transport import WorkerCommandTransport

    error = "Worker 文件通道检查失败：请确认目录可访问且内核支持并允许 openat2"

    async def health(_self, name, payload):
        assert name == "dsh_health" and payload == {}
        return {"healthy": False, "file_channel_verified": False, "error": error}

    monkeypatch.setattr(WorkerCommandTransport, "run_command", health)
    desired = {"runtime_engine": "dsh"}
    result = await worker_health.probe_equipment(
        SimpleNamespace(id=1, runtime_engine="dsh"), desired, SimpleNamespace(),
        worker_instance=SimpleNamespace(status="online"),
    )
    assert result["error"] == error
    assert worker_health.health_status(result, desired) == "UNAVAILABLE"


class Native:
    def __init__(self, binding):
        self.binding = binding
        self.state = "succeeded"
        self.stop_reason = "end_turn"
        self.submission_id = "initial"
        self.inputs = []
        self.ack_lost = False
        self.stop_confirmed = True
        self.trace_complete = True

    async def snapshot(self, **kwargs):
        return {
            "binding_id": self.binding.id,
            "native_session_id": self.binding.native_session_id,
            "state": self.state,
            "stop_reason": self.stop_reason,
            "submission_id": self.submission_id,
            "permissions": [],
            "fence": self.binding.fencing_token,
        }

    async def submit(self, **kwargs):
        self.inputs.append(kwargs)
        self.submission_id = kwargs["submission_id"]
        self.state = "running"
        if self.ack_lost:
            raise ConnectionError("模拟 ACK 丢失")
        return await self.snapshot()

    async def cancel(self, *, fence):
        self.state, self.stop_reason = "stopped", "cancelled"
        return {"stop_confirmed": self.stop_confirmed, "fence": fence}

    async def dispose(self, *, fence):
        return {
            "stop_confirmed": True,
            "fence": fence,
            "state": "succeeded",
            "stop_reason": "end_turn",
            "submission_id": self.submission_id,
        }

    async def read_trace_page(self, **kwargs):
        return {
            "complete": self.trace_complete,
            "sha256": "a" * 64,
            "event_count": 4,
            "native_usage_totals": {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110},
        }


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(
        "app.runtime.lifecycle.get_settings", lambda: SimpleNamespace(experience_review_enabled=False)
    )
    async with factory() as db:
        native = {}
        for index in (1, 2):
            worker = AgentWorker(
                id=f"w{index}", name="Worker", role="build", digital_employee_id="same-owner"
            )
            env = OpenCodeExecutionEnvironment(
                id=index,
                employee_id=worker.id,
                runtime_engine="dsh",
                mode="LINUX_REMOTE",
                worker_channel="worker",
                name="DSH",
                workspace_root=str(tmp_path / f"repo{index}"),
            )
            snapshot = {
                "environment_id": index,
                "runtime_engine": "dsh",
                "worker_channel": "worker",
                "mode": "LINUX_REMOTE",
                "name": "DSH",
                "workspace_root": env.workspace_root,
                "opencode_path": "unused",
            }
            task = Task(
                id=f"t{index}",
                title="task",
                task_type="ISSUE",
                employee_id=worker.id,
                status="RUNNING",
                workspace=str(tmp_path / f"worktree{index}"),
                current_session_id=f"b{index}",
                last_message_id="initial",
                execution_environment_snapshot=snapshot,
            )
            binding = RuntimeSession(
                id=f"b{index}",
                task_id=task.id,
                employee_id=worker.id,
                runtime_engine="dsh",
                native_session_id=str(uuid4()),
                workspace=task.workspace,
                environment=snapshot,
                fencing_token=1,
            )
            db.add_all([worker, env, task, binding])
            native[binding.id] = Native(binding)
        await db.commit()

        async def client(self, binding):
            return native[binding.id]

        monkeypatch.setattr(DshLifecycle, "client", client)
        monkeypatch.setattr(
            "app.api.workbench_routes.DshRuntimeClient", lambda _id, binding: native[binding.id]
        )
        yield db, RuntimeService(db), native
    await engine.dispose()


async def test_end_turn_waits_for_human_confirmation_and_keeps_occupancy(runtime):
    db, service, native = runtime
    task = await service.refresh("t1")
    assert task.status == "WAITING"
    assert task.waiting_reason == "EXECUTION_RESULT_REVIEW"
    assert task_occupies(task)
    assert (await db.get(RuntimeSession, "b1")).status == "SUCCEEDED"
    assert (await db.get(Task, "t2")).status == "RUNNING"
    task = await service.confirm_completion("t1")
    assert task.status == "SUCCEEDED"
    assert not task_occupies(task)
    assert (await db.get(RuntimeSession, "b1")).total_tokens == 110


async def test_unknown_and_lost_ack_are_reconciled_without_resubmission(runtime):
    db, service, native = runtime
    n = native["b1"]
    n.ack_lost = True
    with pytest.raises(ConnectionError):
        await DshLifecycle(service).send("t1", "continue once", expected_binding_id="b1")
    task = await db.get(Task, "t1")
    assert task.pending_message_id and task.pending_prompt
    assert task_occupies(task)
    await service.refresh("t1")
    assert task.pending_message_id is None and task.pending_prompt is None
    assert len(n.inputs) == 1
    n.state = "unknown"
    await service.refresh("t1")
    assert (await db.get(RuntimeSession, "b1")).status == "UNKNOWN"
    assert task.status != "SUCCEEDED" and task_occupies(task)
    with pytest.raises(RuntimeStateError):
        await service.confirm_completion("t1")


async def test_unconfirmed_cancel_holds_worker_confirmed_cancel_releases(runtime):
    db, service, native = runtime
    n = native["b1"]
    n.stop_confirmed = False
    with pytest.raises(RuntimeStateError, match="保留占用"):
        await service.stop("t1")
    task = await db.get(Task, "t1")
    assert task.status == "RUNNING" and task_occupies(task)
    n.stop_confirmed = True
    await service.stop("t1")
    assert task.status == "CANCELED" and not task_occupies(task)
    assert task.execution_stop_session_id == "b1"
    assert (await db.get(Task, "t2")).status == "RUNNING"


async def test_incomplete_trace_does_not_redefine_human_completion(runtime):
    db, service, native = runtime
    native["b1"].trace_complete = False
    await service.confirm_completion("t1")
    assert (await db.get(Task, "t1")).status == "SUCCEEDED"
    assert (await db.get(RuntimeSession, "b1")).total_tokens is None


async def test_workbench_rejects_cross_task_native_and_history_writes(runtime):
    db, service, native = runtime
    app = FastAPI()
    app.include_router(router)

    async def session():
        yield db

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        n1, n2 = native["b1"].binding.native_session_id, native["b2"].binding.native_session_id
        for path in [
            f"/api/tasks/t2/workbench/b1?native_session_id={n1}",
            f"/api/tasks/t1/workbench/b1?native_session_id={n2}",
        ]:
            assert (await client.get(path)).status_code == 404
        response = await client.post(
            "/api/tasks/t1/workbench/b1/input",
            json={
                "native_session_id": n1,
                "text": "task one only",
            },
        )
        assert response.status_code == 200, response.text
        assert len(native["b1"].inputs) == 1 and not native["b2"].inputs
        task = await db.get(Task, "t1")
        task.current_session_id = "new-binding"
        await db.commit()
        assert (
            await client.post(
                "/api/tasks/t1/workbench/b1/input",
                json={
                    "native_session_id": n1,
                    "text": "must not run",
                },
            )
        ).status_code == 409


async def test_closed_task_restart_keeps_history_readonly_and_requires_current_binding(runtime, monkeypatch):
    db, _, native = runtime
    task = await db.get(Task, "t1")
    task.status = "SUCCEEDED"
    await db.commit()
    calls = []

    async def restart(self, task_id, **kwargs):
        calls.append((task_id, kwargs))

    monkeypatch.setattr(RuntimeService, "start", restart)
    app = FastAPI()
    app.include_router(router)

    async def session():
        yield db

    app.dependency_overrides[get_session] = session
    body = {"native_session_id": native["b1"].binding.native_session_id}
    base = "/api/tasks/t1/workbench/b1"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(base + "/input", json={**body, "text": "must not run"})).status_code == 409
        assert (await client.post(base + "/action", json={**body, "action": "restart"})).status_code == 200
        assert calls == [("t1", {"restart": True, "expected_session_id": "b1"})]
        task.current_session_id = "new-binding"
        await db.commit()
        assert (await client.post(base + "/action", json={**body, "action": "restart"})).status_code == 409
        assert len(calls) == 1

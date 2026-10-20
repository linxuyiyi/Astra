import asyncio
import json
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.workspace_routes import load_proxy_secrets, redact_secret_fields
from app.database import get_session
from app.executors.errors import OpenCodeSessionNotFound
from app.main import app
from app.models import (
    AgentWorker,
    Base,
    OpenCodeExecutionEnvironment,
    RuntimeSession,
    Task,
)
from app.runtime.environment import environment_snapshot
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.runtime.session_connection import prepare_workspace
from app.runtime.workspace_changes import collect_workspace_changes, workspace_diff
from app.runtime.workspace_files import (
    materialize_task_attachments,
    same_workspace_path,
    upload,
    validate_path,
)
from app.worker_agent.workspace import prepare_workspace_local


class NativeServer:
    def __init__(self):
        self.sessions = {"ses_current": []}
        self.state = "idle"
        self.pending = []
        self.pending_permissions = []
        self.sent = []
        self.fail_after_accept = False
        self.created_options = []
        self.children_by_session = {}
        self.abort_count = 0
        self.auth = None
        self.base_url = "http://native"
        self.exchange_sessions: list[str | None] = []

    def _timeout(self):
        return 5

    async def health(self):
        return {"healthy": True, "version": "1.2.27"}

    async def session(self, directory, sid):
        if sid not in self.sessions:
            raise OpenCodeSessionNotFound(sid)
        return {"id": sid, "directory": str(directory)}

    async def status(self, directory, sid):
        await self.session(directory, sid)
        return self.state

    async def questions(self, directory):
        return self.pending

    async def permissions(self, directory):
        return []

    async def children(self, directory, sid):
        return self.children_by_session.get(sid, [])

    async def messages(self, directory, sid):
        await self.session(directory, sid)
        return self.sessions[sid]

    async def messages_page(self, directory, sid, *, limit, before=None):
        messages = await self.messages(directory, sid)
        return {"messages": messages, "next_before": None}

    async def exchange(self, method, path, *, params=None, body=None, session_id=None, task_id=None):
        self.exchange_sessions.append(session_id)
        async with httpx.AsyncClient(auth=self.auth, timeout=self._timeout()) as http:
            response = await http.request(method, self.base_url + path, params=params, json=body)
        headers = {key.lower(): value for key, value in response.headers.items()}
        return response.status_code, response.content, headers

    async def request(self, method, path, *, directory=None, body=None, session_id=None, task_id=None):
        if method == "POST" and path.endswith("/prompt_async"):
            sid = path.split("/")[2]
            self.sent.append((sid, body))
            self.sessions[sid].append(
                {"info": {"id": body["messageID"], "role": "user"}, "parts": body["parts"]}
            )
            # Real OpenCode persists the assistant placeholder synchronously before
            # prompt_async returns 204; mirror that so an in-flight turn always has an
            # assistant message. A turn that errors before execution (unknown model or
            # provider) persists no assistant message at all.
            self.sessions[sid].append(
                {"info": {"id": f"asst_{body['messageID']}", "role": "assistant"}, "parts": []}
            )
            self.state = "busy"
            if self.fail_after_accept:
                raise ConnectionError("ACK lost")
        if path == "/question":
            return self.pending
        if path == "/permission":
            return self.pending_permissions

    async def abort(self, directory, sid):
        await self.session(directory, sid)
        self.abort_count += 1
        self.state = "idle"
        self.pending = []
        return True

    async def create_session(self, directory, **kwargs):
        self.created_options.append(kwargs)
        sid = f"ses_{len(self.sessions)}"
        self.sessions[sid] = []
        return {"id": sid}


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    from app.database import init_database

    # Proxy path checks read worker_channel from the process database. Create it
    # here so the lookup does not depend on an earlier test having migrated.
    await init_database()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    native = NativeServer()

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield native

    monkeypatch.setattr("app.runtime.lifecycle.connect", connect)
    monkeypatch.setattr("app.api.workspace_routes.connect", connect)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with factory() as session:
        employee = AgentWorker(id="E1", name="Engineer", role="build", status="IDLE")
        environment = OpenCodeExecutionEnvironment(
            employee_id="E1",
            mode="LOCAL",
            name="test",
            workspace_root=str(workspace),
            opencode_server_port=54197,
            repository_id="repo-session",
            status="AVAILABLE",
        )
        session.add_all([employee, environment])
        await session.flush()
        task = Task(
            id="T1",
            title="Task",
            task_type="ISSUE",
            status="FAILED",
            employee_id="E1",
            repository_id="repo-session",
            current_session_id="ses_current",
            workspace=str(workspace),
            execution_environment_snapshot=environment_snapshot(environment),
        )
        session.add(task)
        session.add(
            RuntimeSession(
                id="ses_current",
                task_id="T1",
                employee_id="E1",
                workspace=str(workspace),
                environment=environment_snapshot(environment),
            )
        )
        await session.commit()

        async def dependency():
            yield session

        app.dependency_overrides[get_session] = dependency
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://astra") as api:
            yield SimpleNamespace(
                session=session,
                native=native,
                task=task,
                environment=environment,
                service=RuntimeService(session),
                factory=factory,
                api=api,
                workspace=workspace,
            )
        app.dependency_overrides.clear()
    await engine.dispose()


async def test_workspace_unknown_version_is_not_rewritten_or_served(runtime):
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    stored = dict(binding.environment)
    assert "opencode_version" not in stored

    response = await runtime.api.get("/api/tasks/T1/workspace")
    assert response.status_code == 200
    body = response.json()
    assert body["runtime"]["opencode_version"] == "unknown"
    assert body["runtime"]["workspace_ui"] is None
    assert body["runtime"]["workspace_ui_error"] == "版本未知/界面已退役"
    assert body["sessions"][0]["opencode_version"] == "unknown"
    assert body["sessions"][0]["workspace_ui"] is None
    assert body["sessions"][0]["workspace_ui_error"] == "版本未知/界面已退役"
    await runtime.session.refresh(binding)
    assert binding.environment == stored


async def test_retired_recorded_version_keeps_the_snapshot_and_hides_the_ui(runtime):
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.environment = {**binding.environment, "opencode_version": "1.18.31"}
    await runtime.session.commit()

    response = await runtime.api.get("/api/tasks/T1/workspace")
    assert response.status_code == 200
    body = response.json()
    assert body["runtime"]["opencode_version"] == "1.18.31"
    assert body["runtime"]["workspace_ui"] is None
    assert "版本未知/界面已退役" in body["runtime"]["workspace_ui_error"]
    assert "1.18.31" in body["runtime"]["workspace_ui_error"]
    await runtime.session.refresh(binding)
    assert binding.environment["opencode_version"] == "1.18.31"


async def test_supported_version_uses_its_bundle(runtime):
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.environment = {**binding.environment, "opencode_version": "1.18.35"}
    await runtime.session.commit()

    response = await runtime.api.get("/api/tasks/T1/workspace")
    assert response.status_code == 200
    body = response.json()
    assert body["runtime"]["opencode_version"] == "1.18.35"
    assert body["runtime"]["workspace_ui"] == "/opencode-workspace/1.18.35/index.html"
    assert body["runtime"]["workspace_ui_error"] is None


async def test_missing_workspace_bundle_is_not_served_as_the_spa(runtime):
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.environment = {**binding.environment, "opencode_version": "9.9.9"}
    await runtime.session.commit()

    response = await runtime.api.get("/api/tasks/T1/workspace")
    assert response.status_code == 200
    body = response.json()
    assert body["runtime"]["workspace_ui"] is None
    assert "版本未知/界面已退役" in body["runtime"]["workspace_ui_error"]
    assert "9.9.9" in body["runtime"]["workspace_ui_error"]
    assert body["sessions"][0]["workspace_ui"] is None


async def test_continue_keeps_session_and_does_not_create_execution_copies(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "continue"}]})
    assert r.task.status == "RUNNING"
    assert r.task.current_session_id == "ses_current"
    assert await r.session.scalar(select(func.count()).select_from(RuntimeSession)) == 1
    assert r.task.pending_prompt is None


async def test_waiting_takes_priority_over_native_busy_and_resumes(runtime):
    r = runtime
    r.native.state = "busy"
    r.native.pending = [{"id": "q1", "sessionID": "ses_current"}]
    await r.service.refresh("T1")
    assert r.task.status == "WAITING"
    with pytest.raises(RuntimeStateError, match="回答"):
        await r.service.send("T1", {"parts": []})
    r.native.pending = []
    await r.service.refresh("T1")
    assert r.task.status == "RUNNING"


async def test_lost_ack_is_reconciled_without_duplicate_prompt(runtime):
    r = runtime
    r.native.fail_after_accept = True
    with pytest.raises(ConnectionError):
        await r.service.send("T1", {"parts": [{"type": "text", "text": "continue"}]})
    assert r.task.status == "FAILED"  # Transport loss does not assert an execution outcome.
    assert r.task.pending_message_id
    r.native.fail_after_accept = False
    with pytest.raises(RuntimeStateError, match="结果未知"):
        await r.service.retry_submission("T1")
    await r.session.refresh(r.task)
    stamped = {
        key: {**value, "body_fetched_at": (datetime.now(UTC) - timedelta(seconds=180)).isoformat()}
        for key, value in r.task.submission_attempts.items()
    }
    r.task.submission_attempts = stamped
    await r.session.commit()
    await r.service.retry_submission("T1", confirm_unknown=True)
    assert len(r.native.sent) == 1
    assert r.task.pending_prompt is None
    assert r.task.status == "RUNNING"


async def test_previous_completed_turn_cannot_complete_new_submission(runtime):
    r = runtime
    r.native.sessions["ses_current"] = [
        {"info": {"id": "old", "role": "assistant", "time": {"completed": 1}, "finish": "stop"}}
    ]
    await r.service.send("T1", {"parts": [{"type": "text", "text": "next turn"}]})
    r.native.state = "idle"
    await r.service.refresh("T1")
    assert r.task.status == "RUNNING"
    r.native.sessions["ses_current"].append(
        {
            "info": {"role": "assistant", "time": {"completed": 2}, "finish": "stop"},
            "parts": [{"type": "text", "text": "new turn completed"}],
        }
    )
    await r.service.refresh("T1")
    assert r.task.status == "WAITING"
    assert r.task.human_required_reason == "执行结束，待确认"


async def test_manual_confirm_waits_for_business_confirmation(runtime):
    r = runtime
    r.task.completion_policy = "MANUAL_CONFIRM"
    await r.session.commit()

    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)

    session = await r.session.get(RuntimeSession, "ses_current")
    employee = await r.session.get(AgentWorker, "E1")
    assert session.status == "SUCCEEDED"
    assert r.task.status == "WAITING"
    assert r.task.human_required_reason == "执行结束，待确认"
    assert r.task.submitted_at is None
    assert r.task.confirmed_at is None
    assert employee.current_task_count == 1

    response = await r.api.post("/api/tasks/T1/confirm")
    assert response.status_code == 200
    assert response.json()["status"] == "SUCCEEDED"
    assert r.task.confirmed_at is not None


async def test_child_session_question_projects_task_waiting(runtime):
    r = runtime
    r.native.sessions["ses_child"] = []
    r.native.children_by_session["ses_current"] = [{"id": "ses_child"}]
    r.native.state = "busy"
    r.native.pending = [{"id": "q_child", "sessionID": "ses_child"}]

    await r.service.refresh("T1")

    assert r.task.status == "WAITING"
    with pytest.raises(RuntimeStateError, match="Session 树"):
        await r.service.send("T1", {"parts": [{"type": "text", "text": "should block"}]})


async def test_auto_final_text_claim_does_not_decide_task_status(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    assistant = r.native.sessions["ses_current"][-1]
    assistant["parts"] = [{"type": "text", "text": "任务未完成，无法继续，需要后续处理"}]
    assistant["info"].update({"time": {"completed": 1}, "finish": "stop"})
    r.native.state = "idle"

    await r.service.refresh("T1")

    # Reply text never closes the business Task. The round waits for acceptance.
    assert r.task.status == "WAITING"
    assert r.task.status != "SUCCEEDED"


async def test_auto_unverifiable_result_requires_native_question(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    assistant = r.native.sessions["ses_current"][-1]
    assistant["parts"] = [{"type": "text", "text": "修改完成但无法验证"}]
    assistant["info"].update({"time": {"completed": 1}, "finish": "stop"})
    r.native.state = "busy"
    r.native.pending = [{"id": "q_verify", "sessionID": "ses_current"}]

    await r.service.refresh("T1")

    # Only a native question/permission can express a blocked result.
    assert r.task.status == "WAITING"


async def test_auto_completed_turn_projects_succeeded(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    session = await r.session.get(RuntimeSession, "ses_current")
    assert r.task.status == "WAITING"
    assert session.status == "SUCCEEDED"


async def test_retry_submission_aborts_if_stop_invalidates_inflight_send(runtime):
    r = runtime
    pending_id = "msg_retryrace01"
    r.task.last_message_id = pending_id
    r.task.pending_message_id = pending_id
    r.task.pending_prompt = {
        "action": "prompt_async",
        "body": {"messageID": pending_id, "parts": [{"type": "text", "text": "retry"}]},
    }
    r.task.pending_attempt = 1
    r.task.submission_attempts = {
        f"{pending_id}:1": {"request_id": "req_seed_abort", "body_fetched_at": None},
    }
    r.task.status = "FAILED"
    await r.session.commit()

    started = asyncio.Event()
    release = asyncio.Event()
    original_request = r.native.request

    async def delayed_request(method, path, **kwargs):
        result = await original_request(method, path, **kwargs)
        if method == "POST" and path.endswith("/prompt_async"):
            started.set()
            await release.wait()
        return result

    r.native.request = delayed_request
    retry_task = asyncio.create_task(r.service.retry_submission("T1"))
    await started.wait()

    async with r.factory() as control_session:
        await RuntimeService(control_session).stop("T1")

    release.set()
    with pytest.raises(RuntimeStateError, match="失效"):
        await retry_task

    await r.session.refresh(r.task)
    assert r.task.status == "CANCELED"
    assert r.native.state == "idle"
    assert r.native.abort_count >= 2


async def test_send_does_not_abort_when_background_refresh_acks_first(runtime):
    r = runtime
    started = asyncio.Event()
    release = asyncio.Event()
    original_request = r.native.request

    async def delayed_request(method, path, **kwargs):
        result = await original_request(method, path, **kwargs)
        if method == "POST" and path.endswith("/prompt_async"):
            started.set()
            await release.wait()
        return result

    r.native.request = delayed_request
    send_task = asyncio.create_task(
        r.service.send("T1", {"parts": [{"type": "text", "text": "continue"}]})
    )
    await started.wait()

    await r.service.refresh("T1")
    assert r.task.pending_message_id is None

    release.set()
    result = await send_task

    assert result.status == "RUNNING"
    assert r.native.abort_count == 0
    assert r.native.state == "busy"


async def test_delayed_ack_does_not_reopen_succeeded_task(runtime):
    r = runtime
    started = asyncio.Event()
    release = asyncio.Event()
    original_request = r.native.request

    async def delayed_request(method, path, **kwargs):
        result = await original_request(method, path, **kwargs)
        if method == "POST" and path.endswith("/prompt_async"):
            started.set()
            await release.wait()
        return result

    r.native.request = delayed_request
    send_task = asyncio.create_task(
        r.service.send("T1", {"parts": [{"type": "text", "text": "continue"}]})
    )
    await started.wait()

    # The background refresh concludes the round and ACKs the submission while
    # the send request is still in flight.
    await _complete_current_turn(r)
    assert r.task.status == "WAITING"

    release.set()
    result = await send_task

    assert result.status == "WAITING"
    assert r.task.status == "WAITING"
    binding = await r.session.get(RuntimeSession, "ses_current")
    assert binding.status == "SUCCEEDED"


async def test_delayed_ack_does_not_clear_manual_confirmation(runtime):
    r = runtime
    r.task.completion_policy = "MANUAL_CONFIRM"
    await r.session.commit()

    started = asyncio.Event()
    release = asyncio.Event()
    original_request = r.native.request

    async def delayed_request(method, path, **kwargs):
        result = await original_request(method, path, **kwargs)
        if method == "POST" and path.endswith("/prompt_async"):
            started.set()
            await release.wait()
        return result

    r.native.request = delayed_request
    send_task = asyncio.create_task(
        r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    )
    await started.wait()

    await _complete_current_turn(r)
    assert r.task.status == "WAITING"
    response = await r.api.post("/api/tasks/T1/confirm")
    assert response.status_code == 200
    assert r.task.confirmed_at is not None

    release.set()
    result = await send_task

    assert result.status == "SUCCEEDED"
    assert r.task.status == "SUCCEEDED"
    assert r.task.confirmed_at is not None


async def test_stop_cancels_continued_round_after_lost_ack_on_succeeded_task(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "first"}]})
    await _complete_current_turn(r)
    assert r.task.status == "WAITING"

    # The user continues on the same Session, but the ACK is lost.
    r.native.fail_after_accept = True
    with pytest.raises(ConnectionError):
        await r.service.send("T1", {"parts": [{"type": "text", "text": "continue"}]})
    assert r.task.status == "WAITING"
    assert r.task.pending_message_id is not None
    assert r.native.state == "busy"

    stopped = await r.service.stop("T1")

    assert stopped.status == "CANCELED"
    assert r.task.status == "CANCELED"
    assert r.task.pending_message_id is None
    assert r.task.pending_prompt is None
    assert r.native.state == "idle"


async def test_stop_refuses_completed_task_without_active_round(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "first"}]})
    await _complete_current_turn(r)
    await r.service.confirm_completion("T1")
    assert r.task.status == "SUCCEEDED"

    with pytest.raises(RuntimeStateError, match="已结束"):
        await r.service.stop("T1")

    await r.session.refresh(r.task)
    assert r.task.status == "SUCCEEDED"


async def test_retry_submission_converges_when_background_refresh_acks_first(runtime):
    r = runtime
    pending_id = "msg_retryack0001"
    r.task.last_message_id = pending_id
    r.task.pending_message_id = pending_id
    r.task.pending_prompt = {
        "action": "prompt_async",
        "body": {"messageID": pending_id, "parts": [{"type": "text", "text": "retry"}]},
    }
    r.task.pending_attempt = 1
    r.task.submission_attempts = {
        f"{pending_id}:1": {"request_id": "req_seed_ack", "body_fetched_at": None},
    }
    r.task.status = "FAILED"
    await r.session.commit()

    started = asyncio.Event()
    release = asyncio.Event()
    original_request = r.native.request

    async def delayed_request(method, path, **kwargs):
        result = await original_request(method, path, **kwargs)
        if method == "POST" and path.endswith("/prompt_async"):
            started.set()
            await release.wait()
        return result

    r.native.request = delayed_request
    retry_task = asyncio.create_task(r.service.retry_submission("T1"))
    await started.wait()

    await r.service.refresh("T1")
    assert r.task.pending_message_id is None

    release.set()
    result = await retry_task

    assert result.status == "RUNNING"
    assert r.native.abort_count == 0
    assert len(r.native.sent) == 1


async def test_start_lost_ack_preserves_recovery_and_refresh_converges(runtime):
    r = runtime
    aborted = []
    original_abort = r.native.abort

    async def tracking_abort(directory, sid):
        aborted.append(sid)
        return await original_abort(directory, sid)

    r.native.abort = tracking_abort
    r.native.fail_after_accept = True

    with pytest.raises(ConnectionError):
        await r.service.start("T1", restart=True)

    await r.session.refresh(r.task)
    created = r.task.current_session_id
    assert created != "ses_current"
    assert r.task.pending_message_id is not None
    assert r.task.pending_prompt is not None
    assert r.task.runtime_connection == "UNAVAILABLE"
    assert created not in aborted
    assert r.native.sessions[created]
    assert r.native.state == "busy"

    r.native.fail_after_accept = False
    await r.service.refresh("T1")

    await r.session.refresh(r.task)
    assert r.task.status == "RUNNING"
    assert r.task.pending_message_id is None
    assert r.native.abort_count == len(aborted)


async def test_start_definitive_failure_clears_pending_and_aborts_created_session(runtime):
    from app.executors.errors import OpenCodeExecutionError

    r = runtime
    aborted = []
    original_abort = r.native.abort

    async def tracking_abort(directory, sid):
        aborted.append(sid)
        return await original_abort(directory, sid)

    r.native.abort = tracking_abort
    original_request = r.native.request

    async def failing_request(method, path, **kwargs):
        if method == "POST" and path.endswith("/prompt_async"):
            raise OpenCodeExecutionError("provider rejected input")
        return await original_request(method, path, **kwargs)

    r.native.request = failing_request
    with pytest.raises(OpenCodeExecutionError):
        await r.service.start("T1", restart=True)

    await r.session.refresh(r.task)
    created = r.task.current_session_id
    assert created != "ses_current"
    assert r.task.pending_message_id is None
    assert r.task.pending_prompt is None
    assert created in aborted


async def test_start_rolls_back_new_binding_when_supersede_fails(runtime, monkeypatch):
    r = runtime

    async def boom(*_args, **_kwargs):
        raise RuntimeError("supersede failed")

    monkeypatch.setattr(r.service, "_supersede_session", boom)
    with pytest.raises(RuntimeError, match="supersede failed"):
        await r.service.start("T1", restart=True)

    await r.session.refresh(r.task)
    # The whole round is one transaction: the new binding must not survive.
    assert r.task.current_session_id == "ses_current"
    assert await r.session.scalar(select(func.count()).select_from(RuntimeSession)) == 1
    old = await r.session.get(RuntimeSession, "ses_current")
    assert old.superseded_by is None


async def test_restart_from_running_definitive_failure_does_not_leave_fake_running(runtime):
    from app.executors.errors import OpenCodeExecutionError

    r = runtime
    await r.service.project(r.task, "RUNNING")
    original_request = r.native.request

    async def failing_request(method, path, **kwargs):
        if method == "POST" and path.endswith("/prompt_async"):
            raise OpenCodeExecutionError("provider rejected restart")
        return await original_request(method, path, **kwargs)

    r.native.request = failing_request
    with pytest.raises(OpenCodeExecutionError):
        await r.service.start("T1", restart=True)

    await r.session.refresh(r.task)
    created = r.task.current_session_id
    assert created != "ses_current"
    # A definitively failed restart must not leave RUNNING with a dead Session.
    assert r.task.status == "WAITING"
    assert r.task.pending_message_id is None
    assert r.task.pending_prompt is None
    assert r.task.runtime_error
    binding = await r.session.get(RuntimeSession, created)
    assert binding.status == "INTERRUPTED"


async def test_retry_submission_is_not_stale_when_bound_session_is_still_running(runtime):
    r = runtime
    # A new round may be bound while the Task status still reflects the old
    # CANCELED result; the RUNNING RuntimeSession is the source of truth.
    pending_id = "msg_retrycancel01"
    r.task.status = "CANCELED"
    r.task.pending_message_id = pending_id
    r.task.pending_prompt = {
        "action": "prompt_async",
        "body": {"messageID": pending_id, "parts": [{"type": "text", "text": "retry"}]},
    }
    binding = await r.session.get(RuntimeSession, "ses_current")
    binding.status = "RUNNING"
    await r.session.commit()

    with pytest.raises(RuntimeStateError, match="已结束"):
        await r.service.retry_submission("T1")
    assert not r.native.sent


async def test_turn_without_assistant_reply_projects_failed(runtime):
    r = runtime
    await r.service.send("T1", {"messageID": "msg_client0001", "parts": [{"type": "text", "text": "go"}]})
    assert r.task.status == "RUNNING"
    # OpenCode errored before persisting any assistant reply (e.g. unknown model or
    # provider): only the user message exists and the session is idle. This must not
    # stay RUNNING forever.
    r.native.state = "idle"
    r.native.sessions["ses_current"] = [
        {"info": {"id": "msg_client0001", "role": "user"}, "parts": []}
    ]
    # Only an aged turn without any assistant reply is a real failure; a freshly
    # submitted prompt stays RUNNING until the no-reply window elapses.
    r.task.turn_started_at = datetime.now(UTC) - timedelta(minutes=5)
    await r.session.commit()
    await r.service.refresh("T1")
    assert r.task.status == "WAITING"
    assert r.task.failure_reason


async def test_missing_session_never_creates_replacement(runtime):
    r = runtime
    r.native.sessions.clear()
    response = await r.api.post("/api/tasks/T1/reconnect")
    assert response.status_code == 404
    assert r.task.current_session_id == "ses_current"
    assert not r.native.sessions
    assert r.task.status == "FAILED"


async def test_disconnect_preserves_running_and_cancel_is_explicit(runtime):
    r = runtime
    await r.service.project(r.task, "RUNNING")
    await r.service.disconnected("T1", ConnectionError("network down"))
    assert r.task.status == "RUNNING"
    assert r.task.runtime_connection == "UNAVAILABLE"
    await r.service.stop("T1")
    assert r.task.status == "CANCELED"
    r.native.state = "idle"
    await r.service.refresh("T1")
    assert r.task.status == "CANCELED"


async def test_cancel_captures_final_session_usage(runtime):
    r = runtime
    await r.service.project(r.task, "RUNNING")
    r.native.sessions["ses_current"] = [
        {
            "info": {
                "id": "m_final",
                "role": "assistant",
                "modelID": "local-proxy/GLM-5.2-Tester",
                "time": {"created": 1_700_000_000_000, "completed": 1_700_000_003_000},
                "tokens": {"input": 11, "output": 7},
            },
            "parts": [],
        }
    ]
    await r.session.commit()

    await r.service.stop("T1")

    binding = await r.session.get(RuntimeSession, "ses_current")
    assert binding.status == "INTERRUPTED"
    assert binding.input_tokens == 11
    assert binding.output_tokens == 7
    assert binding.model_name == "local-proxy/GLM-5.2-Tester"
    assert binding.native_session_count == 1
    assert binding.execution_seconds == 3


async def test_employee_without_model_inherits_global_runtime_model(runtime, monkeypatch):
    from app.config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "opencode_model", "local-proxy/GLM-5.2-Tester")
    runtime.environment.model_name = None
    runtime.task.execution_environment_snapshot["model_name"] = None
    await runtime.session.commit()

    await runtime.service.start("T1", restart=True)

    assert runtime.native.created_options[-1]["model_name"] == "local-proxy/GLM-5.2-Tester"
    current = await runtime.session.get(RuntimeSession, runtime.task.current_session_id)
    assert current.model_name == "local-proxy/GLM-5.2-Tester"


async def test_explicit_restart_supersedes_each_running_session(runtime):
    r = runtime
    await r.service.start("T1", restart=True)
    first_new = r.task.current_session_id
    assert first_new != "ses_current"
    assert await r.session.scalar(select(func.count()).select_from(RuntimeSession)) == 2
    old = await r.session.get(RuntimeSession, "ses_current")
    assert old.status == "INTERRUPTED"
    assert old.terminal_reason == "aborted"
    assert old.superseded_by == first_new and old.superseded_at is not None

    await r.service.start("T1", restart=True)
    second_new = r.task.current_session_id
    assert second_new != first_new
    assert await r.session.scalar(select(func.count()).select_from(RuntimeSession)) == 3
    first = await r.session.get(RuntimeSession, first_new)
    assert first.status == "INTERRUPTED"
    assert first.superseded_by == second_new


async def test_restart_does_not_rewrite_business_completion_time(runtime):
    from app.runtime.session_state import SessionState

    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "first"}]})
    await _complete_current_turn(r)
    await r.service.confirm_completion("T1")
    first_completed_at = r.task.completed_at
    assert first_completed_at is not None

    await r.service.start("T1", restart=True)
    await r.session.refresh(r.task)
    assert r.task.completed_at == first_completed_at

    # A second successful execution keeps the original completion fact.
    await r.service.apply_session_state(
        r.task,
        SessionState(status="SUCCEEDED", terminal_reason="completed", finish_reason="stop"),
    )
    await r.session.refresh(r.task)
    assert r.task.status == "WAITING"
    assert r.task.completed_at == first_completed_at


async def test_restart_after_cancel_starts_new_session_without_abort(runtime):
    r = runtime
    aborted = []
    original_abort = r.native.abort

    async def tracking_abort(directory, sid):
        aborted.append(sid)
        return await original_abort(directory, sid)

    r.native.abort = tracking_abort
    await r.service.stop("T1")
    assert r.task.status == "CANCELED"
    old = await r.session.get(RuntimeSession, "ses_current")
    assert old.status == "INTERRUPTED"

    await r.service.start("T1", restart=True)

    await r.session.refresh(r.task)
    new_id = r.task.current_session_id
    assert new_id != "ses_current"
    assert r.task.status == "RUNNING"
    assert r.task.turn_started_at is not None
    assert r.task.execution_error is None
    assert new_id not in aborted
    assert r.native.state == "busy"


async def test_restart_resets_turn_started_at_for_new_running_round(runtime):
    r = runtime
    await r.service.project(r.task, "RUNNING")
    stale_start = datetime.now(UTC) - timedelta(days=1)
    r.task.turn_started_at = stale_start
    await r.session.commit()

    await r.service.start("T1", restart=True)

    await r.session.refresh(r.task)
    assert r.task.status == "RUNNING"
    assert r.task.turn_started_at is not None
    assert r.task.turn_started_at > stale_start + timedelta(hours=1)

    # A fresh round must not inherit the previous round's timeout window.
    r.native.state = "busy"
    await r.service.refresh("T1")
    assert r.task.status == "RUNNING"
    assert r.native.state == "busy"


async def test_restart_from_failed_initializes_new_round_fields(runtime):
    r = runtime
    r.task.turn_started_at = datetime.now(UTC) - timedelta(days=1)
    r.task.execution_error = {"code": "old", "message": "old"}
    r.task.failure_reason = "old failure"
    r.task.finished_at = datetime.now(UTC) - timedelta(days=1)
    await r.session.commit()

    await r.service.start("T1", restart=True)

    await r.session.refresh(r.task)
    assert r.task.status == "RUNNING"
    assert r.task.execution_error is None
    assert r.task.failure_reason is None
    assert r.task.finished_at is None
    assert r.task.turn_started_at > datetime.now(UTC) - timedelta(minutes=1)

    old = await r.session.get(RuntimeSession, "ses_current")
    assert old.superseded_by == r.task.current_session_id




def test_windows_workspace_path_comparison_accepts_slash_and_case_variants(runtime):
    assert same_workspace_path(
        runtime.environment,
        r"D:\Code\Astra\.astra-tasks\E1\T1",
        "d:/Code/Astra/.astra-tasks/E1/T1",
    )


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", ".git/config", "..\\secret"])
async def test_file_path_cannot_escape_workspace(runtime, path):
    with pytest.raises(PermissionError):
        await validate_path(runtime.environment, str(runtime.workspace), path)


async def test_symlink_escape_and_upload_symlink_are_rejected(runtime, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (runtime.workspace / "escape").symlink_to(outside)
    (runtime.workspace / "uploads").symlink_to(outside)
    with pytest.raises(PermissionError):
        await validate_path(runtime.environment, str(runtime.workspace), "escape/private")
    with pytest.raises(PermissionError):
        await upload(runtime.environment, str(runtime.workspace), "test.txt", b"content")
    assert not list(outside.iterdir())


async def test_upload_lands_in_workspace_and_returns_actual_path(runtime):
    response = await runtime.api.post("/api/tasks/T1/files/input.txt", content=b"hello")
    assert response.status_code == 200
    path = Path(response.json()["path"])
    assert path.parent == runtime.workspace / "uploads"
    assert path.read_bytes() == b"hello"

async def test_task_creation_attachment_materializes_once(runtime):
    attachments = [
        {
            "name": "requirements.md",
            "size": 5,
            "content_type": "text/markdown",
            "content": "data:text/markdown;base64,aGVsbG8=",
        }
    ]
    first = await materialize_task_attachments(
        runtime.environment, str(runtime.workspace), attachments
    )
    assert "content" not in first[0]
    path = Path(first[0]["workspace_path"])
    assert path.read_bytes() == b"hello"

    second = await materialize_task_attachments(
        runtime.environment, str(runtime.workspace), first
    )
    assert second[0]["workspace_path"] == first[0]["workspace_path"]
    assert len(list((runtime.workspace / "uploads").iterdir())) == 1



@pytest.mark.parametrize("path", ["session/ses_other/message", "global/dispose", "config", "pty"])
async def test_gateway_rejects_cross_session_and_global_mutations(runtime, path):
    response = await runtime.api.post(f"/api/tasks/T1/opencode/ses_current/{path}", json={})
    assert response.status_code == 403
    assert not runtime.native.sent


async def test_shells_read_is_allowed_but_pty_lifecycle_is_denied(runtime, monkeypatch):
    """Native Settings only needs the read-only shell list; PTY lifecycle stays denied."""
    original = httpx.AsyncClient

    def upstream(request):
        assert request.url.path == "/pty/shells"
        assert request.url.params["directory"] == str(runtime.workspace)
        return httpx.Response(200, json=[{"path": "/bin/bash", "name": "bash", "acceptable": True}])

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    shells = await runtime.api.get("/api/tasks/T1/opencode/ses_current/pty/shells")
    assert shells.status_code == 200
    assert shells.json() == [{"path": "/bin/bash", "name": "bash", "acceptable": True}]
    assert (
        await runtime.api.post("/api/tasks/T1/opencode/ses_current/pty", json={})
    ).status_code == 403
    assert (
        await runtime.api.get("/api/tasks/T1/opencode/ses_current/pty/pty_1/connect")
    ).status_code == 403
    assert not runtime.native.sent


async def test_gateway_rejects_forged_binding_and_directory(runtime):
    response = await runtime.api.get("/api/tasks/T1/opencode/ses_other/session")
    assert response.status_code == 404
    response = await runtime.api.get(
        "/api/tasks/T1/opencode/ses_current/session", params={"directory": "/other"}
    )
    assert response.status_code == 403


async def test_native_composer_submission_uses_existing_session(runtime):
    response = await runtime.api.post(
        "/api/tasks/T1/opencode/ses_current/session/ses_current/prompt_async",
        json={"messageID": "msg_client1234", "parts": [{"type": "text", "text": "hello"}]},
    )
    assert response.status_code == 204
    assert runtime.native.sent[0][0] == "ses_current"
    assert runtime.native.sent[0][1]["messageID"] == "msg_client1234"


async def test_history_session_is_read_only(runtime):
    runtime.task.current_session_id = "ses_new"
    await runtime.session.commit()
    response = await runtime.api.post(
        "/api/tasks/T1/opencode/ses_current/session/ses_current/prompt_async", json={"parts": []}
    )
    assert response.status_code == 403


async def test_task_list_and_detail_have_identical_status(runtime):
    detail = (await runtime.api.get("/api/tasks/T1")).json()
    listing = (await runtime.api.get("/api/tasks", params={"status": "FAILED"})).json()
    assert detail["runtime_state"] == listing[0]["runtime_state"] == "FAILED"
    assert detail["runtime_status_label"] == listing[0]["runtime_status_label"] == "执行失败"


async def test_global_session_status_read_is_allowed_and_scoped(runtime, monkeypatch):
    from app.database import init_database

    await init_database()
    # "/session/status" textually matches the per-session read pattern. It is a global
    # read and must be forwarded + scoped to the task's sessions, never rejected as a
    # foreign session id literally named "status".
    class FakeResponse:
        is_error = False
        status_code = 200
        headers = {}
        content = b'{"ses_current": "idle", "ses_foreign": "busy"}'

        def json(self):
            return {"ses_current": "idle", "ses_foreign": "busy"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, *, params=None, json=None):
            assert url.endswith("/session/status")
            assert params["directory"] == str(runtime.workspace)
            return FakeResponse()

    monkeypatch.setattr("tests.platform.test_session_workspace.httpx.AsyncClient", FakeClient)
    response = await runtime.api.get("/api/tasks/T1/opencode/ses_current/session/status")
    assert response.status_code == 200, response.text
    assert response.json() == {"ses_current": "idle"}
    assert runtime.native.exchange_sessions == ["ses_current"]


async def test_native_config_uses_bound_employee_model_and_status_is_filtered(runtime, monkeypatch):
    """Regression: global status is not a Session ID, and composer defaults use the employee model."""
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.environment = {**binding.environment, "model_name": "deepseek/deepseek-flash"}
    await runtime.session.commit()
    original = httpx.AsyncClient

    def upstream(request):
        if request.url.path == "/config":
            return httpx.Response(200, json={"model": "other/image-model", "provider": {"secret": "private"}})
        return httpx.Response(200, json={"ses_current": {"type": "busy"}, "ses_other": {"type": "busy"}})

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    config = await runtime.api.get("/api/tasks/T1/opencode/ses_current/config")
    assert config.status_code == 200
    assert config.json() == {"model": "deepseek/deepseek-flash"}
    status = await runtime.api.get("/api/tasks/T1/opencode/ses_current/session/status")
    assert status.status_code == 200
    assert status.json() == {"ses_current": {"type": "busy"}}


async def test_v2_absolute_api_paths_are_proxied(runtime, monkeypatch):
    original = httpx.AsyncClient
    seen: list[str] = []

    def upstream(request):
        seen.append(request.url.path)
        assert request.url.params["location[directory]"] == str(runtime.workspace)
        if request.url.path == "/api/provider":
            return httpx.Response(404, json={"error": "missing"})
        if request.url.path == "/provider":
            return httpx.Response(200, json={"all": [{"id": "opencode", "models": {}}]})
        if request.url.path == "/api/session/active":
            return httpx.Response(200, json={"ses_current": {"type": "idle"}})
        if request.url.path == "/api/model":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/config":
            return httpx.Response(
                200,
                text="<!doctype html><html></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )
        if request.url.path == "/config":
            return httpx.Response(200, json={"model": "other/image-model", "provider": {"secret": "private"}})
        return httpx.Response(404, json={"error": "missing"})

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    provider = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/provider")
    assert provider.status_code == 200, provider.text
    assert seen[:2] == ["/api/provider", "/provider"]
    active = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/session/active")
    assert active.status_code == 200, active.text
    assert active.json() == {"ses_current": {"type": "idle"}}
    models = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/model")
    assert models.status_code == 200
    config = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/config")
    assert config.status_code == 200, config.text
    assert config.json() == {"model": "other/image-model"}
    assert seen[-2:] == ["/api/config", "/config"]
    foreign = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/session/ses_other/message")
    assert foreign.status_code == 403


async def test_v2_session_lists_stay_inside_one_task(runtime, monkeypatch):
    """Two tasks share one Worker environment. Each proxy only sees its own sessions."""

    workspace = str(runtime.workspace)
    snapshot = environment_snapshot(runtime.environment)
    runtime.environment.worker_channel = "worker"
    runtime.session.add(
        Task(
            id="T2",
            title="Other",
            task_type="ISSUE",
            status="RUNNING",
            employee_id="E1",
            current_session_id="ses_other",
            workspace=workspace,
            execution_environment_snapshot=snapshot,
        )
    )
    runtime.session.add(
        RuntimeSession(
            id="ses_other",
            task_id="T2",
            employee_id="E1",
            workspace=workspace,
            environment=snapshot,
        )
    )
    await runtime.session.commit()
    original = httpx.AsyncClient
    sessions = {
        "data": [{"id": "ses_current", "title": "one"}, {"id": "ses_other", "title": "two"}],
        "cursor": {"next": None},
    }
    active = {
        "data": {
            "ses_current": {"type": "busy"},
            "ses_other": {"type": "busy"},
        }
    }

    def upstream(request):
        assert request.url.params["location[directory]"] == workspace
        if request.url.path == "/api/session":
            return httpx.Response(200, json=sessions)
        if request.url.path == "/api/session/active":
            return httpx.Response(200, json=active)
        return httpx.Response(404, json={"error": "missing"})

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    own = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/session")
    assert own.status_code == 200, own.text
    assert own.json() == {
        "data": [{"id": "ses_current", "title": "one"}],
        "cursor": {"next": None},
    }
    other = await runtime.api.get("/api/tasks/T2/opencode/ses_other/api/session")
    assert other.status_code == 200, other.text
    assert other.json()["data"] == [{"id": "ses_other", "title": "two"}]
    own_active = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/session/active")
    assert own_active.status_code == 200, own_active.text
    assert own_active.json() == {"data": {"ses_current": {"type": "busy"}}}
    other_active = await runtime.api.get("/api/tasks/T2/opencode/ses_other/api/session/active")
    assert other_active.json()["data"] == {"ses_other": {"type": "busy"}}
    slashed = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/session/")
    assert slashed.status_code == 200, slashed.text
    assert slashed.json()["data"] == [{"id": "ses_current", "title": "one"}]
    doubled = await runtime.api.get("/api/tasks/T2/opencode/ses_other/api//session//")
    assert doubled.status_code == 200, doubled.text
    assert doubled.json()["data"] == [{"id": "ses_other", "title": "two"}]
    slashed_active = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/session/active/")
    assert slashed_active.status_code == 200, slashed_active.text
    assert slashed_active.json() == {"data": {"ses_current": {"type": "busy"}}}
    doubled_active = await runtime.api.get("/api/tasks/T2/opencode/ses_other/api/session//active//")
    assert doubled_active.json()["data"] == {"ses_other": {"type": "busy"}}


async def test_provider_model_and_config_responses_redact_gateway_keys(runtime, monkeypatch):
    secret = "gw-test-secret-value"
    original = httpx.AsyncClient

    def upstream(request):
        path = request.url.path
        if path == "/api/provider":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "opencode",
                            "api": {
                                "settings": {"apiKey": secret, "baseURL": "http://127.0.0.1:18110/v1"},
                                "options": {"token": secret},
                                "headers": {"Authorization": f"Bearer {secret}"},
                            },
                            "models": [
                                {
                                    "id": "m",
                                    "api": {"settings": {"api_key": secret, "temperature": 0}},
                                }
                            ],
                        }
                    ]
                },
            )
        if path == "/api/provider/opencode":
            return httpx.Response(
                200,
                json={"data": {"id": "opencode", "api": {"settings": {"apiKey": secret, "baseURL": "http://gw"}}}},
            )
        if path == "/api/model":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "opencode/deepseek-v4.1-flash-free",
                            "api": {
                                "settings": {"apiKey": secret},
                                "headers": {"authorization": secret, "password": secret},
                            },
                        }
                    ]
                },
            )
        if path == "/provider":
            return httpx.Response(
                200,
                json={
                    "all": [
                        {
                            "id": "opencode",
                            "key": secret,
                            "options": {"apiKey": secret},
                            "headers": {"authorization": secret},
                            "models": {
                                "m": {
                                    "id": "m",
                                    "options": {"apiKey": secret},
                                    "headers": {"secret": secret},
                                }
                            },
                        }
                    ]
                },
            )
        if path == "/config":
            return httpx.Response(
                200,
                json={
                    "model": "other/image-model",
                    "provider": {"secret": secret, "options": {"apiKey": secret}},
                },
            )
        return httpx.Response(404, json={"error": "missing"})

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    provider = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/provider")
    assert provider.status_code == 200, provider.text
    assert secret not in provider.text
    body = provider.json()
    assert body["data"][0]["api"]["settings"]["baseURL"] == "http://127.0.0.1:18110/v1"
    assert body["data"][0]["models"][0]["api"]["settings"]["temperature"] == 0
    one = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/provider/opencode")
    assert one.status_code == 200, one.text
    assert secret not in one.text
    assert one.json()["data"]["api"]["settings"]["baseURL"] == "http://gw"
    models = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/model")
    assert models.status_code == 200, models.text
    assert secret not in models.text
    assert models.json()["data"][0]["id"] == "opencode/deepseek-v4.1-flash-free"
    legacy = await runtime.api.get("/api/tasks/T1/opencode/ses_current/provider")
    assert legacy.status_code == 200, legacy.text
    assert secret not in legacy.text
    assert legacy.json()["all"][0]["models"]["m"]["options"] == {}
    config = await runtime.api.get("/api/tasks/T1/opencode/ses_current/config")
    assert config.status_code == 200, config.text
    assert secret not in config.text
    assert config.json() == {"model": "other/image-model"}
    v1_config = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/config")
    assert v1_config.status_code == 200, v1_config.text
    assert secret not in v1_config.text


async def test_v2_model_redacts_options_api_key_copied_into_request_body(runtime, monkeypatch):
    secret = "gw-test-secret-value"
    configured = "gw-workspace-config-key"
    (runtime.workspace / "opencode.json").write_text(
        json.dumps({"provider": {"liveprobe": {"options": {"apiKey": configured}}}}),
        encoding="utf-8",
    )
    original = httpx.AsyncClient

    def upstream(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "opencode/deepseek-v4.1-flash-free",
                        "request": {
                            "headers": {"x-api-key": secret, "accept": "application/json"},
                            "body": {
                                "apiKey": secret,
                                "model": "deepseek-v4.1-flash-free",
                                "copied": configured,
                            },
                        },
                    },
                    {
                        "id": "opencode/free-tier",
                        "request": {"body": {"apiKey": "public", "model": "free-model"}},
                    },
                ]
            },
        )

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    models = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/model")
    assert models.status_code == 200, models.text
    assert secret not in models.text
    assert configured not in models.text
    assert "public" not in models.text
    body = models.json()["data"]
    assert body[0]["request"]["headers"] == {"accept": "application/json"}
    assert body[0]["request"]["body"] == {
        "model": "deepseek-v4.1-flash-free",
        "copied": "[redacted]",
    }
    assert body[1]["request"]["body"] == {"model": "free-model"}


def test_redact_large_provider_catalog_stays_fast():
    secret = "gw-test-secret-value"
    catalog = {
        "data": [
            {
                "id": f"p{index}",
                "api": {
                    "settings": {"apiKey": secret, "baseURL": "http://gw"},
                    "headers": {"authorization": secret},
                },
                "models": [
                    {
                        "id": f"m{item}",
                        "api": {"settings": {"key": secret, "name": "keep"}},
                        "request": {"body": {"apiKey": secret, "model": "keep-model"}},
                    }
                    for item in range(20)
                ],
            }
            for index in range(200)
        ]
    }
    started = time.perf_counter()
    redact_secret_fields(catalog)
    assert time.perf_counter() - started < 1
    assert secret not in json.dumps(catalog)
    assert catalog["data"][0]["api"]["settings"]["baseURL"] == "http://gw"
    assert catalog["data"][0]["models"][0]["api"]["settings"]["name"] == "keep"
    assert catalog["data"][0]["models"][0]["request"]["body"] == {"model": "keep-model"}


async def test_native_abort_accepts_empty_body(runtime):
    await runtime.service.project(runtime.task, "RUNNING")
    response = await runtime.api.post("/api/tasks/T1/opencode/ses_current/session/ses_current/abort")
    assert response.status_code == 200
    assert runtime.task.status == "WAITING"


async def test_native_abort_can_continue_on_the_same_session(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "first"}]})
    response = await r.api.post("/api/tasks/T1/opencode/ses_current/session/ses_current/abort")
    assert response.status_code == 200
    binding = await r.session.get(RuntimeSession, "ses_current")
    assert binding.status == "INTERRUPTED"
    await r.service.refresh("T1")
    assert r.task.status == "WAITING"
    await r.service.send("T1", {"parts": [{"type": "text", "text": "continue"}]})
    assert r.task.status == "RUNNING"
    assert r.task.current_session_id == "ses_current"
    assert binding.status == "RUNNING"
    assert len(r.native.sent) == 2 and r.native.state == "busy"
    assert r.native.abort_count == 1


async def test_old_ack_does_not_abort_a_new_turn_in_the_same_session(runtime):
    r = runtime
    started, release = asyncio.Event(), asyncio.Event()
    original = r.native.request
    first_id = "msg_oldack001"

    async def delayed(method, path, **kwargs):
        result = await original(method, path, **kwargs)
        if kwargs.get("body", {}).get("messageID") == first_id:
            started.set()
            await release.wait()
        return result

    r.native.request = delayed
    first = asyncio.create_task(r.service.send("T1", {
        "messageID": first_id, "parts": [{"type": "text", "text": "first"}],
    }))
    await asyncio.wait_for(started.wait(), 2)
    await _complete_current_turn(r)
    async with r.factory() as session:
        await RuntimeService(session).send("T1", {
            "messageID": "msg_newack002", "parts": [{"type": "text", "text": "next"}],
        })
    release.set()
    await first
    assert r.task.last_message_id == "msg_newack002"
    assert r.task.status == "RUNNING"
    assert r.native.state == "busy" and r.native.abort_count == 0


async def test_gateway_rejects_workspace_override_and_foreign_question(runtime):
    response = await runtime.api.get(
        "/api/tasks/T1/opencode/ses_current/session", params={"workspace": "foreign"}
    )
    assert response.status_code == 403
    runtime.native.pending = [{"id": "q_foreign", "sessionID": "ses_other"}]
    response = await runtime.api.post(
        "/api/tasks/T1/opencode/ses_current/question/q_foreign/reply", json={"answers": [["yes"]]}
    )
    assert response.status_code == 403


async def test_native_permission_reply_is_allowed_only_for_task_session(runtime, monkeypatch):
    """原生 UI 用 /session/{sid}/permissions/{id} 回答权限，必须可用且仍受任务边界约束。"""

    runtime.native.pending_permissions = [{"id": "per_foreign", "sessionID": "ses_other"}]
    response = await runtime.api.post(
        "/api/tasks/T1/opencode/ses_current/session/ses_current/permissions/per_foreign",
        json={"response": "once"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "请求不属于此任务 Session"

    response = await runtime.api.post(
        "/api/tasks/T1/opencode/ses_other/session/ses_other/permissions/per_foreign",
        json={"response": "once"},
    )
    assert response.status_code == 404

    runtime.native.pending_permissions = [{"id": "per_own", "sessionID": "ses_current"}]
    original = httpx.AsyncClient
    sent = {}

    def handler(request):
        sent["method"] = request.method
        sent["path"] = request.url.path
        sent["query"] = dict(request.url.params)
        sent["body"] = json.loads(request.content or b"{}")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    response = await runtime.api.post(
        "/api/tasks/T1/opencode/ses_current/session/ses_current/permissions/per_own",
        json={"response": "once"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert sent["method"] == "POST"
    assert sent["path"] == "/session/ses_current/permissions/per_own"
    assert sent["body"] == {"response": "once"}
    assert sent["query"]["directory"] == str(runtime.workspace)


@pytest.mark.parametrize("terminal", ["SUCCEEDED", "CANCELED"])
async def test_native_permission_reply_is_denied_after_business_terminal(runtime, terminal):
    """业务终态后原生权限与问题答复都必须拒绝写入。"""

    runtime.task.status = terminal
    await runtime.session.commit()
    runtime.native.pending_permissions = [{"id": "per_own", "sessionID": "ses_current"}]
    for path in (
        "session/ses_current/permissions/per_own",
        "permission/per_own/reply",
    ):
        response = await runtime.api.post(f"/api/tasks/T1/opencode/ses_current/{path}", json={})
        assert response.status_code == 409, path


@pytest.mark.parametrize(
    ("path", "current_path"),
    [
        ("session/ses_prev/prompt_async", "session/ses_current/prompt_async"),
        ("session/ses_prev/permissions/per_own", "session/ses_current/permissions/per_own"),
        ("permission/per_own/reply", "permission/per_own/reply"),
    ],
)
async def test_bound_historical_session_rejects_native_writes(runtime, path, current_path):
    """任务自身已绑定的历史 Session 仍然只读，含原生权限答复。"""

    runtime.session.add(
        RuntimeSession(
            id="ses_prev",
            task_id="T1",
            employee_id="E1",
            workspace=str(runtime.workspace),
            environment=environment_snapshot(runtime.environment),
        )
    )
    await runtime.session.commit()
    runtime.native.pending_permissions = [{"id": "per_own", "sessionID": "ses_prev"}]
    response = await runtime.api.post(f"/api/tasks/T1/opencode/ses_prev/{path}", json={})
    assert response.status_code == 403
    assert response.json()["detail"] == "历史 Session 只读"
    assert not runtime.native.sent

    # 对照：同一原生操作在当前 Session 上不是被路径形状拒绝，证明上面的 403 只来自历史绑定。
    runtime.native.pending_permissions = [{"id": "per_own", "sessionID": "ses_current"}]
    current = await runtime.api.post(f"/api/tasks/T1/opencode/ses_current/{current_path}", json={})
    assert current.status_code != 403, current_path


async def test_timeout_aborts_native_busy_but_waiting_does_not_timeout(runtime):
    from datetime import UTC, datetime, timedelta

    await runtime.service.project(runtime.task, "RUNNING")
    runtime.task.turn_started_at = datetime.now(UTC) - timedelta(days=1)
    await runtime.session.commit()
    runtime.native.state = "busy"
    runtime.native.pending = [{"id": "q", "sessionID": "ses_current"}]
    await runtime.service.refresh("T1")
    assert runtime.task.status == "WAITING"
    assert runtime.native.state == "busy"
    runtime.native.pending = []
    await runtime.service.refresh("T1")
    assert runtime.task.status == "RUNNING"
    runtime.task.turn_started_at = datetime.now(UTC) - timedelta(days=1)
    await runtime.session.commit()
    await runtime.service.refresh("T1")
    assert runtime.native.state == "idle"
    assert runtime.task.status == "WAITING"
    assert "时间上限" in runtime.task.failure_reason


async def _complete_current_turn(r):
    r.native.state = "idle"
    assistant = r.native.sessions["ses_current"][-1]
    assistant["parts"] = [
        {"type": "text", "text": "DONE"},
        {"type": "tool", "tool": "bash", "callID": "call_1", "state": {"status": "completed"}},
    ]
    assistant["info"].update(
        {
            "time": {"completed": 1},
            "finish": "stop",
            "tokens": {"input": 100, "output": 20, "cache": {"read": 5}},
        }
    )
    await r.service.refresh("T1")


async def test_succeeded_persists_runtime_session_terminal(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    session = await r.session.get(RuntimeSession, "ses_current")
    assert r.task.status == "WAITING"
    assert session.status == "SUCCEEDED"
    assert session.finish_reason == "stop"
    assert session.finished_at is not None
    assert session.terminal_reason == "completed"


async def test_terminal_session_stores_aggregate_usage(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    session = await r.session.get(RuntimeSession, "ses_current")
    assert session.total_tokens == 125
    assert session.input_tokens == 100 and session.output_tokens == 20
    assert session.cache_read_tokens == 5
    assert session.llm_call_count == 1
    assert session.tool_call_count == 1
    assert session.tool_usage == [
        {
            "tool_name": "bash",
            "call_count": 1,
            "success_count": 1,
            "failure_count": 0,
            "duration_ms": 0,
        }
    ]


async def test_step_finish_tokens_do_not_double_count_message_usage(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    assistant = r.native.sessions["ses_current"][-1]
    assistant["parts"] = [
        {
            "type": "step-finish",
            "tokens": {"input": 100, "output": 20, "cache": {"read": 5}},
        },
        {"type": "text", "text": "DONE"},
    ]
    assistant["info"].update(
        {
            "time": {"completed": datetime.now(UTC).timestamp()},
            "finish": "stop",
            "tokens": {"input": 100, "output": 20, "cache": {"read": 5}},
        }
    )
    r.native.state = "idle"

    await r.service.refresh("T1")

    session = await r.session.get(RuntimeSession, "ses_current")
    assert session.total_tokens == 125
    assert session.llm_call_count == 1


async def test_continue_after_success_clears_finished_at(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    session = await r.session.get(RuntimeSession, "ses_current")
    assert session.finished_at is not None

    await r.service.send("T1", {"parts": [{"type": "text", "text": "again"}]})
    session = await r.session.get(RuntimeSession, "ses_current")
    assert r.task.status == "RUNNING"
    assert session.status == "RUNNING"
    assert session.finished_at is None

    await _complete_current_turn(r)
    session = await r.session.get(RuntimeSession, "ses_current")
    assert session.status == "SUCCEEDED" and session.finished_at is not None


async def test_restart_preserves_succeeded_old_status(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    await r.service.start("T1", restart=True)
    new_id = r.task.current_session_id
    old = await r.session.get(RuntimeSession, "ses_current")
    assert old.status == "SUCCEEDED"
    assert old.superseded_by == new_id
    new = await r.session.get(RuntimeSession, new_id)
    assert new.status == "RUNNING" and new.runtime_context_hash is not None








async def test_restart_allows_explicit_new_session_when_old_session_is_missing(runtime):
    r = runtime
    await r.service.project(r.task, "RUNNING")
    r.native.sessions.clear()

    await r.service.start("T1", restart=True)

    assert r.task.current_session_id != "ses_current"
    old = await r.session.get(RuntimeSession, "ses_current")
    assert old.status == "INTERRUPTED"
    assert old.terminal_reason == "session_missing"
    assert old.superseded_by == r.task.current_session_id


async def test_restart_aborts_old_session_with_old_environment_binding(runtime, tmp_path, monkeypatch):
    r = runtime
    old_root = r.task.execution_environment_snapshot["workspace_root"]
    new_root = tmp_path / "new-employee-root"
    new_root.mkdir()
    r.environment.workspace_root = str(new_root)
    await r.session.commit()

    observed_roots = []

    @asynccontextmanager
    async def observed_connect(environment, **kwargs):
        observed_roots.append(environment.workspace_root)
        yield r.native

    monkeypatch.setattr("app.runtime.lifecycle.connect", observed_connect)

    await r.service.start("T1", restart=True)

    assert observed_roots[0] == old_root
    assert str(new_root) in observed_roots[1:]
    assert r.task.execution_environment_snapshot["workspace_root"] == str(new_root)


async def test_restart_refuses_when_old_session_does_not_stop(runtime, monkeypatch):
    r = runtime
    await r.service.project(r.task, "RUNNING")

    class StuckClient:
        async def abort(self, *_args, **_kwargs):
            return True

        async def status(self, *_args, **_kwargs):
            return "busy"

    @asynccontextmanager
    async def stuck_connect(*_args, **_kwargs):
        yield StuckClient()

    monkeypatch.setattr("app.runtime.lifecycle.connect", stuck_connect)
    monkeypatch.setattr(
        "app.runtime.lifecycle.get_settings",
        lambda: SimpleNamespace(opencode_restart_abort_timeout_seconds=0),
    )
    with pytest.raises(RuntimeStateError, match="超时"):
        await r.service.start("T1", restart=True)
    # No second binding may be created while the old Session can still write.
    assert await r.session.scalar(select(func.count()).select_from(RuntimeSession)) == 1


async def test_disconnect_marks_session_unknown_without_failing_task(runtime):
    r = runtime
    await r.service.project(r.task, "RUNNING")
    await r.service.disconnected("T1", ConnectionError("network down"))
    session = await r.session.get(RuntimeSession, "ses_current")
    assert session.status == "UNKNOWN"
    assert session.terminal_reason == "transport_unknown"
    assert r.task.status == "RUNNING"
    assert r.task.waiting_reason == "TRANSPORT_RECOVERY"


async def test_refresh_recovers_unknown_session_to_terminal(runtime):
    r = runtime
    session = await r.session.get(RuntimeSession, "ses_current")
    session.status = "UNKNOWN"
    await r.session.commit()
    r.native.sessions["ses_current"] = [
        {"info": {"id": "u1", "role": "user"}, "parts": []},
        {
            "info": {"id": "a1", "role": "assistant", "time": {"completed": 1}, "finish": "stop"},
            "parts": [{"type": "text", "text": "恢复后确认执行完成"}],
        },
    ]
    r.task.last_message_id = "u1"
    await r.session.commit()
    r.native.state = "idle"
    await r.service.refresh("T1")
    session = await r.session.get(RuntimeSession, "ses_current")
    assert session.status == "SUCCEEDED"
    assert r.task.status == "WAITING"
    assert r.task.human_required_reason == "执行结束，待确认"


@pytest.mark.parametrize("params", [
    {"location[directory]": "/other"},
    {"location[workspace]": "foreign"},
    {"location": "foreign"},
])
async def test_gateway_rejects_current_protocol_location_override(runtime, params):
    response = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/reference", params=params)
    assert response.status_code == 403


async def test_native_vcs_diff_uses_bound_git_without_upstream(runtime):
    repo = runtime.workspace
    _init_repo(repo)
    (repo / "hello.ts").write_text("before\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True, capture_output=True)
    (repo / "hello.ts").write_text("after\n")
    base = "/api/tasks/T1/opencode/ses_current/vcs/diff"
    response = await runtime.api.get(base, params={"mode": "git"})
    assert response.status_code == 200
    assert response.json()[0]["before"] == "before\n"
    assert response.json()[0]["after"] == "after\n"
    raw = await runtime.api.get(base + "/raw", params={"mode": "git"})
    assert raw.status_code == 200
    assert "-before\n+after" in raw.text
    outside = await runtime.api.get(base, params={"directory": str(repo.parent)})
    assert outside.status_code == 403


@pytest.mark.parametrize("mode", ["git", "task"])
async def test_vcs_diff_redacts_configured_secrets_in_file_text(runtime, mode):
    secret = "diff-workspace-file-canary"
    repo = runtime.workspace
    _init_repo(repo)
    (repo / "opencode.json").write_text(
        json.dumps({"provider": {"liveprobe": {"options": {"apiKey": secret}}}}),
        encoding="utf-8",
    )
    (repo / "notes.txt").write_text(f"echo {secret}\ncurl -H 'Authorization: Bearer {secret}'\n")
    base = "/api/tasks/T1/opencode/ses_current/vcs/diff"
    response = await runtime.api.get(base, params={"mode": mode})
    assert response.status_code == 200, response.text
    assert secret not in response.text
    notes = next(item for item in response.json() if item["file"] == "notes.txt")
    assert notes["after"] == "echo [redacted]\ncurl -H 'Authorization: Bearer [redacted]'\n"
    assert "[redacted]" in notes["patch"]
    assert secret not in notes["patch"]
    raw = await runtime.api.get(base + "/raw", params={"mode": mode})
    assert raw.status_code == 200, raw.text
    assert raw.headers["content-type"].startswith("text/x-diff")
    assert secret not in raw.text
    assert "echo [redacted]" in raw.text
    assert "Authorization: Bearer [redacted]" in raw.text


async def test_diff_stays_redacted_when_remote_connect_fails(runtime, monkeypatch):
    secret = "diff-cached-remote-canary"
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.environment = {**binding.environment, "mode": "LINUX_REMOTE"}
    await runtime.session.commit()

    class Client:
        async def exchange(self, method, path, *, params=None, body=None, session_id=None, task_id=None):
            if path == "/config":
                payload = {"provider": {"nexus-gateway": {"options": {"apiKey": secret}}}}
                return 200, json.dumps(payload).encode(), {}
            return 200, b"{}", {}

    seeded = await load_proxy_secrets(
        binding.workspace,
        SimpleNamespace(mode="LINUX_REMOTE", id=runtime.environment.id, worker_epoch=0),
        Client(),
    )
    assert secret in seeded

    @asynccontextmanager
    async def fail_connect(*_args, **_kwargs):
        raise ConnectionError("opencode health failed")
        yield

    async def fake_diff(*_args, **_kwargs):
        return [
            {
                "file": "notes.txt",
                "patch": f"+echo {secret}\n",
                "before": "",
                "after": f"curl -H 'Authorization: Bearer {secret}'\n",
                "additions": 1,
                "deletions": 0,
                "status": "added",
            }
        ]

    monkeypatch.setattr("app.api.workspace_routes.connect", fail_connect)
    monkeypatch.setattr("app.api.workspace_routes.workspace_diff", fake_diff)
    base = "/api/tasks/T1/opencode/ses_current/vcs/diff"
    response = await runtime.api.get(base, params={"mode": "git"})
    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert response.json()[0]["after"] == "curl -H 'Authorization: Bearer [redacted]'\n"
    assert response.json()[0]["patch"] == "+echo [redacted]\n"
    raw = await runtime.api.get(base + "/raw", params={"mode": "git"})
    assert raw.status_code == 200, raw.text
    assert secret not in raw.text
    assert raw.text == "+echo [redacted]\n"


async def test_cold_remote_diff_fails_closed_without_leaking(runtime, monkeypatch):
    secret = "cold-diff-unredacted-canary"
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.environment = {**binding.environment, "mode": "LINUX_REMOTE"}
    await runtime.session.commit()
    await runtime.session.refresh(runtime.task)
    status_before = runtime.task.status
    (runtime.workspace / "notes.txt").write_text(secret + "\n")

    class Hanging:
        async def exchange(self, method, path, *, params=None, body=None, session_id=None, task_id=None):
            await asyncio.sleep(30)
            return 200, b"{}", {}

    class Stopped:
        async def exchange(self, method, path, *, params=None, body=None, session_id=None, task_id=None):
            raise ConnectionError("opencode stopped")

    clients = {"client": Hanging()}

    @asynccontextmanager
    async def open_client(*_args, **_kwargs):
        yield clients["client"]

    calls = {"diff": 0}

    async def fake_diff(*_args, **_kwargs):
        calls["diff"] += 1
        return [
            {
                "file": "notes.txt",
                "patch": f"+{secret}\n",
                "before": "",
                "after": secret,
                "additions": 1,
                "deletions": 0,
                "status": "added",
            }
        ]

    monkeypatch.setattr("app.api.workspace_routes.connect", open_client)
    monkeypatch.setattr("app.api.workspace_routes.workspace_diff", fake_diff)
    base = "/api/tasks/T1/opencode/ses_current/vcs/diff"
    started = time.perf_counter()
    hung = await runtime.api.get(base, params={"mode": "git"})
    assert time.perf_counter() - started < 8
    assert hung.status_code == 503, hung.text
    assert secret not in hung.text
    assert hung.json()["detail"] == "OpenCode 不可用，暂时无法安全显示 Diff"

    clients["client"] = Stopped()
    for mode in ("git", "task"):
        for path in (base, base + "/raw"):
            response = await runtime.api.get(path, params={"mode": mode})
            assert response.status_code == 503, response.text
            assert secret not in response.text
            assert response.json()["detail"] == "OpenCode 不可用，暂时无法安全显示 Diff"
    assert calls["diff"] == 0
    file_body = await runtime.api.get(
        "/api/tasks/T1/opencode/ses_current/file/content",
        params={"path": "notes.txt"},
    )
    assert file_body.status_code == 503, file_body.text
    assert secret not in file_body.text
    await runtime.session.refresh(runtime.task)
    assert runtime.task.status == status_before


async def test_global_config_alone_does_not_make_a_cold_diff_known(runtime, monkeypatch):
    secret = "global-only-config-canary-value"
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.environment = {**binding.environment, "mode": "LINUX_REMOTE"}
    await runtime.session.commit()

    class Client:
        async def exchange(self, method, path, *, params=None, body=None, session_id=None, task_id=None):
            if path == "/config":
                await asyncio.sleep(30)
                payload = {"provider": {"nexus-gateway": {"options": {"apiKey": secret}}}}
                return 200, json.dumps(payload).encode(), {}
            if path == "/global/config":
                return 200, b"{}", {}
            raise AssertionError(path)

    @asynccontextmanager
    async def open_client(*_args, **_kwargs):
        yield Client()

    calls = {"diff": 0}

    async def fake_diff(*_args, **_kwargs):
        calls["diff"] += 1
        return [
            {
                "file": "notes.txt",
                "patch": secret,
                "before": "",
                "after": secret,
                "additions": 1,
                "deletions": 0,
                "status": "added",
            }
        ]

    monkeypatch.setattr("app.api.workspace_routes.connect", open_client)
    monkeypatch.setattr("app.api.workspace_routes.workspace_diff", fake_diff)
    base = "/api/tasks/T1/opencode/ses_current/vcs/diff"
    for path in (base, base + "/raw"):
        response = await runtime.api.get(path, params={"mode": "git"})
        assert response.status_code == 503, response.text
        assert secret not in response.text
        assert response.json()["detail"] == "OpenCode 不可用，暂时无法安全显示 Diff"
    assert calls["diff"] == 0


async def test_session_diff_fails_closed_when_remote_config_is_unknown(runtime, monkeypatch):
    secret = "session-diff-gateway-canary"
    runtime.environment.worker_channel = "worker"
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.environment = {**binding.environment, "mode": "LINUX_REMOTE"}
    await runtime.session.commit()
    await runtime.session.refresh(runtime.task)
    status_before = runtime.task.status
    original = httpx.AsyncClient

    def upstream(request):
        if request.url.path == "/config":
            return httpx.Response(500, text="config down")
        if request.url.path == "/global/config":
            return httpx.Response(200, json={})
        if request.url.path == "/session/ses_current/diff":
            return httpx.Response(200, json=[{"file": "notes.txt", "patch": f"+{secret}\n", "after": secret}])
        return httpx.Response(404, json={"error": "missing"})

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    response = await runtime.api.get("/api/tasks/T1/opencode/ses_current/session/ses_current/diff")
    assert response.status_code == 503, response.text
    assert secret not in response.text
    assert response.json()["detail"] == "OpenCode 不可用，暂时无法安全显示 Diff"
    await runtime.session.refresh(runtime.task)
    assert runtime.task.status == status_before


async def test_historical_diff_reads_its_bound_workspace_after_relocation(runtime, tmp_path):
    r = runtime
    _init_repo(r.workspace)
    binding = await r.session.get(RuntimeSession, "ses_current")
    binding.workspace_base_commit = subprocess.check_output(
        ["git", "-C", str(r.workspace), "rev-parse", "HEAD"], text=True,
    ).strip()
    (r.workspace / "a.txt").write_text("old task changes\n")
    new_workspace = tmp_path / "relocated"
    new_workspace.mkdir()
    _init_repo(new_workspace)
    r.task.workspace = str(new_workspace)
    r.task.current_session_id = "ses_new"
    await r.session.commit()
    response = await r.api.get("/api/tasks/T1/opencode/ses_current/vcs/diff")
    assert response.status_code == 200
    assert response.json()[0]["after"] == "old task changes\n"


async def test_idle_employee_heartbeat_does_not_require_on_demand_server(runtime, monkeypatch):
    @asynccontextmanager
    async def unavailable_connect(*args, **kwargs):
        raise AssertionError("idle employee heartbeat must not start OpenCode Serve")
        yield

    monkeypatch.setattr("app.runtime.lifecycle.connect", unavailable_connect)
    employee = await runtime.session.get(AgentWorker, "E1")
    employee.status = "OFFLINE"
    runtime.task.status = "SUCCEEDED"
    await runtime.session.commit()

    healthy = await runtime.service.refresh_employee_heartbeats()

    await runtime.session.refresh(employee)
    assert "E1" in healthy
    assert employee.status == "IDLE"
    assert employee.current_task_count == 0


async def test_active_employee_heartbeat_releases_db_transaction_before_runtime_io(runtime, monkeypatch):
    observations = []

    @asynccontextmanager
    async def observed_connect(*args, **kwargs):
        observations.append((kwargs.get("start"), runtime.session.in_transaction()))
        yield runtime.native

    monkeypatch.setattr("app.runtime.lifecycle.connect", observed_connect)
    runtime.task.status = "RUNNING"
    await runtime.session.commit()

    await runtime.service.refresh_employee_heartbeats()

    assert observations == [(True, False)]


async def test_stop_can_force_cancel_local_session_when_no_runtime_listener(runtime, monkeypatch):
    @asynccontextmanager
    async def unavailable_connect(*args, **kwargs):
        raise ConnectionError("runtime process is gone")
        yield

    monkeypatch.setattr("app.runtime.lifecycle.connect", unavailable_connect)
    await runtime.service.project(runtime.task, "WAITING")

    stopped = await runtime.service.stop("T1")

    assert stopped.status == "CANCELED"
    session = await runtime.session.get(RuntimeSession, "ses_current")
    assert session.status == "INTERRUPTED"
    assert session.terminal_reason == "aborted"


async def test_manual_confirm_allows_continuing_without_confirmation(runtime):
    r = runtime
    r.task.completion_policy = "MANUAL_CONFIRM"
    await r.session.commit()
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    assert r.task.status == "WAITING"
    await r.service.send("T1", {"parts": [{"type": "text", "text": "another"}]})
    assert r.task.current_session_id == "ses_current"
    assert len(r.native.sent) == 2
    assert r.task.submitted_at is None


async def test_new_turn_after_confirmation_requires_renewed_confirmation(runtime):
    r = runtime
    r.task.completion_policy = "MANUAL_CONFIRM"
    await r.session.commit()

    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    await r.api.post("/api/tasks/T1/confirm")
    await r.session.refresh(r.task)
    assert r.task.status == "SUCCEEDED"
    assert r.task.confirmed_at is not None

    # Periodic refresh must not revert a confirmed terminal task to WAITING.
    await r.service.refresh("T1")
    await r.session.refresh(r.task)
    assert r.task.status == "SUCCEEDED"
    assert r.task.confirmed_at is not None

    with pytest.raises(RuntimeStateError, match="已结束"):
        await r.service.send("T1", {"parts": [{"type": "text", "text": "new turn"}]})
    assert r.task.status == "SUCCEEDED"
    assert len(r.native.sent) == 1


async def test_child_question_answer_resumes_parent_session(runtime):
    r = runtime
    r.native.sessions["ses_child"] = []
    r.native.children_by_session["ses_current"] = [{"id": "ses_child"}]
    r.native.state = "busy"
    r.native.pending = [{"id": "q_child", "sessionID": "ses_child"}]

    await r.service.refresh("T1")
    assert r.task.status == "WAITING"

    # Answering the child question clears the native wait; the same parent Session
    # must resume RUNNING without creating a replacement Session.
    r.native.pending = []
    await r.service.refresh("T1")
    assert r.task.status == "RUNNING"
    assert r.task.current_session_id == "ses_current"
    assert await r.session.scalar(select(func.count()).select_from(RuntimeSession)) == 1


async def test_child_session_usage_counts_toward_runtime_session(runtime):
    r = runtime
    r.task.status = "RUNNING"
    await r.session.commit()
    r.native.sessions["ses_child"] = [
        {
            "info": {"id": "c1", "role": "assistant", "tokens": {"input": 7, "output": 3}},
            "parts": [],
        },
        {
            "info": {"id": "c2", "role": "assistant"},
            "parts": [{"type": "tool", "tool": "read", "state": {"status": "completed"}}],
        },
    ]
    r.native.children_by_session["ses_current"] = [{"id": "ses_child"}]
    r.native.state = "idle"

    await r.service.refresh("T1")

    session = await r.session.get(RuntimeSession, "ses_current")
    assert session.input_tokens == 7
    assert session.output_tokens == 3
    assert session.total_tokens == 10
    assert session.tool_call_count == 1
    assert session.tool_usage[0]["tool_name"] == "read"


async def test_task_summary_records_committed_changes_with_clean_tree(runtime, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init")
    git("config", "user.email", "e2e@example.com")
    git("config", "user.name", "e2e")
    (repo / "a.py").write_text("a\n")
    git("add", ".")
    git("commit", "-m", "base")
    baseline = git("rev-parse", "HEAD")
    (repo / "b.py").write_text("b\n")
    git("add", ".")
    git("commit", "-m", "add b")
    assert git("status", "--porcelain") == ""  # committed work, clean tree

    r = runtime
    r.task.workspace = str(repo)
    r.task.execution_environment_snapshot = {
        **r.task.execution_environment_snapshot,
        "workspace_root": str(repo),
    }
    binding = await r.session.get(RuntimeSession, "ses_current")
    binding.workspace = str(repo)
    binding.workspace_base_commit = baseline
    await r.session.commit()

    changes = await collect_workspace_changes(r.session, r.task)

    assert changes["baseline_commit"] == baseline
    assert "b.py" in changes["files"]
    assert len(changes["commits"]) == 1




def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "a@b.c"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"],
        check=True,
        capture_output=True,
        text=True,
    )


def _workspace_environment(root: Path) -> OpenCodeExecutionEnvironment:
    return OpenCodeExecutionEnvironment(
        id="ENV1",
        employee_id="E1",
        mode="LOCAL",
        name="test",
        workspace_root=str(root),
        opencode_server_port=54198,
    )


async def test_prepare_workspace_reuses_a_valid_residual_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    environment = _workspace_environment(repo)
    task = Task(id="T9", title="t", task_type="ISSUE")
    target = Path(repo).parent / ".astra-tasks" / "E1" / "T9"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(target), "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )

    # A crashed run's worktree is legal residue and must be reused idempotently.
    assert await prepare_workspace(task, environment, reuse_existing=True) == str(target)
    assert await prepare_workspace(task, environment, reuse_existing=True) == str(target)


async def test_prepare_workspace_rejects_a_foreign_directory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    environment = _workspace_environment(repo)
    task = Task(id="T9", title="t", task_type="ISSUE")
    target = Path(repo).parent / ".astra-tasks" / "E1" / "T9"
    target.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="不是合法的 Git worktree"):
        await prepare_workspace(task, environment, reuse_existing=True)


async def test_prepare_workspace_refuses_missing_reuse_workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    environment = _workspace_environment(repo)
    task = Task(id="T9", title="t", task_type="ISSUE", workspace=str(tmp_path / "gone"))

    with pytest.raises(RuntimeError, match="不存在或不可用"):
        await prepare_workspace(task, environment, reuse_existing=True)


async def test_prepare_workspace_reuses_a_valid_bound_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    environment = _workspace_environment(repo)
    target = Path(repo).parent / ".astra-tasks" / "E1" / "T9"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(target), "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    task = Task(id="T9", title="t", task_type="ISSUE", workspace=str(target))

    assert await prepare_workspace(task, environment, reuse_existing=True) == str(target)


async def test_prepare_workspace_reuses_a_plain_bound_directory(tmp_path):
    root = tmp_path / "plain-root"
    root.mkdir()
    workspace = root / "task-workspace"
    workspace.mkdir()
    environment = _workspace_environment(root)
    task = Task(id="T9", title="t", task_type="ISSUE", workspace=str(workspace))

    assert await prepare_workspace(task, environment, reuse_existing=True) == str(workspace)


async def test_task_diff_retains_committed_and_uncommitted_unicode_files(runtime):
    r = runtime
    _init_repo(r.workspace)
    baseline = subprocess.check_output(
        ["git", "-C", str(r.workspace), "rev-parse", "HEAD"], text=True,
    ).strip()
    binding = await r.session.get(RuntimeSession, "ses_current")
    binding.workspace_base_commit = baseline
    await r.session.commit()
    file = r.workspace / "中文 文件.txt"
    file.write_text("first\n")
    subprocess.run(["git", "-C", str(r.workspace), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r.workspace), "commit", "-m", "result"], check=True, capture_output=True)
    cumulative = await workspace_diff(r.session, r.task)
    assert [item["file"] for item in cumulative] == [file.name]
    assert cumulative[0]["after"] == "first\n"
    assert cumulative[0]["status"] == "added"
    assert await workspace_diff(r.session, r.task, cumulative=False) == []
    file.write_text("second\n")
    current = await workspace_diff(r.session, r.task, cumulative=False)
    assert current[0]["before"] == "first\n" and current[0]["after"] == "second\n"
    (r.workspace / "new.txt").write_text("untracked\n")
    assert {item["file"] for item in await workspace_diff(r.session, r.task)} == {file.name, "new.txt"}
    summary = await collect_workspace_changes(r.session, r.task)
    assert summary["files"] == ["new.txt", file.name]
    assert summary["changed_files"] == 2


async def test_completion_rejects_native_busy_and_pending_requests(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    with pytest.raises(RuntimeStateError, match="仍在执行"):
        await r.service.confirm_completion("T1")
    await _complete_current_turn(r)
    r.native.pending = [{"id": "q1", "sessionID": "ses_current"}]
    with pytest.raises(RuntimeStateError, match="问题或权限"):
        await r.service.confirm_completion("T1")
    r.native.pending = []
    await r.service.confirm_completion("T1")
    assert r.task.status == "SUCCEEDED"
    await r.service.refresh("T1")
    assert r.task.status == "SUCCEEDED"


async def test_busy_observation_does_not_read_full_messages(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    async def unexpected(*args):
        raise AssertionError("busy observation must not fetch full history")
    r.native.messages = unexpected
    await r.service.refresh("T1")
    assert r.task.status == "RUNNING"


async def test_idle_completed_turn_does_not_repeat_full_history_reads(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    async def unexpected(*args):
        raise AssertionError("idle completed turn must not fetch full history again")
    r.native.messages = unexpected
    await r.service.refresh("T1")
    assert r.task.status == "WAITING"


async def test_new_prompt_resets_timeout_window_after_idle(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "first"}]})
    await _complete_current_turn(r)
    r.task.turn_started_at = datetime.now(UTC) - timedelta(days=1)
    await r.session.commit()
    await r.service.send("T1", {"parts": [{"type": "text", "text": "next"}]})
    await r.service.refresh("T1")
    assert r.native.abort_count == 0
    assert r.task.status == "RUNNING"


async def test_task_worktree_inherits_project_config_and_preserves_its_own(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _init_repo(root)
    (root / "opencode.json").write_text('{"model":"company/model"}')
    env = _workspace_environment(root)
    task = Task(id="T9", title="t", task_type="ISSUE")
    task.workspace = await prepare_workspace(task, env)
    config = Path(task.workspace) / "opencode.json"
    assert config.read_text() == (root / "opencode.json").read_text()
    config.write_text('{"model":"task/override"}')
    await prepare_workspace(task, env)
    assert config.read_text() == '{"model":"task/override"}'


@pytest.mark.parametrize("invalid", ["plain", "nested", "symlink", "foreign", "missing"])
async def test_remote_worktree_shell_rejects_invalid_reuse(tmp_path, invalid):
    """The Agent prepares the task directory locally. Invalid reuse still fails."""
    root = tmp_path / "project"
    root.mkdir()
    _init_repo(root)
    target = tmp_path / "task"
    if invalid == "plain":
        target.mkdir()
    elif invalid == "nested":
        target = root / "nested"
        target.mkdir()
    elif invalid == "symlink":
        target.symlink_to(root, target_is_directory=True)
    elif invalid == "foreign":
        target.mkdir()
        _init_repo(target)
    with pytest.raises(RuntimeError):
        prepare_workspace_local(str(root), str(target), reuse_existing=True)


async def test_remote_worktree_shell_reuses_absolute_common_dir_and_config(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _init_repo(root)
    (root / "opencode.json").write_text('{"model":"company/model"}')
    target = tmp_path / "task"
    prepare_workspace_local(str(root), str(target))
    prepare_workspace_local(str(root), str(target), reuse_existing=True)
    assert (target / "opencode.json").read_text() == (root / "opencode.json").read_text()


async def test_retry_then_final_error_waits_without_releasing_worker(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    r.native.state = "retry"
    assistant = r.native.sessions["ses_current"][-1]
    assistant["info"]["error"] = {"name": "RateLimit"}
    await r.service.refresh("T1")
    assert r.task.status == "RUNNING"
    binding = await r.session.get(RuntimeSession, "ses_current")
    assert binding.status == "RUNNING"

    r.native.state = "idle"
    await r.service.refresh("T1")
    assert binding.status == "FAILED"
    assert binding.terminal_reason == "assistant_error"
    assert r.task.status == "WAITING"
    assert "执行错误" in r.task.human_required_reason
    employee = await r.session.get(AgentWorker, "E1")
    assert employee.current_task_count == 1
    assert employee.status == "WAIT_HUMAN"


async def test_idle_without_reply_converges_after_grace(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    anchor = r.task.last_message_id
    r.native.sessions["ses_current"] = [{"info": {"id": anchor, "role": "user"}, "parts": []}]
    r.native.state = "idle"
    r.task.turn_started_at = datetime.now(UTC) - timedelta(minutes=5)
    await r.session.commit()
    await r.service.refresh("T1")
    binding = await r.session.get(RuntimeSession, "ses_current")
    assert binding.status == "FAILED"
    assert binding.terminal_reason == "no_reply"
    assert r.task.status == "WAITING"


async def test_missing_anchor_keeps_task_status_and_records_transport_recovery(runtime):
    r = runtime
    r.native.sessions["ses_current"] = [
        {"info": {"id": "old", "role": "assistant", "finish": "stop", "time": {"completed": 1}},
         "parts": [{"type": "text", "text": "上一轮"}]},
    ]
    r.native.state = "idle"
    r.task.status = "RUNNING"
    r.task.last_message_id = "msg_missing_round"
    r.task.turn_started_at = datetime.now(UTC) - timedelta(minutes=5)
    await r.session.commit()
    await r.service.refresh("T1")
    binding = await r.session.get(RuntimeSession, "ses_current")
    assert binding.status == "INTERRUPTED"
    assert binding.terminal_reason == "unconfirmed"
    assert r.task.status == "RUNNING"
    assert r.task.waiting_reason == "TRANSPORT_RECOVERY"
    assert r.task.status != "SUCCEEDED"


async def test_disconnect_keeps_task_and_worker_until_reconcile(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    session_id = r.task.current_session_id
    await r.service.disconnected("T1", ConnectionError("reset"))
    assert r.task.status == "RUNNING"
    assert r.task.runtime_connection == "UNAVAILABLE"
    assert "状态待确认" in r.task.runtime_error
    binding = await r.session.get(RuntimeSession, session_id)
    assert binding.status == "UNKNOWN"
    employee = await r.session.get(AgentWorker, "E1")
    assert employee.current_task_count == 1

    await _complete_current_turn(r)
    assert r.task.current_session_id == session_id
    assert r.task.status == "WAITING"
    assert binding.status == "SUCCEEDED"
    assert await r.session.scalar(select(func.count()).select_from(RuntimeSession)) == 1


async def test_acceptance_confirm_releases_worker_and_continue_reuses_session(runtime):
    from app.runtime.dispatcher import Dispatcher

    r = runtime
    r.task.responsible_employee_id = "E1"
    employee = await r.session.get(AgentWorker, "E1")
    employee.category_ids = ["testing"]
    employee.task_types = ["ISSUE"]
    r.task.category_id = "testing"
    await r.session.commit()
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    assert r.task.status == "WAITING"
    assert employee.current_task_count == 1

    queued = Task(
        id="T2", title="other", task_type="ISSUE", status="PENDING", category_id="testing",
    )
    r.session.add(queued)
    await r.session.commit()
    assert await Dispatcher(r.session).claim_once() is None

    await r.service.confirm_completion("T1")
    assert r.task.status == "SUCCEEDED"
    assert employee.current_task_count == 0
    assert employee.status == "IDLE"


async def test_confirmation_skips_review_when_disabled(runtime, monkeypatch):
    from app.config.settings import get_settings
    from app.models import ExperienceReviewJob

    monkeypatch.setattr(get_settings(), "experience_review_enabled", False)
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    await r.service.confirm_completion("T1")
    count = await r.session.scalar(
        select(func.count()).select_from(ExperienceReviewJob).where(
            ExperienceReviewJob.task_id_snapshot == "T1"
        )
    )
    assert count == 0


async def test_restart_supersedes_pending_experience_review_job(runtime, monkeypatch):
    from app.config.settings import get_settings
    from app.models import ExperienceReviewJob

    monkeypatch.setattr(get_settings(), "experience_review_enabled", True)
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    await r.service.confirm_completion("T1")
    job = await r.session.scalar(
        select(ExperienceReviewJob).where(ExperienceReviewJob.task_id_snapshot == "T1")
    )
    assert job is not None and job.status == "PENDING"

    await r.service.start("T1", restart=True)
    await r.session.refresh(job)
    assert job.status == "SUPERSEDED"
    assert job.error_code == "new_round"


async def test_confirmation_creates_one_experience_review_job(runtime, monkeypatch):
    from app.config.settings import get_settings
    from app.models import ExperienceReviewJob

    monkeypatch.setattr(get_settings(), "experience_review_enabled", True)
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    await r.service.confirm_completion("T1")
    job = await r.session.scalar(
        select(ExperienceReviewJob).where(ExperienceReviewJob.task_id_snapshot == "T1")
    )
    assert job is not None
    assert job.status == "PENDING"
    assert job.source_snapshot["root_session_id"] == r.task.current_session_id
    await r.service.confirm_completion("T1")
    count = await r.session.scalar(
        select(func.count()).select_from(ExperienceReviewJob).where(
            ExperienceReviewJob.task_id_snapshot == "T1"
        )
    )
    assert count == 1


async def test_continue_after_acceptance_returns_to_running(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    session_id = r.task.current_session_id
    await r.service.send("T1", {"parts": [{"type": "text", "text": "revise"}]})
    assert r.task.status == "RUNNING"
    assert r.task.current_session_id == session_id


async def test_repeated_refresh_does_not_duplicate_waiting_wake(runtime):
    from app.models import Event, TaskWakeOutbox

    r = runtime
    r.task.responsible_employee_id = "E1"
    await r.session.commit()
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    await r.service.refresh("T1")
    waiting = (
        await r.session.scalars(
            select(Event).where(Event.task_id == "T1", Event.event_type == "TASK_WAITING")
        )
    ).all()
    pending = (
        await r.session.scalars(
            select(TaskWakeOutbox).where(
                TaskWakeOutbox.task_id == "T1", TaskWakeOutbox.status == "pending",
            )
        )
    ).all()
    assert len(waiting) == 1
    assert len(pending) == 1
    assert "执行结束，待确认" in pending[0].hint


async def test_cancel_keeps_worker_until_execution_stops(runtime, monkeypatch):
    from app.config.settings import get_settings

    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    employee = await r.session.get(AgentWorker, "E1")
    assert employee.current_task_count == 1

    async def stuck_abort(directory, sid):
        r.native.abort_count += 1
        r.native.state = "busy"
        return True

    r.native.abort = stuck_abort
    monkeypatch.setattr(get_settings(), "opencode_restart_abort_timeout_seconds", 1)
    with pytest.raises(RuntimeStateError, match="未释放 Worker"):
        await r.service.stop("T1")
    assert r.task.status == "RUNNING"
    assert employee.current_task_count == 1


async def test_human_retry_keeps_inflight_when_message_read_fails(runtime):
    r = runtime
    pending_id = "msg_readfail0001"
    since = (datetime.now(UTC) - timedelta(seconds=180)).isoformat()
    r.task.pending_message_id = pending_id
    r.task.pending_attempt = 1
    r.task.pending_prompt = {
        "action": "prompt_async",
        "body": {"messageID": pending_id, "parts": [{"type": "text", "text": "retry"}]},
        "in_flight": True,
        "in_flight_since": since,
    }
    r.task.submission_attempts = {
        f"{pending_id}:1": {"request_id": "req_read", "body_fetched_at": None},
    }
    await r.session.commit()

    async def broken_page(*_args, **_kwargs):
        raise ConnectionError("page failed")

    r.native.messages_page = broken_page
    with pytest.raises(ConnectionError, match="page failed"):
        await r.service.retry_submission("T1")
    await r.session.refresh(r.task)
    assert r.task.pending_prompt["in_flight"] is True
    assert r.task.pending_prompt["in_flight_since"] != since
    assert r.task.runtime_connection == "UNAVAILABLE"
    assert not r.native.sent


async def test_human_retry_refuses_without_fetch_evidence(runtime):
    r = runtime
    pending_id = "msg_noevidence01"
    r.task.pending_message_id = pending_id
    r.task.pending_prompt = {
        "action": "prompt_async",
        "body": {"messageID": pending_id, "parts": [{"type": "text", "text": "retry"}]},
    }
    await r.session.commit()
    with pytest.raises(RuntimeStateError, match="请求记录"):
        await r.service.retry_submission("T1")
    assert not r.native.sent

    r.task.pending_attempt = 1
    r.task.submission_attempts = {
        f"{pending_id}:1": {
            "request_id": "req_fetched",
            "body_fetched_at": datetime.now(UTC).isoformat(),
        },
    }
    await r.session.commit()
    with pytest.raises(RuntimeStateError, match="结果未知"):
        await r.service.retry_submission("T1", confirm_unknown=True)
    assert not r.native.sent


async def test_unfetched_attempt_can_be_retried_once(runtime):
    r = runtime
    pending_id = "msg_unfetched001"
    r.task.status = "RUNNING"
    r.task.last_message_id = pending_id
    r.task.pending_message_id = pending_id
    r.task.pending_attempt = 1
    r.task.pending_prompt = {
        "action": "prompt_async",
        "body": {"messageID": pending_id, "parts": [{"type": "text", "text": "again"}]},
    }
    r.task.submission_attempts = {
        f"{pending_id}:1": {"request_id": "req_unfetched", "body_fetched_at": None},
    }
    await r.session.commit()
    sessions = set(r.native.sessions)
    await r.service.retry_submission("T1")
    assert len(r.native.sent) == 1
    assert set(r.native.sessions) == sessions
    assert r.task.current_session_id == "ses_current"


async def test_visible_prompt_refresh_does_not_resend_or_open_a_session(runtime):
    r = runtime
    pending_id = "msg_seen0000001"
    r.native.sessions["ses_current"] = [
        {"info": {"id": pending_id, "role": "user"}, "parts": [{"type": "text", "text": "go"}]}
    ]
    r.native.state = "idle"
    r.task.status = "RUNNING"
    r.task.pending_message_id = pending_id
    r.task.last_message_id = pending_id
    r.task.pending_attempt = 1
    r.task.pending_prompt = {
        "action": "prompt_async",
        "body": {"messageID": pending_id, "parts": [{"type": "text", "text": "go"}]},
        "in_flight": True,
        "in_flight_since": datetime.now(UTC).isoformat(),
    }
    await r.session.commit()
    sessions = set(r.native.sessions)
    reads = []

    def _track(name, method):
        async def wrapped(*args, **kwargs):
            reads.append(name)
            return await method(*args, **kwargs)

        return wrapped

    r.native.status = _track("status", r.native.status)
    r.native.messages_page = _track("messages_page", r.native.messages_page)
    r.native.questions = _track("questions", r.native.questions)
    r.native.permissions = _track("permissions", r.native.permissions)
    await r.service.refresh("T1")
    await r.session.refresh(r.task)
    assert reads[:4] == ["status", "questions", "permissions", "messages_page"]
    assert r.native.sent == []
    assert set(r.native.sessions) == sessions
    assert r.task.current_session_id == "ses_current"
    assert r.task.pending_message_id is None
    assert r.task.pending_prompt is None


async def test_refresh_reads_later_pages_before_clearing_pending(runtime):
    r = runtime
    pending_id = "msg_page2000001"
    pages = {
        None: {
            "messages": [{"info": {"id": "msg_other000001", "role": "user"}, "parts": []}],
            "next_before": "cursor-2",
        },
        "cursor-2": {
            "messages": [
                {"info": {"id": pending_id, "role": "user"}, "parts": [{"type": "text", "text": "go"}]}
            ],
            "next_before": None,
        },
    }

    async def messages_page(_directory, _sid, *, limit, before=None):
        assert limit == 100
        return pages[before]

    r.native.messages_page = messages_page
    r.native.state = "idle"
    r.task.status = "RUNNING"
    r.task.pending_message_id = pending_id
    r.task.last_message_id = pending_id
    r.task.pending_attempt = 1
    r.task.pending_prompt = {
        "action": "prompt_async",
        "body": {"messageID": pending_id, "parts": [{"type": "text", "text": "go"}]},
        "in_flight": True,
        "in_flight_since": datetime.now(UTC).isoformat(),
    }
    await r.session.commit()
    await r.service.refresh("T1")
    await r.session.refresh(r.task)
    assert r.task.pending_message_id is None
    assert r.task.runtime_error is None


async def test_refresh_stops_at_the_message_page_bound(runtime):
    from app.runtime.lifecycle import _MAX_MESSAGE_PAGES

    r = runtime
    calls = {"n": 0}

    async def messages_page(_directory, _sid, *, limit, before=None):
        calls["n"] += 1
        return {
            "messages": [{"info": {"id": f"msg_{calls['n']:03}", "role": "assistant"}, "parts": []}],
            "next_before": f"cursor-{calls['n']}",
        }

    r.native.messages_page = messages_page
    r.native.state = "idle"
    r.task.status = "RUNNING"
    r.task.pending_message_id = "msg_missing"
    r.task.last_message_id = "msg_missing"
    r.task.pending_attempt = 1
    r.task.pending_prompt = {"action": "prompt_async", "body": {"messageID": "msg_missing"}}
    await r.session.commit()
    await r.service.refresh("T1")
    await r.session.refresh(r.task)
    assert calls["n"] == _MAX_MESSAGE_PAGES
    assert r.task.pending_message_id == "msg_missing"


async def test_cancel_does_not_release_worker_until_human_confirms(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await r.service.stop("T1")
    employee = await r.session.get(AgentWorker, "E1")
    await r.session.refresh(r.task)
    await r.session.refresh(employee)
    assert r.task.status == "CANCELED"
    assert employee.current_task_count == 1
    assert employee.status == "WORKING"

    rejected = await r.api.post(
        "/api/tasks/T1/confirm-execution-stop",
        json={"reason": "已经看过进程"},
    )
    assert rejected.status_code == 409

    missing_operator = await r.api.post(
        "/api/tasks/T1/confirm-execution-stop",
        json={"reason": "已经看过进程"},
        headers={"X-Astra-User-Id": "   "},
    )
    assert missing_operator.status_code == 409

    response = await r.api.post(
        "/api/tasks/T1/confirm-execution-stop",
        json={"reason": "已经看过进程"},
        headers={"X-Astra-User-Id": "ada"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELED"
    await r.session.refresh(r.task)
    await r.session.refresh(employee)
    assert r.task.execution_stop_operator == "ada"
    assert r.task.execution_stop_reason == "已经看过进程"
    assert r.task.execution_stop_session_id == r.task.current_session_id
    assert r.task.execution_stop_attempt == r.task.pending_attempt
    assert employee.current_task_count == 0
    assert employee.status == "IDLE"


async def test_restart_clears_execution_stop_confirmation(runtime):
    r = runtime
    await r.service.stop("T1")
    await r.service.confirm_execution_stop("T1", operator="ada", reason="已停止")
    employee = await r.session.get(AgentWorker, "E1")
    await r.session.refresh(employee)
    assert employee.current_task_count == 0

    await r.service.start("T1", restart=True)
    await r.session.refresh(r.task)
    await r.session.refresh(employee)
    assert r.task.execution_stop_confirmed_at is None
    assert r.task.current_session_id != "ses_current"
    assert employee.current_task_count == 1


async def test_remote_worker_redacts_configured_secret_values(runtime, monkeypatch):
    """The workspace lives on the Worker. Value redaction still uses OpenCode config."""

    canary = "remote-worker-api-key-canary"
    runtime.environment.worker_channel = "worker"
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.workspace = "/srv/worker/task"
    binding.environment = {**binding.environment, "mode": "LINUX_REMOTE"}
    runtime.task.workspace = "/srv/worker/task"
    await runtime.session.commit()
    original = httpx.AsyncClient

    def upstream(request):
        if request.url.path == "/config":
            return httpx.Response(
                200,
                json={"provider": {"nexus-gateway": {"options": {"apiKey": canary, "baseURL": "http://gw"}}}},
            )
        if request.url.path == "/global/config":
            return httpx.Response(200, json={})
        if request.url.path == "/api/model":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "opencode/keep",
                            "request": {"body": {"copied": canary, "model": "keep-model"}},
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": "missing"})

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    models = await runtime.api.get("/api/tasks/T1/opencode/ses_current/api/model")
    assert models.status_code == 200, models.text
    assert canary not in models.text
    assert models.json()["data"][0]["request"]["body"] == {
        "copied": "[redacted]",
        "model": "keep-model",
    }
    assert models.json()["data"][0]["id"] == "opencode/keep"


async def test_upstream_error_bodies_are_redacted_before_return(runtime, monkeypatch):
    secret = "upstream-error-body-canary"
    (runtime.workspace / "opencode.json").write_text(
        json.dumps({"provider": {"liveprobe": {"options": {"apiKey": secret}}}}),
        encoding="utf-8",
    )
    original = httpx.AsyncClient
    calls = {"n": 0}

    def upstream(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "apiKey": "err-field-api-key-canary",
                        "nested": [
                            {"token": "err-field-token-canary", "message": "kept"},
                            secret,
                        ],
                        "detail": "failed",
                    }
                },
            )
        return httpx.Response(
            502,
            text=f"gateway rejected {secret}",
            headers={"content-type": "text/plain"},
        )

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    failed = await runtime.api.get("/api/tasks/T1/opencode/ses_current/session/ses_current/message")
    assert failed.status_code == 400, failed.text
    assert "err-field-api-key-canary" not in failed.text
    assert "err-field-token-canary" not in failed.text
    assert secret not in failed.text
    assert failed.json() == {
        "error": {"nested": [{"message": "kept"}], "detail": "failed"}
    }
    text = await runtime.api.get("/api/tasks/T1/opencode/ses_current/session/ses_current/message")
    assert text.status_code == 502, text.text
    assert secret not in text.text
    assert text.text == "gateway rejected [redacted]"


async def test_message_history_redacts_secret_names_and_embedded_values(runtime, monkeypatch):
    """Successful session history is redacted even though it is not a config path."""

    configured = "history-config-api-key-canary"
    field = "history-field-api-key-canary"
    (runtime.workspace / "opencode.json").write_text(
        json.dumps({"provider": {"liveprobe": {"options": {"apiKey": configured}}}}),
        encoding="utf-8",
    )
    seen: list[str] = []
    original = httpx.AsyncClient

    def upstream(request):
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json=[
                {
                    "info": {
                        "id": "msg_user",
                        "role": "user",
                        "tokens": {
                            "input": 8,
                            "output": 1,
                            "reasoning": 0,
                            "cache": {"read": 0, "write": 0},
                        },
                        "input_tokens": 8,
                        "output_tokens": 1,
                    },
                    "parts": [{"type": "text", "text": f"user prompt {configured} please"}],
                },
                {
                    "info": {"id": "msg_asst", "role": "assistant", "apiKey": field},
                    "parts": [
                        {
                            "type": "tool",
                            "state": {
                                "input": {"command": f"curl -H 'Authorization: Bearer {configured}'"},
                                "output": f"echo {configured}",
                            },
                        }
                    ],
                },
            ],
        )

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    history = await runtime.api.get("/api/tasks/T1/opencode/ses_current/session/ses_current/message")
    assert history.status_code == 200, history.text
    assert configured not in history.text
    assert field not in history.text
    assert "/config" not in seen
    body = history.json()
    assert body[0]["parts"][0]["text"] == "user prompt [redacted] please"
    assert body[0]["info"]["tokens"]["input"] == 8
    assert body[0]["info"]["input_tokens"] == 8
    assert body[0]["info"]["output_tokens"] == 1
    assert "apiKey" not in body[1]["info"]
    state = body[1]["parts"][0]["state"]
    assert state["input"]["command"] == "curl -H 'Authorization: Bearer [redacted]'"
    assert state["output"] == "echo [redacted]"


async def test_remote_config_secrets_are_read_once_inside_the_cache_window(runtime, monkeypatch):
    canary = "cached-remote-config-canary"
    runtime.environment.worker_channel = "worker"
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.workspace = "/srv/worker/task"
    binding.environment = {**binding.environment, "mode": "LINUX_REMOTE"}
    runtime.task.workspace = "/srv/worker/task"
    await runtime.session.commit()
    seen: list[str] = []
    original = httpx.AsyncClient

    def upstream(request):
        seen.append(request.url.path)
        if request.url.path == "/config":
            return httpx.Response(
                200,
                json={"provider": {"nexus-gateway": {"options": {"apiKey": canary}}}},
            )
        if request.url.path == "/global/config":
            return httpx.Response(200, json={})
        if request.url.path == "/session/ses_current/message":
            return httpx.Response(
                200,
                json=[{"parts": [{"type": "text", "text": f"echo {canary}"}]}],
            )
        return httpx.Response(404, json={"error": "missing"})

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    first = await runtime.api.get("/api/tasks/T1/opencode/ses_current/session/ses_current/message")
    second = await runtime.api.get("/api/tasks/T1/opencode/ses_current/session/ses_current/message")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert canary not in first.text
    assert canary not in second.text
    assert first.json()[0]["parts"][0]["text"] == "echo [redacted]"
    assert second.json()[0]["parts"][0]["text"] == "echo [redacted]"
    assert seen.count("/config") == 1
    assert seen.count("/global/config") == 1
    assert seen.count("/session/ses_current/message") == 2


FAST_503_ROUTES = (
    "session/ses_current/diff",
    "file/content?path=notes.txt",
    "find?pattern=canary",
)


async def _remote_binding(runtime):
    runtime.environment.worker_channel = "worker"
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.environment = {**binding.environment, "mode": "LINUX_REMOTE"}
    await runtime.session.commit()
    await runtime.session.refresh(runtime.task)


@pytest.mark.parametrize("route", FAST_503_ROUTES)
async def test_file_body_routes_fail_fast_when_opencode_is_down_and_cache_is_cold(
    runtime, monkeypatch, route
):
    from app.api import workspace_routes

    await _remote_binding(runtime)
    status_before = runtime.task.status
    monkeypatch.setattr(workspace_routes, "_SECRET_FETCH_TIMEOUT_SECONDS", 0.5)
    seen: list[bool] = []

    class Hanging:
        async def exchange(self, *_args, **_kwargs):
            await asyncio.sleep(60)

    @asynccontextmanager
    async def stopped_opencode(*_args, start=True, **_kwargs):
        seen.append(start)
        if start:
            await asyncio.sleep(60)  # health check against a stopped OpenCode
        yield Hanging()

    monkeypatch.setattr(workspace_routes, "connect", stopped_opencode)
    loop = asyncio.get_running_loop()
    started = loop.time()
    response = await asyncio.wait_for(
        runtime.api.get(f"/api/tasks/T1/opencode/ses_current/{route}"), timeout=10
    )
    assert loop.time() - started < 6
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "OpenCode 不可用，暂时无法安全显示 Diff"}
    assert True not in seen  # never reached health / session_tree
    await runtime.session.refresh(runtime.task)
    assert runtime.task.status == status_before


@pytest.mark.parametrize("route", FAST_503_ROUTES)
async def test_file_body_routes_redact_with_a_warm_cache(runtime, monkeypatch, route):
    from app.api import workspace_routes

    secret = "warm-cache-file-body-canary"
    # Treat the local fixture as remote for the secret gate only.
    monkeypatch.setattr(workspace_routes, "_needs_remote_secrets", lambda *_args: True)
    key = await workspace_routes._remote_secret_cache_key(runtime.environment, str(runtime.workspace))
    workspace_routes._REMOTE_SECRET_CACHE[key] = (
        asyncio.get_running_loop().time() + 1e9, frozenset({secret})
    )
    original = httpx.AsyncClient

    def upstream(request):
        if request.url.path in {"/config", "/global/config"}:
            return httpx.Response(500, text="down")
        if request.url.path == "/session/ses_current/diff":
            return httpx.Response(200, json=[{"file": "notes.txt", "patch": f"+{secret}\n"}])
        if request.url.path == "/file/content":
            return httpx.Response(200, json={"type": "text", "content": f"KEY={secret}"})
        if request.url.path == "/find":
            return httpx.Response(200, json=[{"path": "notes.txt", "lines": {"text": secret}}])
        return httpx.Response(404, json={"error": "missing"})

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    response = await runtime.api.get(f"/api/tasks/T1/opencode/ses_current/{route}")
    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert "[redacted]" in response.text


async def test_file_body_route_reads_config_once_with_a_stale_cache(runtime, monkeypatch):
    from app.api import workspace_routes

    secret = "stale-cache-file-body-canary"
    monkeypatch.setattr(workspace_routes, "_needs_remote_secrets", lambda *_args: True)
    key = await workspace_routes._remote_secret_cache_key(runtime.environment, str(runtime.workspace))
    workspace_routes._REMOTE_SECRET_CACHE[key] = (0.0, frozenset({secret}))  # expired
    original = httpx.AsyncClient
    config_reads: list[str] = []

    def upstream(request):
        if request.url.path in {"/config", "/global/config"}:
            config_reads.append(request.url.path)
            return httpx.Response(500, text="down")
        if request.url.path == "/file/content":
            return httpx.Response(200, json={"type": "text", "content": f"KEY={secret}"})
        return httpx.Response(404, json={"error": "missing"})

    monkeypatch.setattr(
        "tests.platform.test_session_workspace.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs),
    )
    response = await runtime.api.get("/api/tasks/T1/opencode/ses_current/file/content?path=notes.txt")
    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert sorted(config_reads) == ["/config", "/global/config"]

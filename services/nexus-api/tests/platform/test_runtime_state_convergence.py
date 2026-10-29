"""原生证据不完整、停止核实和并发控制状态的回归。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.config.settings import get_settings
from app.models import AgentWorker, RuntimeSession
from app.runtime.lifecycle import RuntimeStateError
from tests.platform import test_session_workspace

runtime = test_session_workspace.runtime


async def prepare(runtime):
    runtime.task.status = "RUNNING"
    runtime.task.last_message_id = "u1"
    runtime.task.turn_started_at = datetime.now(UTC) - timedelta(minutes=5)
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.status = "RUNNING"
    employee = await runtime.session.get(AgentWorker, "E1")
    employee.status = "WORKING"
    employee.current_task_count = 1
    runtime.native.sessions["ses_current"] = [
        {"info": {"id": "u1", "role": "user"}, "parts": []},
        {"info": {"id": "a1", "role": "assistant"}, "parts": []},
    ]
    await runtime.session.commit()
    return binding


async def test_old_idle_is_rechecked_after_messages(runtime):
    binding = await prepare(runtime)
    original = runtime.native.messages

    async def become_busy(directory, sid):
        runtime.native.state = "busy"
        return await original(directory, sid)

    runtime.native.messages = become_busy
    await runtime.service.refresh("T1", client=runtime.native)
    assert binding.status == "RUNNING"
    assert runtime.task.status == "RUNNING"
    assert binding.finished_at is None


async def test_incomplete_result_stays_unknown_and_can_finish(runtime):
    binding = await prepare(runtime)
    await runtime.service.refresh("T1", client=runtime.native)
    assert binding.status == "UNKNOWN"
    assert binding.terminal_reason == "incomplete"
    assert binding.finished_at is None
    assert runtime.task.status == "RUNNING"
    assert runtime.task.waiting_reason == "TRANSPORT_RECOVERY"
    runtime.native.sessions["ses_current"][-1] = {
        "info": {"id": "a1", "role": "assistant", "finish": "stop", "time": {"completed": 1}},
        "parts": [{"type": "text", "text": "done"}],
    }
    await runtime.service.refresh("T1", client=runtime.native)
    assert binding.status == "SUCCEEDED"
    assert runtime.task.waiting_reason == "EXECUTION_RESULT_REVIEW"
    assert runtime.task.runtime_error is None


@pytest.mark.parametrize("old_reason", ["incomplete", "no_reply", "aborted", "timeout", "assistant_error"])
async def test_old_terminal_projection_does_not_hide_completed_transcript(runtime, old_reason):
    binding = await prepare(runtime)
    binding.status = "FAILED" if old_reason == "assistant_error" else "INTERRUPTED"
    binding.terminal_reason = old_reason
    binding.finished_at = datetime.now(UTC)
    runtime.task.status = "WAITING"
    runtime.native.sessions["ses_current"][-1] = {
        "info": {"id": "a1", "role": "assistant", "finish": "stop", "time": {"completed": 1}},
        "parts": [{"type": "text", "text": "done"}],
    }
    await runtime.session.commit()
    await runtime.service.refresh("T1", client=runtime.native)
    assert binding.status == "SUCCEEDED"
    assert runtime.task.waiting_reason == "EXECUTION_RESULT_REVIEW"


@pytest.mark.parametrize("reason", ["aborted", "timeout"])
async def test_confirmed_stop_is_preserved_with_incomplete_history(runtime, reason):
    binding = await prepare(runtime)
    binding.status, binding.terminal_reason = "INTERRUPTED", reason
    stopped_at = datetime.now(UTC)
    binding.finished_at = stopped_at
    runtime.task.status = "WAITING"
    await runtime.session.commit()
    await runtime.service.refresh("T1", client=runtime.native)
    assert binding.status == "INTERRUPTED"
    assert binding.terminal_reason == reason
    assert binding.finished_at == stopped_at
    assert runtime.task.status == "WAITING"


@pytest.mark.parametrize("accepted", [False, True])
async def test_timeout_keeps_running_until_stop_is_confirmed(runtime, monkeypatch, accepted):
    binding = await prepare(runtime)
    runtime.task.turn_started_at = datetime.now(UTC) - timedelta(days=1)
    runtime.native.state = "busy"
    await runtime.session.commit()
    monkeypatch.setattr(get_settings(), "opencode_restart_abort_timeout_seconds", 0)

    async def ineffective_abort(*args):
        return accepted

    runtime.native.abort = ineffective_abort
    await runtime.service.refresh("T1", client=runtime.native)
    assert runtime.native.state == "busy"
    assert binding.status == "RUNNING"
    assert binding.finished_at is None
    assert runtime.task.status == "RUNNING"
    assert "超时" in runtime.task.runtime_error
    employee = await runtime.session.get(AgentWorker, "E1")
    assert employee.current_task_count == 1


async def test_timeout_accepts_missing_session_as_stop_confirmation(runtime):
    binding = await prepare(runtime)
    runtime.task.turn_started_at = datetime.now(UTC) - timedelta(days=1)
    runtime.native.state = "busy"
    await runtime.session.commit()

    async def disappear(*args):
        runtime.native.sessions.clear()
        return True

    runtime.native.abort = disappear
    await runtime.service.refresh("T1", client=runtime.native)
    assert binding.status == "INTERRUPTED"
    assert binding.terminal_reason == "timeout"
    assert runtime.task.status == "WAITING"


async def test_unknown_native_status_is_not_stop_confirmation(runtime, monkeypatch):
    await prepare(runtime)
    runtime.native.state = "unknown"
    monkeypatch.setattr(get_settings(), "opencode_restart_abort_timeout_seconds", 0.01)
    with pytest.raises(RuntimeStateError):
        await runtime.service._wait_for_session_stop(runtime.native, runtime.workspace, "ses_current")


async def test_timeout_does_not_interrupt_while_a_child_session_is_busy(runtime, monkeypatch):
    binding = await prepare(runtime)
    runtime.task.turn_started_at = datetime.now(UTC) - timedelta(days=1)
    runtime.native.state = "busy"
    await runtime.session.commit()
    monkeypatch.setattr(get_settings(), "opencode_restart_abort_timeout_seconds", 0.01)
    original_status = runtime.native.status
    original_abort = runtime.native.abort

    async def child_created_during_abort(directory, sid):
        accepted = await original_abort(directory, sid)
        runtime.native.sessions["ses_child"] = []
        runtime.native.children_by_session["ses_current"] = [{"id": "ses_child"}]
        return accepted

    async def child_still_busy(directory, sid):
        return "busy" if sid == "ses_child" else await original_status(directory, sid)

    runtime.native.status = child_still_busy
    runtime.native.abort = child_created_during_abort
    await runtime.service.refresh("T1", client=runtime.native)
    assert runtime.native.state == "idle"
    assert binding.status == "UNKNOWN"
    assert binding.finished_at is None
    assert runtime.task.status == "RUNNING"
    assert "停止尚未确认" in runtime.task.runtime_error
    assert (await runtime.session.get(AgentWorker, "E1")).current_task_count == 1


async def test_timeout_does_not_abort_an_unconfirmed_submission(runtime):
    binding = await prepare(runtime)
    runtime.task.turn_started_at = datetime.now(UTC) - timedelta(days=1)
    runtime.task.pending_message_id = "u1"
    runtime.task.pending_prompt = {"body": {"messageID": "u1"}}
    runtime.native.state = "busy"
    await runtime.session.commit()
    await runtime.service.refresh("T1", client=runtime.native)
    assert runtime.native.abort_count == 0
    assert binding.status == "RUNNING"


async def test_heartbeat_preserves_pause_over_a_cached_employee(runtime):
    await prepare(runtime)
    employee = await runtime.session.get(AgentWorker, "E1")
    employee.status = "OFFLINE"
    await runtime.session.commit()

    async with runtime.factory() as other:
        await other.execute(update(AgentWorker).where(AgentWorker.id == "E1").values(status="PAUSED"))
        await other.commit()

    async def unexpected_probe():
        raise AssertionError("occupied Worker must not be probed")

    runtime.native.health = unexpected_probe
    await runtime.service.refresh_employee_heartbeats()
    await runtime.session.refresh(employee)
    assert employee.status == "PAUSED"
    assert employee.current_task_count == 1


async def test_busy_worker_is_not_marked_offline_by_probe_age(runtime):
    from app.runtime.heartbeat import mark_stale_employees_offline

    await prepare(runtime)
    employee = await runtime.session.get(AgentWorker, "E1")
    employee.last_heartbeat = datetime.now(UTC) - timedelta(days=1)
    await runtime.session.commit()
    assert await mark_stale_employees_offline(runtime.session) == []
    assert employee.status == "WORKING"


async def test_timeout_does_not_swallow_transport_errors(runtime):
    await prepare(runtime)
    runtime.task.turn_started_at = datetime.now(UTC) - timedelta(days=1)
    runtime.native.state = "busy"
    await runtime.session.commit()

    async def unreachable(*args):
        raise ConnectionError("disconnected")

    runtime.native.abort = unreachable
    with pytest.raises(ConnectionError):
        await runtime.service.refresh("T1", client=runtime.native)
    assert runtime.task.status == "RUNNING"


async def test_timeout_stop_transport_loss_is_not_an_interruption(runtime):
    binding = await prepare(runtime)
    runtime.task.turn_started_at = datetime.now(UTC) - timedelta(days=1)
    runtime.native.state = "busy"
    await runtime.session.commit()
    original = runtime.native.status
    calls = 0

    async def disconnect_after_abort(directory, sid):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ConnectionError("stop unconfirmed")
        return await original(directory, sid)

    runtime.native.status = disconnect_after_abort
    with pytest.raises(ConnectionError):
        await runtime.service.refresh("T1", client=runtime.native)
    assert binding.status == "RUNNING"
    assert binding.finished_at is None
    await runtime.service.disconnected("T1", ConnectionError("stop unconfirmed"))
    assert binding.status == "UNKNOWN"
    assert runtime.task.status == "RUNNING"
    assert (await runtime.session.get(AgentWorker, "E1")).current_task_count == 1

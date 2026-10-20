"""Codex 复审：历史成功投影、等待原因通知、unconfirmed 恢复、取消与新输入互斥。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

import tests.platform.test_session_workspace as session_workspace
from app.models import AgentWorker, Event, RuntimeSession, TaskWakeOutbox
from app.runtime.lifecycle import RuntimeService, RuntimeStateError


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    async for value in session_workspace.runtime.__wrapped__(tmp_path, monkeypatch):
        yield value

_ACCEPTANCE = "执行结束，待确认"


async def _waiting_counts(session):
    waiting = await session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.task_id == "T1", Event.event_type == "TASK_WAITING")
    )
    pending = await session.scalar(
        select(func.count())
        .select_from(TaskWakeOutbox)
        .where(TaskWakeOutbox.task_id == "T1", TaskWakeOutbox.status == "pending")
    )
    return waiting, pending


async def test_existing_succeeded_session_reprojects_running_task(runtime):
    r = runtime
    r.task.status = "RUNNING"
    r.task.responsible_employee_id = "E1"
    r.task.human_required_reason = None
    r.task.current_progress = "执行中"
    binding = await r.session.get(RuntimeSession, "ses_current")
    binding.status = "SUCCEEDED"
    binding.terminal_reason = "completed"
    binding.finish_reason = "stop"
    r.native.state = "idle"
    await r.session.commit()

    await r.service.refresh("T1")
    assert r.task.status == "WAITING"
    assert r.task.human_required_reason == _ACCEPTANCE
    assert r.task.current_progress == _ACCEPTANCE
    employee = await r.session.get(AgentWorker, "E1")
    assert employee.current_task_count == 1
    before = await _waiting_counts(r.session)

    await r.service.refresh("T1")
    assert r.task.status == "WAITING"
    assert r.task.human_required_reason == _ACCEPTANCE
    assert await _waiting_counts(r.session) == before


async def test_question_to_acceptance_wakes_for_the_new_result(runtime):
    r = runtime
    r.task.status = "RUNNING"
    r.task.responsible_employee_id = "E1"
    r.task.last_message_id = "msg_question01"
    r.native.state = "idle"
    r.native.pending = [{"sessionID": "ses_current", "id": "q1"}]
    r.native.sessions["ses_current"] = [
        {
            "info": {"id": "msg_question01", "role": "user"},
            "parts": [{"type": "text", "text": "need a decision"}],
        }
    ]
    await r.session.commit()

    await r.service.refresh("T1")
    assert r.task.status == "WAITING"
    assert "回答问题" in (r.task.human_required_reason or "")
    question_wakes = (
        await r.session.scalars(select(TaskWakeOutbox).where(TaskWakeOutbox.task_id == "T1"))
    ).all()
    assert len(question_wakes) == 1
    assert question_wakes[0].status == "pending"
    question_wakes[0].status = "delivered"
    await r.session.commit()

    r.native.pending = []
    r.native.state = "idle"
    r.native.sessions["ses_current"] = [
        {
            "info": {"id": "msg_question01", "role": "user"},
            "parts": [{"type": "text", "text": "need a decision"}],
        },
        {
            "info": {
                "id": "asst_question01",
                "role": "assistant",
                "finish": "stop",
                "time": {"completed": 1},
            },
            "parts": [{"type": "text", "text": "done"}],
        },
    ]
    await r.service.refresh("T1")
    assert r.task.status == "WAITING"
    assert r.task.human_required_reason == _ACCEPTANCE
    assert r.task.current_progress == _ACCEPTANCE
    pending = (
        await r.session.scalars(
            select(TaskWakeOutbox).where(
                TaskWakeOutbox.task_id == "T1", TaskWakeOutbox.status == "pending"
            )
        )
    ).all()
    assert len(pending) == 1
    assert "执行结束" in pending[0].hint
    delivered = await r.session.scalar(
        select(func.count())
        .select_from(TaskWakeOutbox)
        .where(TaskWakeOutbox.task_id == "T1", TaskWakeOutbox.status == "delivered")
    )
    assert delivered == 1
    before = await _waiting_counts(r.session)

    await r.service.refresh("T1")
    assert r.task.current_progress == _ACCEPTANCE
    assert await _waiting_counts(r.session) == before


async def test_unconfirmed_round_recovers_when_native_messages_reappear(runtime):
    r = runtime
    r.task.status = "WAITING"
    r.task.human_required_reason = "当前轮结果无法确认，状态待确认，可在 Workspace 核对后继续"
    r.task.last_message_id = "u1"
    binding = await r.session.get(RuntimeSession, "ses_current")
    binding.status = "INTERRUPTED"
    binding.terminal_reason = "unconfirmed"
    r.native.state = "idle"
    r.native.sessions["ses_current"] = [
        {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "go"}]},
        {
            "info": {
                "id": "a1",
                "role": "assistant",
                "finish": "stop",
                "time": {"completed": 1},
            },
            "parts": [{"type": "text", "text": "done"}],
        },
    ]
    await r.session.commit()

    await r.service.refresh("T1")
    await r.session.refresh(binding)
    assert binding.status == "SUCCEEDED"
    assert binding.terminal_reason == "completed"
    assert r.task.status == "WAITING"
    assert r.task.human_required_reason == _ACCEPTANCE
    assert r.task.current_progress == _ACCEPTANCE
    before = await _waiting_counts(r.session)

    await r.service.refresh("T1")
    await r.session.refresh(binding)
    assert binding.status == "SUCCEEDED"
    assert await _waiting_counts(r.session) == before


async def test_cancel_does_not_release_worker_after_a_concurrent_new_turn(runtime):
    r = runtime
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    employee = await r.session.get(AgentWorker, "E1")
    assert employee.current_task_count == 1
    assert r.native.state == "busy"

    gate = asyncio.Event()
    release = asyncio.Event()
    original = r.native.messages

    async def hooked(directory, sid):
        gate.set()
        await release.wait()
        return await original(directory, sid)

    r.native.messages = hooked
    stopping = asyncio.create_task(r.service.stop("T1"))
    await asyncio.wait_for(gate.wait(), 2)
    send_error = None
    try:
        async with r.factory() as other:
            try:
                await RuntimeService(other).send(
                    "T1", {"parts": [{"type": "text", "text": "next"}]}
                )
            except RuntimeStateError as exc:
                send_error = str(exc)
    finally:
        release.set()
    await stopping

    await r.session.refresh(r.task)
    await r.session.refresh(employee)
    if send_error:
        assert "取消" in send_error
        assert r.task.status == "CANCELED"
        assert r.task.pending_message_id is None
        assert employee.current_task_count == 1
        assert r.native.state != "busy"
    else:
        assert r.task.status != "CANCELED"
        assert r.task.pending_message_id
        assert employee.current_task_count == 1


def _cancel_token(seconds: int = 300) -> dict:
    return {
        "cancel_token": {
            "id": "tok-inflight",
            "until": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
            "close": True,
        }
    }


def _left_running_after_close(native, task, employee) -> bool:
    return native.state == "busy" and task.status == "CANCELED" and employee.current_task_count == 0


async def _delay_prompt_until_released(native, release: asyncio.Event, started: asyncio.Event):
    original = native.request

    async def delayed(method, path, **kwargs):
        if method == "POST" and path.endswith("/prompt_async"):
            started.set()
            await release.wait()
            await original(method, path, **kwargs)
            raise ConnectionError("ACK lost")
        return await original(method, path, **kwargs)

    native.request = delayed


async def test_delayed_send_after_cancel_does_not_leave_native_busy(runtime):
    r = runtime
    started, release = asyncio.Event(), asyncio.Event()
    await _delay_prompt_until_released(r.native, release, started)
    sending = asyncio.create_task(
        r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    )
    await asyncio.wait_for(started.wait(), 2)
    employee = await r.session.get(AgentWorker, "E1")
    await r.session.refresh(r.task)
    await r.session.refresh(employee)
    assert r.task.pending_message_id
    assert employee.current_task_count == 1

    with pytest.raises(RuntimeStateError, match="取消中"):
        await r.service.stop("T1")
    await r.service.refresh("T1")
    await r.session.refresh(r.task)
    await r.session.refresh(employee)
    assert r.task.status != "CANCELED"
    assert r.task.pending_message_id
    assert employee.current_task_count == 1
    assert not _left_running_after_close(r.native, r.task, employee)

    release.set()
    with pytest.raises(ConnectionError, match="ACK lost"):
        await sending
    await r.session.refresh(r.task)
    await r.session.refresh(employee)
    assert not _left_running_after_close(r.native, r.task, employee)
    assert r.task.status == "CANCELED"
    assert r.task.pending_message_id is None
    assert employee.current_task_count == 1
    assert r.native.state != "busy"


async def test_delayed_retry_after_cancel_does_not_leave_native_busy(runtime):
    r = runtime
    pending_id = "msg_retrylate01"
    r.task.status = "RUNNING"
    r.task.last_message_id = pending_id
    r.task.pending_message_id = pending_id
    r.task.pending_prompt = {
        "action": "prompt_async",
        "body": {"messageID": pending_id, "parts": [{"type": "text", "text": "retry"}]},
    }
    r.task.pending_attempt = 1
    r.task.submission_attempts = {
        f"{pending_id}:1": {"request_id": "req_seed_cancel", "body_fetched_at": None},
    }
    employee = await r.session.get(AgentWorker, "E1")
    employee.current_task_count = 1
    employee.status = "WORKING"
    await r.session.commit()

    started, release = asyncio.Event(), asyncio.Event()
    await _delay_prompt_until_released(r.native, release, started)
    retrying = asyncio.create_task(r.service.retry_submission("T1"))
    await asyncio.wait_for(started.wait(), 2)

    with pytest.raises(RuntimeStateError, match="取消中"):
        await r.service.stop("T1")
    await r.session.refresh(r.task)
    await r.session.refresh(employee)
    assert r.task.status == "RUNNING"
    assert r.task.pending_message_id == pending_id
    assert employee.current_task_count == 1

    release.set()
    with pytest.raises(ConnectionError, match="ACK lost"):
        await retrying
    await r.session.refresh(r.task)
    await r.session.refresh(employee)
    assert not _left_running_after_close(r.native, r.task, employee)
    assert r.task.status == "CANCELED"
    assert r.task.pending_message_id is None
    assert employee.current_task_count == 1
    assert r.native.state != "busy"


async def test_refresh_finishes_cancel_when_inflight_sender_is_gone(runtime):
    r = runtime
    pending_id = "msg_crash01"
    employee = await r.session.get(AgentWorker, "E1")
    employee.current_task_count = 1
    employee.status = "WORKING"
    r.task.status = "RUNNING"
    r.task.last_message_id = pending_id
    r.task.pending_message_id = pending_id
    r.task.suspension = _cancel_token()
    r.task.pending_prompt = {
        "action": "prompt_async",
        "body": {"messageID": pending_id, "parts": [{"type": "text", "text": "go"}]},
        "in_flight": True,
        "in_flight_since": datetime.now(UTC).isoformat(),
    }
    r.native.state = "idle"
    r.native.sessions["ses_current"] = []
    await r.session.commit()

    await r.service.refresh("T1")
    await r.session.refresh(r.task)
    await r.session.refresh(employee)
    assert r.task.status == "RUNNING"
    assert employee.current_task_count == 1

    r.task.pending_prompt = {
        **r.task.pending_prompt,
        "in_flight_since": (datetime.now(UTC) - timedelta(seconds=120)).isoformat(),
    }
    await r.session.commit()
    await r.service.refresh("T1")
    await r.session.refresh(r.task)
    await r.session.refresh(employee)
    assert r.task.status == "CANCELED"
    assert r.task.pending_message_id is None
    assert employee.current_task_count == 1
    assert r.native.state != "busy"


async def test_refresh_finishes_cancel_when_inflight_message_landed(runtime):
    r = runtime
    pending_id = "msg_crash02"
    employee = await r.session.get(AgentWorker, "E1")
    employee.current_task_count = 1
    employee.status = "WORKING"
    r.task.status = "RUNNING"
    r.task.last_message_id = pending_id
    r.task.pending_message_id = pending_id
    r.task.suspension = _cancel_token()
    r.task.pending_prompt = {
        "action": "prompt_async",
        "body": {"messageID": pending_id, "parts": [{"type": "text", "text": "go"}]},
        "in_flight": True,
        "in_flight_since": datetime.now(UTC).isoformat(),
    }
    r.native.state = "busy"
    r.native.sessions["ses_current"] = [
        {
            "info": {"id": pending_id, "role": "user"},
            "parts": [{"type": "text", "text": "go"}],
        }
    ]
    await r.session.commit()

    await r.service.refresh("T1")
    await r.session.refresh(r.task)
    await r.session.refresh(employee)
    assert not _left_running_after_close(r.native, r.task, employee)
    assert r.task.status == "CANCELED"
    assert employee.current_task_count == 1
    assert r.native.state != "busy"

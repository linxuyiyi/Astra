"""取证探针：断言当前缺陷可重现，不作为期望行为的回归测试。

从 services/nexus-api 运行 pytest -c pytest.ini -q -s 此文件的绝对路径。
仅使用现有隔离 SQLite fixture 和受控原生客户端，不访问真实 Worker。
"""

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "services/nexus-api"))

# 在导入 app 前启用仓库现有的隔离测试配置。
importlib.import_module("tests.platform.conftest")
from app.models import AgentWorker, RuntimeSession
from sqlalchemy import update
from tests.platform import test_session_workspace

pytest_plugins = ["tests.platform.conftest"]
runtime = test_session_workspace.runtime


async def prepare(runtime):
    runtime.task.status = "RUNNING"
    runtime.task.last_message_id = "u1"
    runtime.task.turn_started_at = datetime.now(UTC) - timedelta(minutes=5)
    binding = await runtime.session.get(RuntimeSession, "ses_current")
    binding.status = "RUNNING"
    runtime.native.sessions["ses_current"] = [
        {"info": {"id": "u1", "role": "user"}, "parts": []},
        {"info": {"id": "a1", "role": "assistant"}, "parts": []},
    ]
    await runtime.session.commit()
    return binding


async def test_old_idle_plus_new_busy_messages_is_interrupted(runtime):
    binding = await prepare(runtime)
    original = runtime.native.messages

    async def become_busy(directory, sid):
        runtime.native.state = "busy"
        return await original(directory, sid)

    runtime.native.messages = become_busy
    await runtime.service.refresh("T1", client=runtime.native)
    assert runtime.native.state == "busy"
    assert binding.status == "INTERRUPTED"
    assert binding.terminal_reason == "incomplete"
    assert runtime.task.status == "WAITING"
    print("stale_idle: native=busy, session=INTERRUPTED/incomplete, task=WAITING")


async def test_incomplete_terminal_hides_later_success(runtime):
    binding = await prepare(runtime)
    await runtime.service.refresh("T1", client=runtime.native)
    assert binding.status == "INTERRUPTED"
    runtime.native.sessions["ses_current"][-1] = {
        "info": {
            "id": "a1",
            "role": "assistant",
            "finish": "stop",
            "time": {"completed": datetime.now(UTC).timestamp()},
        },
        "parts": [{"type": "text", "text": "done"}],
    }

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("历史快路径之外不应调用此探针")

    runtime.native.messages = must_not_be_called
    await runtime.service.refresh("T1", client=runtime.native)
    assert binding.status == "INTERRUPTED"
    assert binding.terminal_reason == "incomplete"
    print(
        "sticky_terminal: native transcript=completed, session=INTERRUPTED, history not reread"
    )


async def test_timeout_abort_false_still_marks_interrupted(runtime):
    binding = await prepare(runtime)
    runtime.task.turn_started_at = datetime.now(UTC) - timedelta(days=1)
    runtime.native.state = "busy"
    await runtime.session.commit()

    async def ineffective_abort(*args):
        return False

    runtime.native.abort = ineffective_abort
    await runtime.service.refresh("T1", client=runtime.native)
    assert runtime.native.state == "busy"
    assert binding.status == "INTERRUPTED"
    assert binding.terminal_reason == "timeout"
    print("timeout: abort=False, native=busy, session=INTERRUPTED/timeout")


async def test_heartbeat_overwrites_concurrent_pause(runtime):
    await prepare(runtime)
    employee = await runtime.session.get(AgentWorker, "E1")
    employee.status = "OFFLINE"
    await runtime.session.commit()

    async def pause_during_probe():
        async with runtime.factory() as other:
            await other.execute(
                update(AgentWorker)
                .where(AgentWorker.id == "E1")
                .values(status="PAUSED")
            )
            await other.commit()
        return {"healthy": True}

    runtime.native.health = pause_during_probe
    await runtime.service.refresh_employee_heartbeats()
    await runtime.session.refresh(employee)
    assert employee.status == "WORKING"
    print(
        "heartbeat: another transaction committed PAUSED, stale probe overwrote it with WORKING"
    )

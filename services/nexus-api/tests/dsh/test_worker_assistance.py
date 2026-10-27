"""DSH 求助经数字员工回传，使用真实 TaskTools/RuntimeService 与替身 Worker。"""

import pytest

from app.models import Task
from app.task_tools import TaskTools, nexus_employee_id, nexus_session_id
from tests.dsh.test_runtime_integration import runtime  # noqa: F401, F811


@pytest.fixture
async def assistance(runtime, monkeypatch):  # noqa: F811
    db, service, native = runtime
    task = await db.get(Task, "t1")
    task.responsible_employee_id = "owner"
    task.source_session_id = "chat1"
    await db.commit()
    n = native["b1"]
    n.permissions, n.decisions, n.trace_reads = [], [], []
    original_snapshot = n.snapshot

    async def snapshot(**kwargs):
        return {**await original_snapshot(**kwargs), "permissions": list(n.permissions)}

    async def reply_permission(**kwargs):
        n.decisions.append(kwargs)
        n.permissions = []
        n.state = "running"
        return {"accepted": True}

    async def trace(*, cursor, limit):
        n.trace_reads.append(cursor)
        return {"events": [{
            "seq": cursor, "type": "assistant/message", "data": {"message": {
                "id": f"m{cursor}", "content": [{"type": "text", "text": "请确认目标环境"}],
            }},
        }], "next_cursor": 40 if cursor == 0 else None}

    monkeypatch.setattr(n, "snapshot", snapshot)
    monkeypatch.setattr(n, "reply_permission", reply_permission, raising=False)
    monkeypatch.setattr(n, "read_trace_page", trace)
    tokens = [(nexus_employee_id, nexus_employee_id.set("owner")),
              (nexus_session_id, nexus_session_id.set("chat1"))]
    try:
        yield db, service, native, TaskTools(db)
    finally:
        for variable, token in tokens:
            variable.reset(token)


async def test_employee_reads_business_question_and_answers_same_dsh_session(assistance):
    db, service, native, tools = assistance
    await service.refresh("t1")
    first = await tools.assistance("t1")
    second = await tools.assistance("t1", cursor=first["next_cursor"])
    assert first["can_input"] and first["output"][0]["text"] == "请确认目标环境"
    assert second["next_cursor"] is None
    assert native["b1"].trace_reads == [0, 40]
    answer = await tools.input("t1", first["session_id"], first["submission_id"], "部署测试环境")
    assert answer["ok"] and answer["status"] == "RUNNING"
    assert len(native["b1"].inputs) == 1 and native["b2"].inputs == []
    assert native["b1"].inputs[0]["text"] == "部署测试环境"
    assert (await db.get(Task, "t1")).current_session_id == "b1"
    duplicate = await tools.input("t1", first["session_id"], first["submission_id"], "部署测试环境")
    assert duplicate["ok"] is False and len(native["b1"].inputs) == 1


@pytest.mark.parametrize("option,ok", [("yes", True), ("no", True), ("forever", False), ("other", False)])
async def test_employee_handles_native_acp_permission_without_reading_active_trace(assistance, option, ok):
    db, service, native, tools = assistance
    n = native["b1"]
    n.state = "running"
    n.permissions = [{
        "id": "p1", "native_session_id": n.binding.native_session_id,
        "options": [{"optionId": "yes", "kind": "allow_once"},
                    {"optionId": "no", "kind": "reject_once"},
                    {"optionId": "forever", "kind": "allow_always"}],
        "tool_call": {"title": "运行已授权测试"},
    }]
    await service.refresh("t1")
    request = await tools.assistance("t1")
    assert request["requests"][0]["kind"] == "permission"
    assert not request["can_input"] and n.trace_reads == []
    result = await tools.reply("t1", "b1", "p1", "permission", decision=option)
    assert result["ok"] is ok
    assert len(n.decisions) == int(ok)
    assert (await db.get(Task, "t2")).status == "RUNNING"


@pytest.mark.parametrize("mutation", ["foreign-native", "expired", "old-binding", "old-turn", "closed"])
async def test_dsh_stale_answers_are_refused(assistance, mutation):
    db, _, native, tools = assistance
    n = native["b1"]
    n.permissions = [{"id": "p1", "native_session_id": n.binding.native_session_id,
                      "options": [{"optionId": "yes", "kind": "allow_once"}]}]
    task = await db.get(Task, "t1")
    if mutation == "foreign-native":
        n.permissions[0]["native_session_id"] = native["b2"].binding.native_session_id
    elif mutation == "expired":
        n.permissions = []
    elif mutation == "old-binding":
        task.current_session_id = "b2"
    elif mutation == "old-turn":
        task.last_message_id = "another-turn"
    else:
        task.status = "CANCELED"
    await db.commit()
    if mutation == "old-turn":
        result = await tools.input("t1", "b1", "initial", "旧答案")
    else:
        result = await tools.reply("t1", "b1", "p1", "permission", decision="yes")
    assert result["ok"] is False and n.decisions == [] and n.inputs == []


async def test_same_waiting_state_new_permission_wakes_once(assistance):
    from sqlalchemy import select

    from app.models import TaskWakeOutbox

    db, service, native, _ = assistance
    n = native["b1"]
    n.permissions = [{"id": "p1"}]
    await service.refresh("t1")
    rows = list((await db.scalars(select(TaskWakeOutbox))).all())
    count = len(rows)
    await service.refresh("t1")
    assert len(list((await db.scalars(select(TaskWakeOutbox))).all())) == count
    n.permissions = [{"id": "p2"}]
    await service.refresh("t1")
    rows = list((await db.scalars(select(TaskWakeOutbox))).all())
    assert len(rows) == count + 1
    assert len([x for x in rows if x.status == "pending"]) == 1

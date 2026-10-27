"""数字员工到原生 Worker 的求助闭环；Runtime 使用替身，投影与归属校验是真实代码。"""

import json

import pytest
from sqlalchemy import select

from app.models import Event, Task
from app.task_mcp import build_mcp_server
from app.task_tools import TaskTools, nexus_employee_id, nexus_session_id
from tests.platform.test_session_workspace import runtime  # noqa: F401, F811


@pytest.fixture
async def assistance(runtime, monkeypatch):  # noqa: F811
    from app.runtime import lifecycle

    monkeypatch.setattr("app.runtime.assistance.connect", lifecycle.connect)
    runtime.task.status = "WAITING"
    runtime.task.responsible_employee_id = "owner"
    runtime.task.source_session_id = "chat-owner"
    runtime.native.pending = [{
        "id": "q1", "sessionID": "ses_current",
        "questions": [{"question": "发布到哪个环境？", "options": [{"label": "测试环境"}]}],
    }]
    await runtime.session.commit()
    runtime.replies = []
    original = runtime.native.request

    async def request(method, path, **kwargs):
        if method == "POST" and path.endswith("/reply"):
            runtime.replies.append((path, kwargs))
            if path.startswith("/question/"):
                runtime.native.pending = [x for x in runtime.native.pending if x["id"] != path.split("/")[2]]
            else:
                runtime.native.pending_permissions = []
            runtime.native.state = "busy"
            return True
        return await original(method, path, **kwargs)

    monkeypatch.setattr(runtime.native, "request", request)
    # NativeServer 的 permissions 默认不返回 pending_permissions，补上真实原生查询行为。
    async def permissions(_directory):
        return runtime.native.pending_permissions

    monkeypatch.setattr(runtime.native, "permissions", permissions)
    tokens = [(nexus_employee_id, nexus_employee_id.set("owner")),
              (nexus_session_id, nexus_session_id.set("chat-owner"))]
    try:
        yield runtime, TaskTools(runtime.session)
    finally:
        for variable, token in tokens:
            variable.reset(token)


def test_mcp_exposes_employee_assistance_tools():
    names = {tool.name for tool in build_mcp_server()._tool_manager.list_tools()}
    assert names >= {"nexus_task_assistance", "nexus_task_reply", "nexus_task_input"}


async def test_opencode_employee_reads_paginated_native_results_without_user_prompt(assistance):
    r, tools = assistance
    r.native.sessions["ses_current"] = [{
        "info": {"id": "msg_000", "role": "user"},
        "parts": [{"type": "text", "text": "private runtime instructions"}],
    }] + [{
        "info": {"id": f"msg_{i:03}", "role": "assistant"},
        "parts": [{"type": "tool", "tool": "bash", "state": {
            "status": "completed", "output": f"verified delivery {i}", "metadata": {"exit": 0},
        }}],
    } for i in range(1, 42)]
    first = await tools.assistance("T1")
    assert first["ok"] and first["next_cursor"] == 40
    assert len(first["output"]) == 40
    assert first["output"][0]["exit_code"] == 0
    assert "private runtime instructions" not in json.dumps(first)
    last = await tools.assistance("T1", cursor=first["next_cursor"])
    assert last["next_cursor"] is None
    assert [event["excerpt"] for event in last["output"]] == ["verified delivery 41"]


async def test_employee_answers_current_worker_without_human_workspace(assistance):
    r, tools = assistance
    r.native.pending.append({"id": "other", "sessionID": "ses_foreign", "questions": []})
    current = await tools.assistance("T1")
    assert current["ok"] is True
    assert [x["id"] for x in current["requests"]] == ["q1"]
    result = await tools.reply(
        "T1", current["session_id"], "q1", "question", answers=[["测试环境"]],
    )
    assert result == {"ok": True, "accepted": True, "task_id": "T1", "refresh_required": False}
    assert r.task.status == "RUNNING"  # 原生 busy 投影，不能自动验收。
    assert r.replies[0][1]["body"] == {"answers": [["测试环境"]]}
    assert r.replies[0][1]["session_id"] == "ses_current"
    assert r.replies[0][1]["task_id"] == "T1"
    event = await r.session.scalar(select(Event).where(Event.event_type == "WORKER_ASSISTANCE_REPLIED"))
    assert event.detail["responsible_employee_id"] == "owner"
    assert "测试环境" not in json.dumps(event.detail, ensure_ascii=False)
    duplicate = await tools.reply("T1", "ses_current", "q1", "question", answers=[["测试环境"]])
    assert duplicate["ok"] is False and len(r.replies) == 1


async def test_multi_question_reply_keeps_individual_answers(assistance):
    r, tools = assistance
    r.native.pending[0]["questions"].append({"question": "需要哪些检查？", "multiple": True})
    answers = [["测试环境"], ["类型检查", "集成测试"]]
    assert (await tools.reply("T1", "ses_current", "q1", "question", answers=answers))["ok"]
    assert r.replies[0][1]["body"]["answers"] == answers


@pytest.mark.parametrize("answers", [None, [], [[]], [[""]], [["a"], ["b"]]])
async def test_incomplete_answers_are_not_sent(assistance, answers):
    r, tools = assistance
    result = await tools.reply("T1", "ses_current", "q1", "question", answers=answers)
    assert result["ok"] is False and r.replies == []


@pytest.mark.parametrize("decision,ok", [("once", True), ("reject", True), ("always", False)])
async def test_only_one_time_permission_decisions(assistance, decision, ok):
    r, tools = assistance
    r.native.pending_permissions = [{"id": "p1", "sessionID": "ses_current", "permission": "bash"}]
    current = await tools.assistance("T1")
    assert {x["kind"] for x in current["requests"]} == {"question", "permission"}
    result = await tools.reply("T1", "ses_current", "p1", "permission", decision=decision)
    assert result["ok"] is ok
    assert len(r.replies) == int(ok)


@pytest.mark.parametrize("field,value", [
    ("responsible_employee_id", "another"), ("source_session_id", "another-chat"),
    ("current_session_id", "new-session"), ("status", "SUCCEEDED"), ("status", "CANCELED"),
    ("status", "FAILED"), ("pending_message_id", "inflight"),
    ("suspension", {"cancel_token": {"id": "cancel"}}),
])
async def test_stale_or_unauthorized_reply_never_writes(assistance, field, value):
    r, tools = assistance
    setattr(r.task, field, value)
    await r.session.commit()
    result = await tools.reply("T1", "ses_current", "q1", "question", answers=[["yes"]])
    assert result["ok"] is False and r.replies == []


async def test_same_employee_multiple_tasks_cannot_answer_foreign_request(assistance):
    r, tools = assistance
    r.native.pending.append({"id": "q-other", "sessionID": "ses_other", "questions": []})
    r.session.add(Task(
        id="T2", title="另一个任务", task_type="ISSUE", status="WAITING",
        responsible_employee_id="owner", source_session_id="chat-owner", current_session_id="ses_other",
    ))
    await r.session.commit()
    result = await tools.reply("T1", "ses_current", "q-other", "question", answers=[["yes"]])
    assert result["ok"] is False and r.replies == []


async def test_source_session_is_required_even_for_same_employee(assistance):
    r, tools = assistance
    token = nexus_session_id.set("")
    try:
        assert (await tools.assistance("T1"))["ok"] is False
    finally:
        nexus_session_id.reset(token)
    assert r.replies == []


async def test_read_rechecks_binding_after_runtime_io(assistance, monkeypatch):
    r, tools = assistance

    async def changed(_directory):
        r.task.current_session_id = "new-session"
        await r.session.commit()
        return r.native.pending

    monkeypatch.setattr(r.native, "questions", changed)
    result = await tools.assistance("T1")
    assert result["ok"] is False and "已切换" in result["error"]


async def test_accepted_reply_is_not_reported_failed_when_refresh_fails(assistance, monkeypatch):
    r, tools = assistance

    async def unavailable(*_args, **_kwargs):
        raise ConnectionError("Worker offline")

    monkeypatch.setattr("app.runtime.lifecycle.RuntimeService.refresh", unavailable)
    result = await tools.reply("T1", "ses_current", "q1", "question", answers=[["测试环境"]])
    assert result["ok"] and result["accepted"] and result["refresh_required"]
    assert len(r.replies) == 1


async def test_worker_failure_does_not_claim_answered_or_leak_secrets(assistance, monkeypatch):
    r, tools = assistance

    async def unavailable(*_args, **_kwargs):
        raise ConnectionError("Bearer private-test-secret")

    monkeypatch.setattr(r.native, "request", unavailable)
    result = await tools.reply("T1", "ses_current", "q1", "question", answers=[["测试环境"]])
    assert result["ok"] is False
    assert "private-test-secret" not in json.dumps(result)
    assert not result.get("accepted")

"""跨阶段 MCP 续答复用原 Task / Session，员工保留整体完成后的关单能力。"""

import pytest
from sqlalchemy import func, select

from app.models import RuntimeSession, Task
from app.task_mcp import build_mcp_server
from tests.platform.test_session_workspace import _complete_current_turn, runtime  # noqa: F401
from tests.platform.test_worker_assistance import assistance  # noqa: F401


@pytest.mark.parametrize("policy", ["AUTO", "MANUAL_CONFIRM"])
@pytest.mark.parametrize("finish", ["stop", "end_turn"])
async def test_review_then_commit_reuses_task_and_session(assistance, policy, finish):  # noqa: F811
    r, tools = assistance
    r.native.pending = []
    r.task.completion_policy = policy
    await r.session.commit()
    await r.service.send("T1", {"parts": [{"type": "text", "text": "修改后等待检视再 Commit"}]})
    await _complete_current_turn(r)
    r.native.sessions["ses_current"][-1]["info"]["finish"] = finish
    await r.service.refresh("T1")
    assert r.task.status == "WAITING" and r.task.confirmed_at is None
    current = await tools.assistance("T1")
    assert current["ok"] and current["can_input"] and not current["requests"]
    brief = await tools.get("T1")
    assert brief["session_status"] == "SUCCEEDED"
    assert brief["waiting_reason"] == "EXECUTION_RESULT_REVIEW"
    assert brief["current_session_id"] == "ses_current"
    result = await tools.input("T1", current["session_id"], current["submission_id"], "检视通过，继续 Commit")
    assert result["ok"] and result["status"] == "RUNNING"
    assert r.task.current_session_id == "ses_current" and len(r.native.sent) == 2
    assert r.native.sent[-1][0] == "ses_current"
    assert r.native.sent[-1][1]["parts"][0]["text"] == "检视通过，继续 Commit"
    duplicate = await tools.input("T1", current["session_id"], current["submission_id"], "继续 Commit")
    assert not duplicate["ok"] and len(r.native.sent) == 2
    await _complete_current_turn(r)
    assert r.task.status == "WAITING"
    confirmed = await tools.confirm("T1")
    assert confirmed["ok"] and confirmed["status"] == "SUCCEEDED"
    assert await r.session.scalar(select(func.count()).select_from(Task)) == 1
    assert await r.session.scalar(select(func.count()).select_from(RuntimeSession)) == 1


async def test_resume_keeps_original_session(assistance):  # noqa: F811
    r, tools = assistance
    result = await tools.resume("T1")
    assert result["ok"] and r.task.current_session_id == "ses_current"
    assert not r.native.sent and not r.native.created_options
    assert await r.session.scalar(select(func.count()).select_from(RuntimeSession)) == 1


@pytest.mark.parametrize("blocked", ["busy", "question", "permission", "child_question"])
async def test_input_cannot_bypass_active_turn_or_hitl(assistance, blocked):  # noqa: F811
    r, tools = assistance
    r.native.pending = []
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    current = await tools.assistance("T1")
    if blocked == "busy":
        r.native.state = "busy"
    elif blocked == "permission":
        r.native.pending_permissions = [{"id": "p1", "sessionID": "ses_current"}]
    elif blocked == "child_question":
        r.native.children_by_session["ses_current"] = [{"id": "child"}]
        r.native.pending = [{"id": "q1", "sessionID": "child"}]
    else:
        r.native.pending = [{"id": "q1", "sessionID": "ses_current"}]
    assert not (await tools.assistance("T1"))["can_input"]
    result = await tools.input("T1", current["session_id"], current["submission_id"], "continue")
    assert not result["ok"] and len(r.native.sent) == 1


@pytest.mark.parametrize("changed", ["anchor", "source", "session", "cancel"])
async def test_input_revalidates_after_native_io(assistance, monkeypatch, changed):  # noqa: F811
    r, tools = assistance
    r.native.pending = []
    await r.service.send("T1", {"parts": [{"type": "text", "text": "go"}]})
    await _complete_current_turn(r)
    current = await tools.assistance("T1")
    original = r.native.status

    async def status(directory, sid):
        value = await original(directory, sid)
        if changed == "anchor":
            r.task.last_message_id = "msg_newer000"
        elif changed == "source":
            r.task.source_session_id = "another-chat"
        elif changed == "session":
            r.task.current_session_id = "another-session"
        else:
            r.task.status = "CANCELED"
        await r.session.commit()
        return value

    monkeypatch.setattr(r.native, "status", status)
    result = await tools.input("T1", current["session_id"], current["submission_id"], "continue")
    assert not result["ok"] and len(r.native.sent) == 1


def test_confirm_remains_available():
    names = {tool.name for tool in build_mcp_server()._tool_manager.list_tools()}
    assert "nexus_task_confirm" in names

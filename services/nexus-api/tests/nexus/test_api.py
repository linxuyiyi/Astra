"""Nexus API 骨架测试。不启动 AgentScope，也不生成模拟回复。"""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text as sql_text

from nexus_api.agentscope import AgentScopeClient, AgentScopeError
from nexus_api.main import (
    assistant_messages,
    assistant_reply_messages_after,
    create_app,
    data_dir,
    interleave_messages,
    model_input_blocks,
    observe_stream_event,
    package_dir,
    repo_root,
    session_transcript,
    stream_cursor,
)
from tests.nexus.clients import TestClient

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_AGENTSCOPE_BASE_URL", "")
    application = create_app()
    with TestClient(application) as test_client:
        yield test_client


def employee_body(**overrides):
    payload = {
        "name": "星河",
        "domain": "测试自动化工程师",
        "description": "负责自动化脚本",
        "instructions": "能直接回答的问题直接回答；需要同事时联系对方。",
        "avatar": "robot-v1-0",
        "enabled": True,
        "knowledge": [],
        "skills": [],
        "tools": [],
        "model_route_id": "route-test",
    }
    payload.update(overrides)
    return payload


def test_health_and_runtime_report_pinned_version(client):
    assert client.get("/api/health").json()["product"] == "nexus"
    assert client.get("/api/health").json()["version"] == "1.4.9"
    assert client.get("/openapi.json").json()["info"]["version"] == "1.4.9"
    runtime = client.get("/api/v1/runtime").json()
    assert runtime["agentscope_version"] == "2.0.10"
    assert runtime["configured"] is False
    assert runtime["reachable"] is False


def test_skill_catalog_is_empty_when_service_is_down(client):
    payload = client.get("/api/v1/skills").json()
    assert payload == {"available": False, "items": [{"id": "task-management", "name": "任务管理"}]}


def test_employee_crud_rejects_duplicates_and_model_is_absent(client):
    created = client.post("/api/v1/employees", json=employee_body())
    assert created.status_code == 201
    employee = created.json()["employee"]
    assert "model" not in employee
    assert created.json()["agentscope"]["ok"] is False

    duplicate = client.post("/api/v1/employees", json=employee_body(name="星河", domain="另一领域"))
    assert duplicate.status_code == 409
    assert "名称" in duplicate.json()["detail"]

    same_domain = client.post("/api/v1/employees", json=employee_body(name="初禾", domain="测试自动化工程师"))
    assert same_domain.status_code == 409

    listed = client.get("/api/v1/employees", params={"q": "自动化"}).json()["items"]
    assert [item["name"] for item in listed] == ["星河"]

    disabled = client.patch(f"/api/v1/employees/{employee['id']}", json=employee_body(enabled=False))
    assert disabled.status_code == 200
    assert disabled.json()["employee"]["enabled"] is False


def test_delete_removes_channel_membership(client):
    first = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
    second = client.post(
        "/api/v1/employees",
        json=employee_body(name="知微", domain="防线运营人员"),
    ).json()["employee"]
    channel = client.post(
        "/api/v1/channels",
        json={"name": "版本交付协同", "description": "跨岗位讨论", "member_ids": [first["id"], second["id"]]},
    )
    assert channel.status_code == 201
    assert client.delete(f"/api/v1/employees/{first['id']}").status_code == 200
    remaining = client.get("/api/v1/channels").json()["items"][0]["members"]
    assert [item["employee_id"] for item in remaining if item["member_type"] == "employee"] == [second["id"]]


def test_private_chat_keeps_threads_and_does_not_invent_replies(client):
    employee = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
    headers = {"X-Owner-Id": "browser-a"}
    first = client.post(
        f"/api/v1/employees/{employee['id']}/conversations",
        headers=headers,
        json={"name": "新对话"},
    )
    second = client.post(
        f"/api/v1/employees/{employee['id']}/conversations",
        headers=headers,
        json={"name": "新对话"},
    )
    assert first.json()["id"] == second.json()["id"]
    sent = client.post(
        f"/api/v1/conversations/{first.json()['id']}/messages",
        headers=headers,
        json={"text": "你负责哪些工作？", "attachment_ids": []},
    )
    assert sent.status_code == 503
    assert "AgentScope" in sent.json()["detail"]
    stored = client.get(f"/api/v1/conversations/{first.json()['id']}", headers=headers).json()["messages"]
    assert stored == []
    forged = client.get(
        f"/api/v1/conversations/{first.json()['id']}?uid=browser-a",
        headers={"X-Owner-Id": "browser-b"},
    )
    assert forged.status_code == 404


def test_disabled_employee_cannot_receive_private_message(client):
    employee = client.post("/api/v1/employees", json=employee_body(enabled=False)).json()["employee"]
    headers = {"X-Owner-Id": "browser-a"}
    conversation = client.post(
        f"/api/v1/employees/{employee['id']}/conversations",
        headers=headers,
        json={},
    ).json()
    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        headers=headers,
        json={"text": "你好"},
    )
    assert response.status_code == 409


def test_session_transcript_keeps_speaker_and_tool_cards():
    rows = session_transcript(
        [
            {
                "id": "u",
                "role": "user",
                "name": "张三",
                "metadata": {"verified_uid": "u1"},
                "content": [{"type": "text", "text": "hi"}],
            },
            {
                "id": "a",
                "role": "assistant",
                "finished_at": "2026-10-30T00:00:00Z",
                "usage": {"input_tokens": 12, "output_tokens": 4},
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_call", "id": "c1", "name": "read", "input": '{"path":"a"}'},
                    {"type": "tool_result", "id": "c1", "name": "read", "output": "file", "state": "success"},
                ],
            },
        ],
        "emp",
    )
    assert rows[0]["speaker"] == "张三"
    assert rows[1]["text"] == "ok"
    assert rows[1]["finished_at"] == "2026-10-30T00:00:00+00:00"
    assert rows[1]["usage"] == {"input_tokens": 12, "output_tokens": 4}
    assert rows[1]["tools"] == [
        {
            "id": "c1",
            "name": "read",
            "input": '{"path":"a"}',
            "output": "file",
            "state": "success",
        }
    ]


def test_me_ignores_query_uid(client):
    from tests.nexus.clients import AnonymousClient

    assert client.get("/api/v1/me?uid=from-query").json() == {"uid": "u1"}
    with AnonymousClient(client.app) as anonymous:
        assert anonymous.get("/api/v1/me?uid=u1", headers={"X-Verified-UID": "u1"}).status_code == 401


def test_me_records_temporary_browser_uid_cookie(client):
    found = client.get("/api/v1/me", headers={"Cookie": "nexus_uid=somebody"})
    assert found.status_code == 200
    assert found.json() == {"uid": "somebody"}


def test_parallel_private_creates_share_one_row(client):
    employee = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
    barrier = threading.Barrier(8)
    results: list[tuple[int, str]] = []
    guard = threading.Lock()

    def once() -> None:
        barrier.wait()
        response = client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers={"X-Owner-Id": "same-owner"},
            json={"name": "新对话"},
        )
        with guard:
            results.append((response.status_code, response.json()["id"]))

    threads = [threading.Thread(target=once) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {code for code, _ident in results} <= {200, 201}
    assert len({ident for _code, ident in results}) == 1
    assert sum(code == 201 for code, _ident in results) == 1


def test_claim_private_session_keeps_the_first_writer(client):
    from nexus_api.db import connect
    from nexus_api.main import claim_private_session, db_path

    employee = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
    created = client.post(
        f"/api/v1/employees/{employee['id']}/conversations",
        headers={"X-Owner-Id": "owner"},
        json={"name": "私聊"},
    )
    conversation_id = created.json()["id"]
    connection = connect(db_path())
    try:
        first = claim_private_session(connection, conversation_id, "session-a", "ws-1")
        second = claim_private_session(connection, conversation_id, "session-b", "ws-2")
    finally:
        connection.close()
    assert first == "session-a"
    assert second == "session-a"
    stored = client.get(
        f"/api/v1/conversations/{conversation_id}",
        headers={"X-Owner-Id": "owner"},
    ).json()
    assert stored["agentscope_session_id"] == "session-a"
    assert stored["event_cursor"] == ""


def test_session_transcript_renders_display_text():
    rows = session_transcript(
        [
            {
                "id": "u",
                "role": "user",
                "name": "张三",
                "metadata": {
                    "display_text": "看日志",
                    "attachments": [{"id": "att", "name": "run.log"}],
                },
                "content": [{"type": "text", "text": "看日志\n\n文件正文 LOG-TOKEN-88"}],
            }
        ],
        "emp",
    )
    assert rows[0]["text"] == "看日志"
    assert "LOG-TOKEN-88" not in rows[0]["text"]
    assert rows[0]["attachments"][0]["name"] == "run.log"


def test_model_input_includes_text_and_does_not_invent_binary_analysis():
    text = model_input_blocks("看日志", [("run.log", "text/plain", b"LOG-TOKEN-88")])
    assert any("LOG-TOKEN-88" in block.get("text", "") for block in text)
    binary = model_input_blocks("", [("数据.bin", "application/octet-stream", b"\x00\x01secret")])
    assert "不要描述它的内容" in binary[0]["text"]
    assert "secret" not in binary[0]["text"]
    assert "已解析" not in binary[0]["text"]


def test_owners_keep_separate_conversations_across_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_AGENTSCOPE_BASE_URL", "")
    owner_a = {"X-Owner-Id": "anon-a"}
    owner_b = {"X-Owner-Id": "anon-b"}
    application = create_app()
    with TestClient(application) as test_client:
        employee = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        first = test_client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers=owner_a,
            json={"name": "新对话"},
        ).json()
        second = test_client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers=owner_a,
            json={"name": "新对话"},
        ).json()
        assert first["id"] == second["id"]
        sent = test_client.post(
            f"/api/v1/conversations/{first['id']}/messages",
            headers=owner_a,
            json={"text": "只属于 a"},
        )
        assert sent.status_code == 503
        hidden = test_client.get(f"/api/v1/employees/{employee['id']}/conversations", headers=owner_b)
        assert hidden.json()["items"] == []
        missing = test_client.get(f"/api/v1/conversations/{first['id']}", headers=owner_b)
        assert missing.status_code == 404
        query_only = test_client.get(f"/api/v1/conversations/{first['id']}?uid=anon-a")
        assert query_only.status_code == 404
    restarted = create_app()
    with TestClient(restarted) as again:
        listed = again.get(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers=owner_a,
        ).json()["items"]
        assert [item["id"] for item in listed] == [first["id"]]
        restored = again.get(f"/api/v1/conversations/{first['id']}", headers=owner_a).json()
        assert restored["messages"] == []


def test_new_skills_are_rejected_when_catalog_is_unavailable(client):
    response = client.post("/api/v1/employees", json=employee_body(skills=["not-a-real-skill"]))
    assert response.status_code == 422
    assert "技能" in response.json()["detail"]


def test_builtin_task_skill_can_be_saved_when_catalog_is_down(client):
    response = client.post("/api/v1/employees", json=employee_body(skills=["task-management"]))
    assert response.status_code == 201
    assert response.json()["employee"]["skills"] == ["task-management"]


def test_wake_target_uses_mailbox_and_rejects_foreign_session(client):
    from nexus_api.db import connect
    from nexus_api.main import db_path

    employee = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
    other = client.post(
        "/api/v1/employees",
        json=employee_body(name="知微", domain="防线运营人员"),
    ).json()["employee"]
    connection = connect(db_path())
    connection.execute(
        sql_text(
            """
            UPDATE t_nexus_employees SET agentscope_agent_id = :p0, mailbox_session_id =
            :p1 WHERE id = :p2
            """
        ),
        {"p0": "agt_1", "p1": "ses_mail", "p2": employee["id"]},
    )
    connection.execute(
        sql_text(
            """
            UPDATE t_nexus_employees SET agentscope_agent_id = :p0, mailbox_session_id =
            :p1 WHERE id = :p2
            """
        ),
        {"p0": "agt_2", "p1": "ses_other", "p2": other["id"]},
    )
    connection.commit()
    connection.close()

    mailbox = client.get("/api/v1/internal/wake-target", params={"employee_id": employee["id"]})
    assert mailbox.status_code == 200
    assert mailbox.json() == {"agent_id": "agt_1", "session_id": "ses_mail"}
    foreign = client.get(
        "/api/v1/internal/wake-target",
        params={"employee_id": employee["id"], "session_id": "ses_other"},
    )
    assert foreign.status_code == 404


@pytest.mark.parametrize("unavailable", [False, True])
def test_private_task_wake_refreshes_assistance_before_delivery(client, monkeypatch, unavailable):
    from nexus_api.db import connect

    employee = client.post(
        "/api/v1/employees", json=employee_body(skills=["task-management"]),
    ).json()["employee"]
    conversation = client.post(f"/api/v1/employees/{employee['id']}/conversations",
                               json={"name": "求助来源"}).json()
    with connect() as db:
        db.execute(sql_text("UPDATE t_nexus_employees SET agentscope_agent_id='agent-task' WHERE id=:i"),
                   {"i": employee["id"]})
        db.execute(
            sql_text("UPDATE t_nexus_conversations SET agentscope_session_id='session-task' WHERE id=:i"),
            {"i": conversation["id"]},
        )
        db.commit()
    calls = []

    async def attach(_client, staff, sid, **kwargs):
        calls.append((staff["id"], sid, kwargs))
        if unavailable:
            raise AgentScopeError("unavailable")

    monkeypatch.setattr("nexus_api.main.attach_task_capability", attach)
    response = client.get("/api/v1/internal/wake-target",
                          params={"employee_id": employee["id"], "session_id": "session-task"})
    assert response.status_code == (503 if unavailable else 200), response.text
    assert calls == [
        (employee["id"], "session-task", {"conversation_id": conversation["id"], "strict": True}),
    ]


def test_new_knowledge_and_tools_are_rejected_when_catalog_is_unavailable(client):
    assert client.get("/api/v1/knowledge").json() == {"available": False, "items": []}
    assert client.get("/api/v1/tools").json() == {"available": False, "items": []}
    knowledge = client.post("/api/v1/employees", json=employee_body(knowledge=["自动化框架 Wiki"]))
    assert knowledge.status_code == 422
    assert "知识库" in knowledge.json()["detail"]
    tools = client.post("/api/v1/employees", json=employee_body(tools=["query_task"]))
    assert tools.status_code == 422
    assert "工具" in tools.json()["detail"]
    known_name = client.post("/api/v1/employees", json=employee_body(tools=["contact_employee"]))
    assert known_name.status_code == 422


def test_connected_catalog_accepts_only_returned_resources(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    application = create_app()
    chat_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"version": "2.0.10"})
        if request.url.path == "/agent/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.method == "PATCH" and request.url.path.startswith("/agent/"):
            return httpx.Response(200, json={})
        if request.method == "DELETE" and request.url.path.startswith("/agent/"):
            return httpx.Response(204)
        if request.url.path == "/sessions/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"session-{body['name']}"})
        if request.url.path == "/skill":
            return httpx.Response(200, json=[{"id": "failure-analysis", "name": "失败分析"}])
        if request.url.path == "/knowledge_bases/":
            return httpx.Response(
                200,
                json={
                    "knowledge_bases": [{"id": "kb-1", "name": "自动化框架"}],
                    "total": 1,
                    "page": 1,
                    "page_size": 128,
                },
            )
        if request.url.path == "/chat/":
            body = json.loads(request.content.decode())
            chat_calls.append(body["agent_id"])
            return httpx.Response(200, json={"status": "started", "session_id": "session-chat"})
        return httpx.Response(404, json={"detail": "missing"})

    with TestClient(application) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        tools = test_client.get("/api/v1/tools").json()
        assert tools["available"] is True
        tool_ids = {item["id"] for item in tools["items"]}
        assert {"Bash", "contact_employee", "contact_human"} <= tool_ids
        assert "query_task" not in tool_ids
        assert "TeamSay" not in tool_ids
        knowledge = test_client.get("/api/v1/knowledge").json()
        assert knowledge == {"available": True, "items": [{"id": "kb-1", "name": "自动化框架"}]}

        rejected_knowledge = test_client.post(
            "/api/v1/employees",
            json=employee_body(knowledge=["自动化框架 Wiki"]),
        )
        assert rejected_knowledge.status_code == 422
        rejected_tool = test_client.post("/api/v1/employees", json=employee_body(tools=["query_task"]))
        assert rejected_tool.status_code == 422

        created = test_client.post(
            "/api/v1/employees",
            json=employee_body(
                knowledge=["kb-1"],
                skills=["failure-analysis"],
                tools=["contact_employee"],
            ),
        )
        assert created.status_code == 201, created.text
        employee = created.json()["employee"]
        assert employee["knowledge"] == ["kb-1"]
        assert employee["skills"] == ["failure-analysis"]
        assert employee["tools"] == ["contact_employee"]
        assert employee["agentscope_agent_id"] == "agent-星河"
        assert created.json()["agentscope"]["ok"] is True

        headers = {"X-Owner-Id": "browser-a"}
        conversation = test_client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers=headers,
            json={},
        ).json()
        disabled = test_client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(
                enabled=False,
                knowledge=["kb-1"],
                skills=["failure-analysis"],
                tools=["contact_employee"],
            ),
        )
        assert disabled.json()["employee"]["enabled"] is False
        refused = test_client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=headers,
            json={"text": "禁用后不要运行"},
        )
        assert refused.status_code == 409
        assert chat_calls == []
        enabled = test_client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(
                enabled=True,
                knowledge=["kb-1"],
                skills=["failure-analysis"],
                tools=["contact_employee"],
            ),
        )
        assert enabled.json()["employee"]["enabled"] is True
        accepted = test_client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=headers,
            json={"text": "重新启用后再运行"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["agentscope"]["accepted"] is True
        assert chat_calls == ["agent-星河"]


def test_live_client_posts_chat_without_writing_a_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_AGENTSCOPE_BASE_URL", "")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/agent/":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/sessions/":
            return httpx.Response(201, json={"session_id": f"session-{len(seen)}"})
        if request.url.path == "/chat/":
            return httpx.Response(200, json={"status": "started", "session_id": "session-chat"})
        if request.url.path == "/skill":
            return httpx.Response(200, json=[{"id": "failure-analysis", "name": "失败分析"}])
        if request.url.path == "/workspace/mcp":
            if request.method == "POST":
                return httpx.Response(201, json={"ok": True})
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "missing"})

    application = create_app()
    with TestClient(application) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        created = test_client.post("/api/v1/employees", json=employee_body(skills=["failure-analysis"]))
        assert created.status_code == 201
        employee = created.json()["employee"]
        assert employee["agentscope_agent_id"] == "agent-1"
        assert employee["mailbox_session_id"]
        assert created.json()["agentscope"]["ok"] is True

        headers = {"X-Owner-Id": "browser-a"}
        conversation = test_client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers=headers,
            json={"name": "新对话"},
        ).json()
        first = test_client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=headers,
            json={"text": "看一下失败日志"},
        )
        second = test_client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=headers,
            json={"text": "再看一次"},
        )
        assert first.json()["agentscope"]["accepted"] is True
        assert second.json()["agentscope"]["accepted"] is True
        assert first.json()["agentscope"]["session_id"] == second.json()["agentscope"]["session_id"]
        chat_calls = [request for request in seen if request.url.path == "/chat/"]
        assert len(chat_calls) == 2
        payload = json.loads(chat_calls[0].content.decode())
        assert payload["agent_id"] == "agent-1"
        assert payload["input"]["role"] == "user"
        assert payload["input"]["name"] == "browser-a"
        assert payload["input"]["metadata"]["verified_uid"] == "browser-a"
        assert payload["input"]["content"] == [{"type": "text", "text": "看一下失败日志"}]
        assert chat_calls[0].headers["x-user-id"] == "nexus"
        session_calls = [request for request in seen if request.url.path == "/sessions/"]
        # 邮箱一次，私聊一次。第二条消息复用私聊 Session。
        assert len(session_calls) == 2
        stored = test_client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers).json()
        assert stored["messages"] == []
        assert stored["history_error"]
        schedule_mcp_calls = [
            request
            for request in seen
            if request.url.path == "/workspace/mcp"
            and request.method == "POST"
            and b"nexus-schedule" in request.content
        ]
        assert len(schedule_mcp_calls) == 2
        schedule_payload = json.loads(schedule_mcp_calls[0].content.decode())
        assert schedule_payload["name"] == "nexus-schedule"
        headers = schedule_payload["mcp_config"]["headers"]
        assert headers["X-Nexus-Employee-Id"] == employee["id"]
        assert headers["X-Nexus-Session-Id"]
        assert "Authorization" not in headers


def test_peer_hint_returns_limit_text_without_delivering():
    import asyncio
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "nexus_agent_service",
        ROOT / "services/agent-service/agent_service.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen: dict[str, str] = {}

    class Response:
        status_code = 409

        def json(self):
            return {"detail": "已经把同样的话交给对方，不要再联系。"}

        def raise_for_status(self):
            raise AssertionError("不应抛出")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, params, headers=None):
            seen.update(params)
            return Response()

    class Http:
        def AsyncClient(self, timeout):
            assert timeout == 20
            return Client()

    async def deliver(*_args, **_kwargs):
        raise AssertionError("达到上限时不应投递")

    result = asyncio.run(
        module.deliver_peer_hint(
            Http(),
            deliver,
            None,
            None,
            "http://nexus",
            "nexus",
            "agent-ops",
            "session-ops",
            "自动化",
            "自动化失败了",
        )
    )
    payload = json.loads(result)
    assert payload == {"ok": False, "peer": "自动化", "error": "已经把同样的话交给对方，不要再联系。"}
    assert seen["message"] == "自动化失败了"
    assert seen["name"] == "自动化"


def test_task_hint_uses_nexus_task_source():
    import asyncio
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "nexus_agent_service_task_hint",
        ROOT / "services/agent-service/agent_service.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen: dict[str, object] = {}

    class Block:
        def __init__(self, hint: str, source: str) -> None:
            self.payload = {"type": "hint", "hint": hint, "source": source}

        def model_dump(self, mode: str = "json") -> dict[str, object]:
            assert mode == "json"
            return self.payload

    async def deliver(_bus, **kwargs):
        seen.update(kwargs)

    asyncio.run(
        module.deliver_task_hint(
            deliver,
            Block,
            None,
            "nexus",
            "agt_1",
            "ses_1",
            "请先查询任务",
        )
    )
    assert seen["session_id"] == "ses_1"
    assert seen["agent_id"] == "agt_1"
    assert seen["payload"]["source"] == "nexus-task"


def test_peer_resolver_returns_mailbox_without_a_scripted_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    application = create_app()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.url.path == "/sessions/":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"session-{body['agent_id']}-{body['name']}"})
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    with TestClient(application) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        star = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        zhi = test_client.post(
            "/api/v1/employees",
            json=employee_body(name="知微", domain="防线运营人员"),
        ).json()["employee"]
        resolved = test_client.get(
            "/api/v1/peers/resolve",
            params={"session_id": star["mailbox_session_id"], "name": "知微", "message": "先从邮箱联系"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["session_id"] == zhi["mailbox_session_id"]
        assert resolved.json()["sender_name"] == "星河"
        headers = {"X-Owner-Id": "browser-a"}
        conversation = test_client.post(
            f"/api/v1/employees/{star['id']}/conversations",
            headers=headers,
            json={"name": "新对话"},
        ).json()
        test_client.app.state.agentscope.trigger_chat = _accept
        # 直接写入 session 绑定，模拟一次已被接受的私聊。
        from nexus_api.db import connect
        from nexus_api.main import db_path

        connection = connect(db_path())
        connection.execute(
            sql_text("""
                UPDATE t_nexus_conversations SET agentscope_session_id = :p0 WHERE id = :p1
                """),
            {"p0": "session-private", "p1": conversation["id"]},
        )
        connection.commit()
        connection.close()
        resolved = test_client.get(
            "/api/v1/peers/resolve",
            params={"session_id": "session-private", "name": "知微"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["agent_id"] == zhi["agentscope_agent_id"]
        assert resolved.json()["session_id"] == zhi["mailbox_session_id"]
        assert resolved.json()["sender_name"] == "星河"
        disabled = test_client.patch(
            f"/api/v1/employees/{zhi['id']}",
            json=employee_body(name="知微", domain="防线运营人员", enabled=False),
        )
        assert disabled.status_code == 200
        assert disabled.json()["employee"]["enabled"] is False
        blocked = test_client.get(
            "/api/v1/peers/resolve",
            params={"session_id": "session-private", "name": "知微"},
        )
        assert blocked.status_code == 404
        assert "禁用" in blocked.json()["detail"]
        headers = {"X-Owner-Id": "browser-a"}
        peer_conversation = test_client.post(
            f"/api/v1/employees/{zhi['id']}/conversations",
            headers=headers,
            json={"name": "新对话"},
        ).json()
        refused = test_client.post(
            f"/api/v1/conversations/{peer_conversation['id']}/messages",
            headers=headers,
            json={"text": "禁用后不应开始运行"},
        )
        assert refused.status_code == 409


async def _accept(*_args, **_kwargs):
    return "session-private"


def test_stream_cursor_is_the_redis_checkpoint_not_an_event_id():
    assert stream_cursor("1710000000000-0") == "1710000000000-0"
    assert stream_cursor("7b674dd0149e4f19b8d5034b8437963b") is None
    assert stream_cursor(None) is None


def test_empty_route_does_not_attach_a_vendor_config():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"session_id": "session-model"})

    gateway = {
        "type": "openai_credential",
        "credential_id": "cred-gw",
        "model": "opencode/deepseek-v4.1-flash-free",
        "parameters": {},
    }
    import asyncio

    client = AgentScopeClient(
        "http://agentscope.test",
        "nexus",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
    )
    with pytest.raises(AgentScopeError) as raised:
        asyncio.run(client.create_session("agent-1", "邮箱"))
    assert raised.value.status_code == 422
    assert "请先选择模型路由" in raised.value.detail
    assert seen == []

    assert asyncio.run(client.create_session("agent-1", "邮箱", chat_model_config=gateway)) == "session-model"
    body = json.loads(seen[0].content.decode())
    assert body["chat_model_config"] == gateway
    assert "deepseek-chat" not in seen[0].content.decode()


def test_private_attachments_are_forwarded_and_session_replies_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    seen: list[dict] = []
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/" and request.method == "POST":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/sessions/" and request.method == "POST":
            return httpx.Response(201, json={"session_id": "session-chat"})
        if request.url.path == "/chat/":
            seen.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"status": "started", "session_id": "session-chat"})
        if request.url.path == "/sessions/session-chat/messages":
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "id": "u1",
                            "role": "user",
                            "name": "anon-a",
                            "metadata": {"verified_uid": "anon-a"},
                            "content": [{"type": "text", "text": "看日志"}],
                        },
                        {
                            "id": "a1",
                            "role": "assistant",
                            "content": [{"type": "text", "text": "模型自己的回复"}],
                        },
                    ],
                    "has_more": False,
                    "is_running": False,
                },
            )
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "missing"})

    application = create_app()
    with TestClient(application) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        employee = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        headers = {"X-Owner-Id": "anon-a"}
        conversation = test_client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers=headers,
            json={"name": "新对话"},
        ).json()
        log_file = test_client.post(
            "/api/v1/attachments",
            headers=headers,
            files={"file": ("run.log", b"LOG-TOKEN-88", "text/plain")},
        )
        image = test_client.post(
            "/api/v1/attachments",
            headers=headers,
            files={"file": ("shot.png", png, "image/png")},
        )
        assert log_file.status_code == 201
        assert image.status_code == 201
        denied = test_client.get(
            f"/api/v1/attachments/{log_file.json()['id']}?uid=anon-a",
            headers={"X-Owner-Id": "anon-b"},
        )
        assert denied.status_code == 404
        downloaded = test_client.get(f"/api/v1/attachments/{log_file.json()['id']}", headers=headers)
        assert downloaded.status_code == 200
        assert downloaded.content == b"LOG-TOKEN-88"
        sent = test_client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=headers,
            json={
                "text": "看日志",
                "attachment_ids": [log_file.json()["id"], image.json()["id"]],
            },
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["agentscope"]["accepted"] is True
        content = seen[0]["input"]["content"]
        logged = [
            block
            for block in content
            if block.get("type") == "text" and "LOG-TOKEN-88" in block.get("text", "")
        ]
        assert logged
        image_block = next(block for block in content if block.get("type") == "data")
        assert image_block["source"]["media_type"] == "image/png"
        assert image_block["source"]["type"] == "base64"
        restored = test_client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers).json()
        assert [item["sender"] for item in restored["messages"]] == ["user", employee["id"]]
        assert restored["messages"][0]["speaker"] == "anon-a"
        assert restored["messages"][1]["text"] == "模型自己的回复"
        assert seen[0]["input"]["name"] == "anon-a"
        assert seen[0]["input"]["metadata"]["verified_uid"] == "anon-a"
        assert seen[0]["input"]["metadata"]["attachments"][0]["name"] == "run.log"
        assert "已解析" not in json.dumps(restored, ensure_ascii=False)


def test_peer_toolkit_drops_team_and_task_tools_but_keeps_contact():
    import asyncio
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "nexus_agent_service_tools",
        ROOT / "services/agent-service/agent_service.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Toolkit:
        def __init__(self) -> None:
            self.removed: list[str] = []

        async def remove_tool(self, names: list[str]) -> None:
            self.removed = list(names)

    async def original(**_kwargs):
        return Toolkit()

    toolkit = asyncio.run(module.without_team_and_task_tools(original)())
    assert "AgentCreate" in toolkit.removed
    assert "TeamCreate" not in toolkit.removed
    assert "TeamSay" not in toolkit.removed
    assert "AgentInvite" not in toolkit.removed
    assert "TaskGet" in toolkit.removed
    assert "ToolStop" in toolkit.removed
    assert "ScheduleCreate" in toolkit.removed
    assert "contact_employee" not in toolkit.removed
    assert "contact_human" not in toolkit.removed


def test_launcher_parses_and_does_not_import_agentscope():
    source = (ROOT / "services/agent-service/agent_service.py").read_text(encoding="utf-8")
    ast.parse(source)
    assert "import agentscope" not in source.split("def main")[0]
    assert "deliver_to_inbox" in source
    assert "async def contact_human" in source
    assert "IsolationPolicy.PER_SESSION" in source
    assert "without_team_and_task_tools" in source
    assert "TeamCreate(" not in source
    assert "TaskGet(" not in source
    version = (ROOT / "services/agent-service/agentscope.version").read_text(encoding="utf-8").strip()
    assert version == "agentscope==2.0.10"


def test_data_dir_does_not_follow_the_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)
    assert data_dir() == package_dir() / "data"
    monkeypatch.setenv("NEXUS_DATA_DIR", "services/nexus-api/data")
    assert data_dir() == (repo_root() / "services/nexus-api/data").resolve()
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path / "absolute"))
    assert data_dir() == tmp_path / "absolute"


def test_private_stream_skips_the_saved_cursor_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    sse = (
        'data: {"type":"REPLY_START","id":"s1","reply_id":"r1"}\n\n'
        'data: {"type":"TEXT_BLOCK_DELTA","id":"d1","delta":"在"}\n\n'
        'data: {"type":"REPLY_END","id":"e1","reply_id":"r1","finished_reason":"completed"}\n\n'
        'data: {"type":"TEXT_BLOCK_DELTA","id":"late","delta":"不应再出现"}\n\n'
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/agent/" and request.method == "POST":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/sessions/" and request.method == "POST":
            return httpx.Response(201, json={"session_id": "session-chat"})
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        if request.url.path == "/sessions/session-chat/messages":
            return httpx.Response(
                200,
                json={"messages": [], "event_cursor": "20-1", "has_more": False},
            )
        if request.url.path == "/sessions/session-chat/stream":
            return httpx.Response(200, content=sse.encode())
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "missing"})

    application = create_app()
    with TestClient(application) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        employee = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        headers = {"X-Owner-Id": "browser-a"}
        conversation = test_client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers=headers,
            json={"name": "新对话"},
        ).json()
        from nexus_api.db import connect

        connection = connect(tmp_path / "nexus.db")
        connection.execute(
            sql_text("""
                UPDATE t_nexus_conversations SET agentscope_session_id = :p0 WHERE id = :p1
                """),
            {"p0": "session-chat", "p1": conversation["id"]},
        )
        connection.commit()
        connection.close()
        with test_client.stream(
            "GET",
            f"/api/v1/conversations/{conversation['id']}/stream",
            headers=headers,
        ) as response:
            assert response.status_code == 200
            body = response.read().decode()
    assert "NEXUS_RUN_FINISHED" in body
    assert "不应再出现" not in body
    streamed = [item for item in seen if item.url.path == "/sessions/session-chat/stream"]
    assert streamed[0].url.params["after"] == "20-1"


def test_stream_closes_when_the_started_reply_ends():
    started = 0
    events = [
        {"type": "REPLY_END", "reply_id": "old"},
        {"type": "REPLY_START", "reply_id": "new"},
        {"type": "TEXT_BLOCK_DELTA", "delta": "在"},
        {"type": "REPLY_END", "reply_id": "new", "finished_reason": "completed"},
        {"type": "TEXT_BLOCK_DELTA", "delta": "不应再出现"},
    ]
    closed_at = None
    terminals = []
    for index, event in enumerate(events):
        started, terminal, close = observe_stream_event(started, event)
        if terminal:
            terminals.append(terminal)
        if close:
            closed_at = index
            break
    assert closed_at == 3
    assert terminals == [
        {
            "type": "NEXUS_RUN_FINISHED",
            "reply_id": "new",
            "finished_reason": "completed",
        }
    ]


def test_mailbox_hop_is_scoped_to_the_conversation_and_reply_is_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    patched: list[dict] = []
    chats: list[dict] = []
    delivery_ids = []
    peer_text = "阈值按账号计算"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chat/":
            chats.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "started", "session_id": "session-private"})
        if request.method == "PATCH" and request.url.path.startswith("/sessions/"):
            patched.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={})
        if request.url.path == "/agent/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.url.path == "/sessions/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"session-{body['agent_id']}"})
        if request.url.path == "/sessions/session-agent-知微/messages":
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "role": "assistant",
                            "finished_at": "2026-10-23T01:00:00Z",
                            "content": [
                                {"type": "hint", "id": delivery_ids[0], "hint": "方案要点"},
                                {"type": "text", "text": peer_text},
                            ],
                        },
                    ],
                    "has_more": False,
                },
            )
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [], "has_more": False})
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "missing"})

    application = create_app()
    with TestClient(application) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        star = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        zhi = test_client.post(
            "/api/v1/employees",
            json=employee_body(name="知微", domain="防线运营人员"),
        ).json()["employee"]
        wang = test_client.post(
            "/api/v1/employees",
            json=employee_body(name="王五", domain="测试工程师"),
        ).json()["employee"]
        assert patched
        assert patched[0]["permission_mode"] == "dont_ask"
        headers = {"X-Owner-Id": "browser-a"}
        first = test_client.post(
            f"/api/v1/employees/{star['id']}/conversations",
            headers=headers,
            json={"name": "第一条"},
        ).json()
        again_private = test_client.post(
            f"/api/v1/employees/{star['id']}/conversations",
            headers=headers,
            json={"name": "第二条"},
        ).json()
        assert again_private["id"] == first["id"]
        from nexus_api.db import connect, utcnow

        connection = connect(tmp_path / "nexus.db")
        connection.execute(
            sql_text("""
                UPDATE t_nexus_conversations SET agentscope_session_id = :p0 WHERE id = :p1
                """),
            {"p0": "session-private", "p1": first["id"]},
        )
        now = utcnow()
        connection.execute(
            sql_text(
                """
                INSERT INTO t_nexus_conversations (
                    id, employee_id, owner_id, kind, name, agentscope_session_id, created_at,
                updated_at
                ) VALUES ('conv_other', :p0, 'browser-a', 'note', '另一范围', 'session-other',
                :p1, :p2)
                """
            ),
            {"p0": star["id"], "p1": now, "p2": now},
        )
        connection.commit()
        connection.close()

        def resolve(session_id: str, name: str, message: str):
            result = test_client.get(
                "/api/v1/peers/resolve",
                params={"session_id": session_id, "name": name, "message": message},
            )
            if result.status_code == 200:
                delivery_ids.append(result.json()["delivery_id"])
            return result

        assert resolve("session-private", "知微", "方案要点").status_code == 200
        assert resolve("session-private", "知微", "再补充一点").status_code == 200
        blocked = resolve("session-private", "知微", "第三次不该成功")
        assert blocked.status_code == 409
        forwarded = resolve(zhi["mailbox_session_id"], "王五", "请看测试要点")
        assert forwarded.status_code == 200
        assert forwarded.json()["session_id"] == wang["mailbox_session_id"]
        assert forwarded.json()["sender_name"] == "知微"
        back = resolve(wang["mailbox_session_id"], "星河", "测试要点如下")
        assert back.status_code == 200
        assert back.json()["session_id"] == star["mailbox_session_id"]
        again = resolve(star["mailbox_session_id"], "知微", "邮箱里的第三次")
        assert again.status_code == 409
        other = resolve("session-other", "知微", "另一条对话还可以联系")
        assert other.status_code == 200

        stored = test_client.get(f"/api/v1/conversations/{first['id']}", headers=headers).json()
        peer_lines = [item for item in stored["messages"] if item["sender"] == zhi["id"]]
        assert [item["text"] for item in peer_lines] == ["阈值按账号计算"]
        assert (
            assistant_reply_messages_after(
                [{"role": "user", "content": [{"type": "text", "text": "方案要点"}]}],
                "方案要点",
            )
            == []
        )

        native = test_client.get(f"/api/v1/conversations/{first['id']}/messages", headers=headers).json()
        assert any(m["name"] == "知微" and "阈值按账号计算" in str(m["content"]) for m in native["messages"])
        sent = test_client.post(
            f"/api/v1/conversations/{first['id']}/messages", headers=headers, json={"text": "同事回复了吗？"}
        )
        assert sent.status_code == 200
        assert sent.json()["agentscope"]["accepted"] is True
        assert any(
            b.get("type") == "hint" and "阈值按账号计算" in b.get("hint", "")
            for m in chats[-1]["input"]
            for b in m["content"]
        )

        peer_text = '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="Bash">'
        malformed = test_client.get(f"/api/v1/conversations/{first['id']}/messages", headers=headers).json()
        failed = [m for m in malformed["messages"] if (m.get("metadata") or {}).get("peer_employee_id")]
        assert failed[0]["metadata"]["nexus_error"] == "NEXUS_UPSTREAM_TOOL_PARSE_ERROR"
        assert "DSML" not in str(failed[0]["content"])


def test_multiline_hint_inside_the_reply_is_returned_once():
    needle = "请李四联系王五。\n给出测试要点。"
    dumped = json.dumps({"hint": needle}, ensure_ascii=False)
    assert needle not in dumped
    replies = assistant_reply_messages_after(
        [
            {
                "role": "assistant",
                "finished_at": "2026-10-23T01:00:00Z",
                "content": [
                    {"type": "hint", "id": "delivery-one", "hint": needle},
                    {"type": "text", "text": "已转达王五。\n阈值按账号计算"},
                ],
            },
            {
                "role": "assistant",
                "finished_at": "2026-10-23T01:00:00Z",
                "content": [
                    {"type": "hint", "hint": "另一条唤醒"},
                    {"type": "text", "text": "下一跳的答复"},
                ],
            },
        ],
        "delivery-one",
    )
    assert len(replies) == 1
    assert replies[0]["content"][1]["text"] == "已转达王五。\n阈值按账号计算"


def test_channel_timestamps_sort_by_instant_not_local_text():
    normalized = assistant_messages(
        [
            {
                "id": "m1",
                "role": "assistant",
                "content": [{"type": "text", "text": "同事先说"}],
                "created_at": "2026-10-15T00:13:55.943",
            }
        ],
        "emp_1",
    )
    assert normalized[0]["created_at"].endswith("+00:00")
    ordered = interleave_messages(
        [
            {
                "id": "user",
                "sender": "user",
                "created_at": "2026-10-14T17:00:00+00:00",
                "text": "后说",
            }
        ],
        [
            {
                "id": "agent",
                "sender": "emp",
                "created_at": "2026-10-15T00:00:00+08:00",
                "text": "先说",
            }
        ],
    )
    assert [item["id"] for item in ordered] == ["agent", "user"]


def load_agent_service(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, ROOT / "services/agent-service/agent_service.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_chat_uses_dont_ask(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    patches: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH" and request.url.path.startswith("/sessions/"):
            patches.append((request.url.path, json.loads(request.content.decode())))
            return httpx.Response(200, json={})
        if request.url.path == "/agent/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.url.path == "/sessions/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"session-{body['name']}"})
        if request.url.path == "/chat/" and request.method == "POST":
            return httpx.Response(200, json={"status": "started"})
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "missing"})

    application = create_app()
    with TestClient(application) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        employee = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        headers = {"X-Owner-Id": "browser-a"}
        conversation = test_client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers=headers,
            json={"name": "私聊"},
        ).json()
        sent = test_client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            headers=headers,
            json={"text": "帮我看一下"},
        )
        assert sent.status_code == 200, sent.text

    private = [item for item in patches if item[0] == "/sessions/session-私聊"]
    assert private
    assert private[0][1]["permission_mode"] == "dont_ask"


def test_dont_ask_allows_platform_mcp_tools():
    import asyncio

    module = load_agent_service("nexus_agent_service_task_permission")

    class Behavior:
        ALLOW = "ALLOW"

    class Decision:
        def __init__(self, behavior, message) -> None:
            self.behavior = behavior
            self.message = message

    class Tool:
        def __init__(self, name: str) -> None:
            self.name = name

        async def check_permissions(self, *_args, **_kwargs):
            return "original"

    module.allow_nexus_task_mcp(Tool, Decision, Behavior)
    allowed = asyncio.run(Tool("mcp__nexus-task__nexus_task_get").check_permissions())
    scheduled = asyncio.run(Tool("mcp__nexus-schedule__schedule_create").check_permissions())
    other = asyncio.run(Tool("mcp__other__bash").check_permissions())
    assert allowed.behavior == Behavior.ALLOW
    assert scheduled.behavior == Behavior.ALLOW
    assert other == "original"


def test_task_hint_body_accepts_json_at_module_level():
    import __future__

    module = load_agent_service("nexus_agent_service_hint_model")
    tree = ast.parse((ROOT / "services/agent-service/agent_service.py").read_text(encoding="utf-8"))
    assert any(isinstance(node, ast.ClassDef) and node.name == "TaskHintBody" for node in tree.body)

    from fastapi import FastAPI

    probe = FastAPI()
    code = compile(
        "def register(app):\n"
        "    @app.post('/nexus/task-hint')\n"
        "    async def task_hint(body: TaskHintBody) -> dict:\n"
        "        return {'session_id': body.session_id, 'agent_id': body.agent_id}\n",
        "<task-hint-probe>",
        "exec",
        flags=__future__.annotations.compiler_flag,
    )
    namespace = dict(module.__dict__)
    exec(code, namespace)
    namespace["register"](probe)
    with TestClient(probe) as test_client:
        missing = test_client.post("/nexus/task-hint", json={})
        posted = test_client.post(
            "/nexus/task-hint",
            json={"session_id": "ses_1", "agent_id": "agt_1", "hint": "请查询"},
        )
    assert posted.status_code == 200, posted.text
    assert posted.json()["session_id"] == "ses_1"
    locations = [tuple(item["loc"]) for item in missing.json()["detail"]]
    assert ("query", "body") not in locations
    assert any(item[0] == "body" for item in locations)


def test_new_employee_attaches_task_capability_with_the_new_agent_id(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    attached: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.url.path == "/sessions/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"session-{body['agent_id']}"})
        if request.method == "PATCH" and request.url.path.startswith("/sessions/"):
            return httpx.Response(200, json={})
        if request.url.path in {"/workspace/skill/upload", "/workspace/mcp"} and request.method == "POST":
            attached.append((request.url.path, request.url.params.get("agent_id")))
            return httpx.Response(200, json={})
        if request.url.path == "/workspace/mcp" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "missing"})

    application = create_app()
    with TestClient(application) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        created = test_client.post("/api/v1/employees", json=employee_body(skills=["task-management"]))
        assert created.status_code == 201, created.text
        employee = created.json()["employee"]

    assert employee["agentscope_agent_id"] == "agent-星河"
    assert ("/workspace/skill/upload", "agent-星河") in attached
    assert ("/workspace/mcp", "agent-星河") not in attached


def test_disabled_employee_defers_wake(client):
    from nexus_api.db import connect
    from nexus_api.main import db_path

    employee = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
    connection = connect(db_path())
    connection.execute(
        sql_text(
            """
            UPDATE t_nexus_employees SET enabled = 0, agentscope_agent_id = :p0,
            mailbox_session_id = :p1 WHERE id = :p2
            """
        ),
        {"p0": "agt_1", "p1": "ses_mail", "p2": employee["id"]},
    )
    connection.commit()
    connection.close()

    deferred = client.get("/api/v1/internal/wake-target", params={"employee_id": employee["id"]})
    assert deferred.status_code == 409
    assert "禁用" in deferred.json()["detail"]


def test_task_skill_is_revoked_and_restored_on_existing_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (
                request.method,
                request.url.path,
                request.url.params.get("session_id"),
            )
        )
        if request.url.path == "/agent/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.method == "PATCH" and request.url.path.startswith("/agent/"):
            return httpx.Response(200, json={})
        if request.url.path == "/sessions/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"session-{body['agent_id']}"})
        if request.method == "PATCH" and request.url.path.startswith("/sessions/"):
            return httpx.Response(200, json={})
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/workspace/skill":
            return httpx.Response(200, json=[{"name": "任务管理", "dir": "task-management"}])
        if request.method == "GET" and request.url.path == "/workspace/mcp":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "nexus-task",
                        "is_healthy": True,
                        "tools": [{"name": "nexus_task_get"}],
                    }
                ],
            )
        if request.url.path in {"/workspace/skill/upload", "/workspace/mcp"} or request.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"detail": "missing"})

    application = create_app()
    with TestClient(application) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        created = test_client.post("/api/v1/employees", json=employee_body(skills=["task-management"]))
        assert created.status_code == 201, created.text
        employee = created.json()["employee"]
        from nexus_api.db import connect
        from nexus_api.main import db_path

        connection = connect(db_path())
        connection.execute(
            sql_text(
                """
                INSERT INTO t_nexus_conversations (
                    id, employee_id, owner_id, name, agentscope_session_id, created_at,
                updated_at
                ) VALUES ('conv_1', :p0, 'owner', '对话', 'ses_chat', 't', 't')
                """
            ),
            {"p0": employee["id"]},
        )
        connection.commit()
        connection.close()
        calls.clear()
        removed = test_client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=[]),
        )
        assert removed.status_code == 200, removed.text
        revoked = set(calls)
        assert ("DELETE", "/workspace/skill/任务管理", employee["mailbox_session_id"]) in revoked
        assert ("DELETE", "/workspace/mcp/nexus-task", employee["mailbox_session_id"]) in revoked
        assert ("DELETE", "/workspace/mcp/nexus-task", "ses_chat") in revoked
        calls.clear()
        restored = test_client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=["task-management"]),
        )
        assert restored.status_code == 200, restored.text
        mcp_calls = [item for item in calls if item[0] == "POST" and item[1] == "/workspace/mcp"]
        assert {item[2] for item in mcp_calls} == {"ses_chat"}


def test_task_mcp_discovery_error_is_visible_on_employee_capability(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_TASK_MCP_URL", "http://task.test/mcp")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/" and request.method == "POST":
            return httpx.Response(201, json={"agent_id": "agent-task"})
        if request.url.path == "/sessions/" and request.method == "POST":
            return httpx.Response(201, json={"session_id": "session-mail"})
        if request.method == "PATCH" and request.url.path.startswith("/sessions/"):
            return httpx.Response(200, json={})
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        if request.url.path == "/workspace/skill":
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json=[{"name": "任务管理", "dir": "task-management"}],
                )
            return httpx.Response(201, json={})
        if request.url.path == "/workspace/mcp":
            if request.method == "POST":
                return httpx.Response(201, json={})
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "nexus-task",
                        "is_healthy": False,
                        "error": "连接被拒绝",
                        "tools": [],
                    }
                ],
            )
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"status": "idle"})
        return httpx.Response(200, json={})

    application = create_app()
    with TestClient(application) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="http://agentscope.test",
            ),
        )
        created = test_client.post(
            "/api/v1/employees",
            json=employee_body(skills=["task-management"]),
        )
        assert created.status_code == 201, created.text
        employee = created.json()["employee"]
        from nexus_api.db import connect
        from nexus_api.main import db_path

        connection = connect(db_path())
        connection.execute(
            sql_text(
                """
                INSERT INTO t_nexus_conversations (
                    id, employee_id, owner_id, name, agentscope_session_id,
                    created_at, updated_at
                ) VALUES ('conv_task', :p0, 'owner', '任务对话', 'session-chat', 't', 't')
                """
            ),
            {"p0": employee["id"]},
        )
        connection.commit()
        connection.close()

        updated = test_client.patch(
            f"/api/v1/employees/{employee['id']}",
            json=employee_body(skills=["task-management"]),
        )
        assert updated.status_code == 200, updated.text
        payload = updated.json()
        assert payload["sync"]["status"] == "error"
        assert "任务 MCP 连接或工具发现失败" in payload["sync"]["detail"]
        capability = payload["employee"]["capability"]
        assert capability["runtime_status"] == "error"
        assert "任务 MCP 连接或工具发现失败" in capability["error"]
        assert capability["task"]["status"] == "error"


def test_employee_avatar_persists_and_metadata_edits_do_not_change_it(client):
    body = employee_body(avatar="botpixel-v1-headphones")
    response = client.post("/api/v1/employees", json=body)
    assert response.status_code == 201
    employee = response.json()["employee"]
    assert employee["avatar"] == "botpixel-v1-headphones"
    changed = {**body, "name": "新名字", "domain": "新职责"}
    changed.pop("avatar")
    updated = client.patch(f"/api/v1/employees/{employee['id']}", json=changed)
    assert updated.status_code == 200
    assert updated.json()["employee"]["avatar"] == "botpixel-v1-headphones"
    assert client.get("/api/v1/employees").json()["items"][0]["avatar"] == "botpixel-v1-headphones"


@pytest.mark.parametrize("value", [None, "🤖", "unknown", "https://example.com/avatar.svg", "<svg/>"])
def test_employee_avatar_rejects_unsupported_values_without_writing(client, value):
    response = client.post("/api/v1/employees", json=employee_body(avatar=value))
    assert response.status_code == 422
    assert client.get("/api/v1/employees").json()["items"] == []


def test_employee_avatar_default_and_failed_update_preserve_saved_identity(client):
    body = employee_body()
    body.pop("avatar")
    response = client.post("/api/v1/employees", json=body)
    assert response.status_code == 201
    employee = response.json()["employee"]
    assert employee["avatar"] == "robot-v1-0"
    rejected = client.patch(f"/api/v1/employees/{employee['id']}", json={**body, "avatar": "invalid"})
    assert rejected.status_code == 422
    assert client.get("/api/v1/employees").json()["items"][0]["avatar"] == "robot-v1-0"

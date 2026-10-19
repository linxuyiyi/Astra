"""聊天岛原生传输：原生历史、运行态与归属校验。"""

from __future__ import annotations

import sqlite3

import httpx
from fastapi.testclient import TestClient

from nexus_api.agentscope import AgentScopeClient
from nexus_api.main import create_app, db_path

OWNER = {"X-Owner-Id": "browser-native"}
OTHER = {"X-Owner-Id": "browser-other"}


def employee_body() -> dict:
    return {
        "name": "星河",
        "domain": "测试自动化工程师",
        "description": "负责自动化脚本",
        "instructions": "直接回答。",
        "avatar": "👩🏻‍💻",
        "enabled": True,
        "knowledge": [],
        "skills": [],
        "tools": [],
        "model_route_id": "route-test",
    }


def build_client(
    tmp_path,
    monkeypatch,
    message_response,
    stream_sse: bytes | None = None,
    calls: list[httpx.Request] | None = None,
):
    """建一个只有 AgentScope 上游是假的 app，员工与会话走真实 HTTP。"""

    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_AGENTSCOPE_BASE_URL", "")

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        path = request.url.path
        if path == "/agent/":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if path == "/sessions/":
            return httpx.Response(201, json={"session_id": "session-mailbox"})
        if path.endswith("/messages"):
            status, payload = message_response
            return httpx.Response(status, json=payload)
        if path.endswith("/stream") and stream_sse is not None:
            return httpx.Response(200, content=stream_sse)
        if path.endswith("/interrupt") and request.method == "POST":
            return httpx.Response(202, json={"status": "accepted"})
        return httpx.Response(404, json={"detail": "missing"})

    application = create_app()
    test_client = TestClient(application)
    test_client.__enter__()
    test_client.app.state.agentscope = AgentScopeClient(
        "http://agentscope.test",
        "nexus",
        httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://agentscope.test",
        ),
    )
    return test_client


def create_conversation(test_client) -> str:
    employee = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
    conversation = test_client.post(
        f"/api/v1/employees/{employee['id']}/conversations",
        headers=OWNER,
        json={"name": "新对话"},
    ).json()
    return conversation["id"]


def bind_session(conversation_id: str, session_id: str) -> None:
    with sqlite3.connect(db_path()) as connection:
        connection.execute(
            "UPDATE conversations SET agentscope_session_id = ? WHERE id = ?",
            (session_id, conversation_id),
        )


def test_native_history_keeps_content_blocks_and_running_state(tmp_path, monkeypatch):
    message = {
        "id": "msg-1",
        "name": "星河",
        "role": "assistant",
        "created_at": "2026-10-20T01:00:00Z",
        "content": [
            {"type": "thinking", "thinking": "先看日志", "id": "b1", "created_at": ""},
            {
                "type": "tool_call",
                "name": "Grep",
                "id": "call-1",
                "input": '{"pattern":"error"}',
                "state": "finished",
                "created_at": "",
            },
        ],
    }
    client = build_client(
        tmp_path,
        monkeypatch,
        (200, {"messages": [message], "event_cursor": "7-0", "is_running": True, "has_more": False}),
    )
    try:
        conversation_id = create_conversation(client)
        bind_session(conversation_id, "session-1")

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=OWNER,
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["event_cursor"] == "7-0"
        assert payload["is_running"] is True
        assert payload["messages"][0]["content"][0]["type"] == "thinking"
        assert payload["messages"][0]["content"][1]["name"] == "Grep"
    finally:
        client.__exit__(None, None, None)


def test_native_history_rejects_other_owner(tmp_path, monkeypatch):
    client = build_client(
        tmp_path,
        monkeypatch,
        (200, {"messages": [], "event_cursor": "", "is_running": False, "has_more": False}),
    )
    try:
        conversation_id = create_conversation(client)
        bind_session(conversation_id, "session-1")

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=OTHER,
        )

        assert response.status_code == 404
        assert "messages" not in response.json()
    finally:
        client.__exit__(None, None, None)


def test_native_history_without_session_is_empty(tmp_path, monkeypatch):
    client = build_client(
        tmp_path,
        monkeypatch,
        (200, {"messages": [], "event_cursor": "", "is_running": False, "has_more": False}),
    )
    try:
        conversation_id = create_conversation(client)

        payload = client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=OWNER,
        ).json()

        assert payload == {"messages": [], "event_cursor": "", "is_running": False}
    finally:
        client.__exit__(None, None, None)


def test_native_history_surfaces_upstream_error(tmp_path, monkeypatch):
    client = build_client(tmp_path, monkeypatch, (500, {"detail": "会话不存在"}))
    try:
        conversation_id = create_conversation(client)
        bind_session(conversation_id, "session-1")

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=OWNER,
        )

        assert response.status_code == 502
        assert "会话不存在" in response.json()["detail"]
    finally:
        client.__exit__(None, None, None)


def test_flat_history_stays_unchanged(tmp_path, monkeypatch):
    """旧页面的扁平响应不能被原生接口顺带改掉。"""

    remote = {
        "id": "msg-1",
        "name": "星河",
        "role": "assistant",
        "created_at": "2026-10-20T01:00:00Z",
        "content": [{"type": "text", "text": "收到", "id": "b1", "created_at": ""}],
    }
    client = build_client(
        tmp_path,
        monkeypatch,
        (200, {"messages": [remote], "event_cursor": "3-0", "is_running": False, "has_more": False}),
    )
    try:
        conversation_id = create_conversation(client)
        bind_session(conversation_id, "session-1")

        flat = client.get(f"/api/v1/conversations/{conversation_id}", headers=OWNER).json()

        assert flat["messages"][0]["text"] == "收到"
        assert "content" not in flat["messages"][0]
        assert flat["event_cursor"] == "3-0"
    finally:
        client.__exit__(None, None, None)


def test_raw_stream_keeps_connection_and_skips_private_events(tmp_path, monkeypatch):
    """原生流：两轮回复同一条连接，且不混入 NEXUS 私有收尾事件。"""

    sse = (
        'data: {"type":"REPLY_START","id":"s1","reply_id":"r1"}\n\n'
        'data: {"type":"REPLY_END","id":"e1","reply_id":"r1","finished_reason":"completed"}\n\n'
        'data: {"type":"REPLY_START","id":"s2","reply_id":"r2"}\n\n'
        'data: {"type":"REPLY_END","id":"e2","reply_id":"r2","finished_reason":"completed"}\n\n'
    )
    client = build_client(
        tmp_path,
        monkeypatch,
        (200, {"messages": [], "event_cursor": "7-0", "is_running": False, "has_more": False}),
        stream_sse=sse.encode(),
    )
    try:
        conversation_id = create_conversation(client)
        bind_session(conversation_id, "session-1")

        with client.stream(
            "GET",
            f"/api/v1/conversations/{conversation_id}/stream?raw=1",
            headers=OWNER,
        ) as response:
            assert response.status_code == 200
            body = response.read().decode()

        assert "NEXUS_RUN_FINISHED" not in body
        assert body.count("REPLY_END") == 2
    finally:
        client.__exit__(None, None, None)


def test_default_stream_still_injects_terminal_event(tmp_path, monkeypatch):
    sse = (
        'data: {"type":"REPLY_START","id":"s1","reply_id":"r1"}\n\n'
        'data: {"type":"REPLY_END","id":"e1","reply_id":"r1","finished_reason":"completed"}\n\n'
        'data: {"type":"TEXT_BLOCK_DELTA","id":"late","delta":"不应再出现"}\n\n'
    )
    client = build_client(
        tmp_path,
        monkeypatch,
        (200, {"messages": [], "event_cursor": "7-0", "is_running": False, "has_more": False}),
        stream_sse=sse.encode(),
    )
    try:
        conversation_id = create_conversation(client)
        bind_session(conversation_id, "session-1")

        with client.stream(
            "GET",
            f"/api/v1/conversations/{conversation_id}/stream",
            headers=OWNER,
        ) as response:
            body = response.read().decode()

        assert "NEXUS_RUN_FINISHED" in body
        assert "不应再出现" not in body
    finally:
        client.__exit__(None, None, None)


def test_interrupt_reaches_the_bound_session(tmp_path, monkeypatch):
    calls: list[httpx.Request] = []
    client = build_client(
        tmp_path,
        monkeypatch,
        (200, {"messages": [], "event_cursor": "", "is_running": True, "has_more": False}),
        calls=calls,
    )
    try:
        conversation_id = create_conversation(client)
        bind_session(conversation_id, "session-1")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/interrupt",
            headers=OWNER,
        )

        assert response.status_code == 200
        assert response.json() == {"accepted": True}
        interrupts = [item for item in calls if item.url.path.endswith("/interrupt")]
        assert len(interrupts) == 1
        assert interrupts[0].url.params["agent_id"] == "agent-1"
    finally:
        client.__exit__(None, None, None)


def test_interrupt_rejects_other_owner_without_calling_upstream(tmp_path, monkeypatch):
    calls: list[httpx.Request] = []
    client = build_client(
        tmp_path,
        monkeypatch,
        (200, {"messages": [], "event_cursor": "", "is_running": True, "has_more": False}),
        calls=calls,
    )
    try:
        conversation_id = create_conversation(client)
        bind_session(conversation_id, "session-1")

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/interrupt",
            headers=OTHER,
        )

        assert response.status_code == 404
        assert not [item for item in calls if item.url.path.endswith("/interrupt")]
    finally:
        client.__exit__(None, None, None)


def test_interrupt_without_session_is_rejected(tmp_path, monkeypatch):
    client = build_client(
        tmp_path,
        monkeypatch,
        (200, {"messages": [], "event_cursor": "", "is_running": False, "has_more": False}),
    )
    try:
        conversation_id = create_conversation(client)

        response = client.post(
            f"/api/v1/conversations/{conversation_id}/interrupt",
            headers=OWNER,
        )

        assert response.status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_native_group_timeline_keeps_blocks_and_shows_human_once(tmp_path, monkeypatch):
    """群组原生时间线：成员 Session 保留完整块，人类消息只出现一次。"""

    remote = [
        {
            "id": "copy-1",
            "role": "user",
            "name": "browser-native",
            "created_at": "2026-10-20T08:00:00Z",
            "content": [{"type": "text", "text": "大家好", "id": "b0", "created_at": ""}],
            "metadata": {"message_id": "msg_h1", "display_text": "大家好"},
        },
        {
            "id": "reply-1",
            "role": "assistant",
            "name": "星河",
            "created_at": "2026-10-20T08:00:05Z",
            "content": [
                {"type": "thinking", "thinking": "先回应", "id": "b1", "created_at": ""},
                {"type": "text", "text": "收到", "id": "b2", "created_at": ""},
            ],
        },
    ]
    client = build_client(
        tmp_path,
        monkeypatch,
        (200, {"messages": remote, "event_cursor": "5-0", "is_running": False, "has_more": False}),
    )
    try:
        employee = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        channel = client.post(
            "/api/v1/channels",
            json={"name": "验收群", "description": "群组原生时间线", "member_ids": [employee["id"]]},
        ).json()
        with sqlite3.connect(db_path()) as connection:
            connection.execute(
                "UPDATE channel_members SET agentscope_session_id = ? WHERE channel_id = ?",
                ("session-1", channel["id"]),
            )
            connection.execute(
                """
                INSERT INTO messages (id, conversation_id, channel_id, sender, text,
                                      attachments_json, accepted, speaker_uid, created_at)
                VALUES (?, NULL, ?, 'user', '大家好', '[]', 1, 'browser-native', ?)
                """,
                ("msg_h1", channel["id"], "2026-10-20T08:00:00Z"),
            )

        native = client.get(f"/api/v1/channels/{channel['id']}/timeline/native").json()

        assert len(native["streams"]) == 2
        human, member = native["streams"]
        assert [item["id"] for item in human["messages"]] == ["msg_h1"]
        assert [item["id"] for item in member["messages"]] == ["reply-1"]
        assert [block["type"] for block in member["messages"][0]["content"]] == [
            "thinking",
            "text",
        ]
        assert member["label"] == "星河"

        legacy = client.get(f"/api/v1/channels/{channel['id']}/timeline").json()
        assert len(legacy["items"]) == 2
        assert [item["kind"] for item in legacy["items"]] == ["human", "reply"]
    finally:
        client.__exit__(None, None, None)

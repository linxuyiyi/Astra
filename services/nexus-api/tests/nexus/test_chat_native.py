"""聊天岛原生传输：原生历史、运行态与归属校验。"""

from __future__ import annotations

import sqlite3

import httpx

from nexus_api.agentscope import AgentScopeClient
from nexus_api.main import create_app, db_path
from tests.nexus.clients import TestClient

OWNER = {"X-Owner-Id": "browser-native"}
OTHER = {"X-Owner-Id": "browser-other"}


def employee_body() -> dict:
    return {
        "name": "星河",
        "domain": "测试自动化工程师",
        "description": "负责自动化脚本",
        "instructions": "直接回答。",
        "avatar": "robot-v1-0",
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
            status, payload = message_response(request) if callable(message_response) else message_response
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
            "UPDATE t_nexus_conversations SET agentscope_session_id = ? WHERE id = ?",
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
        assert payload["session_id"] == "session-1"
        assert payload["event_cursor"] == "7-0"
        assert payload["is_running"] is True
        assert payload["messages"] == [message]
    finally:
        client.__exit__(None, None, None)


def test_private_history_pages_bound_upstream_reads_and_merge_peer_replies(tmp_path, monkeypatch):
    def message(prefix, n):
        return {
            "id": f"{prefix}-{n}", "role": "assistant", "name": prefix,
            "created_at": f"2026-11-18T01:00:{n:02d}Z", "metadata": {},
            "content": [{"type": "text", "text": str(n)}],
        }

    rows = [message("native", n) for n in (1, 3, 6, 8)]
    rows[2]["metadata"] = {"nexus_peer_context": True}
    peers = [{"native": message("peer", n)} for n in (2, 4, 5, 7)]
    reads = []

    async def peers_of(*_):
        return peers

    def upstream(request):
        before = request.url.params.get("before")
        limit = int(request.url.params["limit"])
        reads.append((before, limit))
        end = next(i for i, row in enumerate(rows) if row["id"] == before) if before else len(rows)
        start = max(0, end - limit)
        return 200, {"messages": rows[start:end], "event_cursor": "" if before else "7-0",
                     "is_running": False, "has_more": start > 0}

    monkeypatch.setattr("nexus_api.main.peer_replies_for_conversation", peers_of)
    client = build_client(tmp_path, monkeypatch, upstream)
    try:
        cid = create_conversation(client)
        bind_session(cid, "session-1")
        params = {"limit": 2}
        ids = []
        for _ in range(10):
            response = client.get(f"/api/v1/conversations/{cid}/messages", headers=OWNER, params=params)
            assert response.status_code == 200
            page = response.json()
            assert len(page["messages"]) <= 2
            ids = [row["id"] for row in page["messages"]] + ids
            if not page["before"]:
                break
            params["before"] = page["before"]
        assert ids == ["native-1", "peer-2", "native-3", "peer-4", "peer-5", "peer-7", "native-8"]
        assert all(limit == 2 and (before is None or before.startswith("native-")) for before, limit in reads)
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

        assert payload["messages"] == [] and payload["before"] is None
        assert not payload["event_cursor"] and not payload["is_running"]
        assert not payload["session_id"] and payload["viewer_id"] == OWNER["X-Owner-Id"]
        assert payload["employee"]["name"] == "星河"
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


def test_stream_keeps_connection_and_skips_private_events(tmp_path, monkeypatch):
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
            f"/api/v1/conversations/{conversation_id}/stream",
            headers=OWNER,
        ) as response:
            assert response.status_code == 200
            body = response.read().decode()

        assert "NEXUS_RUN_FINISHED" not in body
        assert body.count("REPLY_END") == 2
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


def test_raw_stream_without_cursor_replays_from_the_start(tmp_path, monkeypatch):
    """空 cursor 的 raw 流不能把起点提升成最新 checkpoint，否则会整轮跳过回复。"""

    sse = 'data: {"type":"REPLY_START","id":"s1","reply_id":"r1"}\n\n'
    calls: list[httpx.Request] = []
    client = build_client(
        tmp_path,
        monkeypatch,
        (200, {"messages": [], "event_cursor": "9-0", "is_running": True, "has_more": False}),
        stream_sse=sse.encode(),
        calls=calls,
    )
    try:
        conversation_id = create_conversation(client)
        bind_session(conversation_id, "session-1")

        with client.stream(
            "GET",
            f"/api/v1/conversations/{conversation_id}/stream",
            headers=OWNER,
        ) as response:
            assert response.status_code == 200
            assert "REPLY_START" in response.read().decode()

        streamed = [item for item in calls if item.url.path.endswith("/stream")]
        assert streamed
        assert "after" not in streamed[0].url.params
        # 不再为“补一个最新游标”去读一次 messages。
        assert not [item for item in calls if item.url.path.endswith("/messages")]
    finally:
        client.__exit__(None, None, None)


def test_busy_chat_is_an_explicit_conflict(tmp_path, monkeypatch):
    client = build_client(tmp_path, monkeypatch, (200, {"messages": [], "is_running": True}))

    async def busy(*args, **kwargs):
        from nexus_api.agentscope import AgentScopeError

        raise AgentScopeError("Session already has an active chat run", status_code=409)

    monkeypatch.setattr(client.app.state.agentscope, "trigger_chat", busy)
    try:
        cid = create_conversation(client)
        bind_session(cid, "session-1")
        response = client.post(
            f"/api/v1/conversations/{cid}/messages", headers=OWNER,
            json={"client_id": "busy-message", "content": [{"type": "text", "text": "不要丢弃"}]},
        )
        assert response.status_code == 409
    finally:
        client.__exit__(None, None, None)

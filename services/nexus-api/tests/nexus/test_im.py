"""IM 往返。入站、运行、出站认领和 ACK 都走 HTTP。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from nexus_api.agentscope import AgentScopeClient
from nexus_api.db import connect, init_db
from nexus_api.im import ImError, accept_inbound, bind_bot, claim_outbox, contact_human
from nexus_api.main import capture_im_reply, create_app


def employee_body(**overrides):
    payload = {
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
    payload.update(overrides)
    return payload


def _reply_frame(reply_id: str) -> bytes:
    return (
        f'data: {{"type":"REPLY_START","id":"s","reply_id":"{reply_id}"}}\n\n'
        f'data: {{"type":"REPLY_END","id":"e","reply_id":"{reply_id}","finished_reason":"completed"}}\n\n'
    ).encode()


def test_im_http_round_trip_claims_and_acks_the_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    chats: list[dict] = []
    turns: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/" and request.method == "POST":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/sessions/" and request.method == "POST":
            body = json.loads(request.content.decode())
            session_id = "ses-private" if body.get("workspace_id") else f"session-{body['name']}"
            return httpx.Response(201, json={"session_id": session_id})
        if request.url.path == "/chat/":
            body = json.loads(request.content.decode())
            chats.append(body)
            turns.append({"id": f"r{len(turns) + 1}", "text": "已经收到"})
            return httpx.Response(200, json={"status": "started", "session_id": body["session_id"]})
        if request.url.path == "/sessions/ses-private/stream":
            current = turns[-1] if turns else {"id": "r1", "text": "已经收到"}
            return httpx.Response(200, content=_reply_frame(current["id"]))
        if request.url.path == "/sessions/ses-private/messages":
            messages = [
                {"id": "user-1", "role": "user", "content": [{"type": "text", "text": "你好"}]},
                *[
                    {
                        "id": item["id"],
                        "role": "assistant",
                        "content": [{"type": "text", "text": item["text"]}],
                    }
                    for item in turns
                ],
            ]
            return httpx.Response(
                200,
                json={
                    "messages": messages,
                    "event_cursor": f"c{len(turns)}",
                    "has_more": False,
                },
            )
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    with TestClient(create_app()) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        employee = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        saved = test_client.post(
            "/api/v1/im/bots",
            json={"employee_id": employee["id"], "provider": "mock", "bot_id": "bot-1"},
        )
        assert saved.status_code == 200
        accepted = test_client.post(
            "/api/v1/im/events",
            json={
                "provider": "mock",
                "event_id": "evt-1",
                "bot_id": "bot-1",
                "sender_uid": "u9",
                "text": "你好",
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["duplicate"] is False
        assert accepted.json()["session_id"] == "ses-private"
        opened = test_client.get(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers={"X-Verified-UID": "u9"},
        )
        assert opened.status_code == 200, opened.text
        assert opened.json()["items"][0]["agentscope_session_id"] == "ses-private"
        assert chats[0]["session_id"] == "ses-private"
        assert chats[0]["input"]["metadata"]["verified_uid"] == "u9"
        claimed = test_client.get("/api/v1/im/outbox")
        assert claimed.status_code == 200, claimed.text
        first = claimed.json()
        assert first["body"] == "已经收到"
        assert first["state"] == "inflight"
        assert first["conversation_key"] == "u9"
        assert first["lease_until"]
        held = test_client.get("/api/v1/im/outbox")
        assert held.status_code == 404
        queued = test_client.post(
            "/api/v1/im/contacts",
            json={
                "agent_id": "agent-1",
                "conversation_key": "u9",
                "session_id": "ses-private",
                "body": "工具发出的第二句",
            },
        )
        assert queued.status_code == 200, queued.text
        assert queued.json()["state"] == "queued"
        acked = test_client.post("/api/v1/im/acks", json={"delivery_id": first["delivery_id"]})
        assert acked.json()["state"] == "acked"
        nxt = test_client.get("/api/v1/im/outbox")
        assert nxt.status_code == 200, nxt.text
        assert nxt.json()["body"] == "工具发出的第二句"
        assert nxt.json()["delivery_id"] == queued.json()["delivery_id"]
        again = test_client.get("/api/v1/im/outbox")
        assert again.status_code == 404
        test_client.post("/api/v1/im/acks", json={"delivery_id": nxt.json()["delivery_id"]})
        empty = test_client.get("/api/v1/im/outbox")
        assert empty.status_code == 404
        duplicate = test_client.post(
            "/api/v1/im/events",
            json={
                "provider": "mock",
                "event_id": "evt-1",
                "bot_id": "bot-1",
                "sender_uid": "u9",
                "text": "你好",
            },
        )
        assert duplicate.json()["duplicate"] is True
        assert [item["input"]["content"][0]["text"] for item in chats].count("你好") == 1
        assert test_client.get("/api/v1/im/outbox").status_code == 404
        followed = test_client.post(
            "/api/v1/im/events",
            json={
                "provider": "mock",
                "event_id": "evt-2",
                "bot_id": "bot-1",
                "sender_uid": "u9",
                "text": "接着说",
                "conversation_key": "u9",
            },
        )
        assert followed.status_code == 200, followed.text
        assert followed.json()["session_id"] == "ses-private"
        assert chats[-1]["session_id"] == "ses-private"
        assert chats[-1]["input"]["content"][0]["text"] == "接着说"
        reply = test_client.get("/api/v1/im/outbox")
        assert reply.json()["body"] == "已经收到"
        assert reply.json()["delivery_id"] != first["delivery_id"]


def test_im_reply_ignores_a_replayed_previous_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    started = {"chat": False}
    streamed_after: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/" and request.method == "POST":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/sessions/" and request.method == "POST":
            return httpx.Response(201, json={"session_id": "ses-private"})
        if request.url.path == "/chat/":
            started["chat"] = True
            return httpx.Response(200, json={"status": "started", "session_id": "ses-private"})
        if request.url.path == "/sessions/ses-private/stream":
            streamed_after.append(request.url.params.get("after"))
            previous = _reply_frame("old")
            current = _reply_frame("new")
            return httpx.Response(200, content=previous + current)
        if request.url.path == "/sessions/ses-private/messages":
            messages = [
                {
                    "id": "old",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "上一轮"}],
                }
            ]
            if started["chat"] and request.url.params.get("limit") != "1":
                messages.append(
                    {
                        "id": "new",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "这一轮"}],
                    }
                )
            return httpx.Response(
                200,
                json={"messages": messages, "event_cursor": "c-old", "has_more": False},
            )
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    with TestClient(create_app()) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        employee = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        saved = test_client.post(
            "/api/v1/im/bots",
            json={"employee_id": employee["id"], "provider": "mock", "bot_id": "bot-1"},
        )
        assert saved.status_code == 200
        accepted = test_client.post(
            "/api/v1/im/events",
            json={
                "provider": "mock",
                "event_id": "evt-replay",
                "bot_id": "bot-1",
                "sender_uid": "u9",
                "text": "现在说",
            },
        )
        assert accepted.status_code == 200, accepted.text
        claimed = test_client.get("/api/v1/im/outbox")
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["body"] == "这一轮"
        assert streamed_after == ["c-old"]


def test_concurrent_im_event_accepts_once(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    chats: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/" and request.method == "POST":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/sessions/" and request.method == "POST":
            body = json.loads(request.content.decode())
            session_id = "ses-private" if body.get("workspace_id") else f"session-{body['name']}"
            return httpx.Response(201, json={"session_id": session_id})
        if request.url.path == "/chat/":
            body = json.loads(request.content.decode())
            text = body["input"]["content"][0]["text"]
            if text == "并发":
                chats.append(text)
                entered.set()
                await release.wait()
            return httpx.Response(200, json={"status": "started", "session_id": body["session_id"]})
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, content=b"")
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [], "has_more": False})
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    async def scenario():
        application = create_app()
        async with application.router.lifespan_context(application):
            application.state.agentscope = AgentScopeClient(
                "http://agentscope.test",
                "nexus",
                httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
            )
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://nexus.test") as client:
                employee = (await client.post("/api/v1/employees", json=employee_body())).json()["employee"]
                opened = await client.post(
                    f"/api/v1/employees/{employee['id']}/conversations",
                    headers={"X-Verified-UID": "u1"},
                    json={},
                )
                assert opened.status_code == 201, opened.text
                sent = await client.post(
                    f"/api/v1/conversations/{opened.json()['id']}/messages",
                    headers={"X-Verified-UID": "u1"},
                    json={"text": "先有私聊"},
                )
                assert sent.status_code == 200, sent.text
                saved = await client.post(
                    "/api/v1/im/bots",
                    json={"employee_id": employee["id"], "provider": "mock", "bot_id": "bot-1"},
                )
                assert saved.status_code == 200, saved.text
                payload = {
                    "provider": "mock",
                    "event_id": "race-1",
                    "bot_id": "bot-1",
                    "sender_uid": "u1",
                    "text": "并发",
                }

                async def once():
                    response = await client.post("/api/v1/im/events", json=payload)
                    return response.status_code, response.json()

                first = asyncio.create_task(once())
                second = None
                try:
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    second = asyncio.create_task(once())
                    loser = await asyncio.wait_for(second, timeout=3)
                finally:
                    release.set()
                assert second is not None
                winner = await asyncio.wait_for(first, timeout=3)
                return winner, loser

    winner, loser = asyncio.run(scenario())
    assert chats == ["并发"]
    assert winner[0] == 200
    assert winner[1]["duplicate"] is False
    assert loser[0] == 409
    assert loser[1]["detail"] == "这个事件正在处理"


def test_im_http_waits_for_agentscope_and_web_stays_available(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    chats: list[dict] = []
    failed = {"im": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/sessions/" and request.method == "POST":
            return httpx.Response(201, json={"session_id": "ses-private"})
        if request.url.path == "/chat/":
            body = json.loads(request.content.decode())
            text = body["input"]["content"][0]["text"]
            if text == "从 IM 来" and not failed["im"]:
                failed["im"] = True
                return httpx.Response(503, json={"detail": "还没接受"})
            chats.append(body)
            return httpx.Response(200, json={"status": "started", "session_id": body["session_id"]})
        return httpx.Response(200, json={})

    with TestClient(create_app()) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        employee = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        unbound = test_client.post(
            "/api/v1/im/events",
            json={
                "provider": "mock",
                "event_id": "e0",
                "bot_id": "missing",
                "sender_uid": "u1",
                "text": "hi",
            },
        )
        assert unbound.status_code == 409
        assert "IM 不可用" in unbound.json()["detail"]
        web = test_client.post(
            f"/api/v1/employees/{employee['id']}/conversations",
            headers={"X-Verified-UID": "u1"},
            json={},
        )
        assert web.status_code == 201
        opened = test_client.post(
            f"/api/v1/conversations/{web.json()['id']}/messages",
            headers={"X-Verified-UID": "u1"},
            json={"text": "网页上的私聊"},
        )
        assert opened.status_code == 200
        saved = test_client.post(
            "/api/v1/im/bots",
            json={"employee_id": employee["id"], "provider": "mock", "bot_id": "bot-1"},
        )
        assert saved.status_code == 200
        rejected = test_client.post(
            "/api/v1/im/events",
            json={
                "provider": "mock",
                "event_id": "e1",
                "bot_id": "bot-1",
                "sender_uid": "u1",
                "text": "从 IM 来",
            },
        )
        assert rejected.status_code == 503
        assert [item["input"]["content"][0]["text"] for item in chats].count("从 IM 来") == 0
        accepted = test_client.post(
            "/api/v1/im/events",
            json={
                "provider": "mock",
                "event_id": "e1",
                "bot_id": "bot-1",
                "sender_uid": "u1",
                "text": "从 IM 来",
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["session_id"] == "ses-private"
        assert accepted.json()["duplicate"] is False
        duplicate = test_client.post(
            "/api/v1/im/events",
            json={
                "provider": "mock",
                "event_id": "e1",
                "bot_id": "bot-1",
                "sender_uid": "u1",
                "text": "从 IM 来",
            },
        )
        assert duplicate.json()["duplicate"] is True
        assert [item["input"]["content"][0]["text"] for item in chats].count("从 IM 来") == 1
        refused = test_client.post(
            "/api/v1/im/groups",
            json={"employee_id": employee["id"], "external_key": "g1", "session_id": "ses-private"},
        )
        assert refused.status_code == 409
        grouped = test_client.post(
            "/api/v1/im/groups",
            json={"employee_id": employee["id"], "external_key": "g1", "session_id": "ses-group"},
        )
        assert grouped.status_code == 200
        group_event = test_client.post(
            "/api/v1/im/events",
            json={
                "provider": "mock",
                "event_id": "e2",
                "bot_id": "bot-1",
                "sender_uid": "u1",
                "text": "外部群",
                "external_key": "g1",
            },
        )
        assert group_event.json()["session_id"] == "ses-group"
        assert chats[-1]["session_id"] == "ses-group"


def test_outbox_reclaims_only_an_expired_lease(tmp_path):
    path = tmp_path / "im.sqlite3"
    init_db(path)
    connection = connect(path)
    first = contact_human(
        connection,
        employee_id="E1",
        conversation_key="u1",
        session_id="ses",
        body="第一句",
    )
    second = contact_human(
        connection,
        employee_id="E1",
        conversation_key="u2",
        session_id="ses-2",
        body="另一路",
    )
    claimed = claim_outbox(connection)
    assert claimed["delivery_id"] == first["delivery_id"]
    assert claim_outbox(connection)["delivery_id"] == second["delivery_id"]
    assert claim_outbox(connection) is None
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    connection.execute(
        "UPDATE t_nexus_im_outbox SET lease_until = ? WHERE delivery_id = ?",
        (past, first["delivery_id"]),
    )
    connection.commit()
    again = claim_outbox(connection)
    assert again["delivery_id"] == first["delivery_id"]
    assert again["lease_until"] != past


def test_expired_event_claim_can_be_taken_again(tmp_path):
    path = tmp_path / "im.sqlite3"
    init_db(path)
    connection = connect(path)
    now = datetime.now(UTC).isoformat()
    connection.execute(
        """
        INSERT INTO t_nexus_conversations (
            id, employee_id, owner_id, kind, name, agentscope_session_id, created_at, updated_at
        ) VALUES ('c1', 'E1', 'u1', 'private', '私聊', 'ses', ?, ?)
        """,
        (now, now),
    )
    bind_bot(connection, "E1", "mock", "bot-1")
    calls = {"n": 0}

    async def accept(employee_id, session_id, text):
        calls["n"] += 1

    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    connection.execute(
        """
        INSERT INTO t_nexus_im_events (
            provider, event_id, employee_id, session_id, accepted, claim_token, claim_until
        ) VALUES ('mock', 'evt-crash', 'E1', 'ses', 0, 'stuck', ?)
        """,
        (past,),
    )
    connection.commit()
    result = asyncio.run(
        accept_inbound(
            connection,
            provider="mock",
            event_id="evt-crash",
            bot_id="bot-1",
            sender_uid="u1",
            text="再来",
            accept=accept,
        )
    )
    assert result["duplicate"] is False
    assert calls["n"] == 1


def test_stolen_event_claim_does_not_record_the_first_handler(tmp_path):
    path = tmp_path / "im.sqlite3"
    init_db(path)
    connection = connect(path)
    now = datetime.now(UTC).isoformat()
    connection.execute(
        """
        INSERT INTO t_nexus_conversations (
            id, employee_id, owner_id, kind, name, agentscope_session_id, created_at, updated_at
        ) VALUES ('c1', 'E1', 'u1', 'private', '私聊', 'ses', ?, ?)
        """,
        (now, now),
    )
    bind_bot(connection, "E1", "mock", "bot-1")

    async def accept(_employee_id, _session_id, _text):
        connection.execute(
            "UPDATE t_nexus_im_events SET claim_token = 'other' "
            "WHERE provider = 'mock' AND event_id = 'evt-steal'"
        )
        connection.commit()

    with pytest.raises(ImError) as caught:
        asyncio.run(
            accept_inbound(
                connection,
                provider="mock",
                event_id="evt-steal",
                bot_id="bot-1",
                sender_uid="u1",
                text="再来",
                accept=accept,
            )
        )
    assert caught.value.status_code == 409
    row = connection.execute(
        "SELECT accepted, claim_token FROM t_nexus_im_events "
        "WHERE provider = 'mock' AND event_id = 'evt-steal'"
    ).fetchone()
    assert row["accepted"] == 0
    assert row["claim_token"] == "other"


class _EmptyStream:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_bytes(self):
        yield (
            b'data: {"type":"REPLY_START","reply_id":"r2"}\n\n'
            b'data: {"type":"REPLY_END","reply_id":"r2"}\n\n'
        )


class _QuietClient:
    configured = True

    def stream_session(self, _agent_id, _session_id, after=None):
        return _EmptyStream()

    async def list_session_messages(self, _agent_id, _session_id):
        return [
            {"id": "r1", "role": "assistant", "content": [{"type": "text", "text": "上一轮"}]},
            {"id": "r2", "role": "assistant", "content": []},
        ]


def test_silent_im_turn_does_not_repeat_the_previous_reply(tmp_path):
    path = tmp_path / "im.sqlite3"
    init_db(path)
    connection = connect(path)
    asyncio.run(
        capture_im_reply(
            connection,
            _QuietClient(),
            {"id": "E1", "agentscope_agent_id": "agent-1"},
            "ses",
            "u1",
        )
    )
    assert claim_outbox(connection) is None


def test_late_assistant_message_is_still_enqueued(tmp_path):
    path = tmp_path / "im.sqlite3"
    init_db(path)
    connection = connect(path)
    calls = {"n": 0}

    class _Late:
        configured = True

        def stream_session(self, _agent_id, _session_id, after=None):
            return _EmptyStream()

        async def list_session_messages(self, _agent_id, _session_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return []
            return [
                {
                    "id": "r2",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "晚到的回复"}],
                }
            ]

    asyncio.run(
        capture_im_reply(
            connection,
            _Late(),
            {"id": "E1", "agentscope_agent_id": "agent-1"},
            "ses",
            "u1",
        )
    )
    claimed = claim_outbox(connection)
    assert claimed is not None
    assert claimed["body"] == "晚到的回复"
    assert calls["n"] == 2


def test_send_private_passes_message_id_to_trigger_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    chats: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/" and request.method == "POST":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/sessions/" and request.method == "POST":
            return httpx.Response(201, json={"session_id": "ses-1"})
        if request.url.path.startswith("/sessions/") and request.method == "PATCH":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/chat/":
            body = json.loads(request.content.decode())
            chats.append(body)
            return httpx.Response(200, json={"status": "started", "session_id": body["session_id"]})
        if request.url.path == "/sessions/ses-1/messages":
            # AgentScope returns the persisted message using the passed message_id
            msg_id = chats[-1]["input"]["id"] if chats else "msg-fallback"
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "id": msg_id,
                            "role": "user",
                            "name": "u1",
                            "content": [{"type": "text", "text": "你好"}],
                            "metadata": {"display_text": "你好", "message_id": msg_id},
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    ],
                    "event_cursor": "",
                    "has_more": False,
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(handler)
    with TestClient(create_app()) as client:
        client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=transport, base_url="http://agentscope.test"),
        )
        emp = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        headers = {"X-User-ID": "u1", "X-Verified-UID": "u1"}
        conv_resp = client.post(
            f"/api/v1/employees/{emp['id']}/conversations",
            headers=headers,
            json={"name": "私聊"},
        )
        assert conv_resp.status_code in (200, 201), conv_resp.text
        conv = conv_resp.json()
        send_resp = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers=headers,
            json={"text": "你好", "attachment_ids": []},
        )
        assert send_resp.status_code == 200
        sent_msg = send_resp.json()["message"]
        assert sent_msg["accepted"] is True
        assert len(chats) == 1
        # Check that trigger_chat got the exact message id from send_private
        assert chats[0]["input"]["id"] == sent_msg["id"]
        assert chats[0]["input"]["metadata"]["message_id"] == sent_msg["id"]

        # Check get_conversation returns the exact message id
        get_resp = client.get(f"/api/v1/conversations/{conv['id']}", headers=headers)
        assert get_resp.status_code == 200
        history_msgs = get_resp.json()["messages"]
        assert len(history_msgs) == 1
        assert history_msgs[0]["id"] == sent_msg["id"]



"""人类群投递、时间线，以及员工邀请的 workspace 选择。"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from nexus_api.agentscope import AgentScopeClient
from nexus_api.groups import HumanEvents, chat_inputs, merge_timeline, messages_since
from nexus_api.main import create_app

ROOT = Path(__file__).resolve().parents[4]


def employee_body(**overrides):
    payload = {
        "name": "星河",
        "domain": "测试自动化工程师",
        "description": "负责自动化脚本",
        "instructions": "能直接回答的问题直接回答；需要同事时联系对方。",
        "avatar": "👩🏻‍💻",
        "enabled": True,
        "knowledge": [],
        "skills": [],
        "tools": [],
        "model_route_id": "route-test",
    }
    payload.update(overrides)
    return payload


def test_backlog_keeps_original_ids_until_the_cursor_moves():
    history = [
        {
            "id": "m1",
            "text": "先说一件事",
            "speaker_uid": "u-old",
            "content": [
                {"type": "text", "text": "先说一件事"},
                {"type": "data", "name": "a.png"},
            ],
        },
        {"id": "m2", "text": "@星河 接着看"},
    ]
    assert [item["id"] for item in messages_since(history, "")] == ["m1", "m2"]
    payload = chat_inputs(
        history,
        speaker="张三",
        verified_uid="u1",
        current_content=[{"type": "text", "text": "@星河 接着看\n\n文件正文"}],
    )
    assert [item["id"] for item in payload] == ["m1", "m2"]
    assert payload[0]["name"] == "u-old"
    assert payload[0]["metadata"]["verified_uid"] == "u-old"
    assert payload[0]["content"][1]["name"] == "a.png"
    assert payload[0]["metadata"]["display_text"] == "先说一件事"
    assert payload[1]["metadata"]["verified_uid"] == "u1"
    assert payload[1]["metadata"]["display_text"] == "@星河 接着看"
    assert payload[1]["content"][0]["text"].endswith("文件正文")
    assert [item["id"] for item in messages_since(history, "m2")] == []
    assert messages_since(history, "missing") == []


def test_timeline_dedupes_humans_and_team_hints_and_pages():
    hint = '<team-message from="星河">\n看一下\n</team-message>'
    back = '<team-message from="知微">\n看完了\n</team-message>'
    broadcast = '<team-message from="星河">\n全体注意\n</team-message>'
    streams = [
        {
            "session_id": "ses-a",
            "label": "星河",
            "messages": [
                {
                    "id": "u1",
                    "role": "user",
                    "name": "张三",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "content": [{"type": "text", "text": "先说一件事"}],
                },
                {
                    "id": "a1",
                    "role": "assistant",
                    "created_at": "2026-01-01T00:00:02+00:00",
                    "content": [{"type": "hint", "id": "h-back", "text": back}],
                },
            ],
        },
        {
            "session_id": "ses-b",
            "label": "知微",
            "messages": [
                {
                    "id": "u1",
                    "role": "user",
                    "name": "张三",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "content": [{"type": "text", "text": "先说一件事"}],
                },
                {
                    "id": "b1",
                    "role": "assistant",
                    "created_at": "2026-01-01T00:00:01+00:00",
                    "content": [{"type": "hint", "id": "h-ask", "text": hint}],
                },
                {
                    "id": "b2",
                    "role": "assistant",
                    "created_at": "2026-01-01T00:00:03+00:00",
                    "content": [{"type": "hint", "id": "h-all", "text": broadcast}],
                },
            ],
        },
        {
            "session_id": "ses-c",
            "label": "初禾",
            "messages": [
                {
                    "id": "c1",
                    "role": "assistant",
                    "created_at": "2026-01-01T00:00:03+00:00",
                    "content": [{"type": "hint", "id": "h-all", "text": broadcast}],
                }
            ],
        },
    ]
    page = merge_timeline(streams, limit=2)
    assert [item["id"] for item in page["items"]] == ["h-back", "h-all"]
    assert page["next_cursor"] == ""
    assert page["items"][0]["speaker"] == "知微→星河"
    assert page["items"][1]["speaker"] == "星河→全体"
    forward = merge_timeline(streams, cursor="2026-01-01T00:00:00+00:00|ses-a|u1", limit=2)
    assert [item["id"] for item in forward["items"]] == ["h-ask", "h-back"]
    rest = merge_timeline(streams, cursor=forward["next_cursor"], limit=10)
    assert [item["id"] for item in rest["items"]] == ["h-all"]
    assert rest["next_cursor"] == ""
    mailbox = {
        "session_id": "mail-b",
        "label": "知微邮箱",
        "messages": [
            {
                "id": "mail-1",
                "role": "assistant",
                "created_at": "2026-01-01T00:00:04+00:00",
                "content": [{"type": "hint", "id": "mail-hint", "text": back}],
            }
        ],
    }
    team_only = merge_timeline(streams)
    assert "看完了" in [item["text"] for item in team_only["items"]]
    assert "mail-hint" not in {item["id"] for item in team_only["items"]}
    assert mailbox["session_id"] not in {item["session_id"] for item in team_only["items"]}


def test_timeline_includes_the_employees_own_reply():
    streams = [
        {
            "session_id": "ses-a",
            "label": "星河",
            "messages": [
                {
                    "id": "u1",
                    "role": "user",
                    "name": "u-old",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "metadata": {"display_text": "问一句"},
                    "content": [{"type": "text", "text": "问一句"}],
                },
                {
                    "id": "a1",
                    "role": "assistant",
                    "created_at": "2026-01-01T00:00:01+00:00",
                    "content": [{"type": "text", "text": "答完了"}],
                },
            ],
        }
    ]
    page = merge_timeline(streams)
    assert [(item["kind"], item["speaker"], item["text"]) for item in page["items"]] == [
        ("human", "u-old", "问一句"),
        ("reply", "星河", "答完了"),
    ]


def test_human_event_skips_the_sender():
    events = HumanEvents()
    sender = events.subscribe("ch", "u1")
    other = events.subscribe("ch", "u2")
    assert events.publish("ch", "u1", {"id": "m1"}) == 1
    assert other.get_nowait()["id"] == "m1"
    assert sender.empty()


def test_channel_mention_replays_the_unmentioned_line_with_its_id(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    application = create_app()
    calls: list[dict] = []
    records: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/":
            body = json.loads(request.content.decode())
            assert body["chat_config"]["invite_config"]["invitable"] is True
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.url.path == "/sessions/":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"session-{body['name']}"})
        if request.url.path == "/nexus/record-message":
            records.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/chat/":
            calls.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"status": "started", "session_id": "ignored"})
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
        channel = test_client.post(
            "/api/v1/channels",
            json={"name": "版本交付", "description": "群", "member_ids": [star["id"]]},
        ).json()
        quiet = test_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers={"X-Verified-UID": "u1"},
            json={"text": "先说一件事"},
        )
        assert quiet.status_code == 200, quiet.text
        assert quiet.json()["triggered"] == []
        assert calls == []
        assert records[0]["text"] == "先说一件事"
        asked = test_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers={"X-Verified-UID": "u1"},
            json={"text": "@星河 那件事怎么样"},
        )
        assert asked.status_code == 200, asked.text
        assert asked.json()["triggered"][0]["accepted"] is True
        sent = calls[0]["input"]
        assert [item["id"] for item in sent] == [records[0]["message_id"], asked.json()["message"]["id"]]
        assert sent[0]["content"][0]["text"] == "先说一件事"
        again = test_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers={"X-Verified-UID": "u1"},
            json={"text": "@星河 再追问"},
        )
        assert [item["id"] for item in calls[1]["input"]] == [again.json()["message"]["id"]]
        listed = test_client.get(f"/api/v1/channels/{channel['id']}/messages").json()["messages"]
        assert [item["text"] for item in listed] == ["先说一件事", "@星河 那件事怎么样", "@星河 再追问"]


def test_channel_edit_keeps_the_cursor_speakers_and_attachments(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.url.path == "/sessions/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"session-{body['agent_id']}-{body['name']}"})
        if request.url.path == "/nexus/record-message":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/chat/":
            calls.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"status": "started", "session_id": "ignored"})
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    with TestClient(create_app()) as test_client:
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
        channel = test_client.post(
            "/api/v1/channels",
            json={"name": "版本交付", "description": "群", "member_ids": [star["id"]]},
        ).json()
        uploaded = test_client.post(
            "/api/v1/attachments",
            headers={"X-Verified-UID": "u-old"},
            files={"file": ("note.txt", b"OLD-NOTE", "text/plain")},
        )
        assert uploaded.status_code == 201, uploaded.text
        quiet = test_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers={"X-Verified-UID": "u-old"},
            json={"text": "先说一件事", "attachment_ids": [uploaded.json()["id"]]},
        )
        assert quiet.status_code == 200, quiet.text
        asked = test_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers={"X-Verified-UID": "u1"},
            json={"text": "@星河 那件事怎么样"},
        )
        assert asked.status_code == 200, asked.text
        first = calls[0]["input"]
        assert first[0]["name"] == "u-old"
        assert first[0]["metadata"]["verified_uid"] == "u-old"
        assert any("OLD-NOTE" in block.get("text", "") for block in first[0]["content"])
        assert first[1]["name"] == "u1"
        assert not any("OLD-NOTE" in block.get("text", "") for block in first[1]["content"])
        edited = test_client.patch(
            f"/api/v1/channels/{channel['id']}",
            json={
                "name": "版本交付",
                "description": "改过的目标",
                "member_ids": [star["id"], zhi["id"]],
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["created_at"] == channel["created_at"]
        from nexus_api.db import connect

        connection = connect(tmp_path / "nexus.db")
        cursors = {
            row["employee_id"]: row["last_delivered_message_id"]
            for row in connection.execute(
                "SELECT employee_id, last_delivered_message_id "
                "FROM t_nexus_channel_members WHERE channel_id = ?",
                (channel["id"],),
            )
        }
        connection.close()
        assert cursors[star["id"]] == asked.json()["message"]["id"]
        assert cursors[zhi["id"]] == ""
        again = test_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers={"X-Verified-UID": "u2"},
            json={"text": "@星河 再追问"},
        )
        assert [item["id"] for item in calls[1]["input"]] == [again.json()["message"]["id"]]
        assert calls[1]["input"][0]["name"] == "u2"
        wider = test_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers={"X-Verified-UID": "u1"},
            json={"text": "@知微 你也看"},
        )
        backlog = calls[2]["input"]
        assert wider.status_code == 200, wider.text
        assert [item["name"] for item in backlog] == ["u-old", "u1", "u2", "u1"]
        assert any("OLD-NOTE" in block.get("text", "") for block in backlog[0]["content"])
        assert not any("OLD-NOTE" in block.get("text", "") for block in backlog[-1]["content"])


def test_timeline_merges_replies_and_invited_member_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.url.path == "/sessions/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"session-{body['name']}"})
        if request.url.path == "/sessions/" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "sessions": [
                        {
                            "session": {"id": "session-时间线"},
                            "team": {
                                "team": {"session_id": "session-时间线"},
                                "members": [
                                    {
                                        "session_id": "session-时间线",
                                        "agent": {"id": "agent-星河", "name": "星河"},
                                    },
                                    {
                                        "session_id": "team-peer",
                                        "agent": {"id": "agent-知微", "name": "知微"},
                                    },
                                ],
                            },
                        }
                    ]
                },
            )
        if request.method == "GET" and request.url.path.endswith("/messages"):
            if request.url.path == "/sessions/team-peer/messages":
                return httpx.Response(
                    200,
                    json={
                        "messages": [
                            {
                                "id": "peer-1",
                                "role": "assistant",
                                "created_at": "2099-01-01T00:00:02+00:00",
                                "content": [{"type": "text", "text": "同事补充"}],
                            }
                        ],
                        "has_more": False,
                    },
                )
            if "时间线" in request.url.path and "记录" not in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "messages": [
                            {
                                "id": "reply-1",
                                "role": "assistant",
                                "created_at": "2099-01-01T00:00:01+00:00",
                                "content": [{"type": "text", "text": "我看过了"}],
                            }
                        ],
                        "has_more": False,
                    },
                )
            return httpx.Response(200, json={"messages": [], "has_more": False})
        if request.url.path == "/nexus/record-message":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/chat/":
            return httpx.Response(200, json={"status": "started", "session_id": "ignored"})
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    with TestClient(create_app()) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        star = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        channel = test_client.post(
            "/api/v1/channels",
            json={"name": "时间线", "description": "群", "member_ids": [star["id"]]},
        ).json()
        asked = test_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers={"X-Verified-UID": "u-old"},
            json={"text": "@星河 先问一下"},
        )
        assert asked.status_code == 200, asked.text
        page = test_client.get(f"/api/v1/channels/{channel['id']}/timeline")
        assert page.status_code == 200, page.text
        rows = [(item["kind"], item["speaker"], item["text"]) for item in page.json()["items"]]
        assert ("human", "u-old", "@星河 先问一下") in rows
        assert ("reply", "星河", "我看过了") in rows
        assert ("reply", "知微", "同事补充") in rows


def test_member_session_is_created_once_under_the_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    member_creates: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/" and request.method == "POST":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.url.path == "/sessions/" and request.method == "POST":
            body = json.loads(request.content.decode())
            if body.get("name") == "并发群":
                member_creates.append(body["name"])
                entered.set()
                await release.wait()
            return httpx.Response(201, json={"session_id": f"session-{body['name']}"})
        if request.url.path == "/nexus/record-message":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/chat/":
            return httpx.Response(200, json={"status": "started", "session_id": "ignored"})
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
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
                channel = (
                    await client.post(
                        "/api/v1/channels",
                        json={"name": "并发群", "description": "群", "member_ids": [employee["id"]]},
                    )
                ).json()

                async def mention():
                    return await client.post(
                        f"/api/v1/channels/{channel['id']}/messages",
                        headers={"X-Verified-UID": "u1"},
                        json={"text": "@星河 一起看"},
                    )

                first = asyncio.create_task(mention())
                second = None
                try:
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    second = asyncio.create_task(mention())
                    await asyncio.sleep(0.05)
                    assert member_creates == ["并发群"]
                finally:
                    release.set()
                assert second is not None
                followed = await asyncio.wait_for(second, timeout=3)
                led = await asyncio.wait_for(first, timeout=3)
                assert led.status_code == 200, led.text
                assert followed.status_code == 200, followed.text
                assert member_creates == ["并发群"]

    asyncio.run(scenario())


def test_running_record_session_rejects_without_a_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    application = create_app()
    chats: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/":
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/sessions/":
            return httpx.Response(201, json={"session_id": "record-1"})
        if request.url.path == "/nexus/record-message":
            return httpx.Response(409, json={"detail": "会话正在运行，没有写入"})
        if request.url.path == "/chat/":
            chats.append("chat")
            return httpx.Response(200, json={"status": "started", "session_id": "x"})
        return httpx.Response(404)

    with TestClient(application) as test_client:
        test_client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://agentscope.test"),
        )
        star = test_client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        channel = test_client.post(
            "/api/v1/channels",
            json={"name": "忙碌", "description": "运行中", "member_ids": [star["id"]]},
        ).json()
        sent = test_client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            json={"text": "@星河 现在写"},
        )
        assert sent.status_code == 200, sent.text
        assert chats == []
        assert sent.json()["triggered"][0]["accepted"] is False
        assert "运行" in sent.json()["record"]["detail"]
        visible = test_client.get(f"/api/v1/channels/{channel['id']}/messages").json()["messages"]
        assert visible[0]["text"] == "@星河 现在写"


def _agent_service():
    spec = importlib.util.spec_from_file_location(
        "nexus_agent_service_team",
        ROOT / "services/agent-service/agent_service.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_invite_uses_mailbox_workspace_and_not_a_private_or_other_team():
    module = _agent_service()

    class Session:
        def __init__(self, session_id: str, workspace_id: str) -> None:
            self.id = session_id
            self.config = type("Config", (), {"workspace_id": workspace_id})()

    private_a = Session("priv-u1", "ws-u1")
    private_b = Session("priv-u2", "ws-u2")
    mailbox_a = Session("mail-a", "ws-mail-a")
    mailbox_b = Session("mail-b", "ws-mail-b")
    other_team = Session("team-old", "ws-team-old")
    chosen_a = module.select_invite_session([private_a, other_team, mailbox_a], "mail-a")
    chosen_b = module.select_invite_session([private_b, mailbox_b], "mail-b")
    assert chosen_a.config.workspace_id == "ws-mail-a"
    assert chosen_b.config.workspace_id == "ws-mail-b"
    assert module.select_invite_session([private_a, other_team], None) is None
    assert module.select_invite_session([private_a], "missing") is None
    assert len({private_a.config.workspace_id, private_b.config.workspace_id, "ws-mail-a", "ws-mail-b"}) == 4

    class Storage:
        def __init__(self) -> None:
            self.sessions = [private_a, other_team, mailbox_a]

        async def list_sessions(self, user_id: str, agent_id: str):
            return list(self.sessions)

        async def get_session(self, *args):
            raise AssertionError("邀请不应再读私聊")

    async def lookup(agent_id: str) -> str:
        return "mail-a"

    view = module.MailboxSessionView(Storage(), lookup)
    listed = asyncio.run(view.list_sessions("nexus", "agent-a"))
    assert [item.id for item in listed] == ["mail-a"]


def test_record_write_takes_the_checkpoint_lock_and_skips_a_running_session():
    module = _agent_service()

    class Keys:
        @staticmethod
        def session_lock(session_id: str) -> str:
            return f"run:{session_id}"

        @staticmethod
        def session_event_checkpoint_lock(session_id: str) -> str:
            return f"ckpt:{session_id}"

        @staticmethod
        def session_events(session_id: str) -> str:
            return f"events:{session_id}"

    class Storage:
        def __init__(self) -> None:
            self.saved = []

        async def upsert_message(self, user_id, session_id, message) -> None:
            self.saved.append((user_id, session_id, message.id))

    class Bus:
        def __init__(self, locked: bool) -> None:
            self.locked = locked
            self.held: list[str] = []
            self.published = []

        async def is_locked(self, key: str) -> bool:
            return self.locked and key.startswith("run:")

        def acquire_lock(self, key: str):
            bus = self

            class Guard:
                async def __aenter__(self):
                    bus.held.append(key)

                async def __aexit__(self, *args):
                    return False

            return Guard()

        async def publish(self, key: str, payload: dict) -> None:
            self.published.append((key, payload))

    message = type("Msg", (), {"id": "m1"})()
    storage = Storage()
    idle = Bus(False)
    asyncio.run(
        module.write_record_message(
            storage,
            idle,
            message,
            user_id="nexus",
            session_id="record",
            keys=Keys,
        )
    )
    assert storage.saved == [("nexus", "record", "m1")]
    assert idle.held == ["ckpt:record"]
    assert idle.published[0][1]["type"] == "record"
    with pytest.raises(module.RecordRunning):
        asyncio.run(
            module.write_record_message(
                storage,
                Bus(True),
                message,
                user_id="nexus",
                session_id="record",
                keys=Keys,
            )
        )
    assert len(storage.saved) == 1


def test_launcher_keeps_team_tools_and_does_not_construct_agent_create():
    source = (ROOT / "services/agent-service/agent_service.py").read_text(encoding="utf-8")
    ast.parse(source)
    assert "AgentCreate" in source
    assert "AgentCreate(" not in source
    assert "TeamCreate(" not in source
    assert "write_record_message" in source
    assert "MailboxSessionView" in source


def test_timeline_keeps_two_identical_messages_sent_in_the_same_minute():
    """用户连续发送同样的文字是两条消息，不能按内容指纹折叠。"""

    streams = [
        {
            "session_id": "ses-a",
            "label": "星河",
            "messages": [
                {
                    "id": "m1",
                    "role": "user",
                    "name": "u1",
                    "created_at": "2026-01-01T00:00:05+00:00",
                    "metadata": {"display_text": "继续"},
                    "content": [{"type": "text", "text": "继续"}],
                },
                {
                    "id": "m2",
                    "role": "user",
                    "name": "u1",
                    "created_at": "2026-01-01T00:00:06+00:00",
                    "metadata": {"display_text": "继续"},
                    "content": [{"type": "text", "text": "继续"}],
                },
            ],
        }
    ]
    page = merge_timeline(streams)
    assert [item["id"] for item in page["items"]] == ["m1", "m2"]


def test_timeline_dedupes_the_same_message_id_across_sessions():
    """同一条人类消息被写进多个员工会话，id 相同只出现一次。"""

    message = {
        "id": "m1",
        "role": "user",
        "name": "u1",
        "created_at": "2026-01-01T00:00:05+00:00",
        "metadata": {"display_text": "一起看"},
        "content": [{"type": "text", "text": "一起看"}],
    }
    streams = [
        {"session_id": "ses-a", "label": "星河", "messages": [dict(message)]},
        {"session_id": "ses-b", "label": "知微", "messages": [dict(message)]},
    ]
    page = merge_timeline(streams)
    assert [item["id"] for item in page["items"]] == ["m1"]


def test_channel_native_timeline_renders_human_attachments(tmp_path, monkeypatch):
    """人类消息的附件必须进 content；只放 metadata 时历史重载后附件会消失。"""

    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    application = create_app()
    with TestClient(application) as client:
        employee = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        channel = client.post(
            "/api/v1/channels",
            json={"name": "图片", "description": "看图", "member_ids": [employee["id"]]},
        ).json()
        uploaded = client.post(
            "/api/v1/attachments",
            headers={"X-Owner-Id": "u1"},
            files={"file": ("pic.png", b"PNG-BYTES", "image/png")},
        ).json()
        sent = client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers={"X-Owner-Id": "u1"},
            json={"text": "", "attachment_ids": [uploaded["id"]]},
        )
        assert sent.status_code == 200, sent.text

        timeline = client.get(f"/api/v1/channels/{channel['id']}/timeline/native").json()

    human = next(stream for stream in timeline["streams"] if stream["session_id"] == channel["id"])
    content = human["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": ""}
    assert content[1] == {
        "type": "data",
        "id": f"att-{uploaded['id']}",
        "name": "pic.png",
        "created_at": sent.json()["message"]["created_at"],
        "source": {
            "type": "url",
            "url": f"/api/v1/attachments/{uploaded['id']}",
            "media_type": "image/png",
        },
    }


def test_channel_message_event_carries_native_message_and_new_sessions(tmp_path, monkeypatch):
    """其他人靠事件同步人类消息与刚建出来的成员 Session。"""

    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    application = create_app()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.url.path == "/sessions/":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"session-{body['name']}"})
        if request.url.path in {"/nexus/record-message", "/chat/"}:
            return httpx.Response(200, json={"ok": True, "status": "started"})
        if request.url.path == "/skill":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    published: list[dict] = []
    with TestClient(application) as client:
        client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="http://agentscope.test"
            ),
        )
        client.app.state.human_events.publish = (
            lambda channel_id, sender, payload: published.append(payload) or 0
        )
        employee = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        channel = client.post(
            "/api/v1/channels",
            json={"name": "同步", "description": "群", "member_ids": [employee["id"]]},
        ).json()
        uploaded = client.post(
            "/api/v1/attachments",
            headers={"X-Owner-Id": "u1"},
            files={"file": ("pic.png", b"PNG-BYTES", "image/png")},
        ).json()
        sent = client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers={"X-Owner-Id": "u1"},
            json={"text": "@星河 看图", "attachment_ids": [uploaded["id"]]},
        )
        assert sent.status_code == 200, sent.text

    event = published[-1]
    assert event["id"] == sent.json()["message"]["id"]
    assert event["speaker"] == "u1"
    assert event["sessions"] == [
        {"employee_id": employee["id"], "session_id": f"session-{channel['name']}"}
    ]
    assert event["message"]["content"][0] == {"type": "text", "text": "@星河 看图"}
    assert event["message"]["content"][1]["source"]["url"] == f"/api/v1/attachments/{uploaded['id']}"


def test_channel_record_conflict_still_broadcasts_the_human_message(tmp_path, monkeypatch):
    """记录 Session 忙碌（409）时人类消息已落库，其他成员仍要收到广播。"""

    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    application = create_app()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"agent_id": f"agent-{body['name']}"})
        if request.url.path == "/sessions/":
            body = json.loads(request.content.decode())
            return httpx.Response(201, json={"session_id": f"session-{body['name']}"})
        if request.url.path == "/nexus/record-message":
            return httpx.Response(409, json={"detail": "Session is busy"})
        if request.url.path in {"/chat/", "/skill"}:
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    published: list[dict] = []
    with TestClient(application) as client:
        client.app.state.agentscope = AgentScopeClient(
            "http://agentscope.test",
            "nexus",
            httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="http://agentscope.test"
            ),
        )
        client.app.state.human_events.publish = (
            lambda channel_id, sender, payload: published.append(payload) or 0
        )
        employee = client.post("/api/v1/employees", json=employee_body()).json()["employee"]
        channel = client.post(
            "/api/v1/channels",
            json={"name": "忙碌", "description": "群", "member_ids": [employee["id"]]},
        ).json()
        sent = client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers={"X-Owner-Id": "u1"},
            json={"text": "@星河 在吗"},
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["record"]["accepted"] is False

        timeline = client.get(f"/api/v1/channels/{channel['id']}/timeline/native").json()

    assert len(published) == 1
    assert published[0]["id"] == sent.json()["message"]["id"]
    human = next(stream for stream in timeline["streams"] if stream["session_id"] == channel["id"])
    assert human["messages"][0]["content"][0]["text"] == "@星河 在吗"

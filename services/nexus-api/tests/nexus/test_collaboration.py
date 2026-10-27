"""真实身份/数据库/HTTP 集成；AgentScope 替身仅用于确定性生命周期与上下文反例。"""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from threading import Event as ThreadEvent

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from nexus_api.collaboration import followup, select_participants
from nexus_api.db import connect, new_id, utcnow
from nexus_api.main import create_app
from nexus_api.sql import insert_record


class FakeAgentService:
    configured = True
    user_id = "nexus"

    def __init__(self):
        self.sessions = {}
        self.calls = []
        self.delay = 0.01
        self.interrupts = []
        self.models = []
        self.usage = {"input_tokens": 20, "output_tokens": 30}

    async def aclose(self):
        pass

    async def create_session(self, agent_id, name, **kwargs):
        sid = new_id("session")
        self.sessions[sid] = {"messages": [], "running": False}
        return sid

    async def set_permission_mode(self, *args):
        pass

    async def replace_session_model(self, *args):
        self.models.append(args)

    async def latest_event_cursor(self, *args):
        return "0-0"

    async def trigger_inputs(self, agent_id, sid, inputs):
        # 官方 AgentScope 2.0.10 Msg.validate_role_content 的 HTTP 边界：
        # user 只允许 text/data；不能让宽松替身掩盖实际 /chat/ 422。
        for message in inputs:
            if message.get("role") == "user":
                assert all(block["type"] in {"text", "data"} for block in message.get("content", []))
        assert not self.sessions[sid]["running"], "旧运行尚未释放"
        self.calls.append((agent_id, inputs))
        mid = new_id("reply")
        body = (
            "我是小牛码，负责开发。小掌柜，你负责哪些工作？"
            if agent_id == "agent-dev"
            else "我是小掌柜，我看到小牛码负责开发，我负责自动化测试。"
        )
        msg = {
            "id": mid,
            "name": agent_id,
            "role": "assistant",
            "content": [{"type": "text", "text": body}],
            "finished_at": utcnow(),
            "usage": dict(self.usage),
        }
        self.sessions[sid].update(running=True, reply=msg)
        return sid

    @asynccontextmanager
    async def stream_session(self, agent_id, sid, after):
        owner = self

        class Response:
            status_code = 200

            async def aiter_lines(self):
                data = owner.sessions[sid]
                msg = data["reply"]
                yield "data: " + json.dumps({"type": "REPLY_START", "reply_id": msg["id"]})
                await asyncio.sleep(owner.delay)
                if data["running"]:
                    yield "data: " + json.dumps(
                        {
                            "type": "TEXT_BLOCK_DELTA",
                            "reply_id": msg["id"],
                            "block_id": "b",
                            "delta": msg["content"][0]["text"],
                        }
                    )
                if data["running"]:
                    data["messages"].append(msg)
                data["running"] = False
                yield "data: " + json.dumps(
                    {"type": "REPLY_END", "reply_id": msg["id"], "finished_reason": "stop"}
                )

        yield Response()

    async def read_session_detail(self, agent_id, sid):
        data = self.sessions[sid]
        return data["messages"], "0-0", data["running"]

    async def interrupt_session(self, agent_id, sid):
        self.interrupts.append(sid)
        data = self.sessions[sid]
        if data["running"]:
            data["messages"].append(data["reply"])
        data["running"] = False


def record_uid(client, username):
    client.cookies.set("nexus_uid", username)
    assert client.get("/api/v1/me").json()["uid"] == username
    return username


def add_staff():
    with connect() as db:
        for eid, name, domain in [("dev", "小牛码", "软件开发"), ("qa", "小掌柜", "自动化测试工程师")]:
            insert_record(
                db,
                "t_nexus_employees",
                {
                    "id": eid,
                    "name": name,
                    "domain": domain,
                    "description": domain,
                    "instructions": "按实际发言交流",
                    "avatar": "🤖",
                    "enabled": 1,
                    "knowledge_json": "[]",
                    "skills_json": "[]",
                    "tools_json": "[]",
                    "agentscope_agent_id": "agent-" + eid,
                    "model_route_id": "route-test",
                    "created_at": utcnow(),
                    "updated_at": utcnow(),
                },
                ignore_keys=["id"],
            )
        db.commit()


@pytest.fixture
def setup(monkeypatch):
    async def sync(*args, **kwargs):
        return {}

    monkeypatch.setattr("nexus_api.main.sync_employee_skills", sync)
    app = create_app()
    fake = FakeAgentService()
    with TestClient(app) as owner:
        app.state.agentscope = fake
        uid = record_uid(owner, "owner")
        add_staff()
        group = owner.post("/api/v1/channels", json={"name": "研发协作", "member_ids": ["dev", "qa"]}).json()
        yield app, owner, uid, group["id"], fake


def wait_done(client, cid, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        page = client.get(f"/api/v1/channels/{cid}/messages").json()
        discussion = page["channel"]["discussion"]
        if discussion and discussion["state"] not in {"queued", "running", "superseded"}:
            return page
        time.sleep(0.03)
    raise AssertionError(f"讨论未结束: {page}")


def send(client, cid, body, key=None):
    return client.post(
        f"/api/v1/channels/{cid}/messages", json={"text": body, "client_id": key or new_id("client")}
    )


def join(owner, other, cid):
    invitation = owner.post(f"/api/v1/channels/{cid}/invitations").json()
    response = other.post("/api/v1/channels/join", json={"token": invitation["token"]})
    assert response.status_code == 200, response.text


def assistance_event(cid, sid, initiator):
    from sqlalchemy.orm import Session

    from app.models import Event, Task, TaskWakeOutbox
    from app.models import utcnow as db_now

    with connect() as db:
        with Session(bind=db) as session:
            session.add(Task(
                id="T-ASSIST", title="协助任务", task_type="ISSUE", status="WAITING",
                responsible_employee_id="dev", source_channel_id=cid, source_session_id=sid,
                waiting_reason="HITL_QUESTION",
            ))
            session.flush()
            session.add(Event(
                task_id="T-ASSIST", event_type="group_task_requested", message="来源审计",
                detail={"initiator_id": initiator},
            ))
            event = Event(task_id="T-ASSIST", event_type="TASK_WAITING", message="Worker 请求协助")
            session.add(event)
            session.flush()
            event_id = event.id
            session.add(TaskWakeOutbox(
                event_id=event_id, task_id="T-ASSIST", responsible_employee_id="dev",
                source_session_id=sid, status="pending", next_retry_at=db_now(),
                hint="请调用 nexus_task_get 和 nexus_task_assistance 先处理 T-ASSIST",
            ))
            session.commit()
        db.commit()
    return event_id


@pytest.mark.parametrize("revoked", [False, True])
def test_worker_assistance_starts_new_authorized_employee_discussion(setup, monkeypatch, revoked):
    _, owner, uid, cid, fake = setup
    send(owner, cid, "@小牛码 你好")
    old = wait_done(owner, cid)["channel"]["discussion"]
    original_calls = len(fake.calls)

    async def attach(*args, **kwargs):
        pass  # 这里只替换外部 AgentScope 资源安装，讨论、身份与队列使用真实实现。

    monkeypatch.setattr("nexus_api.task_skill.attach_task_capability", attach)
    with connect() as db:
        sid = db.execute(text("""SELECT agentscope_session_id FROM t_nexus_channel_members
            WHERE channel_id=:c AND employee_id='dev'"""), {"c": cid}).scalar()
        db.execute(text("UPDATE t_nexus_employees SET skills_json='[\"task-management\"]' WHERE id='dev'"))
        if revoked:
            db.execute(text("""UPDATE t_nexus_channel_members SET membership_active=0
                WHERE channel_id=:c AND member_type='human' AND member_id=:u"""), {"c": cid, "u": uid})
        db.commit()
    event_id = assistance_event(cid, sid, uid)
    headers = {"X-Nexus-Internal-Key": "test-internal-key"}
    for duplicate in (False, True):
        result = owner.post("/api/v1/internal/group-task-event", headers=headers, json={"event_id": event_id})
        assert result.status_code == 200, result.text
        assert result.json()["duplicate"] is duplicate
        if not duplicate:
            assert result.json()["assistance_scheduled"] is not revoked
    with connect() as db:
        rows = list(db.execute(text("SELECT * FROM t_nexus_discussions WHERE channel_id=:c"),
                               {"c": cid}).mappings())
    if revoked:
        assert len(rows) == 1 and len(fake.calls) == original_calls
    else:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            page = owner.get(f"/api/v1/channels/{cid}/messages").json()
            discussion = page["channel"]["discussion"]
            if discussion["id"] != old["id"] and discussion["state"] == "completed":
                break
            time.sleep(0.03)
        else:
            raise AssertionError(f"任务协助讨论没有完成：{page}")
        assert len(fake.calls) == original_calls + 1
        assert fake.calls[-1][0] == "agent-dev"
        hints = [m for m in fake.calls[-1][1] if m.get("metadata", {}).get("source") == "nexus-task"]
        assert any("nexus_task_assistance" in b["text"] for m in hints for b in m["content"])
        notice = next(x for x in page["items"] if x["id"] == f"task_event_{event_id}")
        assert "nexus_task_assistance" not in notice["text"]
        with connect() as db:
            current = db.execute(text("SELECT * FROM t_nexus_discussions WHERE id=:i"),
                                 {"i": discussion["id"]}).mappings().one()
        assert current["initiator_id"] == uid
        assert json.loads(current["participants_json"]) == ["dev"]


def test_peer_http_response_matches_committed_delivery(setup):
    _, owner, _, cid, fake = setup
    fake.delay = 0.25
    with connect() as db:
        db.execute(
            text("UPDATE t_nexus_employees SET tools_json=:t WHERE id='dev'"),
            {"t": json.dumps(["contact_employee"])},
        )
        db.commit()
    sent = send(owner, cid, "@小牛码 请联系同事")
    did = sent.json()["discussion_id"]
    deadline = time.monotonic() + 3
    sid = None
    while time.monotonic() < deadline:
        with connect() as db:
            sid = db.execute(
                text(
                    "SELECT agentscope_session_id FROM t_nexus_channel_members "
                    "WHERE channel_id=:c AND employee_id='dev'"
                ),
                {"c": cid},
            ).scalar()
        if sid:
            break
        time.sleep(0.01)
    assert sid
    response = owner.get(
        "/api/v1/peers/resolve",
        headers={"X-Nexus-Internal-Key": "test-internal-key"},
        params={"session_id": sid, "name": "小掌柜", "message": "唯一联系反例", "execution_id": did},
    )
    assert response.status_code == 200, response.text
    assert response.json()["scheduled"] is True
    page = wait_done(owner, cid)
    assert sum(item["text"] == "唯一联系反例" for item in page["items"]) == 1


def test_peer_delivery_wait_does_not_block_runtime_writer(setup, monkeypatch):
    app, owner, _, _, _ = setup
    loop = owner.portal.call(asyncio.get_running_loop)
    with connect() as db:
        db.execute(text("UPDATE t_nexus_channel_members SET agentscope_session_id='group-session' "
                        "WHERE employee_id='dev'"))
        db.commit()

    def waiting_delivery(*args):
        # 同步目录写入等待锁时，异步 Runtime writer 必须仍能释放锁。
        writer_finished = ThreadEvent()
        loop.call_soon_threadsafe(writer_finished.set)
        if not writer_finished.wait(1):
            raise RuntimeError("联系投递阻塞了 Runtime writer 所在的事件循环")
        return {"scheduled": True, "sender_name": "小牛码"}

    monkeypatch.setattr(app.state.collaboration, "request_peer", waiting_delivery)
    response = owner.get(
        "/api/v1/peers/resolve",
        headers={"X-Nexus-Internal-Key": "test-internal-key"},
        params={"session_id": "group-session", "name": "小掌柜", "message": "并发联系"},
    )
    assert response.status_code == 200
    assert response.json()["scheduled"] is True


def test_temporary_uid_and_group_membership_boundary(setup):
    app, _, uid, cid, _ = setup
    outsider = TestClient(app)
    assert outsider.get(f"/api/v1/channels/{cid}/messages").status_code == 401
    # UID 只是临时标识；明确允许恢复同一个 UID，并非认证凭据。
    assert outsider.get(f"/api/v1/channels/{cid}/messages", headers={"X-Owner-Id": uid}).status_code == 200
    record_uid(outsider, "outsider")
    assert outsider.get(f"/api/v1/channels/{cid}/timeline/native").status_code == 403
    assert (
        outsider.post(
            f"/api/v1/channels/{cid}/messages",
            json={"text": "伪造", "client_id": "spoof-0001", "sender_id": uid},
        ).status_code
        == 422
    )
    assert outsider.get("/api/v1/internal/group-policy?session_id=x").status_code == 401


def test_lifecycle_permissions_invite_read_leave_and_revocation(setup):
    app, owner, uid, cid, _ = setup
    other = TestClient(app)
    other_uid = record_uid(other, "colleague")
    join(owner, other, cid)
    assert other.post(f"/api/v1/channels/{cid}/invitations").status_code == 403
    assert other.patch(f"/api/v1/channels/{cid}/humans/{uid}", json={"role": "admin"}).status_code == 403
    assert (
        owner.patch(f"/api/v1/channels/{cid}/humans/{other_uid}", json={"role": "admin"}).status_code == 200
    )
    assert other.post(f"/api/v1/channels/{cid}/invitations").status_code == 200
    assert other.post(f"/api/v1/channels/{cid}/read", json={"seq": 9999}).status_code == 200
    assert other.post(f"/api/v1/channels/{cid}/leave").status_code == 200
    assert other.get(f"/api/v1/channels/{cid}/messages").status_code == 403
    assert other.get(f"/api/v1/channels/{cid}/events").status_code == 403
    join(owner, other, cid)
    member = next(
        m
        for m in other.get(f"/api/v1/channels/{cid}/members").json()["members"]
        if m["member_id"] == other_uid
    )
    assert member["role"] == "member" and member["read_seq"] == 0
    assert owner.delete(f"/api/v1/channels/{cid}/humans/{other_uid}").status_code == 200
    assert send(other, cid, "离群后发送").status_code == 403
    assert owner.post(f"/api/v1/channels/{cid}/leave").status_code == 409


def test_ordered_real_previous_reply_and_finite_turns(setup):
    _, owner, _, cid, fake = setup
    result = send(owner, cid, "@小牛码 @小掌柜 你们相互介绍下")
    assert result.status_code == 200, result.text
    assert result.json()["participants"] == ["dev", "qa"]
    page = wait_done(owner, cid)
    assert page["channel"]["discussion"]["state"] == "completed"
    assert [a for a, _ in fake.calls] == ["agent-dev", "agent-qa"]
    assert any(
        "小掌柜，你负责哪些工作？" in str(item["content"]) and item["name"] == "小牛码"
        for item in fake.calls[1][1]
    )
    replies = [item for item in page["items"] if item["sender_type"] == "employee"]
    assert [item["sender_id"] for item in replies] == ["dev", "qa"]
    assert all(item["text"] == item["message"]["content"][0]["text"] for item in replies)
    assert page["items"] == sorted(page["items"], key=lambda item: item["seq"])


def test_no_mentions_and_single_mention(setup):
    _, owner, _, cid, fake = setup
    assert send(owner, cid, "大家好").json()["participants"]
    wait_done(owner, cid)
    fake.calls.clear()
    assert send(owner, cid, "自动化测试进展如何？").json()["participants"] == ["qa"]
    wait_done(owner, cid)
    assert [a for a, _ in fake.calls] == ["agent-qa"]
    fake.calls.clear()
    assert send(owner, cid, "@小掌柜 你好").json()["participants"] == ["qa"]
    wait_done(owner, cid)
    assert [a for a, _ in fake.calls] == ["agent-qa"]


def test_humans_concurrent_idempotent_paginated_and_no_model_calls(setup):
    app, owner, _, cid, fake = setup
    other = TestClient(app)
    record_uid(other, "colleague")
    join(owner, other, cid)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda i: send(owner if i % 2 else other, cid, f"工作记录{i}", f"same-msg-{i:04}"), range(24)
            )
        )
    assert all(r.status_code == 200 for r in results)
    assert send(owner, cid, "工作记录1", "same-msg-0001").json()["duplicate"]
    assert send(owner, cid, "不同正文", "same-msg-0001").status_code == 409
    assert not fake.calls
    page = owner.get(f"/api/v1/channels/{cid}/messages?limit=10").json()
    all_items = page["items"]
    while page["before"]:
        page = owner.get(f"/api/v1/channels/{cid}/messages?limit=10&before={page['before']}").json()
        all_items = page["items"] + all_items
    human = [x for x in all_items if x["sender_type"] == "human"]
    assert len(human) == 24
    assert len({x["id"] for x in human}) == 24
    assert len({x["sender_id"] for x in human}) == 2
    assert len({x["seq"] for x in all_items}) == len(all_items)
    assert other.get(f"/api/v1/channels/{cid}/messages?limit=100").json()["items"] == all_items


def test_new_message_interrupts_and_replaces_goal(setup):
    _, owner, _, cid, fake = setup
    fake.delay = 1
    send(owner, cid, "@小牛码 @小掌柜 讨论测试")
    deadline = time.monotonic() + 3
    while not fake.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fake.calls
    send(owner, cid, "@小掌柜 请只回答现在的问题")
    page = wait_done(owner, cid)
    assert fake.interrupts
    assert [a for a, _ in fake.calls] == ["agent-dev", "agent-qa"]
    assert page["channel"]["discussion"]["state"] == "completed"


def test_tool_policy_checks_human_and_employee_and_retired_binding(setup):
    app, owner, _, cid, _ = setup
    other = TestClient(app)
    record_uid(other, "colleague")
    join(owner, other, cid)
    send(other, cid, "@小掌柜 测试")
    wait_done(other, cid)
    with connect() as db:
        db.execute(
            text(
                'UPDATE t_nexus_employees SET tools_json=\'["Bash","contact_employee"]\', '
                "skills_json='[\"task-management\"]' WHERE id='qa'"
            )
        )
        db.execute(
            text(
                "UPDATE t_nexus_discussions SET state='running',active_employee_id='qa' WHERE channel_id=:c"
            ),
            {"c": cid},
        )
        sid = db.execute(
            text(
                "SELECT agentscope_session_id FROM t_nexus_channel_members "
                "WHERE channel_id=:c AND employee_id='qa'"
            ),
            {"c": cid},
        ).scalar_one()
        db.execute(
            text(
                "UPDATE t_nexus_channels SET lease_until='2999-01-01',lease_token='policy-test' WHERE id=:c"
            ),
            {"c": cid},
        )
        db.commit()
    policy = app.state.collaboration.policy(sid)
    assert policy["allowed_tools"] == ["Bash", "contact_employee"]
    assert policy["task_allowed"]
    assert app.state.collaboration.policy(sid, "Bash", policy["discussion_id"])["task_allowed"]
    assert (
        owner.patch(f"/api/v1/channels/{cid}", json={"name": "群组", "member_ids": ["dev"]}).status_code
        == 200
    )
    with pytest.raises(HTTPException):
        app.state.collaboration.policy(sid)


def test_routing_suppresses_human_chatter_and_limits_followup():
    staff = [
        {
            "id": "qa",
            "name": "自动化同事",
            "enabled": True,
            "agentscope_agent_id": "a",
            "domain": "自动化测试工程师",
            "description": "测试用例",
        }
    ]
    assert select_participants("@小王 你明天到吗？", staff, human_count=2) == []
    assert select_participants("我已经更新了自动化测试报告", staff, human_count=2) == []
    assert select_participants("自动化测试进展如何？", staff, human_count=2) == ["qa"]
    assert followup("自动化同事，谢谢你", staff, "other", {"qa": 0}) == []
    assert followup("自动化同事，你怎么看？", staff, "other", {"qa": 2}) == []


def test_attachment_access_is_revoked_with_membership(setup):
    app, owner, _, cid, _ = setup
    other = TestClient(app)
    record_uid(other, "colleague")
    join(owner, other, cid)
    attachment = owner.post(
        "/api/v1/attachments", files={"file": ("report.txt", b"group-only-report", "text/plain")}
    ).json()
    sent = owner.post(
        f"/api/v1/channels/{cid}/messages",
        json={"text": "共享报告", "attachment_ids": [attachment["id"]], "client_id": "attachment-0001"},
    )
    assert sent.status_code == 200
    assert other.get(f"/api/v1/attachments/{attachment['id']}").status_code == 200
    assert other.post(f"/api/v1/channels/{cid}/leave").status_code == 200
    assert other.get(f"/api/v1/attachments/{attachment['id']}").status_code == 404


def test_local_account_and_permission_routes_are_removed(setup):
    app, owner, _, _, _ = setup
    paths = set(app.openapi()["paths"])
    assert not any(path.startswith("/api/v1/auth/") for path in paths)
    assert not any(path.startswith("/api/v1/users") for path in paths)
    for path in ["auth/register", "auth/login", "auth/logout", "users/owner/permissions"]:
        assert owner.post(f"/api/v1/{path}", json={}).status_code == 404
    assert owner.get("/api/v1/users").status_code == 404


def test_different_humans_employee_requests_are_queued_independently(setup):
    app, owner, _, cid, fake = setup
    other = TestClient(app)
    record_uid(other, "colleague")
    join(owner, other, cid)
    fake.delay = 0.2
    first = send(owner, cid, "@小掌柜 说明测试范围").json()["discussion_id"]
    second = send(other, cid, "@小牛码 说明开发范围").json()["discussion_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with connect() as db:
            states = dict(
                db.execute(
                    text("SELECT id,state FROM t_nexus_discussions WHERE channel_id=:c"), {"c": cid}
                ).all()
            )
        if states.get(first) == states.get(second) == "completed":
            break
        time.sleep(0.03)
    assert states[first] == states[second] == "completed"
    assert {a for a, _ in fake.calls} == {"agent-dev", "agent-qa"}


def test_existing_sse_connection_stops_when_human_leaves(setup):
    app, owner, _, cid, _ = setup
    other = TestClient(app)
    record_uid(other, "colleague")
    join(owner, other, cid)

    def leave():
        time.sleep(0.2)
        assert other.post(f"/api/v1/channels/{cid}/leave").status_code == 200
        send(owner, cid, "离群之后的私密消息")

    with ThreadPoolExecutor(max_workers=1) as pool:
        operation = pool.submit(leave)
        with other.stream("GET", f"/api/v1/channels/{cid}/events") as response:
            body = response.read().decode()
        operation.result()
    assert '"type":"revoked"' in body
    assert "离群之后的私密消息" not in body


def wait_discussion_state(client, cid, did, timeout=10):
    end = time.monotonic() + timeout
    state = ""
    while time.monotonic() < end:
        with connect() as db:
            row = db.execute(text("SELECT state FROM t_nexus_discussions WHERE id=:i"), {"i": did}).first()
        state = row[0] if row else ""
        if state in {"completed", "limited", "failed", "interrupted"}:
            return state
        time.sleep(0.03)
    raise AssertionError(f"讨论未结束: {state}")


def test_token_budget_stops_following_employee_and_caps_output(setup):
    _, owner, _, cid, fake = setup
    fake.usage = {"input_tokens": 9000, "output_tokens": 16001}
    sent = send(owner, cid, "@小牛码 @小掌柜 你们相互介绍下")
    assert sent.json()["participants"] == ["dev", "qa"]
    page = wait_done(owner, cid)
    assert page["channel"]["discussion"]["state"] == "limited"
    assert [agent for agent, _ in fake.calls] == ["agent-dev"]
    assert fake.models[0][2]["parameters"]["max_tokens"] == 768
    with connect() as db:
        tokens = db.execute(
            text("SELECT tokens FROM t_nexus_discussions WHERE id=:i"),
            {"i": sent.json()["discussion_id"]},
        ).scalar_one()
    assert tokens == 25001


def test_expired_lease_interrupts_stale_run_then_processes_queue(setup):
    app, owner, uid, cid, fake = setup
    stale, queued, stale_session = new_id("dis"), new_id("dis"), new_id("session")
    fake.sessions[stale_session] = {"messages": [], "running": True, "reply": None}
    with connect() as db:
        db.execute(
            text(
                """UPDATE t_nexus_channel_members SET agentscope_session_id=:s
                WHERE channel_id=:c AND employee_id='dev'"""
            ),
            {"s": stale_session, "c": cid},
        )
        db.execute(
            text(
                """INSERT INTO t_nexus_discussions
                (id,channel_id,message_id,initiator_id,generation,state,participants_json,
                 active_employee_id,created_at) VALUES (:i,:c,:m,:u,0,'running','["dev"]','dev',:t)"""
            ),
            {"i": stale, "c": cid, "m": new_id("msg"), "u": uid, "t": utcnow()},
        )
        db.execute(
            text(
                """INSERT INTO t_nexus_discussions
                (id,channel_id,message_id,initiator_id,generation,state,participants_json,created_at)
                VALUES (:i,:c,:m,:u,1,'queued','["qa"]',:t)"""
            ),
            {"i": queued, "c": cid, "m": new_id("msg"), "u": uid, "t": utcnow()},
        )
        db.execute(
            text(
                """UPDATE t_nexus_channels SET lease_token='dead-lease',
                lease_until='2000-01-01T00:00:00+00:00' WHERE id=:c"""
            ),
            {"c": cid},
        )
        db.commit()
    assert wait_discussion_state(owner, cid, queued) == "completed"
    with connect() as db:
        states = dict(
            db.execute(text("SELECT id,state FROM t_nexus_discussions WHERE channel_id=:c"), {"c": cid}).all()
        )
        lease = db.execute(
            text("SELECT lease_token,lease_until FROM t_nexus_channels WHERE id=:c"), {"c": cid}
        ).one()
    assert states[stale] == "interrupted"
    assert fake.interrupts == [stale_session]
    assert [agent for agent, _ in fake.calls] == ["agent-qa"]
    assert tuple(lease) == ("", "")


def test_unconfirmed_stop_keeps_lease_for_recovery(monkeypatch, setup):
    app, owner, uid, cid, fake = setup

    async def stuck(self, employee, session_id):
        raise TimeoutError("AgentScope 未确认中断")

    monkeypatch.setattr(app.state.collaboration, "stop_session", stuck.__get__(app.state.collaboration))
    stale = new_id("dis")
    with connect() as db:
        db.execute(
            text(
                """UPDATE t_nexus_channel_members SET agentscope_session_id='ses-stale'
                WHERE channel_id=:c AND employee_id='dev'"""
            ),
            {"c": cid},
        )
        db.execute(
            text(
                """INSERT INTO t_nexus_discussions
                (id,channel_id,message_id,initiator_id,generation,state,participants_json,
                 active_employee_id,created_at) VALUES (:i,:c,:m,:u,0,'running','["dev"]','dev',:t)"""
            ),
            {"i": stale, "c": cid, "m": new_id("msg"), "u": uid, "t": utcnow()},
        )
        db.execute(
            text(
                """UPDATE t_nexus_channels SET lease_token='dead-lease',
                lease_until='2000-01-01T00:00:00+00:00' WHERE id=:c"""
            ),
            {"c": cid},
        )
        db.commit()
    deadline = time.monotonic() + 5
    token = ""
    while time.monotonic() < deadline:
        with connect() as db:
            token = db.execute(
                text("SELECT lease_token FROM t_nexus_channels WHERE id=:c"), {"c": cid}
            ).scalar_one()
        if token and token != "dead-lease":
            stale_lease = token
            break
        time.sleep(0.03)
    assert token and token != "dead-lease"
    # 未确认停止时租约保留，等待过期后重试，而不是立即释放造成双执行。
    time.sleep(0.5)
    with connect() as db:
        kept = db.execute(
            text("SELECT lease_token FROM t_nexus_channels WHERE id=:c"), {"c": cid}
        ).scalar_one()
    assert kept == stale_lease


def test_tool_policy_requires_user_grant_and_matching_mcp_ref(setup):
    app, owner, _, cid, _ = setup
    other = TestClient(app)
    record_uid(other, "colleague")
    join(owner, other, cid)
    send(other, cid, "@小掌柜 测试")
    done = wait_done(other, cid)
    with connect() as db:
        now = utcnow()
        insert_record(
            db,
            "t_nexus_mcps",
            {
                "id": "mcp-demo",
                "name": "演示工具",
                "url": "http://127.0.0.1:9/mcp",
                "runtime_name": "demo",
                "created_at": now,
                "updated_at": now,
            },
            ignore_keys=["id"],
        )
        db.execute(text("UPDATE t_nexus_employees SET mcp_json='[\"mcp-demo\"]' WHERE id='qa'"))
        db.execute(text("UPDATE t_nexus_channels SET lease_until='2999-01-01' WHERE id=:c"), {"c": cid})
        db.execute(
            text(
                """UPDATE t_nexus_discussions SET state='running',active_employee_id='qa'
                WHERE id=:i"""
            ),
            {"i": done["channel"]["discussion"]["id"]},
        )
        sid = db.execute(
            text(
                """SELECT agentscope_session_id FROM t_nexus_channel_members
                WHERE channel_id=:c AND employee_id='qa'"""
            ),
            {"c": cid},
        ).scalar_one()
        db.commit()
    tool = "mcp__demo__lookup"
    discussion_id = done["channel"]["discussion"]["id"]
    with pytest.raises(HTTPException):
        app.state.collaboration.policy(sid, tool, discussion_id, "mcp-other")
    with pytest.raises(HTTPException):
        app.state.collaboration.policy(sid, tool, discussion_id, "")
    policy = app.state.collaboration.policy(sid, tool, discussion_id, "mcp-demo")
    assert policy["mcp_refs"] == {"mcp-demo": "demo"}
    with pytest.raises(HTTPException):
        app.state.collaboration.policy(sid, tool, "dis-other", "mcp-demo")


def test_task_notification_survives_closed_discussion_and_is_idempotent(setup):
    from sqlalchemy.orm import Session

    from app.models import Event, Task, TaskWakeOutbox
    from app.models import utcnow as db_now

    _, owner, _, cid, _ = setup
    send(owner, cid, "@小牛码 你好")
    page = wait_done(owner, cid)
    with connect() as db:
        sid = db.execute(
            text(
                "SELECT agentscope_session_id FROM t_nexus_channel_members "
                "WHERE channel_id=:c AND employee_id='dev'"
            ),
            {"c": cid},
        ).scalar()
        with Session(bind=db) as session:
            session.add(
                Task(
                    id="T-NOTICE",
                    title="交付验收",
                    task_type="REQUIREMENT",
                    responsible_employee_id="dev",
                    source_channel_id=cid,
                    source_session_id=sid,
                    status="WAITING",
                )
            )
            session.flush()
            event = Event(task_id="T-NOTICE", event_type="TASK_WAITING", message="执行结束，待确认")
            session.add(event)
            session.flush()
            eid = event.id
            session.add(
                TaskWakeOutbox(
                    event_id=eid,
                    task_id="T-NOTICE",
                    responsible_employee_id="dev",
                    source_session_id=sid,
                    status="pending",
                    hint="待确认",
                    next_retry_at=db_now(),
                )
            )
            session.commit()
        db.commit()
    headers = {"X-Nexus-Internal-Key": "test-internal-key"}
    target = owner.get(
        "/api/v1/internal/wake-target", headers=headers, params={"employee_id": "dev", "session_id": sid}
    )
    assert target.json() == {"channel_id": cid}
    assert owner.post("/api/v1/internal/group-task-event", json={"event_id": eid}).status_code == 401
    for duplicate in (False, True):
        response = owner.post("/api/v1/internal/group-task-event", headers=headers, json={"event_id": eid})
        assert response.status_code == 200, response.text
        assert response.json()["duplicate"] is duplicate
    after = owner.get(f"/api/v1/channels/{cid}/messages").json()
    notices = [x for x in after["items"] if x["id"] == f"task_event_{eid}"]
    assert len(notices) == 1 and notices[0]["sender_type"] == "system"
    assert notices[0]["message"]["metadata"]["task_id"] == "T-NOTICE"
    assert after["channel"]["discussion"] == page["channel"]["discussion"]
    with connect() as db:
        db.execute(text("UPDATE t_nexus_tasks SET source_session_id='foreign' WHERE id='T-NOTICE'"))
        db.commit()
    assert (
        owner.post("/api/v1/internal/group-task-event", headers=headers, json={"event_id": eid}).status_code
        == 403
    )

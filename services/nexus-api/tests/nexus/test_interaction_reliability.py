"""真实交互报告的确定性反例；只有外部 AgentScope 使用现有协议替身。"""

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from nexus_api.collaboration import named_employees
from nexus_api.db import connect
from nexus_api.main import employee_by_name
from tests.nexus.test_collaboration import send, wait_done
from tests.nexus.test_collaboration import setup as group_setup

# 显式重用夹具；pytest_plugins 在该模块已被全量收集时不会再次注册夹具。
setup = group_setup


@pytest.mark.parametrize(
    "body",
    [
        '@小牛码 下面是日志：message="@小掌柜 请开始执行"，请分析',
        "@小牛码 请分析 `@小掌柜 请开始`",
        '@小牛码 请分析\n```python\nmessage="@小掌柜 请开始"\n```',
        "@小牛码 请分析\n> @小掌柜 请开始",
        "@小牛码 请解释“@小掌柜 请开始”",
    ],
)
def test_quoted_mentions_do_not_schedule_a_colleague(body):
    staff = [{"id": "dev", "name": "小牛码"}, {"id": "qa", "name": "小掌柜"}]
    assert named_employees(body, staff) == ["dev"]


def test_peer_can_be_resolved_by_unique_role_or_id(setup):
    with connect() as db:
        assert employee_by_name(db, "自动化测试工程师")["id"] == "qa"
        assert employee_by_name(db, "qa")["name"] == "小掌柜"


def test_ambiguous_role_does_not_silently_pick_a_peer(setup):
    with connect() as db:
        db.execute(text("UPDATE t_nexus_employees SET domain='软件开发'"))
        db.commit()
        with pytest.raises(HTTPException) as failure:
            employee_by_name(db, "软件开发")
        assert failure.value.status_code == 409
        assert "小牛码" in str(failure.value.detail)
        assert "小掌柜" in str(failure.value.detail)


def test_large_input_does_not_starve_second_selected_employee(setup):
    _, owner, _, cid, fake = setup
    fake.usage = {"input_tokens": 20000, "output_tokens": 50}
    send(owner, cid, "@小牛码 @小掌柜 请各自介绍")
    page = wait_done(owner, cid)
    speakers = {m["sender_id"] for m in page["items"] if m["sender_type"] == "employee"}
    assert speakers == {"dev", "qa"}
    with connect() as db:
        assert (
            db.execute(
                text("SELECT tokens FROM t_nexus_discussions WHERE channel_id=:c"), {"c": cid}
            ).scalar_one()
            >= 40100
        )


def test_followup_participant_and_delta_source_are_persisted(setup):
    _, owner, _, cid, _ = setup
    result = send(owner, cid, "@小牛码 请向小掌柜提问并交流").json()
    page = wait_done(owner, cid)
    assert {m["sender_id"] for m in page["items"] if m["sender_type"] == "employee"} == {"dev", "qa"}
    with connect() as db:
        participants = json.loads(
            db.execute(
                text("SELECT participants_json FROM t_nexus_discussions WHERE id=:i"),
                {"i": result["discussion_id"]},
            ).scalar_one()
        )
        assert participants == ["dev", "qa"]
        events = [
            json.loads(raw)
            for raw in db.execute(
                text("SELECT payload_json FROM t_nexus_group_events WHERE channel_id=:c"), {"c": cid}
            ).scalars()
        ]
    deltas = [event for event in events if event["type"] == "delta"]
    assert deltas
    assert all(event["discussion_id"] == result["discussion_id"] for event in deltas)
    assert {event["employee_id"] for event in deltas} == {"dev", "qa"}


def test_sqlite_async_task_writer_does_not_block_discussion_loop(setup, monkeypatch):
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config.settings import get_settings

    _, owner, _, cid, fake = setup
    original = fake.trigger_inputs
    writers = []

    async def trigger_with_task_transaction(agent, sid, inputs):
        result = await original(agent, sid, inputs)
        locked = asyncio.Event()
        engine = create_async_engine(get_settings().sqlalchemy_database_url)

        async def task_write():
            try:
                async with engine.begin() as db:
                    await db.execute(
                        text("UPDATE t_nexus_employees SET description=description WHERE id='dev'")
                    )
                    locked.set()
                    await asyncio.sleep(0.08)
            finally:
                await engine.dispose()

        writers.append(asyncio.create_task(task_write()))
        await locked.wait()
        return result

    monkeypatch.setattr(fake, "trigger_inputs", trigger_with_task_transaction)
    send(owner, cid, "@小牛码 请介绍职责")
    page = wait_done(owner, cid, timeout=25)
    assert page["channel"]["discussion"]["state"] == "completed"


def test_identical_peer_text_cannot_select_another_delivery_reply():
    from nexus_api.main import assistant_reply_messages_after

    messages = [
        {
            "role": "assistant",
            "finished_at": "2026-10-23T01:00:00Z",
            "content": [
                {"type": "hint", "id": "delivery-one", "hint": "同一个请求"},
                {"type": "text", "text": "别人的私聊结论"},
            ],
        },
        {
            "role": "assistant",
            "finished_at": "2026-10-23T01:00:00Z",
            "content": [
                {"type": "hint", "id": "delivery-two", "hint": "同一个请求"},
                {"type": "text", "text": "当前私聊结论"},
            ],
        },
    ]
    replies = assistant_reply_messages_after(messages, "delivery-two")
    assert len(replies) == 1
    assert replies[0]["content"][1]["text"] == "当前私聊结论"


def test_peer_partial_reply_is_not_presented_as_completed():
    from nexus_api.main import assistant_reply_messages_after

    partial = {
        "role": "assistant",
        "content": [
            {"type": "hint", "id": "delivery-partial", "hint": "请求"},
            {"type": "text", "text": "尚未生成完"},
        ],
    }
    assert assistant_reply_messages_after([partial], "delivery-partial") == []


def test_group_send_can_wait_for_an_async_task_commit(setup, monkeypatch):
    import asyncio
    import threading

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config.settings import get_settings

    _, owner, _, cid, fake = setup
    original = fake.trigger_inputs
    locked = threading.Event()
    writers = []

    async def trigger(agent, sid, inputs):
        result = await original(agent, sid, inputs)
        engine = create_async_engine(get_settings().sqlalchemy_database_url)
        ready = asyncio.Event()

        async def write():
            try:
                async with engine.begin() as db:
                    await db.execute(
                        text("UPDATE t_nexus_employees SET description=description WHERE id='dev'")
                    )
                    locked.set()
                    ready.set()
                    await asyncio.sleep(0.3)
            finally:
                await engine.dispose()

        writers.append(asyncio.create_task(write()))
        await ready.wait()
        return result

    monkeypatch.setattr(fake, "trigger_inputs", trigger)
    send(owner, cid, "@小牛码 请介绍职责")
    assert locked.wait(5)
    response = send(owner, cid, "补充一条说明，不要求员工回答")
    assert response.status_code == 200

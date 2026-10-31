import asyncio

import httpx
import pytest
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.task_mcp import NexusIdentityMiddleware, build_mcp_server
from app.task_tools import (
    nexus_channel_id,
    nexus_conversation_id,
    nexus_discussion_id,
    nexus_employee_id,
    nexus_session_id,
)


def test_middleware_carries_context_without_key_or_business_check():
    async def endpoint(_request):
        return JSONResponse(
            {
                "employee": nexus_employee_id.get(),
                "session": nexus_session_id.get(),
                "conversation": nexus_conversation_id.get(),
                "channel": nexus_channel_id.get(),
                "discussion": nexus_discussion_id.get(),
            }
        )

    app = Starlette(routes=[Route("/mcp", endpoint, methods=["POST"])])
    app.add_middleware(NexusIdentityMiddleware)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mcp"
        ) as client:
            discovery = await client.post("/mcp")
            assert discovery.status_code == 200
            assert discovery.json()["employee"] == ""

            accepted = await client.post(
                "/mcp",
                headers={
                    "X-Nexus-Employee-Id": "emp_9",
                    "X-Nexus-Session-Id": "session-9",
                    "X-Nexus-Conversation-Id": "conv-9",
                    "X-Nexus-Channel-Id": "chan-9",
                    "X-Nexus-Discussion-Id": "disc-9",
                },
            )
            assert accepted.status_code == 200
            assert accepted.json() == {
                "employee": "emp_9",
                "session": "session-9",
                "conversation": "conv-9",
                "channel": "chan-9",
                "discussion": "disc-9",
            }
            health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert nexus_employee_id.get() == ""
        assert nexus_session_id.get() == ""

    asyncio.run(scenario())


async def test_tool_call_requires_current_private_session_context():
    from nexus_api.db import connect, utcnow

    now = utcnow()
    with connect() as db:
        db.execute(
            text(
                """INSERT INTO t_nexus_employees
                (id,name,domain,description,instructions,avatar,enabled,knowledge_json,
                 skills_json,tools_json,created_at,updated_at)
                VALUES ('emp_9','员工','测试','','','🤖',1,'[]','[\"task-management\"]','[]',:t,:t)"""
            ),
            {"t": now},
        )
        db.execute(
            text(
                """INSERT INTO t_nexus_conversations
                (id,employee_id,owner_id,name,agentscope_session_id,created_at,updated_at)
                VALUES ('conv-9','emp_9','u9','私聊','session-9',:t,:t)"""
            ),
            {"t": now},
        )
        db.commit()

    server = build_mcp_server()
    employee_token = nexus_employee_id.set("emp_9")
    session_token = nexus_session_id.set("session-9")
    conversation_token = nexus_conversation_id.set("conv-9")
    try:
        accepted = await server.call_tool("nexus_task_get", {"task_id": "missing"})
        assert accepted.structured_content["ok"] is False
        assert accepted.structured_content["error"] == "任务不存在"
    finally:
        nexus_conversation_id.reset(conversation_token)
        nexus_session_id.reset(session_token)
        nexus_employee_id.reset(employee_token)

    employee_token = nexus_employee_id.set("emp_9")
    session_token = nexus_session_id.set("session-other")
    try:
        rejected = await server.call_tool("nexus_task_get", {"task_id": "missing"})
        assert rejected.structured_content["ok"] is False
        assert "业务上下文" in rejected.structured_content["error"]
    finally:
        nexus_session_id.reset(session_token)
        nexus_employee_id.reset(employee_token)


async def test_tool_call_without_context_returns_business_error():
    server = build_mcp_server()
    result = await server.call_tool("nexus_task_get", {"task_id": "missing"})
    assert result.structured_content["ok"] is False
    assert "业务上下文" in result.structured_content["error"]


async def test_group_tool_call_requires_running_discussion_and_uses_initiator_actor():
    from datetime import UTC, datetime, timedelta

    from nexus_api.db import connect, utcnow

    now = utcnow()
    lease = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
    with connect() as db:
        db.execute(
            text(
                """INSERT INTO t_nexus_employees
                (id,name,domain,description,instructions,avatar,enabled,knowledge_json,
                 skills_json,tools_json,created_at,updated_at)
                VALUES ('emp_group','群员工','测试','','','🤖',1,'[]',
                        '[\"task-management\"]','[]',:t,:t)"""
            ),
            {"t": now},
        )
        db.execute(
            text(
                """INSERT INTO t_nexus_channels
                (id,name,description,next_seq,generation,lease_token,lease_until,
                 record_agent_id,record_session_id,created_at,updated_at)
                VALUES ('chan_group','群','',0,1,'lease',:lease,'','',:t,:t)"""
            ),
            {"t": now, "lease": lease},
        )
        db.execute(
            text(
                """INSERT INTO t_nexus_channel_members
                (channel_id,employee_id,member_type,member_id,membership_active,
                 agentscope_session_id)
                VALUES ('chan_group','emp_group','employee','emp_group',1,'group-session')"""
            )
        )
        db.execute(
            text(
                """INSERT INTO t_nexus_channel_members
                (channel_id,employee_id,member_type,member_id,membership_active)
                VALUES ('chan_group','','human','u-owner',1)"""
            )
        )
        db.execute(
            text(
                """INSERT INTO t_nexus_discussions
                (id,channel_id,message_id,initiator_id,generation,state,
                 participants_json,turns,tokens,active_employee_id,detail,created_at)
                VALUES ('disc_group','chan_group','msg_group','u-owner',1,'running',
                        '[]',0,0,'emp_group','',:t)"""
            ),
            {"t": now},
        )
        db.commit()

    server = build_mcp_server()
    tokens = [
        (nexus_employee_id, nexus_employee_id.set("emp_group")),
        (nexus_session_id, nexus_session_id.set("group-session")),
        (nexus_channel_id, nexus_channel_id.set("chan_group")),
        (nexus_discussion_id, nexus_discussion_id.set("disc_group")),
    ]
    try:
        accepted = await server.call_tool("nexus_task_list", {"limit": 20})
        assert accepted.structured_content["ok"] is True
        assert accepted.structured_content["tasks"] == []
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)

    with connect() as db:
        db.execute(
            text("UPDATE t_nexus_discussions SET state='completed' WHERE id='disc_group'")
        )
        db.commit()

    tokens = [
        (nexus_employee_id, nexus_employee_id.set("emp_group")),
        (nexus_session_id, nexus_session_id.set("group-session")),
        (nexus_channel_id, nexus_channel_id.set("chan_group")),
        (nexus_discussion_id, nexus_discussion_id.set("disc_group")),
    ]
    try:
        rejected = await server.call_tool("nexus_task_list", {"limit": 20})
        assert rejected.structured_content["ok"] is False
        assert "群讨论已结束" in rejected.structured_content["error"]
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def test_task_mcp_initializes_and_lists_tools_without_business_context():
    from fastapi.testclient import TestClient

    server = build_mcp_server()
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host="127.0.0.1",
    )
    app.add_middleware(NexusIdentityMiddleware)
    headers = {
        "accept": "application/json, text/event-stream",
        "host": "127.0.0.1:18202",
    }
    with TestClient(app) as client:
        initialized = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "task-test", "version": "1"},
                },
            },
            headers=headers,
        )
        assert initialized.status_code == 200, initialized.text
        assert initialized.json()["result"]["serverInfo"]["name"] == "NEXUS Task"
        tools = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        )
        assert tools.status_code == 200, tools.text
        names = {item["name"] for item in tools.json()["result"]["tools"]}
        assert {"nexus_task_get", "nexus_task_create", "nexus_task_reply"} <= names


async def test_schedule_task_identity_requires_official_policy_and_current_capability(monkeypatch):
    from nexus_api.group_task_auth import authorize_task_source

    calls = []
    state = {"task_allowed": True, "employee_id": "emp-schedule"}

    async def policy(_self, method, url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json=state, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.AsyncClient, "request", policy)
    user = "nexus-schedule-" + "a" * 64
    assert await authorize_task_source("schedule-session", "emp-schedule", "", "", user, "agent") == user
    assert calls[0]["params"]["session_id"] == "schedule-session"
    state["task_allowed"] = False
    with pytest.raises(PermissionError):
        await authorize_task_source("schedule-session", "emp-schedule", "", "", user, "agent")
    state["task_allowed"] = True
    with pytest.raises(PermissionError):
        await authorize_task_source("schedule-session", "other-employee", "", "", user, "agent")
    with pytest.raises(PermissionError):
        await authorize_task_source("schedule-session", "emp-schedule", "forged-channel", "", user, "agent")
    with pytest.raises(PermissionError):
        await authorize_task_source("schedule-session", "emp-schedule", "", "", user, "")
    assert len(calls) == 3

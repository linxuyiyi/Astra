# -*- coding: utf-8 -*-
"""AgentScope HTTP 适配器合同测试。

分两类，均基于真实 HTTP 语义，不用假模型：

1. 明确标记的**单元**测试：用 ``httpx.MockTransport`` 断言请求头、路径、
   参数、错误收敛与 SSE 解析；不冒充真实服务联调。
2. 针对**真实官方 ASGI 服务**（真实 lifespan + SQLite）的合同测试：
   通过适配器完成 Agent/Session CRUD 与默认配置读取。

真实 Uvicorn 进程与真实模型联调见 ``scripts/smoke_runtime.py``。
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus_backend.adapter import AgentScopeHTTPClient, UpstreamError

USER_ID = "nexus-service"


def _mock_adapter(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://agentscope.test",
    )
    return AgentScopeHTTPClient(
        "http://agentscope.test",
        USER_ID,
        client=client,
    ), client


def _json(request: httpx.Request) -> dict:
    return json.loads(request.content.decode() or "{}")


@pytest.mark.asyncio
async def test_every_request_carries_internal_user_header():
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-User-ID"))
        return httpx.Response(200, json={"status": "ok", "version": "2.0.10"})

    adapter, client = _mock_adapter(handler)
    try:
        await adapter.native_health()
        await adapter.list_agents()
    finally:
        await client.aclose()
    assert seen == [USER_ID, USER_ID]


SENTINEL = "sk-sentinel-should-never-be-public-0123456789"


@pytest.mark.asyncio
async def test_non_2xx_becomes_safe_error_without_body_leak():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "detail": f"bad request token={SENTINEL}",
                "sql": f"INSERT INTO t VALUES ('{SENTINEL}')",
            },
        )

    adapter, client = _mock_adapter(handler)
    try:
        with pytest.raises(UpstreamError) as excinfo:
            await adapter.update_agent("a1", name="x")
    finally:
        await client.aclose()
    err = excinfo.value
    assert err.status_code == 400
    assert "400" in err.public_message
    assert "PATCH" in err.public_message
    assert "/agent/a1" in err.public_message
    assert "上游拒绝请求" in err.public_message
    assert SENTINEL not in str(err)
    assert SENTINEL not in err.public_message


@pytest.mark.asyncio
async def test_transport_error_has_no_status_and_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError(f"connection refused token={SENTINEL}")

    adapter, client = _mock_adapter(handler)
    try:
        with pytest.raises(UpstreamError) as excinfo:
            await adapter.trigger_chat("a1", "s1", None)
    finally:
        await client.aclose()
    # 非幂等 POST 在超时/连接错误后不得自动重试。
    assert calls["n"] == 1
    assert excinfo.value.status_code is None
    # 只暴露异常类型，不泄露原始异常消息。
    assert "ConnectError" in excinfo.value.public_message
    assert SENTINEL not in str(excinfo.value)


@pytest.mark.asyncio
async def test_trigger_chat_body_shape_returns_started():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = _json(request)
        return httpx.Response(200, json={"status": "started", "session_id": "s1"})

    adapter, client = _mock_adapter(handler)
    msg = {"id": "m1", "role": "user", "name": "人类", "content": []}
    try:
        result = await adapter.trigger_chat("a1", "s1", msg)
    finally:
        await client.aclose()
    assert captured["path"] == "/chat/"
    assert captured["body"] == {
        "agent_id": "a1",
        "session_id": "s1",
        "input": msg,
    }
    assert result == {"status": "started", "session_id": "s1"}


@pytest.mark.asyncio
async def test_create_agent_and_session_parse_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/" and request.method == "POST":
            assert _json(request)["name"] == "员工A"
            return httpx.Response(201, json={"agent_id": "agent-1"})
        if request.url.path == "/sessions/" and request.method == "POST":
            body = _json(request)
            assert body["agent_id"] == "agent-1"
            return httpx.Response(201, json={"session_id": "sess-1"})
        return httpx.Response(404, json={"detail": "unexpected"})

    adapter, client = _mock_adapter(handler)
    try:
        agent_id = await adapter.create_agent(
            name="员工A",
            system_prompt="你是员工A。",
        )
        session_id = await adapter.create_session(agent_id=agent_id, name="对话")
    finally:
        await client.aclose()
    assert agent_id == "agent-1"
    assert session_id == "sess-1"


@pytest.mark.asyncio
async def test_list_messages_uses_before_cursor_not_offset():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "messages": [],
                "event_cursor": None,
                "is_running": False,
                "has_more": False,
            },
        )

    adapter, client = _mock_adapter(handler)
    try:
        await adapter.list_messages("a1", "s1", before="m-100", limit=25)
    finally:
        await client.aclose()
    assert captured["params"]["before"] == "m-100"
    assert captured["params"]["limit"] == "25"
    assert "offset" not in captured["params"]


@pytest.mark.asyncio
async def test_stream_events_parses_data_frames_and_skips_heartbeat():
    payload = (
        'data: {"type": "REPLY_START", "id": "e1", "reply_id": "r1"}\n\n'
        ":\n\n"
        'data: {"type": "TEXT_BLOCK_DELTA", "id": "e2", "reply_id": "r1", '
        '"delta": "你好"}\n\n'
        'data: {"type": "REPLY_END", "id": "e3", "reply_id": "r1", '
        '"finished_reason": "completed"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sessions/s1/stream"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=payload.encode(),
        )

    adapter, client = _mock_adapter(handler)
    events = []
    try:
        stream = adapter.stream_events("a1", "s1")
        async for event in stream:
            events.append(event)
            if event.get("type") == "REPLY_END":
                break
        await stream.aclose()
    finally:
        await client.aclose()
    assert [e["type"] for e in events] == [
        "REPLY_START",
        "TEXT_BLOCK_DELTA",
        "REPLY_END",
    ]
    assert events[1]["delta"] == "你好"


@pytest.mark.asyncio
async def test_stream_non_200_becomes_safe_error_without_body_leak():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=f"upstream {SENTINEL}".encode())

    adapter, client = _mock_adapter(handler)
    try:
        stream = adapter.stream_events("a1", "s1")
        with pytest.raises(UpstreamError) as excinfo:
            async for _ in stream:
                pass
    finally:
        await client.aclose()
    assert excinfo.value.status_code == 503
    assert "GET" in excinfo.value.public_message
    assert SENTINEL not in str(excinfo.value)


@pytest.mark.asyncio
async def test_stream_rejects_malformed_json_frame():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"data: {not-json token=secret\n\n",
        )

    adapter, client = _mock_adapter(handler)
    try:
        with pytest.raises(UpstreamError) as excinfo:
            async for _ in adapter.stream_events("a1", "s1"):
                pass
    finally:
        await client.aclose()
    assert "非法 JSON" in excinfo.value.public_message


@pytest.mark.asyncio
async def test_stream_rejects_non_object_event():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: ["not", "an", "object"]\n\n',
        )

    adapter, client = _mock_adapter(handler)
    try:
        with pytest.raises(UpstreamError) as excinfo:
            async for _ in adapter.stream_events("a1", "s1"):
                pass
    finally:
        await client.aclose()
    assert "不是 JSON 对象" in excinfo.value.public_message


class _ReadErrorStream(httpx.AsyncByteStream):
    """读取阶段抛 ReadError 的事件流，用于验证错误映射与关闭。"""

    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        yield b'data: {"type": "REPLY_START", "id": "e1"}\n\n'
        raise httpx.ReadError(f"boom token={SENTINEL}")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_stream_read_error_maps_to_upstream_error_and_closes():
    broken = _ReadErrorStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=broken,
        )

    adapter, client = _mock_adapter(handler)
    received: list[dict] = []
    try:
        with pytest.raises(UpstreamError) as excinfo:
            async for event in adapter.stream_events("a1", "s1"):
                received.append(event)
    finally:
        await client.aclose()
    assert received == [{"type": "REPLY_START", "id": "e1"}]
    assert excinfo.value.status_code is None
    assert "ReadError" in excinfo.value.public_message
    assert SENTINEL not in str(excinfo.value)
    assert broken.closed is True


@pytest.mark.asyncio
async def test_delete_agent_ignore_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "gone"})

    adapter, client = _mock_adapter(handler)
    try:
        assert await adapter.delete_agent("a1", ignore_missing=True) is False
        with pytest.raises(UpstreamError):
            await adapter.delete_agent("a1", ignore_missing=False)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_probe_collects_status_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "not_ready"})

    adapter, client = _mock_adapter(handler)
    try:
        probe = await adapter.probe_native_health()
    finally:
        await client.aclose()
    assert probe.reachable is True
    assert probe.http_status == 503
    assert probe.body["status"] == "not_ready"


@pytest.mark.asyncio
async def test_interrupt_session_uses_202_and_agent_query():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(202, json={"session_id": "s1"})

    adapter, client = _mock_adapter(handler)
    try:
        result = await adapter.interrupt_session("a1", "s1")
    finally:
        await client.aclose()
    assert captured["path"] == "/sessions/s1/interrupt"
    assert captured["params"] == {"agent_id": "a1"}
    assert result == {"session_id": "s1"}


@pytest.mark.asyncio
async def test_iter_all_messages_uses_before_cursor_pagination():
    pages = {
        None: {
            "messages": [{"id": "m3"}, {"id": "m4"}],
            "has_more": True,
            "is_running": False,
            "event_cursor": None,
        },
        "m3": {
            "messages": [{"id": "m1"}, {"id": "m2"}],
            "has_more": False,
            "is_running": False,
            "event_cursor": None,
        },
    }
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        before = request.url.params.get("before")
        seen.append(before)
        return httpx.Response(200, json=pages[before])

    adapter, client = _mock_adapter(handler)
    try:
        messages = await adapter.iter_all_messages("a1", "s1", page_size=2)
    finally:
        await client.aclose()
    assert [m["id"] for m in messages] == ["m1", "m2", "m3", "m4"]
    assert seen == [None, "m3"]


@pytest.mark.asyncio
async def test_iter_all_messages_dedupes_replayed_page():
    pages = {
        None: {
            "messages": [{"id": "m3"}, {"id": "m4"}],
            "has_more": True,
            "event_cursor": None,
        },
        "m3": {
            "messages": [{"id": "m1"}, {"id": "m3"}],
            "has_more": False,
            "event_cursor": None,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("before")])

    adapter, client = _mock_adapter(handler)
    try:
        messages = await adapter.iter_all_messages("a1", "s1", page_size=2)
    finally:
        await client.aclose()
    # 跨页重复的 m3 只保留一次，不重复加入。
    assert [m["id"] for m in messages] == ["m1", "m3", "m4"]


@pytest.mark.asyncio
async def test_iter_all_messages_fails_when_has_more_but_page_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"messages": [], "has_more": True, "event_cursor": None},
        )

    adapter, client = _mock_adapter(handler)
    try:
        with pytest.raises(UpstreamError) as excinfo:
            await adapter.iter_all_messages("a1", "s1", page_size=2)
    finally:
        await client.aclose()
    assert "历史可能不完整" in excinfo.value.public_message


@pytest.mark.asyncio
async def test_iter_all_messages_fails_on_stuck_cursor():
    pages = {
        None: {"messages": [{"id": "m3"}], "has_more": True},
        "m3": {"messages": [{"id": "m3"}, {"id": "m2"}], "has_more": True},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("before")])

    adapter, client = _mock_adapter(handler)
    try:
        with pytest.raises(UpstreamError) as excinfo:
            await adapter.iter_all_messages("a1", "s1", page_size=1)
    finally:
        await client.aclose()
    assert "游标未推进" in excinfo.value.public_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    [
        "not-an-object",
        {"messages": "not-a-list", "has_more": False},
        {"messages": [], "has_more": "yes"},
        {"messages": [{"missing_id": True}], "has_more": False},
        {"messages": [{"id": ""}], "has_more": False},
        {"messages": [{"id": "dup"}, {"id": "dup"}], "has_more": False},
    ],
)
async def test_iter_all_messages_fails_on_invalid_shape(page):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page)

    adapter, client = _mock_adapter(handler)
    try:
        with pytest.raises(UpstreamError) as excinfo:
            await adapter.iter_all_messages("a1", "s1", page_size=2)
    finally:
        await client.aclose()
    assert "消息分页契约无效" in excinfo.value.public_message


# ----------------------------------------------------------------------
# 针对真实官方 ASGI 服务的合同测试（真实 lifespan + SQLite，非 mock）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_adapter_against_real_agentscope_asgi(tmp_path):
    from nexus_agent_service.app import build_app as build_agentscope_app
    from nexus_agent_service.config import (
        load_config as load_agentscope_config,
    )

    config = load_agentscope_config(
        env={
            "AGENTSCOPE_DATA_DIR": str(tmp_path / "agentscope"),
            "AGENTSCOPE_INTERNAL_USER_ID": USER_ID,
        },
    )
    upstream = build_agentscope_app(config)
    transport = httpx.ASGITransport(app=upstream)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://agentscope.test",
    )
    adapter = AgentScopeHTTPClient(
        "http://agentscope.test",
        USER_ID,
        client=client,
    )
    async with upstream.router.lifespan_context(upstream):
        try:
            native = await adapter.native_health()
            assert native["version"] == "2.0.10"
            assert native["status"] == "ok"

            defaults = await adapter.service_defaults()
            assert defaults["model_configured"] is False

            agent_id = await adapter.create_agent(
                name="契约测试员工",
                system_prompt="你是契约测试员工。",
            )
            listing = await adapter.list_agents()
            matched = next(
                a for a in listing["agents"] if a["id"] == agent_id
            )
            assert matched["data"]["id"] != matched["id"]

            session_id = await adapter.create_session(
                agent_id=agent_id,
                name="契约测试会话",
            )
            second_session = await adapter.create_session(
                agent_id=agent_id,
                name="契约测试会话2",
            )
            views = await adapter.list_sessions(agent_id)
            by_id = {v["session"]["id"]: v for v in views["sessions"]}
            assert session_id in by_id and second_session in by_id
            ws_first = by_id[session_id]["session"]["config"]["workspace_id"]
            ws_second = by_id[second_session]["session"]["config"][
                "workspace_id"
            ]
            assert ws_first != ws_second

            status = await adapter.get_session_status(agent_id, session_id)
            assert status["session_id"] == session_id
            assert status["status"] == "idle"

            history = await adapter.list_messages(agent_id, session_id)
            assert history["messages"] == []
            assert history["is_running"] is False
            # 真实空会话也满足严格分页契约（has_more/messages 形状有效）。
            assert (
                await adapter.iter_all_messages(agent_id, session_id)
                == []
            )

            assert await adapter.delete_session(agent_id, session_id) is True
            assert (
                await adapter.delete_session(agent_id, second_session) is True
            )
            assert await adapter.delete_agent(agent_id) is True
        finally:
            await client.aclose()

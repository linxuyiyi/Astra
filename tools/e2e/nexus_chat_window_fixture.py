"""聊天窗口受控上游 E2E：真实 Nexus API/SQLite/SSE，AgentScope 协议用内存夹具。

仅允许 NEXUS_CHAT_E2E=1 和临时 SQLite 库。输出均带 [受控上游验收]，不调用模型或 Worker。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/nexus-api"))


def now():
    return datetime.now(UTC).isoformat()


class FixtureStream(httpx.AsyncByteStream):
    def __init__(self, upstream, session, after):
        self.upstream, self.session, self.after = upstream, session, after

    async def __aiter__(self):
        cursor = self.after
        self.upstream.streams += 1
        try:
            while True:
                for event in self.session["events"]:
                    index = int(event["id"].split("-")[0])
                    if index > cursor:
                        cursor = index
                        yield f"id: {event['id']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
                await asyncio.sleep(0.1)
                yield b": heartbeat\n\n"
        finally:
            self.upstream.streams -= 1


class ScriptedAgentScope:
    def __init__(self):
        self.sessions = {}
        self.tasks = set()
        self.attempts = {}
        self.streams = 0
        self.calls = []

    def session(self, sid):
        return self.sessions.setdefault(
            sid,
            {
                "messages": [],
                "events": [],
                "running": False,
                "task": None,
                "text": "",
                "stop_failed": False,
            },
        )

    def event(self, session, kind, **body):
        event = {
            "id": f"{len(session['events']) + 1}-0",
            "type": kind,
            "created_at": now(),
            **body,
        }
        session["events"].append(event)
        return event

    async def produce(self, session, agent, text):
        reply = {
            "id": f"fixture-reply-{uuid.uuid4().hex[:12]}",
            "name": {
                "office-agent-0": "小牛马",
                "office-agent-1": "小鲁班",
                "office-agent-2": "小蒜鸟",
            }.get(agent, "验收员工"),
            "role": "assistant",
            "content": [],
            "metadata": {},
            "created_at": now(),
            "finished_at": None,
        }
        session["messages"].append(reply)
        base = {"reply_id": reply["id"], "session_id": session["id"]}
        self.event(session, "REPLY_START", name=reply["name"], role="assistant", **base)
        thought = {
            "type": "thinking",
            "id": "thought",
            "thinking": "这是受控协议夹具，不代表真实模型推理。",
            "created_at": now(),
            "finished_at": now(),
        }
        reply["content"].append(thought)
        self.event(session, "THINKING_BLOCK_START", block_id="thought", **base)
        self.event(
            session,
            "THINKING_BLOCK_DELTA",
            block_id="thought",
            delta=thought["thinking"],
            **base,
        )
        self.event(session, "THINKING_BLOCK_END", block_id="thought", **base)
        tool_id = "fixture-tool"
        tool = {
            "type": "tool_call",
            "id": tool_id,
            "name": "FixtureRead",
            "input": '{"path":"fixture.txt"}',
            "state": "allowed",
            "created_at": now(),
        }
        reply["content"].append(tool)
        self.event(
            session,
            "TOOL_CALL_START",
            tool_call_id=tool_id,
            tool_call_name="FixtureRead",
            **base,
        )
        self.event(
            session,
            "TOOL_CALL_DELTA",
            tool_call_id=tool_id,
            delta=tool["input"],
            **base,
        )
        self.event(session, "TOOL_CALL_END", tool_call_id=tool_id, **base)
        interrupted = False
        try:
            if "确认卡" in text:
                tool["state"] = "asking"
                self.event(
                    session,
                    "REQUIRE_USER_CONFIRM",
                    tool_calls=[copy.deepcopy(tool)],
                    **base,
                )
                await asyncio.sleep(60)
            result = {
                "type": "tool_result",
                "id": tool_id,
                "name": "FixtureRead",
                "output": "[fixture] permission denied"
                if "工具失败" in text
                else "[fixture] controlled result",
                "state": "error" if "工具失败" in text else "success",
                "created_at": now(),
                "finished_at": now(),
            }
            reply["content"].append(result)
            self.event(
                session,
                "TOOL_RESULT_START",
                tool_call_id=tool_id,
                tool_call_name="FixtureRead",
                **base,
            )
            self.event(
                session,
                "TOOL_RESULT_TEXT_DELTA",
                tool_call_id=tool_id,
                delta=result["output"],
                **base,
            )
            self.event(
                session,
                "TOOL_RESULT_END",
                tool_call_id=tool_id,
                state=result["state"],
                **base,
            )
            tool["state"] = "finished"
            output = (
                "[受控上游验收] 已收到："
                + text
                + "\n\n**Markdown 验证**\n\n```python\nprint('e2e')\n```\n\n| 项目 | 结果 |\n| --- | --- |\n| 窗口 | 正常 |"
            )
            block = {"type": "text", "id": "body", "text": "", "created_at": now()}
            reply["content"].append(block)
            self.event(session, "TEXT_BLOCK_START", block_id="body", **base)
            for index in range(0, len(output), 12):
                chunk = output[index : index + 12]
                block["text"] += chunk
                self.event(
                    session, "TEXT_BLOCK_DELTA", block_id="body", delta=chunk, **base
                )
                if "快速回复" not in text:
                    await asyncio.sleep(0.6 if "慢回复" in text else 0.07)
            self.event(session, "TEXT_BLOCK_END", block_id="body", **base)
        except asyncio.CancelledError:
            interrupted = True
            tool["state"] = "finished"
        finally:
            reply["usage"] = {"input_tokens": 120, "output_tokens": 45}
            self.event(
                session, "MODEL_CALL_END", input_tokens=120, output_tokens=45, **base
            )
            reply["finished_at"] = now()
            reply["finished_reason"] = "interrupted" if interrupted else "completed"
            self.event(
                session, "REPLY_END", finished_reason=reply["finished_reason"], **base
            )
            session["running"] = False

    async def handle(self, request):
        path, method = request.url.path, request.method
        self.calls.append({"method": method, "path": path})
        body = (
            json.loads(request.content)
            if request.content
            and request.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        if path == "/health":
            return httpx.Response(200, json={"version": "2.0.10", "fixture": True})
        if path == "/sessions/" and method == "POST":
            sid = f"fixture-session-{uuid.uuid4().hex[:10]}"
            self.session(sid)["id"] = sid
            return httpx.Response(201, json={"session_id": sid})
        if path == "/chat/" and method == "POST":
            sid = body["session_id"]
            session = self.session(sid)
            session["id"] = sid
            inputs = (
                body["input"] if isinstance(body["input"], list) else [body["input"]]
            )
            user = copy.deepcopy(
                next(
                    (
                        item
                        for item in reversed(inputs)
                        if "display_text" in item.get("metadata", {})
                        and item.get("metadata", {}).get("sender_type", "human")
                        == "human"
                    ),
                    inputs[-1],
                )
            )
            text = str(
                user.get("metadata", {}).get("display_text")
                or "".join(
                    block.get("text", "")
                    for block in user.get("content", [])
                    if block.get("type") == "text"
                )
            )
            key = (sid, text)
            self.attempts[key] = self.attempts.get(key, 0) + 1
            if "失败一次" in text and self.attempts[key] == 1:
                return httpx.Response(
                    503, json={"detail": "[fixture] unavailable once"}
                )
            if session["running"]:
                return httpx.Response(409, json={"detail": "会话正在执行"})
            user.setdefault("id", f"fixture-user-{uuid.uuid4().hex[:10]}")
            user.update(created_at=now(), finished_at=now())
            user.setdefault("metadata", {})
            session["messages"].append(user)
            session["running"], session["text"] = True, text
            session["stop_failed"] = False
            if "快速回复" in text:
                await self.produce(session, body["agent_id"], text)
            else:
                task = asyncio.create_task(
                    self.produce(session, body["agent_id"], text)
                )
                session["task"] = task
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            return httpx.Response(200, json={"status": "started", "session_id": sid})
        if path.startswith("/sessions/"):
            sid = path.split("/")[2]
            session = self.session(sid)
            session["id"] = sid
            if path.endswith("/messages"):
                return httpx.Response(
                    200,
                    json={
                        "messages": session["messages"],
                        "event_cursor": f"{len(session['events'])}-0",
                        "is_running": session["running"],
                        "has_more": False,
                    },
                )
            if path.endswith("/stream"):
                after = int(request.url.params.get("after", "0-0").split("-")[0])
                return httpx.Response(
                    200,
                    stream=FixtureStream(self, session, after),
                    headers={"content-type": "text/event-stream"},
                )
            if path.endswith("/interrupt"):
                if "停止失败" in session["text"] and not session["stop_failed"]:
                    session["stop_failed"] = True
                    return httpx.Response(
                        503, json={"detail": "[fixture] stop unavailable once"}
                    )
                if session["task"] and not session["task"].done():
                    session["task"].cancel()
                    await asyncio.gather(session["task"], return_exceptions=True)
                return httpx.Response(202, json={"status": "accepted"})
            return httpx.Response(200, json={"status": "idle"})
        if path in {"/skill", "/mcp", "/workspace/skill", "/workspace/mcp", "/tools"}:
            return httpx.Response(200, json=[])
        if path == "/knowledge_bases/":
            return httpx.Response(200, json={"knowledge_bases": [], "total": 0})
        if path.startswith("/agent/") and method == "GET":
            return httpx.Response(
                200,
                json={"name": "验收员工", "tools": [], "skills": [], "knowledge": []},
            )
        return httpx.Response(200, json={"ok": True})


def create_preview_app():
    if os.environ.get("NEXUS_CHAT_E2E") != "1":
        raise RuntimeError("该工厂仅用于显式启用的受控验收")
    directory = Path(os.environ["ASTRA_DATA_DIR"]).resolve()
    temporary_roots = [Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()]
    if (
        not any(directory.is_relative_to(root) for root in temporary_roots)
        or "chat-window-e2e" not in directory.name
    ):
        raise RuntimeError("验收只允许专用临时目录")
    from nexus_api import collaboration, main, model_route
    from nexus_api.agentscope import AgentScopeClient
    from nexus_pixel_office_fixture import create_preview_app as create_base

    async def fixture_config(*_args, **_kwargs):
        return {
            "type": "openai_credential",
            "credential_id": "controlled-fixture",
            "model": "controlled-fixture",
            "parameters": {},
        }

    main.chat_config_for_route = fixture_config
    model_route.chat_config_for_route = fixture_config
    collaboration.chat_config_for_route = fixture_config
    app = create_base()
    original = app.router.lifespan_context
    upstream = ScriptedAgentScope()

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            from nexus_api.db import connect
            from sqlalchemy import text

            with connect() as database:
                database.execute(
                    text("UPDATE t_nexus_employees SET model_route_id='fixture-route'")
                )
                database.commit()
            await application.state.agentscope.aclose()
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(upstream.handle),
                base_url="http://controlled-fixture.invalid",
            )
            application.state.agentscope = AgentScopeClient(
                "http://controlled-fixture.invalid", "nexus", client
            )
            yield
            for task in list(upstream.tasks):
                task.cancel()
            await asyncio.gather(*upstream.tasks, return_exceptions=True)

    app.router.lifespan_context = lifespan
    app.state.chat_fixture = upstream

    @app.get("/__chat_e2e__/state")
    async def fixture_state():
        return {
            "fixture": True,
            "active_streams": upstream.streams,
            "running_sessions": [
                sid for sid, session in upstream.sessions.items() if session["running"]
            ],
            "pending_tasks": len(upstream.tasks),
        }

    # 原服务末尾挂载静态 SPA；测试诊断路由必须排在该 catch-all 前。
    app.router.routes.insert(0, app.router.routes.pop())
    return app

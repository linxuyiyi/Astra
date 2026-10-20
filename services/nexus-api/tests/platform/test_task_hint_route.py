"""HttpWakeSender 打到 agent_service 注册的真实 FastAPI 路由。"""

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "services" / "agent-service"))

from agent_service import register_task_hint, reset_event_claims  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.domain import TaskStatus  # noqa: E402
from app.models import Base, TaskWakeOutbox  # noqa: E402
from app.runtime.task_wakeup import HttpWakeSender, deliver_pending  # noqa: E402
from app.services import TaskService  # noqa: E402
from tests.platform.test_runtime import new_task  # noqa: E402


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


@pytest.fixture
def wake_bases(monkeypatch):
    monkeypatch.setenv("ASTRA_NEXUS_API_BASE", "http://wake.test")
    monkeypatch.setenv("ASTRA_AGENTSCOPE_BASE_URL", "http://wake.test")
    get_settings.cache_clear()
    reset_event_claims()
    yield
    get_settings.cache_clear()
    reset_event_claims()


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, app: FastAPI):
        self.inner = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        self.event_ids: list[object] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nexus/task-hint":
            self.event_ids.append(json.loads(request.content or b"{}").get("event_id"))
        return await self.inner.handle_async_request(request)


def _app(deliver, wake):
    app = FastAPI()
    register_task_hint(app, deliver)

    @app.get("/api/v1/internal/wake-target")
    async def wake_target(employee_id: str, session_id: str = ""):
        del employee_id, session_id
        result = wake()
        if isinstance(result, HTTPException):
            raise result
        return result

    return app


def _client(app):
    return httpx.AsyncClient(transport=RecordingTransport(app), base_url="http://wake.test")


async def _failed_task(session):
    task = await TaskService(session).create(
        new_task(responsible_employee_id="emp_1", source_session_id="ses_1")
    )
    await TaskService(session).transition(task, TaskStatus.FAILED)
    return await session.scalar(select(TaskWakeOutbox))


async def test_real_sender_reaches_inbox_and_accepts_integer_event_id(session, wake_bases):
    del wake_bases
    inbox: list[str] = []

    async def deliver(user_id, agent_id, session_id, hint):
        assert user_id == "nexus"
        assert agent_id == "agt_1"
        assert session_id == "ses_1"
        inbox.append(hint)

    app = _app(deliver, lambda: {"session_id": "ses_1", "agent_id": "agt_1"})
    row = await _failed_task(session)
    async with _client(app) as client:
        schema = await client.get("/openapi.json")
        assert schema.status_code == 200, schema.text
        parameters = schema.json()["paths"]["/nexus/task-hint"]["post"].get("parameters") or []
        assert all(item.get("name") != "request" for item in parameters)
        direct = await client.post(
            "/nexus/task-hint",
            json={"session_id": "ses_1", "agent_id": "agt_1", "hint": "整数事件", "event_id": 41},
        )
        assert direct.status_code == 200, direct.text
        await HttpWakeSender(client).send(row)
        await HttpWakeSender(client).send(row)
        delivered = await deliver_pending(session, HttpWakeSender(client))
        captured = client._transport.event_ids
    assert captured[0] == 41
    assert captured[1] == str(row.event_id)
    assert isinstance(captured[1], str)
    assert inbox == ["整数事件", row.hint]
    assert delivered == 1
    current = await session.scalar(select(TaskWakeOutbox))
    assert current.status == "delivered"


async def test_contract_422_fails_the_outbox_once(session, wake_bases):
    del wake_bases
    inbox: list[str] = []

    async def deliver(_user_id, _agent_id, _session_id, hint):
        inbox.append(hint)

    app = _app(deliver, lambda: {"session_id": "ses_1", "agent_id": " "})
    await _failed_task(session)
    async with _client(app) as client:
        assert await deliver_pending(session, HttpWakeSender(client)) == 0
    row = await session.scalar(select(TaskWakeOutbox))
    assert row.status == "failed"
    assert row.attempts == 1
    assert inbox == []
    async with _client(app) as client:
        assert await deliver_pending(session, HttpWakeSender(client)) == 0
    assert (await session.scalar(select(TaskWakeOutbox))).status == "failed"


async def test_disabled_employee_retries_after_reenable(session, wake_bases):
    del wake_bases
    inbox: list[str] = []
    enabled = {"value": False}

    async def deliver(_user_id, _agent_id, _session_id, hint):
        inbox.append(hint)

    def wake():
        if not enabled["value"]:
            return HTTPException(status_code=409, detail="员工已禁用，稍后再投递")
        return {"session_id": "ses_1", "agent_id": "agt_1"}

    app = _app(deliver, wake)
    await _failed_task(session)
    async with _client(app) as client:
        assert await deliver_pending(session, HttpWakeSender(client)) == 0
        row = await session.scalar(select(TaskWakeOutbox))
        assert row.status == "pending"
        assert row.attempts == 1
        enabled["value"] = True
        assert await deliver_pending(session, HttpWakeSender(client), now=row.next_retry_at) == 1
    assert inbox
    assert (await session.scalar(select(TaskWakeOutbox))).status == "delivered"


async def test_inflight_duplicate_is_retryable_until_inbox_accepts(session, wake_bases):
    del wake_bases
    import asyncio

    inbox: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def deliver(_user_id, _agent_id, _session_id, hint):
        if hint == "held":
            started.set()
            await release.wait()
            raise RuntimeError("inbox rejected")
        inbox.append(hint)

    app = _app(deliver, lambda: {"session_id": "ses_1", "agent_id": "agt_1"})
    row = await _failed_task(session)
    async with _client(app) as client:
        pending = asyncio.create_task(
            client.post(
                "/nexus/task-hint",
                headers={"X-Nexus-Event-Id": str(row.event_id)},
                json={
                    "session_id": "ses_1",
                    "agent_id": "agt_1",
                    "hint": "held",
                    "event_id": row.event_id,
                },
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        assert await deliver_pending(session, HttpWakeSender(client)) == 0
        waiting = await session.scalar(select(TaskWakeOutbox))
        assert waiting.status == "pending"
        assert waiting.attempts == 1
        assert inbox == []
        release.set()
        failed = await pending
        assert failed.status_code == 500, failed.text
        assert await deliver_pending(session, HttpWakeSender(client), now=waiting.next_retry_at) == 1
    assert inbox == [row.hint]
    assert (await session.scalar(select(TaskWakeOutbox))).status == "delivered"

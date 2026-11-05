"""Fixes from the phase-2 local verification and code review."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.api.workspace_routes import native_events
from app.database import SessionLocal
from app.main import app
from app.models import WorkerCommand
from app.worker_agent.connection import (
    AgentConfig,
    WorkerConnectionManager,
    _bounded_event_frame,
)
from app.worker_agent.local_opencode import bind_local_opencode
from app.worker_agent.process_env import child_environ
from app.worker_agent.workspace import prepare_workspace_local
from app.worker_gateway.events import EventLog
from app.worker_gateway.protocol import WorkerMessage, envelope
from app.worker_gateway.service import (
    WorkerGateway,
    expire_due_commands_statement,
    held_slot_expire_statement,
)
from tests.platform.test_worker_gateway import ADMIN_HEADERS, gateway_for


async def _complete(gateway: WorkerGateway, frame: dict, epoch: int, result: dict | None = None) -> None:
    reply = envelope(
        request_id=frame["request_id"],
        agent_worker_id=frame["agent_worker_id"],
        session_id=frame.get("session_id"),
        message_type="response",
        payload={"stage": "result", "ok": True, "result": result or {"ok": True}, "error_code": None},
    )
    reply.epoch = epoch
    async with SessionLocal() as session:
        await gateway.apply_response(
            session,
            agent_worker_id=frame["agent_worker_id"],
            epoch=epoch,
            message=reply,
        )
    await gateway.bus.publish_response(frame["request_id"], reply.to_dict())


def test_resume_wakes_a_paused_event_reader():
    manager = WorkerConnectionManager(
        AgentConfig(
            gateway_url="ws://nexus.internal/api/worker-gateway/connect",
            agent_worker_id="wrk",
            token="wtk_test",
        )
    )
    manager.paused_directories.add("/srv/app")
    manager.resume_waiters["/srv/app"] = asyncio.Event()
    manager._note_event_control("resume_events", {"directory": "/srv/app"})
    assert "/srv/app" not in manager.paused_directories
    assert manager.resume_waiters["/srv/app"].is_set()


def test_child_environ_drops_worker_credentials_and_preserves_managed_config():
    source = {"NEXUS_WORKER_TOKEN": "canary", "OPENCODE_CONFIG": "/managed/config.jsonc", "PATH": "/usr/bin"}
    result = child_environ(source)
    assert "NEXUS_WORKER_TOKEN" not in result
    assert result["OPENCODE_CONFIG"] == source["OPENCODE_CONFIG"]


def test_oversized_event_frame_becomes_a_gap_marker():
    incoming = WorkerMessage(
        request_id="evt",
        agent_worker_id="wrk",
        session_id=None,
        type="event",
        payload={},
    )
    config = AgentConfig(
        gateway_url="ws://nexus",
        agent_worker_id="wrk",
        token="wtk_test",
        max_frame_bytes=180,
    )
    frame = _bounded_event_frame(
        incoming,
        config,
        directory="/srv/app",
        seq=3,
        event={"type": "file.edited", "properties": {"text": "x" * 400}},
        epoch=1,
        expires_at=10,
    )
    assert frame["payload"]["dropped"] is True
    assert frame["payload"]["reason"] == "event_too_large"
    assert "event" not in frame["payload"]


def test_prepare_workspace_stays_under_the_configured_root_and_rejects_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "opencode.json").write_text("{}\n")
    outside = tmp_path / "elsewhere"
    with pytest.raises(RuntimeError, match="不在允许范围内"):
        prepare_workspace_local(str(root), str(outside), allowed_root=str(root))
    link = root / "opencode.jsonc"
    link.symlink_to(root / "opencode.json")
    inside = root / "task"
    with pytest.raises(RuntimeError, match="符号链接"):
        prepare_workspace_local(str(root), str(inside), allowed_root=str(root))
    allowed = root.parent / ".astra-tasks" / "emp" / "task"
    link.unlink()
    created = prepare_workspace_local(str(root), str(allowed), allowed_root=str(root))
    assert created["created"] == "true"
    assert (allowed / "opencode.json").is_file()


async def test_admin_command_route_rejects_restart_opencode():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/worker-gateway/workers/wrk_missing/commands",
            headers=ADMIN_HEADERS,
            json={"request_id": "cmd-restart", "name": "restart_opencode", "payload": {}},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "maintenance_command"


@pytest.mark.asyncio
async def test_local_opencode_call_has_an_overall_timeout(monkeypatch):
    class Hang:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, *_args, **_kwargs):
            await asyncio.sleep(5)
            return None

    monkeypatch.setattr(httpx, "AsyncClient", Hang)
    manager = SimpleNamespace(
        config=SimpleNamespace(
            gateway_url="http://127.0.0.1:9",
            token="wtk_test",
            opencode_url="http://127.0.0.1:9",
        )
    )
    bind_local_opencode(manager, timeout=0.05)
    with pytest.raises(TimeoutError):
        await manager.local_request("GET", "/global/health", {"query": {}, "body": {}})


def test_mysql_dialect_compiles_sweep_without_returning():
    from sqlalchemy.dialects import mysql

    now = datetime.now(UTC)
    due = (
        WorkerCommand.status.in_(("pending", "accepted")),
        WorkerCommand.deadline_at.is_not(None),
        WorkerCommand.deadline_at <= now,
    )
    values = {
        "status": "expired",
        "error_code": "timeout",
        "finished_at": now,
        "updated_at": now,
        "slot_held": False,
    }
    dialect = mysql.dialect()
    statements = (
        held_slot_expire_statement(1, due, values),
        expire_due_commands_statement(due, values),
    )
    for statement in statements:
        sql = str(statement.compile(dialect=dialect))
        assert "RETURNING" not in sql.upper()
    root = Path(__file__).resolve().parents[2] / "app"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if ".returning(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.skipif(
    not os.environ.get("ASTRA_TEST_MYSQL_URL"),
    reason="未提供 ASTRA_TEST_MYSQL_URL，只编译 MySQL 方言",
)
def test_sweep_statements_execute_on_mysql():
    from sqlalchemy import create_engine

    engine = create_engine(os.environ["ASTRA_TEST_MYSQL_URL"])
    now = datetime.now(UTC).replace(tzinfo=None)
    due = (
        WorkerCommand.status.in_(("pending", "accepted")),
        WorkerCommand.deadline_at.is_not(None),
        WorkerCommand.deadline_at <= now,
    )
    values = {
        "status": "expired",
        "error_code": "timeout",
        "finished_at": now,
        "updated_at": now,
        "slot_held": False,
    }
    with engine.begin() as connection:
        for statement in (
            held_slot_expire_statement(-1, due, values),
            expire_due_commands_statement(due, values),
        ):
            compiled = statement.compile(dialect=connection.dialect)
            assert "RETURNING" not in str(compiled).upper()
            connection.execute(statement)


@pytest.mark.asyncio
async def test_native_event_keepalive_is_an_sse_comment(monkeypatch):
    class Client:
        records_fetch = True

        async def events(self, directory, *, consumer):
            yield {"type": "astra.keepalive", "directory": str(directory)}
            yield {
                "type": "astra.event_gap",
                "directory": str(directory),
                "reason": "worker_reconnected",
                "from": 0,
                "to": None,
            }

        async def children(self, *_args, **_kwargs):
            return []

    @asynccontextmanager
    async def fake_connect(*_args, **_kwargs):
        yield Client()

    monkeypatch.setattr("app.api.workspace_routes.connect", fake_connect)
    chunks = [chunk async for chunk in native_events(SimpleNamespace(), "/srv/app", "ses", False)]
    assert chunks[0].startswith(": keepalive")
    assert "worker_reconnected" in chunks[1]


@pytest.mark.asyncio
async def test_duplicate_http_request_does_not_start_another_task():
    from app.worker_gateway.protocol import parse_message

    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def local_request(_method, _path, _body):
        calls["n"] += 1
        started.set()
        await release.wait()
        return {"status": 204, "body": ""}

    manager = WorkerConnectionManager(
        AgentConfig(
            gateway_url="ws://127.0.0.1/api/worker-gateway/connect",
            agent_worker_id="wrk",
            token="wtk_test",
        )
    )
    manager.epoch = 1
    manager.clock = lambda: 0
    manager.fetch_body = lambda _request_id, _epoch: _ready({"body": "{}"})
    manager.local_request = local_request
    manager.upload_body = lambda *_args: _ready(None)

    class Transport:
        def __init__(self):
            self.sent = []

        async def send(self, message):
            self.sent.append(message)

        async def recv(self):
            raise ConnectionError("closed")

        async def close(self):
            return None

    message = envelope(
        request_id="req-http",
        agent_worker_id="wrk",
        session_id=None,
        message_type="command",
        payload={"name": "http", "data": {"method": "POST", "path": "/x", "ready": True}},
    )
    message.epoch = 1
    message.expires_at = 10**12
    incoming = parse_message(json.dumps(message.to_dict()), max_bytes=10**6)
    transport = Transport()
    manager._start_http(transport, incoming, 1)
    await asyncio.wait_for(started.wait(), 1)
    manager._start_http(transport, incoming, 1)
    assert calls["n"] == 1
    assert len(manager.http_tasks) == 1
    release.set()
    await asyncio.gather(*list(manager.http_tasks.values()))


async def _ready(value):
    return value


def test_prepare_workspace_rejects_a_symlink_opencode_directory(tmp_path):
    root = tmp_path / "root"
    (root / ".opencode").mkdir(parents=True)
    (root / ".opencode" / "opencode.json").write_text("{}\n")
    target = tmp_path / "task"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / ".opencode").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="符号链接"):
        prepare_workspace_local(str(root), str(target))
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_trimmed_event_log_keeps_a_gap_for_a_lagging_cursor():
    gateway = gateway_for()
    log = EventLog(gateway.bus, limit=2)
    await log.touch_cursor(worker_id="wrk", epoch=1, directory="/work", consumer="lag", seq=1)
    for seq in (1, 2, 3):
        await log.append(
            worker_id="wrk",
            epoch=1,
            directory="/work",
            payload={"seq": seq, "directory": "/work", "event": {"type": "server.heartbeat", "n": seq}},
        )
    state = await log._load(log._key("wrk", 1, "/work"))
    assert state["events"][0]["gap"] is True
    assert state["events"][0]["reason"] == "log_trimmed"
    assert state["events"][0]["from"] == 1
    assert any(item.get("event", {}).get("n") == 3 for item in state["events"])


def test_child_environ_keeps_proxy_ca_and_an_explicit_allowlist():
    cleaned = child_environ(
        {
            "PATH": "/usr/bin",
            "HTTP_PROXY": "http://proxy:8080",
            "https_proxy": "http://proxy:8080",
            "NO_PROXY": "localhost",
            "no_proxy": "localhost",
            "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/ca.pem",
            "CUSTOM_CA_BUNDLE": "/etc/ssl/custom.pem",
            "NEXUS_WORKER_CHILD_ENV_ALLOW": "CUSTOM_CA_BUNDLE,NEXUS_WORKER_TOKEN,OPENAI_API_KEY",
            "NEXUS_WORKER_TOKEN": "wtk_secret",
            "OPENAI_API_KEY": "sk-secret",
        }
    )
    assert cleaned["HTTP_PROXY"] == "http://proxy:8080"
    assert cleaned["https_proxy"] == "http://proxy:8080"
    assert cleaned["NO_PROXY"] == "localhost"
    assert cleaned["no_proxy"] == "localhost"
    assert cleaned["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/certs/ca.pem"
    assert cleaned["CUSTOM_CA_BUNDLE"] == "/etc/ssl/custom.pem"
    assert "NEXUS_WORKER_TOKEN" not in cleaned
    assert "OPENAI_API_KEY" not in cleaned
    assert "NEXUS_WORKER_CHILD_ENV_ALLOW" not in cleaned


async def _until(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")

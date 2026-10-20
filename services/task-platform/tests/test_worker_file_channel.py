"""File bytes stay on the phase-2 data channel. http remains an 8 MiB JSON body."""

import base64
import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import WorkerCommand, WorkerHttpSlot
from app.runtime.worker_client import WorkerCommandError, command_result
from app.worker_gateway.data_channel import (
    MAX_BODY_BYTES,
    MAX_FILE_BYTES,
    MAX_FILE_TEXT_BYTES,
    DataChannel,
    RawBody,
)
from app.worker_gateway.protocol import WorkerGatewayError
from app.worker_gateway.routes import request_body_response
from app.worker_gateway.service import SIDE_EFFECT_COMMANDS
from tests.test_worker_gateway import bring_online, gateway_for


@pytest.fixture
async def environment_id() -> int:
    from app.database import init_database
    from app.models import AgentWorker, OpenCodeExecutionEnvironment

    await init_database()
    async with SessionLocal() as session:
        employee = AgentWorker(id=f"emp-{uuid4().hex[:8]}", name="远程员工", role="执行")
        session.add(employee)
        await session.flush()
        environment = OpenCodeExecutionEnvironment(
            employee_id=employee.id, mode="LINUX_REMOTE", name="云执行机", worker_channel="ssh"
        )
        session.add(environment)
        await session.commit()
        return environment.id


def test_command_result_keeps_legacy_text_and_attaches_raw_body():
    legacy = {"payload": {"stage": "result", "ok": False, "error_code": "符号链接指向任务 Workspace 之外"}}
    with pytest.raises(WorkerCommandError) as old:
        command_result(legacy)
    assert old.value.code == "符号链接指向任务 Workspace 之外"
    assert str(old.value) == "符号链接指向任务 Workspace 之外"
    framed = {
        "payload": {
            "stage": "result",
            "ok": False,
            "error_code": "symlink_escape",
            "error": "符号链接指向任务 Workspace 之外",
        }
    }
    with pytest.raises(WorkerCommandError) as current:
        command_result(framed)
    assert current.value.code == "symlink_escape"
    assert str(current.value) == "符号链接指向任务 Workspace 之外"
    raw = base64.b64encode(b"\xff").decode("ascii")
    result = command_result({"result": {"code": 0, "size": 1, "sha256": "abc"}, "body": raw})
    assert result["body"] == raw
    assert result["code"] == 0


def test_file_channel_limits_do_not_raise_the_http_cap():
    assert MAX_BODY_BYTES == 8 * 1024 * 1024
    assert MAX_FILE_BYTES == 20 * 1024 * 1024
    assert MAX_FILE_TEXT_BYTES > MAX_BODY_BYTES
    assert "upload_file" in SIDE_EFFECT_COMMANDS
    assert "read_file" not in SIDE_EFFECT_COMMANDS
    assert "git_readonly" not in SIDE_EFFECT_COMMANDS
    assert "validate_path" not in SIDE_EFFECT_COMMANDS


async def _dispatch(gateway, enrolled, name, request_id, payload):
    async with SessionLocal() as session:
        return await gateway.dispatch(
            session,
            worker_instance_id=enrolled.worker_instance_id,
            request_id=request_id,
            name=name,
            payload=payload,
        )


async def test_file_commands_do_not_take_http_slots_and_upload_is_not_republished(environment_id):
    gateway = gateway_for()
    enrolled, _auth = await bring_online(gateway, environment_id)
    for name in ("validate_path", "read_file", "git_readonly", "upload_file"):
        await _dispatch(gateway, enrolled, name, f"req-{name}", {"workspace": "/ws"})
    async with SessionLocal() as session:
        slots = (
            await session.scalars(
                select(WorkerHttpSlot).where(
                    WorkerHttpSlot.worker_instance_id == enrolled.worker_instance_id
                )
            )
        ).all()
        rows = (
            await session.scalars(
                select(WorkerCommand).where(
                    WorkerCommand.worker_instance_id == enrolled.worker_instance_id
                )
            )
        ).all()
    assert slots == []
    assert {row.slot_held for row in rows} == {False}
    upload = next(row for row in rows if row.name == "upload_file")
    delivered, reason = await gateway._handoff(upload.id, owner_only=False)
    assert delivered is False
    assert reason == "needs_reconcile"


async def test_empty_upload_is_fetched_as_empty_bytes(environment_id):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    content = b""
    digest = hashlib.sha256(content).hexdigest()
    channel = DataChannel(gateway)
    await channel.stage_upload("req-empty-upload", content, 60)
    await _dispatch(
        gateway,
        enrolled,
        "upload_file",
        "req-empty-upload",
        {"workspace": "/ws", "filename": "empty.txt", "size": 0, "sha256": digest},
    )
    async with SessionLocal() as session:
        fetched = await channel.fetch_request(
            session,
            request_id="req-empty-upload",
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
        )
    assert isinstance(fetched, RawBody)
    assert fetched.content == b""
    assert fetched.sha256 == digest
    response = request_body_response(fetched)
    assert response.body == b""
    assert response.headers["x-astra-sha256"] == digest


async def test_upload_get_returns_raw_bytes_and_git_hash_is_of_those_bytes(environment_id, monkeypatch):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    content = b"\xffhello"
    digest = hashlib.sha256(content).hexdigest()
    channel = DataChannel(gateway)
    await channel.stage_upload("req-upload-file", content, 60)
    await _dispatch(
        gateway,
        enrolled,
        "upload_file",
        "req-upload-file",
        {"workspace": "/ws", "filename": "a.txt", "size": len(content), "sha256": digest},
    )
    async with SessionLocal() as session:
        fetched = await channel.fetch_request(
            session,
            request_id="req-upload-file",
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
        )
    assert isinstance(fetched, RawBody)
    assert fetched.content == content
    assert fetched.sha256 == digest
    response = request_body_response(fetched)
    assert response.media_type == "application/octet-stream"
    assert response.body == content
    assert response.headers["x-astra-sha256"] == digest

    await _dispatch(gateway, enrolled, "upload_file", "req-missing-body", {"workspace": "/ws"})
    async with SessionLocal() as session:
        with pytest.raises(WorkerGatewayError) as missing:
            await channel.fetch_request(
                session,
                request_id="req-missing-body",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
            )
    assert missing.value.code == "unknown_command"

    monkeypatch.setattr("app.worker_gateway.data_channel.MAX_BODY_BYTES", 4)
    monkeypatch.setattr("app.worker_gateway.data_channel.MAX_FILE_TEXT_BYTES", 64)
    monkeypatch.setattr("app.worker_gateway.data_channel.MAX_FILE_BYTES", 8)
    raw = b"\xff\xfe\xfd\xfc"
    encoded = base64.b64encode(raw).decode("ascii")
    assert len(encoded) > 4
    await _dispatch(
        gateway,
        enrolled,
        "git_readonly",
        "req-git-body",
        {"workspace": "/ws", "operation": "head"},
    )
    published = []

    async def capture(request_id, frame):
        published.append((request_id, frame))

    monkeypatch.setattr(gateway.bus, "publish_response", capture)
    async with SessionLocal() as session:
        stored = await channel.commit_upload(
            session,
            request_id="req-git-body",
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
            upload_id="up-git",
            http_status=200,
            headers={},
            content_type=None,
            chunk=encoded,
            complete=True,
            result={"code": 0, "size": 999, "sha256": "lied"},
        )
    assert stored["result"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert stored["result"]["size"] == len(raw)
    assert stored["result"]["code"] == 0
    assert published == [("req-git-body", {"result": stored["result"], "body": encoded})]
    assert base64.b64decode(published[0][1]["body"]) == raw

    await _dispatch(gateway, enrolled, "http", "req-http-cap", {"method": "GET", "path": "/", "body": {}})
    async with SessionLocal() as session:
        with pytest.raises(WorkerGatewayError) as limited:
            await channel.commit_upload(
                session,
                request_id="req-http-cap",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                upload_id="up-http",
                http_status=200,
                headers={},
                content_type="application/json",
                chunk="12345",
                complete=True,
            )
    assert limited.value.code == "response_too_large"
    assert published == [("req-git-body", {"result": stored["result"], "body": encoded})]


async def test_cas_conflict_does_not_wake_the_waiter(environment_id, monkeypatch):
    gateway = gateway_for()
    enrolled, auth = await bring_online(gateway, environment_id)
    await _dispatch(gateway, enrolled, "read_file", "req-read", {"workspace": "/ws", "path": "a"})
    published = []

    async def capture(request_id, frame):
        published.append(request_id)

    monkeypatch.setattr(gateway.bus, "publish_response", capture)
    channel = DataChannel(gateway)
    encoded = base64.b64encode(b"a").decode("ascii")
    async with SessionLocal() as session:
        await channel.commit_upload(
            session,
            request_id="req-read",
            worker_instance_id=enrolled.worker_instance_id,
            epoch=auth.epoch,
            upload_id="up-win",
            http_status=200,
            headers={},
            content_type=None,
            chunk=encoded,
            complete=True,
            result={"missing": False},
        )
    async with SessionLocal() as session:
        with pytest.raises(WorkerGatewayError):
            await channel.commit_upload(
                session,
                request_id="req-read",
                worker_instance_id=enrolled.worker_instance_id,
                epoch=auth.epoch,
                upload_id="up-lose",
                http_status=200,
                headers={},
                content_type=None,
            chunk=base64.b64encode(b"b").decode("ascii"),
            complete=True,
        )
    assert published == ["req-read"]

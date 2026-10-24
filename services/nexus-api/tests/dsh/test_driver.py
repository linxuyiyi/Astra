"""DSH 受理/隔离反例。假 ACP 只用于故障窗口，真实协议另由 Linux 探针覆盖。"""

import asyncio
import errno
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.worker_agent.dsh_acp.driver import DriverConfig, DshAcpDriver


class Host:
    instances = []

    def __init__(self, executable, workspace, home, patch, **kwargs):
        self.workspace, self.home = workspace, home
        self.session_id = None
        self.process = None
        self.stop_confirmed = False
        self.inputs = []
        self.prompt = None
        self.permission_sink = kwargs.get("permission_sink")
        self.instances.append(self)

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            cwd=self.workspace,
            start_new_session=True,
        )

    async def create_or_resume(self, *, session_id=None, mcp_servers=None):
        self.session_id = session_id or str(uuid4())

    def submit(self, text):
        self.inputs.append(text)
        self.prompt = asyncio.get_running_loop().create_future()
        return self.prompt

    async def cancel(self):
        if self.prompt and not self.prompt.done():
            self.prompt.set_result(SimpleNamespace(stop_reason="cancelled"))
        return {"confirmed": True}

    async def dispose(self):
        if self.prompt and not self.prompt.done():
            self.prompt.set_exception(ConnectionError("ACP 连接丢失"))
        if self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()
        self.stop_confirmed = True


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    from app.worker_agent.dsh_acp import preflight

    monkeypatch.setattr(preflight, "inspect_installation", lambda *_: {})
    root = tmp_path / "repo"
    root.mkdir()
    workspace = tmp_path / ".astra-tasks" / "employee" / "task-1"
    workspace.mkdir(parents=True)
    key = tmp_path / "key"
    key.write_text("gateway-secret-that-must-not-leak")
    key.chmod(0o600)
    config = DriverConfig(
        Path(sys.executable),
        tmp_path / "state",
        root,
        "http://127.0.0.1:9999/v1",
        key,
        {"qa": {"context_window": 32000, "max_tokens": 4000}},
    )
    Host.instances = []
    driver = DshAcpDriver(config, "worker-1", supervisor_factory=Host)
    prepare = {"binding_id": "binding-1", "task_id": "task-1", "workspace": str(workspace), "model": "qa"}
    try:
        yield driver, config, prepare
    finally:
        if driver.db:
            try:
                await driver.dispose()
            except Exception:
                pass


async def prepare_and_input(driver, prepare):
    view = await driver.handle("dsh_prepare", prepare)
    args = {
        "binding_id": prepare["binding_id"],
        "task_id": prepare["task_id"],
        "native_session_id": view["native_session_id"],
        "submission_id": "s1",
        "generation": 1,
        "fence": 1,
        "text": "do exactly one operation",
    }
    return args


@pytest.mark.parametrize("error_number", [errno.ENOSYS, errno.EPERM])
async def test_health_rejects_unusable_file_channel_before_gateway_or_native(rig, monkeypatch, error_number):
    import httpx

    from app.worker_agent import files

    driver, _, _ = rig
    calls = []
    opened = []
    original_client = httpx.AsyncClient

    def catalog(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"data": [{"id": "qa"}]})

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(catalog), **kwargs),
    )

    def unavailable(directory, _relative):
        opened.append(directory)
        error = OSError(error_number, "openat2 unavailable")
        raise files.map_open_error(error) from error

    monkeypatch.setattr(files, "validate_inside", unavailable)
    result = await driver.health()
    assert result["healthy"] is False
    assert result["file_channel_verified"] is False
    assert "openat2" in result["error"]
    assert calls == [] and Host.instances == []
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF


async def test_duplicate_different_gateway_request_ids_execute_once(rig):
    driver, _, prepare = rig
    data = await prepare_and_input(driver, prepare)
    await asyncio.gather(*(driver.handle("dsh_submit", data) for _ in range(5)))
    assert Host.instances[0].inputs == [data["text"]]
    assert driver.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    # 账本不保存 prompt 或 Gateway key。
    driver.db.execute("PRAGMA wal_checkpoint(FULL)")
    assert data["text"].encode() not in (driver.config.state_root / "receipts.sqlite3").read_bytes()
    assert (
        b"gateway-secret-that-must-not-leak"
        not in (driver.config.state_root / "receipts.sqlite3").read_bytes()
    )
    with pytest.raises(ValueError, match="内容发生变化"):
        await driver.handle("dsh_submit", {**data, "text": "different operation"})


async def test_cancel_fences_late_submit_and_new_generation_keeps_native_session(rig):
    driver, _, prepare = rig
    data = await prepare_and_input(driver, prepare)
    await driver.handle("dsh_submit", data)
    stopped = await driver.handle("dsh_cancel", {**data, "fence": 2})
    assert stopped["stop_confirmed"] is True
    await driver.handle("dsh_submit", data)  # 同一受理返回旧凭据，不执行。
    with pytest.raises(RuntimeError, match="fence"):
        await driver.handle("dsh_submit", {**data, "submission_id": "late", "fence": 1})
    assert len(Host.instances) == 1
    await driver.handle("dsh_submit", {**data, "submission_id": "next", "fence": 3, "generation": 2})
    assert len(Host.instances) == 2
    assert Host.instances[1].session_id == data["native_session_id"]
    assert Host.instances[0].home == Host.instances[1].home
    old = await driver.handle("dsh_submit", data)
    assert old["receipt"]["submission_id"] == "s1"
    assert old["receipt"]["fence"] == 1
    assert old["receipt"]["generation"] == 1
    assert old["submission_id"] == "next"
    assert Host.instances[1].inputs == [data["text"]]


async def test_cancel_before_prepare_prevents_late_host_creation(rig):
    driver, _, prepare = rig
    view = await driver.handle("dsh_cancel", {**prepare, "fence": 2})
    assert view["stop_confirmed"] is True
    with pytest.raises(ValueError):
        await driver.handle("dsh_prepare", prepare)
    assert not Host.instances


async def test_task_native_and_workspace_cannot_cross_bindings(rig):
    driver, _, prepare = rig
    data = await prepare_and_input(driver, prepare)
    for mutation in ({"task_id": "task-2"}, {"native_session_id": str(uuid4())}):
        with pytest.raises(ValueError):
            await driver.handle("dsh_submit", {**data, **mutation})
    with pytest.raises((ValueError, RuntimeError)):
        await driver.handle("dsh_prepare", {**prepare, "binding_id": "another", "workspace": "/tmp"})
    assert not Host.instances[0].inputs


async def test_restart_unknown_receipt_never_replays_prompt(rig):
    driver, config, prepare = rig
    data = await prepare_and_input(driver, prepare)
    await driver.handle("dsh_submit", data)
    await driver.dispose()
    new = DshAcpDriver(config, "worker-1", supervisor_factory=Host)
    try:
        view = await new.handle("dsh_submit", data)
        assert view["state"] == "unknown"
        assert len(Host.instances) == 1
        stopped = await new.handle("dsh_cancel", {**data, "fence": 2})
        assert stopped["stop_confirmed"]
        await new.handle("dsh_submit", {**data, "submission_id": "next", "fence": 3, "generation": 2})
        assert Host.instances[-1].session_id == data["native_session_id"]
    finally:
        await new.dispose()
    driver.db = None


async def test_permission_rejects_cross_session_and_disconnect_denies(rig):
    driver, _, prepare = rig
    data = await prepare_and_input(driver, prepare)
    await driver.handle("dsh_submit", data)
    host = Host.instances[0]
    future = asyncio.create_task(
        host.permission_sink(
            data["native_session_id"],
            [
                {"optionId": "yes", "kind": "allow_once"},
                {"optionId": "always", "kind": "allow_always"},
            ],
            {"toolCallId": "tool-1", "title": "write outside workspace"},
        )
    )
    await asyncio.sleep(0)
    view = await driver.handle("dsh_snapshot", data)
    approval = view["permissions"][0]["id"]
    with pytest.raises(ValueError):
        await driver.handle("dsh_permission", {**data, "permission_id": approval, "option_id": "always"})
    with pytest.raises(ValueError):
        await driver.handle(
            "dsh_permission", {**data, "task_id": "task-2", "permission_id": approval, "option_id": "yes"}
        )
    driver.connection_lost()
    assert await future is None


async def test_profile_drift_and_second_writer_are_rejected(rig):
    driver, config, prepare = rig
    data = await prepare_and_input(driver, prepare)
    with pytest.raises(RuntimeError, match="写入者"):
        DshAcpDriver(config, "worker-1", supervisor_factory=Host)
    await driver.handle("dsh_cancel", {**data, "fence": 2})
    patch = config.state_root / prepare["binding_id"] / "nexus-gateway.patch.yml"
    patch.write_text("[]")
    with pytest.raises(RuntimeError, match="完整性"):
        await driver.handle("dsh_submit", {**data, "submission_id": "next", "fence": 3})


async def test_confirm_dispose_reclaims_host_and_preserves_native_outcome_without_message_mirror(rig):
    driver, _, prepare = rig
    data = await prepare_and_input(driver, prepare)
    accepted = await driver.handle("dsh_submit", data)
    assert "events" not in accepted and "permissions" not in accepted
    host = Host.instances[0]
    host.prompt.set_result(SimpleNamespace(stop_reason="end_turn"))
    await asyncio.sleep(0)
    view = await driver.handle("dsh_dispose", {**data, "fence": 2})
    assert view["state"] == "succeeded" and view["stop_confirmed"] is True
    assert host.process.returncode is not None
    assert view["native_session_id"] == data["native_session_id"]
    assert "events" not in view and "permissions" not in view
    with pytest.raises(RuntimeError, match="fence"):
        await driver.handle("dsh_submit", {**data, "submission_id": "late"})

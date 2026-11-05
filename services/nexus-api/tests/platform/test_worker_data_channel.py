"""Request-ready frames, HTTP bodies, event cursors, and the worker mark."""

import asyncio
import json
import subprocess

import pytest

from app.worker_agent.connection import WorkerConnectionManager
from app.worker_agent.workspace import prepare_workspace_local
from app.worker_gateway.bus import MemoryRouteBus
from app.worker_gateway.events import EventLog
from app.worker_gateway.protocol import envelope


@pytest.mark.asyncio
async def test_event_gap_coalesces_refresh_and_cursor_pause():
    bus = MemoryRouteBus()
    log = EventLog(bus, limit=2, lease_seconds=30)
    first = await log.append(
        worker_id="wrk", epoch=1, directory="/work", payload={"seq": 1, "event": {"type": "a"}}
    )
    assert first["action"] == "store"
    gap = await log.append(
        worker_id="wrk", epoch=1, directory="/work", payload={"seq": 3, "event": {"type": "c"}}
    )
    assert gap["action"] == "gap"
    assert await log.begin_refresh(worker_id="wrk", epoch=1, directory="/work") is True
    assert await log.begin_refresh(worker_id="wrk", epoch=1, directory="/work") is False
    paced = EventLog(MemoryRouteBus(), limit=2, lease_seconds=30)

    async def store(seq: int, kind: str) -> None:
        await paced.append(
            worker_id="wrk",
            epoch=1,
            directory="/work",
            payload={"seq": seq, "event": {"type": kind}},
        )

    await store(1, "a")
    await store(2, "b")
    assert await paced.control(worker_id="wrk", epoch=1, directory="/work") == "stop_events"
    await store(1, "a")
    await store(2, "b")
    await paced.touch_cursor(worker_id="wrk", epoch=1, directory="/work", consumer="browser:1", seq=0)
    assert await paced.control(worker_id="wrk", epoch=1, directory="/work") == "pause_events"
    await paced.touch_cursor(worker_id="wrk", epoch=1, directory="/work", consumer="browser:1", seq=2)
    assert await paced.control(worker_id="wrk", epoch=1, directory="/work") == "resume_events"


@pytest.mark.asyncio
async def test_http_command_does_not_block_ping():
    from app.worker_agent.connection import AgentConfig
    from app.worker_gateway.protocol import parse_message

    started = asyncio.Event()
    release = asyncio.Event()

    async def local_request(_method, _path, _body):
        started.set()
        await release.wait()
        return {"status": 204, "body": ""}

    async def fetch_body(_request_id, _epoch):
        return {"body": "{}"}

    async def upload_body(_request_id, _epoch, _result):
        return None

    manager = WorkerConnectionManager(
        AgentConfig(
            gateway_url="ws://127.0.0.1/api/worker-gateway/connect",
            agent_worker_id="wrk",
            token="wtk_test",
        )
    )
    manager.epoch = 1
    manager.clock = lambda: 0
    manager.fetch_body = fetch_body
    manager.local_request = local_request
    manager.upload_body = upload_body

    class Transport:
        def __init__(self):
            self.sent = []

        async def send(self, message):
            self.sent.append(message)

        async def recv(self):
            raise ConnectionError("closed")

        async def close(self):
            return None

    def framed(name, request_id):
        message = envelope(
            request_id=request_id,
            agent_worker_id="wrk",
            session_id=None,
            message_type="command",
            payload={"name": name, "data": {"method": "POST", "path": "/x", "ready": True}},
        )
        message.epoch = 1
        message.expires_at = 10**12
        return parse_message(json.dumps(message.to_dict()), max_bytes=10**6)

    transport = Transport()
    manager._start_http(transport, framed("http", "req-http"), 1)
    await asyncio.wait_for(started.wait(), 1)
    await manager._handle_command(transport, framed("ping", "ping-1"), 1)
    assert any(item["payload"].get("result", {}).get("pong") for item in transport.sent)
    release.set()
    await asyncio.gather(*manager.http_tasks.values())


def test_prepare_workspace_copies_opencode_config(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "opencode.json").write_text('{"model":"deepseek-flash"}\n')
    target = tmp_path / "task"
    result = prepare_workspace_local(str(root), str(target))
    assert result["created"] == "true"
    assert json.loads((target / "opencode.json").read_text())["model"] == "deepseek-flash"


def _commit_repo(path):
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=astra@localhost",
            "-c",
            "user.name=Astra",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )


def test_prepare_workspace_adds_a_detached_worktree_once(tmp_path):
    root = tmp_path / "root"
    _commit_repo(root)
    (root / "opencode.json").write_text('{"model":"deepseek-flash"}\n')
    subprocess.run(["git", "-C", str(root), "add", "opencode.json"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=astra@localhost",
            "-c",
            "user.name=Astra",
            "commit",
            "-m",
            "config",
        ],
        check=True,
        capture_output=True,
    )
    target = tmp_path / "tasks" / "T1"
    first = prepare_workspace_local(str(root), str(target))
    assert first["created"] == "true"
    assert (target / ".git").exists()
    (target / "opencode.json").write_text('{"model":"local"}\n')
    second = prepare_workspace_local(str(root), str(target))
    assert second["created"] == "false"
    assert json.loads((target / "opencode.json").read_text())["model"] == "local"
    listed = subprocess.check_output(["git", "-C", str(root), "worktree", "list", "--porcelain"], text=True)
    assert f"worktree {target.resolve()}" in listed


def test_prepare_workspace_rejects_reuse_symlink_subdir_and_foreign_repo(tmp_path):
    root = tmp_path / "root"
    _commit_repo(root)
    with pytest.raises(RuntimeError, match="请显式重新执行"):
        prepare_workspace_local(str(root), str(tmp_path / "missing"), reuse_existing=True)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(RuntimeError, match="符号链接"):
        prepare_workspace_local(str(root), str(link))
    nested = root / "nested"
    nested.mkdir()
    with pytest.raises(RuntimeError, match="不是目录根"):
        prepare_workspace_local(str(root), str(nested))
    other = tmp_path / "other"
    _commit_repo(other)
    with pytest.raises(RuntimeError, match="不属于当前仓库"):
        prepare_workspace_local(str(root), str(other))

"""SSH channel lifecycle owns the target flock, without a remote service."""

import shlex
from unittest.mock import AsyncMock

import pytest

from app.runtime.ssh_client import SSHSession


async def test_lock_is_sorted_quoted_and_held_by_open_stdin():
    session = SSHSession(host="example.invalid", port=22, username="root", password="unused")
    channel = AsyncMock()
    channel.stdout.readline.return_value = b"NEXUS_LOCKED\n"
    session.open_exec = AsyncMock(return_value=channel)
    result = await session.hold_lock(["/locks/worker 2", "/locks/a'workspace"])
    command = session.open_exec.call_args.args[0]
    words = shlex.split(command)
    assert words[:6] == ["flock", "-n", "/locks/a'workspace", "flock", "-n", "/locks/worker 2"]
    assert words[6:8] == ["sh", "-c"]
    assert "read -r release" in words[8]
    session.open_exec.assert_awaited_once_with(command, keep_stdin=True)
    assert result is channel
    channel.close.assert_not_awaited()


@pytest.mark.parametrize("reply", [b"", b"busy\n"])
async def test_failed_lock_closes_channel(reply):
    session = SSHSession(host="example.invalid", port=22, username="root", password="unused")
    channel = AsyncMock()
    channel.stdout.readline.return_value = reply
    session.open_exec = AsyncMock(return_value=channel)
    with pytest.raises(RuntimeError, match="远端锁"):
        await session.hold_lock(["/locks/worker"])
    channel.close.assert_awaited_once()

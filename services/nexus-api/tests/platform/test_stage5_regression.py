"""阶段 5：反向隧道和 ssh 执行没有残留调用方；超时和进程重启后 Task 仍绑定原来的 Session。"""

import asyncio
import inspect
from pathlib import Path

from app.domain import TaskStatus
from app.runtime.lifecycle import RuntimeService
from app.runtime.session_connection import connect
from app.state_machine import TaskStateMachine

APP = Path(__file__).resolve().parents[2] / "app"
ROOT = Path(__file__).resolve().parents[4]
BANNED = (
    "ensure_remote_mcp_tunnel",
    "ensure_remote_gateway_tunnel",
    "close_remote_mcp_tunnels",
    "open_reverse_tunnel",
    "SSHReverseTunnel",
    "SSHTunnel",
)
SSH_CALLS = {
    ("app/runtime/environment.py", "SSHSession("),
    ("app/runtime/environment.py", ".open_session("),
    ("app/runtime/maintenance.py", ".open_session("),
    ("app/runtime/remote_provisioning.py", ".open_session("),
    ("app/runtime/ssh_client.py", ".open_session("),
}


def test_deleted_tunnel_and_ssh_execution_have_no_callers():
    found = set()
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(APP.parent).as_posix()
        for name in BANNED:
            assert name not in text, f"{relative} still mentions {name}"
        assert ".run_remote(" not in text
        for needle in ("SSHSession(", ".open_session(", ".run_remote("):
            if needle in text:
                found.add((relative, needle))
        if path.name == "opencode_remote.py":
            continue
        assert "RemoteOpenCodeExecutor" not in text
        assert "from app.executors.opencode_remote import" not in text
        assert "import app.executors.opencode_remote" not in text
    assert found == SSH_CALLS
    connect_source = inspect.getsource(connect)
    start_source = inspect.getsource(RuntimeService.start)
    assert "open_session" not in connect_source
    assert "opencode_remote" not in connect_source
    assert "RemoteOpenCodeExecutor" not in start_source
    assert "open_session" not in start_source


def test_cli_fallback_agentscope_and_state_machine_stay():
    cli = (APP / "executors" / "opencode_cli.py").read_text(encoding="utf-8")
    assert "class OpenCodeCLIExecutor" in cli

    assert set(TaskStateMachine.transitions) == set(TaskStatus)

    agentscope = (ROOT / "services" / "nexus-api" / "nexus_api" / "agentscope.py").read_text(encoding="utf-8")
    assert "import agentscope" not in agentscope
    assert "from agentscope" not in agentscope
    launcher = (ROOT / "services" / "agent-service" / "agent_service.py").read_text(encoding="utf-8")
    head = launcher.split("def main()", 1)[0]
    assert "import agentscope" not in head
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import agentscope" not in text
        assert "from agentscope" not in text


class _TimeoutClient:
    def events(self, _directory):
        async def _gen():
            await asyncio.Event().wait()
            yield {}

        return _gen()

    async def status(self, *_args, **_kwargs):
        raise TimeoutError("network timeout")

    async def create_session(self, *_args, **_kwargs):
        raise AssertionError("超时和重启不能新建 Session")

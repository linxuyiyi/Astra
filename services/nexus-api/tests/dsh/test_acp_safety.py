"""ACP 基础设施的安全反例；真实引擎兼容性由独立 P0 探针验证。"""

import asyncio
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp.schema import PermissionOption, ToolCallUpdate

from app.worker_agent.dsh_acp.process import AcpProcessSupervisor
from app.worker_agent.dsh_acp.profile import GatewayModel, prepare_profile, profile_patch
from app.worker_agent.dsh_acp.trace import TraceUnavailable, read_trace_page

SESSION = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def binding(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    home = tmp_path / "home"
    patch, _ = prepare_profile(home, GatewayModel("http://nexus.test/v1", "alias", 32768, 1024), "test-key")
    return AcpProcessSupervisor(Path("/not-started/dsh"), workspace, home, patch)


@pytest.mark.parametrize("url", [
    "https://host", "https://host/v1?key=secret", "https://user:secret@host/v1",
    "ftp://host/v1", "https://host/v1#token", "file:///v1", "https://host/v1/chat/completions",
])
def test_profile_rejects_ambiguous_or_secret_urls(url):
    with pytest.raises(ValueError):
        GatewayModel(url, "route", 32768, 1000)


def test_profile_does_not_overwrite_existing_home_or_follow_link(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    sentinel = original / ".credentials.yaml"
    sentinel.write_text("user credentials")
    link = tmp_path / "link"
    link.symlink_to(original, target_is_directory=True)
    model = GatewayModel("http://gateway/v1", "route", 100, 10)
    for path in (original, link):
        with pytest.raises((ValueError, FileExistsError)):
            prepare_profile(path, model, "probe")
    assert sentinel.read_text() == "user credentials"


def test_profile_contains_only_explicit_gateway_model_and_private_credentials(tmp_path):
    home = tmp_path / "home"
    model = GatewayModel("https://gateway/v1", "alias", 32768, 2000)
    path, digest = prepare_profile(home, model, "not-a-provider-key")
    assert "not-a-provider-key" not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((home / ".credentials.yaml").stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    rows = {row["id"]: row for row in profile_patch(home, model)}
    assert rows["llm-deepseek"]["disabled"] and rows["llm-deepseek-account"]["disabled"]
    assert rows["tool-web"]["disabled"]
    assert rows["acp"]["config"] == {"provider": "nexus-gateway", "model": "alias"}
    assert len(digest) == 64


@pytest.mark.skipif(os.name != "posix", reason="POSIX 真实 flock 所有权测试")
def test_two_hosts_cannot_write_same_home(binding):
    second = AcpProcessSupervisor(binding.executable, binding.workspace, binding.home, binding.patch)
    binding._lock_home()
    try:
        with pytest.raises(RuntimeError, match="已有活跃所有者"):
            second._lock_home()
    finally:
        os.close(binding._lock_fd)
        binding._lock_fd = None
    second._lock_home()
    os.close(second._lock_fd)
    second._lock_fd = None


def options():
    return [PermissionOption(kind="allow_once", name="Allow", option_id="allow"),
            PermissionOption(kind="reject_once", name="Reject", option_id="deny"),
            PermissionOption(kind="allow_always", name="Always", option_id="always")]


async def reply(binding, session=SESSION):
    binding._tool_calls["tool-1"] = {"toolCallId": "tool-1", "rawInput": {"command": "probe"}}
    return await binding.request_permission(
        options(), session, ToolCallUpdate(tool_call_id="tool-1", title="probe"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [None, "unknown", "always"])
async def test_permission_never_infers_allow(binding, choice):
    binding.session_id = SESSION

    async def decide(*_):
        return choice

    binding.permission_sink = decide
    assert (await reply(binding)).outcome.outcome == "cancelled"


@pytest.mark.asyncio
async def test_permission_timeout_and_wrong_session_fail_closed(binding):
    binding.session_id = SESSION
    binding.permission_timeout = .01
    calls = []

    async def decide(*_):
        calls.append(True)
        await asyncio.sleep(10)
        return "allow"

    binding.permission_sink = decide
    assert (await reply(binding, "other-session")).outcome.outcome == "cancelled"
    assert calls == []
    assert (await reply(binding)).outcome.outcome == "cancelled"
    assert calls == [True]


@pytest.mark.asyncio
async def test_permission_explicit_one_shot_choice(binding):
    binding.session_id = SESSION

    async def decide(_session, _options, tool):
        assert tool["rawInput"] == {"command": "probe"}
        return "allow"

    binding.permission_sink = decide
    assert (await reply(binding)).outcome.option_id == "allow"


@pytest.mark.asyncio
async def test_unknown_permission_call_id_cannot_be_authorized(binding):
    binding.session_id = SESSION
    calls = []

    async def decide(*_):
        calls.append(True)
        return "allow"

    binding.permission_sink = decide
    result = await binding.request_permission(
        options(), SESSION, ToolCallUpdate(tool_call_id="unseen"),
    )
    assert result.outcome.outcome == "cancelled" and calls == []


@pytest.mark.asyncio
async def test_cancel_notification_alone_does_not_release_session(binding):
    binding.session_id = SESSION
    binding._prompt = asyncio.get_running_loop().create_future()
    called = []

    async def cancel(**_):
        called.append("cancel_sent")

    async def dispose():
        called.append("owned_process_terminated")
        binding.process = SimpleNamespace(returncode=-9)
        binding.stop_confirmed = True

    binding.connection = SimpleNamespace(cancel=cancel)
    binding.dispose = dispose
    result = await binding.cancel(timeout=.01)
    assert called == ["cancel_sent", "owned_process_terminated"]
    assert result["method"] == "owned_process_terminated"
    assert not binding._prompt.cancelled()  # 超时不得取消本地 future 冒充原生停止
    binding._prompt.cancel()


@pytest.mark.asyncio
async def test_no_local_prompt_is_not_native_stop_evidence(binding):
    binding.session_id = SESSION
    binding.connection = SimpleNamespace()
    called = []

    async def dispose():
        called.append(True)
        binding.stop_confirmed = False

    binding.dispose = dispose
    result = await binding.cancel()
    assert called == [True] and result["confirmed"] is False


@pytest.mark.asyncio
async def test_foreign_session_update_is_rejected(binding):
    binding.session_id = SESSION
    with pytest.raises(RuntimeError, match="不属于"):
        await binding.session_update("other", SimpleNamespace())


def trace_file(binding, events):
    path = binding.home / "sessions" / "project" / SESSION / "session.v4.jsonl"
    path.parent.mkdir(parents=True)
    header = {"type": "session", "version": 4, "id": SESSION,
              "cwd": str(binding.workspace), "isSeeded": False}
    path.write_text("\n".join(json.dumps(row) for row in [header, *events]) + "\n")
    return path


def test_trace_pagination_preserves_native_sequences_and_digest(binding):
    trace_file(binding, [{"seq": i, "type": "tool/result", "data": {}} for i in range(3)])
    first = read_trace_page(binding.home, binding.workspace, SESSION, limit=2)
    second = read_trace_page(binding.home, binding.workspace, SESSION, cursor=first["next_cursor"], limit=2)
    assert [row["seq"] for row in first["events"] + second["events"]] == [0, 1, 2]
    assert second["next_cursor"] is None
    assert first["sha256"] == second["sha256"]


@pytest.mark.parametrize("events", [
    [{"seq": 1, "type": "tool/call"}],
    [{"seq": 0, "type": "tool/call"}, {"seq": 0, "type": "tool/result"}],
    [{"seq": 0, "type": "turn/end", "data": {"turn": 1}}],
])
def test_trace_gaps_duplicates_and_missing_start_are_not_complete(binding, events):
    trace_file(binding, events)
    with pytest.raises(TraceUnavailable):
        read_trace_page(binding.home, binding.workspace, SESSION)


def test_disconnected_turn_is_not_a_complete_result(binding):
    trace_file(binding, [{"seq": 0, "type": "turn/start", "data": {"turn": 1}}])
    page = read_trace_page(binding.home, binding.workspace, SESSION)
    assert page["complete"] is False and page["unsettled_turns"] == [1]


def test_trace_binding_traversal_version_and_partial_tail_fail_closed(binding):
    path = trace_file(binding, [{"seq": 0, "type": "tool/call"}])
    for session, work, version in [("../../outside", binding.workspace, "0.2.1-rc.2"),
                                    (SESSION, binding.workspace.parent, "0.2.1-rc.2"),
                                    (SESSION, binding.workspace, "latest")]:
        with pytest.raises(TraceUnavailable):
            read_trace_page(binding.home, work, session, dsh_version=version)
    path.write_text(path.read_text() + '{"seq":1')
    with pytest.raises(TraceUnavailable, match="尾部"):
        read_trace_page(binding.home, binding.workspace, SESSION)


def test_symlink_trace_cannot_escape_home(binding, tmp_path):
    path = trace_file(binding, [])
    outside = tmp_path / "outside"
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(TraceUnavailable, match="符号链接"):
        read_trace_page(binding.home, binding.workspace, SESSION)

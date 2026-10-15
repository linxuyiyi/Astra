# -*- coding: utf-8 -*-
"""冒烟脚本 guard 归因回归测试。

只验证 ``scripts/smoke_resources.py::_run_guard_denied`` 的**本轮归因**：既有
正常历史回复不得被算作本轮 guard 回复；本轮任何新的正常成功回复必须失败；
本轮原生 error 记录（空文本/明确 guard 拒绝）可接受。使用纯内存 fake adapter，
不启动真实服务、不读取 Home 或 key。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from nexus_backend.adapter import UpstreamError

ROOT = Path(__file__).resolve().parents[2]
SMOKE_PATH = ROOT / "scripts" / "smoke_resources.py"


def _load_smoke():
    spec = importlib.util.spec_from_file_location(
        "nexus_smoke_resources",
        SMOKE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()


def _assistant(message_id: str, text: str) -> dict:
    return {
        "id": message_id,
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
    }


def _user(message_id: str) -> dict:
    return {
        "id": message_id,
        "role": "user",
        "content": [{"type": "text", "text": "请按指示处理本条消息。"}],
    }


class FakeAdapter:
    """返回 baseline / after 两套完整 history；触发后切换。

    ``trigger_error`` 非空时 ``trigger_chat`` 抛出该异常（用于模拟 admission
    直接拒绝等上游错误）。
    """

    def __init__(self, before, after, events, *, status="idle",
                 trigger_error=None):
        self._before = before
        self._after = after
        self._events = events
        self._status = status
        self._trigger_error = trigger_error
        self._attempted = False

    async def list_messages(self, agent_id, session_id, **kwargs):
        return {"event_cursor": "cursor-1"}

    async def trigger_chat(self, agent_id, session_id, input_msg):
        self._attempted = True
        if self._trigger_error is not None:
            raise self._trigger_error
        return {"status": "started"}

    async def stream_events(self, agent_id, session_id, *, after=None):
        for event in self._events:
            yield event

    async def get_session_status(self, agent_id, session_id):
        return {"status": self._status}

    async def iter_all_messages(self, agent_id, session_id, **kwargs):
        return self._after if self._attempted else self._before


def _upstream(status: int) -> UpstreamError:
    return UpstreamError("POST", "/chat/", status, "上游拒绝请求")


_DENIED_EVENTS = [{"type": "REPLY_END", "finished_reason": "error"}]

_HISTORY = [
    _user("u-1"),
    _assistant("a-1", "探针。"),
    _assistant("a-2", "联通正常。"),
    _assistant("a-3", "42"),
]


@pytest.mark.asyncio
async def test_existing_history_with_new_error_record_passes():
    after = _HISTORY + [_user("u-new"), _assistant("a-new", "")]
    adapter = FakeAdapter(_HISTORY, after, _DENIED_EVENTS)
    result = await smoke._run_guard_denied(adapter, "agent-1", "sess-1")
    assert result["new_assistant_messages"] == 1
    assert result["new_assistant_texts"] == [""]
    # 旧成功历史的 42 绝不被算作本轮回复。
    assert result["model_call_started"] is False
    assert result["finished_reason"] == "error"


@pytest.mark.asyncio
async def test_only_historical_success_no_new_reply_passes():
    adapter = FakeAdapter(_HISTORY, _HISTORY, _DENIED_EVENTS)
    result = await smoke._run_guard_denied(adapter, "agent-1", "sess-1")
    assert result["new_assistant_messages"] == 0


@pytest.mark.asyncio
async def test_new_successful_reply_fails():
    after = _HISTORY + [_user("u-new"), _assistant("a-new", "这是新的成功回复")]
    adapter = FakeAdapter(_HISTORY, after, _DENIED_EVENTS)
    with pytest.raises(smoke.SmokeError):
        await smoke._run_guard_denied(adapter, "agent-1", "sess-1")


@pytest.mark.asyncio
async def test_guard_word_in_one_new_message_does_not_mask_another():
    """本轮一条 guard 词消息不得吞掉另一条正常新回复。"""
    after = _HISTORY + [
        _user("u-new"),
        _assistant("a-new-1", "运行被 guard 拒绝"),
        _assistant("a-new-2", "正常新回复"),
    ]
    adapter = FakeAdapter(_HISTORY, after, _DENIED_EVENTS)
    with pytest.raises(smoke.SmokeError):
        await smoke._run_guard_denied(adapter, "agent-1", "sess-1")


@pytest.mark.asyncio
async def test_model_call_started_this_round_fails():
    events = [
        {"type": "MODEL_CALL_START"},
        {"type": "REPLY_END", "finished_reason": "error"},
    ]
    adapter = FakeAdapter(_HISTORY, _HISTORY, events)
    with pytest.raises(smoke.SmokeError):
        await smoke._run_guard_denied(adapter, "agent-1", "sess-1")


@pytest.mark.asyncio
async def test_missing_reply_end_fails():
    """断流/超时导致无本轮 REPLY_END：不得当作 guard 拒绝成功。"""
    adapter = FakeAdapter(_HISTORY, _HISTORY, [])
    with pytest.raises(smoke.SmokeError):
        await smoke._run_guard_denied(adapter, "agent-1", "sess-1")


@pytest.mark.asyncio
async def test_finished_reason_none_fails():
    """REPLY_END 但 finished_reason 为 None：不算明确 error。"""
    events = [{"type": "REPLY_END", "finished_reason": None}]
    adapter = FakeAdapter(_HISTORY, _HISTORY, events)
    with pytest.raises(smoke.SmokeError):
        await smoke._run_guard_denied(adapter, "agent-1", "sess-1")


@pytest.mark.asyncio
async def test_not_idle_fails(monkeypatch):
    """会话未回到 idle：不得忽略等待超时，必须失败。"""
    adapter = FakeAdapter(
        _HISTORY,
        _HISTORY,
        _DENIED_EVENTS,
        status="running",
    )

    original_wait_idle = smoke._wait_idle

    async def _fast_wait_idle(adapter_, agent_id, session_id, **kwargs):
        return await original_wait_idle(
            adapter_,
            agent_id,
            session_id,
            timeout=0.1,
        )

    monkeypatch.setattr(smoke, "_wait_idle", _fast_wait_idle)
    with pytest.raises(smoke.SmokeError):
        await smoke._run_guard_denied(adapter, "agent-1", "sess-1")


# ----------------------------------------------------------------------
# admission 直接 409 拒绝路径（禁用/删除中，原生 handler 前拒绝）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_admission_409_with_unchanged_history_passes():
    """明确 409 且历史无新用户/assistant：算运行 admission 真实阻断。"""
    adapter = FakeAdapter(
        _HISTORY,
        _HISTORY,
        [],
        trigger_error=_upstream(409),
    )
    result = await smoke._run_guard_denied(adapter, "agent-1", "sess-1")
    assert result["trigger_status"] == "rejected"
    assert result["admission_status"] == 409
    assert result["new_user_messages"] == 0
    assert result["new_assistant_messages"] == 0
    assert result["model_call_started"] is False
    assert result["session_status"] == "idle"


@pytest.mark.asyncio
async def test_admission_409_with_new_assistant_fails():
    """409 后仍出现新 assistant 执行：不得当作拒绝成功。"""
    after = _HISTORY + [_user("u-new"), _assistant("a-new", "新成功回复")]
    adapter = FakeAdapter(
        _HISTORY,
        after,
        [],
        trigger_error=_upstream(409),
    )
    with pytest.raises(smoke.SmokeError):
        await smoke._run_guard_denied(adapter, "agent-1", "sess-1")


@pytest.mark.asyncio
async def test_admission_409_with_new_user_fails():
    """409 后仍出现新用户消息：拒绝未真实阻断。"""
    after = _HISTORY + [_user("u-new")]
    adapter = FakeAdapter(
        _HISTORY,
        after,
        [],
        trigger_error=_upstream(409),
    )
    with pytest.raises(smoke.SmokeError):
        await smoke._run_guard_denied(adapter, "agent-1", "sess-1")


@pytest.mark.asyncio
async def test_admission_409_running_session_fails():
    """409 后会话仍在运行：拒绝未真实阻断。"""
    adapter = FakeAdapter(
        _HISTORY,
        _HISTORY,
        [],
        status="running",
        trigger_error=_upstream(409),
    )
    with pytest.raises(smoke.SmokeError):
        await smoke._run_guard_denied(adapter, "agent-1", "sess-1")


@pytest.mark.asyncio
async def test_non_409_upstream_error_is_not_swallowed():
    """非 409 上游错误必须如实抛出，绝不吞成拒绝成功。"""
    adapter = FakeAdapter(
        _HISTORY,
        _HISTORY,
        [],
        trigger_error=_upstream(500),
    )
    with pytest.raises(UpstreamError):
        await smoke._run_guard_denied(adapter, "agent-1", "sess-1")

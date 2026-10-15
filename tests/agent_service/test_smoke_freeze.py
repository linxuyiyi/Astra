# -*- coding: utf-8 -*-
"""冻结冒烟脚本负向回归（不启动真实进程、不读取 Home/密钥）。

只验证 ``scripts/smoke_freeze.py`` 的纯判定逻辑：模型 error/空输出不得算基线
通过；冻结报告未捕获在途真实 Task、仍保留 pending、未排空/未确认时不得算
cancel-tail；服务 stop 失败必须让冒烟失败并保留证据。使用内存 fake，不调用
真实 API、不读取密钥。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SMOKE_PATH = ROOT / "scripts" / "smoke_freeze.py"


def _load_smoke():
    spec = importlib.util.spec_from_file_location(
        "nexus_smoke_freeze",
        SMOKE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()


def _session(*, alive_at_capture=True, alive=False, pending=None, sid="s1"):
    return {
        "session_id": sid,
        "captured_chat_alive_at_capture": alive_at_capture,
        "captured_chat_alive": alive,
        "pending": pending if pending is not None else [],
    }


def _report(session, *, drained=True, verified=True, archival="not_verified"):
    return {
        "execution_drained": drained,
        "persistence_verified": verified,
        "accepted_input_archival": archival,
        "sessions": [session],
    }


# ----------------------------------------------------------------------
# 基线轮：模型 error / 空输出 / canceled 不得通过
# ----------------------------------------------------------------------
def test_baseline_completed_with_text_passes():
    smoke._require_completed_round(
        {"finished_reason": "completed", "text": "你好"},
        label="基线",
    )


@pytest.mark.parametrize("reason", ["error", "interrupted", "canceled", None])
def test_baseline_non_completed_fails(reason):
    with pytest.raises(smoke.SmokeError):
        smoke._require_completed_round(
            {"finished_reason": reason, "text": "非空"},
            label="基线",
        )


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_baseline_empty_output_fails(text):
    with pytest.raises(smoke.SmokeError):
        smoke._require_completed_round(
            {"finished_reason": "completed", "text": text},
            label="基线",
        )


# ----------------------------------------------------------------------
# cancel-tail 报告：必须证明捕获在途真实 Task 并最终排空确认
# ----------------------------------------------------------------------
def test_cancel_tail_report_valid_passes():
    session = _session(alive_at_capture=True, alive=False, pending=[])
    result = smoke._require_cancel_tail_report(
        _report(session),
        session_id="s1",
    )
    assert result is session


def test_cancel_tail_not_captured_inflight_fails():
    session = _session(alive_at_capture=False, alive=False)
    with pytest.raises(smoke.SmokeError):
        smoke._require_cancel_tail_report(_report(session), session_id="s1")


def test_cancel_tail_still_alive_fails():
    session = _session(alive_at_capture=True, alive=True)
    with pytest.raises(smoke.SmokeError):
        smoke._require_cancel_tail_report(_report(session), session_id="s1")


def test_cancel_tail_pending_fails():
    session = _session(
        alive_at_capture=True,
        alive=False,
        pending=[{"entry_id": "e", "kind": "state"}],
    )
    with pytest.raises(smoke.SmokeError):
        smoke._require_cancel_tail_report(_report(session), session_id="s1")


def test_cancel_tail_not_drained_or_not_verified_fails():
    session = _session(alive_at_capture=True, alive=False)
    with pytest.raises(smoke.SmokeError):
        smoke._require_cancel_tail_report(
            _report(session, drained=False),
            session_id="s1",
        )
    with pytest.raises(smoke.SmokeError):
        smoke._require_cancel_tail_report(
            _report(session, verified=False),
            session_id="s1",
        )


def test_cancel_tail_wrong_archival_fails():
    session = _session(alive_at_capture=True, alive=False)
    with pytest.raises(smoke.SmokeError):
        smoke._require_cancel_tail_report(
            _report(session, archival="archived"),
            session_id="s1",
        )


def test_cancel_tail_missing_session_fails():
    session = _session(alive_at_capture=True, alive=False, sid="other")
    with pytest.raises(smoke.SmokeError):
        smoke._require_cancel_tail_report(_report(session), session_id="s1")


# ----------------------------------------------------------------------
# 最终取消尾轮：必须 interrupted 且保留已观测真实输出
# ----------------------------------------------------------------------
def test_cancel_tail_round_interrupted_with_prefix_passes():
    smoke._require_cancel_tail_round(
        {"finished_reason": "interrupted", "text": "1\n2\n3\n4"},
        label="本轮",
        observed_prefix="1\n2\n",
    )


@pytest.mark.parametrize("reason", ["completed", "error", None, "exceed_max_iters"])
def test_cancel_tail_round_non_interrupted_fails(reason):
    with pytest.raises(smoke.SmokeError):
        smoke._require_cancel_tail_round(
            {"finished_reason": reason, "text": "1\n2\n"},
            label="本轮",
            observed_prefix="1\n",
        )


def test_cancel_tail_round_empty_body_fails():
    with pytest.raises(smoke.SmokeError):
        smoke._require_cancel_tail_round(
            {"finished_reason": "interrupted", "text": "   "},
            label="本轮",
            observed_prefix="x",
        )


def test_cancel_tail_round_missing_observed_prefix_fails():
    """只留中断占位、丢掉已观测真实输出，不得算取消验收通过。"""
    with pytest.raises(smoke.SmokeError):
        smoke._require_cancel_tail_round(
            {"finished_reason": "interrupted", "text": "（工具调用被中断）"},
            label="本轮",
            observed_prefix="1\n2\n",
        )


def test_require_persisted_reply_found_and_missing():
    messages = [{"id": "r1", "role": "assistant", "content": []}]
    reply = smoke._require_persisted_reply(messages, "r1", label="本轮")
    assert reply is messages[0]
    with pytest.raises(smoke.SmokeError):
        smoke._require_persisted_reply(messages, "missing", label="本轮")
    with pytest.raises(smoke.SmokeError):
        smoke._require_persisted_reply(messages, None, label="本轮")


# ----------------------------------------------------------------------
# 停止失败：不得伪 passed，且必须保留证据
# ----------------------------------------------------------------------
def test_stop_failure_overrides_passed_and_preserves():
    evidence = {"status": "passed"}
    preserve = smoke._finalize_status(evidence, ["停止 nexus-backend 失败"])
    assert evidence["status"] == "failed"
    assert evidence["stop_failures"]
    assert preserve is True


def test_clean_stop_keeps_passed_and_drops_dir():
    evidence = {"status": "passed"}
    preserve = smoke._finalize_status(evidence, [])
    assert evidence["status"] == "passed"
    assert preserve is False


def test_failed_run_preserves_dir():
    evidence = {"status": "failed", "error": "SmokeError"}
    preserve = smoke._finalize_status(evidence, [])
    assert preserve is True


def test_stop_service_raises_on_nonzero(monkeypatch):
    class _Proc:
        returncode = 1
        stderr = "无法确认退出"

    monkeypatch.setattr(
        smoke,
        "_run_services",
        lambda *a, **k: _Proc(),
    )
    with pytest.raises(smoke.SmokeError):
        smoke._stop_service("agentscope", {})


def test_stop_all_collects_failures(monkeypatch):
    calls: list[str] = []

    def fake_stop(service, env):
        calls.append(service)
        if service == "agentscope":
            raise smoke.SmokeError("停止 agentscope 失败")

    monkeypatch.setattr(smoke, "_stop_service", fake_stop)
    failures = smoke._stop_all({})
    assert calls == ["nexus-backend", "agentscope"]
    assert failures == ["停止 agentscope 失败"]


def test_main_skips_without_key(monkeypatch, tmp_path):
    """缺模型凭据只 skipped（退出码 2），绝不伪 passed，也不读取项目 .env。"""
    monkeypatch.delenv("AGENTSCOPE_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_INTERNAL_TOKEN", raising=False)
    monkeypatch.setattr(smoke, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    monkeypatch.setattr(smoke, "load_dotenv", lambda *a, **k: False)
    code = smoke.main([])
    assert code == 2
    files = list(tmp_path.glob("smoke-freeze-*.json"))
    assert files, "skipped 证据必须写入"
    evidence = json.loads(files[0].read_text())
    assert evidence["status"] == "skipped"


def test_assistant_text_joins_text_blocks():
    message = {
        "content": [
            {"type": "text", "text": "a"},
            {"type": "image", "text": "ignored"},
            {"type": "text", "text": "b"},
        ],
    }
    assert smoke._assistant_text(message) == "ab"
    assert smoke._assistant_text({"content": "not-a-list"}) == ""

# -*- coding: utf-8 -*-
"""控制脚本 scripts/services.py 的边界单元测试（不启动真实服务）。

覆盖：精确进程归属、旧 PID 格式拒绝、跨进程锁、终止失败保留、实例不匹配
健康过滤与 status 返回码。真实启动/停止/重启见 smoke_service.py 与
smoke_runtime.py。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

from nexus_agent_service.config import load_config

ROOT = Path(__file__).resolve().parents[2]
SERVICES_PATH = ROOT / "scripts" / "services.py"
MARKER = "nexus_agent_service"


def _load_services():
    spec = importlib.util.spec_from_file_location(
        "nexus_services_control",
        SERVICES_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


services = _load_services()


def _config(tmp_path):
    return load_config(
        env={"AGENTSCOPE_DATA_DIR": str(tmp_path / "agentscope")},
    )


def _spec(config):
    return services.agentscope_spec(config)


def _record(spec, **overrides):
    fields = {
        "pid": 4242,
        "instance_id": "mine",
        "interpreter": "/usr/bin/python3",
        "host": spec.host,
        "port": spec.port,
        "started_at": "2026-01-01T00:00:00",
    }
    fields.update(overrides)
    return services.InstanceRecord(**fields)


def test_process_owned_requires_exact_interpreter_module_nonce(monkeypatch):
    command = "/usr/bin/python3 -m nexus_agent_service --instance-id abc123"
    monkeypatch.setattr(services, "process_command", lambda pid: command)
    assert (
        services.process_owned_by(1, "abc123", "/usr/bin/python3", MARKER)
        is True
    )
    assert (
        services.process_owned_by(1, "other", "/usr/bin/python3", MARKER)
        is False
    )
    assert (
        services.process_owned_by(1, "abc123", "/usr/bin/python", MARKER)
        is False
    )


def test_process_owned_rejects_other_module(monkeypatch):
    command = "/usr/bin/python3 -m nexus_backend --instance-id abc123"
    monkeypatch.setattr(services, "process_command", lambda pid: command)
    assert (
        services.process_owned_by(1, "abc123", "/usr/bin/python3", MARKER)
        is False
    )


def test_process_owned_rejects_substring_only_match(monkeypatch):
    command = (
        "/usr/bin/python3 /tmp/nexus_agent_service_backup.py "
        "--instance-id abc"
    )
    monkeypatch.setattr(services, "process_command", lambda pid: command)
    assert (
        services.process_owned_by(1, "abc", "/usr/bin/python3", MARKER)
        is False
    )


def test_process_owned_rejects_missing_instance_flag(monkeypatch):
    command = "/usr/bin/python3 -m nexus_agent_service"
    monkeypatch.setattr(services, "process_command", lambda pid: command)
    assert (
        services.process_owned_by(1, "abc", "/usr/bin/python3", MARKER)
        is False
    )


def test_read_record_rejects_legacy_integer(tmp_path):
    pid_file = tmp_path / "agentscope.pid"
    pid_file.write_text("12345")
    assert services.read_record(pid_file) is None

    spec = _spec(_config(tmp_path))
    record = _record(spec)
    pid_file.write_text(record.to_json())
    assert services.read_record(pid_file) == record


def test_service_lock_is_exclusive(tmp_path):
    spec = _spec(_config(tmp_path))
    paths = spec.paths()
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with services.service_lock(paths, timeout=5.0):
            entered.set()
            release.wait(5.0)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert entered.wait(5.0)
        with pytest.raises(TimeoutError):
            with services.service_lock(paths, timeout=0.2):
                pass
    finally:
        release.set()
        thread.join(5.0)


def test_stop_refuses_unowned_process_keeps_pid(tmp_path, monkeypatch):
    spec = _spec(_config(tmp_path))
    paths = spec.paths()
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    record = _record(spec)
    paths.pid_file.write_text(record.to_json())
    monkeypatch.setattr(services, "is_alive", lambda pid: True)
    monkeypatch.setattr(services, "record_is_owned", lambda r, m: False)

    assert services.command_stop(spec, paths) == 1
    assert paths.pid_file.exists()
    assert services.read_record(paths.pid_file) == record


def test_terminate_failure_returns_false(tmp_path, monkeypatch):
    spec = _spec(_config(tmp_path))
    record = _record(spec)
    monkeypatch.setattr(services, "STOP_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(services, "is_alive", lambda pid: True)
    monkeypatch.setattr(services, "record_is_owned", lambda r, m: True)
    monkeypatch.setattr(services.os, "kill", lambda pid, sig: None)

    assert services._terminate(record, MARKER) is False


def _fake_http(primary_body, native_body=None, primary_status=200):
    def fake_http_get(url, headers=None, timeout=2.0):
        if url.endswith("/nexus/health"):
            return primary_status, primary_body
        return 200, native_body if native_body is not None else {"status": "ok"}

    return fake_http_get


def test_fetch_report_filters_instance_mismatch(tmp_path, monkeypatch):
    spec = _spec(_config(tmp_path))
    monkeypatch.setattr(
        services,
        "http_get",
        _fake_http({"instance_id": "other", "ready": True}),
    )
    assert services.fetch_report(spec, "mine") is None
    assert services.fetch_report(spec, "other") is not None


def test_wait_for_health_stops_when_child_exited(tmp_path, monkeypatch):
    spec = _spec(_config(tmp_path))

    class _Dead:
        def poll(self):
            return 1

    monkeypatch.setattr(
        services,
        "fetch_report",
        lambda s, i: {
            "primary_status": 200,
            "primary": {"instance_id": i, "ready": True},
            "native_status": 200,
            "native": {"status": "ok"},
        },
    )
    assert services.wait_for_health(spec, 1.0, _Dead(), "mine") is None


def test_wait_for_health_requires_ready(tmp_path, monkeypatch):
    spec = _spec(_config(tmp_path))

    class _Alive:
        def poll(self):
            return None

    monkeypatch.setattr(
        services,
        "fetch_report",
        lambda s, i: {
            "primary_status": 503,
            "primary": {"instance_id": i, "ready": False},
            "native_status": 200,
            "native": {"status": "ok"},
        },
    )
    assert services.wait_for_health(spec, 1.0, _Alive(), "mine") is None


def test_status_legacy_pid_record_refused(tmp_path):
    spec = _spec(_config(tmp_path))
    paths = spec.paths()
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.pid_file.write_text("12345")
    assert services.command_status(spec, paths) == 1


def test_status_instance_mismatch_not_ok(tmp_path, monkeypatch):
    spec = _spec(_config(tmp_path))
    paths = spec.paths()
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.pid_file.write_text(_record(spec).to_json())
    monkeypatch.setattr(services, "record_is_owned", lambda r, m: True)

    monkeypatch.setattr(
        services,
        "http_get",
        _fake_http(
            {"instance_id": "other", "ready": True, "model_configured": False},
        ),
    )
    assert services.command_status(spec, paths) == 1


def test_status_healthy_matching_instance_ok(tmp_path, monkeypatch):
    spec = _spec(_config(tmp_path))
    paths = spec.paths()
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.pid_file.write_text(_record(spec).to_json())
    monkeypatch.setattr(services, "record_is_owned", lambda r, m: True)

    monkeypatch.setattr(
        services,
        "http_get",
        _fake_http(
            {"instance_id": "mine", "ready": True, "model_configured": True},
        ),
    )
    assert services.command_status(spec, paths) == 0


def test_status_not_ready_returns_one(tmp_path, monkeypatch):
    spec, paths = _owned_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        services,
        "http_get",
        _fake_http({"instance_id": "mine", "ready": False}),
    )
    assert services.command_status(spec, paths) == 1


def _healthy_report(instance_id: str = "mine") -> dict:
    return {
        "primary_status": 200,
        "primary": {"instance_id": instance_id, "ready": True},
        "native_status": 200,
        "native": {"status": "ok"},
    }


def _owned_setup(tmp_path, monkeypatch):
    spec = _spec(_config(tmp_path))
    paths = spec.paths()
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.pid_file.write_text(_record(spec).to_json())
    monkeypatch.setattr(services, "record_is_owned", lambda r, m: True)
    return spec, paths


def test_start_existing_owned_healthy_returns_zero(tmp_path, monkeypatch):
    spec, paths = _owned_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        services,
        "fetch_report",
        lambda s, i: _healthy_report(i),
    )

    def _no_restart(*args, **kwargs):
        raise AssertionError("健康实例不得自动重启")

    monkeypatch.setattr(services.subprocess, "Popen", _no_restart)
    assert services.command_start(spec, paths) == 0


def test_start_existing_owned_unhealthy_returns_one_without_restart(
    tmp_path,
    monkeypatch,
):
    spec, paths = _owned_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        services,
        "fetch_report",
        lambda s, i: {
            "primary_status": 503,
            "primary": {"instance_id": i, "ready": True},
            "native_status": 200,
            "native": {"status": "ok"},
        },
    )

    def _no_restart(*args, **kwargs):
        raise AssertionError("不健康实例不得自动重启")

    monkeypatch.setattr(services.subprocess, "Popen", _no_restart)
    assert services.command_start(spec, paths) == 1
    # 保留 PID 记录，不误删。
    assert services.read_record(paths.pid_file) is not None


# ----------------------------------------------------------------------
# 内部秘密（运行 guard 回调鉴权）准备
# ----------------------------------------------------------------------
def test_ensure_internal_token_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN", "given-token")
    token_file = tmp_path / "tok"
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN_FILE", str(token_file))
    assert services.ensure_internal_token() == "given-token"
    # 已导出秘密时不落盘、不产生文件。
    assert not token_file.exists()


def test_ensure_internal_token_file_created_0600_and_reused(
    tmp_path,
    monkeypatch,
):
    token_file = tmp_path / "tok"
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN", "")
    token = services.ensure_internal_token()
    assert token
    assert token_file.exists()
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"
    # 再次调用复用同一秘密（等同另一进程读取同一文件）。
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN", "")
    assert services.ensure_internal_token() == token


def test_ensure_internal_token_persisted_value_reused(tmp_path, monkeypatch):
    token_file = tmp_path / "tok"
    token_file.write_text("persisted-secret")
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN", "")
    assert services.ensure_internal_token() == "persisted-secret"


def test_ensure_internal_token_disk_failure_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN_FILE", str(blocker / "tok"))
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN", "")
    with pytest.raises(services.TokenError):
        services.ensure_internal_token()


def test_status_does_not_create_token_file(tmp_path, monkeypatch, capsys):
    token_file = tmp_path / "status-token"
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN", "")
    # status 未运行返回 1，但不得产生秘密文件（status/stop 不生产秘密）。
    assert services.main(["status", "agentscope"]) == 1
    assert not token_file.exists()


def test_ensure_internal_token_concurrent_same_value(tmp_path, monkeypatch):
    """并发准备共享同一 token：文件锁 + 原子创建保证一致。"""
    import threading

    token_file = tmp_path / "tok"
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("NEXUS_INTERNAL_TOKEN", "")
    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker():
        os.environ.pop("NEXUS_INTERNAL_TOKEN", None)
        barrier.wait()
        results.append(services.ensure_internal_token())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results[0] == results[1]
    assert token_file.stat().st_mode & 0o777 == 0o600


def test_build_child_env_merges_loopback_into_no_proxy(monkeypatch):
    """子进程 env 在既有 NO_PROXY 上补入回环地址，保留用户列表与代理。"""
    child = services.build_child_env(
        {
            "NO_PROXY": "example.com,.internal",
            "HTTP_PROXY": "http://cloud-proxy:8080",
        },
    )
    tokens = [t.strip() for t in child["NO_PROXY"].split(",") if t.strip()]
    assert tokens[0] == "example.com"
    assert ".internal" in tokens
    assert "127.0.0.1" in tokens
    assert "localhost" in tokens
    assert "::1" in tokens
    assert child["no_proxy"] == child["NO_PROXY"]
    # 不覆盖/不删除既有代理设置。
    assert child["HTTP_PROXY"] == "http://cloud-proxy:8080"
    print("no_proxy merge ok")


def test_build_child_env_adds_loopback_when_no_proxy_absent():
    child = services.build_child_env({})
    for host in ("127.0.0.1", "localhost", "::1"):
        assert host in child["NO_PROXY"]

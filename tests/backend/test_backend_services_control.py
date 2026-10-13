# -*- coding: utf-8 -*-
"""Nexus 后端在统一控制脚本 ServiceSpec 下的边界测试（不启动真实进程）。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from nexus_backend.config import load_config

ROOT = Path(__file__).resolve().parents[2]
SERVICES_PATH = ROOT / "scripts" / "services.py"


def _load_services():
    spec = importlib.util.spec_from_file_location(
        "nexus_services_control_backend",
        SERVICES_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


services = _load_services()


def _backend_spec(tmp_path):
    config = load_config(
        env={"NEXUS_BACKEND_DATA_DIR": str(tmp_path / "nexus-backend")},
    )
    return services.backend_spec(config)


def test_backend_spec_shape(tmp_path):
    spec = _backend_spec(tmp_path)
    assert spec.name == "nexus-backend"
    assert spec.module == "nexus_backend"
    assert spec.health_path == "/api/health"
    assert spec.native_health_path is None
    assert spec.pid_filename == "nexus-backend.pid"
    assert spec.base_url == f"http://{spec.host}:{spec.port}"


def test_process_ownership_distinguishes_services(monkeypatch):
    command = "/usr/bin/python3 -m nexus_backend --instance-id abc123"
    monkeypatch.setattr(services, "process_command", lambda pid: command)
    assert (
        services.process_owned_by(1, "abc123", "/usr/bin/python3", "nexus_backend")
        is True
    )
    assert (
        services.process_owned_by(
            1,
            "abc123",
            "/usr/bin/python3",
            "nexus_agent_service",
        )
        is False
    )


def test_build_spec_loads_each_service_independently():
    assert services.build_spec("nexus-backend").name == "nexus-backend"
    assert services.build_spec("agentscope").name == "agentscope"


def test_build_spec_ignores_broken_agentscope_config(monkeypatch):
    def boom():
        raise ValueError("AGENTSCOPE_SERVICE_HOST 非法")

    # 另一个服务（agentscope）配置非法，不得阻塞后端服务的加载。
    monkeypatch.setattr(services, "load_agentscope_config", boom)
    assert services.build_spec("nexus-backend").name == "nexus-backend"


def test_build_spec_ignores_broken_backend_config(monkeypatch):
    def boom():
        raise ValueError("NEXUS_BACKEND_PORT 非法")

    # 后端配置非法不得阻塞 agentscope 服务的加载。
    monkeypatch.setattr(services, "load_backend_config", boom)
    assert services.build_spec("agentscope").name == "agentscope"

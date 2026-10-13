# -*- coding: utf-8 -*-
"""Nexus 后端配置解析与边界单元测试（不启动真实服务）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus_backend.config import (
    DEFAULT_AGENTSCOPE_SERVICE_URL,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_REQUEST_TIMEOUT,
    EXPECTED_AGENTSCOPE_VERSION,
    PROJECT_ROOT,
    load_config,
)


def test_defaults_without_env():
    config = load_config(env={})
    assert config.host == DEFAULT_HOST
    assert config.port == DEFAULT_PORT
    assert config.internal_user_id == "nexus-service"
    assert config.agentscope_service_url == DEFAULT_AGENTSCOPE_SERVICE_URL
    assert config.request_timeout == DEFAULT_REQUEST_TIMEOUT
    assert config.connect_timeout == DEFAULT_CONNECT_TIMEOUT
    assert config.expected_agentscope_version == EXPECTED_AGENTSCOPE_VERSION
    assert config.data_dir == (PROJECT_ROOT / "data" / "nexus-backend").resolve()
    assert config.base_url == f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def test_env_overrides_and_relative_data_dir():
    config = load_config(
        env={
            "NEXUS_BACKEND_PORT": "9100",
            "NEXUS_BACKEND_DATA_DIR": "data/custom-backend",
            "AGENTSCOPE_SERVICE_URL": "http://127.0.0.1:9999/",
            "AGENTSCOPE_INTERNAL_USER_ID": "tenant-x",
            "NEXUS_BACKEND_REQUEST_TIMEOUT": "12.5",
            "NEXUS_BACKEND_CONNECT_TIMEOUT": "2",
        },
    )
    assert config.port == 9100
    assert config.data_dir == (PROJECT_ROOT / "data" / "custom-backend").resolve()
    # 尾部斜杠被归一化，避免拼接出双斜杠路径。
    assert config.agentscope_service_url == "http://127.0.0.1:9999"
    assert config.internal_user_id == "tenant-x"
    assert config.request_timeout == 12.5
    assert config.connect_timeout == 2.0


def test_absolute_data_dir():
    config = load_config(env={"NEXUS_BACKEND_DATA_DIR": "/tmp/nexus-backend"})
    assert config.data_dir == Path("/tmp/nexus-backend").resolve()


@pytest.mark.parametrize("port", ["not-a-port", "0", "-1", "65536"])
def test_invalid_port_rejected(port):
    with pytest.raises(ValueError):
        load_config(env={"NEXUS_BACKEND_PORT": port})


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com"])
def test_non_loopback_host_rejected(host):
    with pytest.raises(ValueError):
        load_config(env={"NEXUS_BACKEND_HOST": host})


@pytest.mark.parametrize(
    "url",
    ["ftp://127.0.0.1:8766", "http://example.com:8766", "http://10.0.0.1:8766"],
)
def test_non_loopback_service_url_rejected(url):
    with pytest.raises(ValueError):
        load_config(env={"AGENTSCOPE_SERVICE_URL": url})


def test_localhost_and_ipv6_service_url_allowed():
    assert (
        load_config(env={"AGENTSCOPE_SERVICE_URL": "http://localhost:8766"})
        .agentscope_service_url
        == "http://localhost:8766"
    )
    assert (
        load_config(env={"AGENTSCOPE_SERVICE_URL": "http://[::1]:8766"})
        .agentscope_service_url
        == "http://[::1]:8766"
    )


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_timeout_rejected(value):
    with pytest.raises(ValueError):
        load_config(env={"NEXUS_BACKEND_REQUEST_TIMEOUT": value})

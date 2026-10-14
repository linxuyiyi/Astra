# -*- coding: utf-8 -*-
"""配置解析与边界单元测试（不启动真实服务）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus_agent_service.config import (
    DEFAULT_CREDENTIAL_ID,
    DEFAULT_HOST,
    DEFAULT_MODEL_NAME,
    DEFAULT_PORT,
    PROJECT_ROOT,
    load_config,
    load_project_env,
)


def test_defaults_without_env():
    config = load_config(env={})
    assert config.host == DEFAULT_HOST
    assert config.port == DEFAULT_PORT
    assert config.internal_user_id == "nexus-service"
    assert config.model_name == DEFAULT_MODEL_NAME
    assert config.credential_id == DEFAULT_CREDENTIAL_ID
    assert config.model_configured is False
    assert config.data_dir == (PROJECT_ROOT / "data" / "agentscope").resolve()
    assert config.database_url.startswith("sqlite+aiosqlite:///")
    assert config.base_url == f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def test_env_overrides_and_relative_data_dir(tmp_path):
    config = load_config(
        env={
            "AGENTSCOPE_SERVICE_HOST": "127.0.0.1",
            "AGENTSCOPE_SERVICE_PORT": "9001",
            "AGENTSCOPE_DATA_DIR": "data/custom",
            "AGENTSCOPE_MODEL_API_KEY": "  secret-value  ",
            "AGENTSCOPE_MODEL_NAME": "deepseek-flash",
        },
    )
    assert config.port == 9001
    assert config.data_dir == (PROJECT_ROOT / "data" / "custom").resolve()
    # 密钥被 strip，但不参与任何公开返回。
    assert config.model_api_key == "secret-value"
    assert config.model_configured is True


def test_absolute_data_dir():
    config = load_config(env={"AGENTSCOPE_DATA_DIR": "/tmp/nexus-abs"})
    assert config.data_dir == Path("/tmp/nexus-abs").resolve()


def test_invalid_port_rejected():
    with pytest.raises(ValueError):
        load_config(env={"AGENTSCOPE_SERVICE_PORT": "not-a-port"})


@pytest.mark.parametrize("port", ["0", "-1", "65536", "70000"])
def test_out_of_range_port_rejected(port):
    with pytest.raises(ValueError):
        load_config(env={"AGENTSCOPE_SERVICE_PORT": port})


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com"])
def test_non_loopback_host_rejected(host):
    with pytest.raises(ValueError):
        load_config(env={"AGENTSCOPE_SERVICE_HOST": host})


def test_loopback_hosts_allowed():
    assert load_config(env={"AGENTSCOPE_SERVICE_HOST": "localhost"}).host == (
        "localhost"
    )
    ipv6 = load_config(env={"AGENTSCOPE_SERVICE_HOST": "::1"})
    assert ipv6.host == "::1"
    assert ipv6.base_url == "http://[::1]:8766"


def test_blank_key_means_unconfigured():
    config = load_config(env={"AGENTSCOPE_MODEL_API_KEY": "   "})
    assert config.model_configured is False


def test_model_parameters_default_empty_keeps_official_default():
    config = load_config(env={"AGENTSCOPE_MODEL_API_KEY": "k"})
    assert config.default_chat_model_config()["parameters"] == {}


def test_model_parameters_json_parsed_into_default_config():
    config = load_config(
        env={
            "AGENTSCOPE_MODEL_API_KEY": "k",
            "AGENTSCOPE_MODEL_PARAMETERS": '{"max_tokens": 1024, "thinking": false}',
        },
    )
    assert config.default_chat_model_config()["parameters"] == {
        "max_tokens": 1024,
        "thinking": False,
    }


@pytest.mark.parametrize("raw", ["[1,2]", "not-json", '"text"', "42"])
def test_invalid_model_parameters_rejected(raw):
    with pytest.raises(ValueError):
        load_config(env={"AGENTSCOPE_MODEL_PARAMETERS": raw})



def test_default_chat_model_config_has_no_secret():
    config = load_config(env={"AGENTSCOPE_MODEL_API_KEY": "super-secret"})
    payload = config.default_chat_model_config()
    assert payload == {
        "type": "deepseek_chat",
        "credential_id": DEFAULT_CREDENTIAL_ID,
        "model": DEFAULT_MODEL_NAME,
        "parameters": {},
    }
    assert "super-secret" not in repr(payload)


def test_api_key_hidden_from_config_repr():
    config = load_config(env={"AGENTSCOPE_MODEL_API_KEY": "super-secret"})
    assert "super-secret" not in repr(config)


def test_load_project_env_does_not_invent_values(monkeypatch, tmp_path):
    # 不存在 .env 时不得改变环境。
    monkeypatch.delenv("AGENTSCOPE_MODEL_API_KEY", raising=False)
    load_project_env(tmp_path)
    config = load_config(env={})
    assert config.model_configured is False


def test_knowledge_dirs_derive_from_data_dir():
    """未显式配置时，vector/blob 目录必须从实际 AGENTSCOPE_DATA_DIR 派生。"""
    config = load_config(
        env={
            "AGENTSCOPE_DATA_DIR": "/tmp/nexus-isolated",
            "AGENTSCOPE_KNOWLEDGE_ENABLED": "true",
        },
    )
    data_dir = Path("/tmp/nexus-isolated").resolve()
    assert config.knowledge_enabled is True
    assert config.knowledge_vector_dir == data_dir / "knowledge" / "vector"
    assert config.knowledge_blob_dir == data_dir / "knowledge" / "blobs"
    # 隔离实例绝不落入项目默认 data。
    assert PROJECT_ROOT not in config.knowledge_vector_dir.parents


def test_embedding_config_parsed_and_defaults():
    config = load_config(env={})
    assert config.embedding_model == "embeddinggemma:300m"
    assert config.embedding_dimensions == 768
    assert config.embedding_credential_id == "nexus-default-ollama-embedding"

    override = load_config(
        env={
            "AGENTSCOPE_EMBEDDING_HOST": "http://127.0.0.1:11435",
            "AGENTSCOPE_EMBEDDING_MODEL": "m",
            "AGENTSCOPE_EMBEDDING_DIMENSIONS": "1024",
            "AGENTSCOPE_EMBEDDING_CREDENTIAL_ID": "cid",
        },
    )
    assert override.embedding_host == "http://127.0.0.1:11435"
    assert override.embedding_model == "m"
    assert override.embedding_dimensions == 1024
    assert override.embedding_credential_id == "cid"


@pytest.mark.parametrize("raw", ["0", "-3", "abc"])
def test_invalid_embedding_dimensions_rejected(raw):
    with pytest.raises(ValueError):
        load_config(env={"AGENTSCOPE_EMBEDDING_DIMENSIONS": raw})

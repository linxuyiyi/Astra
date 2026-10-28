from __future__ import annotations

import pytest

from nr_version_mcp.config import ConfigError, Settings, load_settings


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "NR_API_BASE_URL": "http://127.0.0.1:8000/api/v1",
        "NR_SESSION_COOKIE": "sessionid=secret-value",
    }
    env.update(overrides)
    return env


def test_default_status_scope_is_released() -> None:
    settings = load_settings(_env())

    assert settings.default_statuses == ("RELEASED",)
    assert settings.base_url == "http://127.0.0.1:8000/api/v1"


def test_default_statuses_can_be_overridden() -> None:
    settings = load_settings(_env(NR_DEFAULT_STATUSES="RELEASED, draft"))

    assert settings.default_statuses == ("RELEASED", "DRAFT")


def test_base_url_must_be_loopback() -> None:
    with pytest.raises(ConfigError, match="回环"):
        load_settings(_env(NR_API_BASE_URL="https://nr.example.com/api/v1"))

    with pytest.raises(ConfigError, match="回环"):
        load_settings(_env(NR_API_BASE_URL="http://10.0.0.8:8000/api/v1"))


def test_cookie_is_required() -> None:
    with pytest.raises(ConfigError, match="NR_SESSION_COOKIE"):
        load_settings(_env(NR_SESSION_COOKIE=""))


def test_bare_cookie_value_uses_configured_cookie_name() -> None:
    settings = load_settings(_env(NR_SESSION_COOKIE="abc123", NR_SESSION_COOKIE_NAME="sessionid"))

    assert settings.session_cookie == "sessionid=abc123"


def test_full_cookie_header_is_preserved_and_hidden_from_repr() -> None:
    settings = load_settings(_env(NR_SESSION_COOKIE="sessionid=abc123; theme=dark"))

    assert settings.session_cookie == "sessionid=abc123; theme=dark"
    assert "abc123" not in repr(settings)


def test_cookie_rejects_newlines() -> None:
    with pytest.raises(ConfigError, match="换行"):
        load_settings(_env(NR_SESSION_COOKIE="sessionid=abc\nInjected: value"))


def test_invalid_default_status_is_rejected() -> None:
    with pytest.raises(ConfigError, match="未知状态"):
        load_settings(_env(NR_DEFAULT_STATUSES="RELEASED,UNKNOWN"))


def test_settings_repr_never_exposes_cookie() -> None:
    settings = Settings(
        base_url="http://127.0.0.1:8000/api/v1",
        session_cookie="sessionid=top-secret",
    )

    assert "top-secret" not in repr(settings)

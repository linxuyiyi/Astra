import pytest

from nr_version_mcp.config import (
    API_BASE_URL_ENV,
    SESSION_COOKIE_ENV,
    TIMEOUT_ENV,
    ConfigurationError,
    Settings,
)


def test_settings_defaults():
    settings = Settings.from_env({SESSION_COOKIE_ENV: "sessionid=abc"})

    assert settings.api_base_url == "http://127.0.0.1:8000/api/v1"
    assert settings.timeout_seconds == 15.0


def test_settings_normalizes_trailing_slash():
    settings = Settings.from_env(
        {
            API_BASE_URL_ENV: "https://nr.example.test/api/v1/",
            SESSION_COOKIE_ENV: "sessionid=abc",
            TIMEOUT_ENV: "7.5",
        }
    )

    assert settings.api_base_url == "https://nr.example.test/api/v1"
    assert settings.timeout_seconds == 7.5


def test_missing_session_cookie_is_configuration_error():
    with pytest.raises(ConfigurationError) as exc_info:
        Settings.from_env({})

    assert SESSION_COOKIE_ENV in exc_info.value.message


def test_invalid_base_url_is_configuration_error():
    with pytest.raises(ConfigurationError):
        Settings.from_env(
            {
                API_BASE_URL_ENV: "ftp://example.test/api",
                SESSION_COOKIE_ENV: "sessionid=abc",
            }
        )


@pytest.mark.parametrize("timeout", ["0", "-1", "abc"])
def test_invalid_timeout_is_configuration_error(timeout):
    with pytest.raises(ConfigurationError):
        Settings.from_env(
            {
                SESSION_COOKIE_ENV: "sessionid=abc",
                TIMEOUT_ENV: timeout,
            }
        )


def test_settings_repr_does_not_expose_cookie():
    settings = Settings.from_env({SESSION_COOKIE_ENV: "sessionid=super-secret"})

    assert "super-secret" not in repr(settings)

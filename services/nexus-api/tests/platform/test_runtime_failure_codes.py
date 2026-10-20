from pathlib import Path

from app.config.settings import Settings
from app.executors.opencode_server import OpenCodeSessionNotFound


def settings() -> Settings:
    return Settings(
        executor_type="opencode_server",
        opencode_model="opencode/glm-5.1",
        opencode_timeout_seconds=2,
        seed_demo=False,
    )


def test_every_failure_has_one_shared_policy():
    from app.runtime.failure import FailureCode, failure_for

    for code in FailureCode:
        failure = failure_for(code, "raw detail")
        assert failure.failure_code == code
        assert failure.failure_scope
        assert failure.recovery_action
        assert failure.raw_error == "raw detail"
    assert failure_for(FailureCode.SESSION_NOT_FOUND).recovery_action == "CREATE_SESSION"
    assert not failure_for(FailureCode.USER_CANCELLED).retryable


async def test_http_failures_are_typed_without_message_matching(monkeypatch):
    import httpx
    import pytest

    from app.executors.errors import OpenCodeExecutionError, OpenCodeServerUnavailable
    from app.executors.opencode_server import OpenCodeServerClient

    original = httpx.AsyncClient
    for status_code, expected in [
        (404, OpenCodeSessionNotFound),
        (503, OpenCodeServerUnavailable),
        (400, OpenCodeExecutionError),
    ]:
        transport = httpx.MockTransport(
            lambda request, status_code=status_code: httpx.Response(status_code, text="opaque error")
        )
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda transport=transport, **kwargs: original(transport=transport, **kwargs),
        )
        with pytest.raises(expected):
            await OpenCodeServerClient(settings()).session(Path("/tmp"), "ses-typed")

import asyncio
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from app.config.settings import Settings
from app.executors.opencode_server import OpenCodeServerClient
from app.models import Task
from app.runtime.opencode_server import OpenCodeServerManager


def test_control_client_timeout_is_bounded_and_separate_from_execution():
    settings = Settings()
    client = OpenCodeServerClient(settings)

    assert client._timeout_seconds == settings.opencode_control_timeout_seconds
    assert client._timeout_seconds < settings.opencode_timeout_seconds


def test_exact_version_magic_value_is_rejected():
    with pytest.raises(ValueError, match="0.0.0"):
        Settings(seed_demo=False, opencode_server_version="0.0.0")


def test_version_check_accepts_only_the_supported_release():
    strict = OpenCodeServerManager(Settings(seed_demo=False, opencode_server_version="1.18.33"))
    assert strict.version_error("1.18.33") is None
    assert strict.version_error("v1.18.33") is None
    assert strict.version_error("1.0.0") is not None
    assert strict.version_error("1.2.27") is not None
    assert strict.version_error("v1.2.27") is not None
    assert strict.version_error("1.18.25") is not None
    assert strict.version_error("1.18.35") is not None
    assert strict.version_error(None) is not None
    assert strict.version_error("") is not None

    relaxed = OpenCodeServerManager(
        Settings(
            seed_demo=False,
            opencode_server_version="1.18.33",
            opencode_server_version_check_enabled=False,
        )
    )
    assert relaxed.version_error("1.0.0") is None
    assert relaxed.version_error(None) is None


class FakeServerClient:
    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        session_id: str = "ses_main",
        final_messages: list[dict[str, Any]] | None = None,
    ):
        self.event_items = events
        self.session_id = session_id
        self.final_messages = final_messages or []
        self.message_calls = 0
        self.prompts: list[tuple[str, str]] = []
        self.question_replies: list[tuple[str, list[list[str]]]] = []
        self.aborted: list[str] = []
        self.directories: list[Path | PurePosixPath] = []

    async def health(self):
        return {"healthy": True, "version": "test"}

    async def events(self, directory):
        self.directories.append(directory)
        for event in self.event_items:
            yield event
            await asyncio.sleep(0)
        await asyncio.Event().wait()

    async def create_session(self, directory, **_kwargs):
        self.directories.append(directory)
        return {"id": self.session_id}

    async def session(self, _directory, session_id):
        assert session_id == self.session_id
        return {"id": session_id}

    async def prompt(self, directory, session_id, *, text, **_kwargs):
        self.directories.append(directory)
        self.prompts.append((session_id, text))

    async def messages(self, _directory, _session_id):
        self.message_calls += 1
        return [] if self.message_calls == 1 else self.final_messages

    async def permissions(self, _directory):
        return []

    async def reply_question(self, _directory, request_id, answers):
        self.question_replies.append((request_id, answers))
        return True

    async def reply_permission(self, _directory, _request_id, _reply):
        return True

    async def abort(self, _directory, session_id):
        self.aborted.append(session_id)
        return True

    async def status(self, _directory, _session_id):
        return "idle"


def settings(**values) -> Settings:
    defaults = {
        "executor_type": "opencode_server",
        "opencode_model": "opencode/glm-5.1",
        "opencode_timeout_seconds": 2,
        "seed_demo": False,
    }
    defaults.update(values)
    return Settings(
        **defaults,
    )


def task(**values) -> Task:
    defaults = {
        "id": "ASTRA-SERVER-TEST",
        "source": "manual",
        "task_type": "ISSUE",
        "title": "Server runtime test",
        "description": "Use real structured events",
        "acceptance_criteria": [{"id": "AC-01", "content": "done"}],
    }
    defaults.update(values)
    return Task(**defaults)


def raw_event(event_type: str, **properties) -> dict[str, Any]:
    return {"type": event_type, "properties": properties}


async def _append(items: list[Any], value: Any) -> None:
    items.append(value)


async def test_messages_page_uses_native_opaque_cursor(monkeypatch):
    import httpx

    real_client = httpx.AsyncClient
    calls = []

    def respond(request):
        calls.append(request.url.params)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json=[{"info": {"id": "msg_latest"}, "parts": []}],
                headers={"x-next-cursor": "opaque+/cursor="},
            )
        assert request.url.params["before"] == "opaque+/cursor="
        return httpx.Response(200, json=[{"info": {"id": "msg_early"}, "parts": []}])

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            **kwargs,
            transport=httpx.MockTransport(respond),
            trust_env=False,
        ),
    )
    client = OpenCodeServerClient(Settings(), base_url="http://localhost:1")
    first = await client.messages_page(Path("/workspace"), "ses_a", limit=1)
    second = await client.messages_page(Path("/workspace"), "ses_a", limit=1, before=first["next_before"])
    assert first["next_before"] == "opaque+/cursor="
    assert second["next_before"] is None  # Full-sized last pages do not imply another page.
    assert calls[0]["limit"] == "1" and calls[0]["directory"] == "/workspace"


async def test_direct_client_accepts_task_submission_request_id(monkeypatch):
    from unittest.mock import AsyncMock

    client = OpenCodeServerClient(Settings())
    exchange = AsyncMock(return_value=(204, b"", {}))
    monkeypatch.setattr(client, "exchange", exchange)
    await client.request(
        "POST",
        "/session/ses_local/prompt_async",
        command_request_id="submit_current",
        body={"messageID": "msg_current"},
    )
    assert exchange.await_count == 1

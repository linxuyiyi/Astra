import asyncio
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from app.config.settings import Settings
from app.executors.opencode_remote_server import (
    RemoteOpenCodeServerConnection,
    RemoteOpenCodeServerExecutor,
)
from app.executors.opencode_server import (
    OpenCodeServerClient,
    OpenCodeServerExecutor,
)
from app.models import OpenCodeExecutionEnvironment, Task
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
    strict = OpenCodeServerManager(Settings(seed_demo=False))
    assert strict.version_error("1.18.35") is None
    assert strict.version_error("v1.18.35") is None
    assert strict.version_error("1.0.0") is not None
    assert strict.version_error("1.2.27") is not None
    assert strict.version_error("v1.2.27") is not None
    assert strict.version_error("1.18.25") is not None
    assert strict.version_error("1.18.33") is not None
    assert strict.version_error(None) is not None
    assert strict.version_error("") is not None

    relaxed = OpenCodeServerManager(
        Settings(seed_demo=False, opencode_server_version_check_enabled=False)
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
        "review_history": [],
    }
    defaults.update(values)
    return Task(**defaults)


def raw_event(event_type: str, **properties) -> dict[str, Any]:
    return {"type": event_type, "properties": properties}


async def test_server_executor_streams_tools_messages_and_usage(tmp_path: Path):
    client = FakeServerClient(
        [
            raw_event("server.connected"),
            raw_event(
                "message.part.updated",
                part={
                    "id": "prt_tool",
                    "sessionID": "ses_main",
                    "type": "tool",
                    "tool": "bash",
                    "callID": "call_1",
                    "state": {
                        "status": "completed",
                        "title": "Run pwd",
                        "input": {"command": "pwd"},
                        "output": str(tmp_path),
                    },
                },
            ),
            raw_event(
                "message.updated",
                info={
                    "id": "msg_assistant",
                    "sessionID": "ses_main",
                    "role": "assistant",
                    "providerID": "opencode",
                    "modelID": "glm-5.1",
                    "tokens": {"input": 12, "output": 3, "cache": {"read": 2, "write": 1}},
                },
            ),
            raw_event(
                "message.part.updated",
                part={"id": "prt_text", "sessionID": "ses_main", "type": "text", "text": "DONE"},
            ),
            raw_event("session.idle", sessionID="ses_main"),
        ],
        final_messages=[
            {
                "info": {"id": "msg_assistant", "role": "assistant"},
                "parts": [{"type": "text", "text": "DONE"}],
            }
        ],
    )
    observed: list[dict[str, Any]] = []
    sessions: list[str] = []
    executor = OpenCodeServerExecutor(
        settings(),
        client=client,
        event_callback=lambda event: _append(observed, event),
        session_callback=lambda session_id: _append(sessions, session_id),
    )

    result = await executor.start(task(), tmp_path)

    assert result.status == "SUCCEEDED"
    assert result.session_id == "ses_main"
    assert sessions == ["ses_main"]
    assert any(item.get("part", {}).get("tool") == "bash" for item in observed)
    assert result.token_usage.total_tokens == 18
    assert result.tool_usage[0].name == "bash"
    assert client.prompts and client.prompts[0][0] == "ses_main"
    assert "Server runtime test" in client.prompts[0][1]
    assert "ASTRA_RESULT_JSON" not in client.prompts[0][1]


async def test_server_executor_blocks_on_native_question_and_replies_in_same_session(
    tmp_path: Path,
):
    first_client = FakeServerClient(
        [
            raw_event("server.connected"),
            raw_event(
                "question.asked",
                id="que_1",
                sessionID="ses_main",
                questions=[
                    {
                        "question": "Which marker?",
                        "options": [
                            {"label": "BLUE", "description": "Print blue"},
                            {"label": "GREEN", "description": "Print green"},
                        ],
                    }
                ],
            ),
        ]
    )
    current_task = task()
    first = await OpenCodeServerExecutor(settings(), client=first_client).start(
        current_task,
        tmp_path,
    )

    assert first.status == "WAITING_INPUT"
    assert first.inquiry and first.inquiry["runtime_request_id"] == "que_1"
    current_task.human_intervention = {**first.inquiry, "response": "BLUE"}
    second_client = FakeServerClient(
        [
            raw_event("server.connected"),
            raw_event("question.replied", id="que_1", sessionID="ses_main"),
            raw_event(
                "message.part.updated",
                part={
                    "id": "prt_answer",
                    "sessionID": "ses_main",
                    "type": "text",
                    "text": "QUESTION_RESUMED:BLUE",
                },
            ),
            raw_event("session.idle", sessionID="ses_main"),
        ],
        final_messages=[
            {
                "info": {"id": "msg_answer", "role": "assistant"},
                "parts": [{"type": "text", "text": "QUESTION_RESUMED:BLUE"}],
            }
        ],
    )

    resumed = await OpenCodeServerExecutor(settings(), client=second_client)._execute(
        current_task,
        tmp_path,
        session_id="ses_main",
    )

    assert resumed.status == "SUCCEEDED"
    assert second_client.question_replies == [("que_1", [["BLUE"]])]
    assert second_client.prompts == []


async def test_server_executor_observes_child_session_internal_tools(tmp_path: Path):
    client = FakeServerClient(
        [
            raw_event("server.connected"),
            raw_event(
                "session.created",
                info={
                    "id": "ses_child",
                    "sessionID": "ses_child",
                    "parentID": "ses_main",
                    "agent": "explore",
                },
            ),
            raw_event(
                "message.part.updated",
                part={
                    "id": "prt_child_tool",
                    "sessionID": "ses_child",
                    "type": "tool",
                    "tool": "grep",
                    "callID": "call_child",
                    "state": {"status": "completed", "title": "Search files"},
                },
            ),
            raw_event(
                "message.part.updated",
                part={"id": "prt_done", "sessionID": "ses_main", "type": "text", "text": "DONE"},
            ),
            raw_event("session.idle", sessionID="ses_main"),
        ]
    )
    observed: list[dict[str, Any]] = []
    executor = OpenCodeServerExecutor(
        settings(),
        client=client,
        event_callback=lambda event: _append(observed, event),
    )

    result = await executor.start(task(), tmp_path)

    assert result.status == "SUCCEEDED"
    child_tool = next(item for item in observed if item.get("part", {}).get("callID") == "call_child")
    assert child_tool["agent"] == "explore"
    assert child_tool["part"]["agent"] == "explore"


async def test_server_timeout_aborts_and_fails_instead_of_requesting_human(tmp_path: Path):
    client = FakeServerClient([raw_event("server.connected")])
    executor = OpenCodeServerExecutor(
        settings(opencode_timeout_seconds=0),
        client=client,
    )

    result = await executor.start(task(), tmp_path)

    assert result.status == "FAILED"
    assert result.exit_code == 124
    assert result.inquiry is None
    assert client.aborted == ["ses_main"]


def remote_environment() -> OpenCodeExecutionEnvironment:
    return OpenCodeExecutionEnvironment(
        employee_id="remote-agent",
        mode="LINUX_REMOTE",
        name="Remote Linux",
        host="linux.internal",
        port=22,
        username="astra",
        ssh_password="secret",
        opencode_server_port=4196,
        opencode_path="/opt/OpenCode/opencode",
        model_name="opencode/glm-5.1",
        workspace_root="/srv/projects/NEXUS 工作区",
    )


async def test_unmarked_remote_connection_does_not_open_ssh():
    opened = False

    class Runtime:
        async def open_session(self, _environment):
            nonlocal opened
            opened = True
            raise AssertionError("ssh")

    with pytest.raises(RuntimeError, match="尚未安装 Worker"):
        async with RemoteOpenCodeServerConnection(remote_environment(), settings(), Runtime()):
            pass
    assert opened is False


async def test_remote_executor_uses_remote_posix_workspace_and_structured_events(tmp_path: Path):
    from contextlib import asynccontextmanager

    client = FakeServerClient(
        [
            raw_event("server.connected"),
            raw_event(
                "message.part.updated",
                part={
                    "id": "prt_remote",
                    "sessionID": "ses_remote",
                    "type": "text",
                    "text": "REMOTE_DONE",
                },
            ),
            raw_event("session.idle", sessionID="ses_remote"),
        ],
        session_id="ses_remote",
    )
    executor = RemoteOpenCodeServerExecutor(remote_environment(), settings())

    @asynccontextmanager
    async def connection():
        yield client

    executor.connection = connection
    result = await executor.start(task(id="ASTRA-REMOTE-SERVER"), tmp_path)

    assert result.status == "SUCCEEDED"
    assert result.session_id == "ses_remote"
    assert client.directories
    assert all(directory == PurePosixPath("/srv/projects/NEXUS 工作区") for directory in client.directories)


async def _append(items: list[Any], value: Any) -> None:
    items.append(value)


async def test_messages_page_uses_native_opaque_cursor(monkeypatch):
    import httpx

    real_client = httpx.AsyncClient
    calls = []

    def respond(request):
        calls.append(request.url.params)
        if len(calls) == 1:
            return httpx.Response(200, json=[{"info": {"id": "msg_latest"}, "parts": []}],
                                  headers={"x-next-cursor": "opaque+/cursor="})
        assert request.url.params["before"] == "opaque+/cursor="
        return httpx.Response(200, json=[{"info": {"id": "msg_early"}, "parts": []}])

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(respond), trust_env=False,
    ))
    client = OpenCodeServerClient(Settings(), base_url="http://localhost:1")
    first = await client.messages_page(Path("/workspace"), "ses_a", limit=1)
    second = await client.messages_page(Path("/workspace"), "ses_a", limit=1, before=first["next_before"])
    assert first["next_before"] == "opaque+/cursor="
    assert second["next_before"] is None  # Full-sized last pages do not imply another page.
    assert calls[0]["limit"] == "1" and calls[0]["directory"] == "/workspace"

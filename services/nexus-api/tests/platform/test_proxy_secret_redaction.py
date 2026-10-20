"""Secret redaction for Workspace SSE, lists, field names, and error bodies."""

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.api.workspace_routes import (
    load_proxy_secrets,
    native_events,
    redact_secret_fields,
    redact_upstream_body,
)

AUTH = "canary-authorization-value"
XAPI = "canary-x-api-key-value"
APIKEY = "canary-options-api-key-value"
NESTED = "canary-nested-secret-value"
XAUTH = "canary-x-auth-token-value"
XGOOG = "canary-x-goog-api-key-value"
ACCESS = "canary-access-token-value"
REFRESH = "canary-refresh-token-value"
CLIENT = "canary-client-secret-value"
COOKIE = "canary-session-cookie-value"
PASSWORD = "canary-db-password-value"
VALUE = "canary-remote-config-value"
FIELD_CANARIES = (AUTH, XAPI, APIKEY, NESTED, XAUTH, XGOOG, ACCESS, REFRESH, CLIENT, COOKIE, PASSWORD)


def _tool_event() -> dict:
    return {
        "id": "evt_tool",
        "type": "message.part.updated",
        "properties": {
            "sessionID": "ses",
            "part": {
                "id": "prt_tool",
                "sessionID": "ses",
                "messageID": "msg_1",
                "type": "tool",
                "callID": "call_1",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {
                        "command": f"curl -H 'Authorization: Bearer {VALUE}'",
                        "request": {
                            "headers": {
                                "authorization": AUTH,
                                "x-api-key": XAPI,
                                "x-auth-token": XAUTH,
                                "x-goog-api-key": XGOOG,
                                "accept": "application/json",
                            },
                            "body": {"options": {"apiKey": APIKEY}, "model": "keep-model"},
                        },
                        "nested": {"one": {"two": {"secret": NESTED, "name": "kept"}}},
                        "access_token": ACCESS,
                        "refresh-token": REFRESH,
                        "client_secret": CLIENT,
                        "session_cookie": COOKIE,
                        "db_password": PASSWORD,
                        "copied": VALUE,
                        "args": ["keep-arg", VALUE, ["inner", VALUE]],
                        "tokens": {
                            "input": 12,
                            "output": 3,
                            "reasoning": 1,
                            "cache": {"read": 2, "write": 4},
                        },
                        "input_tokens": 12,
                        "output_tokens": 3,
                        "total_tokens": 15,
                        "prompt_tokens": 12,
                        "cache_read_tokens": 2,
                        "setCacheKey": True,
                        "authorizationUrl": "https://example.test/oauth",
                    },
                    "output": f"echo {VALUE}",
                    "title": "Run pwd",
                    "metadata": {},
                    "time": {"start": 1, "end": 2},
                },
            },
            "time": 3,
        },
    }


def _prompt_event() -> dict:
    return {
        "id": "evt_prompt",
        "type": "message.part.updated",
        "properties": {
            "sessionID": "ses",
            "part": {
                "id": "prt_text",
                "sessionID": "ses",
                "messageID": "msg_user",
                "type": "text",
                "text": f"user prompt {VALUE} please",
            },
            "time": 4,
        },
    }


def _gap_event() -> dict:
    return {
        "type": "astra.event_gap",
        "directory": "/srv/worker/task",
        "reason": "worker_reconnected",
        "detail": f"lost {VALUE}",
    }


def _usage_event() -> dict:
    return {
        "id": "evt_usage",
        "type": "message.updated",
        "properties": {
            "sessionID": "ses",
            "info": {
                "id": "msg_1",
                "sessionID": "ses",
                "role": "assistant",
                "cost": 0.01,
                "tokens": {
                    "input": 12,
                    "output": 3,
                    "reasoning": 1,
                    "cache": {"read": 2, "write": 4},
                },
                "input_tokens": 12,
                "output_tokens": 3,
            },
        },
    }


class _RemoteClient:
    def __init__(self, events):
        self._events = events
        self.config_reads = 0

    async def children(self, *_args, **_kwargs):
        return []

    async def events(self, _directory):
        for event in self._events:
            yield copy.deepcopy(event)

    async def exchange(self, method, path, *, params=None, body=None, session_id=None, task_id=None):
        assert method == "GET"
        assert params["directory"] == "/srv/worker/task"
        self.config_reads += 1
        if path == "/config":
            payload = {"provider": {"nexus-gateway": {"options": {"apiKey": VALUE}}}}
            return 200, json.dumps(payload).encode(), {"content-type": "application/json"}
        return 404, b"{}", {}


def _unwrap(chunk: str, global_stream: bool) -> dict:
    assert chunk.startswith("data: ")
    payload = json.loads(chunk.removeprefix("data: ").strip())
    return payload["payload"] if global_stream else payload


@pytest.mark.asyncio
@pytest.mark.parametrize("global_stream", [False, True])
async def test_sse_redacts_tool_part_canaries_on_both_event_modes(monkeypatch, global_stream):
    client = _RemoteClient([_tool_event(), _prompt_event(), _usage_event(), _gap_event()])
    environment = SimpleNamespace(mode="LINUX_REMOTE", id=7, worker_channel="worker")

    @asynccontextmanager
    async def fake_connect(*_args, **_kwargs):
        yield client

    monkeypatch.setattr("app.api.workspace_routes.connect", fake_connect)
    chunks = [
        chunk
        async for chunk in native_events(environment, "/srv/worker/task", "ses", global_stream)
    ]
    assert client.config_reads >= 1
    raw = "".join(chunks)
    for canary in (*FIELD_CANARIES, VALUE):
        assert canary not in raw
    events = [_unwrap(chunk, global_stream) for chunk in chunks]
    tool = next(
        item
        for item in events
        if item.get("type") == "message.part.updated" and item["properties"]["part"]["type"] == "tool"
    )
    prompt = next(
        item
        for item in events
        if item.get("type") == "message.part.updated" and item["properties"]["part"]["type"] == "text"
    )
    usage = next(item for item in events if item.get("type") == "message.updated")
    gap = next(item for item in events if item.get("type") == "astra.event_gap")
    assert tool["type"] == "message.part.updated"
    state = tool["properties"]["part"]["state"]
    state_input = state["input"]
    assert state_input["command"] == "curl -H 'Authorization: Bearer [redacted]'"
    assert state["output"] == "echo [redacted]"
    assert prompt["properties"]["part"]["text"] == "user prompt [redacted] please"
    assert gap["detail"] == "lost [redacted]"
    assert state_input["request"]["headers"] == {"accept": "application/json"}
    assert state_input["request"]["body"] == {"options": {}, "model": "keep-model"}
    assert state_input["nested"] == {"one": {"two": {"name": "kept"}}}
    assert state_input["args"] == ["keep-arg", ["inner"]]
    assert state_input["copied"] == "[redacted]"
    assert state_input["tokens"] == {
        "input": 12,
        "output": 3,
        "reasoning": 1,
        "cache": {"read": 2, "write": 4},
    }
    assert state_input["input_tokens"] == 12
    assert state_input["output_tokens"] == 3
    assert state_input["total_tokens"] == 15
    assert state_input["prompt_tokens"] == 12
    assert state_input["cache_read_tokens"] == 2
    assert state_input["setCacheKey"] is True
    assert state_input["authorizationUrl"] == "https://example.test/oauth"
    info = usage["properties"]["info"]
    assert info["tokens"]["input"] == 12
    assert info["tokens"]["output"] == 3
    assert info["input_tokens"] == 12
    assert info["output_tokens"] == 3
    assert info["cost"] == 0.01
    if global_stream:
        wrapped = json.loads(chunks[0].removeprefix("data: ").strip())
        assert wrapped["directory"] == "/srv/worker/task"


def test_secret_text_inside_strings_and_lists_is_replaced():
    secret = "configured-list-canary-value"
    payload = {
        "items": [
            "keep",
            secret,
            f"curl -H 'Authorization: Bearer {secret}'",
            ["nested-keep", secret, {"token": "field-token-canary-value"}],
        ],
        "note": f"echo {secret}",
        "plain": "public",
    }
    redact_secret_fields(payload, {secret, "public"})
    assert payload["items"] == [
        "keep",
        "curl -H 'Authorization: Bearer [redacted]'",
        ["nested-keep", {}],
    ]
    assert payload["note"] == "echo [redacted]"
    assert payload["plain"] == "public"
    assert secret not in json.dumps(payload)


def test_new_secret_field_names_redact_without_dropping_token_counts():
    payload = {
        "X-Auth-Token": XAUTH,
        "X-Goog-Api-Key": XGOOG,
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "client_secret": CLIENT,
        "Cookie": COOKIE,
        "dbPassword": PASSWORD,
        "authorization": AUTH,
        "apiKey": APIKEY,
        "privateKey": "canary-private-key-value",
        "private_key": "canary-private-key-value-2",
        "access_key": "canary-access-key-value",
        "credentials": "canary-credentials-value",
        "bearer": "canary-bearer-value",
        "tokens": {"input": 4, "output": 5, "reasoning": 0, "cache": {"read": 1, "write": 1}},
        "input_tokens": 4,
        "output_tokens": 5,
        "inputTokens": 9,
        "maxTokens": 100,
        "token_count": 6,
        "setCacheKey": False,
        "authorizationUrl": "https://example.test/oauth",
        "title": "kept",
    }
    redact_secret_fields(payload)
    rendered = json.dumps(payload)
    for canary in (
        XAUTH,
        XGOOG,
        ACCESS,
        REFRESH,
        CLIENT,
        COOKIE,
        PASSWORD,
        AUTH,
        APIKEY,
        "canary-private-key-value",
        "canary-private-key-value-2",
        "canary-access-key-value",
        "canary-credentials-value",
        "canary-bearer-value",
    ):
        assert canary not in rendered
    assert payload["tokens"]["input"] == 4
    assert payload["input_tokens"] == 4
    assert payload["output_tokens"] == 5
    assert payload["inputTokens"] == 9
    assert payload["maxTokens"] == 100
    assert payload["token_count"] == 6
    assert payload["setCacheKey"] is False
    assert payload["authorizationUrl"] == "https://example.test/oauth"
    assert payload["title"] == "kept"


def _spellings(snake: str, camel: str, concat: str) -> list[str]:
    pascal = camel[:1].upper() + camel[1:]
    return [
        snake,
        snake.replace("_", "-"),
        camel,
        pascal,
        snake.upper(),
        concat,
        concat.upper(),
    ]


def test_concatenated_secret_names_redact_and_usage_fields_stay():
    names = {
        "client_secret": ("clientSecret", "clientsecret"),
        "access_token": ("accessToken", "accesstoken"),
        "refresh_token": ("refreshToken", "refreshtoken"),
        "private_key": ("privateKey", "privatekey"),
        "access_key": ("accessKey", "accesskey"),
        "x_auth_token": ("xAuthToken", "xauthtoken"),
        "x_goog_api_key": ("xGoogApiKey", "xgoogapikey"),
    }
    payload: dict[str, object] = {}
    canaries: list[str] = []
    for snake, (camel, concat) in names.items():
        for spelling in _spellings(snake, camel, concat):
            canary = f"canary-{len(canaries):02d}-secret-value"
            payload[spelling] = canary
            canaries.append(canary)
    assert len(canaries) == 49
    payload.update(
        {
            "tokens": {"input": 4, "output": 5},
            "input_tokens": 4,
            "inputTokens": 9,
            "inputtokens": 11,
            "maxTokens": 100,
            "output_tokens": 5,
            "setCacheKey": False,
            "cache_key": "keep-cache-key",
            "publicKey": "keep-public-key",
            "authorizationUrl": "https://example.test/oauth",
            "title": "kept",
        }
    )
    redact_secret_fields(payload)
    rendered = json.dumps(payload)
    for canary in canaries:
        assert canary not in rendered
    assert payload["tokens"] == {"input": 4, "output": 5}
    assert payload["input_tokens"] == 4
    assert payload["inputTokens"] == 9
    assert payload["inputtokens"] == 11
    assert payload["maxTokens"] == 100
    assert payload["output_tokens"] == 5
    assert payload["setCacheKey"] is False
    assert payload["cache_key"] == "keep-cache-key"
    assert payload["publicKey"] == "keep-public-key"
    assert payload["authorizationUrl"] == "https://example.test/oauth"
    assert payload["title"] == "kept"


def test_error_json_and_text_bodies_drop_secret_fields_and_values():
    secret = "configured-error-canary-value"
    body = {
        "error": {
            "apiKey": "err-field-api-key-canary",
            "nested": [
                {"token": "err-field-token-canary", "message": "kept"},
                secret,
            ],
            "detail": "failed",
        }
    }
    rendered = redact_upstream_body(json.dumps(body).encode(), {secret})
    parsed = json.loads(rendered)
    assert parsed == {
        "error": {
            "nested": [{"message": "kept"}],
            "detail": "failed",
        }
    }
    text = redact_upstream_body(f"upstream failed {secret} now".encode(), {secret})
    assert secret.encode() not in text
    assert text == b"upstream failed [redacted] now"
    untouched = redact_upstream_body(b'{"error":"missing"}', set())
    assert untouched == b'{"error":"missing"}'


@pytest.mark.asyncio
async def test_remote_workspace_loads_secret_values_through_the_connected_client():
    secret = "platform-config-canary-value"
    environment = SimpleNamespace(mode="LINUX_REMOTE", id=3, worker_channel="worker")

    class Client:
        async def exchange(self, method, path, *, params=None, body=None, session_id=None, task_id=None):
            if path == "/config":
                payload = {
                    "mcp": {"nexus-library": {"headers": {"Authorization": f"Bearer {secret}"}}},
                    "provider": {"nexus-gateway": {"options": {"apiKey": secret, "baseURL": "http://127.0.0.1"}}},
                }
                return 200, json.dumps(payload).encode(), {}
            return 404, b"", {}

    found = await load_proxy_secrets("/srv/worker/task", environment, Client())
    assert secret in found
    payload = {"copied": secret, "baseURL": "http://127.0.0.1", "items": [secret, "keep"]}
    redact_secret_fields(payload, found)
    assert secret not in json.dumps(payload)
    assert payload == {
        "copied": "[redacted]",
        "baseURL": "http://127.0.0.1",
        "items": ["keep"],
    }


@pytest.mark.asyncio
async def test_failed_remote_config_keeps_cached_secrets(monkeypatch):
    secret = "stale-cache-canary-value"
    environment = SimpleNamespace(mode="LINUX_REMOTE", id=11, worker_epoch=4)
    clock = {"now": 1_000.0}
    monkeypatch.setattr("app.api.workspace_routes.time.monotonic", lambda: clock["now"])
    state = {"fail": False}

    class Client:
        def __init__(self):
            self.reads = 0

        async def exchange(self, method, path, *, params=None, body=None, session_id=None, task_id=None):
            self.reads += 1
            if state["fail"]:
                raise ConnectionError("worker down")
            if path == "/config":
                payload = {"provider": {"nexus-gateway": {"options": {"apiKey": secret}}}}
                return 200, json.dumps(payload).encode(), {}
            return 200, b"{}", {}

    client = Client()
    first = await load_proxy_secrets("/srv/missing", environment, client)
    second = await load_proxy_secrets("/srv/missing", environment, client)
    assert secret in first
    assert secret in second
    assert client.reads == 2

    clock["now"] += 31
    state["fail"] = True
    stale = await load_proxy_secrets("/srv/missing", environment, client)
    assert secret in stale
    assert client.reads == 4

    cold = SimpleNamespace(mode="LINUX_REMOTE", id=12, worker_epoch=4)
    empty = await load_proxy_secrets("/srv/missing", cold, client)
    assert secret not in empty


@pytest.mark.asyncio
async def test_remote_secret_cache_is_per_workspace(monkeypatch):
    secret = "workspace-cache-canary-value"
    other = "other-workspace-canary-value"
    environment = SimpleNamespace(mode="LINUX_REMOTE", id=21, worker_epoch=2)
    calls: list[tuple[str, str]] = []

    class Client:
        async def exchange(self, method, path, *, params=None, body=None, session_id=None, task_id=None):
            directory = params["directory"]
            calls.append((path, directory))
            if path != "/config":
                return 200, b"{}", {}
            value = secret if directory.rstrip("/") == "/srv/worker/task" else other
            payload = {"provider": {"nexus-gateway": {"options": {"apiKey": value}}}}
            return 200, json.dumps(payload).encode(), {}

    client = Client()
    first = await load_proxy_secrets("/srv/worker/task", environment, client)
    second = await load_proxy_secrets("/srv/worker/task/", environment, client)
    assert secret in first
    assert secret in second
    assert other not in second
    assert len([item for item in calls if item[0] == "/config"]) == 1

    separate = await load_proxy_secrets("/srv/worker/other", environment, client)
    assert other in separate
    assert secret not in separate
    assert len([item for item in calls if item[0] == "/config"]) == 2


@pytest.mark.asyncio
async def test_partial_config_read_keeps_cached_secret_values(monkeypatch):
    secret = "partial-config-canary-value"
    environment = SimpleNamespace(mode="LINUX_REMOTE", id=22, worker_epoch=1)
    clock = {"now": 5_000.0}
    monkeypatch.setattr("app.api.workspace_routes.time.monotonic", lambda: clock["now"])
    state = {"config": "ok"}

    class Client:
        def __init__(self):
            self.reads = 0

        async def exchange(self, method, path, *, params=None, body=None, session_id=None, task_id=None):
            self.reads += 1
            if path == "/config":
                if state["config"] == "down":
                    raise ConnectionError("config down")
                if state["config"] == "bad-json":
                    return 200, b"not-json", {}
                payload = {"provider": {"nexus-gateway": {"options": {"apiKey": secret}}}}
                return 200, json.dumps(payload).encode(), {}
            if path == "/global/config":
                return 200, b"{}", {}
            return 404, b"", {}

    client = Client()
    found = await load_proxy_secrets("/srv/worker/task", environment, client)
    assert secret in found
    assert client.reads == 2

    clock["now"] += 31
    state["config"] = "down"
    kept = await load_proxy_secrets("/srv/worker/task", environment, client)
    assert secret in kept
    assert client.reads == 4
    cached = await load_proxy_secrets("/srv/worker/task", environment, client)
    assert secret in cached
    assert client.reads == 4

    clock["now"] += 31
    state["config"] = "bad-json"
    still = await load_proxy_secrets("/srv/worker/task", environment, client)
    assert secret in still
    assert client.reads == 6

    cold = SimpleNamespace(mode="LINUX_REMOTE", id=23, worker_epoch=1)
    missing = await load_proxy_secrets("/srv/cold", cold, client)
    assert secret not in missing
    after_miss = client.reads
    assert secret not in await load_proxy_secrets("/srv/cold", cold, client)
    assert client.reads == after_miss + 2
    state["config"] = "ok"
    recovered = await load_proxy_secrets("/srv/cold", cold, client)
    assert secret in recovered

"""API 中转数据面测试：Mock Proxy 透明转发、鉴权、调度、租约、重试与日志。

用一个进程内 Mock Proxy（httpx.MockTransport）替代真实下游，用
``InMemoryRuntimeStore`` 替代 Redis。运行态实现的语义与 Redis 版本一致：并发
租约、周期用量、熔断与心跳。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal, init_database
from app.gateway.config import (
    GatewayConfig,
    GatewayConfigState,
)
from app.gateway.errors import (
    CLIENT_DISCONNECTED,
    NO_ELIGIBLE_NODE,
    NODE_CREDENTIAL_ERROR,
    POOL_SATURATED,
    QUOTA_EXCEEDED,
    RUNTIME_UNAVAILABLE,
)
from app.gateway.health_summary import GatewayOpsService, window_metrics
from app.gateway.models import (
    GatewayKey,
    GatewayRequest,
    GatewayRuntimeEvent,
    utcnow,
)
from app.gateway.probe import ProbeOutcome
from app.gateway.proxy import GatewayProxy
from app.gateway.request_lifecycle import recover_abandoned_requests
from app.gateway.runtime import (
    REVOKED_KEYS_KEY,
    SCOPE_KEY,
    SCOPE_NODE,
    CircuitSnapshot,
    InMemoryRuntimeStore,
    RedisRuntimeStore,
    usage_buckets,
)
from app.gateway.scheduler import REASON_SCHEDULING
from app.gateway.security import CredentialCipher, gateway_key_digest
from app.gateway.server import GatewayDataPlane, RequestRecord, StreamUsageCollector, create_gateway_app

RAW_KEY = "agw_" + uuid4().hex
KEY_DIGEST = gateway_key_digest(RAW_KEY)
MODEL = "gpt-test"
POOL_ID = "gp_test"
NODE_A = "gn_a"
NODE_B = "gn_b"
DOWNSTREAM_KEY = "sk-downstream"

# 真实 Redis 才能验证 Lua 脚本本身；未提供该环境变量时相关用例显式 skip。
REDIS_URL = os.environ.get("ASTRA_TEST_REDIS_URL")


class FakeProbe:
    """进程内探测客户端：按需返回全部成功或全部失败，不发起真实网络。"""

    def __init__(self) -> None:
        self.success = True
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def probe(self, node, probe_types, api_key):
        self.calls.append((node.id, tuple(probe_types)))
        return [ProbeOutcome(probe_type, self.success, 1) for probe_type in probe_types]


@pytest.fixture(scope="module", autouse=True)
def _schema():
    asyncio.run(init_database())
    yield


class MockUpstream:
    """按 host 路由的 Mock Proxy，支持连接失败与 SSE。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_hosts: set[str] = set()
        self.response_status = 200
        self.include_usage = True
        self.usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        self.stream_chunks = [
            b'data: {"id":"c","choices":[{"delta":{"content":"he"}}]}\n\n',
            b'data: {"id":"c","choices":[{"delta":{"content":"llo"}}]}\n\n',
        ]
        self.stream_usage: dict[str, int] | None = {
            "prompt_tokens": 5,
            "completion_tokens": 2,
            "total_tokens": 7,
        }
        self.stream_content_type = "text/event-stream"
        self.stream_terminal = True
        # 模拟 httpx 已解压、但响应头仍写着 gzip 的上游。
        self.decoded_gzip = False

    def stream_bytes(self) -> list[bytes]:
        chunks = list(self.stream_chunks)
        if self.stream_usage is not None:
            tail = {"id": "c", "choices": [{"delta": {}}], "usage": self.stream_usage}
            chunks.append(f"data: {json.dumps(tail)}\n\n".encode())
        if self.stream_terminal:
            chunks.append(b"data: [DONE]\n\n")
        return chunks

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host in self.fail_hosts:
            raise httpx.ConnectError("connection refused", request=request)
        payload: dict[str, Any] = {}
        if request.content:
            try:
                payload = json.loads(request.content)
            except ValueError:
                payload = {}
        if payload.get("stream"):
            return httpx.Response(
                self.response_status,
                content=self._sse(),
                headers={"content-type": self.stream_content_type},
            )
        body: dict[str, Any] = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
        if self.include_usage:
            body["usage"] = self.usage
        if self.decoded_gzip:
            import gzip

            raw = gzip.compress(json.dumps(body).encode())

            class GzipStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield raw

                async def aclose(self) -> None:
                    return None

            return httpx.Response(
                self.response_status,
                stream=GzipStream(),
                headers={"content-type": "application/json", "content-encoding": "gzip"},
            )
        return httpx.Response(self.response_status, json=body)

    async def _sse(self):
        for chunk in self.stream_bytes():
            yield chunk


def _snapshot(
    *,
    nodes: list[dict[str, Any]],
    routes: list[dict[str, Any]],
    keys: list[dict[str, Any]],
    pools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": "2026-09-23T00:00:00+00:00",
        "pools": pools
        if pools is not None
        else [{"id": POOL_ID, "name": "pool", "weight": 1, "enabled": True}],
        "nodes": nodes,
        "routes": routes,
        "keys": keys,
    }


def node_dict(
    node_id: str,
    *,
    host: str,
    cipher: CredentialCipher,
    models: tuple[str, ...] = (MODEL,),
    max_concurrency: int = 0,
    weight: int = 1,
    scheduling_state: str = "ACTIVE",
    health_state: str = "HEALTHY",
    hourly_token_limit: int | None = None,
    quota_policy: str = "STOP",
    auth_type: str = "BEARER",
    auth_header: str = "Authorization",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "name": node_id,
        "pool_id": POOL_ID,
        "base_url": f"http://{host}",
        "api_key_encrypted": cipher.encrypt(DOWNSTREAM_KEY),
        "auth_type": auth_type,
        "auth_header": auth_header,
        "connect_timeout_seconds": None,
        "request_timeout_seconds": None,
        "models": list(models),
        "weight": weight,
        "max_concurrency": max_concurrency,
        "hourly_token_limit": hourly_token_limit,
        "daily_token_limit": None,
        "quota_policy": quota_policy,
        "config_state": "ENABLED",
        "scheduling_state": scheduling_state,
        "health_state": health_state,
    }


def key_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "gk_test",
        "name": "key",
        "key_prefix": RAW_KEY[:12],
        "key_digest": KEY_DIGEST,
        "owner_id": "owner-1",
        "allowed_models": [MODEL],
        "max_concurrency": 0,
        "hourly_token_limit": None,
        "daily_token_limit": None,
        "enabled": True,
        "expires_at": None,
        "revoked_at": None,
    }
    base.update(overrides)
    return base


def build_config(
    cipher: CredentialCipher,
    *,
    nodes: list[dict[str, Any]] | None = None,
    key: dict[str, Any] | None = None,
    keys: list[dict[str, Any]] | None = None,
) -> GatewayConfig:
    default_nodes = [
        node_dict(NODE_A, host="node-a.test", cipher=cipher),
        node_dict(NODE_B, host="node-b.test", cipher=cipher),
    ]
    if keys is None:
        keys = [key if key is not None else key_dict()]
    return GatewayConfig.from_snapshot(
        _snapshot(
            nodes=nodes if nodes is not None else default_nodes,
            routes=[{"id": "gr_test", "model": MODEL, "pool_id": POOL_ID, "enabled": True, "weight": 1}],
            keys=keys,
        ),
    )


def build_plane(
    upstream: MockUpstream,
    *,
    config: GatewayConfig,
    cipher: CredentialCipher,
    store: InMemoryRuntimeStore | None = None,
    probe_client: Any | None = None,
    probe_check_interval: float = 3600,
    lease_ttl: int = 900,
    seed_snapshot_health: bool = True,
    max_retries: int = 1,
    proxy: GatewayProxy | None = None,
    circuit_open_seconds: int = 30,
    instance_id: str = "test-instance",
) -> SimpleNamespace:
    store = store or InMemoryRuntimeStore()
    # 数据面启动不再用配置视图播种健康；测试显式模拟「Redis 已有连接作用域运行态」。
    if seed_snapshot_health:
        for node in config.nodes.values():
            asyncio.run(store.seed_health(node.runtime_scope, node.health_state))
    state = GatewayConfigState(config)
    proxy = proxy or GatewayProxy(transport=httpx.MockTransport(upstream.handler))
    plane = GatewayDataPlane(
        config_state=state,
        runtime=store,
        proxy=proxy,
        cipher=cipher,
        session_factory=SessionLocal,
        config_loader=None,
        instance_id=instance_id,
        heartbeat_interval=3600,
        heartbeat_ttl=60,
        lease_ttl=lease_ttl,
        config_poll_interval=3600,
        runtime_health_interval=3600,
        timezone="Asia/Shanghai",
        probe_client=probe_client,
        probe_check_interval=probe_check_interval,
        max_retries=max_retries,
        circuit_open_seconds=circuit_open_seconds,
    )
    return SimpleNamespace(plane=plane, store=store, state=state, cipher=cipher)


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {RAW_KEY}"}


async def _fetch_logs(limit: int = 20) -> list[GatewayRequest]:
    async with SessionLocal() as session:
        rows = await session.scalars(
            select(GatewayRequest).order_by(GatewayRequest.started_at.desc()).limit(limit)
        )
        return list(rows)


# 请求体上限与数据面实现保持一致；测试独立声明，避免直接依赖被测常量。
MAX_BODY_BYTES = 20 * 1024 * 1024


def _asgi_chat(
    app: Any,
    *,
    headers: dict[str, str],
    chunks: list[bytes],
) -> SimpleNamespace:
    """直接以 ASGI 协议调用数据面，记录 ``receive`` 调用次数。

    用于验证「鉴权/上限校验是否在读取请求体之前完成」：请求体字节数由
    ``receive`` 事件按需拉取，未被消费时调用次数保持为 0。
    """

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    pending = list(chunks)
    state: dict[str, Any] = {"receive_calls": 0, "status": None, "body": b""}

    async def receive() -> dict[str, Any]:
        state["receive_calls"] += 1
        if pending:
            chunk = pending.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            state["status"] = message["status"]
        elif message["type"] == "http.response.body":
            state["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return SimpleNamespace(**state)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(Fernet.generate_key().decode())


@pytest.fixture
def upstream() -> MockUpstream:
    return MockUpstream()


@pytest.fixture
def runtime_app(cipher, upstream):
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    app = create_gateway_app(kv.plane)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, upstream=upstream, **vars(kv))


def test_models_lists_allowed_models(runtime_app):
    response = runtime_app.client.get("/v1/models", headers=auth_headers())
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [MODEL]

    denied = runtime_app.client.get("/v1/models", headers={"Authorization": "Bearer agw_nope"})
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize("refresh_at", ["body", "lease"])
def test_request_keeps_one_config_during_refresh(cipher, upstream, refresh_at):
    old = build_config(
        cipher,
        nodes=[node_dict(NODE_A, host="old.test", cipher=cipher)],
        key=key_dict(owner_id="old-owner"),
    )
    new = build_config(
        cipher,
        nodes=[node_dict(NODE_B, host="new.test", cipher=cipher)],
        key=key_dict(owner_id="new-owner"),
    )

    async def scenario():
        kv = build_plane(upstream, config=old, cipher=cipher, seed_snapshot_health=False)
        for node in [*old.nodes.values(), *new.nodes.values()]:
            await kv.store.seed_health(node.runtime_scope, "HEALTHY")
        await kv.plane.startup()
        acquire = kv.store.acquire

        async def acquire_with_refresh(scope, scope_id, limit, ttl):
            if refresh_at == "lease" and scope == SCOPE_KEY:
                await kv.state.replace(new)
            return await acquire(scope, scope_id, limit, ttl)

        kv.store.acquire = acquire_with_refresh

        async def body():
            yield b'{"model":'
            if refresh_at == "body":
                await kv.state.replace(new)
            yield json.dumps(MODEL).encode() + b',"messages":[]}'

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_gateway_app(kv.plane)),
                base_url="http://gateway.test",
            ) as client:
                first = await client.post(
                    "/v1/chat/completions", content=body(), headers=auth_headers()
                )
                second = await client.post(
                    "/v1/chat/completions",
                    json={"model": MODEL, "messages": []},
                    headers=auth_headers(),
                )
            return first, second
        finally:
            await kv.plane.shutdown()

    first, second = asyncio.run(scenario())
    assert first.status_code == second.status_code == 200
    assert [request.url.host for request in upstream.requests] == ["old.test", "new.test"]
    logs = asyncio.run(_fetch_logs())
    assert next(log for log in logs if log.node_id == NODE_A).owner_id == "old-owner"
    assert next(log for log in logs if log.node_id == NODE_B).owner_id == "new-owner"


def test_invalid_expired_disabled_and_forbidden_keys(cipher, upstream, monkeypatch):
    for key, expected in [
        (key_dict(expires_at="2000-01-01T00:00:00+00:00"), "KEY_EXPIRED"),
        (key_dict(enabled=False), "KEY_DISABLED"),
        (key_dict(revoked_at="2026-01-01T00:00:00+00:00"), "KEY_DISABLED"),
        (key_dict(allowed_models=["other-model"]), "KEY_FORBIDDEN"),
    ]:
        kv = build_plane(upstream, config=build_config(cipher, key=key), cipher=cipher)
        with TestClient(create_gateway_app(kv.plane)) as client:
            response = client.post(
                "/v1/chat/completions",
                json={"model": MODEL, "messages": []},
                headers=auth_headers(),
            )
            assert response.status_code in (401, 403), response.text
            assert response.json()["error"]["code"] == expected
    assert upstream.requests == []


def test_invalid_key_is_rejected_without_reading_request_body(cipher, upstream):
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    result = _asgi_chat(
        create_gateway_app(kv.plane),
        headers={"authorization": "Bearer agw_invalid", "content-type": "application/json"},
        chunks=[json.dumps({"model": MODEL, "messages": []}).encode()],
    )
    assert result.status == 401
    assert json.loads(result.body)["error"]["code"] == "UNAUTHORIZED"
    # 鉴权先于读取：无效 Key 不得消费请求体，也不得转发到下游。
    assert result.receive_calls == 0
    assert upstream.requests == []


def test_revoked_key_is_blocked_immediately_before_config_refresh(cipher, upstream):
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    # 缓存配置仍是启用状态（enabled=True, revoked_at=None）；共享吊销集合追加摘要后，
    # 不等待配置刷新也必须立即拒绝，与前端「立即失效」一致。
    asyncio.run(kv.store.revoke_key_digest(KEY_DIGEST))
    with TestClient(create_gateway_app(kv.plane)) as client:
        chat = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
        models = client.get("/v1/models", headers=auth_headers())
    assert chat.status_code == 401
    assert chat.json()["error"]["code"] == "KEY_DISABLED"
    assert models.status_code == 401
    assert upstream.requests == []


def test_revocation_is_shared_across_data_plane_instances(cipher, upstream):
    store = InMemoryRuntimeStore()
    config = build_config(cipher)
    first = build_plane(upstream, config=config, cipher=cipher, store=store)
    second = build_plane(upstream, config=config, cipher=cipher, store=store)
    asyncio.run(store.revoke_key_digest(KEY_DIGEST))
    with (
        TestClient(create_gateway_app(first.plane)) as client_a,
        TestClient(create_gateway_app(second.plane)) as client_b,
    ):
        response_a = client_a.post(
            "/v1/chat/completions", json={"model": MODEL, "messages": []}, headers=auth_headers()
        )
        response_b = client_b.post(
            "/v1/chat/completions", json={"model": MODEL, "messages": []}, headers=auth_headers()
        )
    assert response_a.status_code == 401
    assert response_b.status_code == 401
    assert response_a.json()["error"]["code"] == "KEY_DISABLED"
    assert response_b.json()["error"]["code"] == "KEY_DISABLED"
    assert upstream.requests == []


def test_cached_enabled_config_cannot_revive_revoked_key(cipher, upstream):
    # 缓存配置的 Key 仍启用且 revoked_at 为空，但共享阻断立即拒绝已吊销摘要。
    kv = build_plane(
        upstream,
        config=build_config(cipher, key=key_dict(enabled=True, revoked_at=None)),
        cipher=cipher,
    )
    asyncio.run(kv.store.revoke_key_digest(KEY_DIGEST))
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "KEY_DISABLED"
    assert upstream.requests == []


def test_revocation_state_fails_closed_on_runtime_or_marker_loss(cipher, upstream):
    scenarios = {
        "runtime": lambda store: store.set_available(False),
        "marker": lambda store: store.clear_revocation_state(),
    }
    for name, lose in scenarios.items():
        store = InMemoryRuntimeStore()
        kv = build_plane(
            upstream,
            config=build_config(cipher),
            cipher=cipher,
            store=store,
            seed_snapshot_health=False,
        )
        with TestClient(create_gateway_app(kv.plane)) as client:
            lose(store)
            chat = client.post(
                "/v1/chat/completions",
                json={"model": MODEL, "messages": []},
                headers=auth_headers(),
            )
            models = client.get("/v1/models", headers=auth_headers())
        assert chat.status_code == 503, name
        assert chat.json()["error"]["code"] == RUNTIME_UNAVAILABLE, name
        assert models.status_code == 503, name
    assert upstream.requests == []


def test_revocation_state_rebuilds_from_db_after_flush(cipher, upstream):
    revoked_raw = "agw_" + uuid4().hex
    revoked_digest = gateway_key_digest(revoked_raw)
    config = build_config(
        cipher,
        keys=[
            key_dict(),
            key_dict(id="gk_revoked", key_prefix=revoked_raw[:12], key_digest=revoked_digest),
        ],
    )

    async def seed_db():
        async with SessionLocal() as session:
            session.add(
                GatewayKey(
                    id=f"gk_db_{uuid4().hex[:8]}",
                    name="revoked-in-db",
                    key_prefix=revoked_raw[:12],
                    key_digest=revoked_digest,
                    enabled=True,
                    revoked_at=utcnow(),
                )
            )
            await session.commit()

    asyncio.run(seed_db())
    store = InMemoryRuntimeStore()
    kv = build_plane(upstream, config=config, cipher=cipher, store=store)
    with TestClient(create_gateway_app(kv.plane)) as client:
        # 启动已同步过一次；模拟 Redis 清空（集合与标记都丢失）后 fail closed。
        store.clear_revocation_state()
        blocked = client.post(
            "/v1/chat/completions", json={"model": MODEL, "messages": []}, headers=auth_headers()
        )
        assert blocked.status_code == 503
        assert blocked.json()["error"]["code"] == RUNTIME_UNAVAILABLE

        # 从 DB 追加同步所有 revoked_at 非空摘要并重建就绪哨兵。
        assert asyncio.run(kv.plane.sync_revocation_state()) is True
        assert asyncio.run(store.revocation_state_ready()) is True
        assert asyncio.run(store.is_key_revoked(revoked_digest)) is True

        # 未吊销的 Key 不受影响；DB 中已吊销的 Key 被立即阻断。
        allowed = client.post(
            "/v1/chat/completions", json={"model": MODEL, "messages": []}, headers=auth_headers()
        )
        assert allowed.status_code == 200, allowed.text
        revoked = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers={"Authorization": f"Bearer {revoked_raw}"},
        )
        assert revoked.status_code == 401
        assert revoked.json()["error"]["code"] == "KEY_DISABLED"
    assert upstream.requests


def test_declared_content_length_over_limit_is_rejected_before_reading(cipher, upstream):
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    result = _asgi_chat(
        create_gateway_app(kv.plane),
        headers={
            "authorization": f"Bearer {RAW_KEY}",
            "content-type": "application/json",
            "content-length": str(MAX_BODY_BYTES + 1),
        },
        chunks=[b"{}"],
    )
    assert result.status == 413
    error = json.loads(result.body)["error"]
    assert error["code"] == "PAYLOAD_TOO_LARGE"
    assert error["type"] == "invalid_request_error"
    assert result.receive_calls == 0
    assert upstream.requests == []


def test_actual_body_over_limit_is_rejected_while_streaming(cipher, upstream):
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    chunk = b"x" * (1024 * 1024)
    result = _asgi_chat(
        create_gateway_app(kv.plane),
        headers={"authorization": f"Bearer {RAW_KEY}", "content-type": "application/json"},
        chunks=[chunk] * 21,
    )
    assert result.status == 413
    assert json.loads(result.body)["error"]["code"] == "PAYLOAD_TOO_LARGE"
    # 无 Content-Length 时按实际流字节强制上限：读到第 21 MiB 即终止。
    assert result.receive_calls == 21
    assert upstream.requests == []


def test_authenticated_request_updates_key_last_used(cipher, upstream):
    key_id = f"gk_{uuid4().hex[:8]}"
    raw = "agw_" + uuid4().hex
    digest = gateway_key_digest(raw)
    config = build_config(
        cipher,
        keys=[key_dict(id=key_id, key_prefix=raw[:12], key_digest=digest)],
    )

    async def seed():
        async with SessionLocal() as session:
            session.add(
                GatewayKey(
                    id=key_id,
                    name="p2-remote",
                    key_prefix=raw[:12],
                    key_digest=digest,
                    enabled=True,
                    last_used_at=None,
                )
            )
            await session.commit()

    asyncio.run(seed())
    before = utcnow()
    kv = build_plane(upstream, config=config, cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers={"Authorization": f"Bearer {raw}"},
        )
    assert response.status_code == 200, response.text

    async def read():
        async with SessionLocal() as session:
            stored = await session.get(GatewayKey, key_id)
            assert stored is not None and stored.last_used_at is not None
            return stored.last_used_at

    assert asyncio.run(read()) >= before
    logs = asyncio.run(_fetch_logs(5))
    assert any(row.key_id == key_id for row in logs)


def test_request_within_limit_is_read_and_forwarded(cipher, upstream):
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    body = json.dumps({"model": MODEL, "messages": []}).encode()
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            content=body,
            headers={**auth_headers(), "content-type": "application/json"},
        )
    assert response.status_code == 200, response.text
    assert json.loads(upstream.requests[-1].content)["model"] == MODEL


def test_decoded_gzip_body_is_forwarded_without_content_encoding(cipher, upstream):
    upstream.decoded_gzip = True
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "hi"
    assert "content-encoding" not in {key.lower() for key in response.headers}


def test_non_streaming_is_transparent_and_records_usage(runtime_app):
    response = runtime_app.client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "hi"

    sent = runtime_app.upstream.requests[-1]
    # 请求体原样转发；认证 Header 被替换为节点凭据，Gateway Key 不再出现。
    assert json.loads(sent.content)["model"] == MODEL
    assert sent.headers["Authorization"] == f"Bearer {DOWNSTREAM_KEY}"

    async def logs():
        return await _fetch_logs()

    rows = asyncio.run(logs())
    latest = next(row for row in rows if row.model == MODEL)
    assert latest.streaming is False
    assert latest.input_tokens == 7
    assert latest.output_tokens == 3
    assert latest.total_tokens == 10
    assert latest.retry_count == 0
    assert latest.node_id == NODE_A

    buckets = usage_buckets(utcnow(), "Asia/Shanghai")
    stored = asyncio.run(
        runtime_app.store.usage(SCOPE_NODE, latest.node_id, "hour", buckets["hour"])
    )
    assert stored.total_tokens == 10
    assert stored.unknown_requests == 0


def test_unknown_usage_is_not_fabricated(cipher, upstream):
    upstream.include_usage = False
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
        assert response.status_code == 200

    rows = asyncio.run(_fetch_logs())
    latest = next(row for row in rows if row.model == MODEL)
    assert latest.input_tokens is None
    assert latest.total_tokens is None
    buckets = usage_buckets(utcnow(), "Asia/Shanghai")
    stored = asyncio.run(kv.store.usage(SCOPE_NODE, NODE_A, "hour", buckets["hour"]))
    assert stored.unknown_requests == 1
    assert stored.total_tokens == 0


def test_streaming_forwards_chunks_and_terminal_usage(runtime_app):
    response = runtime_app.client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [], "stream": True},
        headers=auth_headers(),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert '"he"' in body and '"llo"' in body and "[DONE]" in body

    rows = asyncio.run(_fetch_logs())
    latest = next(row for row in rows if row.streaming and not row.stream_interrupted)
    assert latest.stream_interrupted is False
    assert latest.input_tokens == 5
    assert latest.total_tokens == 7
    assert latest.ttft_ms is not None
    assert latest.ttfb_ms is not None


def test_retry_only_when_request_not_sent(cipher, upstream):
    upstream.fail_hosts = {"node-a.test"}
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 200, response.text
    hosts = [request.url.host for request in upstream.requests]
    assert hosts == ["node-a.test", "node-b.test"]

    rows = asyncio.run(_fetch_logs())
    latest = next(row for row in rows if row.model == MODEL)
    assert latest.retry_count == 1
    assert latest.node_id == NODE_B


class GatedProxy(GatewayProxy):
    """卡住 ``send``，让测试在请求仍进行时读到已经落库的到达行。"""

    def __init__(self, upstream: MockUpstream) -> None:
        super().__init__(transport=httpx.MockTransport(upstream.handler))
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
        self.entered.set()
        await self.release.wait()
        return await super().send(request, stream=stream)


class BarrierProxy(GatewayProxy):
    """两个请求都进入 send 之后一起放行，用来卡住熔断转移前的交错。"""

    def __init__(self, upstream: MockUpstream, *, parties: int = 2) -> None:
        super().__init__(transport=httpx.MockTransport(upstream.handler))
        self.parties = parties
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()
        self._seen = 0

    async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
        self._seen += 1
        if self._seen >= self.parties:
            self.arrived.set()
        await self.release.wait()
        return await super().send(request, stream=stream)


class SequencedProxy(GatewayProxy):
    """让两个请求都通过调度后再按顺序失败，用来验证熔断事件去重。"""

    def __init__(self, upstream: MockUpstream) -> None:
        super().__init__(transport=httpx.MockTransport(upstream.handler))
        self.first = asyncio.Event()
        self.second = asyncio.Event()
        self.release_first = asyncio.Event()
        self.release_second = asyncio.Event()
        self._seen = 0

    async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
        self._seen += 1
        if self._seen == 1:
            self.first.set()
            await self.release_first.wait()
        else:
            self.second.set()
            await self.release_second.wait()
        return await super().send(request, stream=stream)


def test_no_retry_after_downstream_responded(cipher, upstream):
    upstream.response_status = 500
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 500
    assert len(upstream.requests) == 1

    rows = asyncio.run(_fetch_logs())
    latest = next(row for row in rows if row.model == MODEL)
    assert latest.retry_count == 0


def test_connect_failure_without_another_attempt_does_not_count_as_retry(cipher, upstream):
    upstream.fail_hosts = {"node-a.test"}
    nodes = [node_dict(NODE_A, host="node-a.test", cipher=cipher)]
    kv = build_plane(
        upstream,
        config=build_config(cipher, nodes=nodes),
        cipher=cipher,
        max_retries=0,
    )
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 502
    assert len(upstream.requests) == 1
    rows = asyncio.run(_fetch_logs())
    latest = next(row for row in rows if row.model == MODEL and row.status_code == 502)
    assert latest.retry_count == 0


def test_passive_circuit_open_and_recover_are_recorded_once(cipher, upstream):
    node_id = f"gn_{uuid4().hex[:8]}"
    host = f"{node_id}.test"
    upstream.fail_hosts = {host}
    nodes = [node_dict(node_id, host=host, cipher=cipher)]
    proxy = SequencedProxy(upstream)
    kv = build_plane(
        upstream,
        config=build_config(cipher, nodes=nodes),
        cipher=cipher,
        max_retries=0,
        proxy=proxy,
        circuit_open_seconds=1,
    )

    async def post(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )

    async def scenario():
        app = create_gateway_app(kv.plane)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            first_task = asyncio.create_task(post(client))
            await asyncio.wait_for(proxy.first.wait(), 5)
            second_task = asyncio.create_task(post(client))
            await asyncio.wait_for(proxy.second.wait(), 5)
            proxy.release_first.set()
            first = await first_task
            assert first.status_code == 502
            proxy.release_second.set()
            second = await second_task
            assert second.status_code == 502
            await asyncio.sleep(1.1)
            upstream.fail_hosts.clear()
            recovered = await post(client)
            again = await post(client)
            assert recovered.status_code == 200
            assert again.status_code == 200

    asyncio.run(scenario())

    async def load():
        async with SessionLocal() as session:
            rows = await session.scalars(
                select(GatewayRuntimeEvent).where(GatewayRuntimeEvent.node_id == node_id)
            )
            return list(rows)

    rows = asyncio.run(load())
    assert [row.kind for row in rows if row.kind == "circuit_open"] == ["circuit_open"]
    assert [row.kind for row in rows if row.kind == "circuit_closed"] == ["circuit_closed"]


def test_sse_collector_records_first_nonempty_content_across_split_frames():
    collector = StreamUsageCollector()
    assert collector.feed(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n') is False
    assert collector.saw_content is False
    assert collector.feed(b'data: {"choices":[{"delta":{"content":""}}]}\n') is False
    line = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
    assert collector.feed(line[:20]) is False
    assert collector.feed(line[20:]) is True
    assert collector.feed(b'data: {"choices":[{"delta":{"content":"more"}}]}\n') is False


def test_sse_collector_keeps_a_multibyte_character_split_across_chunks():
    collector = StreamUsageCollector()
    raw = 'data: {"choices":[{"delta":{"content":"你"}}]}\n'.encode()
    split = raw.index("你".encode()) + 1
    assert collector.feed(raw[:split]) is False
    assert collector.saw_content is False
    assert collector.feed(raw[split:]) is True
    assert collector.saw_content is True


def _latest_stream(model: str) -> GatewayRequest:
    rows = asyncio.run(_fetch_logs(50))
    return next(row for row in rows if row.streaming and row.model == model)


@pytest.mark.parametrize(
    ("chunks", "expect_ttft"),
    [
        ([b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'], False),
        ([b'data: {"choices":[{"delta":{"content":""}}]}\n\n'], False),
        ([b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'], True),
    ],
)
def test_ttft_follows_real_sse_content_frames(cipher, upstream, chunks, expect_ttft):
    model = f"ttft-{uuid4().hex[:8]}"
    upstream.stream_chunks = chunks
    upstream.stream_usage = None
    node = node_dict(NODE_A, host="node-a.test", cipher=cipher, models=(model,))
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=[node],
            routes=[{"id": f"gr-{model}", "model": model, "pool_id": POOL_ID, "enabled": True, "weight": 1}],
            keys=[key_dict(allowed_models=[model])],
        )
    )
    kv = build_plane(upstream, config=config, cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": model, "messages": [], "stream": True},
            headers=auth_headers(),
        )
    assert response.status_code == 200, response.text
    logged = _latest_stream(model)
    assert (logged.ttft_ms is not None) is expect_ttft


def test_ttft_when_one_content_frame_is_split_across_chunks(cipher, upstream):
    model = f"split-{uuid4().hex[:8]}"
    line = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    upstream.stream_chunks = [line[:17], line[17:]]
    upstream.stream_usage = None
    node = node_dict(NODE_A, host="node-a.test", cipher=cipher, models=(model,))
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=[node],
            routes=[{"id": f"gr-{model}", "model": model, "pool_id": POOL_ID, "enabled": True, "weight": 1}],
            keys=[key_dict(allowed_models=[model])],
        )
    )
    kv = build_plane(upstream, config=config, cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": model, "messages": [], "stream": True},
            headers=auth_headers(),
        )
    assert response.status_code == 200, response.text
    assert _latest_stream(model).ttft_ms is not None


def test_stream_429_is_an_upstream_status(cipher, upstream):
    model = f"s429-{uuid4().hex[:8]}"
    upstream.response_status = 429
    upstream.stream_chunks = [b'data: {"choices":[{"delta":{"content":"no"}}]}\n\n']
    upstream.stream_usage = None
    node = node_dict(NODE_A, host="node-a.test", cipher=cipher, models=(model,))
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=[node],
            routes=[{"id": f"gr-{model}", "model": model, "pool_id": POOL_ID, "enabled": True, "weight": 1}],
            keys=[key_dict(allowed_models=[model])],
        )
    )
    kv = build_plane(upstream, config=config, cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": model, "messages": [], "stream": True},
            headers=auth_headers(),
        )
    assert response.status_code == 429
    logged = _latest_stream(model)
    assert logged.error_code == "UPSTREAM_429"
    assert logged.ttft_ms is not None


def test_arrival_row_counts_in_qps_before_the_upstream_returns(cipher, upstream):
    model = f"inflight-{uuid4().hex[:8]}"
    node = node_dict(NODE_A, host="node-a.test", cipher=cipher, models=(model,))
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=[node],
            routes=[{"id": f"gr-{model}", "model": model, "pool_id": POOL_ID, "enabled": True, "weight": 1}],
            keys=[key_dict(allowed_models=[model])],
        )
    )
    proxy = GatedProxy(upstream)
    kv = build_plane(upstream, config=config, cipher=cipher, proxy=proxy)
    started = utcnow()

    async def scenario():
        app = create_gateway_app(kv.plane)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            task = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    json={"model": model, "messages": []},
                    headers=auth_headers(),
                )
            )
            await asyncio.wait_for(proxy.entered.wait(), 5)
            async with SessionLocal() as session:
                rows = list(
                    await session.scalars(
                        select(GatewayRequest).where(
                            GatewayRequest.finished_at.is_(None),
                            GatewayRequest.started_at >= started,
                        )
                    )
                )
                assert len(rows) == 1
                assert rows[0].finished_at is None
                assert rows[0].model == model
                assert rows[0].node_id == NODE_A
                assert rows[0].pool_id == POOL_ID
                assert rows[0].key_id == "gk_test"
                assert rows[0].gateway_instance_id == "test-instance"
                request_id = rows[0].id
                facts = await GatewayOpsService(
                    session,
                    runtime=kv.store,
                    timezone="Asia/Shanghai",
                    probe_interval_seconds=300,
                )._facts(horizon=timedelta(minutes=5))
            mine = [
                sample
                for sample in facts.samples
                if sample.finished_at is None and sample.started_at >= started
            ]
            assert len(mine) == 1
            assert mine[0].finished_at is None
            assert mine[0].model == model
            assert mine[0].node_id == NODE_A
            assert window_metrics(
                [sample for sample in facts.samples if sample.node_id == NODE_A and sample.model == model],
                facts.now,
            ).qps_requests == 1
            proxy.release.set()
            response = await task
            assert response.status_code == 200
        async with SessionLocal() as session:
            done = list(
                await session.scalars(select(GatewayRequest).where(GatewayRequest.model == model))
            )
        assert len(done) == 1
        assert done[0].finished_at is not None
        assert done[0].id == request_id
        assert done[0].status_code == 200

    asyncio.run(scenario())


def _post(client, model: str):
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": []},
        headers=auth_headers(),
    )


def test_single_candidate_with_default_retries_does_not_count_a_retry(cipher, upstream):
    model = f"once-{uuid4().hex[:8]}"
    upstream.fail_hosts = {"node-a.test"}
    node = node_dict(NODE_A, host="node-a.test", cipher=cipher, models=(model,))
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=[node],
            routes=[{"id": f"gr-{model}", "model": model, "pool_id": POOL_ID, "enabled": True, "weight": 1}],
            keys=[key_dict(allowed_models=[model])],
        )
    )
    kv = build_plane(upstream, config=config, cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = _post(client, model)
    # 默认还会再找一轮；这一轮没有下一个候选，于是返回无可用节点，而不是把没发出的尝试算成重试。
    assert response.status_code == 503
    assert response.json()["error"]["code"] == NO_ELIGIBLE_NODE
    assert len(upstream.requests) == 1
    rows = asyncio.run(_fetch_logs())
    latest = next(row for row in rows if row.model == model)
    assert latest.retry_count == 0


def test_missing_next_candidate_does_not_count_a_retry(cipher, upstream):
    model = f"nocand-{uuid4().hex[:8]}"
    upstream.fail_hosts = {"node-a.test"}
    node_a = node_dict(NODE_A, host="node-a.test", cipher=cipher, models=(model,))
    node_b = node_dict(NODE_B, host="node-b.test", cipher=cipher, models=(model,))
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=[node_a, node_b],
            routes=[{"id": f"gr-{model}", "model": model, "pool_id": POOL_ID, "enabled": True, "weight": 1}],
            keys=[key_dict(allowed_models=[model])],
        )
    )
    kv = build_plane(upstream, config=config, cipher=cipher)
    asyncio.run(kv.store.reset_health(config.nodes[NODE_B].runtime_scope))
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = _post(client, model)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == NO_ELIGIBLE_NODE
    assert [request.url.host for request in upstream.requests] == ["node-a.test"]
    rows = asyncio.run(_fetch_logs())
    latest = next(row for row in rows if row.model == model)
    assert latest.retry_count == 0


def test_credential_decrypt_failure_does_not_count_a_retry(cipher, upstream):
    model = f"decrypt-{uuid4().hex[:8]}"
    node = node_dict(NODE_A, host="node-a.test", cipher=cipher, models=(model,))
    node["api_key_encrypted"] = "not-a-fernet-token"
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=[node],
            routes=[{"id": f"gr-{model}", "model": model, "pool_id": POOL_ID, "enabled": True, "weight": 1}],
            keys=[key_dict(allowed_models=[model])],
        )
    )
    kv = build_plane(upstream, config=config, cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = _post(client, model)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == NODE_CREDENTIAL_ERROR
    assert upstream.requests == []
    rows = asyncio.run(_fetch_logs())
    latest = next(row for row in rows if row.model == model)
    assert latest.retry_count == 0


def test_failed_attempts_on_two_candidates_count_one_extra_send(cipher, upstream):
    model = f"both-{uuid4().hex[:8]}"
    upstream.fail_hosts = {"node-a.test", "node-b.test"}
    nodes = [
        node_dict(NODE_A, host="node-a.test", cipher=cipher, models=(model,)),
        node_dict(NODE_B, host="node-b.test", cipher=cipher, models=(model,)),
    ]
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=nodes,
            routes=[{"id": f"gr-{model}", "model": model, "pool_id": POOL_ID, "enabled": True, "weight": 1}],
            keys=[key_dict(allowed_models=[model])],
        )
    )
    kv = build_plane(upstream, config=config, cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = _post(client, model)
    assert response.status_code == 502
    assert len(upstream.requests) == 2
    rows = asyncio.run(_fetch_logs())
    latest = next(row for row in rows if row.model == model)
    assert latest.retry_count == 1


def test_ttft_survives_a_multibyte_character_split_in_the_sse_stream(cipher, upstream):
    model = f"utf-{uuid4().hex[:8]}"
    raw = 'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'.encode()
    split = raw.index("你".encode()) + 1
    upstream.stream_chunks = [raw[:split], raw[split:]]
    upstream.stream_usage = None
    node = node_dict(NODE_A, host="node-a.test", cipher=cipher, models=(model,))
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=[node],
            routes=[{"id": f"gr-{model}", "model": model, "pool_id": POOL_ID, "enabled": True, "weight": 1}],
            keys=[key_dict(allowed_models=[model])],
        )
    )
    kv = build_plane(upstream, config=config, cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": model, "messages": [], "stream": True},
            headers=auth_headers(),
        )
    assert response.status_code == 200, response.text
    assert "你" in response.text
    assert _latest_stream(model).ttft_ms is not None


def test_concurrent_failures_record_one_circuit_open_event(cipher, upstream):
    node_id = f"gn_{uuid4().hex[:8]}"
    host = f"{node_id}.test"
    model = f"barrier-{uuid4().hex[:8]}"
    upstream.fail_hosts = {host}
    node = node_dict(node_id, host=host, cipher=cipher, models=(model,))
    proxy = BarrierProxy(upstream)
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=[node],
            routes=[{"id": f"gr-{model}", "model": model, "pool_id": POOL_ID, "enabled": True, "weight": 1}],
            keys=[key_dict(allowed_models=[model])],
        )
    )
    kv = build_plane(
        upstream,
        config=config,
        cipher=cipher,
        max_retries=0,
        proxy=proxy,
        circuit_open_seconds=30,
    )

    async def post(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/v1/chat/completions",
            json={"model": model, "messages": []},
            headers=auth_headers(),
        )

    async def scenario():
        app = create_gateway_app(kv.plane)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            first = asyncio.create_task(post(client))
            second = asyncio.create_task(post(client))
            await asyncio.wait_for(proxy.arrived.wait(), 5)
            proxy.release.set()
            done = await asyncio.gather(first, second)
            assert [item.status_code for item in done] == [502, 502]

    asyncio.run(scenario())

    async def load():
        async with SessionLocal() as session:
            rows = await session.scalars(
                select(GatewayRuntimeEvent).where(GatewayRuntimeEvent.node_id == node_id)
            )
            return [row.kind for row in rows]

    assert asyncio.run(load()).count("circuit_open") == 1


def test_circuit_transition_is_atomic_on_the_memory_store():
    async def scenario():
        store = InMemoryRuntimeStore()
        scope = f"mem-{uuid4().hex[:8]}"
        opened = await asyncio.gather(
            store.transition_circuit_open(scope, 30),
            store.transition_circuit_open(scope, 30),
        )
        assert sum(1 for item in opened if item.changed) == 1
        assert {item.current for item in opened} == {"OPEN"}
        refreshed = await store.transition_circuit_open(scope, 30)
        assert refreshed.changed is False
        assert refreshed.previous == "OPEN"
        closed = await asyncio.gather(
            store.transition_circuit_closed(scope),
            store.transition_circuit_closed(scope),
        )
        assert sum(1 for item in closed if item.changed) == 1
        assert await store.circuit_snapshot(scope) == CircuitSnapshot("CLOSED", 0)

    asyncio.run(scenario())


def test_startup_marks_dead_generation_lost_and_leaves_legacy_rows(cipher, upstream):
    instance = f"boot-{uuid4().hex[:8]}"
    model = f"life-{uuid4().hex[:8]}"
    old = utcnow() - timedelta(hours=1)
    dead_id = uuid4().hex
    legacy_id = uuid4().hex
    live_started_holder: dict[str, object] = {}

    async def seed():
        async with SessionLocal() as session:
            session.add(
                GatewayRequest(
                    id=dead_id,
                    model=model,
                    gateway_instance_id=instance,
                    gateway_instance_started_at=old,
                    started_at=old,
                    finished_at=None,
                    status_code=None,
                    error_code=None,
                )
            )
            session.add(
                GatewayRequest(
                    id=legacy_id,
                    model=model,
                    started_at=old,
                    finished_at=None,
                    status_code=None,
                )
            )
            await session.commit()

    asyncio.run(seed())
    node = node_dict(NODE_A, host="node-a.test", cipher=cipher, models=(model,))
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=[node],
            routes=[{"id": f"gr-{model}", "model": model, "pool_id": POOL_ID, "enabled": True, "weight": 1}],
            keys=[key_dict(allowed_models=[model])],
        )
    )
    kv = build_plane(upstream, config=config, cipher=cipher, instance_id=instance)
    with TestClient(create_gateway_app(kv.plane)):
        live_started_holder["started"] = kv.plane._started_at

    async def load():
        async with SessionLocal() as session:
            dead = await session.get(GatewayRequest, dead_id)
            legacy = await session.get(GatewayRequest, legacy_id)
            assert dead is not None and legacy is not None
            assert dead.outcome == "lost"
            assert dead.finished_at is not None
            assert dead.status_code is None
            assert dead.error_code is None
            assert legacy.outcome is None
            assert legacy.finished_at is None
            assert legacy.gateway_instance_id is None
            store = InMemoryRuntimeStore()
            started = kv.plane._started_at
            await store.heartbeat(
                instance, {"instance_id": instance, "started_at": started.isoformat()}, 60
            )
            kept_id = uuid4().hex
            session.add(
                GatewayRequest(
                    id=kept_id,
                    model=model,
                    gateway_instance_id=instance,
                    gateway_instance_started_at=started,
                    started_at=utcnow(),
                    finished_at=None,
                )
            )
            await session.commit()
            await recover_abandoned_requests(session, store)
            kept = await session.get(GatewayRequest, kept_id)
            assert kept is not None
            assert kept.outcome is None
            assert kept.finished_at is None

    asyncio.run(load())
    assert live_started_holder["started"] == kv.plane._started_at


def test_runtime_unavailable_rejects_new_requests(cipher, upstream):
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    kv.store.set_available(False)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == RUNTIME_UNAVAILABLE
    assert upstream.requests == []


def test_least_active_prefers_idle_node(cipher, upstream):
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    asyncio.run(kv.store.acquire(SCOPE_NODE, NODE_A, 0, 900))
    asyncio.run(kv.store.acquire(SCOPE_NODE, NODE_A, 0, 900))
    asyncio.run(kv.store.acquire(SCOPE_NODE, NODE_A, 0, 900))
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 200
    assert upstream.requests[-1].url.host == "node-b.test"


def test_draining_and_unknown_nodes_are_not_schedulable(cipher, upstream):
    nodes = [
        node_dict(NODE_A, host="node-a.test", cipher=cipher, scheduling_state="DRAINING"),
        node_dict(NODE_B, host="node-b.test", cipher=cipher, health_state="UNKNOWN"),
    ]
    kv = build_plane(upstream, config=build_config(cipher, nodes=nodes), cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == NO_ELIGIBLE_NODE
    assert error["detail"][REASON_SCHEDULING] == 1
    assert upstream.requests == []


def test_full_pool_reports_saturation(cipher, upstream):
    nodes = [node_dict(NODE_A, host="node-a.test", cipher=cipher, max_concurrency=1)]
    kv = build_plane(upstream, config=build_config(cipher, nodes=nodes), cipher=cipher)
    asyncio.run(kv.store.acquire(SCOPE_NODE, NODE_A, 1, 900))
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == POOL_SATURATED


def test_quota_stop_blocks_new_requests(cipher, upstream):
    nodes = [
        node_dict(NODE_A, host="node-a.test", cipher=cipher, hourly_token_limit=10),
    ]
    kv = build_plane(upstream, config=build_config(cipher, nodes=nodes), cipher=cipher)
    buckets = usage_buckets(utcnow(), "Asia/Shanghai")
    asyncio.run(
        kv.store.record_usage(
            SCOPE_NODE, NODE_A, "hour", buckets["hour"], total_tokens=10, known=True
        )
    )
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == QUOTA_EXCEEDED
    assert upstream.requests == []


def test_atomic_lease_single_final_slot():
    async def scenario():
        store = InMemoryRuntimeStore()
        first, second = await asyncio.gather(
            store.acquire(SCOPE_NODE, NODE_A, 1, 900),
            store.acquire(SCOPE_NODE, NODE_A, 1, 900),
        )
        return first, second, await store.active(SCOPE_NODE, NODE_A)

    first, second, active = asyncio.run(scenario())
    granted = [value for value in (first, second) if value is not None]
    # 每请求唯一 lease ID，最后一个槽位只被一个请求拿到。
    assert len(granted) == 1 and isinstance(granted[0], str)
    assert active == 1


def test_unique_lease_ids_release_independently():
    async def scenario():
        store = InMemoryRuntimeStore()
        first = await store.acquire(SCOPE_NODE, NODE_A, 0, 900)
        second = await store.acquire(SCOPE_NODE, NODE_A, 0, 900)
        two = await store.active(SCOPE_NODE, NODE_A)
        await store.release(SCOPE_NODE, NODE_A, first)
        one = await store.active(SCOPE_NODE, NODE_A)
        await store.release(SCOPE_NODE, NODE_A, second)
        return first, second, two, one, await store.active(SCOPE_NODE, NODE_A)

    first, second, two, one, zero = asyncio.run(scenario())
    assert first != second
    assert (two, one, zero) == (2, 1, 0)


def test_expired_lease_is_pruned_and_renew_keeps_it_alive():
    async def scenario():
        store = InMemoryRuntimeStore()
        lease = await store.acquire(SCOPE_NODE, NODE_A, 1, 1)
        assert lease is not None
        # 未续租：TTL 到期后按各租约独立回收，唯一槽位自动腾出。
        await asyncio.sleep(1.1)
        assert await store.active(SCOPE_NODE, NODE_A) == 0
        reclaimed = await store.acquire(SCOPE_NODE, NODE_A, 1, 1)
        assert reclaimed is not None
        assert await store.renew(SCOPE_NODE, NODE_A, reclaimed, 900) is True
        await asyncio.sleep(1.1)
        # 续租后即便超过原 TTL 仍然活跃，槽位不会被误回收。
        assert await store.active(SCOPE_NODE, NODE_A) == 1
        await store.release(SCOPE_NODE, NODE_A, reclaimed)
        assert await store.renew(SCOPE_NODE, NODE_A, reclaimed, 900) is False

    asyncio.run(scenario())


def test_runtime_health_streaks_remove_and_restore():
    async def scenario():
        store = InMemoryRuntimeStore()
        state = None
        for _ in range(3):
            state = await store.record_probe(NODE_A, success=False)
        removed = state
        for _ in range(2):
            state = await store.record_probe(NODE_A, success=True)
        return removed, state, await store.health(NODE_A)

    removed, restored, final = asyncio.run(scenario())
    assert removed == "UNHEALTHY"
    assert restored == "HEALTHY"
    assert final == "HEALTHY"


def test_passive_failure_degrades_healthy_node():
    async def scenario():
        store = InMemoryRuntimeStore()
        await store.seed_health(NODE_A, "HEALTHY")
        await store.mark_degraded(NODE_A)
        return await store.health(NODE_A)

    assert asyncio.run(scenario()) == "DEGRADED"


def test_circuit_open_to_half_open_trial_and_recovery():
    async def scenario():
        store = InMemoryRuntimeStore()
        assert await store.circuit(NODE_A) == "CLOSED"
        # open_seconds=0 立即到期，进入 HALF_OPEN。
        await store.set_circuit_open(NODE_A, 0)
        assert await store.circuit(NODE_A) == "HALF_OPEN"
        assert await store.try_circuit_trial(NODE_A) is True
        assert await store.try_circuit_trial(NODE_A) is False  # 只放行少量试探
        await store.set_circuit_closed(NODE_A)
        assert await store.circuit(NODE_A) == "CLOSED"
        # 再次失败 → 重新打开；OPEN 窗口内不放行任何试探。
        await store.set_circuit_open(NODE_A, 30)
        assert await store.circuit(NODE_A) == "OPEN"
        assert await store.try_circuit_trial(NODE_A) is False

    asyncio.run(scenario())


def test_revoked_key_digests_are_append_only_and_ready():
    async def scenario():
        store = InMemoryRuntimeStore()
        assert await store.revocation_state_ready() is True
        await store.revoke_key_digest("digest-a")
        await store.revoke_key_digest("digest-b")
        # 追加同步绝不整表替换：并发吊销的条目必须保留。
        await store.sync_revoked_key_digests(["digest-c"])
        return (
            await store.is_key_revoked("digest-a"),
            await store.is_key_revoked("digest-b"),
            await store.is_key_revoked("digest-c"),
            await store.is_key_revoked("digest-missing"),
            await store.revocation_state_ready(),
        )

    a, b, c, missing, ready = asyncio.run(scenario())
    assert (a, b, c, missing, ready) == (True, True, True, False, True)


def test_is_key_revoked_fails_closed_when_ready_sentinel_missing():
    async def scenario():
        store = InMemoryRuntimeStore()
        await store.revoke_key_digest("digest-a")
        assert await store.is_key_revoked("digest-a") is True
        # 模拟 Redis 淘汰整个集合键：就绪哨兵与摘要一起丢失。is_key_revoked 本身必须
        # 失败关闭，绝不返回「未吊销」，否则两次调用之间清空会形成放行窗口。
        store.clear_revocation_state()
        ready = await store.revocation_state_ready()
        blocked = False
        try:
            await store.is_key_revoked("digest-a")
        except RuntimeError:
            blocked = True
        # DB 追加同步重建哨兵与摘要后恢复阻断。
        await store.sync_revoked_key_digests(["digest-a"])
        return (
            ready,
            blocked,
            await store.revocation_state_ready(),
            await store.is_key_revoked("digest-a"),
        )

    ready, blocked, restored, revoked = asyncio.run(scenario())
    assert (ready, blocked, restored, revoked) == (False, True, True, True)


def test_key_concurrency_is_enforced(cipher, upstream):
    key = key_dict(max_concurrency=1)
    kv = build_plane(upstream, config=build_config(cipher, key=key), cipher=cipher)
    asyncio.run(kv.store.acquire(SCOPE_KEY, "gk_test", 1, 900))
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "KEY_CONCURRENCY_EXCEEDED"


def test_header_auth_uses_encrypted_credential_only(cipher, upstream):
    nodes = [
        node_dict(
            NODE_A,
            host="node-a.test",
            cipher=cipher,
            auth_type="HEADER",
            auth_header="X-Api-Key",
        )
    ]
    kv = build_plane(upstream, config=build_config(cipher, nodes=nodes), cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 200
    sent = upstream.requests[-1]
    assert sent.headers["X-Api-Key"] == DOWNSTREAM_KEY
    # 客户端 Authorization 不转发到下游（除节点认证 Header 外不保留）。
    assert sent.headers.get("Authorization") != f"Bearer {RAW_KEY}"


def test_client_disconnect_releases_leases_and_records(cipher, upstream):
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    plane = kv.plane

    async def scenario():
        await plane.startup()
        try:
            key = plane.authenticate(f"Bearer {RAW_KEY}", config=plane.config_state.config)
            key_lease = await plane.runtime.acquire(SCOPE_KEY, key.id, 0, 900)
            assert key_lease is not None
            node_id = NODE_A
            node_lease = await plane.runtime.acquire(SCOPE_NODE, node_id, 0, 900)
            assert node_lease is not None
            record = RequestRecord(id=uuid4().hex, started_at=utcnow(), streaming=True, model=MODEL)
            record.key_id = key.id
            record.node_id = node_id
            node = plane.config_state.config.nodes[node_id]
            request = plane.proxy.build_request(
                node,
                "POST",
                "/v1/chat/completions",
                headers={},
                content=json.dumps({"stream": True}).encode(),
            )
            upstream_response = await plane.proxy.send(request, stream=True)
            renewals = plane._start_lease_renewals((SCOPE_NODE, node_id, node_lease))
            generator = plane._stream_body(
                upstream_response,
                record,
                key,
                node_id,
                node.runtime_scope,
                node_lease,
                key_lease,
                renewals,
            )
            await generator.__anext__()
            await generator.aclose()
            return (
                record,
                await plane.runtime.active(SCOPE_NODE, node_id),
                await plane.runtime.active(SCOPE_KEY, key.id),
            )
        finally:
            await plane.shutdown()

    record, node_active, key_active = asyncio.run(scenario())
    assert record.stream_interrupted is True
    assert record.error_code == CLIENT_DISCONNECTED
    assert node_active == 0
    assert key_active == 0
    rows = asyncio.run(_fetch_logs())
    logged = next(row for row in rows if row.id == record.id)
    assert logged.stream_interrupted is True
    assert logged.error_code == CLIENT_DISCONNECTED


def test_heartbeat_registers_and_retires_instance(cipher, upstream):
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)

    async def scenario():
        await kv.plane.startup()
        await kv.plane.heartbeat_now()
        payload = kv.store.instances()
        await kv.plane.shutdown()
        return payload, kv.store.instances()

    registered, after_shutdown = asyncio.run(scenario())
    assert "test-instance" in registered
    assert "config_version" not in registered["test-instance"]
    assert registered["test-instance"]["app_version"]
    assert "test-instance" not in after_shutdown

def test_scheduler_reads_live_runtime_health_not_snapshot(cipher, upstream):
    nodes = [node_dict(NODE_A, host="node-a.test", cipher=cipher, health_state="HEALTHY")]
    store = InMemoryRuntimeStore()
    kv = build_plane(
        upstream,
        config=build_config(cipher, nodes=nodes),
        cipher=cipher,
        store=store,
        seed_snapshot_health=False,
    )
    # 配置视图仍是 HEALTHY，但运行态连接作用域健康为 UNHEALTHY。
    node = kv.plane.config_state.config.nodes[NODE_A]
    asyncio.run(store.seed_health(node.runtime_scope, "UNHEALTHY"))
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 503
    assert upstream.requests == []


def test_background_probe_removes_and_restores_node(cipher, upstream):
    probe = FakeProbe()
    nodes = [node_dict(NODE_A, host="node-a.test", cipher=cipher, health_state="HEALTHY")]
    store = InMemoryRuntimeStore()
    kv = build_plane(
        upstream,
        config=build_config(cipher, nodes=nodes),
        cipher=cipher,
        store=store,
        probe_client=probe,
    )
    node = kv.plane.config_state.config.nodes[NODE_A]
    scope = node.runtime_scope

    async def scenario():
        probe.success = False
        for _ in range(3):
            await kv.plane._probe_node(node)
        removed = await store.health(scope)
        opened = await store.circuit(scope)
        probe.success = True
        for _ in range(2):
            await kv.plane._probe_node(node)
        return removed, opened, await store.health(scope), await store.circuit(scope)

    removed, opened, restored, closed = asyncio.run(scenario())
    assert removed == "UNHEALTHY"
    assert opened == "OPEN"
    assert restored == "HEALTHY"
    assert closed == "CLOSED"
    # 默认验证策略是一次 SSE；轮次里只有 STREAM 一步。
    assert probe.calls and probe.calls[0][1] == ("STREAM",)



def test_passive_transport_failure_degrades_node(cipher, upstream):
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    upstream.fail_hosts = {"node-a.test", "node-b.test"}
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code in (502, 504)
    # 被动传输故障降级为 DEGRADED，而不是直接摘除（摘除由连续主动探测负责）。
    node_a = kv.plane.config_state.config.nodes[NODE_A]
    node_b = kv.plane.config_state.config.nodes[NODE_B]
    assert asyncio.run(kv.store.health(node_a.runtime_scope)) == "DEGRADED"
    assert asyncio.run(kv.store.health(node_b.runtime_scope)) == "DEGRADED"


def test_missing_runtime_health_does_not_fall_back_to_snapshot(cipher, upstream):
    # 配置视图健康为 HEALTHY，但运行态缺失（如被 reset 清空、Redis 丢失）：
    # 必须视为 UNKNOWN 而不是回退快照 HEALTHY，否则从未验证的连接仍会被调度。
    nodes = [node_dict(NODE_A, host="node-a.test", cipher=cipher, health_state="HEALTHY")]
    kv = build_plane(
        upstream,
        config=build_config(cipher, nodes=nodes),
        cipher=cipher,
        seed_snapshot_health=False,
    )
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == NO_ELIGIBLE_NODE
    assert upstream.requests == []


def test_reset_runtime_health_does_not_fall_back_to_snapshot(cipher, upstream):
    # 直接复现回归：运行态健康被 reset 清空后，调度绝不能回退到配置视图 HEALTHY。
    nodes = [node_dict(NODE_A, host="node-a.test", cipher=cipher, health_state="HEALTHY")]
    store = InMemoryRuntimeStore()
    kv = build_plane(upstream, config=build_config(cipher, nodes=nodes), cipher=cipher, store=store)
    node = kv.plane.config_state.config.nodes[NODE_A]
    assert asyncio.run(store.health(node.runtime_scope)) == "HEALTHY"
    asyncio.run(store.reset_health(node.runtime_scope))
    assert asyncio.run(store.health(node.runtime_scope)) is None

    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == NO_ELIGIBLE_NODE
    assert upstream.requests == []


def test_new_connection_health_is_isolated_by_scope(cipher, upstream):
    # 旧连接的健康作用域已是 HEALTHY；新保存配置换成不同连接后，旧健康不得
    # 授权新配置，旧实例后续探测成功也不能把新连接判为 HEALTHY。
    old_config = build_config(
        cipher,
        nodes=[node_dict(NODE_A, host="node-a.test", cipher=cipher)],
    )
    old_scope = old_config.nodes[NODE_A].runtime_scope

    new_config = build_config(
        cipher,
        nodes=[node_dict(NODE_A, host="node-a-v2.test", cipher=cipher, health_state="UNKNOWN")],
    )
    new_scope = new_config.nodes[NODE_A].runtime_scope
    assert new_scope != old_scope

    store = InMemoryRuntimeStore()
    asyncio.run(store.seed_health(old_scope, "HEALTHY"))
    kv = build_plane(upstream, config=new_config, cipher=cipher, store=store)

    def chat(client: TestClient):
        return client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )

    with TestClient(create_gateway_app(kv.plane)) as client:
        blocked = chat(client)
        assert blocked.status_code == 503
        assert blocked.json()["error"]["code"] == NO_ELIGIBLE_NODE
        assert upstream.requests == []

        # 旧连接后续探测成功（旧实例/旧版本）不能把新配置判为 HEALTHY。
        for _ in range(2):
            asyncio.run(store.record_probe(old_scope, success=True))
        still_blocked = chat(client)
        assert still_blocked.status_code == 503
        assert upstream.requests == []

        # 新连接自己的完整探测成功后才重新可调度。
        for _ in range(2):
            asyncio.run(store.record_probe(new_scope, success=True))
        recovered = chat(client)
        assert recovered.status_code == 200, recovered.text
        assert upstream.requests[-1].url.host == "node-a-v2.test"


@pytest.mark.skipif(
    not REDIS_URL,
    reason="未提供 ASTRA_TEST_REDIS_URL：Lua 脚本未在真实 Redis 上联调，默认仅验证进程内等价语义",
)
async def _assert_half_open_snapshot_and_transition(store) -> None:
    scope = f"snap-{uuid4().hex[:8]}"
    await store.set_circuit_open(scope, 0)
    if isinstance(store, RedisRuntimeStore):
        await asyncio.sleep(0.05)
    snapshot = await store.circuit_snapshot(scope)
    assert snapshot == CircuitSnapshot("HALF_OPEN", 0)
    assert await store.try_circuit_trial(scope) is True
    assert await store.circuit_snapshot(scope) == CircuitSnapshot("HALF_OPEN", 1)
    # 再读一次仍不消耗试探。
    assert await store.circuit_snapshot(scope) == CircuitSnapshot("HALF_OPEN", 1)
    assert await store.try_circuit_trial(scope) is False
    opened = await asyncio.gather(
        store.transition_circuit_open(scope, 30),
        store.transition_circuit_open(scope, 30),
    )
    assert sum(1 for item in opened if item.changed) == 1


def test_memory_half_open_snapshot_does_not_consume_trials():
    asyncio.run(_assert_half_open_snapshot_and_transition(InMemoryRuntimeStore()))


@pytest.mark.skipif(
    not REDIS_URL,
    reason="未提供 ASTRA_TEST_REDIS_URL：Lua 脚本未在真实 Redis 上联调，默认仅验证进程内等价语义",
)
def test_redis_lua_scripts_against_real_redis():
    async def scenario():
        store = RedisRuntimeStore(REDIS_URL)
        scope_id = f"redis-{uuid4().hex[:8]}"
        try:
            lease = await store.acquire(SCOPE_NODE, scope_id, 1, 5)
            assert lease is not None
            # 限额检查在 Lua 内完成，最后一个槽位不会超卖。
            assert await store.acquire(SCOPE_NODE, scope_id, 1, 5) is None
            assert await store.renew(SCOPE_NODE, scope_id, lease, 5) is True
            await store.release(SCOPE_NODE, scope_id, lease)
            assert await store.active(SCOPE_NODE, scope_id) == 0
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    not REDIS_URL,
    reason="未提供 ASTRA_TEST_REDIS_URL：半开快照与原子熔断转移未在真实 Redis 上联调",
)
def test_redis_half_open_snapshot_matches_memory():
    async def scenario():
        store = RedisRuntimeStore(REDIS_URL)
        try:
            await _assert_half_open_snapshot_and_transition(store)
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    not REDIS_URL,
    reason="未提供 ASTRA_TEST_REDIS_URL：吊销集合与就绪哨兵未在真实 Redis 上联调",
)
def test_redis_revocation_set_is_append_only_and_marked_ready():
    async def scenario():
        store = RedisRuntimeStore(REDIS_URL)
        suffix = uuid4().hex[:8]
        member_prefix = f"astra-test-revoked:{os.getpid()}:"
        first, second, third = (f"{member_prefix}{suffix}-{name}" for name in ("a", "b", "c"))
        try:
            await store.sync_revoked_key_digests([first, second])
            assert await store.revocation_state_ready() is True
            await store.revoke_key_digest(third)
            assert await store.is_key_revoked(first) is True
            assert await store.is_key_revoked(third) is True
            assert await store.is_key_revoked(f"rev-{suffix}-missing") is False
            # 追加同步逐条 SADD，既有成员与并发吊销都不会被清空。
            await store.sync_revoked_key_digests([third])
            assert await store.is_key_revoked(second) is True
            # 整集合键被 maxmemory 淘汰/清空：哨兵与摘要一起消失，绝不放行旧 Key。
            await store._connection().delete(REVOKED_KEYS_KEY)
            assert await store.revocation_state_ready() is False
            with pytest.raises(RuntimeError):
                await store.is_key_revoked(first)
            # DB 追加同步用一次 SADD 重建摘要与哨兵，阻断恢复。
            await store.sync_revoked_key_digests([first, second])
            assert await store.revocation_state_ready() is True
            assert await store.is_key_revoked(first) is True
        finally:
            await store.close()

    asyncio.run(scenario())


def test_frame_has_content_supports_reasoning_and_tool_calls():
    from app.gateway.server import _frame_has_content

    # 1. role-only 不算
    assert not _frame_has_content({"choices": [{"delta": {"role": "assistant"}}]})
    # 2. 空内容不算
    assert not _frame_has_content({"choices": [{"delta": {"content": ""}}]})
    assert not _frame_has_content({"choices": [{"delta": {"content": None}}]})
    # 3. 正文内容算
    assert _frame_has_content({"choices": [{"delta": {"content": "Hello"}}]})
    # 4. reasoning_content (DeepSeek R1 等推理模型) 算首 token
    assert _frame_has_content({"choices": [{"delta": {"reasoning_content": "Thinking..."}}]})
    assert _frame_has_content({"choices": [{"delta": {"reasoning": "Plan:"}}]})
    assert not _frame_has_content({"choices": [{"delta": {"reasoning_content": ""}}]})
    # 5. tool_calls (带 ID 或函数名/参数) 算首 token
    assert _frame_has_content({"choices": [{"delta": {"tool_calls": [{"id": "call_123"}]}}]})
    empty_call = {"function": {"name": "", "arguments": ""}}
    assert not _frame_has_content({"choices": [{"delta": {"tool_calls": [empty_call]}}]})


def test_output_tps_calculation_rules():
    from app.gateway.server import GatewayDataPlane, RequestRecord

    now = utcnow()
    # 1. 成功且流式、Token 数 > 1：正常计算生成速度 Token/s
    rec = RequestRecord(
        id="r1",
        started_at=now,
        streaming=True,
        status_code=200,
        stream_interrupted=False,
        duration_ms=2500,
        ttft_ms=500,  # 生成耗时 = (2500 - 500) / 1000 = 2.0s
    )
    plane = GatewayDataPlane(
        session_factory=SessionLocal,
        config_state=GatewayConfigState(),
        proxy=GatewayProxy(),
        runtime=InMemoryRuntimeStore(),
        cipher=CredentialCipher(Fernet.generate_key().decode()),
    )
    asyncio.run(plane._record_usage(rec, {"prompt_tokens": 10, "completion_tokens": 20}))
    assert rec.output_tps == 10.0  # 20 tokens / 2.0s = 10.0 TPS

    # 2. 单 Token：首 token 即末 token，不伪造生成速度
    rec_single = RequestRecord(
        id="r2",
        started_at=now,
        streaming=True,
        status_code=200,
        stream_interrupted=False,
        duration_ms=2500,
        ttft_ms=500,
    )
    asyncio.run(plane._record_usage(rec_single, {"prompt_tokens": 10, "completion_tokens": 1}))
    assert rec_single.output_tps is None

    # 3. 中途断开：不显示正常性能值
    rec_interrupted = RequestRecord(
        id="r3",
        started_at=now,
        streaming=True,
        status_code=200,
        stream_interrupted=True,
        duration_ms=2500,
        ttft_ms=500,
    )
    asyncio.run(plane._record_usage(rec_interrupted, {"prompt_tokens": 10, "completion_tokens": 20}))
    assert rec_interrupted.output_tps is None

    # 4. 非流式响应：不伪造 TPS
    rec_non_stream = RequestRecord(
        id="r4",
        started_at=now,
        streaming=False,
        status_code=200,
        stream_interrupted=False,
        duration_ms=2500,
        ttft_ms=500,
    )
    asyncio.run(plane._record_usage(rec_non_stream, {"prompt_tokens": 10, "completion_tokens": 20}))
    assert rec_non_stream.output_tps is None


def test_transparent_model_routing_a_to_b():
    # 测试请求模型 A (GLM-5.2) -> 实际模型 B (GLM-5.3) 的透明路由
    client_model = "GLM-5.2"
    actual_model = "GLM-5.3"
    cipher = CredentialCipher(Fernet.generate_key().decode())
    upstream = MockUpstream()
    node = node_dict(NODE_A, host="node-a.test", cipher=cipher, models=(actual_model,))
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=[node],
            routes=[
                {
                    "id": "gr-glm",
                    "model": client_model,
                    "target_model": actual_model,
                    "pool_id": POOL_ID,
                    "enabled": True,
                    "weight": 1,
                }
            ],
            keys=[key_dict(allowed_models=[client_model])],
        )
    )
    kv = build_plane(upstream, config=config, cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_headers(),
            json={"model": client_model, "messages": [{"role": "user", "content": "hello"}]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body.get("model") == client_model

    assert len(upstream.requests) == 1
    upstream_req_payload = json.loads(upstream.requests[0].content)
    assert upstream_req_payload["model"] == actual_model

    async def check_db():
        async with SessionLocal() as session:
            row = (
                await session.scalars(
                    select(GatewayRequest).order_by(GatewayRequest.started_at.desc()).limit(1)
                )
            ).first()
            assert row is not None
            assert row.model == client_model
            assert row.actual_model == actual_model
            assert row.status_code == 200

    asyncio.run(check_db())



def test_stream_with_only_the_terminal_marker_is_a_failure(cipher, upstream):
    upstream.stream_chunks = [b"data: [DONE]\n\n"]
    upstream.stream_usage = None
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [], "stream": True},
            headers=auth_headers(),
        )
        assert response.status_code == 200
        assert "[DONE]" in response.text

    latest = asyncio.run(_fetch_logs())[0]
    assert latest.streaming is True
    assert latest.error_code == "STREAM_EMPTY"
    assert latest.stream_interrupted is True


def test_stream_without_a_terminal_marker_is_incomplete(cipher, upstream):
    upstream.stream_chunks = [
        b'data: {"id":"c","choices":[{"delta":{"content":"he"}}]}\n\n',
    ]
    upstream.stream_usage = None
    upstream.stream_terminal = False
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [], "stream": True},
            headers=auth_headers(),
        )
        assert response.status_code == 200

    latest = asyncio.run(_fetch_logs())[0]
    assert latest.error_code == "STREAM_INCOMPLETE"
    assert latest.stream_interrupted is True


def test_stream_with_wrong_content_type_is_a_protocol_error(cipher, upstream):
    upstream.stream_content_type = "application/json"
    kv = build_plane(upstream, config=build_config(cipher), cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [], "stream": True},
            headers=auth_headers(),
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_PROTOCOL_ERROR"
    assert len(upstream.requests) == 1
    latest = asyncio.run(_fetch_logs())[0]
    assert latest.error_code == "UPSTREAM_PROTOCOL_ERROR"


def test_stream_model_alias_is_rewritten_across_chunk_boundaries(cipher, upstream):
    frame = json.dumps(
        {
            "id": "c",
            "model": "real-model",
            "choices": [{"delta": {"content": "你好"}}],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    data = b"data: " + frame + b"\n\n"
    split = data.index("你".encode()) + 1  # 切在多字节字符中间
    upstream.stream_chunks = [data[:split], data[split:], b"data: [DONE]\n\n"]
    upstream.stream_usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    config = GatewayConfig.from_snapshot(
        _snapshot(
            nodes=[node_dict(NODE_A, host="node-a.test", cipher=cipher, models=("real-model",))],
            routes=[
                {
                    "id": "gr_alias",
                    "model": MODEL,
                    "pool_id": POOL_ID,
                    "enabled": True,
                    "weight": 1,
                    "target_model": "real-model",
                }
            ],
            keys=[key_dict()],
        )
    )
    kv = build_plane(upstream, config=config, cipher=cipher)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [], "stream": True},
            headers=auth_headers(),
        )
        assert response.status_code == 200
        body = response.text

    frames = [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ") and "[DONE]" not in line
    ]
    modelled = [item for item in frames if "model" in item]
    assert modelled and all(item["model"] == MODEL for item in modelled)
    assert "real-model" not in body
    assert "你好" in body

    latest = asyncio.run(_fetch_logs())[0]
    assert latest.actual_model == "real-model"
    assert latest.input_tokens == 1 and latest.total_tokens == 3
    assert latest.error_code is None


def test_stream_model_rewriter_is_incremental():
    from app.gateway.server import StreamModelRewriter

    rewriter = StreamModelRewriter("client-model")
    frame = b'data: {"model":"upstream","choices":[]}\n'
    assert rewriter.feed(frame[:10]) == b""
    rewritten = rewriter.feed(frame[10:])
    assert json.loads(rewritten[len(b"data: ") :].decode())["model"] == "client-model"
    assert rewriter.feed(b"data: [DONE]\n") == b"data: [DONE]\n"
    assert rewriter.feed(b": ping\n") == b": ping\n"
    rewriter.feed(b"partial")
    assert rewriter.flush() == b"partial"


@pytest.mark.parametrize("period", ["hour", "day"])
def test_key_quota_blocks_before_any_upstream_request(cipher, upstream, period):
    limit_field = "hourly_token_limit" if period == "hour" else "daily_token_limit"
    config = build_config(cipher, key=key_dict(**{limit_field: 10}))
    kv = build_plane(upstream, config=config, cipher=cipher)
    buckets = usage_buckets(utcnow(), "Asia/Shanghai")
    asyncio.run(
        kv.store.record_usage(
            SCOPE_KEY, "gk_test", period, buckets[period], total_tokens=10, known=True
        )
    )
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "KEY_QUOTA_EXCEEDED"
    assert upstream.requests == []


def test_alert_and_record_policies_never_change_weight_or_block(cipher, upstream):
    from app.gateway.runtime import TokenUsage
    from app.gateway.scheduler import effective_weight, quota_state

    assert quota_state(79, 100, "ALERT") == "OK"
    assert quota_state(100, 100, "ALERT") == "ALERT"
    assert quota_state(120, 100, "RECORD") == "RECORD"
    assert quota_state(100, 100, "STOP") == "STOP"

    node = build_config(
        cipher,
        nodes=[node_dict(NODE_A, host="node-a.test", cipher=cipher, quota_policy="ALERT")],
    ).nodes[NODE_A]
    base = effective_weight(node, TokenUsage(), TokenUsage(), health_state="HEALTHY")
    for used in (79, 100, 120):
        usage = TokenUsage(total_tokens=used)
        assert effective_weight(node, usage, usage, health_state="HEALTHY") == base

    # ALERT 达限仍可服务，且上游只收到一次请求。
    kv = build_plane(
        upstream,
        config=build_config(
            cipher,
            nodes=[
                node_dict(
                    NODE_A, host="node-a.test", cipher=cipher, hourly_token_limit=10,
                    quota_policy="ALERT",
                )
            ],
        ),
        cipher=cipher,
    )
    buckets = usage_buckets(utcnow(), "Asia/Shanghai")
    asyncio.run(
        kv.store.record_usage(
            SCOPE_NODE, NODE_A, "hour", buckets["hour"], total_tokens=12, known=True
        )
    )
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": []},
            headers=auth_headers(),
        )
    assert response.status_code == 200
    assert len(upstream.requests) == 1


@pytest.mark.asyncio
async def test_stream_body_yields_first_chunk_before_upstream_completes(cipher):
    """整流缓冲会破坏首字延迟与取消；生成器必须在结束标记前先产出第一块。"""

    import threading

    gate = threading.Event()

    class GatedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"c","choices":[{"delta":{"content":"a"}}]}\n\n'
            await asyncio.to_thread(gate.wait)
            yield b"data: [DONE]\n\n"

        async def aclose(self) -> None:
            return None

    config = build_config(cipher)
    kv = build_plane(
        upstream=MockUpstream(), config=config, cipher=cipher, seed_snapshot_health=False
    )
    record = RequestRecord(
        id=uuid4().hex,
        started_at=utcnow(),
        gateway_instance_id="test-instance",
        gateway_instance_started_at=utcnow(),
    )
    record.streaming = True
    key = next(iter(config.keys.values()))
    upstream_response = httpx.Response(
        200, stream=GatedStream(), headers={"content-type": "text/event-stream"}
    )
    record.status_code = 200
    stream = kv.plane._stream_body(
        upstream_response,
        record,
        key,
        NODE_A,
        config.nodes[NODE_A].runtime_scope,
        None,
        None,
        [],
    )
    first = await asyncio.wait_for(stream.__anext__(), timeout=2)
    assert b'"a"' in first
    gate.set()
    rest = b"".join([chunk async for chunk in stream])
    assert b"[DONE]" in rest


@pytest.mark.asyncio
async def test_stream_body_records_an_upstream_error_after_content(cipher):
    """上游写到一半抛错：日志必须是 STREAM_INTERRUPTED，且节点被动失败。"""

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"c","choices":[{"delta":{"content":"a"}}]}\n\n'
            raise httpx.ReadError("upstream dropped")

        async def aclose(self) -> None:
            return None

    config = build_config(cipher)
    kv = build_plane(
        upstream=MockUpstream(), config=config, cipher=cipher, seed_snapshot_health=False
    )
    record = RequestRecord(
        id=uuid4().hex,
        started_at=utcnow(),
        gateway_instance_id="test-instance",
        gateway_instance_started_at=utcnow(),
    )
    record.streaming = True
    record.status_code = 200
    key = next(iter(config.keys.values()))
    upstream_response = httpx.Response(
        200, stream=BrokenStream(), headers={"content-type": "text/event-stream"}
    )
    stream = kv.plane._stream_body(
        upstream_response,
        record,
        key,
        NODE_A,
        config.nodes[NODE_A].runtime_scope,
        None,
        None,
        [],
    )
    with pytest.raises(httpx.ReadError):
        async for _ in stream:
            pass

    latest = (await _fetch_logs())[0]
    assert latest.error_code == "STREAM_INTERRUPTED"
    assert latest.stream_interrupted is True

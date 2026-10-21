"""API 中转控制面测试：保存校验、凭据脱敏、安全下线、探测与审计。"""

import asyncio
import json
import socket
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.gateway_routes import (
    gateway_cipher,
    gateway_probe_client,
    gateway_runtime,
)
from app.database import SessionLocal
from app.gateway.config import ConfigLoader, GatewayConfigState
from app.gateway.health import record_background_probe
from app.gateway.models import (
    GatewayHealthCheck,
    GatewayKey,
    GatewayNode,
    GatewayProbeRun,
    GatewayRequest,
)
from app.gateway.probe import HttpGatewayProbeClient, ProbeOutcome
from app.gateway.proxy import GatewayProxy
from app.gateway.runtime import (
    SCOPE_NODE,
    SCOPE_PROBE,
    InMemoryRuntimeStore,
    node_runtime_scope,
)
from app.gateway.security import CredentialCipher, gateway_key_digest
from app.gateway.server import GatewayDataPlane, create_gateway_app
from app.main import app


class FakeProbe:
    def __init__(self) -> None:
        self.success = True
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def probe(self, node, probe_types, api_key):
        self.calls.append((node.id, tuple(probe_types)))
        return [ProbeOutcome(probe_type, self.success, 3) for probe_type in probe_types]


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def test_model_routes_can_be_read_with_real_probe_client(gateway_env):
    # 员工首次建立 Session 会读取路由；使用真实依赖，避免 FakeProbe 掩盖
    # 客户端构造参数不匹配而令整个路由接口返回 500。
    override = app.dependency_overrides.pop(gateway_probe_client)
    try:
        with TestClient(app) as client:
            response = client.get("/api/gateway/routes")
        assert response.status_code == 200, response.text
        assert isinstance(response.json(), list)
    finally:
        app.dependency_overrides[gateway_probe_client] = override


@pytest.fixture
def gateway_env():
    master_key = Fernet.generate_key().decode()
    probe = FakeProbe()
    runtime = InMemoryRuntimeStore()
    overrides = {
        gateway_cipher: lambda: CredentialCipher(master_key),
        gateway_probe_client: lambda: probe,
        gateway_runtime: lambda: runtime,
    }
    app.dependency_overrides.update(overrides)
    try:
        yield SimpleNamespace(
            master_key=master_key, probe=probe, runtime=runtime
        )
    finally:
        for dependency in overrides:
            app.dependency_overrides.pop(dependency, None)


def create_pool(client: TestClient, *, enabled: bool = True) -> dict:
    response = client.post(
        "/api/gateway/pools",
        json={"name": unique("pool"), "enabled": enabled},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_node(
    client: TestClient, pool_id: str, *, api_key: str | None = None, **extra
) -> dict:
    payload = {
        "name": unique("node"),
        "pool_id": pool_id,
        "base_url": "https://proxy.example.com",
        "models": ["gpt-test"],
    }
    if api_key is not None:
        payload["api_key"] = api_key
    payload.update(extra)
    response = client.post("/api/gateway/nodes", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def read_scope(node_id: str) -> str:
    async def run() -> str:
        async with SessionLocal() as session:
            node = await session.get(GatewayNode, node_id)
            return node_runtime_scope(node)

    return asyncio.run(run())


def create_route(client: TestClient, pool_id: str, model: str, **extra) -> dict:
    payload = {"model": model, "pool_id": pool_id}
    payload.update(extra)
    response = client.post(
        "/api/gateway/routes",
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_control_plane_crud_without_management_credentials(gateway_env):
    with TestClient(app) as client:
        assert client.get("/api/gateway/pools").status_code == 200
        pool = create_pool(client)
        updated = client.patch(
            f"/api/gateway/pools/{pool['id']}", json={"name": unique("renamed")}
        )
        assert updated.status_code == 200
        assert any(item["id"] == pool["id"] for item in client.get("/api/gateway/pools").json())
        assert client.delete(f"/api/gateway/pools/{pool['id']}").status_code == 204
        assert all(item["id"] != pool["id"] for item in client.get("/api/gateway/pools").json())
        assert client.get("/api/gateway/permissions").status_code == 404


def test_control_plane_audit_uses_astra_source_despite_forged_identity(gateway_env):
    with TestClient(app) as client:
        forged = {
            "Authorization": "Bearer arbitrary-token",
            "X-Astra-User-Id": "forged-admin",
            "X-Astra-Role": "publish",
        }
        created = client.post("/api/gateway/pools", json={"name": unique("pool")}, headers=forged)
        assert created.status_code == 201, created.text
        audits = client.get(
            "/api/gateway/audits",
            params={"resource_type": "pool", "resource_id": created.json()["id"]},
        ).json()
        assert audits
        assert {(item["actor"], item["actor_role"]) for item in audits} == {("astra", "system")}


def test_node_credential_encrypted_and_key_hashed_with_redacted_responses(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"], api_key="sk-node-secret")
        assert node["has_api_key"] is True
        response_text = json.dumps(node)
        assert "sk-node-secret" not in response_text
        assert "api_key_encrypted" not in response_text

        created_key = client.post(
            "/api/gateway/keys",
            json={"name": unique("key"), "allowed_models": ["gpt-test"]},
        )
        assert created_key.status_code == 201, created_key.text
        raw_key = created_key.json()["key"]
        assert raw_key.startswith("agw_")
        assert created_key.json()["key_prefix"] == raw_key[:12]

        listed = client.get("/api/gateway/keys").json()
        assert all("key_digest" not in item for item in listed)
        assert all("key" not in item for item in listed)

        async def read_secrets():
            async with SessionLocal() as session:
                stored_node = await session.get(GatewayNode, node["id"])
                key = await session.get(GatewayKey, created_key.json()["id"])
                return stored_node.api_key_encrypted, key.key_digest, key.key_prefix

        stored_cipher, stored_digest, stored_prefix = asyncio.run(read_secrets())
        assert stored_cipher and stored_cipher != "sk-node-secret"
        assert CredentialCipher(gateway_env.master_key).decrypt(stored_cipher) == "sk-node-secret"
        assert stored_digest == gateway_key_digest(raw_key)
        assert raw_key not in stored_digest
        assert stored_prefix == raw_key[:12]


def test_revoke_key_blocks_shared_state_before_db_and_fails_closed(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        create_node(client, pool["id"])
        created = client.post(
            "/api/gateway/keys",
            json={"name": unique("key"), "allowed_models": ["gpt-test"]},
        )
        assert created.status_code == 201, created.text
        key_id = created.json()["id"]
        digest = gateway_key_digest(created.json()["key"])

        async def read_key():
            async with SessionLocal() as session:
                return await session.get(GatewayKey, key_id)

        # Redis 不可用：吊销必须结构化 503，且 DB 吊销不得提交——证明共享阻断先写。
        gateway_env.runtime.set_available(False)
        failed = client.post(f"/api/gateway/keys/{key_id}/revoke")
        assert failed.status_code == 503
        assert asyncio.run(read_key()).revoked_at is None

        # 恢复后：先追加共享阻断状态，再提交 DB 吊销，返回成功。
        gateway_env.runtime.set_available(True)
        revoked = client.post(f"/api/gateway/keys/{key_id}/revoke")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["revoked_at"] is not None
        assert asyncio.run(gateway_env.runtime.is_key_revoked(digest)) is True
        assert asyncio.run(read_key()).revoked_at is not None

        audits = client.get(
            "/api/gateway/audits",
            params={"resource_type": "key", "resource_id": key_id},
        ).json()
        assert any(item["action"] == "key.revoke" for item in audits)
        assert any(item["id"] == key_id for item in client.get("/api/gateway/keys").json())


@pytest.mark.parametrize("state", ["enabled", "disabled", "revoked"])
def test_delete_key_preserves_history_and_permanent_revocation(gateway_env, state):
    with TestClient(app) as client:
        created = client.post("/api/gateway/keys", json={"name": unique("delete-key")}).json()
        key_id = created["id"]
        digest = gateway_key_digest(created["key"])
        if state == "disabled":
            assert client.patch(f"/api/gateway/keys/{key_id}", json={"enabled": False}).status_code == 200
        elif state == "revoked":
            assert client.post(f"/api/gateway/keys/{key_id}/revoke").status_code == 200
        original_revoked_at = read_key_row(key_id).revoked_at
        request_id = unique("historical-request")

        async def save_request():
            async with SessionLocal.begin() as session:
                session.add(GatewayRequest(id=request_id, key_id=key_id, status_code=200, total_tokens=9))

        asyncio.run(save_request())
        cached_config = asyncio.run(ConfigLoader(SessionLocal).load_current())
        assert digest in cached_config.keys
        deleted = client.delete(f"/api/gateway/keys/{key_id}")
        assert deleted.status_code == 204, deleted.text
        assert deleted.content == b""
        assert all(item["id"] != key_id for item in client.get("/api/gateway/keys").json())
        stored = read_key_row(key_id)
        assert stored.deleted_at is not None
        assert stored.revoked_at is not None
        assert stored.enabled is False
        assert stored.key_digest == digest
        if original_revoked_at is not None:
            assert stored.revoked_at == original_revoked_at
        assert asyncio.run(gateway_env.runtime.is_key_revoked(digest)) is True
        assert digest not in asyncio.run(ConfigLoader(SessionLocal).load_current()).keys

        plane = GatewayDataPlane(
            config_state=GatewayConfigState(cached_config), runtime=gateway_env.runtime,
            proxy=GatewayProxy(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
            cipher=CredentialCipher(None), session_factory=SessionLocal,
        )
        with TestClient(create_gateway_app(plane)) as data_client:
            rejected = data_client.get("/v1/models", headers={"Authorization": f"Bearer {created['key']}"})
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "KEY_DISABLED"

        assert client.patch(f"/api/gateway/keys/{key_id}", json={"enabled": True}).status_code == 404
        assert client.post(f"/api/gateway/keys/{key_id}/revoke").status_code == 404
        assert client.delete(f"/api/gateway/keys/{key_id}").status_code == 404

        audits = client.get(
            "/api/gateway/audits", params={"resource_type": "key", "resource_id": key_id}
        ).json()
        assert any(item["action"] == "key.create" for item in audits)
        deletion = next(item for item in audits if item["action"] == "key.delete")
        assert deletion["before"]["deleted_at"] is None
        assert deletion["after"]["deleted_at"] is not None
        assert created["key"] not in json.dumps(audits)
        assert digest not in json.dumps(audits)

        async def check_history_and_rebuild_revocation():
            async with SessionLocal() as session:
                request = await session.get(GatewayRequest, request_id)
                assert request.key_id == key_id
                assert request.total_tokens == 9
            # 使用全新运行态，证明删除后的持久行仍参与数据面的阻断重建。
            runtime = InMemoryRuntimeStore()
            plane = GatewayDataPlane(
                config_state=GatewayConfigState(), runtime=runtime,
                proxy=GatewayProxy(), cipher=CredentialCipher(None), session_factory=SessionLocal,
            )
            assert await plane.sync_revocation_state() is True
            assert await runtime.is_key_revoked(digest) is True

        asyncio.run(check_history_and_rebuild_revocation())


def test_delete_key_fails_closed_and_can_retry(gateway_env):
    with TestClient(app) as client:
        created = client.post("/api/gateway/keys", json={"name": unique("delete-retry")}).json()
        key_id = created["id"]
        gateway_env.runtime.set_available(False)
        failed = client.delete(f"/api/gateway/keys/{key_id}")
        assert failed.status_code == 503, failed.text
        assert failed.json()["detail"]
        stored = read_key_row(key_id)
        assert stored.deleted_at is None
        assert stored.revoked_at is None
        assert stored.enabled is True
        assert any(item["id"] == key_id for item in client.get("/api/gateway/keys").json())
        audits = client.get(
            "/api/gateway/audits", params={"resource_type": "key", "resource_id": key_id}
        ).json()
        assert not any(item["action"] == "key.delete" for item in audits)

        gateway_env.runtime.set_available(True)
        assert client.delete(f"/api/gateway/keys/{key_id}").status_code == 204
        assert read_key_row(key_id).deleted_at is not None


def revoked_key_digests() -> set[str]:
    async def run() -> set[str]:
        async with SessionLocal() as session:
            rows = await session.scalars(
                select(GatewayKey.key_digest).where(GatewayKey.revoked_at.is_not(None))
            )
            return {str(digest) for digest in rows if digest}

    return asyncio.run(run())


def read_key_row(key_id: str) -> GatewayKey | None:
    async def run() -> GatewayKey | None:
        async with SessionLocal() as session:
            return await session.get(GatewayKey, key_id)

    return asyncio.run(run())


def test_node_must_drain_before_delete(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        # 新节点默认安全态 DRAINING/DISABLED 可直接删除；激活后才必须先下线。
        node = create_node(client, pool["id"])
        assert node["scheduling_state"] == "DRAINING"
        for _ in range(2):
            client.post(
                f"/api/gateway/nodes/{node['id']}/probe",
                json={"probe_types": ALL_PROBES},
            )
        activated = client.post(f"/api/gateway/nodes/{node['id']}/activate")
        assert activated.status_code == 200
        assert activated.json()["scheduling_state"] == "ACTIVE"

        blocked = client.delete(f"/api/gateway/nodes/{node['id']}")
        assert blocked.status_code == 409
        drained = client.post(f"/api/gateway/nodes/{node['id']}/drain")
        assert drained.status_code == 200
        assert drained.json()["scheduling_state"] == "DRAINING"
        deleted = client.delete(f"/api/gateway/nodes/{node['id']}")
        assert deleted.status_code == 204


def test_probe_updates_health_streaks_without_touching_configuration(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])
        assert node["health_state"] == "UNKNOWN"

        first = client.post(
            f"/api/gateway/nodes/{node['id']}/probe", json={}
        )
        assert first.status_code == 200
        assert first.json()["node"]["health_state"] == "UNKNOWN"

        second = client.post(
            f"/api/gateway/nodes/{node['id']}/probe", json={}
        )
        assert second.json()["node"]["health_state"] == "HEALTHY"

        gateway_env.probe.success = False
        for _ in range(3):
            failed = client.post(
                f"/api/gateway/nodes/{node['id']}/probe", json={}
            )
        body = failed.json()["node"]
        assert body["health_state"] == "UNHEALTHY"
        # 健康态变化不修改管理员配置态（新节点默认安全态 DISABLED/DRAINING）。
        assert body["config_state"] == "DISABLED"
        assert body["scheduling_state"] == "DRAINING"


def test_credential_replacements_are_audited_without_secrets(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"], api_key="sk-before")
        replaced = client.patch(
            f"/api/gateway/nodes/{node['id']}", json={"api_key": "sk-after"}
        )
        assert replaced.status_code == 200
        audits = client.get(
            "/api/gateway/audits",
            params={"resource_type": "node", "resource_id": node["id"]},
        ).json()
        update_events = [item for item in audits if item["action"] == "node.update"]
        assert update_events
        text = json.dumps(update_events)
        assert "sk-before" not in text
        assert "sk-after" not in text
        assert "has_api_key" in text


def test_credential_operations_require_master_key(gateway_env):
    with TestClient(app) as client:
        app.dependency_overrides[gateway_cipher] = lambda: CredentialCipher(None)
        pool = create_pool(client)
        rejected = client.post(
            "/api/gateway/nodes",
            json={
                "name": unique("node"),
                "pool_id": pool["id"],
                "base_url": "https://proxy.example.com",
                "models": ["gpt-test"],
                "api_key": "sk-secret",
            },
        )
        assert rejected.status_code == 503
        without_secret = client.post(
            "/api/gateway/nodes",
            json={
                "name": unique("node"),
                "pool_id": pool["id"],
                "base_url": "https://proxy.example.com",
                "models": ["gpt-test"],
            },
        )
        assert without_secret.status_code == 201


def test_probe_network_performs_real_dns_and_tcp():
    async def scenario():
        server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = HttpGatewayProbeClient(2.0)
        reachable = (
            await client.probe(SimpleNamespace(base_url=f"http://127.0.0.1:{port}"), ["NETWORK"], None)
        )[0]
        server.close()
        await server.wait_closed()
        refused = (
            await client.probe(SimpleNamespace(base_url=f"http://127.0.0.1:{port}"), ["NETWORK"], None)
        )[0]
        return reachable, refused

    reachable, refused = asyncio.run(scenario())
    assert reachable.success is True
    assert reachable.detail["port"] > 0
    assert refused.success is False
    try:
        socket.getaddrinfo("astra-no-such-host.invalid", 80)
    except socket.gaierror:
        unresolved = asyncio.run(
            HttpGatewayProbeClient(2.0).probe(
                SimpleNamespace(base_url="http://astra-no-such-host.invalid:80"), ["NETWORK"], None
            )
        )[0]
        assert unresolved.success is False
    else:
        pytest.skip("本机 DNS 将 astra-no-such-host.invalid 解析为地址，跳过无法解析的负向断言")


def test_probe_service_model_and_stream_use_node_configuration():
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured[request.url.path] = request
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content)
            if body.get("stream"):
                sse = 'data: {"choices":[{"delta":{"content":"p"}}]}\n\ndata: [DONE]\n\n'
                return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]})
        return httpx.Response(404)

    node = SimpleNamespace(
        base_url="https://proxy.example.com",
        models=["gpt-test"],
        auth_type="HEADER",
        auth_header="X-Api-Key",
        health_path="/readyz",
        connect_timeout_seconds=None,
        request_timeout_seconds=None,
    )
    client = HttpGatewayProbeClient(
        1.0, default_health_path="/health", transport=httpx.MockTransport(handler)
    )
    outcomes = asyncio.run(client.probe(node, ["SERVICE", "MODEL", "STREAM"], "sk-secret"))
    by_type = {outcome.probe_type: outcome for outcome in outcomes}

    assert captured["/readyz"].headers["X-Api-Key"] == "sk-secret"
    assert "Bearer" not in captured["/readyz"].headers["X-Api-Key"]
    assert captured["/v1/chat/completions"].headers["X-Api-Key"] == "sk-secret"
    assert by_type["SERVICE"].success is True
    assert by_type["MODEL"].success is True
    assert by_type["MODEL"].detail["model"] == "gpt-test"
    assert by_type["STREAM"].success is True
    assert by_type["STREAM"].detail["done"] is True


def test_stream_probe_stops_when_upstream_keeps_sending_keepalive():
    class KeepAlive(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b": keep-alive\n\n"
                await asyncio.sleep(0.01)

        async def aclose(self) -> None:
            return None

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=KeepAlive(),
            headers={"content-type": "text/event-stream"},
        )

    node = SimpleNamespace(
        base_url="https://proxy.example.com",
        models=["gpt-test"],
        auth_type="NONE",
        auth_header="Authorization",
        health_path="/health",
        connect_timeout_seconds=None,
        request_timeout_seconds=None,
    )
    client = HttpGatewayProbeClient(0.2, transport=httpx.MockTransport(handler))

    async def scenario():
        return await asyncio.wait_for(client.probe(node, ["STREAM"], None), timeout=2)

    outcome = asyncio.run(scenario())[0]
    assert outcome.success is False
    assert outcome.error == "探测超时"
    assert outcome.latency_ms < 2000


def test_probe_service_falls_back_to_configured_default_health_path():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    node = SimpleNamespace(
        base_url="https://proxy.example.com",
        models=[],
        auth_type="NONE",
        auth_header="Authorization",
        health_path="",
        connect_timeout_seconds=None,
        request_timeout_seconds=None,
    )
    client = HttpGatewayProbeClient(
        1.0, default_health_path="/internal/health", transport=httpx.MockTransport(handler)
    )
    outcome = asyncio.run(client.probe(node, ["SERVICE"], None))[0]
    assert outcome.success is True
    assert seen == ["/internal/health"]


def test_delete_node_requires_zero_active_concurrency(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])
        drained = client.post(f"/api/gateway/nodes/{node['id']}/drain")
        assert drained.status_code == 200

        # 活跃并发来自与数据面同一事实来源（按 lease 的 ZSET），而非旧计数键。
        lease = asyncio.run(
            gateway_env.runtime.acquire(SCOPE_NODE, node["id"], 0, 900)
        )
        assert lease is not None
        blocked = client.delete(f"/api/gateway/nodes/{node['id']}")
        assert blocked.status_code == 409

        asyncio.run(gateway_env.runtime.release(SCOPE_NODE, node["id"], lease))
        deleted = client.delete(f"/api/gateway/nodes/{node['id']}")
        assert deleted.status_code == 204


def test_node_auth_configuration_validated_and_redacted_in_audit(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"], api_key="sk-node")
        updated = client.patch(
            f"/api/gateway/nodes/{node['id']}",
            json={"auth_type": "HEADER", "auth_header": "X-Api-Key"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["auth_type"] == "HEADER"
        assert updated.json()["auth_header"] == "X-Api-Key"
        # 节点凭据只保留加密字段与 has_api_key，响应不含明文或密文。
        assert "custom_headers" not in updated.json()
        assert "api_key_encrypted" not in json.dumps(updated.json())

        audits = client.get(
            "/api/gateway/audits",
            params={"resource_type": "node", "resource_id": node["id"]},
        ).json()
        assert "sk-node" not in json.dumps(audits)

        unknown_auth = client.patch(
            f"/api/gateway/nodes/{node['id']}",
            json={"auth_type": "TOKEN"},
        )
        assert unknown_auth.status_code == 409

        none_auth = client.patch(
            f"/api/gateway/nodes/{node['id']}",
            json={"auth_type": "NONE"},
        )
        assert none_auth.status_code == 200, none_auth.text
        assert none_auth.json()["auth_type"] == "NONE"


ALL_PROBES = ["NETWORK", "SERVICE", "MODEL", "STREAM"]


def test_node_base_url_must_be_root_without_v1_suffix(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        for bad in ("https://proxy.example.com/v1", "https://proxy.example.com/v1/"):
            rejected = client.post(
                "/api/gateway/nodes",
                json={
                    "name": unique("node"),
                    "pool_id": pool["id"],
                    "base_url": bad,
                    "models": ["gpt-test"],
                },
            )
            assert rejected.status_code == 409, rejected.text

        node = create_node(client, pool["id"])
        patched = client.patch(
            f"/api/gateway/nodes/{node['id']}",
            json={"base_url": "https://proxy.example.com/v1"},
        )
        assert patched.status_code == 409
        refreshed = client.get(f"/api/gateway/nodes/{node['id']}").json()
        assert refreshed["base_url"] == "https://proxy.example.com"


def test_partial_probe_cannot_promote_unknown_health(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])
        for _ in range(3):
            partial = client.post(
                f"/api/gateway/nodes/{node['id']}/probe",
                json={"probe_types": ["NETWORK"]},
            ).json()["node"]
        # 仅局部探测即使连续成功也不能让 UNKNOWN 晋升为 HEALTHY。
        assert partial["health_state"] == "UNKNOWN"
        assert partial["success_streak"] == 0

        # 完整探测连续成功两次才晋升为 HEALTHY。
        for _ in range(2):
            full = client.post(
                f"/api/gateway/nodes/{node['id']}/probe",
                json={"probe_types": ALL_PROBES},
            ).json()["node"]
        assert full["health_state"] == "HEALTHY"


def test_activating_unhealthy_node_is_rejected(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])
        assert node["health_state"] == "UNKNOWN"
        drained = client.post(f"/api/gateway/nodes/{node['id']}/drain")
        assert drained.status_code == 200

        assert (
            client.post(
                f"/api/gateway/nodes/{node['id']}/activate"
            ).status_code
            == 409
        )
        assert (
            client.patch(
                f"/api/gateway/nodes/{node['id']}",
                json={"scheduling_state": "ACTIVE"},
            ).status_code
            == 409
        )
        # 未经探测的新节点不能显式以 ACTIVE 创建。
        explicit = client.post(
            "/api/gateway/nodes",
            json={
                "name": unique("node"),
                "pool_id": pool["id"],
                "base_url": "https://proxy.example.com",
                "models": ["gpt-test"],
                "scheduling_state": "ACTIVE",
            },
        )
        assert explicit.status_code == 409


def test_editing_connection_fields_resets_health_scope(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])

        def become_healthy() -> dict:
            for _ in range(2):
                client.post(
                    f"/api/gateway/nodes/{node['id']}/probe",
                    json={"probe_types": ALL_PROBES},
                )
            return client.get(f"/api/gateway/nodes/{node['id']}").json()

        assert become_healthy()["health_state"] == "HEALTHY"
        assert asyncio.run(gateway_env.runtime.health(read_scope(node["id"]))) == "HEALTHY"

        # 与连接无关的字段变化不重置健康。
        client.patch(
            f"/api/gateway/nodes/{node['id']}", json={"weight": 5}
        )
        assert (
            client.get(f"/api/gateway/nodes/{node['id']}").json()[
                "health_state"
            ]
            == "HEALTHY"
        )

        changes = [
            {"base_url": "https://proxy2.example.com"},
            {"auth_type": "HEADER", "auth_header": "X-Api-Key"},
            {"models": ["gpt-other"]},
            {"health_path": "/healthz"},
            {"api_key": "sk-new"},
        ]
        for payload in changes:
            assert become_healthy()["health_state"] == "HEALTHY", payload
            previous_scope = read_scope(node["id"])
            assert asyncio.run(gateway_env.runtime.health(previous_scope)) == "HEALTHY", payload
            changed = client.patch(
                f"/api/gateway/nodes/{node['id']}", json=payload
            ).json()
            # 健康投影重置；连接指纹变化产生新作用域（必须重新探测）。
            assert changed["health_state"] == "UNKNOWN", payload
            assert changed["success_streak"] == 0, payload
            new_scope = read_scope(node["id"])
            assert new_scope != previous_scope, payload
            assert asyncio.run(gateway_env.runtime.health(new_scope)) is None, payload
            # 保留旧作用域运行态，供刷新前已开始的请求继续使用。
            assert asyncio.run(gateway_env.runtime.health(previous_scope)) == "HEALTHY", payload


def test_new_node_defaults_to_safe_draining_disabled(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])
        # 缺省创建绝不能隐式 ACTIVE/ENABLED：新节点默认安全态且健康 UNKNOWN。
        assert node["scheduling_state"] == "DRAINING"
        assert node["config_state"] == "DISABLED"
        assert node["health_state"] == "UNKNOWN"
        # 显式以 ACTIVE 创建仍被拒绝（尚未通过完整探测）。
        explicit = client.post(
            "/api/gateway/nodes",
            json={
                "name": unique("node"),
                "pool_id": pool["id"],
                "base_url": "https://proxy.example.com",
                "models": ["gpt-test"],
                "scheduling_state": "ACTIVE",
            },
        )
        assert explicit.status_code == 409


def test_production_gateway_requires_master_key_and_no_management_config():
    from pydantic import ValidationError

    from app.config.settings import Settings

    with pytest.raises(ValidationError, match="ASTRA_GATEWAY_MASTER_KEY"):
        Settings(
            _env_file=None,
            environment="production",
            gateway_enabled=True,
            gateway_master_key=None,
        )
    master_key = Fernet.generate_key().decode()
    configured = Settings(
        _env_file=None,
        environment="production",
        gateway_enabled=True,
        gateway_master_key=master_key,
    )
    assert configured.gateway_master_key == master_key
    assert "gateway_control_credentials" not in Settings.model_fields


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/gateway/permissions"),
        ("get", "/api/gateway/config/draft"),
        ("get", "/api/gateway/config/versions"),
        ("get", "/api/gateway/config/versions/1"),
        ("post", "/api/gateway/config/publish"),
        ("post", "/api/gateway/config/rollback"),
        ("post", "/api/gateway/config/reconcile"),
        ("get", "/api/gateway/self/requests"),
        ("get", "/api/gateway/self/stats"),
    ],
)
def test_removed_gateway_management_portals_return_404(gateway_env, method, path):
    with TestClient(app) as client:
        response = client.request(method, path, headers={"Authorization": "Bearer arbitrary-key"})
        assert response.status_code == 404


@pytest.mark.parametrize(
    "invalid",
    [
        {"base_url": "   "},
        {"base_url": "proxy.example.com"},
        {"base_url": "ftp://proxy.example.com"},
        {"base_url": "https://"},
        {"health_path": "ready"},
    ],
)
def test_node_invalid_connection_rejected_when_saved(gateway_env, invalid):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])
        create_payload = {
            "name": unique("invalid-node"),
            "pool_id": pool["id"],
            "base_url": "https://proxy.example.com",
            **invalid,
        }
        assert client.post("/api/gateway/nodes", json=create_payload).status_code == 409
        assert client.patch(f"/api/gateway/nodes/{node['id']}", json=invalid).status_code == 409
        stored = client.get(f"/api/gateway/nodes/{node['id']}").json()
        assert stored["base_url"] == node["base_url"]
        assert stored["health_path"] == node["health_path"]


@pytest.mark.parametrize("invalid", [{"model": "   "}, {"pool_id": ""}, {"pool_id": "gp_missing"}])
def test_route_invalid_reference_rejected_when_saved(gateway_env, invalid):
    with TestClient(app) as client:
        pool = create_pool(client)
        route = create_route(client, pool["id"], unique("model"))
        create_payload = {"model": unique("invalid-model"), "pool_id": pool["id"], **invalid}
        assert client.post("/api/gateway/routes", json=create_payload).status_code == 409
        assert client.patch(f"/api/gateway/routes/{route['id']}", json=invalid).status_code == 409
        stored = next(item for item in client.get("/api/gateway/routes").json() if item["id"] == route["id"])
        assert stored["model"] == route["model"]
        assert stored["pool_id"] == pool["id"]


def test_route_target_model_can_be_saved_and_cleared(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        target_model = unique("upstream-model")
        route = create_route(
            client, pool["id"], unique("transparent-model"), target_model=target_model
        )
        assert route["target_model"] == target_model

        def stored_route() -> dict:
            response = client.get("/api/gateway/routes")
            assert response.status_code == 200, response.text
            return next(item for item in response.json() if item["id"] == route["id"])

        assert stored_route()["target_model"] == target_model

        renamed_target = unique("upstream-model")
        patched = client.patch(
            f"/api/gateway/routes/{route['id']}", json={"target_model": renamed_target}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["target_model"] == renamed_target
        assert stored_route()["target_model"] == renamed_target

        cleared = client.patch(
            f"/api/gateway/routes/{route['id']}", json={"target_model": None}
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["target_model"] is None
        assert stored_route()["target_model"] is None


def test_pool_can_be_disabled_while_referenced_but_cannot_be_deleted(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        route = create_route(client, pool["id"], unique("model"))
        disabled = client.patch(f"/api/gateway/pools/{pool['id']}", json={"enabled": False})
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["enabled"] is False
        assert client.delete(f"/api/gateway/pools/{pool['id']}").status_code == 409
        assert client.delete(f"/api/gateway/routes/{route['id']}").status_code == 204
        assert client.delete(f"/api/gateway/pools/{pool['id']}").status_code == 204


def test_route_duplicate_model_update_rejected_without_overwriting_existing_route(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        original = create_route(client, pool["id"], unique("original-model"))
        another = create_route(client, pool["id"], unique("another-model"))
        rejected = client.patch(f"/api/gateway/routes/{another['id']}", json={"model": original["model"]})
        assert rejected.status_code == 409, rejected.text
        stored = next(
            item for item in client.get("/api/gateway/routes").json() if item["id"] == another["id"]
        )
        assert stored["model"] == another["model"]


def test_node_metadata_save_preserves_health_history(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])
        for _ in range(2):
            assert client.post(f"/api/gateway/nodes/{node['id']}/probe", json={}).status_code == 200

        async def runs():
            async with SessionLocal() as session:
                rows = await session.scalars(
                    select(GatewayProbeRun.id).where(GatewayProbeRun.node_id == node["id"])
                )
                return list(rows)

        async def step_rows():
            async with SessionLocal() as session:
                rows = await session.scalars(
                    select(GatewayHealthCheck.id).where(GatewayHealthCheck.node_id == node["id"])
                )
                return list(rows)

        before = asyncio.run(runs())
        # 两次探测 = 两条轮次；新探测不再逐步骤写 health_checks。
        assert len(before) == 2
        assert asyncio.run(step_rows()) == []
        renamed = client.patch(f"/api/gateway/nodes/{node['id']}", json={"name": unique("renamed-node")})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["health_state"] == "HEALTHY"
        assert asyncio.run(runs()) == before


def test_manual_probe_records_one_run_with_steps(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])

        response = client.post(f"/api/gateway/nodes/{node['id']}/probe", json={})
        assert response.status_code == 200, response.text
        body = response.json()

        run = body["run"]
        assert run["source"] == "MANUAL"
        assert run["node_id"] == node["id"]
        assert run["first_model"] == "gpt-test"
        assert run["runtime_scope"]
        assert run["complete"] is True
        assert run["success"] is True
        assert run["duration_ms"] >= 0
        # 常规验证收敛为一次 SSE：默认策略只跑 STREAM 一步。
        assert run["strategy"] == "SSE"
        assert [step["probe_type"] for step in run["steps"]] == ["STREAM"]
        assert run["usage"]["requests"] == 1
        # 旧的 checks 字段仍是同一次轮次的投影，页面不需要同步改动。
        assert [check["probe_type"] for check in body["checks"]] == ["STREAM"]

        async def counts():
            async with SessionLocal() as session:
                runs = await session.scalar(
                    select(func.count())
                    .select_from(GatewayProbeRun)
                    .where(GatewayProbeRun.node_id == node["id"])
                )
                steps = await session.scalar(
                    select(func.count())
                    .select_from(GatewayHealthCheck)
                    .where(GatewayHealthCheck.node_id == node["id"])
                )
                return runs, steps

        assert asyncio.run(counts()) == (1, 0)


def test_probe_runs_history_separates_legacy_steps(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])
        assert client.post(f"/api/gateway/nodes/{node['id']}/probe", json={}).status_code == 200

        async def add_legacy():
            async with SessionLocal() as session:
                session.add(
                    GatewayHealthCheck(
                        node_id=node["id"],
                        probe_type="MODEL",
                        success=True,
                        latency_ms=12,
                    )
                )
                await session.commit()

        asyncio.run(add_legacy())

        history = client.get(f"/api/gateway/nodes/{node['id']}/probe-runs")
        assert history.status_code == 200, history.text
        body = history.json()
        assert len(body["runs"]) == 1
        assert body["runs"][0]["source"] == "MANUAL"
        assert [step["probe_type"] for step in body["legacy_steps"]] == ["MODEL"]

        # 旧步骤记录不进入任何轮次，也不被新轮次覆盖。
        assert body["runs"][0]["steps"]
        assert all(step.get("legacy") is None for step in body["runs"][0]["steps"])


def test_background_probe_records_an_auto_run(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])

    async def scenario():
        async with SessionLocal() as session:
            db_node = await session.get(GatewayNode, node["id"])
            scope = node_runtime_scope(db_node)
        outcomes = [
            ProbeOutcome(probe_type, True, 4) for probe_type in ("NETWORK", "SERVICE", "MODEL", "STREAM")
        ]
        await record_background_probe(
            SessionLocal, node["id"], outcomes, expected_scope=scope
        )
        async with SessionLocal() as session:
            rows = await session.scalars(
                select(GatewayProbeRun)
                .where(GatewayProbeRun.node_id == node["id"])
                .order_by(GatewayProbeRun.started_at)
            )
            return list(rows)

    runs = asyncio.run(scenario())
    assert len(runs) == 1
    assert runs[0].source == "AUTO"
    assert runs[0].success is True
    assert runs[0].complete is True
    assert [step["probe_type"] for step in runs[0].steps] == ["NETWORK", "SERVICE", "MODEL", "STREAM"]


def test_probe_strategy_non_stream_uses_one_model_step(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"], probe_strategy="NON_STREAM")
        response = client.post(f"/api/gateway/nodes/{node['id']}/probe", json={})
        assert response.status_code == 200, response.text
        run = response.json()["run"]

    assert run["strategy"] == "NON_STREAM"
    assert [step["probe_type"] for step in run["steps"]] == ["MODEL"]
    assert run["complete"] is True


def test_node_health_path_is_backfilled_with_the_effective_default(gateway_env):
    settings = gateway_env_settings()
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])
        # 新建时未提供健康路径：保存的是部署有效值，而不是空串继承。
        assert node["health_path"] == settings.gateway_health_path

        patched = client.patch(
            f"/api/gateway/nodes/{node['id']}", json={"health_path": ""}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["health_path"] == settings.gateway_health_path


def gateway_env_settings():
    from app.config.settings import get_settings

    return get_settings()


def test_changing_verification_inputs_invalidates_previous_health(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])
        for _ in range(2):
            assert client.post(f"/api/gateway/nodes/{node['id']}/probe", json={}).status_code == 200
        healthy = client.get(f"/api/gateway/nodes/{node['id']}").json()
        assert healthy["health_state"] == "HEALTHY"
        # 策略变化：连接指纹不变，但旧授权必须失效（DB 与运行态都要回到 UNKNOWN）。
        switched = client.patch(
            f"/api/gateway/nodes/{node['id']}", json={"probe_strategy": "NON_STREAM"}
        )
        assert switched.status_code == 200, switched.text
        body = switched.json()
        assert body["health_state"] == "UNKNOWN"
        assert body["success_streak"] == 0
        assert body["failure_streak"] == 0

        async def runtime_health() -> str | None:
            async with SessionLocal() as session:
                db_node = await session.get(GatewayNode, node["id"])
                scope = node_runtime_scope(db_node)
            return await gateway_env.runtime.health(scope)

        assert asyncio.run(runtime_health()) is None


def test_duplicate_manual_probe_is_rejected_while_a_round_is_running(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])

        async def hold_lease():
            return await gateway_env.runtime.acquire(SCOPE_PROBE, node["id"], 1, 60)

        lease = asyncio.run(hold_lease())
        try:
            blocked = client.post(f"/api/gateway/nodes/{node['id']}/probe", json={})
            assert blocked.status_code == 409, blocked.text
            assert "探测" in blocked.json()["detail"]
        finally:
            asyncio.run(gateway_env.runtime.release(SCOPE_PROBE, node["id"], lease))

        # 租约释放后可以正常探测，且只新增一条轮次。
        ok = client.post(f"/api/gateway/nodes/{node['id']}/probe", json={})
        assert ok.status_code == 200, ok.text

        async def run_count() -> int:
            async with SessionLocal() as session:
                return int(
                    await session.scalar(
                        select(func.count())
                        .select_from(GatewayProbeRun)
                        .where(GatewayProbeRun.node_id == node["id"])
                    )
                    or 0
                )

        assert asyncio.run(run_count()) == 1
        assert len(gateway_env.probe.calls) == 1


def test_late_probe_round_does_not_overwrite_newer_health(gateway_env):
    from datetime import timedelta

    from app.gateway.models import utcnow as gateway_now

    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])
        for _ in range(2):
            assert client.post(f"/api/gateway/nodes/{node['id']}/probe", json={}).status_code == 200
        assert client.get(f"/api/gateway/nodes/{node['id']}").json()["health_state"] == "HEALTHY"

        async def apply_late_round():
            async with SessionLocal() as session:
                db_node = await session.get(GatewayNode, node["id"])
                # 模拟已投影更新的轮次后再到达一条更旧的失败轮次。
                db_node.last_checked_at = gateway_now() + timedelta(seconds=60)
                await session.commit()
                scope = node_runtime_scope(db_node)
            await record_background_probe(
                SessionLocal,
                node["id"],
                [ProbeOutcome("STREAM", False, 1)],
                expected_scope=scope,
            )
            async with SessionLocal() as session:
                return (await session.get(GatewayNode, node["id"])).health_state

        assert asyncio.run(apply_late_round()) == "HEALTHY"


def test_failed_projection_is_marked_and_recovered_without_losing_the_run(gateway_env):
    with TestClient(app) as client:
        pool = create_pool(client)
        node = create_node(client, pool["id"])
        runtime = gateway_env.runtime
        original = runtime.record_probe
        state = {"fail": True}

        async def flaky(*args, **kwargs):
            if state["fail"]:
                state["fail"] = False
                raise RuntimeError("redis down")
            return await original(*args, **kwargs)

        runtime.record_probe = flaky

        # 投影失败不影响轮次与 DB 健康；标记待补投影。
        first = client.post(f"/api/gateway/nodes/{node['id']}/probe", json={})
        assert first.status_code == 200, first.text
        detail = client.get(f"/api/gateway/nodes/{node['id']}").json()["health_detail"]
        assert detail.get("projection_pending") is True

        # 下一次操作先补投影（不重复上一轮探测），成功后清除标记。
        second = client.post(f"/api/gateway/nodes/{node['id']}/probe", json={})
        assert second.status_code == 200, second.text
        detail = client.get(f"/api/gateway/nodes/{node['id']}").json()["health_detail"]
        assert not detail.get("projection_pending")
        assert len(gateway_env.probe.calls) == 2

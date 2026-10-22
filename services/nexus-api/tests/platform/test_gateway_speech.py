"""ASR 必须经过共享网关准入与已持久化配置，不允许直连或浏览器覆盖模型。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.speech_routes import router
from app.database import SessionLocal
from app.gateway.audio import AUDIO_PATH, MAX_AUDIO_BODY_BYTES, parse_audio_upload, probe_audio
from app.gateway.models import GatewayAuditEvent, GatewaySpeechInput
from app.gateway.probe import HttpGatewayProbeClient, probe_types_for_strategy
from app.gateway.runtime import SCOPE_KEY, SCOPE_NODE
from app.gateway.security import CredentialCipher
from app.gateway.server import create_gateway_app
from tests.platform.test_gateway_data_plane import (
    DOWNSTREAM_KEY,
    MODEL,
    NODE_A,
    auth_headers,
    build_config,
    build_plane,
    key_dict,
    node_dict,
)


class AudioUpstream:
    def __init__(self, status=200):
        self.requests = []
        self.status = status

    def handler(self, request):
        self.requests.append(request)
        assert request.url.path == AUDIO_PATH
        audio = parse_audio_upload(request.content, request.headers["content-type"])
        assert audio.fields["model"] == "sensevoice-small-int8"
        assert audio.audio == probe_audio()
        assert request.headers["authorization"] == f"Bearer {DOWNSTREAM_KEY}"
        return httpx.Response(self.status, json={"text": "测试语音识别"})


def plane(upstream, *, key=None, kind="ASR"):
    cipher = CredentialCipher(Fernet.generate_key().decode())
    config = build_config(
        cipher,
        key=key,
        nodes=[
            node_dict(NODE_A, host="asr.test", cipher=cipher, models=("sensevoice-small-int8",)),
        ],
    )
    config.routes[MODEL] = replace(
        config.routes[MODEL], target_model="sensevoice-small-int8", model_type=kind
    )
    return build_plane(upstream, config=config, cipher=cipher)


def post_audio(client, **kwargs):
    return client.post(
        AUDIO_PATH,
        headers=kwargs.pop("headers", auth_headers()),
        data=kwargs.pop("data", {"model": MODEL}),
        files={"file": ("record.wav", probe_audio(), "audio/wav")},
        **kwargs,
    )


@pytest.mark.parametrize("status", [200, 429, 503])
def test_asr_alias_privacy_and_release(status):
    upstream = AudioUpstream(status)
    kv = plane(upstream)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = post_audio(client)
        assert response.status_code == status, response.text
        if status == 200:
            assert response.json()["text"] == "测试语音识别"
    assert len(upstream.requests) == 1
    assert kv.plane._counters["active"] == 0

    async def check():
        assert await kv.store.acquire(SCOPE_KEY, "gk_test", 1, 30)
        assert await kv.store.acquire(SCOPE_NODE, NODE_A, 1, 30)

    asyncio.run(check())


@pytest.mark.parametrize("status", [400, 413, 415, 422])
def test_asr_client_audio_errors_do_not_penalize_node(status):
    upstream = AudioUpstream(status)
    kv = plane(upstream)
    with TestClient(create_gateway_app(kv.plane)) as client:
        for _ in range(4):
            assert post_audio(client).status_code == status
    assert len(upstream.requests) == 4
    scope = kv.state.config.nodes[NODE_A].runtime_scope
    assert asyncio.run(kv.store.circuit(scope)) == "CLOSED"
    assert asyncio.run(kv.store.health(scope)) == "HEALTHY"


@pytest.mark.parametrize(
    "case,status",
    [
        ("auth", 401),
        ("scope", 403),
        ("type", 400),
        ("stream", 400),
        ("format", 400),
        ("missing-model", 400),
        ("duplicate", 400),
        ("too-large", 413),
        ("malformed", 400),
    ],
)
def test_asr_rejections_never_reach_upstream(case, status):
    upstream = AudioUpstream()
    kv = plane(
        upstream,
        key=key_dict(allowed_models=["other"]) if case == "scope" else None,
        kind="CHAT" if case == "type" else "ASR",
    )
    with TestClient(create_gateway_app(kv.plane)) as client:
        if case == "duplicate":
            result = client.post(
                AUDIO_PATH,
                headers=auth_headers(),
                files=[
                    ("model", (None, MODEL)),
                    ("model", (None, MODEL)),
                    ("file", ("x.wav", probe_audio(), "audio/wav")),
                ],
            )
        elif case in {"too-large", "malformed"}:
            headers = auth_headers()
            if case == "too-large":
                headers["Content-Length"] = str(MAX_AUDIO_BODY_BYTES + 1)
            result = client.post(AUDIO_PATH, headers=headers, content=b"malformed")
        else:
            data = {"model": MODEL}
            if case == "stream":
                data["stream"] = "true"
            if case == "format":
                data["response_format"] = "text"
            if case == "missing-model":
                data.clear()
            result = post_audio(client, headers={} if case == "auth" else auth_headers(), data=data)
    assert result.status_code == status, result.text
    assert not upstream.requests


async def test_asr_probe_is_real_audio_protocol():
    seen = []

    def handler(request):
        seen.append(request)
        audio = parse_audio_upload(request.content, request.headers["content-type"])
        assert audio.fields["model"] == "sensevoice-small-int8"
        assert audio.audio == probe_audio()
        return httpx.Response(200, json={"text": ""})

    probe = HttpGatewayProbeClient(transport=httpx.MockTransport(handler))
    result = await probe.probe(
        SimpleNamespace(base_url="http://asr.test", models=["sensevoice-small-int8"]), ["ASR"], None
    )
    assert result[0].success
    assert [request.url.path for request in seen] == [AUDIO_PATH]
    assert probe_types_for_strategy("ASR") == ["ASR"]


def test_persisted_speech_config_and_gateway_bridge(monkeypatch):
    from fastapi import HTTPException

    from app.api import speech_routes as speech_routes_module

    def fake_session_user(request):
        uid = request.headers.get("X-Verified-UID")
        if not uid:
            raise HTTPException(401, "请先登录")
        return {"uid": uid}

    monkeypatch.setattr(speech_routes_module, "session_user", fake_session_user)
    app = FastAPI()
    app.include_router(router)
    from app.api.gateway_routes import router as gateway_router

    app.include_router(gateway_router)
    seen = []
    original_client = httpx.AsyncClient

    def gateway(request):
        seen.append(request)
        assert request.url == f"http://gateway.test{AUDIO_PATH}"
        assert request.headers["authorization"] == "Bearer server-only-test-key"
        audio = parse_audio_upload(request.content, request.headers["content-type"])
        assert audio.fields == {"model": "voice-test"}
        return httpx.Response(200, json={"text": "网关识别文字"})

    monkeypatch.setenv("NEXUS_GATEWAY_OPENAI_BASE", "http://gateway.test/v1")
    monkeypatch.setenv("NEXUS_GATEWAY_API_KEY", "server-only-test-key")
    monkeypatch.setattr(
        "app.api.speech_routes.httpx.AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(gateway), **kw),
    )
    headers = {"X-Verified-UID": "speech-qa"}
    with TestClient(app) as client:
        # 没有身份时不能上传；配置缺失时明确失败，不连接本机。
        assert client.post("/api/asr/transcribe").status_code == 401
        assert client.put("/api/gateway/speech-input", json={"route_id": None}).status_code == 401
        assert (
            client.put("/api/gateway/speech-input", headers=headers, json={"route_id": None}).status_code
            == 200
        )
        assert client.post("/api/asr/transcribe", headers=headers).status_code == 422
        pool = client.post("/api/gateway/pools", json={"name": f"speech-{uuid4().hex}"}).json()
        route = client.post(
            "/api/gateway/routes",
            json={
                "model": "voice-test",
                "model_type": "ASR",
                "pool_id": pool["id"],
            },
        ).json()
        assert (
            client.put(
                "/api/gateway/speech-input", headers=headers, json={"route_id": route["id"]}
            ).status_code
            == 200
        )
    # 新建应用实例/会话仍从数据库取配置。
    fresh = FastAPI()
    fresh.include_router(router)
    with TestClient(fresh) as client:
        assert client.get("/api/gateway/speech-input").json() == {"route_id": route["id"]}
        response = client.post(
            "/api/asr/transcribe", headers=headers, files={"file": ("x.wav", probe_audio(), "audio/wav")}
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"text": "网关识别文字"}
        assert "server-only-test-key" not in response.text
        assert (
            client.post(
                "/api/asr/transcribe",
                headers=headers,
                data={"model": "bypass"},
                files={"file": ("x.wav", probe_audio(), "audio/wav")},
            ).status_code
            == 400
        )
    assert len(seen) == 1
    with TestClient(app) as client:
        client.patch(f"/api/gateway/routes/{route['id']}", json={"enabled": False})
        assert client.post("/api/asr/transcribe", headers=headers).status_code == 422
        assert (
            client.put(
                "/api/gateway/speech-input", headers=headers, json={"route_id": route["id"]}
            ).status_code
            == 422
        )
        assert (
            client.put("/api/gateway/speech-input", headers=headers, json={"route_id": None}).status_code
            == 200
        )

    async def check_audit():
        async with SessionLocal() as session:
            rows = list(
                await session.scalars(
                    select(GatewayAuditEvent).where(GatewayAuditEvent.action == "speech-input.update")
                )
            )
            assert rows and rows[-1].actor == "speech-qa"
            assert (await session.get(GatewaySpeechInput, "default")).route_id is None

    asyncio.run(check_audit())


def test_asr_invalid_success_response_is_protocol_failure():
    class InvalidUpstream:
        def handler(self, request):
            return httpx.Response(200, json={"choices": []})

    kv = plane(InvalidUpstream())
    with TestClient(create_gateway_app(kv.plane)) as client:
        result = post_audio(client)
        assert result.status_code == 502
        assert result.json()["error"]["code"] == "UPSTREAM_PROTOCOL_ERROR"
    assert kv.plane._counters["active"] == 0


async def test_chat_and_embedding_route_types_cannot_be_used_as_speech():
    from nexus_api.model_route import ModelRouteError, require_enabled_route

    async def loader(_url):
        return [{"id": "speech", "model": "voice", "enabled": True, "model_type": "ASR"}]

    with pytest.raises(ModelRouteError):
        await require_enabled_route("speech", loader=loader)
    route = await require_enabled_route("speech", loader=loader, model_type="ASR")
    assert route["model"] == "voice"


async def test_worker_main_model_cannot_resolve_asr_route():
    from app.gateway.models import GatewayPool, GatewayRoute
    from app.runtime.worker_health import _desired_model

    suffix = uuid4().hex
    async with SessionLocal() as session:
        pool = GatewayPool(id=f"pool-{suffix}", name=f"model-type-{suffix}")
        session.add(pool)
        await session.flush()
        model = f"000-asr-{suffix}"
        session.add(GatewayRoute(id=f"asr-{suffix}", model=model, model_type="ASR", pool_id=pool.id))
        await session.commit()
        desired = await _desired_model(session, SimpleNamespace(model_name=model))
        assert desired["resolved"] is False
        assert "未匹配" in desired["error"]
        automatic = await _desired_model(session, SimpleNamespace(model_name=""))
        assert automatic["route"] != model

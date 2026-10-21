"""Embeddings must use the shared admission, alias routing, accounting and leases."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

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


class EmbeddingUpstream:
    def __init__(self, *, status=200):
        self.requests = []
        self.status = status

    def handler(self, request):
        self.requests.append(request)
        assert request.url.path == "/v1/embeddings"
        return httpx.Response(
            self.status,
            json={
                "object": "list",
                "model": "vendor-embed",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, -0.3]}],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            },
        )


def plane(upstream, *, key=None):
    cipher = CredentialCipher(Fernet.generate_key().decode())
    config = build_config(
        cipher,
        key=key,
        nodes=[
            node_dict(NODE_A, host="embed.test", cipher=cipher, models=("vendor-embed",)),
        ],
    )
    config.routes[MODEL] = replace(config.routes[MODEL], target_model="vendor-embed")
    return build_plane(upstream, config=config, cipher=cipher)


def test_embeddings_preserve_vectors_alias_and_usage_and_release_leases():
    upstream = EmbeddingUpstream()
    kv = plane(upstream)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            "/v1/embeddings",
            headers=auth_headers(),
            json={
                "model": MODEL,
                "input": ["中文内容"],
                "dimensions": 2,
                "encoding_format": "float",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["model"] == MODEL
        assert response.json()["data"][0]["embedding"] == [0.1, -0.3]
        assert response.json()["usage"]["total_tokens"] == 3
    request = upstream.requests[0]
    assert request.headers["authorization"] == f"Bearer {DOWNSTREAM_KEY}"
    assert json.loads(request.content)["model"] == "vendor-embed"
    assert json.loads(request.content)["input"] == ["中文内容"]
    assert kv.plane._counters["active"] == 0

    async def no_leaked_lease():
        assert await kv.store.acquire(SCOPE_KEY, "gk_test", 1, 30)
        assert await kv.store.acquire(SCOPE_NODE, NODE_A, 1, 30)

    asyncio.run(no_leaked_lease())


@pytest.mark.parametrize("case,status", [("auth", 401), ("scope", 403), ("stream", 400), ("input", 400)])
def test_rejected_embedding_calls_never_reach_upstream(case, status):
    upstream = EmbeddingUpstream()
    kv = plane(upstream, key=key_dict(allowed_models=["other"]) if case == "scope" else None)
    payload = {"model": MODEL, "input": "text"}
    if case == "stream":
        payload["stream"] = True
    if case == "input":
        del payload["input"]
    with TestClient(create_gateway_app(kv.plane)) as client:
        result = client.post("/v1/embeddings", headers={} if case == "auth" else auth_headers(), json=payload)
    assert result.status_code == status
    assert not upstream.requests


def test_embedding_quota_is_shared_with_chat_admission():
    upstream = EmbeddingUpstream()
    kv = plane(upstream, key=key_dict(hourly_token_limit=1))
    with TestClient(create_gateway_app(kv.plane)) as client:
        body = {"model": MODEL, "input": "text"}
        assert client.post("/v1/embeddings", headers=auth_headers(), json=body).status_code == 200
        result = client.post("/v1/embeddings", headers=auth_headers(), json=body)
        assert result.status_code == 429
    assert len(upstream.requests) == 1


def test_embedding_upstream_failure_is_not_retried_and_releases_capacity():
    upstream = EmbeddingUpstream(status=503)
    kv = plane(upstream)
    with TestClient(create_gateway_app(kv.plane)) as client:
        assert (
            client.post(
                "/v1/embeddings", headers=auth_headers(), json={"model": MODEL, "input": "x"}
            ).status_code
            == 503
        )
    assert len(upstream.requests) == 1
    assert kv.plane._counters["active"] == 0


async def test_embedding_only_probe_uses_embedding_endpoint():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})

    probe = HttpGatewayProbeClient(transport=httpx.MockTransport(handler))
    result = await probe.probe(
        SimpleNamespace(base_url="http://embed.test", models=["embed"]), ["EMBEDDING"], "test-key"
    )
    assert result[0].success
    assert seen == ["/v1/embeddings"]
    assert probe_types_for_strategy("EMBEDDING") == ["EMBEDDING"]

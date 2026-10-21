"""API 中转观测测试：后台探测落库、调用日志分页过滤、范围统计、实例观测。

统计来源固定为 ``gateway_requests``；未知 usage 明确标记，不保存 Prompt。管理台直接
查询日志与统计；不保存 Prompt 或凭据。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
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
from app.database import SessionLocal, init_database
from app.gateway.config import GatewayConfig, GatewayConfigState
from app.gateway.models import (
    GatewayHealthCheck,
    GatewayNode,
    GatewayProbeRun,
    GatewayRequest,
    utcnow,
)
from app.gateway.probe import ProbeOutcome
from app.gateway.proxy import GatewayProxy
from app.gateway.runtime import SCOPE_NODE, InMemoryRuntimeStore
from app.gateway.security import CredentialCipher
from app.gateway.server import GatewayDataPlane
from app.main import app


class FakeProbe:
    def __init__(self) -> None:
        self.success = True
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def probe(self, node, probe_types, api_key):
        self.calls.append((node.id, tuple(probe_types)))
        return [ProbeOutcome(probe_type, self.success, 2) for probe_type in probe_types]


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


@pytest.fixture(scope="module", autouse=True)
def _schema():
    asyncio.run(init_database())
    yield


@pytest.fixture
def observability_env():
    master_key = Fernet.generate_key().decode()
    runtime = InMemoryRuntimeStore()
    overrides = {
        gateway_cipher: lambda: CredentialCipher(master_key),
        gateway_probe_client: lambda: FakeProbe(),
        gateway_runtime: lambda: runtime,
    }
    app.dependency_overrides.update(overrides)
    try:
        yield SimpleNamespace(runtime=runtime)
    finally:
        for dependency in overrides:
            app.dependency_overrides.pop(dependency, None)


def _add_log(
    *,
    key_id: str | None = None,
    model: str | None = None,
    actual_model: str | None = None,
    node_id: str | None = None,
    pool_id: str | None = None,
    status_code: int | None = 200,
    error_code: str | None = None,
    input_tokens: int | None = 10,
    output_tokens: int | None = 5,
    total_tokens: int | None = 15,
    output_tps: float | None = 25.5,
    duration_ms: int | None = 100,
    connect_ms: int | None = 8,
    ttfb_ms: int | None = 20,
    ttft_ms: int | None = 30,
    started_at=None,
    finished_at=None,
    streaming: bool = False,
    unfinished: bool = False,
    outcome: str | None = None,
    gateway_instance_id: str | None = None,
    gateway_instance_started_at=None,
) -> str:
    log_id = uuid4().hex
    start = started_at or utcnow()
    row = GatewayRequest(
        id=log_id,
        key_id=key_id,
        model=model,
        actual_model=actual_model,
        node_id=node_id,
        pool_id=pool_id,
        status_code=status_code,
        error_code=error_code,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        output_tps=output_tps,
        connect_ms=connect_ms,
        ttfb_ms=ttfb_ms,
        ttft_ms=ttft_ms,
        duration_ms=duration_ms,
        streaming=streaming,
        started_at=start,
        finished_at=None if unfinished else (finished_at or (start + timedelta(seconds=2))),
        outcome=outcome,
        gateway_instance_id=gateway_instance_id,
        gateway_instance_started_at=gateway_instance_started_at,
    )

    async def write():
        async with SessionLocal() as session:
            session.add(row)
            await session.commit()

    asyncio.run(write())
    return log_id


# ── 后台探测落库与健康一致性 ───────────────────────────────────────────


def _build_probe_plane(node_id: str, probe: FakeProbe) -> tuple[GatewayDataPlane, InMemoryRuntimeStore]:
    snapshot = {
        "pools": [],
        "nodes": [
            {
                "id": node_id,
                "name": node_id,
                "pool_id": None,
                "base_url": "https://proxy.example.com",
                "auth_type": "NONE",
                "models": ["gpt-test"],
                "config_state": "ENABLED",
                "scheduling_state": "ACTIVE",
                "health_state": "HEALTHY",
            }
        ],
        "routes": [],
        "keys": [],
    }
    runtime = InMemoryRuntimeStore()
    plane = GatewayDataPlane(
        config_state=GatewayConfigState(GatewayConfig.from_snapshot(snapshot)),
        runtime=runtime,
        proxy=GatewayProxy(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        cipher=CredentialCipher(None),
        session_factory=SessionLocal,
        config_loader=None,
        instance_id="obs-plane",
        heartbeat_interval=3600,
        probe_client=probe,
        config_poll_interval=3600,
    )
    return plane, runtime


def _insert_node(node_id: str) -> None:
    async def write():
        async with SessionLocal() as session:
            session.add(
                GatewayNode(
                    id=node_id,
                    name=node_id,
                    pool_id=None,
                    base_url="https://proxy.example.com",
                    auth_type="NONE",
                    models=["gpt-test"],
                )
            )
            await session.commit()

    asyncio.run(write())


def _health_check_count(node_id: str) -> int:
    async def read():
        async with SessionLocal() as session:
            rows = await session.scalars(
                select(GatewayHealthCheck).where(GatewayHealthCheck.node_id == node_id)
            )
            return len(list(rows))

    return asyncio.run(read())


def _probe_run_count(node_id: str) -> int:
    async def count() -> int:
        async with SessionLocal() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(GatewayProbeRun)
                    .where(GatewayProbeRun.node_id == node_id)
                )
                or 0
            )

    return asyncio.run(count())


def test_background_probe_persists_checks_and_matches_runtime_health():
    node_id = unique("gn_obs")
    _insert_node(node_id)
    probe = FakeProbe()
    plane, runtime = _build_probe_plane(node_id, probe)
    node = plane.config_state.config.nodes[node_id]
    scope = node.runtime_scope

    async def db_health() -> str:
        async with SessionLocal() as session:
            row = await session.get(GatewayNode, node_id)
            return row.health_state

    async def run():
        await plane.startup()
        try:
            probe.success = False
            for _ in range(3):
                await plane._probe_node(node)
            removed_runtime = await runtime.health(scope)
            removed_db = await db_health()
            probe.success = True
            for _ in range(2):
                await plane._probe_node(node)
            return removed_runtime, removed_db, await runtime.health(scope), await db_health()
        finally:
            await plane.shutdown()

    removed_runtime, removed_db, restored_runtime, restored_db = asyncio.run(run())
    assert removed_runtime == removed_db == "UNHEALTHY"
    assert restored_runtime == restored_db == "HEALTHY"
    # 每次探测落一条轮次（步骤在轮次内）；启动后台探测循环也会先探一次。
    runs = _probe_run_count(node_id)
    assert runs >= 5
    assert _health_check_count(node_id) == 0


# ── 调用日志分页 / 过滤 ────────────────────────────────────────────────


def test_request_log_pagination_and_filters(observability_env):
    model = unique("model")
    node = unique("node")
    log_ids = [
        _add_log(model=model, node_id=node, status_code=200),
        _add_log(model=model, node_id=node, status_code=500, error_code="UPSTREAM_500"),
        _add_log(model=model, node_id=node, status_code=429, error_code="KEY_CONCURRENCY_EXCEEDED"),
    ]
    with TestClient(app) as client:
        first_page = client.get(
            "/api/gateway/requests",
            params={"model": model, "page": 1, "page_size": 2},
        )
        assert first_page.status_code == 200, first_page.text
        body = first_page.json()
        assert body["total"] == 3
        assert body["page"] == 1 and body["page_size"] == 2
        assert len(body["items"]) == 2

        second_page = client.get(
            "/api/gateway/requests",
            params={"model": model, "page": 2, "page_size": 2},
        ).json()
        assert len(second_page["items"]) == 1
        assert {item["id"] for item in body["items"]} | {
            item["id"] for item in second_page["items"]
        } == set(log_ids)

        by_status = client.get(
            "/api/gateway/requests",
            params={"model": model, "status_code": 500},
        ).json()
        assert by_status["total"] == 1

        by_error = client.get(
            "/api/gateway/requests",
            params={"model": model, "error_code": "UPSTREAM_500"},
        ).json()
        assert by_error["total"] == 1

        by_id = client.get(
            "/api/gateway/requests",
            params={"request_id": log_ids[0]},
        ).json()
        assert by_id["total"] == 1
        assert by_id["items"][0]["id"] == log_ids[0]

        by_node = client.get(
            "/api/gateway/requests",
            params={"model": model, "node_id": node},
        ).json()
        assert by_node["total"] == 3

        # 日志只含元数据，绝不包含 Prompt / 授权头。
        assert "prompt" not in json.dumps(body).lower()
        assert "authorization" not in json.dumps(body).lower()
        assert not hasattr(GatewayRequest, "prompt")


# ── 范围统计与未知 usage ───────────────────────────────────────────────


def test_scope_stats_tokens_latency_peak_and_unknown_usage(observability_env):
    model = unique("model")
    node = unique("node")
    start = utcnow() - timedelta(minutes=30)
    _add_log(
        model=model,
        node_id=node,
        status_code=200,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        started_at=start,
        finished_at=start + timedelta(seconds=5),
    )
    _add_log(
        model=model,
        node_id=node,
        status_code=500,
        error_code="UPSTREAM_500",
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        output_tps=None,
        started_at=start + timedelta(seconds=1),
        finished_at=start + timedelta(seconds=3),
    )
    _add_log(
        model=model,
        node_id=node,
        status_code=200,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        started_at=start + timedelta(seconds=10),
        finished_at=start + timedelta(seconds=20),
    )

    # 运行态当前并发直接从 Redis 运行态读取。
    lease = asyncio.run(observability_env.runtime.acquire(SCOPE_NODE, node, 0, 900))
    assert lease is not None

    with TestClient(app) as client:
        response = client.get(
            "/api/gateway/stats",
            params={"scope": "node", "scope_id": node, "window": "7d"},
        )
        assert response.status_code == 200, response.text
        stats = response.json()

    assert stats["requests"] == 3
    assert stats["success"] == 2
    assert stats["errors"] == 1
    assert stats["input_tokens"] == 10
    assert stats["output_tokens"] == 5
    assert stats["total_tokens"] == 15
    # 未知 usage 只计数，不估算为零。
    assert stats["unknown_usage_requests"] == 1
    assert stats["success_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert stats["qps"] >= 0
    assert stats["active"] == 1
    # 两个请求区间重叠 → 峰值并发至少 2。
    assert stats["peak_concurrency"] >= 2
    assert stats["connect_ms"]["avg"] is not None
    assert stats["ttfb_ms"]["p50"] is not None
    assert stats["ttft_ms"]["p95"] is not None
    assert stats["duration_ms"]["avg"] is not None
    assert len(stats["trend"]) >= 1
    assert sum(bucket["requests"] for bucket in stats["trend"]) == 3
    assert sum(bucket["unknown_usage_requests"] for bucket in stats["trend"]) == 1
    assert stats["in_progress"] == 0
    assert stats["lost"] == 0
    assert stats["legacy_open"] == 0


def test_final_result_stats_exclude_open_lost_and_legacy_rows(observability_env):
    model = unique("model")
    node = unique("node")
    start = utcnow() - timedelta(minutes=2)
    _add_log(
        model=model,
        node_id=node,
        status_code=200,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        started_at=start,
        finished_at=start + timedelta(seconds=1),
    )
    _add_log(
        model=model,
        node_id=node,
        status_code=500,
        error_code="UPSTREAM_500",
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        started_at=start + timedelta(seconds=30),
        finished_at=start + timedelta(seconds=31),
    )
    _add_log(
        model=model,
        node_id=node,
        status_code=None,
        error_code=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        output_tps=None,
        connect_ms=None,
        ttfb_ms=None,
        ttft_ms=None,
        duration_ms=None,
        started_at=start,
        unfinished=True,
        gateway_instance_id="gw-live",
        gateway_instance_started_at=start,
    )
    _add_log(
        model=model,
        node_id=node,
        status_code=None,
        error_code=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        output_tps=None,
        started_at=start,
        finished_at=start + timedelta(seconds=1),
        outcome="lost",
    )
    _add_log(
        model=model,
        node_id=node,
        status_code=None,
        error_code=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        output_tps=None,
        started_at=start,
        unfinished=True,
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/gateway/stats",
            params={"scope": "node", "scope_id": node, "window": "7d"},
        )
        assert response.status_code == 200, response.text
        stats = response.json()

    assert stats["requests"] == 5
    assert stats["success"] == 1
    assert stats["errors"] == 1
    assert stats["success_rate"] == pytest.approx(0.5)
    assert stats["in_progress"] == 1
    assert stats["lost"] == 1
    assert stats["legacy_open"] == 1
    assert stats["input_tokens"] == 10
    assert stats["unknown_usage_requests"] == 1
    assert stats["peak_concurrency"] == 1
    assert sum(bucket["requests"] for bucket in stats["trend"]) == 5
    assert sum(bucket["errors"] for bucket in stats["trend"]) == 1
    assert sum(bucket["success"] for bucket in stats["trend"]) == 1


def test_stats_overview_and_missing_scope_validation(observability_env):
    with TestClient(app) as client:
        overview = client.get("/api/gateway/stats")
        assert overview.status_code == 200
        assert overview.json()["scope"] == "overview"
        assert overview.json()["scope_id"] is None

        missing = client.get(
            "/api/gateway/stats",
            params={"scope": "node"},
        )
        assert missing.status_code == 422

        invalid_window = client.get(
            "/api/gateway/stats",
            params={"scope": "overview", "window": "year"},
        )
        assert invalid_window.status_code == 422


# ── 实例心跳与过期 ─────────────────────────────────────────────────────


def test_instances_mark_expired_and_expose_optional_metrics(observability_env):
    live_id = unique("inst-live")
    stale_id = unique("inst-stale")
    asyncio.run(
        observability_env.runtime.heartbeat(
            live_id,
            {
                "instance_id": live_id,
                "app_version": "0.4.0",
                "pid": 4242,
                "updated_at": utcnow().isoformat(),
                "requests": 11,
                "errors": 1,
                "active": 2,
                "cpu_percent": 12.5,
                "memory_bytes": 1048576,
            },
            60,
        )
    )
    asyncio.run(
        observability_env.runtime.heartbeat(
            stale_id,
            {"instance_id": stale_id, "updated_at": utcnow().isoformat()},
            1,
        )
    )
    time.sleep(1.1)

    with TestClient(app) as client:
        response = client.get("/api/gateway/instances")
        assert response.status_code == 200, response.text
        by_id = {item["instance_id"]: item for item in response.json()}

    assert by_id[live_id]["status"] == "ACTIVE"
    assert by_id[live_id]["cpu_percent"] == 12.5
    assert by_id[live_id]["memory_bytes"] == 1048576
    assert by_id[stale_id]["status"] == "EXPIRED"
    assert by_id[stale_id]["age_seconds"] >= 1


def test_instances_available_without_management_credentials(observability_env):
    with TestClient(app) as client:
        assert client.get("/api/gateway/instances").status_code == 200

"""API 中转控制面 REST 路由。

只提供管理接口，不承载模型数据面转发。配置保存后由数据面自动刷新；
所有写操作记录 Astra 来源审计。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database import get_session
from app.gateway.health_summary import GatewayOpsService, TrendWindow
from app.gateway.models import SCHEDULING_ACTIVE, SCHEDULING_DRAINING
from app.gateway.observability import GatewayObservabilityService
from app.gateway.probe import GatewayProbeClient, HttpGatewayProbeClient
from app.gateway.runtime import RedisRuntimeStore, RuntimeStore
from app.gateway.schemas import (
    AuditEventRead,
    GatewayInstanceRead,
    GatewayKeyCreate,
    GatewayKeyCreated,
    GatewayKeyPatch,
    GatewayKeyRead,
    HealthCheckRead,
    HealthSummary,
    NodeCreate,
    NodePatch,
    NodeProbeResult,
    NodeRead,
    NodeWindowMetric,
    OpsEventRead,
    PoolCreate,
    PoolPatch,
    PoolRead,
    ProbeRequest,
    ProbeRunList,
    ProbeRunRead,
    RequestLogPage,
    RouteCreate,
    RoutePatch,
    RouteRead,
    ScopeStats,
    TrendRead,
)
from app.gateway.security import (
    CredentialCipher,
    CredentialError,
    CredentialUnavailable,
)
from app.gateway.service import (
    GatewayConflict,
    GatewayControlService,
    GatewayNotFound,
    GatewayRuntimeUnavailable,
    probe_checks_from_run,
)

router = APIRouter(prefix="/api/gateway", tags=["api-gateway-control"])


def gateway_cipher() -> CredentialCipher:
    return CredentialCipher(get_settings().gateway_master_key)


def gateway_probe_client() -> GatewayProbeClient:
    settings = get_settings()
    return HttpGatewayProbeClient(
        settings.gateway_probe_timeout_seconds,
        default_health_path=settings.gateway_health_path,
        timezone=settings.timezone,
    )


async def gateway_runtime() -> AsyncIterator[RuntimeStore]:
    """控制面读取节点活跃并发与健康时，必须复用数据面的运行态事实来源。"""

    runtime = RedisRuntimeStore(get_settings().redis_url)
    try:
        yield runtime
    finally:
        await runtime.close()


def gateway_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    cipher: Annotated[CredentialCipher, Depends(gateway_cipher)],
    probe_client: Annotated[GatewayProbeClient, Depends(gateway_probe_client)],
    runtime: Annotated[RuntimeStore, Depends(gateway_runtime)],
) -> GatewayControlService:
    settings = get_settings()
    return GatewayControlService(
        session,
        cipher=cipher,
        probe_client=probe_client,
        runtime=runtime,
        default_health_path=settings.gateway_health_path,
        timezone=settings.timezone,
    )


def gateway_ops(
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[RuntimeStore, Depends(gateway_runtime)],
) -> GatewayOpsService:
    settings = get_settings()
    return GatewayOpsService(
        session,
        runtime=runtime,
        timezone=settings.timezone,
        probe_interval_seconds=settings.gateway_probe_interval_seconds,
    )


def gateway_observability(
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[RuntimeStore, Depends(gateway_runtime)],
) -> GatewayObservabilityService:
    return GatewayObservabilityService(
        session, runtime=runtime, timezone=get_settings().timezone
    )


async def gateway_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, GatewayNotFound):
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})
    if isinstance(exc, GatewayConflict):
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})
    if isinstance(exc, (GatewayRuntimeUnavailable, CredentialUnavailable, CredentialError)):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": str(exc)}
        )
    raise exc


# ── Pool ──────────────────────────────────────────────────────────────


@router.get("/pools", response_model=list[PoolRead])
async def list_pools(
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.list_pools()


@router.post("/pools", response_model=PoolRead, status_code=status.HTTP_201_CREATED)
async def create_pool(
    payload: PoolCreate,
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.create_pool(payload, actor="astra", actor_role="system")


@router.patch("/pools/{pool_id}", response_model=PoolRead)
async def update_pool(
    pool_id: str,
    payload: PoolPatch,
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.update_pool(
        pool_id, payload, actor="astra", actor_role="system"
    )


@router.delete("/pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pool(
    pool_id: str,
    service: GatewayControlService = Depends(gateway_service),
):
    await service.delete_pool(pool_id, actor="astra", actor_role="system")


# ── Node ──────────────────────────────────────────────────────────────


@router.get("/nodes", response_model=list[NodeRead])
async def list_nodes(
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.list_nodes()


@router.get("/nodes/{node_id}", response_model=NodeRead)
async def get_node(
    node_id: str,
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.get_node(node_id)


@router.post("/nodes", response_model=NodeRead, status_code=status.HTTP_201_CREATED)
async def create_node(
    payload: NodeCreate,
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.create_node(payload, actor="astra", actor_role="system")


@router.patch("/nodes/{node_id}", response_model=NodeRead)
async def update_node(
    node_id: str,
    payload: NodePatch,
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.update_node(
        node_id, payload, actor="astra", actor_role="system"
    )


@router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(
    node_id: str,
    service: GatewayControlService = Depends(gateway_service),
):
    await service.delete_node(node_id, actor="astra", actor_role="system")


@router.post("/nodes/{node_id}/drain", response_model=NodeRead)
async def drain_node(
    node_id: str,
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.set_node_scheduling(
        node_id, SCHEDULING_DRAINING, actor="astra", actor_role="system"
    )


@router.post("/nodes/{node_id}/activate", response_model=NodeRead)
async def activate_node(
    node_id: str,
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.set_node_scheduling(
        node_id, SCHEDULING_ACTIVE, actor="astra", actor_role="system"
    )


@router.post("/nodes/{node_id}/probe", response_model=NodeProbeResult)
async def probe_node(
    node_id: str,
    payload: ProbeRequest,
    service: GatewayControlService = Depends(gateway_service),
):
    node, run = await service.probe_node(
        node_id, payload.probe_types, actor="astra", actor_role="system"
    )
    return NodeProbeResult(
        node=NodeRead.model_validate(node),
        run=ProbeRunRead.model_validate(run),
        checks=probe_checks_from_run(run),
    )


@router.get("/nodes/{node_id}/probe-runs", response_model=ProbeRunList)
async def list_node_probe_runs(
    node_id: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    service: GatewayObservabilityService = Depends(gateway_observability),
):
    """按轮次返回探测历史；迁移前的步骤记录以 LEGACY_STEP 单独返回。"""

    runs = await service.list_probe_runs(node_id, limit=limit)
    legacy = await service.list_legacy_health_checks(node_id, limit=limit)
    return ProbeRunList(
        runs=[ProbeRunRead.model_validate(run) for run in runs],
        legacy_steps=[HealthCheckRead.model_validate(check) for check in legacy],
    )


# ── Route ─────────────────────────────────────────────────────────────


@router.get("/routes", response_model=list[RouteRead])
async def list_routes(
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.list_routes()


@router.post("/routes", response_model=RouteRead, status_code=status.HTTP_201_CREATED)
async def create_route(
    payload: RouteCreate,
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.create_route(payload, actor="astra", actor_role="system")


@router.patch("/routes/{route_id}", response_model=RouteRead)
async def update_route(
    route_id: str,
    payload: RoutePatch,
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.update_route(
        route_id, payload, actor="astra", actor_role="system"
    )


@router.delete("/routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_route(
    route_id: str,
    service: GatewayControlService = Depends(gateway_service),
):
    await service.delete_route(route_id, actor="astra", actor_role="system")


# ── Gateway Key ───────────────────────────────────────────────────────


@router.get("/keys", response_model=list[GatewayKeyRead])
async def list_keys(
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.list_keys()


@router.post("/keys", response_model=GatewayKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(
    payload: GatewayKeyCreate,
    service: GatewayControlService = Depends(gateway_service),
):
    key, raw = await service.create_key(
        payload, actor="astra", actor_role="system"
    )
    return GatewayKeyCreated(
        **GatewayKeyRead.model_validate(key).model_dump(), key=raw
    )


@router.patch("/keys/{key_id}", response_model=GatewayKeyRead)
async def update_key(
    key_id: str,
    payload: GatewayKeyPatch,
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.update_key(
        key_id, payload, actor="astra", actor_role="system"
    )


@router.post("/keys/{key_id}/revoke", response_model=GatewayKeyRead)
async def revoke_key(
    key_id: str,
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.revoke_key(key_id, actor="astra", actor_role="system")


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(
    key_id: str,
    service: GatewayControlService = Depends(gateway_service),
):
    await service.delete_key(key_id, actor="astra", actor_role="system")


# ── 审计 ─────────────────────────────────────────────────────────────


@router.get("/audits", response_model=list[AuditEventRead])
async def list_audits(
    resource_type: str | None = None,
    resource_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    service: GatewayControlService = Depends(gateway_service),
):
    return await service.list_audits(
        resource_type=resource_type, resource_id=resource_id, limit=limit
    )


# ── 观测：调用日志 / 统计 / 实例 ───────────────────────────────────────

StatsScope = Literal["overview", "node", "pool", "model", "key"]
StatsWindow = Literal["hour", "today", "7d", "30d"]


def _require_scope_id(scope: str, scope_id: str | None) -> str | None:
    if scope != "overview" and not scope_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{scope} 统计需要 scope_id",
        )
    return scope_id


@router.get("/requests", response_model=RequestLogPage)
async def list_gateway_requests(
    request_id: str | None = None,
    node_id: str | None = None,
    model: str | None = None,
    key_id: str | None = None,
    status_code: int | None = None,
    error_code: str | None = None,
    pool_id: str | None = None,
    started_from: Annotated[datetime | None, Query(alias="from")] = None,
    started_to: Annotated[datetime | None, Query(alias="to")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    service: GatewayObservabilityService = Depends(gateway_observability),
):
    return await service.list_requests(
        page=page,
        page_size=page_size,
        request_id=request_id,
        node_id=node_id,
        model=model,
        key_id=key_id,
        pool_id=pool_id,
        status_code=status_code,
        error_code=error_code,
        started_from=started_from,
        started_to=started_to,
    )


@router.get("/stats", response_model=ScopeStats)
async def get_gateway_stats(
    scope: StatsScope = "overview",
    scope_id: str | None = None,
    window: StatsWindow = "today",
    service: GatewayObservabilityService = Depends(gateway_observability),
):
    return await service.scope_stats(
        scope=scope, scope_id=_require_scope_id(scope, scope_id), window=window
    )


@router.get("/instances", response_model=list[GatewayInstanceRead])
async def list_gateway_instances(
    service: GatewayObservabilityService = Depends(gateway_observability),
):
    return await service.list_instances()


@router.get("/health-summary", response_model=HealthSummary)
async def get_gateway_health_summary(
    service: GatewayOpsService = Depends(gateway_ops),
):
    return await service.health_summary()


@router.get("/node-metrics", response_model=list[NodeWindowMetric])
async def get_gateway_node_metrics(
    service: GatewayOpsService = Depends(gateway_ops),
):
    return await service.node_metrics()


@router.get("/trends", response_model=TrendRead)
async def get_gateway_trends(
    window: TrendWindow = "today",
    group_by: Literal["none", "model", "node"] = "none",
    service: GatewayOpsService = Depends(gateway_ops),
):
    return await service.trends(window=window, group_by=group_by)


@router.get("/ops-events", response_model=list[OpsEventRead])
async def list_gateway_ops_events(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    service: GatewayOpsService = Depends(gateway_ops),
):
    return await service.ops_events(limit=limit)


@router.get("/nodes/{node_id}/health-checks", response_model=list[HealthCheckRead])
async def list_node_health_checks(
    node_id: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    service: GatewayObservabilityService = Depends(gateway_observability),
):
    return await service.list_health_checks(node_id, limit=limit)

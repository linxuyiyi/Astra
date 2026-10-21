"""API 中转控制面服务：配置 CRUD、探测与审计。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.health import (
    apply_probe_outcome,
    build_probe_run,
    probe_complete,
    probe_detail,
    probe_success,
    record_health_transition,
)
from app.gateway.models import (
    AUTH_NONE,
    AUTH_TYPES,
    CONFIG_DISABLED,
    CONFIG_ENABLED,
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
    HEALTH_UNKNOWN,
    PROBE_TYPES,
    SCHEDULING_ACTIVE,
    SCHEDULING_BLOCKED,
    SCHEDULING_DRAINING,
    GatewayAuditEvent,
    GatewayHealthCheck,
    GatewayKey,
    GatewayNode,
    GatewayPool,
    GatewayProbeRun,
    GatewayRoute,
)
from app.gateway.probe import (
    PROBE_STRATEGIES,
    GatewayProbeClient,
    probe_types_for_strategy,
    strategy_for_types,
)
from app.gateway.runtime import SCOPE_NODE, RuntimeStore, node_runtime_scope
from app.gateway.schemas import (
    AuditEventRead,
    GatewayKeyCreate,
    GatewayKeyPatch,
    HealthCheckRead,
    NodeCreate,
    NodePatch,
    PoolCreate,
    PoolPatch,
    RouteCreate,
    RoutePatch,
)
from app.gateway.security import (
    CredentialCipher,
    gateway_key_digest,
    gateway_key_prefix,
    generate_gateway_key,
)
from app.models import utcnow

# 探测失败时熔断打开窗口：与数据面保持一致（OPEN 30 秒后进入 HALF_OPEN）。
RUNTIME_CIRCUIT_OPEN_SECONDS = 30
VALID_CONFIG_STATES = {CONFIG_ENABLED, CONFIG_DISABLED}
VALID_SCHEDULING_STATES = {SCHEDULING_ACTIVE, SCHEDULING_DRAINING, SCHEDULING_BLOCKED}
VALID_QUOTA_POLICIES = {"STOP", "ALERT", "RECORD"}

# 改变节点可达性或认证的配置字段：任一变化都让旧健康态失效，必须重新完整探测后
# 才允许被调度，避免旧健康状态继续授权新配置。凭据（api_key）单独判断。
NODE_CONNECTION_FIELDS = (
    "base_url",
    "auth_type",
    "auth_header",
    "models",
    "health_path",
    "probe_strategy",
)


class GatewayError(Exception):
    """控制面领域错误基类。"""


class GatewayNotFound(GatewayError):
    pass


class GatewayConflict(GatewayError):
    pass


class GatewayRuntimeUnavailable(GatewayError):
    pass


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class GatewayControlService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        cipher: CredentialCipher,
        probe_client: GatewayProbeClient,
        runtime: RuntimeStore | None = None,
        circuit_open_seconds: int = RUNTIME_CIRCUIT_OPEN_SECONDS,
        default_health_path: str = "/health",
    ) -> None:
        self._session = session
        self._cipher = cipher
        self._probe_client = probe_client
        self._runtime = runtime
        self._circuit_open_seconds = circuit_open_seconds
        self._default_health_path = default_health_path or "/health"

    # ── 审计 ───────────────────────────────────────────────────────────

    def _audit(
        self,
        *,
        actor: str,
        actor_role: str,
        action: str,
        resource_type: str,
        resource_id: str | None,
        result: str = "SUCCESS",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> None:
        self._session.add(
            GatewayAuditEvent(
                actor=actor,
                actor_role=actor_role,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                result=result,
                before=before or {},
                after=after or {},
            )
        )

    async def list_audits(
        self,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        limit: int = 100,
    ) -> list[GatewayAuditEvent]:
        stmt = select(GatewayAuditEvent).order_by(GatewayAuditEvent.created_at.desc()).limit(limit)
        if resource_type:
            stmt = stmt.where(GatewayAuditEvent.resource_type == resource_type)
        if resource_id:
            stmt = stmt.where(GatewayAuditEvent.resource_id == resource_id)
        return list(await self._session.scalars(stmt))

    # ── 落库 / 事务失败 ────────────────────────────────────────────────────

    async def _commit(self) -> None:
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise GatewayConflict("唯一约束冲突，请检查名称或模型是否重复") from exc

    # ── Pool ───────────────────────────────────────────────────────────

    async def list_pools(self) -> list[GatewayPool]:
        return list(
            await self._session.scalars(
                select(GatewayPool).order_by(GatewayPool.sort_order, GatewayPool.name)
            )
        )

    async def get_pool(self, pool_id: str) -> GatewayPool:
        pool = await self._session.get(GatewayPool, pool_id)
        if not pool:
            raise GatewayNotFound("资源池不存在")
        return pool

    async def create_pool(self, payload: PoolCreate, *, actor: str, actor_role: str) -> GatewayPool:
        pool = GatewayPool(id=f"gp_{uuid4().hex[:12]}", **payload.model_dump())
        self._session.add(pool)
        await self._session.flush()
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="pool.create",
            resource_type="pool",
            resource_id=pool.id,
            after=self._pool_dict(pool),
        )
        await self._commit()
        return pool

    async def update_pool(
        self, pool_id: str, payload: PoolPatch, *, actor: str, actor_role: str
    ) -> GatewayPool:
        pool = await self.get_pool(pool_id)
        before = self._pool_dict(pool)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(pool, field, value)
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="pool.update",
            resource_type="pool",
            resource_id=pool.id,
            before=before,
            after=self._pool_dict(pool),
        )
        await self._commit()
        return pool

    async def delete_pool(self, pool_id: str, *, actor: str, actor_role: str) -> None:
        pool = await self.get_pool(pool_id)
        node_count = await self._session.scalar(
            select(func.count()).select_from(GatewayNode).where(GatewayNode.pool_id == pool_id)
        )
        route_count = await self._session.scalar(
            select(func.count()).select_from(GatewayRoute).where(GatewayRoute.pool_id == pool_id)
        )
        if node_count or route_count:
            raise GatewayConflict("资源池仍被节点或路由引用，无法删除")
        before = self._pool_dict(pool)
        await self._session.delete(pool)
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="pool.delete",
            resource_type="pool",
            resource_id=pool_id,
            before=before,
        )
        await self._commit()

    # ── Node ───────────────────────────────────────────────────────────

    async def list_nodes(self) -> list[GatewayNode]:
        return list(
            await self._session.scalars(
                select(GatewayNode).order_by(GatewayNode.sort_order, GatewayNode.name)
            )
        )

    async def get_node(self, node_id: str) -> GatewayNode:
        node = await self._session.get(GatewayNode, node_id)
        if not node:
            raise GatewayNotFound("节点不存在")
        return node

    async def create_node(self, payload: NodeCreate, *, actor: str, actor_role: str) -> GatewayNode:
        self._validate_node_states(payload.config_state, payload.scheduling_state, payload.quota_policy)
        self._validate_node_auth(payload.auth_type, payload.auth_header)
        self._validate_node_base_url(payload.base_url)
        self._validate_node_strategy(payload.probe_strategy)
        self._validate_node_health_path(payload.health_path)
        if "scheduling_state" in payload.model_fields_set:
            # 显式要求以 ACTIVE 创建的新节点尚未通过任何探测，直接拒绝。
            self._validate_scheduling_activation(
                current_scheduling=SCHEDULING_DRAINING,
                target_scheduling=payload.scheduling_state,
                health_state=HEALTH_UNKNOWN,
            )
        await self._ensure_pool(payload.pool_id)
        data = payload.model_dump(exclude={"api_key"})
        # 健康路径显式回填有效部署值，不再依赖探测时的隐式继承。
        data["health_path"] = (data.get("health_path") or "").strip() or self._default_health_path
        api_key_encrypted = self._cipher.encrypt(payload.api_key) if payload.api_key else None
        node = GatewayNode(id=f"gn_{uuid4().hex[:12]}", api_key_encrypted=api_key_encrypted, **data)
        self._session.add(node)
        await self._session.flush()
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="node.create",
            resource_type="node",
            resource_id=node.id,
            after=self._redact_node(node),
        )
        await self._commit()
        return node

    async def update_node(
        self, node_id: str, payload: NodePatch, *, actor: str, actor_role: str
    ) -> GatewayNode:
        node = await self.get_node(node_id)
        before = self._redact_node(node)
        critical_change = self._node_connection_changed(node, payload)
        if "api_key" in payload.model_fields_set:
            if payload.api_key:
                node.api_key_encrypted = self._cipher.encrypt(payload.api_key)
            else:
                node.api_key_encrypted = None
        data = payload.model_dump(exclude={"api_key"}, exclude_unset=True)
        if data.get("pool_id"):
            await self._ensure_pool(data["pool_id"])
        if "base_url" in data:
            self._validate_node_base_url(data["base_url"])
        if "health_path" in data:
            self._validate_node_health_path(data["health_path"])
            if not data["health_path"].strip():
                data["health_path"] = self._default_health_path
        if "probe_strategy" in data:
            self._validate_node_strategy(data["probe_strategy"])
        self._validate_node_states(
            data.get("config_state", node.config_state),
            data.get("scheduling_state", node.scheduling_state),
            data.get("quota_policy", node.quota_policy),
        )
        self._validate_node_auth(
            data.get("auth_type", node.auth_type),
            data.get("auth_header", node.auth_header),
        )
        # 连接配置一旦变化，旧健康态立即失效，不允许它继续授权新配置进入调度。
        self._validate_scheduling_activation(
            current_scheduling=node.scheduling_state,
            target_scheduling=data.get("scheduling_state", node.scheduling_state),
            health_state=HEALTH_UNKNOWN if critical_change else node.health_state,
        )
        for field, value in data.items():
            setattr(node, field, value)
        if critical_change:
            # 新连接必须重新探测；刷新前的请求仍可能使用旧连接，因此保留旧作用域
            # 的健康/熔断，并按连接指纹隔离新配置。策略变化不改变连接指纹，
            # 必须显式清掉运行态授权。
            self._reset_node_health(node)
            await self._reset_runtime_health(node)
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="node.update",
            resource_type="node",
            resource_id=node.id,
            before=before,
            after=self._redact_node(node),
        )
        await self._commit()
        return node

    async def delete_node(self, node_id: str, *, actor: str, actor_role: str) -> None:
        node = await self.get_node(node_id)
        if node.scheduling_state != SCHEDULING_DRAINING:
            raise GatewayConflict("节点仍在调度中，请先安全下线（DRAINING）后再删除")
        # 与数据面同一事实来源：RuntimeStore.active 读取按 lease 的 ZSET。
        # 绝不能再读已废弃的计数键，否则永远得到 0 而误删活跃节点。
        if self._runtime is None:
            raise GatewayRuntimeUnavailable("运行态未配置，无法确认节点活跃并发，已拒绝删除")
        try:
            active = await self._runtime.active(SCOPE_NODE, node.id)
        except Exception as exc:  # noqa: BLE001 - Redis 不可达时拒绝删除
            raise GatewayRuntimeUnavailable(
                "无法确认节点活跃并发（Redis 不可用），已拒绝删除"
            ) from exc
        if active > 0:
            raise GatewayConflict(f"节点仍有 {active} 个活跃请求，无法删除")
        before = self._redact_node(node)
        await self._session.delete(node)
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="node.delete",
            resource_type="node",
            resource_id=node_id,
            before=before,
        )
        await self._commit()

    async def set_node_scheduling(
        self, node_id: str, scheduling_state: str, *, actor: str, actor_role: str
    ) -> GatewayNode:
        node = await self.get_node(node_id)
        if scheduling_state not in VALID_SCHEDULING_STATES:
            raise GatewayConflict("非法调度状态")
        self._validate_scheduling_activation(
            current_scheduling=node.scheduling_state,
            target_scheduling=scheduling_state,
            health_state=node.health_state,
        )
        before = self._redact_node(node)
        node.scheduling_state = scheduling_state
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="node.drain" if scheduling_state == SCHEDULING_DRAINING else "node.activate",
            resource_type="node",
            resource_id=node.id,
            before=before,
            after=self._redact_node(node),
        )
        await self._commit()
        return node

    async def probe_node(
        self,
        node_id: str,
        probe_types: list[str],
        *,
        actor: str,
        actor_role: str,
    ) -> tuple[GatewayNode, GatewayProbeRun]:
        node = await self.get_node(node_id)
        selected = probe_types or probe_types_for_strategy(node.probe_strategy)
        unknown = [item for item in selected if item not in PROBE_TYPES]
        if unknown:
            raise GatewayConflict(f"未知探测类型: {', '.join(unknown)}")
        api_key = self._cipher.decrypt(node.api_key_encrypted) if node.api_key_encrypted else None
        started_at = utcnow()
        started_clock = time.monotonic()
        outcomes = await self._probe_client.probe(node, selected, api_key)
        duration_ms = int((time.monotonic() - started_clock) * 1000)
        run = build_probe_run(
            node,
            outcomes,
            source="MANUAL",
            started_at=started_at,
            duration_ms=duration_ms,
            strategy=strategy_for_types(selected),
        )
        self._session.add(run)
        success = probe_success(outcomes)
        previous_health = node.health_state
        if probe_complete(outcomes, probe_types_for_strategy(strategy_for_types(selected))):
            apply_probe_outcome(node, success=success)
            record_health_transition(self._session, node, previous_health)
            node.health_detail = probe_detail(node, outcomes)
            await self._record_runtime_probe(node, success, node.health_detail)
        else:
            # 局部探测不推进健康也不写运行态：只有完整 NETWORK/SERVICE/MODEL/STREAM
            # 结果才能让节点脱离 UNKNOWN，避免局部成功被误判为可调度。
            node.last_checked_at = utcnow()
            node.health_detail = probe_detail(node, outcomes)
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="node.probe",
            resource_type="node",
            resource_id=node.id,
            after={
                "health_state": node.health_state,
                "success_streak": node.success_streak,
                "failure_streak": node.failure_streak,
            },
        )
        await self._commit()
        return node, run

    # ── Route ──────────────────────────────────────────────────────────

    async def list_routes(self) -> list[GatewayRoute]:
        return list(
            await self._session.scalars(
                select(GatewayRoute).order_by(GatewayRoute.model)
            )
        )

    async def get_route(self, route_id: str) -> GatewayRoute:
        route = await self._session.get(GatewayRoute, route_id)
        if not route:
            raise GatewayNotFound("路由不存在")
        return route

    async def create_route(
        self, payload: RouteCreate, *, actor: str, actor_role: str
    ) -> GatewayRoute:
        if not payload.model.strip():
            raise GatewayConflict("路由模型不能为空")
        if not payload.pool_id.strip():
            raise GatewayConflict("路由必须指定资源池")
        await self._ensure_pool(payload.pool_id)
        route = GatewayRoute(id=f"gr_{uuid4().hex[:12]}", **payload.model_dump())
        self._session.add(route)
        await self._session.flush()
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="route.create",
            resource_type="route",
            resource_id=route.id,
            after=self._route_dict(route),
        )
        await self._commit()
        return route

    async def update_route(
        self, route_id: str, payload: RoutePatch, *, actor: str, actor_role: str
    ) -> GatewayRoute:
        route = await self.get_route(route_id)
        before = self._route_dict(route)
        data = payload.model_dump(exclude_unset=True)
        model = data.get("model", route.model)
        pool_id = data.get("pool_id", route.pool_id)
        if not model or not model.strip():
            raise GatewayConflict("路由模型不能为空")
        if not pool_id or not pool_id.strip():
            raise GatewayConflict("路由必须指定资源池")
        await self._ensure_pool(pool_id)
        for field, value in data.items():
            setattr(route, field, value)
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="route.update",
            resource_type="route",
            resource_id=route.id,
            before=before,
            after=self._route_dict(route),
        )
        await self._commit()
        return route

    async def delete_route(self, route_id: str, *, actor: str, actor_role: str) -> None:
        route = await self.get_route(route_id)
        before = self._route_dict(route)
        await self._session.delete(route)
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="route.delete",
            resource_type="route",
            resource_id=route_id,
            before=before,
        )
        await self._commit()

    # ── Gateway Key ────────────────────────────────────────────────────

    async def list_keys(self) -> list[GatewayKey]:
        return list(
            await self._session.scalars(
                select(GatewayKey)
                .where(GatewayKey.deleted_at.is_(None))
                .order_by(GatewayKey.created_at.desc())
            )
        )

    async def get_key(self, key_id: str) -> GatewayKey:
        key = await self._session.get(GatewayKey, key_id)
        if not key or key.deleted_at is not None:
            raise GatewayNotFound("Gateway Key 不存在")
        return key


    async def create_key(
        self, payload: GatewayKeyCreate, *, actor: str, actor_role: str
    ) -> tuple[GatewayKey, str]:
        raw = generate_gateway_key()
        key = GatewayKey(
            id=f"gk_{uuid4().hex[:12]}",
            key_prefix=gateway_key_prefix(raw),
            key_digest=gateway_key_digest(raw),
            **payload.model_dump(),
        )
        self._session.add(key)
        await self._session.flush()
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="key.create",
            resource_type="key",
            resource_id=key.id,
            after=self._redact_key(key),
        )
        await self._commit()
        return key, raw

    async def update_key(
        self, key_id: str, payload: GatewayKeyPatch, *, actor: str, actor_role: str
    ) -> GatewayKey:
        key = await self.get_key(key_id)
        before = self._redact_key(key)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(key, field, value)
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="key.update",
            resource_type="key",
            resource_id=key.id,
            before=before,
            after=self._redact_key(key),
        )
        await self._commit()
        return key

    async def revoke_key(self, key_id: str, *, actor: str, actor_role: str) -> GatewayKey:
        key = await self.get_key(key_id)
        before = self._redact_key(key)
        await self._block_key(key)
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="key.revoke",
            resource_type="key",
            resource_id=key.id,
            before=before,
            after=self._redact_key(key),
        )
        await self._commit()
        return key

    async def delete_key(self, key_id: str, *, actor: str, actor_role: str) -> None:
        key = await self.get_key(key_id)
        before = self._redact_key(key)
        await self._block_key(key)
        key.deleted_at = utcnow()
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="key.delete",
            resource_type="key",
            resource_id=key.id,
            before=before,
            after=self._redact_key(key),
        )
        await self._commit()

    async def _block_key(self, key: GatewayKey) -> None:
        # 先写共享阻断状态再提交 DB 吊销：数据面配置缓存尚未刷新时也必须立即
        # 阻断旧 Key。Redis 失败时返回结构化 503，不提交 DB 吊销或删除。
        if self._runtime is None:
            raise GatewayRuntimeUnavailable("运行态未配置，无法写入 Key 阻断状态，操作已拒绝")
        try:
            await self._runtime.revoke_key_digest(key.key_digest)
        except Exception as exc:  # noqa: BLE001 - Redis 不可达时拒绝吊销和删除
            raise GatewayRuntimeUnavailable(
                "Key 阻断状态未能写入运行态（Redis 不可用），操作已拒绝，请稍后重试"
            ) from exc
        key.enabled = False
        if key.revoked_at is None:
            key.revoked_at = utcnow()


    async def _record_runtime_probe(
        self, node: GatewayNode, success: bool, detail: dict[str, Any]
    ) -> None:
        """把探测结果写入当前连接指纹作用域，供数据面读取最新健康态。

        连接参数变化后，探测结果落在新作用域，刷新前请求所用的旧连接健康与
        熔断不受影响；完全相同的连接自然共享运行态。
        """

        if self._runtime is None:
            return
        scope = node_runtime_scope(node)
        try:
            await self._runtime.record_probe(scope, success=success, detail=detail)
            if success:
                await self._runtime.set_circuit_closed(scope)
            else:
                await self._runtime.set_circuit_open(scope, self._circuit_open_seconds)
        except Exception:  # noqa: BLE001 - 运行态写入失败不阻断控制面响应
            pass


    # ── 脱敏 / 序列化 ──────────────────────────────────────────────────

    def _redact_node(self, node: GatewayNode) -> dict[str, Any]:
        return NodeReadDict.from_node(node)

    def _redact_key(self, key: GatewayKey) -> dict[str, Any]:
        return {
            "id": key.id,
            "name": key.name,
            "key_prefix": key.key_prefix,
            "owner_id": key.owner_id,
            "allowed_models": list(key.allowed_models or []),
            "max_concurrency": key.max_concurrency,
            "hourly_token_limit": key.hourly_token_limit,
            "daily_token_limit": key.daily_token_limit,
            "enabled": key.enabled,
            "expires_at": _iso(key.expires_at),
            "revoked_at": _iso(key.revoked_at),
            "deleted_at": _iso(key.deleted_at),
        }

    @staticmethod
    def _pool_dict(pool: GatewayPool) -> dict[str, Any]:
        return {
            "id": pool.id,
            "name": pool.name,
            "description": pool.description,
            "weight": pool.weight,
            "enabled": pool.enabled,
            "sort_order": pool.sort_order,
        }


    @staticmethod
    def _route_dict(route: GatewayRoute) -> dict[str, Any]:
        return {
            "id": route.id,
            "model": route.model,
            "pool_id": route.pool_id,
            "target_model": route.target_model,
            "description": route.description,
            "enabled": route.enabled,
            "weight": route.weight,
        }


    # ── 校验辅助 ───────────────────────────────────────────────────────

    async def _ensure_pool(self, pool_id: str | None) -> None:
        if pool_id and not await self._session.get(GatewayPool, pool_id):
            raise GatewayConflict("资源池不存在")

    @staticmethod
    def _validate_node_states(config_state: str, scheduling_state: str, quota_policy: str) -> None:
        if config_state not in VALID_CONFIG_STATES:
            raise GatewayConflict("非法配置状态")
        if scheduling_state not in VALID_SCHEDULING_STATES:
            raise GatewayConflict("非法调度状态")
        if quota_policy not in VALID_QUOTA_POLICIES:
            raise GatewayConflict("非法额度策略")

    @staticmethod
    def _validate_node_auth(auth_type: str, auth_header: str) -> None:
        normalized = str(auth_type or "").strip().upper()
        if normalized not in AUTH_TYPES:
            raise GatewayConflict("非法认证方式")
        if normalized == AUTH_NONE:
            return
        if not (auth_header or "").strip():
            raise GatewayConflict("认证 Header 不能为空")

    @staticmethod
    def _validate_node_base_url(base_url: str) -> None:
        """节点地址必须是根地址：数据面/探测会自动追加 /v1/... 路径。"""

        parsed = urlparse(str(base_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise GatewayConflict("Base URL 必须是完整的 HTTP(S) 地址")
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            raise GatewayConflict(
                "Base URL 应填写节点根地址，不要包含 /v1；"
                "Gateway 会自动追加 /v1/chat/completions，否则会请求到 /v1/v1/..."
            )

    @staticmethod
    def _validate_node_health_path(health_path: str) -> None:
        if health_path and not health_path.strip().startswith("/"):
            raise GatewayConflict("节点健康路径必须以 / 开头")

    @staticmethod
    def _validate_scheduling_activation(
        *, current_scheduling: str, target_scheduling: str, health_state: str
    ) -> None:
        """只有健康态 HEALTHY/DEGRADED 的节点才允许新进入 ACTIVE 调度。"""

        if (
            target_scheduling == SCHEDULING_ACTIVE
            and current_scheduling != SCHEDULING_ACTIVE
            and health_state not in (HEALTH_HEALTHY, HEALTH_DEGRADED)
        ):
            raise GatewayConflict(
                "节点尚未通过完整探测（健康态非 HEALTHY/DEGRADED），不能进入 ACTIVE 调度"
            )

    @staticmethod
    def _node_connection_changed(node: GatewayNode, payload: NodePatch) -> bool:
        if "api_key" in payload.model_fields_set:
            return True
        data = payload.model_dump(exclude={"api_key"}, exclude_unset=True)
        return any(
            field in data and data[field] != getattr(node, field)
            for field in NODE_CONNECTION_FIELDS
        )

    async def _reset_runtime_health(self, node: GatewayNode) -> None:
        if self._runtime is None:
            return
        try:
            await self._runtime.reset_health(node_runtime_scope(node))
        except Exception:  # noqa: BLE001 - 运行态不可用时由缺失状态兜底为 UNKNOWN
            return

    @staticmethod
    def _validate_node_strategy(strategy: str) -> None:
        if strategy not in PROBE_STRATEGIES:
            raise GatewayConflict(f"未知探测策略: {strategy}")

    @staticmethod
    def _reset_node_health(node: GatewayNode) -> None:
        node.health_state = HEALTH_UNKNOWN
        node.success_streak = 0
        node.failure_streak = 0
        node.health_detail = {}
        node.last_checked_at = None


class NodeReadDict:
    """节点审计用脱敏字典：绝不包含加密凭据。"""

    @staticmethod
    def from_node(node: GatewayNode) -> dict[str, Any]:
        return {
            "id": node.id,
            "name": node.name,
            "pool_id": node.pool_id,
            "base_url": node.base_url,
            "auth_type": node.auth_type,
            "auth_header": node.auth_header,
            "connect_timeout_seconds": node.connect_timeout_seconds,
            "request_timeout_seconds": node.request_timeout_seconds,
            "health_path": node.health_path,
            "probe_interval_seconds": node.probe_interval_seconds,
            "models": list(node.models or []),
            "weight": node.weight,
            "max_concurrency": node.max_concurrency,
            "hourly_token_limit": node.hourly_token_limit,
            "daily_token_limit": node.daily_token_limit,
            "quota_policy": node.quota_policy,
            "config_state": node.config_state,
            "scheduling_state": node.scheduling_state,
            "health_state": node.health_state,
            "has_api_key": node.has_api_key,
            "sort_order": node.sort_order,
        }


def probe_checks_from_run(run: GatewayProbeRun) -> list[HealthCheckRead]:
    """把轮次里的步骤投影成旧的 checks 形状（页面过渡期兼容，不写库）。"""

    checks: list[HealthCheckRead] = []
    for index, step in enumerate(run.steps or []):
        checks.append(
            HealthCheckRead(
                id=-(index + 1),
                node_id=run.node_id,
                probe_type=str(step.get("probe_type") or ""),
                success=bool(step.get("success")),
                latency_ms=int(step.get("latency_ms") or 0),
                status_code=step.get("status_code"),
                error=step.get("error"),
                detail=step.get("detail") if isinstance(step.get("detail"), dict) else {},
                created_at=run.finished_at or run.started_at,
            )
        )
    return checks


def health_check_read(check: GatewayHealthCheck) -> HealthCheckRead:
    return HealthCheckRead.model_validate(check)


def audit_read(event: GatewayAuditEvent) -> AuditEventRead:
    return AuditEventRead.model_validate(event)

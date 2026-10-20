"""已保存配置的内存视图与自动刷新。

数据面每次请求只读 :class:`GatewayConfigState` 里的内存快照，绝不查询 MySQL。
启动及后台刷新从配置表读取同一事务里的完整配置，成功后原子替换内存视图。
读取失败时保留最近有效配置，不维护版本历史或发布状态。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.gateway.models import GatewayKey, GatewayNode, GatewayPool, GatewayRoute
from app.gateway.runtime import node_runtime_scope

logger = logging.getLogger(__name__)


def _text(value: Any, default: str = "") -> str:
    return str(value) if value is not None else default


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass(frozen=True)
class PoolConfig:
    id: str
    name: str
    weight: int
    enabled: bool


@dataclass(frozen=True)
class NodeConfig:
    id: str
    name: str
    pool_id: str | None
    base_url: str
    api_key_encrypted: str | None
    auth_type: str
    auth_header: str
    connect_timeout_seconds: float | None
    request_timeout_seconds: float | None
    health_path: str
    probe_interval_seconds: int | None
    models: tuple[str, ...]
    weight: int
    max_concurrency: int
    hourly_token_limit: int | None
    daily_token_limit: int | None
    quota_policy: str
    config_state: str
    scheduling_state: str
    health_state: str

    @property
    def runtime_scope(self) -> str:
        """健康/熔断运行态作用域：只对完全相同的下游连接参数有效。"""

        return node_runtime_scope(self)

    @classmethod
    def from_snapshot(cls, item: Mapping[str, Any]) -> NodeConfig:
        return cls(
            id=_text(item.get("id")),
            name=_text(item.get("name")),
            pool_id=item.get("pool_id") or None,
            base_url=_text(item.get("base_url")),
            api_key_encrypted=item.get("api_key_encrypted") or None,
            auth_type=_text(item.get("auth_type"), "BEARER"),
            auth_header=_text(item.get("auth_header"), "Authorization"),
            connect_timeout_seconds=_optional_float(item.get("connect_timeout_seconds")),
            request_timeout_seconds=_optional_float(item.get("request_timeout_seconds")),
            health_path=_text(item.get("health_path")),
            probe_interval_seconds=_optional_int(item.get("probe_interval_seconds")),
            models=tuple(str(model) for model in (item.get("models") or [])),
            weight=int(item.get("weight") or 0),
            max_concurrency=int(item.get("max_concurrency") or 0),
            hourly_token_limit=_optional_int(item.get("hourly_token_limit")),
            daily_token_limit=_optional_int(item.get("daily_token_limit")),
            quota_policy=_text(item.get("quota_policy"), "STOP"),
            config_state=_text(item.get("config_state"), "ENABLED"),
            scheduling_state=_text(item.get("scheduling_state"), "ACTIVE"),
            health_state=_text(item.get("health_state"), "UNKNOWN"),
        )


@dataclass(frozen=True)
class RouteConfig:
    id: str
    model: str
    pool_id: str
    enabled: bool
    weight: int
    target_model: str | None = None

    @classmethod
    def from_snapshot(cls, item: Mapping[str, Any]) -> RouteConfig:
        return cls(
            id=_text(item.get("id")),
            model=_text(item.get("model")),
            pool_id=_text(item.get("pool_id")),
            enabled=bool(item.get("enabled", True)),
            weight=int(item.get("weight") or 0),
            target_model=item.get("target_model") or None,
        )


@dataclass(frozen=True)
class KeyConfig:
    id: str
    name: str
    key_prefix: str
    key_digest: str
    owner_id: str | None
    allowed_models: tuple[str, ...]
    max_concurrency: int
    hourly_token_limit: int | None
    daily_token_limit: int | None
    enabled: bool
    expires_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def from_snapshot(cls, item: Mapping[str, Any]) -> KeyConfig:
        return cls(
            id=_text(item.get("id")),
            name=_text(item.get("name")),
            key_prefix=_text(item.get("key_prefix")),
            key_digest=_text(item.get("key_digest")),
            owner_id=item.get("owner_id") or None,
            allowed_models=tuple(str(model) for model in (item.get("allowed_models") or [])),
            max_concurrency=int(item.get("max_concurrency") or 0),
            hourly_token_limit=_optional_int(item.get("hourly_token_limit")),
            daily_token_limit=_optional_int(item.get("daily_token_limit")),
            enabled=bool(item.get("enabled", True)),
            expires_at=_optional_dt(item.get("expires_at")),
            revoked_at=_optional_dt(item.get("revoked_at")),
        )


@dataclass(frozen=True)
class GatewayConfig:
    pools: Mapping[str, PoolConfig]
    nodes: Mapping[str, NodeConfig]
    routes: Mapping[str, RouteConfig]
    keys: Mapping[str, KeyConfig]

    @classmethod
    def empty(cls) -> GatewayConfig:
        return cls(pools={}, nodes={}, routes={}, keys={})

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any] | None) -> GatewayConfig:
        data = snapshot or {}
        pools = {
            item["id"]: PoolConfig(
                id=_text(item.get("id")),
                name=_text(item.get("name")),
                weight=int(item.get("weight") or 0),
                enabled=bool(item.get("enabled", True)),
            )
            for item in data.get("pools", [])
            if item.get("id")
        }
        nodes = {
            item["id"]: NodeConfig.from_snapshot(item)
            for item in data.get("nodes", [])
            if item.get("id")
        }
        routes = {
            item["model"]: RouteConfig.from_snapshot(item)
            for item in data.get("routes", [])
            if item.get("model")
        }
        keys = {
            item["key_digest"]: KeyConfig.from_snapshot(item)
            for item in data.get("keys", [])
            if item.get("key_digest")
        }
        return cls(pools=pools, nodes=nodes, routes=routes, keys=keys)

    @property
    def empty_config(self) -> bool:
        return not self.nodes and not self.routes


class GatewayConfigState:
    """进程内原子替换的完整配置；旧请求持有旧视图直到结束。"""

    def __init__(self, config: GatewayConfig | None = None) -> None:
        self._config = config or GatewayConfig.empty()
        self._lock = asyncio.Lock()

    @property
    def config(self) -> GatewayConfig:
        return self._config

    async def replace(self, config: GatewayConfig) -> None:
        async with self._lock:
            self._config = config


class ConfigLoader:
    """启动与周期刷新时从配置表读取一致视图，不在请求路径查询数据库。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_current(self) -> GatewayConfig:
        async with self._session_factory() as session, session.begin():
            # 四张表必须来自同一数据库视图。SQLite 的 legacy transaction 模式
            # 不会为 SELECT 自动 BEGIN；MySQL 显式用 REPEATABLE READ 固定读取视图。
            dialect = session.get_bind().dialect.name
            if dialect == "sqlite":
                await session.execute(text("BEGIN"))
            elif dialect == "mysql":
                await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            snapshot: dict[str, Any] = {}
            for name, model in (
                ("pools", GatewayPool), ("nodes", GatewayNode),
                ("routes", GatewayRoute), ("keys", GatewayKey),
            ):
                stmt = select(model)
                if model is GatewayKey:
                    stmt = stmt.where(GatewayKey.deleted_at.is_(None))
                rows = await session.scalars(stmt)
                snapshot[name] = [
                    {column.name: getattr(row, column.name) for column in model.__table__.columns}
                    for row in rows
                ]
            return GatewayConfig.from_snapshot(snapshot)


class ConfigRefresher:
    """周期读取保存配置，失败保留旧视图，恢复后自动追平。"""

    def __init__(
        self,
        *,
        state: GatewayConfigState,
        loader: ConfigLoader,
        interval: float,
    ) -> None:
        self._state = state
        self._loader = loader
        self._interval = interval
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    async def tick(self) -> None:
        try:
            config = await self._loader.load_current()
        except Exception:  # noqa: BLE001 - 数据库故障时保留旧配置并在下一周期重试
            logger.warning("Gateway 配置刷新失败，保留当前配置", exc_info=True)
            return
        if config != self._state.config:
            await self._state.replace(config)

    def stop(self) -> None:
        self._stop.set()

"""API 中转持久化模型。

节点 / Pool / 路由 / Key 表是唯一配置事实源，数据面自动刷新已保存配置。
调用日志与健康记录只保存元数据，不保存 Prompt、Authorization 或原始响应体。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, UTCDateTime, utcnow

# 三维节点状态：配置态、健康态、调度态相互独立。
CONFIG_ENABLED = "ENABLED"
CONFIG_DISABLED = "DISABLED"
# 健康态：UNKNOWN 表示尚未通过完整探测，CHECKING 表示探测进行中，DEGRADED 表示
# 被动故障降级但未达到摘除阈值。只有 HEALTHY / DEGRADED 允许被数据面调度。
HEALTH_UNKNOWN = "UNKNOWN"
HEALTH_CHECKING = "CHECKING"
HEALTH_HEALTHY = "HEALTHY"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_UNHEALTHY = "UNHEALTHY"
HEALTH_STATES = (
    HEALTH_UNKNOWN,
    HEALTH_CHECKING,
    HEALTH_HEALTHY,
    HEALTH_DEGRADED,
    HEALTH_UNHEALTHY,
)
# 调度态：ACTIVE 允许新请求，DRAINING 仅阻止新请求，BLOCKED 由额度或管理员显式阻止。
SCHEDULING_ACTIVE = "ACTIVE"
SCHEDULING_DRAINING = "DRAINING"
SCHEDULING_BLOCKED = "BLOCKED"
SCHEDULING_STATES = (SCHEDULING_ACTIVE, SCHEDULING_DRAINING, SCHEDULING_BLOCKED)

PROBE_NETWORK = "NETWORK"
PROBE_SERVICE = "SERVICE"
PROBE_MODEL = "MODEL"
PROBE_STREAM = "STREAM"
PROBE_TYPES = (PROBE_NETWORK, PROBE_SERVICE, PROBE_MODEL, PROBE_STREAM)

# 节点下游认证方式：Bearer Token、自定义 Header（值取自加密的 api_key_encrypted）、
# 无认证。自定义 Header 名称由 auth_header 指定，凭据值绝不单独明文落库。
AUTH_BEARER = "BEARER"
AUTH_HEADER = "HEADER"
AUTH_NONE = "NONE"
AUTH_TYPES = (AUTH_BEARER, AUTH_HEADER, AUTH_NONE)
DEFAULT_HEALTH_PATH = "/health"

class GatewayPool(Base):
    __tablename__ = "t_nexus_gateway_pools"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class GatewayNode(Base):
    __tablename__ = "t_nexus_gateway_nodes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    pool_id: Mapped[str | None] = mapped_column(
        ForeignKey("t_nexus_gateway_pools.id", ondelete="SET NULL"), nullable=True, index=True
    )
    base_url: Mapped[str] = mapped_column(String(512))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_type: Mapped[str] = mapped_column(String(16), default=AUTH_BEARER)
    auth_header: Mapped[str] = mapped_column(String(64), default="Authorization")
    connect_timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    request_timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    health_path: Mapped[str] = mapped_column(String(256), default="")
    probe_strategy: Mapped[str] = mapped_column(
        String(16), default="SSE", server_default="SSE"
    )
    probe_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    models: Mapped[list[str]] = mapped_column(JSON, default=list)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=0)
    hourly_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_policy: Mapped[str] = mapped_column(String(16), default="STOP")
    config_state: Mapped[str] = mapped_column(String(16), default=CONFIG_ENABLED, index=True)
    scheduling_state: Mapped[str] = mapped_column(String(16), default=SCHEDULING_ACTIVE, index=True)
    health_state: Mapped[str] = mapped_column(String(16), default=HEALTH_UNKNOWN, index=True)
    success_streak: Mapped[int] = mapped_column(Integer, default=0)
    failure_streak: Mapped[int] = mapped_column(Integer, default=0)
    health_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key_encrypted)


class GatewayRoute(Base):
    __tablename__ = "t_nexus_gateway_routes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    pool_id: Mapped[str] = mapped_column(
        ForeignKey("t_nexus_gateway_pools.id", ondelete="RESTRICT"), index=True
    )
    target_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class GatewayKey(Base):
    __tablename__ = "t_nexus_gateway_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    key_prefix: Mapped[str] = mapped_column(String(24), index=True)
    key_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    allowed_models: Mapped[list[str]] = mapped_column(JSON, default=list)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=0)
    hourly_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class GatewayHealthCheck(Base):
    __tablename__ = "t_nexus_gateway_health_checks"
    __table_args__ = (Index("ix_gateway_health_node_time", "node_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("t_nexus_gateway_nodes.id", ondelete="CASCADE"), index=True
    )
    probe_type: Mapped[str] = mapped_column(String(16), index=True)
    success: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class GatewayProbeRun(Base):
    """一次探测操作 = 一条轮次事实。

    步骤存在 ``steps`` 里，不再为每个步骤写一行；迁移前的步骤记录保留在
    ``t_nexus_gateway_health_checks``，以 LEGACY_STEP 只读展示。
    """

    __tablename__ = "t_nexus_gateway_probe_runs"
    __table_args__ = (Index("ix_gateway_probe_runs_node_time", "node_id", "started_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("t_nexus_gateway_nodes.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(16))  # MANUAL / AUTO
    strategy: Mapped[str] = mapped_column(String(16), default="SCOPED")
    first_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    runtime_scope: Mapped[str | None] = mapped_column(String(200), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="FAILED")
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class GatewayRequest(Base):
    """调用日志：只保留元数据与测量值，不保存 Prompt / Authorization / 响应体。"""

    __tablename__ = "t_nexus_gateway_requests"
    __table_args__ = (
        Index("ix_gateway_requests_time", "started_at"),
        Index("ix_gateway_requests_key_time", "key_id", "started_at"),
        Index("ix_gateway_requests_node_time", "node_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    actual_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pool_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    streaming: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stream_interrupted: Mapped[bool] = mapped_column(Boolean, default=False)
    connect_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ttfb_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tps: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # 处理该请求的网关进程。instance_id 跨重启稳定，代次用心跳 started_at 区分。
    gateway_instance_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    gateway_instance_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # lost：处理实例的该代次已不在心跳上，结果未知。不填写 status_code / error_code。
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class GatewayRuntimeEvent(Base):
    """运行态变迁。配置审计仍在 gateway_audit_events，这里只记健康态等运行变化。"""

    __tablename__ = "t_nexus_gateway_runtime_events"
    __table_args__ = (Index("ix_gateway_runtime_node_time", "node_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), default="runtime", index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    pool_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    summary: Mapped[str] = mapped_column(String(300), default="")
    previous: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class GatewayAuditEvent(Base):
    __tablename__ = "t_nexus_gateway_audit_events"
    __table_args__ = (Index("ix_gateway_audit_resource", "resource_type", "resource_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(120), index=True)
    actor_role: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(32), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    result: Mapped[str] = mapped_column(String(16), default="SUCCESS", index=True)
    before: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)

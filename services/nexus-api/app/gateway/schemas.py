"""API 中转控制面请求 / 响应模型。响应只包含脱敏字段。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.gateway.models import AUTH_BEARER, CONFIG_DISABLED, SCHEDULING_DRAINING


class PoolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    enabled: bool = True
    sort_order: int = 0


class PoolPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    enabled: bool | None = None
    sort_order: int | None = None


class PoolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    enabled: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class NodeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    base_url: str = Field(min_length=1, max_length=512)
    pool_id: str | None = None
    api_key: str | None = None
    auth_type: str = AUTH_BEARER
    auth_header: str = Field(default="Authorization", min_length=1, max_length=64)
    connect_timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    request_timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    health_path: str = Field(default="", max_length=256)
    probe_strategy: str = Field(default="SSE", max_length=16)
    probe_interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    models: list[str] = Field(default_factory=list)
    weight: int = Field(default=1, ge=1)
    max_concurrency: int = Field(default=0, ge=0)
    hourly_token_limit: int | None = Field(default=None, ge=0)
    daily_token_limit: int | None = Field(default=None, ge=0)
    quota_policy: str = "STOP"
    # 新节点必须显式启用并探测通过后才能进入调度：默认安全态为 DISABLED/DRAINING，
    # 避免缺省 ACTIVE 绕过「显式 ACTIVE 创建需 HEALTHY」的限制。
    config_state: str = CONFIG_DISABLED
    scheduling_state: str = SCHEDULING_DRAINING
    sort_order: int = 0


class NodePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    pool_id: str | None = None
    api_key: str | None = None
    auth_type: str | None = None
    auth_header: str | None = Field(default=None, min_length=1, max_length=64)
    connect_timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    request_timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    health_path: str | None = Field(default=None, max_length=256)
    probe_strategy: str | None = Field(default=None, max_length=16)
    probe_interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    models: list[str] | None = None
    weight: int | None = Field(default=None, ge=1)
    max_concurrency: int | None = Field(default=None, ge=0)
    hourly_token_limit: int | None = Field(default=None, ge=0)
    daily_token_limit: int | None = Field(default=None, ge=0)
    quota_policy: str | None = None
    config_state: str | None = None
    scheduling_state: str | None = None
    sort_order: int | None = None


class NodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    pool_id: str | None
    base_url: str
    auth_type: str
    auth_header: str
    connect_timeout_seconds: float | None
    request_timeout_seconds: float | None
    health_path: str
    probe_strategy: str
    probe_interval_seconds: int | None
    models: list[str]
    weight: int
    max_concurrency: int
    hourly_token_limit: int | None
    daily_token_limit: int | None
    quota_policy: str
    config_state: str
    scheduling_state: str
    health_state: str
    success_streak: int
    failure_streak: int
    health_detail: dict[str, Any]
    has_api_key: bool
    sort_order: int
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RouteCreate(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    model_type: Literal["CHAT", "EMBEDDING", "ASR"] = "CHAT"
    pool_id: str
    target_model: str | None = Field(default=None, max_length=200)
    description: str = ""
    enabled: bool = True


class RoutePatch(BaseModel):
    model: str | None = Field(default=None, min_length=1, max_length=200)
    model_type: Literal["CHAT", "EMBEDDING", "ASR"] | None = None
    pool_id: str | None = None
    target_model: str | None = Field(default=None, max_length=200)
    description: str | None = None
    enabled: bool | None = None


class RouteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    model: str
    model_type: Literal["CHAT", "EMBEDDING", "ASR"]
    pool_id: str
    target_model: str | None = None
    description: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class SpeechInputConfig(BaseModel):
    route_id: str | None = Field(default=None, min_length=1, max_length=64)


class GatewayKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    owner_id: str | None = None
    allowed_models: list[str] = Field(default_factory=list)
    max_concurrency: int = Field(default=0, ge=0)
    hourly_token_limit: int | None = Field(default=None, ge=0)
    daily_token_limit: int | None = Field(default=None, ge=0)
    expires_at: datetime | None = None


class GatewayKeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    owner_id: str | None = None
    allowed_models: list[str] | None = None
    max_concurrency: int | None = Field(default=None, ge=0)
    hourly_token_limit: int | None = Field(default=None, ge=0)
    daily_token_limit: int | None = Field(default=None, ge=0)
    enabled: bool | None = None
    expires_at: datetime | None = None


class GatewayKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    key_prefix: str
    owner_id: str | None
    allowed_models: list[str]
    max_concurrency: int
    hourly_token_limit: int | None
    daily_token_limit: int | None
    enabled: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class GatewayKeyCreated(GatewayKeyRead):
    key: str


class ProbeRequest(BaseModel):
    probe_types: list[str] = Field(default_factory=list)


class HealthCheckRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    node_id: str
    probe_type: str
    success: bool
    latency_ms: int
    status_code: int | None
    error: str | None
    detail: dict[str, Any]
    created_at: datetime


class ProbeRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    node_id: str
    source: str
    strategy: str
    first_model: str | None
    runtime_scope: str | None
    success: bool
    complete: bool
    status: str
    steps: list[dict[str, Any]]
    usage: dict[str, Any]
    error: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int


class ProbeRunList(BaseModel):
    runs: list[ProbeRunRead]
    legacy_steps: list[HealthCheckRead]


class NodeProbeResult(BaseModel):
    node: NodeRead
    run: ProbeRunRead
    checks: list[HealthCheckRead]


class AuditEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor: str
    actor_role: str
    action: str
    resource_type: str
    resource_id: str | None
    result: str
    before: dict[str, Any]
    after: dict[str, Any]
    created_at: datetime


class RequestLogRead(BaseModel):
    """调用日志：元数据与测量值，绝不包含 Prompt / Authorization / 响应体。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    key_id: str | None
    owner_id: str | None
    model: str | None
    actual_model: str | None = None
    pool_id: str | None
    node_id: str | None
    streaming: bool
    retry_count: int
    status_code: int | None
    error_code: str | None
    stream_interrupted: bool
    connect_ms: int | None
    ttfb_ms: int | None
    ttft_ms: int | None
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    output_tps: float | None
    usage_known: bool
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None = None
    gateway_instance_id: str | None = None


class RequestLogPage(BaseModel):
    items: list[RequestLogRead]
    total: int
    page: int
    page_size: int


class LatencyStats(BaseModel):
    avg: float | None
    p50: float | None
    p95: float | None


class StatBucket(BaseModel):
    bucket_start: datetime
    requests: int
    success: int
    errors: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    unknown_usage_requests: int


class ScopeStats(BaseModel):
    scope: str
    scope_id: str | None
    window: str
    window_start: datetime
    window_end: datetime
    requests: int
    success: int
    errors: int
    success_rate: float | None
    in_progress: int = 0
    lost: int = 0
    legacy_open: int = 0
    input_tokens: int
    output_tokens: int
    total_tokens: int
    unknown_usage_requests: int
    qps: float
    active: int
    peak_concurrency: int
    connect_ms: LatencyStats
    ttfb_ms: LatencyStats
    ttft_ms: LatencyStats
    duration_ms: LatencyStats
    output_tps: LatencyStats
    trend: list[StatBucket]


class OverviewMetrics(BaseModel):
    """运行总览卡片与健康判断共用的窗口指标。缺失为 null，不用均值冒充 P95。"""

    qps: float | None = None
    qps_requests: int = 0
    error_rate: float | None = None
    error_samples: int = 0
    error_failures: int = 0
    error_rate_applies: bool = False
    retry_rate: float | None = None
    upstream_429_rate: float | None = None
    stream_interrupt_rate: float | None = None
    ttft_p95_ms: float | None = None
    output_tps_p95: float | None = None
    connect_p95_ms: float | None = None
    ttfb_p95_ms: float | None = None
    duration_p95_ms: float | None = None
    unknown_usage_share: float | None = None
    token_throughput: float | None = None
    known_total_tokens: int | None = None
    active: int | None = None
    capacity_limit: int | None = None
    capacity_occupancy: float | None = None
    peak_concurrency: int | None = None
    queue: int | None = None


class HealthWindow(BaseModel):
    qps_seconds: int
    error_seconds: int
    start: datetime
    end: datetime


class HealthImpact(BaseModel):
    pool_id: str
    pool_name: str
    model: str
    state: str
    text: str


class IgnoredPool(BaseModel):
    pool_id: str
    pool_name: str
    reason: str


class HealthSummary(BaseModel):
    status: str
    stale: bool
    reasons: list[str]
    impact: list[HealthImpact]
    impact_summary: str
    window: HealthWindow
    generated_at: datetime
    metrics: OverviewMetrics
    ignored_pools: list[IgnoredPool] = Field(default_factory=list)


class NodeWindowMetric(BaseModel):
    node_id: str
    name: str
    pool_id: str | None
    pool_name: str | None
    health: str
    health_label: str
    scheduling: str
    scheduling_label: str
    active: int | None
    capacity_limit: int | None
    occupancy: float | None
    queue: int | None = None
    qps: float | None
    error_rate: float | None
    error_samples: int
    ttft_p95_ms: float | None
    retry_rate: float | None
    abnormal: bool


class TrendPoint(BaseModel):
    bucket_start: datetime
    requests: int
    errors: int
    error_rate: float | None
    ttft_p95_ms: float | None
    output_tps_p95: float | None
    total_tokens: int


class TrendSeries(BaseModel):
    key: str
    label: str
    points: list[TrendPoint]


class TrendRead(BaseModel):
    window: str
    group_by: str
    window_start: datetime
    window_end: datetime
    bucket_seconds: int
    series: list[TrendSeries]
    ttft_p95_ms: float | None
    output_tps_p95: float | None


class OpsEventRead(BaseModel):
    id: str
    source: str
    kind: str
    summary: str
    resource_type: str | None = None
    resource_id: str | None = None
    node_id: str | None = None
    model: str | None = None
    pool_id: str | None = None
    key_id: str | None = None
    created_at: datetime


class GatewayInstanceRead(BaseModel):
    instance_id: str
    status: str
    app_version: str | None = None
    pid: int | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    uptime_seconds: int | None = None
    age_seconds: float | None = None
    requests: int | None = None
    errors: int | None = None
    active: int | None = None
    cpu_percent: float | None = None
    memory_bytes: int | None = None

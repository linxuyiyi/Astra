"""节点过滤与 least active 选择。

纯函数，便于单测。数据面先按配置态、调度态、健康态、熔断、模型支持、并发与
额度过滤，再在剩余候选中以 least active 为主、按权重与剩余额度调整有效权重，
最终由 Redis 原子租约决定谁能真正占用最后一个并发槽。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.gateway.config import NodeConfig
from app.gateway.models import (
    CONFIG_ENABLED,
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
    HEALTH_UNKNOWN,
    SCHEDULING_ACTIVE,
)
from app.gateway.runtime import (
    CIRCUIT_HALF_OPEN,
    CIRCUIT_HALF_OPEN_MAX_TRIALS,
    CIRCUIT_OPEN,
    SCOPE_NODE,
    RuntimeStore,
    TokenUsage,
    node_runtime_scope,
    usage_buckets,
)
from app.models import utcnow

# 不可调度原因，用于无节点时区分无路由、无健康节点、池满载与额度超限。
REASON_CONFIG_DISABLED = "CONFIG_DISABLED"
REASON_SCHEDULING = "SCHEDULING_BLOCKED"
REASON_HEALTH_UNKNOWN = "HEALTH_UNKNOWN"
REASON_HEALTH_CHECKING = "HEALTH_CHECKING"
REASON_HEALTH_UNHEALTHY = "HEALTH_UNHEALTHY"
REASON_CIRCUIT_OPEN = "CIRCUIT_OPEN"
REASON_MODEL_UNSUPPORTED = "MODEL_UNSUPPORTED"
REASON_NODE_FULL = "NODE_FULL"
REASON_QUOTA_EXCEEDED = "QUOTA_EXCEEDED"

QUOTA_OK = "OK"
QUOTA_WARN = "WARN"
QUOTA_THROTTLE = "THROTTLE"
QUOTA_STOP = "STOP"
QUOTA_RECORD = "RECORD"

# 90% 降权、100% 停止新请求；告警（ALERT）在 100% 时重度降权，仅记录（RECORD）不阻止。
_THROTTLE_FACTOR = {QUOTA_OK: 1.0, QUOTA_RECORD: 1.0, QUOTA_WARN: 0.6, QUOTA_THROTTLE: 0.1}
_DEGRADED_FACTOR = 0.5


def quota_state(used_total: int, limit: int | None, policy: str) -> str:
    if limit is None or limit <= 0:
        return QUOTA_OK
    ratio = used_total / limit
    if ratio >= 1:
        if policy == "STOP":
            return QUOTA_STOP
        if policy == "RECORD":
            return QUOTA_RECORD
        return QUOTA_THROTTLE  # ALERT：仍可服务但重度降权
    if ratio >= 0.9:
        return QUOTA_THROTTLE
    if ratio >= 0.8:
        return QUOTA_WARN
    return QUOTA_OK


def node_quota_state(node: NodeConfig, hourly: TokenUsage, daily: TokenUsage) -> str:
    hourly_state = quota_state(hourly.billable_total, node.hourly_token_limit, node.quota_policy)
    daily_state = quota_state(daily.billable_total, node.daily_token_limit, node.quota_policy)
    return _stricter(hourly_state, daily_state)


def node_block_reason(
    node: NodeConfig,
    *,
    model: str,
    active: int,
    circuit: str,
    hourly: TokenUsage,
    daily: TokenUsage,
    health_state: str | None = None,
    half_open_trials: int = 0,
) -> str | None:
    # 健康态只认运行态；调用方未提供时按 UNKNOWN 处理，绝不回退配置健康投影。
    health = health_state if health_state is not None else HEALTH_UNKNOWN
    if node.config_state != CONFIG_ENABLED:
        return REASON_CONFIG_DISABLED
    if node.scheduling_state != SCHEDULING_ACTIVE:
        return REASON_SCHEDULING
    if health == "UNKNOWN":
        return REASON_HEALTH_UNKNOWN
    if health == "CHECKING":
        return REASON_HEALTH_CHECKING
    if health not in (HEALTH_HEALTHY, HEALTH_DEGRADED):
        return REASON_HEALTH_UNHEALTHY
    if circuit == CIRCUIT_OPEN:
        return REASON_CIRCUIT_OPEN
    if circuit == CIRCUIT_HALF_OPEN and half_open_trials >= CIRCUIT_HALF_OPEN_MAX_TRIALS:
        return REASON_CIRCUIT_OPEN
    if model not in node.models:
        return REASON_MODEL_UNSUPPORTED
    if node.max_concurrency > 0 and active >= node.max_concurrency:
        return REASON_NODE_FULL
    if node_quota_state(node, hourly, daily) == QUOTA_STOP:
        return REASON_QUOTA_EXCEEDED
    return None


@dataclass(frozen=True)
class NodeSchedule:
    """一次调度读取：熔断和健康在连接作用域，并发和用量在节点 ID。"""

    scope: str
    active: int
    circuit: str
    half_open_trials: int
    health: str
    hourly: TokenUsage
    daily: TokenUsage


async def read_node_schedule(
    runtime: RuntimeStore,
    node: Any,
    *,
    timezone: str,
    now: datetime | None = None,
) -> NodeSchedule:
    """数据面挑选节点与运行总览共用的运行态读取。

    健康缺失即为 UNKNOWN，不回退配置表上的健康投影。调用方再用
    ``node_block_reason`` 判断能否承接流量。
    """

    buckets = usage_buckets(now or utcnow(), timezone)
    scope = node_runtime_scope(node)
    snapshot = await runtime.circuit_snapshot(scope)
    return NodeSchedule(
        scope=scope,
        active=int(await runtime.active(SCOPE_NODE, node.id)),
        circuit=snapshot.state,
        half_open_trials=snapshot.trials,
        health=await runtime.health(scope) or HEALTH_UNKNOWN,
        hourly=await runtime.usage(SCOPE_NODE, node.id, "hour", buckets["hour"]),
        daily=await runtime.usage(SCOPE_NODE, node.id, "day", buckets["day"]),
    )


def effective_weight(
    node: NodeConfig, hourly: TokenUsage, daily: TokenUsage, *, health_state: str | None = None
) -> float:
    base = float(node.weight) if node.weight and node.weight > 0 else 1.0
    state = node_quota_state(node, hourly, daily)
    factor = _THROTTLE_FACTOR.get(state, 1.0)
    if (health_state if health_state is not None else HEALTH_UNKNOWN) == HEALTH_DEGRADED:
        factor *= _DEGRADED_FACTOR
    return max(base * factor, 0.001)


@dataclass
class Candidate:
    node: NodeConfig
    active: int
    weight: float

    @property
    def score(self) -> float:
        # least active 为主：并发越低越优先；权重越高越优先。
        return (self.active + 1) / self.weight


def order_candidates(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda item: (item.score, item.node.id))


def _stricter(first: str, second: str) -> str:
    severity = {
        QUOTA_OK: 0,
        QUOTA_RECORD: 1,
        QUOTA_WARN: 2,
        QUOTA_THROTTLE: 3,
        QUOTA_STOP: 4,
    }
    return first if severity[first] >= severity[second] else second
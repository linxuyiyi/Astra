"""数据面跨实例运行态：并发租约、周期 Token、节点健康、熔断与实例心跳。

Redis 是唯一事实来源：并发租约、周期配额、节点健康、熔断与心跳都放在 Redis，
进程崩溃后由各自 TTL 回收。绝不退化为进程内计数，否则多实例会超卖。所有租约
获取 / 释放 / 续租 / 剪枝、健康连击与熔断状态转移都用 Lua 脚本原子执行。

并发租约不再使用「单计数键 + 单 TTL」：每个请求持有唯一 lease_id，写入按 scope
隔离的 ZSET（member=lease_id，score=到期毫秒）。Lua 在获取时先剪枝过期租约、
再做限额检查，因此多实例不会超卖；长流通过 :meth:`RuntimeStore.renew` 周期续租，
实例崩溃后每个租约按各自 TTL 独立回收，互不影响。

测试与嵌入式场景使用 :class:`InMemoryRuntimeStore`，语义与 Redis 实现一致（在
单事件循环内无 await 交错，因此同样原子）。真实 Redis 的 Lua 只有在提供
``ASTRA_TEST_REDIS_URL`` 时才联调，默认测试只验证进程内等价语义。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

CIRCUIT_CLOSED = "CLOSED"
CIRCUIT_OPEN = "OPEN"
CIRCUIT_HALF_OPEN = "HALF_OPEN"

SCOPE_NODE = "node"
SCOPE_KEY = "key"

# 节点健康态：与持久化模型保持同一组取值。
HEALTH_UNKNOWN = "UNKNOWN"
HEALTH_CHECKING = "CHECKING"
HEALTH_HEALTHY = "HEALTHY"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_UNHEALTHY = "UNHEALTHY"

# 连续失败 3 次摘除，连续成功 2 次恢复；熔断半开只放行少量试探。
PROBE_FAILURE_THRESHOLD = 3
PROBE_SUCCESS_THRESHOLD = 2
CIRCUIT_HALF_OPEN_MAX_TRIALS = 1

# 租约 ZSET、周期用量、节点健康、熔断与实例心跳键。
LEASE_KEY = "nexus:gateway:lease:{scope}:{scope_id}"
USAGE_KEY = "nexus:gateway:usage:{scope}:{scope_id}:{period}:{bucket}"
HEALTH_KEY = "nexus:gateway:health:{node_id}"
CIRCUIT_KEY = "nexus:gateway:circuit:{node_id}"
INSTANCE_KEY = "nexus:gateway:instance:{instance_id}"
# 实例注册表比心跳保留更久，用于展示已停止心跳的实例（区分 ACTIVE/EXPIRED），
# 避免崩溃实例从集合里直接消失。
INSTANCE_REGISTRY_KEY = "nexus:gateway:instance_registry:{instance_id}"
INSTANCES_SET = "nexus:gateway:instances"
INSTANCE_REGISTRY_TTL_SECONDS = 7 * 86400

# 已吊销 Gateway Key 摘要的共享阻断集合：控制面先追加写入，数据面据此在读取请求体
# 与调度之前阻断，无需等待配置刷新。集合只增不减（吊销不可逆），且没有 TTL——否则
# Redis 清空或重启后旧缓存配置里的 Key 会复活。就绪状态是同集合内的一个哨兵成员，
# 而不是独立键：Redis 按 maxmemory 淘汰只能整体删除这个集合键，哨兵与摘要一起消失，
# 因此不会出现「标记还在、集合已被淘汰」而错误放行。哨兵用不会与 sha256 摘要冲突的
# ``__ready__``：数据面用一次原子读取先查哨兵再查摘要，哨兵缺失即 fail closed 拒绝
# 新请求，等待启动/后台周期重新同步。已吊销 Gateway Key 的 DB 行是永久 tombstone，
# 普通编辑不得删除其吊销状态或重新放行它，否则 Redis 清空后无法从 DB 重建永久吊销。
REVOKED_KEYS_KEY = "nexus:gateway:revoked_keys"
REVOKED_READY_MEMBER = "__ready__"

# 周期用量 TTL，避免崩溃后残留永久占用；健康键 TTL 需长于最大探测间隔。
USAGE_HOUR_TTL_SECONDS = 3 * 3600
USAGE_DAY_TTL_SECONDS = 3 * 86400
HEALTH_TTL_SECONDS = 3 * 86400
# 半开观察窗：OPEN 到期后进入 HALF_OPEN，超过该窗口未结算则回到 CLOSED。
HALF_OPEN_TTL_SECONDS = 60

_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
local limit = tonumber(ARGV[2])
if limit > 0 and count >= limit then
  return -1
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
redis.call('PEXPIRE', KEYS[1], ARGV[5])
return count + 1
"""

_RELEASE_LUA = """
local removed = redis.call('ZREM', KEYS[1], ARGV[1])
if redis.call('ZCARD', KEYS[1]) == 0 then
  redis.call('DEL', KEYS[1])
end
return removed
"""

_RENEW_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if not redis.call('ZSCORE', KEYS[1], ARGV[2]) then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[2])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return 1
"""

_ACTIVE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
if count == 0 then
  redis.call('DEL', KEYS[1])
end
return count
"""

_RECORD_PROBE_LUA = """
local success = ARGV[1] == '1'
local state = redis.call('HGET', KEYS[1], 'state') or 'UNKNOWN'
local ss = tonumber(redis.call('HGET', KEYS[1], 'success_streak') or '0')
local fs = tonumber(redis.call('HGET', KEYS[1], 'failure_streak') or '0')
if success then
  ss = ss + 1
  fs = 0
  if ss >= tonumber(ARGV[3]) then state = 'HEALTHY' end
else
  fs = fs + 1
  ss = 0
  if fs >= tonumber(ARGV[2]) then state = 'UNHEALTHY' end
end
redis.call('HSET', KEYS[1],
  'state', state,
  'success_streak', ss,
  'failure_streak', fs,
  'last_checked_at', ARGV[5],
  'detail', ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[6])
return state
"""

_MARK_DEGRADED_LUA = """
local state = redis.call('HGET', KEYS[1], 'state')
if state == 'HEALTHY' or state == 'DEGRADED' then
  redis.call('HSET', KEYS[1], 'state', 'DEGRADED', 'last_checked_at', ARGV[1])
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return 'DEGRADED'
end
return state or 'UNKNOWN'
"""

_CIRCUIT_EVAL_LUA = """
local state = redis.call('HGET', KEYS[1], 'state')
if not state then return 'CLOSED' end
if state == 'OPEN' then
  local until_ms = tonumber(redis.call('HGET', KEYS[1], 'until') or '0')
  if tonumber(ARGV[1]) < until_ms then return 'OPEN' end
  redis.call('HSET', KEYS[1], 'state', 'HALF_OPEN', 'trials', 0)
  redis.call('HDEL', KEYS[1], 'until')
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 'HALF_OPEN'
end
return state
"""

# 与 circuit_eval 同一套 OPEN→HALF_OPEN 到期规则，额外返回已占用试探次数，不增加试探。
# 返回 {state, trials}。
_CIRCUIT_SNAPSHOT_LUA = """
local state = redis.call('HGET', KEYS[1], 'state')
if not state then return {'CLOSED', '0'} end
if state == 'OPEN' then
  local until_ms = tonumber(redis.call('HGET', KEYS[1], 'until') or '0')
  if tonumber(ARGV[1]) < until_ms then
    return {'OPEN', redis.call('HGET', KEYS[1], 'trials') or '0'}
  end
  redis.call('HSET', KEYS[1], 'state', 'HALF_OPEN', 'trials', 0)
  redis.call('HDEL', KEYS[1], 'until')
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return {'HALF_OPEN', '0'}
end
return {state, redis.call('HGET', KEYS[1], 'trials') or '0'}
"""

# 原子打开：仍在 OPEN 窗口内只刷新截止时间，changed=0；否则写入 OPEN 并返回变化前状态。
# 返回 {changed, previous, current}。
_TRANSITION_OPEN_LUA = """
local now = tonumber(ARGV[1])
local until_ms = tonumber(ARGV[2])
local ttl_ms = ARGV[3]
local state = redis.call('HGET', KEYS[1], 'state')
local previous = 'CLOSED'
if state == 'OPEN' then
  local existing = tonumber(redis.call('HGET', KEYS[1], 'until') or '0')
  if now < existing then
    redis.call('HSET', KEYS[1], 'until', until_ms, 'trials', 0)
    redis.call('PEXPIRE', KEYS[1], ttl_ms)
    return {0, 'OPEN', 'OPEN'}
  end
  previous = 'HALF_OPEN'
elseif state then
  previous = state
end
redis.call('HSET', KEYS[1], 'state', 'OPEN', 'until', until_ms, 'trials', 0)
redis.call('PEXPIRE', KEYS[1], ttl_ms)
return {1, previous, 'OPEN'}
"""

# 原子关闭：缺失或已是 CLOSED 时 changed=0。到期的 OPEN 按 HALF_OPEN 报告变化前状态。
# 返回 {changed, previous, current}。
_TRANSITION_CLOSE_LUA = """
local state = redis.call('HGET', KEYS[1], 'state')
if not state or state == 'CLOSED' then
  if state then redis.call('DEL', KEYS[1]) end
  return {0, 'CLOSED', 'CLOSED'}
end
local previous = state
if state == 'OPEN' then
  local until_ms = tonumber(redis.call('HGET', KEYS[1], 'until') or '0')
  if tonumber(ARGV[1]) >= until_ms then previous = 'HALF_OPEN' end
end
redis.call('DEL', KEYS[1])
return {1, previous, 'CLOSED'}
"""

_SET_CIRCUIT_OPEN_LUA = """
redis.call('HSET', KEYS[1], 'state', 'OPEN', 'until', ARGV[1], 'trials', 0)
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 'OPEN'
"""

_TRY_TRIAL_LUA = """
local state = redis.call('HGET', KEYS[1], 'state')
if not state then return 0 end
if state == 'OPEN' then
  local until_ms = tonumber(redis.call('HGET', KEYS[1], 'until') or '0')
  if tonumber(ARGV[1]) < until_ms then return 0 end
  redis.call('HSET', KEYS[1], 'state', 'HALF_OPEN', 'trials', 0)
  redis.call('HDEL', KEYS[1], 'until')
end
local trials = tonumber(redis.call('HGET', KEYS[1], 'trials') or '0')
if trials >= tonumber(ARGV[2]) then return 0 end
redis.call('HINCRBY', KEYS[1], 'trials', 1)
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return 1
"""

# 就绪哨兵与摘要都在同一个集合键里，必须在同一次 Lua 读取里判断：分开两次调用时
# Redis 恰好在两次之间淘汰或清空整个集合键，会先看到哨兵为真、再看到集合为空而错误
# 放行。先查哨兵，缺失时返回 -1，调用方据此 fail closed；再查摘要。
# KEYS[1]=阻断集合，ARGV[1]=摘要，ARGV[2]=就绪哨兵。
_IS_REVOKED_LUA = """
if redis.call('SISMEMBER', KEYS[1], ARGV[2]) == 0 then
  return -1
end
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then
  return 1
end
return 0
"""


@dataclass(frozen=True)
class CircuitSnapshot:
    """熔断读快照。trials 是半开已占用的试探次数，读取本身不占用名额。"""

    state: str
    trials: int


@dataclass(frozen=True)
class CircuitTransition:
    """一次原子熔断转移。只有 changed 为真的调用方可以记事件。"""

    changed: bool
    previous: str
    current: str


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0
    unknown_requests: int = 0

    @property
    def known(self) -> bool:
        return self.unknown_requests == 0

    @property
    def billable_total(self) -> int:
        return self.total_tokens


def usage_buckets(now: datetime, timezone: str) -> dict[str, str]:
    """按 ``Asia/Shanghai`` 自然小时与自然日生成分桶键。"""

    local = now.astimezone(ZoneInfo(timezone))
    return {"hour": local.strftime("%Y%m%d%H"), "day": local.strftime("%Y%m%d")}


def lease_key(scope: str, scope_id: str) -> str:
    return LEASE_KEY.format(scope=scope, scope_id=scope_id)


def usage_key(scope: str, scope_id: str, period: str, bucket: str) -> str:
    return USAGE_KEY.format(scope=scope, scope_id=scope_id, period=period, bucket=bucket)


def health_key(node_id: str) -> str:
    return HEALTH_KEY.format(node_id=node_id)


def circuit_key(node_id: str) -> str:
    return CIRCUIT_KEY.format(node_id=node_id)


# 运行态健康/熔断作用域分隔符：作用域 ID 由「节点 ID + 连接指纹」组成。
NODE_SCOPE_SEPARATOR = "#"


def node_scope_id(
    node_id: str,
    *,
    base_url: str = "",
    api_key_encrypted: str | None = None,
    auth_type: str = "",
    auth_header: str = "",
    health_path: str = "",
    models: Iterable[str] | None = None,
) -> str:
    """按节点连接参数生成稳定作用域 ID，作为健康/熔断键后缀。

    任何改变下游可达性或认证的字段（地址、凭据、认证方式、健康路径、支持模型）
    变化都会产生不同作用域，因此：

    - 连接编辑产生新作用域，不会清空或改写旧连接的健康/熔断；
    - 旧实例对旧连接的探测或请求结果落在旧作用域，无法把新配置判为 HEALTHY；
    - 相同连接复用同一作用域，重启后仍能读到有效运行态。

    连接未变化时无需重新探测。
    """

    fingerprint = {
        "base_url": str(base_url or ""),
        "api_key_encrypted": api_key_encrypted or "",
        "auth_type": str(auth_type or ""),
        "auth_header": str(auth_header or ""),
        "health_path": str(health_path or ""),
        "models": sorted(str(model) for model in (models or ())),
    }
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return f"{node_id}{NODE_SCOPE_SEPARATOR}{digest}"


def node_runtime_scope(node: Any) -> str:
    """对 ``NodeConfig``（内存视图）或 ``GatewayNode``（配置表）计算同一连接作用域。"""

    return node_scope_id(
        node.id,
        base_url=getattr(node, "base_url", ""),
        api_key_encrypted=getattr(node, "api_key_encrypted", None),
        auth_type=getattr(node, "auth_type", ""),
        auth_header=getattr(node, "auth_header", ""),
        health_path=getattr(node, "health_path", ""),
        models=getattr(node, "models", None),
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def open_deadline_ms(open_seconds: int, now_ms: int | None = None) -> tuple[int, int]:
    """返回 (until_ms, ttl_ms)。0 秒表示窗口已经到期，键仍保留到半开观察窗。"""

    now = _now_ms() if now_ms is None else int(now_ms)
    if int(open_seconds) <= 0:
        return now, HALF_OPEN_TTL_SECONDS * 1000
    span = int(open_seconds) * 1000
    return now + span, span


def _snapshot_from(raw: Any) -> CircuitSnapshot:
    state, trials = raw
    return CircuitSnapshot(state=str(state), trials=int(trials))


def _transition_from(raw: Any) -> CircuitTransition:
    changed, previous, current = raw
    return CircuitTransition(changed=bool(int(changed)), previous=str(previous), current=str(current))


class RuntimeStore(Protocol):
    async def healthy(self) -> bool: ...

    async def acquire(self, scope: str, scope_id: str, limit: int, ttl_seconds: int) -> str | None: ...

    async def release(self, scope: str, scope_id: str, lease_id: str) -> None: ...

    async def renew(self, scope: str, scope_id: str, lease_id: str, ttl_seconds: int) -> bool: ...

    async def active(self, scope: str, scope_id: str) -> int: ...

    async def record_usage(
        self,
        scope: str,
        scope_id: str,
        period: str,
        bucket: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        known: bool,
    ) -> None: ...

    async def usage(self, scope: str, scope_id: str, period: str, bucket: str) -> TokenUsage: ...

    async def record_probe(
        self,
        node_id: str,
        *,
        success: bool,
        detail: dict[str, Any] | None = None,
    ) -> str: ...

    async def health(self, node_id: str) -> str | None: ...

    async def seed_health(self, node_id: str, state: str) -> None: ...

    async def reset_health(self, node_id: str) -> None: ...

    async def mark_degraded(self, node_id: str) -> None: ...

    async def circuit(self, node_id: str) -> str: ...

    async def circuit_snapshot(self, node_id: str) -> CircuitSnapshot: ...

    async def set_circuit_open(self, node_id: str, open_seconds: int) -> None: ...

    async def set_circuit_closed(self, node_id: str) -> None: ...

    async def transition_circuit_open(self, node_id: str, open_seconds: int) -> CircuitTransition: ...

    async def transition_circuit_closed(self, node_id: str) -> CircuitTransition: ...

    async def try_circuit_trial(
        self, node_id: str, max_trials: int = CIRCUIT_HALF_OPEN_MAX_TRIALS
    ) -> bool: ...

    async def heartbeat(self, instance_id: str, payload: dict[str, Any], ttl_seconds: int) -> None: ...

    async def retire(self, instance_id: str) -> None: ...

    async def list_instances(self) -> list[dict[str, Any]]: ...

    async def revoke_key_digest(self, digest: str) -> None: ...

    async def sync_revoked_key_digests(self, digests: Iterable[str]) -> None: ...

    async def is_key_revoked(self, digest: str) -> bool: ...

    async def revocation_state_ready(self) -> bool: ...

    async def close(self) -> None: ...


class RedisRuntimeStore:
    """基于 Redis 的运行态实现，租约、健康与熔断变更全部原子执行。"""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: Any | None = None
        self._scripts: dict[str, Any] = {}

    def _connection(self) -> Any:
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(
                self._redis_url,
                socket_connect_timeout=3,
                socket_timeout=3,
                decode_responses=True,
            )
            for name, script in (
                ("acquire", _ACQUIRE_LUA),
                ("release", _RELEASE_LUA),
                ("renew", _RENEW_LUA),
                ("active", _ACTIVE_LUA),
                ("record_probe", _RECORD_PROBE_LUA),
                ("mark_degraded", _MARK_DEGRADED_LUA),
                ("circuit_eval", _CIRCUIT_EVAL_LUA),
                ("circuit_snapshot", _CIRCUIT_SNAPSHOT_LUA),
                ("circuit_open", _SET_CIRCUIT_OPEN_LUA),
                ("transition_open", _TRANSITION_OPEN_LUA),
                ("transition_close", _TRANSITION_CLOSE_LUA),
                ("circuit_trial", _TRY_TRIAL_LUA),
                ("is_revoked", _IS_REVOKED_LUA),
            ):
                self._scripts[name] = self._client.register_script(script)
        return self._client

    async def healthy(self) -> bool:
        try:
            client = self._connection()
            return bool(await client.ping())
        except Exception:  # noqa: BLE001 - 运行态不可达统一视为不健康
            return False

    # ── 并发租约 ───────────────────────────────────────────────────────

    async def acquire(self, scope: str, scope_id: str, limit: int, ttl_seconds: int) -> str | None:
        self._connection()
        lease_id = uuid4().hex
        now_ms = _now_ms()
        ttl_ms = max(1, int(ttl_seconds)) * 1000
        result = await self._scripts["acquire"](
            keys=[lease_key(scope, scope_id)],
            args=[now_ms, int(limit), now_ms + ttl_ms, lease_id, ttl_ms],
        )
        return None if int(result) < 0 else lease_id

    async def release(self, scope: str, scope_id: str, lease_id: str) -> None:
        self._connection()
        await self._scripts["release"](keys=[lease_key(scope, scope_id)], args=[lease_id])

    async def renew(self, scope: str, scope_id: str, lease_id: str, ttl_seconds: int) -> bool:
        self._connection()
        now_ms = _now_ms()
        ttl_ms = max(1, int(ttl_seconds)) * 1000
        result = await self._scripts["renew"](
            keys=[lease_key(scope, scope_id)],
            args=[now_ms, lease_id, now_ms + ttl_ms, ttl_ms],
        )
        return bool(int(result))

    async def active(self, scope: str, scope_id: str) -> int:
        self._connection()
        result = await self._scripts["active"](
            keys=[lease_key(scope, scope_id)], args=[_now_ms()]
        )
        return int(result)

    # ── 周期用量 ───────────────────────────────────────────────────────

    async def record_usage(
        self,
        scope: str,
        scope_id: str,
        period: str,
        bucket: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        known: bool,
    ) -> None:
        client = self._connection()
        key = usage_key(scope, scope_id, period, bucket)
        ttl = USAGE_HOUR_TTL_SECONDS if period == "hour" else USAGE_DAY_TTL_SECONDS
        pipe = client.pipeline()
        pipe.hincrby(key, "input", int(input_tokens))
        pipe.hincrby(key, "output", int(output_tokens))
        pipe.hincrby(key, "total", int(total_tokens))
        pipe.hincrby(key, "requests", 1)
        if not known:
            pipe.hincrby(key, "unknown", 1)
        pipe.expire(key, ttl)
        await pipe.execute()

    async def usage(self, scope: str, scope_id: str, period: str, bucket: str) -> TokenUsage:
        client = self._connection()
        raw = await client.hgetall(usage_key(scope, scope_id, period, bucket))
        return _usage_from_mapping(raw)

    # ── 健康与熔断 ─────────────────────────────────────────────────────

    async def record_probe(
        self,
        node_id: str,
        *,
        success: bool,
        detail: dict[str, Any] | None = None,
    ) -> str:
        self._connection()
        result = await self._scripts["record_probe"](
            keys=[health_key(node_id)],
            args=[
                "1" if success else "0",
                PROBE_FAILURE_THRESHOLD,
                PROBE_SUCCESS_THRESHOLD,
                json.dumps(detail or {}, ensure_ascii=False),
                datetime.now().isoformat(),
                HEALTH_TTL_SECONDS,
            ],
        )
        return str(result)

    async def health(self, node_id: str) -> str | None:
        client = self._connection()
        value = await client.hget(health_key(node_id), "state")
        return str(value) if value else None

    async def seed_health(self, node_id: str, state: str) -> None:
        client = self._connection()
        key = health_key(node_id)
        created = await client.hsetnx(key, "state", state)
        if created:
            await client.hset(key, mapping={"success_streak": 0, "failure_streak": 0})
            await client.expire(key, HEALTH_TTL_SECONDS)

    async def reset_health(self, node_id: str) -> None:
        """清空运行态健康与熔断，使旧健康状态不再授权新配置调度。"""

        client = self._connection()
        await client.delete(health_key(node_id), circuit_key(node_id))

    async def mark_degraded(self, node_id: str) -> None:
        self._connection()
        await self._scripts["mark_degraded"](
            keys=[health_key(node_id)],
            args=[datetime.now().isoformat(), HEALTH_TTL_SECONDS],
        )

    async def circuit_snapshot(self, node_id: str) -> CircuitSnapshot:
        self._connection()
        result = await self._scripts["circuit_snapshot"](
            keys=[circuit_key(node_id)], args=[_now_ms(), HALF_OPEN_TTL_SECONDS * 1000]
        )
        return _snapshot_from(result)

    async def circuit(self, node_id: str) -> str:
        return (await self.circuit_snapshot(node_id)).state

    async def set_circuit_open(self, node_id: str, open_seconds: int) -> None:
        self._connection()
        until_ms, ttl_ms = open_deadline_ms(open_seconds)
        await self._scripts["circuit_open"](keys=[circuit_key(node_id)], args=[until_ms, ttl_ms])

    async def set_circuit_closed(self, node_id: str) -> None:
        client = self._connection()
        await client.delete(circuit_key(node_id))

    async def transition_circuit_open(self, node_id: str, open_seconds: int) -> CircuitTransition:
        self._connection()
        until_ms, ttl_ms = open_deadline_ms(open_seconds)
        result = await self._scripts["transition_open"](
            keys=[circuit_key(node_id)], args=[_now_ms(), until_ms, ttl_ms]
        )
        return _transition_from(result)

    async def transition_circuit_closed(self, node_id: str) -> CircuitTransition:
        self._connection()
        result = await self._scripts["transition_close"](
            keys=[circuit_key(node_id)], args=[_now_ms()]
        )
        return _transition_from(result)

    async def try_circuit_trial(
        self, node_id: str, max_trials: int = CIRCUIT_HALF_OPEN_MAX_TRIALS
    ) -> bool:
        self._connection()
        result = await self._scripts["circuit_trial"](
            keys=[circuit_key(node_id)],
            args=[_now_ms(), int(max_trials), HALF_OPEN_TTL_SECONDS * 1000],
        )
        return bool(int(result))

    # ── 实例心跳 ───────────────────────────────────────────────────────

    async def heartbeat(self, instance_id: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        client = self._connection()
        body = json.dumps(payload, ensure_ascii=False)
        pipe = client.pipeline()
        pipe.set(INSTANCE_KEY.format(instance_id=instance_id), body, ex=max(1, int(ttl_seconds)))
        pipe.set(
            INSTANCE_REGISTRY_KEY.format(instance_id=instance_id),
            body,
            ex=INSTANCE_REGISTRY_TTL_SECONDS,
        )
        pipe.sadd(INSTANCES_SET, instance_id)
        await pipe.execute()

    async def retire(self, instance_id: str) -> None:
        client = self._connection()
        pipe = client.pipeline()
        pipe.delete(INSTANCE_KEY.format(instance_id=instance_id))
        pipe.delete(INSTANCE_REGISTRY_KEY.format(instance_id=instance_id))
        pipe.srem(INSTANCES_SET, instance_id)
        await pipe.execute()

    async def list_instances(self) -> list[dict[str, Any]]:
        client = self._connection()
        instance_ids = sorted(await client.smembers(INSTANCES_SET))
        result: list[dict[str, Any]] = []
        stale: list[str] = []
        for instance_id in instance_ids:
            live = await client.get(INSTANCE_KEY.format(instance_id=instance_id))
            if live is not None:
                result.append(
                    {
                        "instance_id": instance_id,
                        "status": "ACTIVE",
                        "heartbeat": json.loads(live),
                    }
                )
                continue
            registry = await client.get(INSTANCE_REGISTRY_KEY.format(instance_id=instance_id))
            if registry is not None:
                result.append(
                    {
                        "instance_id": instance_id,
                        "status": "EXPIRED",
                        "heartbeat": json.loads(registry),
                    }
                )
            else:
                stale.append(instance_id)
        if stale:
            await client.srem(INSTANCES_SET, *stale)
        return result

    # ── 已吊销 Key 摘要 ────────────────────────────────────────────────

    async def revoke_key_digest(self, digest: str) -> None:
        """只追加摘要，不单独写就绪哨兵：集合被淘汰后仍须等 DB 同步才恢复放行判断。"""

        client = self._connection()
        await client.sadd(REVOKED_KEYS_KEY, str(digest))

    async def sync_revoked_key_digests(self, digests: Iterable[str]) -> None:
        """用一次 ``SADD`` 追加数据库摘要并把就绪哨兵加入同一集合。

        只做 ``SADD``，绝不 ``DEL`` 或整表替换，因此不会清空已有或并发写入的吊销；
        哨兵与摘要落在同一集合键，Redis 淘汰整个键时两者一起消失，不会留下假就绪。
        """

        client = self._connection()
        members = [str(digest) for digest in digests]
        members.append(REVOKED_READY_MEMBER)
        await client.sadd(REVOKED_KEYS_KEY, *members)

    async def is_key_revoked(self, digest: str) -> bool:
        """原子判断就绪哨兵与摘要；哨兵缺失时抛运行态错误（fail closed）。

        哨兵与摘要在同一集合键、同一次 Lua 执行中读取，因此不可能出现「哨兵为真、
        集合却被淘汰」的中间态而错误放行。调用方无需（也不应）先单独查询就绪状态。
        """

        self._connection()
        result = int(
            await self._scripts["is_revoked"](
                keys=[REVOKED_KEYS_KEY], args=[str(digest), REVOKED_READY_MEMBER]
            )
        )
        if result < 0:
            raise RuntimeError("吊销阻断状态尚未就绪（Redis 已清空或未完成同步）")
        return result == 1

    async def revocation_state_ready(self) -> bool:
        """仅用于诊断/测试：判断同一集合里的就绪哨兵是否存在，鉴权路径不应分步依赖它。"""

        client = self._connection()
        return bool(await client.sismember(REVOKED_KEYS_KEY, REVOKED_READY_MEMBER))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._scripts = {}


class InMemoryRuntimeStore:
    """进程内运行态，语义与 Redis 实现一致，供测试与嵌入式场景使用。"""

    def __init__(self, *, available: bool = True) -> None:
        self._leases: dict[str, dict[str, float]] = {}
        self._usage: dict[str, dict[str, int]] = {}
        self._health: dict[str, dict[str, Any]] = {}
        self._circuits: dict[str, dict[str, Any]] = {}
        self._instances: dict[str, dict[str, Any]] = {}
        self._instance_expiry: dict[str, float] = {}
        self._instance_registry: dict[str, dict[str, Any]] = {}
        self._instance_registry_expiry: dict[str, float] = {}
        # 就绪哨兵与摘要同在一个集合：嵌入式/测试场景默认已就绪（哨兵已入集合）；
        # 生产 Redis 实现必须由启动或后台从数据库同步后才写入哨兵，缺失时 fail closed。
        self._revoked: set[str] = {REVOKED_READY_MEMBER}
        self.available = available

    def set_available(self, available: bool) -> None:
        self.available = available

    async def healthy(self) -> bool:
        return self.available

    # ── 并发租约 ───────────────────────────────────────────────────────

    def _prune(self, key: str) -> dict[str, float]:
        entries = self._leases.get(key, {})
        now = time.monotonic()
        alive = {lease_id: expiry for lease_id, expiry in entries.items() if expiry > now}
        if alive:
            self._leases[key] = alive
        else:
            self._leases.pop(key, None)
        return alive

    async def acquire(self, scope: str, scope_id: str, limit: int, ttl_seconds: int) -> str | None:
        key = lease_key(scope, scope_id)
        alive = self._prune(key)
        if limit > 0 and len(alive) >= limit:
            return None
        lease_id = uuid4().hex
        alive[lease_id] = time.monotonic() + max(1, ttl_seconds)
        self._leases[key] = alive
        return lease_id

    async def release(self, scope: str, scope_id: str, lease_id: str) -> None:
        key = lease_key(scope, scope_id)
        alive = self._prune(key)
        alive.pop(lease_id, None)
        if not alive:
            self._leases.pop(key, None)

    async def renew(self, scope: str, scope_id: str, lease_id: str, ttl_seconds: int) -> bool:
        key = lease_key(scope, scope_id)
        alive = self._prune(key)
        if lease_id not in alive:
            return False
        alive[lease_id] = time.monotonic() + max(1, ttl_seconds)
        self._leases[key] = alive
        return True

    async def active(self, scope: str, scope_id: str) -> int:
        return len(self._prune(lease_key(scope, scope_id)))

    # ── 周期用量 ───────────────────────────────────────────────────────

    async def record_usage(
        self,
        scope: str,
        scope_id: str,
        period: str,
        bucket: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        known: bool,
    ) -> None:
        key = usage_key(scope, scope_id, period, bucket)
        entry = self._usage.setdefault(
            key,
            {"input": 0, "output": 0, "total": 0, "requests": 0, "unknown": 0},
        )
        entry["input"] += int(input_tokens)
        entry["output"] += int(output_tokens)
        entry["total"] += int(total_tokens)
        entry["requests"] += 1
        if not known:
            entry["unknown"] += 1

    async def usage(self, scope: str, scope_id: str, period: str, bucket: str) -> TokenUsage:
        key = usage_key(scope, scope_id, period, bucket)
        return _usage_from_mapping(self._usage.get(key, {}))

    # ── 健康与熔断 ─────────────────────────────────────────────────────

    async def record_probe(
        self,
        node_id: str,
        *,
        success: bool,
        detail: dict[str, Any] | None = None,
    ) -> str:
        entry = self._health.setdefault(
            node_id,
            {
                "state": HEALTH_UNKNOWN,
                "success_streak": 0,
                "failure_streak": 0,
                "last_checked_at": None,
                "detail": {},
            },
        )
        if success:
            entry["success_streak"] += 1
            entry["failure_streak"] = 0
            if entry["success_streak"] >= PROBE_SUCCESS_THRESHOLD:
                entry["state"] = HEALTH_HEALTHY
        else:
            entry["failure_streak"] += 1
            entry["success_streak"] = 0
            if entry["failure_streak"] >= PROBE_FAILURE_THRESHOLD:
                entry["state"] = HEALTH_UNHEALTHY
        entry["last_checked_at"] = datetime.now().isoformat()
        entry["detail"] = dict(detail or {})
        return str(entry["state"])

    async def health(self, node_id: str) -> str | None:
        entry = self._health.get(node_id)
        return str(entry["state"]) if entry else None

    async def seed_health(self, node_id: str, state: str) -> None:
        self._health.setdefault(
            node_id,
            {
                "state": state,
                "success_streak": 0,
                "failure_streak": 0,
                "last_checked_at": None,
                "detail": {},
            },
        )

    async def reset_health(self, node_id: str) -> None:
        self._health.pop(node_id, None)
        self._circuits.pop(node_id, None)

    async def mark_degraded(self, node_id: str) -> None:
        entry = self._health.get(node_id)
        if entry and entry.get("state") in (HEALTH_HEALTHY, HEALTH_DEGRADED):
            entry["state"] = HEALTH_DEGRADED
            entry["last_checked_at"] = datetime.now().isoformat()

    async def _eval_circuit(self, node_id: str) -> tuple[str, dict[str, Any] | None]:
        entry = self._circuits.get(node_id)
        if entry is None:
            return CIRCUIT_CLOSED, None
        if entry.get("state") == CIRCUIT_OPEN:
            if time.monotonic() < float(entry.get("until", 0.0)):
                return CIRCUIT_OPEN, entry
            entry = {"state": CIRCUIT_HALF_OPEN, "trials": 0}
            self._circuits[node_id] = entry
        return str(entry.get("state", CIRCUIT_CLOSED)), entry

    async def circuit_snapshot(self, node_id: str) -> CircuitSnapshot:
        state, entry = await self._eval_circuit(node_id)
        trials = int(entry.get("trials", 0)) if entry else 0
        return CircuitSnapshot(state=state, trials=trials)

    async def circuit(self, node_id: str) -> str:
        return (await self.circuit_snapshot(node_id)).state

    async def set_circuit_open(self, node_id: str, open_seconds: int) -> None:
        self._circuits[node_id] = {
            "state": CIRCUIT_OPEN,
            "until": time.monotonic() + max(0, open_seconds),
            "trials": 0,
        }

    async def set_circuit_closed(self, node_id: str) -> None:
        self._circuits.pop(node_id, None)

    async def transition_circuit_open(self, node_id: str, open_seconds: int) -> CircuitTransition:
        state, _entry = await self._eval_circuit(node_id)
        if state == CIRCUIT_OPEN:
            self._circuits[node_id] = {
                "state": CIRCUIT_OPEN,
                "until": time.monotonic() + max(0, open_seconds),
                "trials": 0,
            }
            return CircuitTransition(changed=False, previous=CIRCUIT_OPEN, current=CIRCUIT_OPEN)
        self._circuits[node_id] = {
            "state": CIRCUIT_OPEN,
            "until": time.monotonic() + max(0, open_seconds),
            "trials": 0,
        }
        return CircuitTransition(changed=True, previous=state, current=CIRCUIT_OPEN)

    async def transition_circuit_closed(self, node_id: str) -> CircuitTransition:
        state, _entry = await self._eval_circuit(node_id)
        self._circuits.pop(node_id, None)
        if state == CIRCUIT_CLOSED:
            return CircuitTransition(changed=False, previous=CIRCUIT_CLOSED, current=CIRCUIT_CLOSED)
        return CircuitTransition(changed=True, previous=state, current=CIRCUIT_CLOSED)

    async def try_circuit_trial(
        self, node_id: str, max_trials: int = CIRCUIT_HALF_OPEN_MAX_TRIALS
    ) -> bool:
        state, entry = await self._eval_circuit(node_id)
        if state != CIRCUIT_HALF_OPEN or entry is None:
            return False
        if int(entry.get("trials", 0)) >= max_trials:
            return False
        entry["trials"] = int(entry.get("trials", 0)) + 1
        return True

    # ── 实例心跳 ───────────────────────────────────────────────────────

    async def heartbeat(self, instance_id: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        now = time.monotonic()
        self._instances[instance_id] = dict(payload)
        self._instance_expiry[instance_id] = now + max(1, int(ttl_seconds))
        self._instance_registry[instance_id] = dict(payload)
        self._instance_registry_expiry[instance_id] = now + INSTANCE_REGISTRY_TTL_SECONDS

    async def retire(self, instance_id: str) -> None:
        self._instances.pop(instance_id, None)
        self._instance_expiry.pop(instance_id, None)
        self._instance_registry.pop(instance_id, None)
        self._instance_registry_expiry.pop(instance_id, None)

    async def list_instances(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        result: list[dict[str, Any]] = []
        for instance_id in sorted(set(self._instances) | set(self._instance_registry)):
            if self._instance_registry_expiry.get(instance_id, 0.0) <= now:
                continue
            live = instance_id in self._instances and self._instance_expiry.get(instance_id, 0.0) > now
            if live:
                result.append(
                    {
                        "instance_id": instance_id,
                        "status": "ACTIVE",
                        "heartbeat": dict(self._instances[instance_id]),
                    }
                )
            else:
                result.append(
                    {
                        "instance_id": instance_id,
                        "status": "EXPIRED",
                        "heartbeat": dict(self._instance_registry.get(instance_id, {})),
                    }
                )
        return result

    # ── 已吊销 Key 摘要 ────────────────────────────────────────────────

    async def revoke_key_digest(self, digest: str) -> None:
        self._require_available()
        self._revoked.add(str(digest))

    async def sync_revoked_key_digests(self, digests: Iterable[str]) -> None:
        self._require_available()
        self._revoked.update(str(digest) for digest in digests)
        self._revoked.add(REVOKED_READY_MEMBER)

    async def is_key_revoked(self, digest: str) -> bool:
        """原子判断就绪哨兵与摘要；哨兵缺失时抛运行态错误（与 Redis 同语义）。

        哨兵与摘要在同一集合内，单事件循环无 await 交错，因此判断同样原子。
        """

        self._require_available()
        if REVOKED_READY_MEMBER not in self._revoked:
            raise RuntimeError("吊销阻断状态尚未就绪（Redis 已清空或未完成同步）")
        return str(digest) in self._revoked

    async def revocation_state_ready(self) -> bool:
        return self.available and REVOKED_READY_MEMBER in self._revoked

    def clear_revocation_state(self) -> None:
        """模拟 Redis 整集合键被淘汰/清空：哨兵与摘要一并丢失（仅供测试与嵌入式场景）。"""

        self._revoked.clear()

    def _require_available(self) -> None:
        if not self.available:
            raise RuntimeError("runtime unavailable")

    async def close(self) -> None:
        return None

    def instances(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        return {
            key: dict(value)
            for key, value in self._instances.items()
            if self._instance_expiry.get(key, 0.0) > now
        }

    def health_snapshot(self, node_id: str) -> dict[str, Any] | None:
        entry = self._health.get(node_id)
        return dict(entry) if entry else None


def _usage_from_mapping(raw: dict[str, Any] | None) -> TokenUsage:
    data = raw or {}

    def number(field: str) -> int:
        value = data.get(field, 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return TokenUsage(
        input_tokens=number("input"),
        output_tokens=number("output"),
        total_tokens=number("total"),
        requests=number("requests"),
        unknown_requests=number("unknown"),
    )


def usage_to_dict(usage: TokenUsage) -> dict[str, int]:
    return {key: int(value) for key, value in asdict(usage).items()}
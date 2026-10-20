"""API 中转观测查询：调用日志、容量与性能统计、实例运行状态。

统计来源固定为 ``gateway_requests``（调用日志表），不新建重复事实表；未知 usage
只计数不估算为零，也不保存 Prompt / Authorization / 响应体。当前并发直接读数据面
运行态（Redis 租约），峰值并发由窗口内请求的起止区间求最大重叠。大流量时可再
增加预聚合表，但不改变来源语义。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.models import (
    GatewayHealthCheck,
    GatewayNode,
    GatewayRequest,
    GatewayRoute,
)
from app.gateway.runtime import RuntimeStore
from app.gateway.schemas import (
    GatewayInstanceRead,
    LatencyStats,
    RequestLogPage,
    RequestLogRead,
    ScopeStats,
    StatBucket,
)
from app.gateway.service import GatewayRuntimeUnavailable
from app.models import utcnow

WINDOW_HOUR = "hour"
WINDOW_TODAY = "today"
WINDOW_7D = "7d"
WINDOW_30D = "30d"
SUPPORTED_WINDOWS = (WINDOW_HOUR, WINDOW_TODAY, WINDOW_7D, WINDOW_30D)

SCOPE_OVERVIEW = "overview"
SCOPE_NODE = "node"
SCOPE_POOL = "pool"
SCOPE_MODEL = "model"
SCOPE_KEY = "key"
SUPPORTED_SCOPES = (SCOPE_OVERVIEW, SCOPE_NODE, SCOPE_POOL, SCOPE_MODEL, SCOPE_KEY)

# 趋势分桶粒度（秒）：小时看 5 分钟，今日看小时，7/30 天看 6 小时与整天。
_TREND_GRANULARITY_SECONDS = {
    WINDOW_HOUR: 300,
    WINDOW_TODAY: 3600,
    WINDOW_7D: 6 * 3600,
    WINDOW_30D: 86400,
}

_STAT_COLUMNS = (
    GatewayRequest.started_at,
    GatewayRequest.finished_at,
    GatewayRequest.status_code,
    GatewayRequest.stream_interrupted,
    GatewayRequest.connect_ms,
    GatewayRequest.ttfb_ms,
    GatewayRequest.ttft_ms,
    GatewayRequest.duration_ms,
    GatewayRequest.input_tokens,
    GatewayRequest.output_tokens,
    GatewayRequest.total_tokens,
    GatewayRequest.output_tps,
    GatewayRequest.outcome,
    GatewayRequest.gateway_instance_id,
)


def window_bounds(window: str, now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """按自然小时 / 自然日（配置时区）计算统计窗口，返回 UTC 边界。"""

    local = now.astimezone(tz)
    if window == WINDOW_HOUR:
        start_local = local.replace(minute=0, second=0, microsecond=0)
    elif window == WINDOW_TODAY:
        start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif window == WINDOW_7D:
        start_local = (local - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif window == WINDOW_30D:
        start_local = (local - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"不支持的统计窗口: {window}")
    return start_local.astimezone(UTC), now


def percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(float(ordered[0]), 3)
    rank = ratio * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(float(ordered[low]), 3)
    interpolated = ordered[low] + (ordered[high] - ordered[low]) * (rank - low)
    return round(float(interpolated), 3)


def _latency(values: list[float]) -> LatencyStats:
    if not values:
        return LatencyStats(avg=None, p50=None, p95=None)
    return LatencyStats(
        avg=round(sum(values) / len(values), 3),
        p50=percentile(values, 0.5),
        p95=percentile(values, 0.95),
    )


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _phase(finished_at: datetime | None, outcome: str | None, instance_id: str | None) -> str:
    """终态统计只认已确认结束的行。进行中、结果未知、历史未完成分开计。"""

    if outcome == "lost":
        return "lost"
    if finished_at is None and instance_id:
        return "in_progress"
    if finished_at is None:
        return "legacy_open"
    return "terminal"


def _is_success(status_code: int | None, stream_interrupted: bool) -> bool:
    return (
        status_code is not None
        and 200 <= int(status_code) < 400
        and not stream_interrupted
    )


def _usage_known(input_tokens: Any, output_tokens: Any, total_tokens: Any) -> bool:
    return input_tokens is not None or output_tokens is not None or total_tokens is not None


class GatewayObservabilityService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        runtime: RuntimeStore | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self._session = session
        self._runtime = runtime
        self._timezone = timezone

    # ── 调用日志 ───────────────────────────────────────────────────────

    async def list_requests(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        request_id: str | None = None,
        node_id: str | None = None,
        model: str | None = None,
        key_id: str | None = None,
        status_code: int | None = None,
        error_code: str | None = None,
        pool_id: str | None = None,
        started_from: datetime | None = None,
        started_to: datetime | None = None,
    ) -> RequestLogPage:
        filters = []
        if request_id:
            filters.append(GatewayRequest.id == request_id)
        if node_id:
            filters.append(GatewayRequest.node_id == node_id)
        if model:
            filters.append(GatewayRequest.model == model)
        if key_id:
            filters.append(GatewayRequest.key_id == key_id)
        if pool_id:
            filters.append(GatewayRequest.pool_id == pool_id)
        if status_code is not None:
            filters.append(GatewayRequest.status_code == status_code)
        if error_code:
            filters.append(GatewayRequest.error_code == error_code)
        if started_from is not None:
            filters.append(GatewayRequest.started_at >= started_from)
        if started_to is not None:
            filters.append(GatewayRequest.started_at <= started_to)

        total = await self._session.scalar(
            select(func.count()).select_from(GatewayRequest).where(*filters)
        )
        rows = list(
            await self._session.scalars(
                select(GatewayRequest)
                .where(*filters)
                .order_by(GatewayRequest.started_at.desc(), GatewayRequest.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return RequestLogPage(
            items=[self._request_read(row) for row in rows],
            total=int(total or 0),
            page=page,
            page_size=page_size,
        )

    @staticmethod
    def _request_read(row: GatewayRequest) -> RequestLogRead:
        return RequestLogRead(
            id=row.id,
            key_id=row.key_id,
            owner_id=row.owner_id,
            model=row.model,
            actual_model=row.actual_model,
            pool_id=row.pool_id,
            node_id=row.node_id,
            streaming=row.streaming,
            retry_count=row.retry_count,
            status_code=row.status_code,
            error_code=row.error_code,
            stream_interrupted=row.stream_interrupted,
            connect_ms=row.connect_ms,
            ttfb_ms=row.ttfb_ms,
            ttft_ms=row.ttft_ms,
            duration_ms=row.duration_ms,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
            output_tps=row.output_tps,
            usage_known=_usage_known(row.input_tokens, row.output_tokens, row.total_tokens),
            started_at=row.started_at,
            finished_at=row.finished_at,
            outcome=row.outcome,
            gateway_instance_id=row.gateway_instance_id,
        )

    async def list_health_checks(
        self, node_id: str, *, limit: int = 100
    ) -> list[GatewayHealthCheck]:
        return list(
            await self._session.scalars(
                select(GatewayHealthCheck)
                .where(GatewayHealthCheck.node_id == node_id)
                .order_by(GatewayHealthCheck.created_at.desc(), GatewayHealthCheck.id.desc())
                .limit(limit)
            )
        )

    # ── 统计 ───────────────────────────────────────────────────────────

    async def scope_stats(
        self,
        *,
        scope: str,
        scope_id: str | None = None,
        window: str = WINDOW_TODAY,
    ) -> ScopeStats:
        if window not in SUPPORTED_WINDOWS:
            raise ValueError(f"不支持的统计窗口: {window}")
        if scope not in SUPPORTED_SCOPES:
            raise ValueError(f"不支持的统计范围: {scope}")
        now = utcnow()
        tz = ZoneInfo(self._timezone)
        start, end = window_bounds(window, now, tz)

        filters = [
            GatewayRequest.started_at >= start,
            GatewayRequest.started_at <= end,
        ]
        # 概览统计窗口内全部已落库调用。节点只统计实际调度到该节点的调用
        # （node_id 等于该节点）。鉴权失败、尚未选中节点，或由其他节点处理的请求
        # 会计入概览，不会出现在某一个节点的窗口请求里。两者不相等是预期口径。
        if scope == SCOPE_NODE:
            filters.append(GatewayRequest.node_id == scope_id)
        elif scope == SCOPE_KEY:
            filters.append(GatewayRequest.key_id == scope_id)
        elif scope == SCOPE_MODEL:
            filters.append(GatewayRequest.model == scope_id)
        elif scope == SCOPE_POOL:
            filters.append(GatewayRequest.pool_id == scope_id)

        rows = (
            await self._session.execute(
                select(*_STAT_COLUMNS).where(*filters).order_by(GatewayRequest.started_at)
            )
        ).all()

        requests = len(rows)
        success = 0
        errors = 0
        in_progress = 0
        lost = 0
        legacy_open = 0
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        unknown_usage = 0
        connect_values: list[float] = []
        ttfb_values: list[float] = []
        ttft_values: list[float] = []
        duration_values: list[float] = []
        tps_values: list[float] = []
        events: list[tuple[datetime, int]] = []

        granularity = _TREND_GRANULARITY_SECONDS[window]
        bucket_starts = self._bucket_starts(start, end, tz, granularity)
        bucket_totals = [
            {
                "requests": 0,
                "success": 0,
                "errors": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "unknown_usage_requests": 0,
            }
            for _ in bucket_starts
        ]

        for row in rows:
            (
                started_at,
                finished_at,
                status_code,
                stream_interrupted,
                connect_ms,
                ttfb_ms,
                ttft_ms,
                duration_ms,
                in_tokens,
                out_tokens,
                tot_tokens,
                output_tps,
                outcome,
                instance_id,
            ) = row
            index = self._bucket_index(started_at, start, tz, granularity)
            if index is not None:
                bucket_totals[index]["requests"] += 1
            phase = _phase(finished_at, outcome, instance_id)
            if phase == "in_progress":
                in_progress += 1
                continue
            if phase == "lost":
                lost += 1
                continue
            if phase == "legacy_open":
                legacy_open += 1
                continue
            succeeded = _is_success(status_code, bool(stream_interrupted))
            if succeeded:
                success += 1
            else:
                errors += 1
            known = _usage_known(in_tokens, out_tokens, tot_tokens)
            if not known:
                unknown_usage += 1
            in_value = int(in_tokens or 0)
            out_value = int(out_tokens or 0)
            tot_value = int(tot_tokens or 0)
            input_tokens += in_value
            output_tokens += out_value
            total_tokens += tot_value

            for target, value in (
                (connect_values, connect_ms),
                (ttfb_values, ttfb_ms),
                (duration_values, duration_ms),
            ):
                number = _number(value)
                if number is not None:
                    target.append(number)

            # TTFT 与 Output TPS 属于流式生成质量指标：
            # 1. 仅对成功且未中断的流式请求统计；
            # 2. 非流式响应、中途断开、失败、单 Token（已为 None）不计入分母，避免失真。
            if succeeded:
                ttft_num = _number(ttft_ms)
                if ttft_num is not None:
                    ttft_values.append(ttft_num)
                tps_num = _number(output_tps)
                if tps_num is not None:
                    tps_values.append(tps_num)

            if started_at is not None and finished_at is not None:
                event_end = finished_at
                event_start = started_at
                if event_start < start:
                    event_start = start
                if event_end > end:
                    event_end = end
                if event_end < event_start:
                    event_end = event_start
                events.append((event_start, 1))
                events.append((event_end, -1))

            if index is None:
                continue
            counter = bucket_totals[index]
            counter["success"] += 1 if succeeded else 0
            counter["errors"] += 0 if succeeded else 1
            counter["input_tokens"] += in_value
            counter["output_tokens"] += out_value
            counter["total_tokens"] += tot_value
            counter["unknown_usage_requests"] += 0 if known else 1

        elapsed = max(1.0, (end - start).total_seconds())
        trend = [
            StatBucket(bucket_start=bucket_start, **bucket_totals[index])
            for index, bucket_start in enumerate(bucket_starts)
        ]
        return ScopeStats(
            scope=scope,
            scope_id=scope_id,
            window=window,
            window_start=start,
            window_end=end,
            requests=requests,
            success=success,
            errors=errors,
            success_rate=round(success / (success + errors), 4) if success + errors else None,
            in_progress=in_progress,
            lost=lost,
            legacy_open=legacy_open,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            unknown_usage_requests=unknown_usage,
            qps=round(requests / elapsed, 4),
            active=await self._current_active(scope, scope_id),
            peak_concurrency=self._peak_concurrency(events),
            connect_ms=_latency(connect_values),
            ttfb_ms=_latency(ttfb_values),
            ttft_ms=_latency(ttft_values),
            duration_ms=_latency(duration_values),
            output_tps=_latency(tps_values),
            trend=trend,
        )

    @staticmethod
    def _bucket_starts(
        start: datetime, end: datetime, tz: ZoneInfo, granularity: int
    ) -> list[datetime]:
        start_local = start.astimezone(tz)
        end_local = end.astimezone(tz)
        count = int((end_local - start_local).total_seconds() // granularity) + 1
        return [start_local + timedelta(seconds=granularity * index) for index in range(count)]

    @staticmethod
    def _bucket_index(
        moment: datetime, start: datetime, tz: ZoneInfo, granularity: int
    ) -> int | None:
        local = moment.astimezone(tz)
        start_local = start.astimezone(tz)
        seconds = (local - start_local).total_seconds()
        if seconds < 0:
            return None
        return int(seconds // granularity)

    @staticmethod
    def _peak_concurrency(events: list[tuple[datetime, int]]) -> int:
        running = 0
        peak = 0
        # 同一时刻先处理结束（-1）再处理开始（+1），避免相邻请求被误算为重叠。
        for _moment, delta in sorted(events, key=lambda item: (item[0], item[1])):
            running += delta
            if running > peak:
                peak = running
        return peak

    # ── 当前并发 ───────────────────────────────────────────────────────

    async def _current_active(self, scope: str, scope_id: str | None) -> int:
        if self._runtime is None:
            return 0
        try:
            if scope == SCOPE_KEY:
                return await self._runtime.active(SCOPE_KEY, scope_id or "")
            node_ids = await self._scope_node_ids(scope, scope_id)
            total = 0
            for node_id in node_ids:
                total += await self._runtime.active(SCOPE_NODE, node_id)
            return total
        except Exception:  # noqa: BLE001 - 运行态不可达时并发展示为 0，不阻断统计
            return 0

    async def _scope_node_ids(self, scope: str, scope_id: str | None) -> list[str]:
        if scope == SCOPE_NODE:
            return [scope_id] if scope_id else []
        nodes = list(await self._session.scalars(select(GatewayNode)))
        if scope == SCOPE_OVERVIEW:
            return [node.id for node in nodes]
        if scope == SCOPE_POOL:
            return [node.id for node in nodes if node.pool_id == scope_id]
        if scope == SCOPE_MODEL:
            route = await self._session.scalar(
                select(GatewayRoute).where(GatewayRoute.model == scope_id)
            )
            if route is None:
                return []
            return [
                node.id
                for node in nodes
                if node.pool_id == route.pool_id and scope_id in (node.models or [])
            ]
        return []

    # ── 实例 ───────────────────────────────────────────────────────────

    async def list_instances(self) -> list[GatewayInstanceRead]:
        if self._runtime is None:
            return []
        try:
            raw = await self._runtime.list_instances()
        except Exception as exc:  # noqa: BLE001 - Redis 不可达时结构化 503
            raise GatewayRuntimeUnavailable("无法读取 Gateway 实例心跳（Redis 不可用）") from exc
        return [self._instance_read(item) for item in raw]

    def _instance_read(self, item: dict[str, Any]) -> GatewayInstanceRead:
        heartbeat = item.get("heartbeat") or {}
        updated_at = _parse_dt(heartbeat.get("updated_at"))
        age_seconds = None
        if updated_at is not None:
            age_seconds = round(max(0.0, (utcnow() - updated_at).total_seconds()), 3)
        return GatewayInstanceRead(
            instance_id=str(item.get("instance_id")),
            status=str(item.get("status")),
            app_version=heartbeat.get("app_version"),
            pid=heartbeat.get("pid"),
            started_at=_parse_dt(heartbeat.get("started_at")),
            updated_at=updated_at,
            uptime_seconds=heartbeat.get("uptime_seconds"),
            age_seconds=age_seconds,
            requests=heartbeat.get("requests"),
            errors=heartbeat.get("errors"),
            active=heartbeat.get("active"),
            cpu_percent=heartbeat.get("cpu_percent"),
            memory_bytes=heartbeat.get("memory_bytes"),
        )

"""未完成调用按网关实例代次回收。

一行进行中的请求属于写入它的进程代次：``gateway_instance_id`` 加上当时心跳里的
``started_at``。只有当前 ACTIVE 心跳的 started_at 与行上的代次一致，才视为仍在处理。
同一 instance_id 重启后 started_at 变化，上一代表面上看起来还在心跳，也不能把旧行留成进行中。

不按年龄失败。心跳缺失、状态不是 ACTIVE、或代次不一致时，把行标成 ``lost``
（结果未知 / 异常结束），写入 finished_at，不填写 status_code 或 error_code。

``gateway_instance_id`` 为空的历史未完成行不猜测、不改写。统计把它们单独计为 legacy_open。

保留清理只能删除 finished_at 已写入的终态历史（正常结束与 lost）。进行中和历史未完成不删。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.models import GatewayRequest
from app.gateway.runtime import RuntimeStore
from app.models import utcnow

OUTCOME_LOST = "lost"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_started_at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return _aware(parsed)


def live_generations(instances: list[dict[str, Any]]) -> set[tuple[str, datetime]]:
    live: set[tuple[str, datetime]] = set()
    for item in instances:
        if item.get("status") != "ACTIVE":
            continue
        instance_id = item.get("instance_id")
        heartbeat = item.get("heartbeat") or {}
        started = parse_started_at(heartbeat.get("started_at"))
        if not instance_id or started is None:
            continue
        live.add((str(instance_id), started))
    return live


async def recover_abandoned_requests(session: AsyncSession, runtime: RuntimeStore) -> int:
    """把失联代次上的未完成行标成 lost。返回改写行数。历史空实例行不动。"""

    instances = await runtime.list_instances()
    live = live_generations(instances)
    rows = list(
        await session.scalars(
            select(GatewayRequest).where(
                GatewayRequest.finished_at.is_(None),
                GatewayRequest.gateway_instance_id.is_not(None),
                or_(GatewayRequest.outcome.is_(None), GatewayRequest.outcome == ""),
            )
        )
    )
    now = utcnow()
    changed = 0
    for row in rows:
        started = parse_started_at(row.gateway_instance_started_at)
        if started is not None and (row.gateway_instance_id, started) in live:
            continue
        row.outcome = OUTCOME_LOST
        row.finished_at = now
        changed += 1
    if changed:
        await session.commit()
    return changed

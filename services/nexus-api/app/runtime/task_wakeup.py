"""投递待发送的任务唤醒。重复投递只标记 outbox，不改任务状态。"""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.models import TaskWakeOutbox
from nexus_api.auth import internal_headers


class WakeRetry(Exception):
    """临时失败。保持 pending，按封顶指数退避再投。"""


class WakePermanent(Exception):
    """对方明确拒绝。不再重试。"""


class HttpWakeSender:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    async def send(self, row: TaskWakeOutbox) -> None:
        settings = get_settings()
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10)
        try:
            try:
                target = await client.get(
                    f"{settings.nexus_api_base.rstrip('/')}/api/v1/internal/wake-target",
                    headers=internal_headers(),
                    params={
                        "employee_id": row.responsible_employee_id,
                        "session_id": row.source_session_id or "",
                    },
                )
            except httpx.HTTPError as exc:
                raise WakeRetry(str(exc)) from exc
            _raise_for_status(target)
            body = target.json()
            try:
                if body.get("channel_id"):
                    response = await client.post(
                        f"{settings.nexus_api_base.rstrip('/')}/api/v1/internal/group-task-event",
                        headers=internal_headers(),
                        json={"event_id": row.event_id},
                    )
                else:
                    response = await client.post(
                        f"{settings.agentscope_base_url.rstrip('/')}/nexus/task-hint",
                        headers={
                            "X-Nexus-Event-Id": str(row.event_id),
                            **internal_headers(),
                        },
                        json={
                            "session_id": body["session_id"],
                            "agent_id": body["agent_id"],
                            "hint": row.hint,
                            "user_id": settings.agentscope_user_id,
                            "event_id": str(row.event_id),
                        },
                    )
            except httpx.HTTPError as exc:
                raise WakeRetry(str(exc)) from exc
            _raise_for_status(response)
        finally:
            if owns_client:
                await client.aclose()


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    detail = response.text.strip() or response.reason_phrase
    if response.status_code in {404, 409} or response.status_code >= 500:
        raise WakeRetry(detail or f"HTTP {response.status_code}")
    raise WakePermanent(detail or f"HTTP {response.status_code}")


def retry_delay(attempts: int, *, base: int, cap: int) -> int:
    step = max(0, attempts - 1)
    return min(cap, base * (2**step))


async def deliver_pending(
    session: AsyncSession,
    sender,
    *,
    now: datetime | None = None,
    limit: int = 20,
) -> int:
    moment = now or datetime.now(UTC)
    settings = get_settings()
    lease = timedelta(seconds=settings.outbox_lease_seconds)
    rows = (
        await session.scalars(
            select(TaskWakeOutbox)
            .where(
                TaskWakeOutbox.status.in_(("pending", "delivering")),
                TaskWakeOutbox.next_retry_at <= moment,
            )
            .order_by(TaskWakeOutbox.id)
            .limit(limit)
        )
    ).all()
    delivered = 0
    for row in rows:
        row_id = row.id
        attempts = row.attempts
        claimed = await session.execute(
            update(TaskWakeOutbox)
            .where(
                TaskWakeOutbox.id == row_id,
                TaskWakeOutbox.status.in_(("pending", "delivering")),
                TaskWakeOutbox.next_retry_at <= moment,
            )
            .values(status="delivering", next_retry_at=moment + lease, updated_at=moment)
        )
        await session.commit()
        if claimed.rowcount != 1:
            continue
        try:
            await sender.send(row)
        except WakePermanent as exc:
            await _settle(
                session,
                row_id,
                status="failed",
                attempts=attempts + 1,
                error=str(exc)[:500],
                moment=moment,
            )
            continue
        except Exception as exc:
            next_attempt = attempts + 1
            delay = retry_delay(
                next_attempt,
                base=settings.outbox_retry_seconds,
                cap=settings.outbox_retry_cap_seconds,
            )
            await _settle(
                session,
                row_id,
                status="pending",
                attempts=next_attempt,
                error=str(exc)[:500],
                moment=moment,
                retry_at=moment + timedelta(seconds=delay),
            )
            continue
        await _settle(
            session,
            row_id,
            status="delivered",
            attempts=attempts,
            error=None,
            moment=moment,
        )
        delivered += 1
    return delivered


async def _settle(
    session: AsyncSession,
    row_id: int,
    *,
    status: str,
    attempts: int,
    error: str | None,
    moment: datetime,
    retry_at: datetime | None = None,
) -> None:
    values: dict = {
        "status": status,
        "attempts": attempts,
        "last_error": error,
        "updated_at": moment,
    }
    if retry_at is not None:
        values["next_retry_at"] = retry_at
    await session.execute(update(TaskWakeOutbox).where(TaskWakeOutbox.id == row_id).values(**values))
    await session.commit()

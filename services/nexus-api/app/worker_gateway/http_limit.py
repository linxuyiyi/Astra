"""Atomic in-flight cap for Worker http commands.

The counter lives on one row per worker and epoch. The conditional update is
the claim: two sessions cannot both pass the same limit.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WorkerHttpSlot

MAX_HTTP_IN_FLIGHT = 8


async def reserve_http_slot(session: AsyncSession, worker_id: str, epoch: int) -> bool:
    claimed = await session.execute(
        update(WorkerHttpSlot)
        .where(
            WorkerHttpSlot.worker_instance_id == worker_id,
            WorkerHttpSlot.epoch == epoch,
            WorkerHttpSlot.inflight < MAX_HTTP_IN_FLIGHT,
        )
        .values(inflight=WorkerHttpSlot.inflight + 1)
    )
    if claimed.rowcount == 1:
        return True
    existing = await session.scalar(
        select(WorkerHttpSlot.worker_instance_id).where(
            WorkerHttpSlot.worker_instance_id == worker_id,
            WorkerHttpSlot.epoch == epoch,
        )
    )
    if existing is not None:
        return False
    try:
        async with session.begin_nested():
            session.add(WorkerHttpSlot(worker_instance_id=worker_id, epoch=epoch, inflight=1))
            await session.flush()
    except IntegrityError:
        claimed = await session.execute(
            update(WorkerHttpSlot)
            .where(
                WorkerHttpSlot.worker_instance_id == worker_id,
                WorkerHttpSlot.epoch == epoch,
                WorkerHttpSlot.inflight < MAX_HTTP_IN_FLIGHT,
            )
            .values(inflight=WorkerHttpSlot.inflight + 1)
        )
        return claimed.rowcount == 1
    return True


async def release_http_slot(session: AsyncSession, worker_id: str, epoch: int) -> None:
    await session.execute(
        update(WorkerHttpSlot)
        .where(
            WorkerHttpSlot.worker_instance_id == worker_id,
            WorkerHttpSlot.epoch == epoch,
            WorkerHttpSlot.inflight > 0,
        )
        .values(inflight=WorkerHttpSlot.inflight - 1)
    )

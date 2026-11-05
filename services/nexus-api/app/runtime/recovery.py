"""按 Task 独立读取原生状态，连接缓慢的 Worker 不阻塞其他 Task。"""

import asyncio
import logging

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Task
from app.runtime.lifecycle import RuntimeService

logger = logging.getLogger(__name__)


async def watch_session(task_id):
    while True:
        try:
            async with SessionLocal() as session:
                task = await session.get(Task, task_id)
                if not task or task.status not in {"RUNNING", "WAITING"}:
                    return
                runtime = RuntimeService(session)
                if task.status == "RUNNING" and not task.current_session_id and not task.pending_message_id:
                    await runtime.start(task_id)
                else:
                    await runtime.refresh(task_id)
        except Exception:
            logger.exception("任务状态读取失败 %s", task_id)
        await asyncio.sleep(2)


async def supervise_sessions():
    listeners = {}
    try:
        while True:
            try:
                async with SessionLocal() as session:
                    ids = set(
                        (
                            await session.scalars(
                                select(Task.id).where(Task.status.in_(("RUNNING", "WAITING")))
                            )
                        ).all()
                    )
                for task_id in ids:
                    if task_id not in listeners or listeners[task_id].done():
                        listeners[task_id] = asyncio.create_task(watch_session(task_id))
                for task_id in set(listeners) - ids:
                    listener = listeners.pop(task_id)
                    listener.cancel()
                    await asyncio.gather(listener, return_exceptions=True)
            except Exception:
                logger.exception("任务巡检失败")
            await asyncio.sleep(2)
    finally:
        for listener in listeners.values():
            listener.cancel()
        await asyncio.gather(*listeners.values(), return_exceptions=True)

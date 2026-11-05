"""原生 SSE 触发 Task 对账，定时扫描仅管理绑定及未完成的启动和提交。"""

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AgentWorker, Task
from app.runtime.lifecycle import RuntimeService
from app.runtime.session_connection import connect, task_session_binding

logger = logging.getLogger(__name__)

_ACTIVE = {"RUNNING", "WAITING"}
_SCAN_SECONDS = 2
_RECONCILE_EVENTS = {
    "session.status", "session.idle", "session.error", "session.deleted",
    "question.asked", "question.replied", "question.rejected",
    "permission.asked", "permission.updated", "permission.replied",
}


def _needs_reconcile(event, sessions):
    kind = event.get("type")
    if kind in {"astra.event_gap", "server.connected", "server.instance.disposed"}:
        return True
    if kind != "session.created" and kind not in _RECONCILE_EVENTS:
        return False
    properties = event.get("properties") or {}
    info = properties.get("info") or {}
    if kind == "session.created":
        if info.get("parentID") not in sessions or not info.get("id"):
            return False
        sessions.add(info["id"])
        return True
    session_id = properties.get("sessionID")
    if kind == "session.deleted":
        session_id = session_id or info.get("id")
    # OpenCode can emit a server-level error without a Session ID.
    return session_id in sessions or (kind == "session.error" and not session_id)


async def _watch_events(task_id, worker, binding, sessions, wake):
    consumer = f"task:{task_id}:{uuid4().hex}"
    disconnected = False
    while True:
        first = True
        try:
            async with connect(worker) as client:
                directory = Path(binding.workspace)
                stream = (
                    client.events(directory, consumer=consumer)
                    if getattr(client, "records_fetch", False)
                    else client.events(directory)
                )
                async for event in stream:
                    if event.get("type") == "astra.keepalive":
                        continue
                    relevant = _needs_reconcile(event, sessions)
                    if first or relevant:
                        wake.set()
                    first = False
                    disconnected = False
        except Exception as exc:
            if not disconnected:
                logger.info("Task %s 原生事件流暂不可用 (%s)", task_id, type(exc).__name__)
        if not disconnected:
            wake.set()
            disconnected = True
        await asyncio.sleep(_SCAN_SECONDS)


async def _stop_listener(listener):
    if listener is not None:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


async def watch_session(task_id):
    binding = listener = wake = sessions = None
    try:
        while True:
            delay = True
            try:
                async with SessionLocal() as session:
                    task = await session.get(Task, task_id)
                    if not task or task.status not in _ACTIVE:
                        return
                    next_binding = task_session_binding(task)
                    if next_binding != binding:
                        await _stop_listener(listener)
                        binding, listener = next_binding, None
                        if binding is not None:
                            worker = await session.get(AgentWorker, binding.worker_id)
                            wake, sessions = asyncio.Event(), {binding.session_id}
                            listener = asyncio.create_task(
                                _watch_events(task_id, worker, binding, sessions, wake)
                            )
                    runtime = RuntimeService(session)
                    if task.pending_message_id:
                        resumed = await runtime.retry_submission(task_id)
                        if not resumed.pending_message_id and binding is not None:
                            wake.set()
                    elif task.status == "RUNNING" and not task.current_session_id:
                        await runtime.start(task_id)
                    elif binding is not None:
                        if wake.is_set():
                            wake.clear()
                            await runtime.refresh(
                                task_id, expected_binding=binding, observed_sessions=sessions
                            )
                        delay = False
            except Exception:
                logger.exception("Task %s 运行状态对账失败", task_id)
            if delay:
                await asyncio.sleep(_SCAN_SECONDS)
            else:
                try:
                    await asyncio.wait_for(wake.wait(), timeout=_SCAN_SECONDS)
                except TimeoutError:
                    pass
    finally:
        await _stop_listener(listener)


async def supervise_sessions():
    listeners = {}
    try:
        while True:
            try:
                async with SessionLocal() as session:
                    ids = set(
                        (
                            await session.scalars(
                                select(Task.id).where(Task.status.in_(_ACTIVE))
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
            await asyncio.sleep(_SCAN_SECONDS)
    finally:
        for listener in listeners.values():
            listener.cancel()
        await asyncio.gather(*listeners.values(), return_exceptions=True)

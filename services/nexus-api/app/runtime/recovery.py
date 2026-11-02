"""Reconnect and reconcile native Sessions without inventing a second execution model."""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import or_, select

from app.config.settings import get_settings
from app.database import SessionLocal
from app.executors.errors import OpenCodeSessionNotFound
from app.executors.opencode_server import server_event_session_id
from app.models import Task
from app.runtime.lifecycle import RuntimeService
from app.runtime.session_connection import connect, resolve_environment, session_tree

logger = logging.getLogger(__name__)

SUPERVISOR_INTERVAL_SECONDS = 2
SUPERVISOR_RETRY_MAX_SECONDS = 30


def _active_task_filter():
    return or_(
        Task.status == "RUNNING",
        Task.status == "WAITING",
        Task.pending_prompt.is_not(None),
    )


async def _refresh_with_client(task_id: str, client) -> SimpleNamespace | None:
    async with SessionLocal() as session:
        task = await RuntimeService(session).refresh(task_id, client=client)
        if task is None:
            return None
        # refresh() may roll back, which expires the Task. Async sessions cannot
        # lazy-load those fields after the session closes, so reload them here.
        await session.refresh(task)
        return SimpleNamespace(status=task.status, current_session_id=task.current_session_id)


async def watch_session(task_id):
    """Use native SSE for immediacy and bounded refresh for eventual convergence."""

    delay = 1
    settings = get_settings()
    loop = asyncio.get_running_loop()
    while True:
        next_event: asyncio.Task | None = None
        try:
            async with SessionLocal() as session:
                task = await session.get(Task, task_id)
                if (
                    not task
                    or not task.current_session_id
                    or not (
                        task.status == "RUNNING"
                        or task.status == "WAITING"
                        or task.pending_prompt
                    )
                ):
                    return
                environment = await resolve_environment(session, task)
                session_id, workspace = task.current_session_id, task.workspace
                await session.commit()

            async with connect(environment, start=True) as client:
                directory = Path(workspace)
                if getattr(client, "records_fetch", False):
                    events = client.events(directory, consumer=f"recovery:{task_id}").__aiter__()
                else:
                    events = client.events(directory).__aiter__()
                # Start the subscription first, then reconcile. A bounded refresh
                # below closes any remaining read-before-subscribe race.
                next_event = asyncio.create_task(anext(events))
                current = await _refresh_with_client(task_id, client)
                if not current or current.status in {"SUCCEEDED", "FAILED", "CANCELED"}:
                    return
                allowed = await session_tree(client, directory, session_id)
                deadline = loop.time() + settings.opencode_reconcile_interval_seconds
                gap_followup = False
                owned_refresh = False

                while True:
                    # The reconcile deadline is absolute: unrelated SSE events must
                    # never push convergence past it.
                    if loop.time() >= deadline:
                        current = await _refresh_with_client(task_id, client)
                        if not current or current.status in {"SUCCEEDED", "FAILED", "CANCELED"}:
                            return
                        allowed = await session_tree(client, directory, session_id)
                        again = False
                        if gap_followup and owned_refresh:
                            finish = getattr(client, "finish_gap_refresh", None)
                            again = finish is not None and await finish(directory)
                        gap_followup = False
                        owned_refresh = False
                        delay = 1
                        deadline = loop.time() + settings.opencode_reconcile_interval_seconds
                        if again:
                            gap_followup = True
                            owned_refresh = True
                            deadline = loop.time()
                        continue

                    done, _ = await asyncio.wait(
                        {next_event},
                        timeout=max(0, deadline - loop.time()),
                    )
                    if not done:
                        continue

                    try:
                        event = next_event.result()
                    except StopAsyncIteration as exc:
                        raise ConnectionError("OpenCode Event SSE 已断开") from exc
                    next_event = asyncio.create_task(anext(events))

                    props = event.get("properties", {})
                    info = props.get("info", {}) if isinstance(props.get("info"), dict) else {}
                    if event.get("type") == "session.created" and info.get("parentID") in allowed:
                        child_id = info.get("id")
                        if child_id:
                            allowed.add(child_id)
                    if event.get("type") == "astra.event_gap":
                        if not gap_followup:
                            claim = getattr(client, "claim_gap_refresh", None)
                            if claim is None or await claim(directory):
                                owned_refresh = True
                            gap_followup = True
                            deadline = min(deadline, loop.time() + 0.25)
                        continue
                    event_session_id = server_event_session_id(event)
                    if event_session_id and event_session_id not in allowed:
                        continue
                    if event.get("type") not in {
                        "session.created",
                        "session.status",
                        "session.idle",
                        "session.error",
                        "question.asked",
                        "question.replied",
                        "permission.asked",
                        "permission.replied",
                    }:
                        continue

                    # OpenCode 1.18.35 emits session.status (idle/busy/retry), the
                    # deprecated session.idle, and session.error {sessionID, error}.
                    # None of those events is a final round result. A live re-read
                    # decides, and a stale round cannot overwrite a newer anchor.
                    if event["type"] == "session.error" and event_session_id == session_id:
                        async with SessionLocal() as session:
                            current = await session.get(Task, task_id)
                            if not current or current.current_session_id != session_id:
                                return
                            await session.commit()

                    # Reconcile once for a burst; native SSE still reaches Workspace immediately.
                    deadline = min(deadline, loop.time() + 0.25)
                    continue
        except asyncio.CancelledError:
            raise
        except OpenCodeSessionNotFound as exc:
            logger.warning("Session missing for %s: %s", task_id, exc)
            return
        except Exception as exc:
            logger.warning("Session connection lost for %s: %s", task_id, exc)
            async with SessionLocal() as session:
                await RuntimeService(session).disconnected(task_id, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        finally:
            if next_event and not next_event.done():
                next_event.cancel()
                await asyncio.gather(next_event, return_exceptions=True)


async def supervise_sessions():
    """Own one native Session watcher per active task."""

    listeners: dict[str, asyncio.Task] = {}
    delay = SUPERVISOR_INTERVAL_SECONDS
    try:
        while True:
            try:
                async with SessionLocal() as session:
                    ids = set(
                        (
                            await session.scalars(
                                select(Task.id).where(
                                    Task.current_session_id.is_not(None),
                                    _active_task_filter(),
                                )
                            )
                        ).all()
                    )
                for task_id in ids:
                    if task_id not in listeners or listeners[task_id].done():
                        listeners[task_id] = asyncio.create_task(watch_session(task_id))
                for task_id in list(listeners):
                    if task_id not in ids:
                        listener = listeners.pop(task_id)
                        listener.cancel()
                        await asyncio.gather(listener, return_exceptions=True)

                delay = SUPERVISOR_INTERVAL_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Session Supervisor 周期失败，退避后重试")
                delay = min(delay * 2, SUPERVISOR_RETRY_MAX_SECONDS)
            await asyncio.sleep(delay)
    finally:
        for listener in listeners.values():
            listener.cancel()
        await asyncio.gather(*listeners.values(), return_exceptions=True)

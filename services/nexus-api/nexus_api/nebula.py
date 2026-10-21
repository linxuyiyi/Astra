"""Nexus 星系首页的只读查询投影。

事实源分工：

* 数字员工目录、私聊会话、员工间职责请求由 ``nexus_api`` 查询共享 SQL 数据库；
* Agent Worker、任务、执行环境、WorkerInstance 在 ``app`` 的 SQLAlchemy 数据层。

这里不复制任何业务状态，也不写业务表。两套访问层按 ID 批量读取后在本模块组装，
保持模块查询边界，也不把两次独立连接读取包装成一个一致快照事务。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config.settings import get_settings
from app.database import get_session
from app.models import (
    AgentWorker,
    OpenCodeExecutionEnvironment,
    RuntimeSession,
    Task,
    WorkerInstance,
)

from .db import connect
from .main import require_owner

logger = logging.getLogger("nexus.nebula")

router = APIRouter(prefix="/api/v1/nebula", tags=["nebula"])

PENDING = "PENDING"
RUNNING = "RUNNING"
WAITING = "WAITING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELED = "CANCELED"

COUNTED_STATUSES = (PENDING, RUNNING, WAITING, FAILED)
PREVIEW_STATUSES = (PENDING, RUNNING, WAITING)
COUNT_FIELD = {RUNNING: "running", PENDING: "queued", WAITING: "waiting", FAILED: "failed"}

QUEUED_PREVIEW_LIMIT = 400
ACTIVE_PREVIEW_LIMIT = 400
AWAITING_PREVIEW_LIMIT = 20
ATTENTION_LIMIT = 30
UNASSIGNED_TASK_LIMIT = 20
CONFLICT_LIMIT = 50
COLLABORATION_RECENT = timedelta(hours=24)
WAITING_REASON_TEXT = {
    "SESSION_MISSING": "执行会话不存在，需要恢复",
    "TRANSPORT_RECOVERY": "执行连接恢复中，等待系统重连",
    "EXECUTION_RESULT_REVIEW": "本轮执行结束，等待人工确认结果",
    "HITL_PERMISSION": "等待人工授权",
    "HITL_QUESTION": "等待人工回答",
    "TIMEOUT": "执行超时",
    "MANUAL_INTERVENTION": "等待人工介入",
    "RETRY_EXHAUSTED": "重试次数已用尽",
}


# ---------------------------------------------------------------------------
# 基础工具


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _now(now: datetime | None) -> datetime:
    current = now or datetime.now(UTC)
    return current if current.tzinfo else current.replace(tzinfo=UTC)


def _day_window(now: datetime) -> tuple[datetime, datetime, str]:
    timezone_name = get_settings().timezone
    timezone = ZoneInfo(timezone_name)
    local_today = now.astimezone(timezone).date()
    start = datetime.combine(local_today, datetime.min.time(), tzinfo=timezone).astimezone(UTC)
    end = datetime.combine(local_today + timedelta(days=1), datetime.min.time(), tzinfo=timezone).astimezone(
        UTC
    )
    return start, end, timezone_name


def _empty_counts() -> dict[str, int]:
    return {"running": 0, "queued": 0, "waiting": 0, "failed": 0, "completedToday": 0}


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = (value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


# ---------------------------------------------------------------------------
# 员工目录与 Worker 事实


def _employee_rows(connection: Connection) -> list[dict[str, Any]]:
    rows = (
        connection.execute(
            sql_text(
                """
                SELECT id, name, domain, description, avatar, enabled,
                       agentscope_agent_id, created_at, updated_at
                FROM t_nexus_employees
                ORDER BY created_at, id
                """
            )
        )
        .mappings()
        .fetchall()
    )
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "domain": row["domain"],
            "description": row["description"],
            "avatar": row["avatar"],
            "enabled": bool(row["enabled"]),
            "agentscope_agent_id": row["agentscope_agent_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _employee_base(row: dict[str, Any], counts: dict[str, int]) -> dict[str, Any]:
    if not row["enabled"]:
        status = "unavailable"
    elif not row["agentscope_agent_id"]:
        status = "unknown"
    else:
        status = "available"
    return {
        "id": row["id"],
        "name": row["name"],
        "domain": row["domain"],
        "description": row["description"],
        "avatar": row["avatar"],
        "enabled": bool(row["enabled"]),
        "interactionStatus": status,
        "counts": counts,
        "createdAt": row["created_at"],
    }


async def _load_workers(
    session: AsyncSession,
) -> tuple[list[AgentWorker], dict[str, WorkerInstance]]:
    workers = (
        (
            await session.execute(
                select(AgentWorker)
                .options(selectinload(AgentWorker.execution_environment))
                .order_by(AgentWorker.id)
            )
        )
        .scalars()
        .all()
    )
    instances = (await session.scalars(select(WorkerInstance))).all()
    return list(workers), {item.environment_id: item for item in instances}


async def _worker_task_counts(session: AsyncSession) -> dict[str, dict[str, int]]:
    rows = (
        await session.execute(
            select(Task.employee_id, Task.status, func.count(Task.id))
            .where(
                Task.employee_id.is_not(None),
                Task.status.in_((RUNNING, WAITING)),
            )
            .group_by(Task.employee_id, Task.status)
        )
    ).all()
    result: dict[str, dict[str, int]] = defaultdict(lambda: {"running": 0, "waiting": 0})
    for worker_id, task_status, count in rows:
        if task_status == RUNNING:
            result[str(worker_id)]["running"] += int(count)
        elif task_status == WAITING:
            result[str(worker_id)]["waiting"] += int(count)
    return dict(result)


def _worker_health(
    worker: AgentWorker,
    environment: OpenCodeExecutionEnvironment | None,
    instance: WorkerInstance | None,
    *,
    now: datetime,
    stale_after: timedelta,
) -> str:
    if not worker.enabled:
        return "offline"
    if worker.status == "ERROR":
        return "error"
    if worker.status == "OFFLINE":
        return "offline"
    if environment is None:
        return "unknown"
    remote = environment.mode == "LINUX_REMOTE" and environment.worker_channel == "worker"
    if remote:
        if instance is None:
            return "unknown"
        if instance.status != "online":
            return "offline"
        heartbeat = instance.last_heartbeat_at or worker.last_heartbeat
        if heartbeat is None:
            return "offline"
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
        if heartbeat.astimezone(UTC) <= now - stale_after:
            return "offline"
        return "online"
    if environment.status == "AVAILABLE":
        return "online"
    if environment.status == "UNAVAILABLE":
        return "error"
    return "unknown"


def _worker_occupancy(counts: dict[str, int]) -> str:
    if counts.get("waiting"):
        return "waiting"
    if counts.get("running"):
        return "running"
    return "idle"


def _worker_dto(
    worker: AgentWorker,
    environment: OpenCodeExecutionEnvironment | None,
    instance: WorkerInstance | None,
    counts: dict[str, int],
    *,
    now: datetime,
    stale_after: timedelta,
) -> dict[str, Any]:
    health = _worker_health(worker, environment, instance, now=now, stale_after=stale_after)
    labels = _unique(
        [
            worker.role,
            *[str(item) for item in ((environment.labels or []) if environment else [])],
        ]
    )
    return {
        "id": worker.id,
        "digitalEmployeeId": worker.digital_employee_id,
        "workerInstanceId": instance.id if instance else None,
        "name": worker.name,
        "role": worker.role,
        "capabilityLabels": labels,
        "environmentMode": environment.mode if environment else None,
        "environmentName": environment.name if environment else None,
        "modelName": environment.model_name if environment else None,
        "health": health,
        "occupancy": _worker_occupancy(counts),
        "taskCount": int(counts.get("running", 0)) + int(counts.get("waiting", 0)),
        "runningTaskCount": int(counts.get("running", 0)),
        "waitingTaskCount": int(counts.get("waiting", 0)),
        "lastHeartbeatAt": _iso((instance.last_heartbeat_at if instance else None) or worker.last_heartbeat),
    }


# ---------------------------------------------------------------------------
# 任务事实


def _resolve_task_owner(
    task: Task,
    employee_ids: set[str],
    worker_owner: dict[str, str],
    worker_known: set[str],
) -> tuple[str | None, list[str]]:
    conflicts: list[str] = []
    worker_id = task.employee_id
    if worker_id and worker_id not in worker_known:
        conflicts.append("TASK_WORKER_MISSING")
    worker_owner_id = worker_owner.get(worker_id) if worker_id else None
    responsible = task.responsible_employee_id
    if responsible and responsible not in employee_ids:
        conflicts.append("TASK_RESPONSIBLE_MISSING")
    if responsible and worker_owner_id and responsible != worker_owner_id:
        conflicts.append("TASK_OWNER_CONFLICT")
    if responsible:
        owner = responsible if responsible in employee_ids else None
    else:
        owner = worker_owner_id
    if owner and owner not in employee_ids:
        owner = None
    return owner, conflicts


async def _load_session_statuses(session: AsyncSession, tasks: list[Task]) -> dict[str, str]:
    ids = [task.current_session_id for task in tasks if task.current_session_id]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(RuntimeSession.id, RuntimeSession.status).where(RuntimeSession.id.in_(ids))
        )
    ).all()
    return {str(row[0]): row[1] for row in rows}


def _task_dto(
    task: Task,
    *,
    owner: str | None,
    conflicts: list[str],
    worker_names: dict[str, str],
    employee_names: dict[str, str],
    session_statuses: dict[str, str],
) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "taskType": task.task_type,
        "responsibleEmployeeId": task.responsible_employee_id,
        "workerId": task.employee_id,
        "workerName": worker_names.get(task.employee_id or ""),
        "ownerId": owner,
        "ownerName": employee_names.get(owner or ""),
        "currentProgress": task.current_progress or "",
        "waitingReason": task.human_required_reason
        or WAITING_REASON_TEXT.get(task.waiting_reason or "", task.waiting_reason or ""),
        "waitingReasonCode": task.waiting_reason,
        "failureReason": task.failure_reason or "",
        "runtimeConnection": task.runtime_connection,
        "currentSessionId": task.current_session_id,
        "sessionStatus": session_statuses.get(task.current_session_id or ""),
        "pendingMessageId": task.pending_message_id,
        "sourceConversationId": task.source_conversation_id,
        "sourceChannelId": task.source_channel_id,
        "inactivityAlert": bool(task.inactivity_alert),
        "conflict": bool(conflicts),
        "conflictCodes": conflicts,
        "createdAt": _iso(task.created_at),
        "updatedAt": _iso(task.updated_at),
        "completedAt": _iso(task.completed_at),
    }


def _conflict(code: str, entity_type: str, entity_id: str, message: str) -> dict[str, str]:
    return {"code": code, "entityType": entity_type, "entityId": entity_id, "message": message}


def _add_conflict(conflicts: list[dict[str, str]], seen: set[tuple[str, str]], item: dict[str, str]) -> None:
    key = (item["code"], item["entityId"])
    if key in seen or len(conflicts) >= CONFLICT_LIMIT:
        return
    seen.add(key)
    conflicts.append(item)


# ---------------------------------------------------------------------------
# 职责请求事实


def _scope_reference(connection: Connection, scope: str, owner_id: str | None) -> dict[str, Any]:
    if scope.startswith("private:"):
        conversation_id = scope.removeprefix("private:")
        row = (
            connection.execute(
                sql_text("SELECT owner_id FROM t_nexus_conversations WHERE id = :p0"), {"p0": conversation_id}
            )
            .mappings()
            .fetchone()
        )
        if row is not None and owner_id and row["owner_id"] == owner_id:
            return {"kind": "conversation", "id": conversation_id}
        return {"kind": "private", "id": None}
    if scope.startswith("mailbox:"):
        return {"kind": "mailbox", "id": scope.removeprefix("mailbox:")}
    row = (
        connection.execute(sql_text("SELECT id FROM t_nexus_channels WHERE id = :p0"), {"p0": scope})
        .mappings()
        .fetchone()
    )
    if row is not None:
        return {"kind": "channel", "id": scope}
    return {"kind": "unknown", "id": None}


def _load_collaborations(
    connection: Connection,
    employee_rows: list[dict[str, Any]],
    *,
    now: datetime,
    owner_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    agent_map: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in employee_rows:
        agent_id = (row.get("agentscope_agent_id") or "").strip()
        if not agent_id:
            continue
        if agent_id in agent_map:
            ambiguous.add(agent_id)
        else:
            agent_map[agent_id] = row["id"]
    for agent_id in ambiguous:
        agent_map.pop(agent_id, None)
    rows = (
        connection.execute(
            sql_text(
                """
                SELECT id, scope, sender_agent_id, target_agent_id, message_key, created_at
                FROM t_nexus_peer_deliveries
                ORDER BY created_at, id
                """
            )
        )
        .mappings()
        .fetchall()
    )

    # 以 (scope, sender, target, message_key) 去重：表里的重复检查是写入时的
    # 契约，聚合层再保证一次，避免历史重复行把协作强度放大。
    seen: set[tuple[str, str, str, str]] = set()
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    reference_cache: dict[str, dict[str, Any]] = {}
    unmapped = 0
    self_requests = 0
    for row in rows:
        dedupe_key = (row["scope"], row["sender_agent_id"], row["target_agent_id"], row["message_key"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        sender = agent_map.get(row["sender_agent_id"])
        target = agent_map.get(row["target_agent_id"])
        if not sender or not target:
            unmapped += 1
            continue
        if sender == target:
            self_requests += 1
            continue
        created = _parse_iso(row["created_at"])
        pair = tuple(sorted((sender, target)))
        edge = edges.get(pair)
        if edge is None:
            edge = {
                "fromEmployeeId": pair[0],
                "toEmployeeId": pair[1],
                "requestCount": 0,
                "lastRequestedAt": None,
                "latestEventId": row["id"],
                "recent": False,
                "directions": [],
                "references": [],
            }
            edges[pair] = edge
        edge["requestCount"] += 1
        last = _parse_iso(edge["lastRequestedAt"])
        if created is not None and (last is None or created > last):
            edge["lastRequestedAt"] = created.isoformat()
            edge["latestEventId"] = row["id"]
        direction = next(
            (
                item
                for item in edge["directions"]
                if item["fromEmployeeId"] == sender and item["toEmployeeId"] == target
            ),
            None,
        )
        if direction is None:
            direction = {
                "fromEmployeeId": sender,
                "toEmployeeId": target,
                "count": 0,
                "lastRequestedAt": None,
                "references": [],
            }
            edge["directions"].append(direction)
        direction["count"] += 1
        direction_last = _parse_iso(direction["lastRequestedAt"])
        if created is not None and (direction_last is None or created > direction_last):
            direction["lastRequestedAt"] = created.isoformat()
        reference = reference_cache.get(row["scope"])
        if reference is None:
            reference = _scope_reference(connection, row["scope"], owner_id)
            reference_cache[row["scope"]] = reference
        if all(
            not (item.get("kind") == reference.get("kind") and item.get("id") == reference.get("id"))
            for item in direction["references"]
        ):
            direction["references"].append(reference)
        if all(
            not (item.get("kind") == reference.get("kind") and item.get("id") == reference.get("id"))
            for item in edge["references"]
        ):
            edge["references"].append(reference)

    items = list(edges.values())
    for edge in items:
        last = _parse_iso(edge["lastRequestedAt"])
        edge["recent"] = bool(last and last >= now - COLLABORATION_RECENT)
        edge["directions"].sort(
            key=lambda item: (-item["count"], item["fromEmployeeId"], item["toEmployeeId"])
        )
        # 快照不携带会话/群组 ID；详情接口按当前 owner 核验后再返回可跳转引用。
        if owner_id is None:
            for item in [*edge["directions"], edge]:
                item["references"] = [
                    {**reference, "id": None}
                    for reference in item["references"]
                    if reference.get("kind") != "conversation"
                ]
    items.sort(key=lambda item: (-item["requestCount"], item["fromEmployeeId"], item["toEmployeeId"]))
    warning = {
        "unmappedRequests": unmapped,
        "selfRequests": self_requests,
        "ambiguousAgentIds": sorted(ambiguous),
    }
    return items, warning


# ---------------------------------------------------------------------------
# 投影组装


async def _collect_core(
    session: AsyncSession,
    connection: Connection,
    *,
    now: datetime,
    owner_id: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    stale_after = timedelta(
        seconds=settings.worker_gateway_ping_seconds + settings.worker_gateway_ping_timeout_seconds
    )
    start, end, timezone_name = _day_window(now)

    employee_rows = _employee_rows(connection)
    employee_ids = {row["id"] for row in employee_rows}
    employee_names = {row["id"]: row["name"] for row in employee_rows}

    degraded: list[str] = []
    warnings: list[dict[str, Any]] = []
    conflicts: list[dict[str, str]] = []
    conflict_seen: set[tuple[str, str]] = set()
    counts = {row["id"]: _empty_counts() for row in employee_rows}
    unassigned_counts = _empty_counts()
    unassigned_tasks = 0
    unassigned_workers = 0

    workers: list[AgentWorker] = []
    instances: dict[str, WorkerInstance] = {}
    worker_task_counts: dict[str, dict[str, int]] = {}
    worker_owner: dict[str, str] = {}
    worker_known: set[str] = set()
    try:
        workers, instances = await _load_workers(session)
        worker_task_counts = await _worker_task_counts(session)
    except SQLAlchemyError:
        logger.exception("Nebula 无法读取 Worker 事实")
        degraded.extend(["workers", "tasks"])
        workers = []
        instances = {}
        worker_task_counts = {}

    for worker in workers:
        worker_known.add(worker.id)
        owner = worker.digital_employee_id
        if owner and owner in employee_ids:
            worker_owner[worker.id] = owner
        elif owner:
            unassigned_workers += 1
            _add_conflict(
                conflicts,
                conflict_seen,
                _conflict(
                    "WORKER_EMPLOYEE_MISSING",
                    "worker",
                    worker.id,
                    "Worker 关联的数字员工不存在，已放入待绑定列表",
                ),
            )
        else:
            unassigned_workers += 1

    worker_names = {worker.id: worker.name for worker in workers}
    worker_dtos: list[dict[str, Any]] = []
    for worker in workers:
        environment = worker.execution_environment
        instance = instances.get(environment.id) if environment is not None else None
        worker_dtos.append(
            _worker_dto(
                worker,
                environment,
                instance,
                worker_task_counts.get(worker.id, {"running": 0, "waiting": 0}),
                now=now,
                stale_after=stale_after,
            )
        )

    active_tasks: list[Task] = []
    queued_tasks: list[Task] = []
    attention_tasks: list[Task] = []
    tasks_by_id: dict[str, Task] = {}
    preview_truncated = False

    if "tasks" not in degraded:
        try:
            # 计数以 SQL 聚合为准；预览按更新时间截断，避免首页被历史任务拖慢。
            owner_status_rows = (
                await session.execute(
                    select(Task.responsible_employee_id, Task.status, func.count(Task.id))
                    .where(Task.responsible_employee_id.is_not(None))
                    .group_by(Task.responsible_employee_id, Task.status)
                )
            ).all()
            for employee_id, task_status, count in owner_status_rows:
                field = COUNT_FIELD.get(str(task_status))
                if field and str(employee_id) in counts:
                    counts[str(employee_id)][field] += int(count)
                elif field:
                    unassigned_counts[field] += int(count)

            unresolved_status_rows = (
                await session.execute(
                    select(Task.employee_id, Task.status, func.count(Task.id))
                    .where(
                        Task.responsible_employee_id.is_(None),
                        Task.employee_id.is_not(None),
                    )
                    .group_by(Task.employee_id, Task.status)
                )
            ).all()
            for worker_id, task_status, count in unresolved_status_rows:
                field = COUNT_FIELD.get(str(task_status))
                if not field:
                    continue
                owner = worker_owner.get(str(worker_id))
                if owner:
                    counts[owner][field] += int(count)
                else:
                    unassigned_counts[field] += int(count)

            orphan_rows = (
                await session.execute(
                    select(Task.status, func.count(Task.id))
                    .where(
                        Task.responsible_employee_id.is_(None),
                        Task.employee_id.is_(None),
                        Task.status.in_(COUNTED_STATUSES),
                    )
                    .group_by(Task.status)
                )
            ).all()
            for task_status, count in orphan_rows:
                field = COUNT_FIELD.get(str(task_status))
                if field:
                    unassigned_counts[field] += int(count)

            completed_rows = (
                await session.execute(
                    select(Task.responsible_employee_id, func.count(Task.id))
                    .where(
                        Task.status == SUCCEEDED,
                        Task.completed_at.is_not(None),
                        Task.completed_at >= start,
                        Task.completed_at < end,
                        Task.responsible_employee_id.is_not(None),
                    )
                    .group_by(Task.responsible_employee_id)
                )
            ).all()
            for employee_id, count in completed_rows:
                if str(employee_id) in counts:
                    counts[str(employee_id)]["completedToday"] += int(count)
                else:
                    unassigned_counts["completedToday"] += int(count)

            unresolved_completed = (
                await session.execute(
                    select(Task.employee_id, func.count(Task.id))
                    .where(
                        Task.status == SUCCEEDED,
                        Task.completed_at.is_not(None),
                        Task.completed_at >= start,
                        Task.completed_at < end,
                        Task.responsible_employee_id.is_(None),
                    )
                    .group_by(Task.employee_id)
                )
            ).all()
            for worker_id, count in unresolved_completed:
                owner = worker_owner.get(str(worker_id)) if worker_id else None
                if owner:
                    counts[owner]["completedToday"] += int(count)
                else:
                    unassigned_counts["completedToday"] += int(count)

            queued_total = await session.scalar(select(func.count(Task.id)).where(Task.status == PENDING))
            active_total = await session.scalar(
                select(func.count(Task.id)).where(Task.status.in_((RUNNING, WAITING)))
            )
            queued_rows = (
                (
                    await session.execute(
                        select(Task)
                        .where(Task.status == PENDING)
                        .order_by(Task.created_at, Task.id)
                        .limit(QUEUED_PREVIEW_LIMIT)
                    )
                )
                .scalars()
                .all()
            )
            active_rows = (
                (
                    await session.execute(
                        select(Task)
                        .where(Task.status.in_((RUNNING, WAITING)))
                        .order_by(Task.updated_at.desc(), Task.id)
                        .limit(ACTIVE_PREVIEW_LIMIT)
                    )
                )
                .scalars()
                .all()
            )
            failed_rows = (
                (
                    await session.execute(
                        select(Task)
                        .where(Task.status == FAILED)
                        .order_by(Task.updated_at.desc(), Task.id)
                        .limit(AWAITING_PREVIEW_LIMIT)
                    )
                )
                .scalars()
                .all()
            )
            queued_tasks = list(queued_rows)
            active_tasks = list(active_rows)
            attention_tasks = list(failed_rows)
            preview_truncated = bool(
                (queued_total or 0) > len(queued_rows) or (active_total or 0) > len(active_rows)
            )
            if preview_truncated:
                warnings.append(
                    {
                        "code": "TASK_PREVIEW_TRUNCATED",
                        "message": "任务量超过首页预览上限，计数以服务端聚合为准",
                        "count": int(queued_total or 0) + int(active_total or 0),
                    }
                )
            for task in [*queued_tasks, *active_tasks, *attention_tasks]:
                tasks_by_id[task.id] = task
        except SQLAlchemyError:
            logger.exception("Nebula 无法读取任务事实")
            if "tasks" not in degraded:
                degraded.append("tasks")
            active_tasks = []
            queued_tasks = []
            attention_tasks = []
            tasks_by_id = {}

    try:
        session_statuses = await _load_session_statuses(session, list(tasks_by_id.values()))
    except SQLAlchemyError:
        logger.exception("Nebula 无法读取 RuntimeSession 状态")
        if "tasks" not in degraded:
            degraded.append("tasks")
        session_statuses = {}

    def owner_and_conflicts(task: Task) -> tuple[str | None, list[str]]:
        return _resolve_task_owner(task, employee_ids, worker_owner, worker_known)

    queued_by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unassigned_preview: list[dict[str, Any]] = []
    for task in queued_tasks:
        owner, codes = owner_and_conflicts(task)
        if not owner:
            if len(unassigned_preview) < UNASSIGNED_TASK_LIMIT:
                unassigned_preview.append(
                    _task_dto(
                        task,
                        owner=None,
                        conflicts=codes,
                        worker_names=worker_names,
                        employee_names=employee_names,
                        session_statuses=session_statuses,
                    )
                )
            continue
        for code in codes:
            if code == "TASK_OWNER_CONFLICT":
                _add_conflict(
                    conflicts,
                    conflict_seen,
                    _conflict(
                        code,
                        "task",
                        task.id,
                        "任务责任员工与执行 Worker 归属不一致，未绘制执行光路",
                    ),
                )
        if len(queued_by_owner[owner]) >= 5:
            continue
        queued_by_owner[owner].append(
            _task_dto(
                task,
                owner=owner,
                conflicts=codes,
                worker_names=worker_names,
                employee_names=employee_names,
                session_statuses=session_statuses,
            )
        )

    active_by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
    attention_by_id: dict[str, dict[str, Any]] = {}
    for task in active_tasks:
        owner, codes = owner_and_conflicts(task)
        dto = _task_dto(
            task,
            owner=owner,
            conflicts=codes,
            worker_names=worker_names,
            employee_names=employee_names,
            session_statuses=session_statuses,
        )
        if not owner:
            if len(unassigned_preview) < UNASSIGNED_TASK_LIMIT:
                unassigned_preview.append(dto)
            continue
        active_by_owner[owner].append(dto)
        if task.status == WAITING or task.runtime_connection == "UNAVAILABLE" or task.inactivity_alert:
            attention_by_id[task.id] = dto
        for code in codes:
            if code == "TASK_OWNER_CONFLICT":
                _add_conflict(
                    conflicts,
                    conflict_seen,
                    _conflict(
                        code,
                        "task",
                        task.id,
                        "任务责任员工与执行 Worker 归属不一致，未绘制执行光路",
                    ),
                )
            elif code == "TASK_WORKER_MISSING":
                _add_conflict(
                    conflicts,
                    conflict_seen,
                    _conflict(code, "task", task.id, "任务绑定的 Agent Worker 不存在"),
                )
    for task in attention_tasks:
        owner, codes = owner_and_conflicts(task)
        dto = _task_dto(
            task,
            owner=owner,
            conflicts=codes,
            worker_names=worker_names,
            employee_names=employee_names,
            session_statuses=session_statuses,
        )
        if not owner:
            if len(unassigned_preview) < UNASSIGNED_TASK_LIMIT:
                unassigned_preview.append(dto)
            continue
        attention_by_id[task.id] = dto

    unassigned_tasks = sum(unassigned_counts[field] for field in ("running", "queued", "waiting", "failed"))

    # 同一员工—Worker 的多个任务聚合成一条执行光路；冲突任务不画线。
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for dto in [dto for items in active_by_owner.values() for dto in items]:
        if dto["status"] not in (RUNNING, WAITING) or not dto["workerId"]:
            continue
        if dto["conflict"]:
            continue
        worker_id = str(dto["workerId"])
        owner = str(dto["ownerId"])
        key = (owner, worker_id)
        edge = edges.setdefault(
            key,
            {
                "digitalEmployeeId": owner,
                "workerId": worker_id,
                "running": 0,
                "waiting": 0,
                "unavailable": 0,
                "taskIdsPreview": [],
            },
        )
        if dto["status"] == RUNNING:
            edge["running"] += 1
        else:
            edge["waiting"] += 1
        if dto["runtimeConnection"] == "UNAVAILABLE":
            edge["unavailable"] += 1
        if len(edge["taskIdsPreview"]) < 5:
            edge["taskIdsPreview"].append(dto["id"])

    attention = sorted(
        attention_by_id.values(),
        key=lambda item: (item["status"] != WAITING, item["updatedAt"] or ""),
    )[:ATTENTION_LIMIT]

    employee_dtos: list[dict[str, Any]] = []
    worker_ids_by_employee: dict[str, list[str]] = defaultdict(list)
    for worker in workers:
        owner = worker_owner.get(worker.id)
        if owner:
            worker_ids_by_employee[owner].append(worker.id)
    for row in employee_rows:
        dto = _employee_base(row, counts[row["id"]])
        dto["workerIds"] = sorted(worker_ids_by_employee.get(row["id"], []))
        employee_dtos.append(dto)

    collaborations: list[dict[str, Any]] = []
    try:
        collaborations, collaboration_warning = _load_collaborations(
            connection, employee_rows, now=now, owner_id=owner_id
        )
        if collaboration_warning["unmappedRequests"] or collaboration_warning["selfRequests"]:
            warnings.append(
                {
                    "code": "COLLABORATION_UNMAPPED",
                    "message": "部分职责请求无法映射到当前员工目录，未绘制协作线",
                    "count": (
                        collaboration_warning["unmappedRequests"] + collaboration_warning["selfRequests"]
                    ),
                }
            )
    except SQLAlchemyError:
        logger.exception("Nebula 无法读取职责请求事实")
        degraded.append("collaborations")

    totals = {
        "employees": len(employee_rows),
        "workers": len(workers),
        "workersOnline": sum(1 for item in worker_dtos if item["health"] == "online"),
        "workersOffline": sum(1 for item in worker_dtos if item["health"] in ("offline", "error")),
        "tasksRunning": sum(item["counts"]["running"] for item in employee_dtos)
        + unassigned_counts["running"],
        "tasksQueued": sum(item["counts"]["queued"] for item in employee_dtos) + unassigned_counts["queued"],
        "tasksWaiting": sum(item["counts"]["waiting"] for item in employee_dtos)
        + unassigned_counts["waiting"],
        "tasksFailed": sum(item["counts"]["failed"] for item in employee_dtos) + unassigned_counts["failed"],
        "completedToday": sum(item["counts"]["completedToday"] for item in employee_dtos)
        + unassigned_counts["completedToday"],
    }

    return {
        "generatedAt": now,
        "timezone": timezone_name,
        "degradedSources": degraded,
        "employees": employee_dtos,
        "employeeRows": employee_rows,
        "workers": worker_dtos,
        "workerTaskCounts": worker_task_counts,
        "workerNames": worker_names,
        "workerOwner": worker_owner,
        "workerKnown": worker_known,
        "activeTasks": [dto for items in active_by_owner.values() for dto in items],
        "queuedTasks": [dto for items in queued_by_owner.values() for dto in items],
        "attentionTasks": attention,
        "unassignedTasks": unassigned_preview,
        "taskEdges": list(edges.values()),
        "collaborations": collaborations,
        "totals": totals,
        "unassigned": {
            "workerCount": unassigned_workers,
            "taskCount": unassigned_tasks,
            "counts": unassigned_counts,
        },
        "dataConflicts": conflicts,
        "dataWarnings": warnings,
        "counts": counts,
    }


def _snapshot_version(core: dict[str, Any], watermarks: list[datetime], now: datetime) -> str:
    digest_source = {
        "totals": core["totals"],
        "employees": [
            (item["id"], item["enabled"], item["counts"], item["workerIds"]) for item in core["employees"]
        ],
        "workers": [
            (item["id"], item["digitalEmployeeId"], item["health"], item["occupancy"])
            for item in core["workers"]
        ],
        "edges": [
            (
                item["digitalEmployeeId"],
                item["workerId"],
                item["running"],
                item["waiting"],
                item["unavailable"],
            )
            for item in core["taskEdges"]
        ],
        "collaborations": [
            (item["fromEmployeeId"], item["toEmployeeId"], item["requestCount"], item["lastRequestedAt"])
            for item in core["collaborations"]
        ],
        "unassigned": core["unassigned"],
    }
    digest = hashlib.sha1(
        json.dumps(digest_source, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:12]
    watermark = max([now, *watermarks])
    return f"{watermark.astimezone(UTC).isoformat()}|{digest}"


async def _task_watermarks(session: AsyncSession) -> list[datetime]:
    values: list[datetime] = []
    rows = (
        await session.execute(
            select(
                select(func.max(Task.updated_at)).scalar_subquery(),
                select(func.max(AgentWorker.updated_at)).scalar_subquery(),
                select(func.max(WorkerInstance.updated_at)).scalar_subquery(),
            )
        )
    ).one()
    for value in rows:
        if isinstance(value, datetime):
            values.append(value if value.tzinfo else value.replace(tzinfo=UTC))
    return values


def _employee_watermarks(employee_rows: list[dict[str, Any]]) -> list[datetime]:
    values: list[datetime] = []
    for row in employee_rows:
        parsed = _parse_iso(row.get("updated_at"))
        if parsed is not None:
            values.append(parsed)
    return values


# ---------------------------------------------------------------------------
# 公共查询入口


async def collect_snapshot(
    session: AsyncSession,
    connection: Connection,
    *,
    now: datetime | None = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    current = _now(now)
    core = await _collect_core(session, connection, now=current, owner_id=owner_id)
    watermarks = _employee_watermarks(core["employeeRows"])
    try:
        watermarks.extend(await _task_watermarks(session))
    except SQLAlchemyError:
        logger.exception("Nebula 无法读取快照水位")
    peer = (
        connection.execute(sql_text("SELECT MAX(created_at) AS watermark FROM t_nexus_peer_deliveries"))
        .mappings()
        .fetchone()
    )
    parsed_peer = _parse_iso(peer["watermark"] if peer else None)
    if parsed_peer is not None:
        watermarks.append(parsed_peer)
    return {
        "snapshotVersion": _snapshot_version(core, watermarks, current),
        "generatedAt": current.astimezone(UTC).isoformat(),
        "timezone": core["timezone"],
        "degradedSources": core["degradedSources"],
        "totals": core["totals"],
        "employees": core["employees"],
        "workers": core["workers"],
        "taskEdges": core["taskEdges"],
        "collaborationEdges": core["collaborations"],
        "queuedTasks": core["queuedTasks"],
        "attentionTasks": core["attentionTasks"],
        "unassignedTasks": core["unassignedTasks"],
        "unassigned": core["unassigned"],
        "dataConflicts": core["dataConflicts"],
        "dataWarnings": core["dataWarnings"],
    }


async def collect_employee_detail(
    session: AsyncSession,
    connection: Connection,
    employee_id: str,
    *,
    now: datetime | None = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    current = _now(now)
    core = await _collect_core(session, connection, now=current, owner_id=owner_id)
    employee = next((item for item in core["employees"] if item["id"] == employee_id), None)
    if employee is None:
        raise ValueError("员工不存在")

    def belongs(item: dict[str, Any]) -> bool:
        return item.get("ownerId") == employee_id

    return {
        "generatedAt": current.astimezone(UTC).isoformat(),
        "timezone": core["timezone"],
        "degradedSources": core["degradedSources"],
        "employee": employee,
        "workers": [item for item in core["workers"] if item["digitalEmployeeId"] == employee_id],
        "activeTasks": [item for item in core["activeTasks"] if belongs(item)],
        "queuedTasks": [item for item in core["queuedTasks"] if belongs(item)],
        "attentionTasks": [item for item in core["attentionTasks"] if belongs(item)],
        "collaborations": [
            item
            for item in core["collaborations"]
            if item["fromEmployeeId"] == employee_id or item["toEmployeeId"] == employee_id
        ],
    }


async def collect_task_detail(
    session: AsyncSession,
    connection: Connection,
    task_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _now(now)
    task = await session.get(Task, task_id)
    if task is None:
        raise ValueError("任务不存在")
    employee_rows = _employee_rows(connection)
    employee_ids = {row["id"] for row in employee_rows}
    employee_names = {row["id"]: row["name"] for row in employee_rows}
    workers, _instances = await _load_workers(session)
    worker_names = {worker.id: worker.name for worker in workers}
    worker_owner = {worker.id: worker.digital_employee_id for worker in workers if worker.digital_employee_id}
    owner, codes = _resolve_task_owner(task, employee_ids, worker_owner, set(worker_names))
    session_statuses = await _load_session_statuses(session, [task])
    return {
        "generatedAt": current.astimezone(UTC).isoformat(),
        "task": _task_dto(
            task,
            owner=owner,
            conflicts=codes,
            worker_names=worker_names,
            employee_names=employee_names,
            session_statuses=session_statuses,
        ),
    }


async def collect_employee_tasks(
    session: AsyncSession,
    connection: Connection,
    employee_id: str,
    *,
    task_status: str | None = None,
    priority: str | None = None,
    query: str | None = None,
    page: int = 1,
    page_size: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _now(now)
    employee_rows = _employee_rows(connection)
    if not any(row["id"] == employee_id for row in employee_rows):
        raise ValueError("员工不存在")
    workers, _instances = await _load_workers(session)
    worker_names = {worker.id: worker.name for worker in workers}
    worker_owner = {worker.id: worker.digital_employee_id for worker in workers if worker.digital_employee_id}
    employee_names = {row["id"]: row["name"] for row in employee_rows}
    owned_ids = sorted(worker_id for worker_id, owner in worker_owner.items() if owner == employee_id)

    if owned_ids:
        condition = or_(
            Task.responsible_employee_id == employee_id,
            and_(
                Task.responsible_employee_id.is_(None),
                Task.employee_id.in_(owned_ids),
            ),
        )
    else:
        condition = Task.responsible_employee_id == employee_id
    filters = [condition]
    if task_status:
        filters.append(Task.status == task_status)
    if priority:
        filters.append(Task.priority == priority)
    needle = (query or "").strip()
    if needle:
        filters.append(or_(Task.id.ilike(f"%{needle}%"), Task.title.ilike(f"%{needle}%")))
    base = select(Task).where(*filters)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        (
            await session.execute(
                base.order_by(Task.updated_at.desc(), Task.id).offset((page - 1) * page_size).limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    session_statuses = await _load_session_statuses(session, list(rows))
    items = []
    for task in rows:
        owner, codes = _resolve_task_owner(
            task,
            {row["id"] for row in employee_rows},
            worker_owner,
            set(worker_names),
        )
        if task.employee_id and task.employee_id not in worker_names:
            codes = [*codes, "TASK_WORKER_MISSING"]
        items.append(
            _task_dto(
                task,
                owner=owner,
                conflicts=codes,
                worker_names=worker_names,
                employee_names=employee_names,
                session_statuses=session_statuses,
            )
        )
    return {
        "employeeId": employee_id,
        "generatedAt": current.astimezone(UTC).isoformat(),
        "page": page,
        "pageSize": page_size,
        "total": int(total or 0),
        "items": items,
    }


async def collect_link_tasks(
    session: AsyncSession,
    connection: Connection,
    worker_id: str,
    *,
    task_status: str | None = None,
    limit: int = 100,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _now(now)
    worker = await session.get(AgentWorker, worker_id)
    if worker is None:
        raise ValueError("Agent Worker 不存在")
    employee_rows = _employee_rows(connection)
    employee_ids = {row["id"] for row in employee_rows}
    employee_names = {row["id"]: row["name"] for row in employee_rows}
    workers, _instances = await _load_workers(session)
    worker_names = {item.id: item.name for item in workers}
    worker_owner = {item.id: item.digital_employee_id for item in workers if item.digital_employee_id}
    status_rows = (
        await session.execute(
            select(Task.status, func.count(Task.id))
            .where(Task.employee_id == worker_id)
            .group_by(Task.status)
        )
    ).all()
    counts = {str(key): int(value) for key, value in status_rows}
    query = select(Task).where(Task.employee_id == worker_id)
    if task_status:
        query = query.where(Task.status == task_status)
    rows = (
        (await session.execute(query.order_by(Task.updated_at.desc(), Task.id).limit(limit))).scalars().all()
    )
    session_statuses = await _load_session_statuses(session, list(rows))
    items = []
    for task in rows:
        owner, codes = _resolve_task_owner(task, employee_ids, worker_owner, set(worker_names))
        items.append(
            _task_dto(
                task,
                owner=owner,
                conflicts=codes,
                worker_names=worker_names,
                employee_names=employee_names,
                session_statuses=session_statuses,
            )
        )
    return {
        "workerId": worker_id,
        "workerName": worker.name,
        "digitalEmployeeId": worker.digital_employee_id,
        "generatedAt": current.astimezone(UTC).isoformat(),
        "counts": counts,
        "items": items,
    }


async def collect_collaborations(
    session: AsyncSession,
    connection: Connection,
    *,
    from_employee_id: str | None = None,
    to_employee_id: str | None = None,
    since: datetime | None = None,
    owner_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _now(now)
    employee_rows = _employee_rows(connection)
    if from_employee_id and not any(row["id"] == from_employee_id for row in employee_rows):
        raise ValueError("员工不存在")
    if to_employee_id and not any(row["id"] == to_employee_id for row in employee_rows):
        raise ValueError("员工不存在")
    items, warning = _load_collaborations(connection, employee_rows, now=current, owner_id=owner_id)
    if from_employee_id and to_employee_id:
        items = [
            item
            for item in items
            if {item["fromEmployeeId"], item["toEmployeeId"]} == {from_employee_id, to_employee_id}
        ]
    elif from_employee_id:
        items = [item for item in items if from_employee_id in (item["fromEmployeeId"], item["toEmployeeId"])]
    elif to_employee_id:
        items = [item for item in items if to_employee_id in (item["fromEmployeeId"], item["toEmployeeId"])]
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        items = [
            item
            for item in items
            if _parse_iso(item["lastRequestedAt"]) is not None
            and _parse_iso(item["lastRequestedAt"]) >= since
        ]
    return {
        "generatedAt": current.astimezone(UTC).isoformat(),
        "items": items,
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# HTTP 路由


def chat_database():
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


@router.get("/snapshot")
async def nebula_snapshot(
    session: AsyncSession = Depends(get_session),
    connection=Depends(chat_database),
) -> dict[str, Any]:
    return await collect_snapshot(session, connection)


@router.get("/employees/{employee_id}")
async def nebula_employee(
    employee_id: str,
    session: AsyncSession = Depends(get_session),
    connection=Depends(chat_database),
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    try:
        return await collect_employee_detail(session, connection, employee_id, owner_id=owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/tasks/{task_id}")
async def nebula_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    connection=Depends(chat_database),
) -> dict[str, Any]:
    try:
        return await collect_task_detail(session, connection, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/employees/{employee_id}/tasks")
async def nebula_employee_tasks(
    employee_id: str,
    task_status: str | None = Query(default=None, alias="status"),
    priority: str | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
    session: AsyncSession = Depends(get_session),
    connection=Depends(chat_database),
) -> dict[str, Any]:
    if task_status and task_status not in {
        PENDING,
        RUNNING,
        WAITING,
        SUCCEEDED,
        FAILED,
        CANCELED,
    }:
        raise HTTPException(status_code=422, detail="未知任务状态")
    try:
        return await collect_employee_tasks(
            session,
            connection,
            employee_id,
            task_status=task_status,
            priority=priority,
            query=q,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/links/{worker_id}/tasks")
async def nebula_link_tasks(
    worker_id: str,
    task_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    connection=Depends(chat_database),
) -> dict[str, Any]:
    if task_status and task_status not in {
        PENDING,
        RUNNING,
        WAITING,
        SUCCEEDED,
        FAILED,
        CANCELED,
    }:
        raise HTTPException(status_code=422, detail="未知任务状态")
    try:
        return await collect_link_tasks(session, connection, worker_id, task_status=task_status, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/collaborations")
async def nebula_collaborations(
    from_employee_id: str | None = Query(default=None, alias="fromEmployeeId"),
    to_employee_id: str | None = Query(default=None, alias="toEmployeeId"),
    since: datetime | None = None,
    session: AsyncSession = Depends(get_session),
    connection=Depends(chat_database),
    owner_id: str = Depends(require_owner),
) -> dict[str, Any]:
    try:
        return await collect_collaborations(
            session,
            connection,
            from_employee_id=from_employee_id,
            to_employee_id=to_employee_id,
            since=since,
            owner_id=owner_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

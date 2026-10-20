"""平台 Tool 的业务实现：统一采用平台业务语义，不暴露数据库表和内部
Repository 方法。查询返回真实数据；统计由数据库 / Backend 计算，不先把
大量任务全部发给模型再让模型自己数。

这些函数同时被助手 MCP Server 和后端测试复用。用户身份只能由后端注入
（``acting_user_id``），不接受模型提供的 user_id。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.assistant.cards import employee_card, knowledge_card, session_card, task_card
from app.assistant.operations import OperationService
from app.assistant.review import review_detail
from app.domain import RuntimeSessionStatus, TaskStatus
from app.models import AgentWorker, KnowledgeItem, RuntimeSession, Task

MAX_LIMIT = 50


def _task_summary(task: Task) -> dict[str, Any]:
    employee = task.employee
    return {
        "task_id": task.id,
        "title": task.title,
        "task_type": task.task_type,
        "status": task.status,
        "priority": task.priority,
        "employee_id": task.employee_id,
        "employee_name": employee.name if employee else None,
        "current_progress": task.current_progress,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }


async def task_search(
    session: AsyncSession,
    *,
    task_id: str | None = None,
    keyword: str | None = None,
    employee_id: str | None = None,
    task_type: str | None = None,
    status: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 20), MAX_LIMIT))
    query = select(Task).options(selectinload(Task.employee))
    if task_id:
        query = query.where(Task.id == task_id.strip())
    if keyword:
        like = f"%{keyword.strip()}%"
        query = query.where(or_(Task.title.like(like), Task.description.like(like)))
    if employee_id:
        query = query.where(Task.employee_id == employee_id)
    if task_type:
        query = query.where(Task.task_type == str(task_type).upper())
    if status:
        query = query.where(Task.status.in_([str(item).upper() for item in status]))
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = (await session.execute(query.order_by(Task.created_at.desc()).limit(limit))).scalars().all()
    return {"items": [_task_summary(task) for task in rows], "total": int(total)}


async def task_get(session: AsyncSession, task_id: str) -> dict[str, Any]:
    task = await session.scalar(select(Task).options(selectinload(Task.employee)).where(Task.id == task_id))
    if not task:
        raise ValueError(f"任务不存在：{task_id}")
    criteria = [item.get("content") for item in task.acceptance_criteria if item.get("content")]
    detail = {
        **_task_summary(task),
        "description": task.description,
        "acceptance_criteria": criteria,
        "completion_policy": task.completion_policy,
        "failure_reason": task.failure_reason,
        "human_required_reason": task.human_required_reason,
        "card": task_card(task),
    }
    detail["current_session_id"] = task.current_session_id
    detail["review"] = await review_detail(session, task)
    detail["task_summary"] = detail["review"]["summary"] if detail["review"]["summary_current"] else None
    return detail


async def task_prepare_create(
    session: AsyncSession,
    *,
    acting_user_id: str,
    conversation_id: str | None,
    task_type: str,
    employee_id: str | None,
    title: str | None = None,
    description: str | None = None,
    category_id: str | None = None,
    acceptance_criteria: list[str] | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    operation = await OperationService(session).prepare_task_create(
        user_id=acting_user_id,
        task_type=task_type,
        employee_id=employee_id,
        title=title,
        description=description,
        category_id=category_id,
        acceptance_criteria=acceptance_criteria,
        priority=priority,
        conversation_id=conversation_id,
    )
    return {
        "operation_id": operation.operation_id,
        "requires_confirmation": True,
        "risk_level": operation.risk_level,
        "expires_at": operation.expires_at.isoformat() if operation.expires_at else None,
        "preview": operation.preview_json,
    }


async def task_prepare_action(
    session: AsyncSession,
    *,
    acting_user_id: str,
    conversation_id: str | None,
    action: str,
    task_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    operation = await OperationService(session).prepare_task_action(
        user_id=acting_user_id,
        action=action,
        task_id=task_id,
        reason=reason,
        conversation_id=conversation_id,
    )
    return {
        "operation_id": operation.operation_id,
        "requires_confirmation": True,
        "risk_level": operation.risk_level,
        "expires_at": operation.expires_at.isoformat() if operation.expires_at else None,
        "preview": operation.preview_json,
    }


async def task_prepare_reassign(
    session: AsyncSession,
    *,
    acting_user_id: str,
    conversation_id: str | None,
    task_id: str,
    employee_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    operation = await OperationService(session).prepare_task_reassign(
        user_id=acting_user_id,
        task_id=task_id,
        employee_id=employee_id,
        reason=reason,
        conversation_id=conversation_id,
    )
    return {
        "operation_id": operation.operation_id,
        "requires_confirmation": True,
        "risk_level": operation.risk_level,
        "expires_at": operation.expires_at.isoformat() if operation.expires_at else None,
        "preview": operation.preview_json,
    }


def _employee_summary(employee: AgentWorker) -> dict[str, Any]:
    return {
        "employee_id": employee.id,
        "name": employee.name,
        "role": employee.role,
        "status": employee.status,
        "task_types": employee.task_types,
        "category_ids": employee.category_ids,
        "enabled": employee.enabled,
        "current_task_count": employee.current_task_count,
    }


async def employee_search(
    session: AsyncSession, *, keyword: str | None = None, limit: int = 20
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 20), MAX_LIMIT))
    query = select(AgentWorker)
    if keyword:
        like = f"%{keyword.strip()}%"
        query = query.where(or_(AgentWorker.name.like(like), AgentWorker.role.like(like)))
    rows = (await session.execute(query.order_by(AgentWorker.id).limit(limit))).scalars().all()
    return {"items": [_employee_summary(employee) for employee in rows], "total": len(rows)}


async def employee_get(session: AsyncSession, employee_id: str) -> dict[str, Any]:
    employee = await session.scalar(
        select(AgentWorker)
        .options(selectinload(AgentWorker.execution_environment))
        .where(AgentWorker.id == employee_id)
    )
    if not employee:
        raise ValueError(f"Agent Worker 不存在：{employee_id}")
    environment = employee.execution_environment
    active_tasks = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.employee_id == employee.id,
                    Task.status.in_(
                        [
                            TaskStatus.PENDING,
                            TaskStatus.PENDING,
                            TaskStatus.RUNNING,
                            TaskStatus.WAITING,
                            TaskStatus.FAILED,
                        ]
                    ),
                )
                .order_by(Task.created_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    return {
        **_employee_summary(employee),
        "description": employee.description,
        "environment_status": environment.status if environment else None,
        "environment_mode": environment.mode if environment else None,
        "active_tasks": [_task_summary(task) for task in active_tasks],
        "card": employee_card(employee),
    }


async def employee_recommend(
    session: AsyncSession,
    *,
    task_type: str | None = None,
    category_id: str | None = None,
    only_available: bool = True,
) -> dict[str, Any]:
    """候选员工由平台规则决定，不让模型按名字猜。"""
    query = select(AgentWorker).where(AgentWorker.enabled.is_(True))
    if only_available:
        query = query.where(AgentWorker.status.in_(["IDLE", "WORKING", "WAIT_HUMAN"]))
    rows = (await session.execute(query.order_by(AgentWorker.current_task_count))).scalars().all()
    candidates = []
    wanted_type = str(task_type or "").upper() or None
    for employee in rows:
        if wanted_type and wanted_type not in employee.task_types:
            continue
        if category_id and employee.category_ids and category_id not in employee.category_ids:
            continue
        reasons = []
        if employee.status == "IDLE":
            reasons.append("当前空闲")
        elif employee.current_task_count == 0:
            reasons.append("当前没有进行中任务")
        if wanted_type in employee.task_types:
            reasons.append(f"支持任务类型 {wanted_type}")
        if category_id in employee.category_ids:
            reasons.append("支持该任务分类")
        candidates.append({**_employee_summary(employee), "recommend_reasons": reasons})
    candidates.sort(key=lambda item: (item["current_task_count"], item["employee_id"]))
    return {"items": candidates[:10], "total": len(candidates)}


def _session_summary(item: RuntimeSession) -> dict[str, Any]:
    return {
        "session_id": item.id,
        "task_id": item.task_id,
        "employee_id": item.employee_id,
        "status": item.status,
        "terminal_reason": item.terminal_reason,
        "model_name": item.model_name,
        "total_tokens": item.total_tokens,
        "tool_call_count": item.tool_call_count,
        "superseded_by": item.superseded_by,
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
    }


async def session_search(
    session: AsyncSession,
    *,
    task_id: str | None = None,
    status: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 20), MAX_LIMIT))
    query = select(RuntimeSession)
    if task_id:
        query = query.where(RuntimeSession.task_id == task_id)
    if status:
        query = query.where(RuntimeSession.status.in_([str(item).upper() for item in status]))
    rows = (
        (await session.execute(query.order_by(RuntimeSession.created_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return {"items": [_session_summary(item) for item in rows], "total": len(rows)}


async def session_get(session: AsyncSession, session_id: str) -> dict[str, Any]:
    item = await session.get(RuntimeSession, session_id)
    if not item:
        raise ValueError(f"执行会话不存在：{session_id}")
    return {**_session_summary(item), "card": session_card(item)}


async def session_failure_summary(session: AsyncSession, *, task_id: str | None = None) -> dict[str, Any]:
    """后端汇总失败现场，再交给模型解释。"""
    query = select(RuntimeSession).where(
        RuntimeSession.status.in_(
            [RuntimeSessionStatus.FAILED.value, RuntimeSessionStatus.INTERRUPTED.value]
        )
    )
    if task_id:
        query = query.where(RuntimeSession.task_id == task_id)
    rows = (
        (await session.execute(query.order_by(RuntimeSession.created_at.desc()).limit(5)))
        .scalars()
        .all()
    )
    items = []
    for item in rows:
        task = await session.get(Task, item.task_id)
        error = (task.execution_error if task else None) or {}
        items.append(
            {
                **_session_summary(item),
                "task_status": task.status if task else None,
                "execution_error": error.get("reason") or error.get("message") or error.get("error"),
                "failure_reason": task.failure_reason if task else None,
                "current_progress": task.current_progress if task else None,
            }
        )
    return {"items": items, "total": len(items)}


async def knowledge_search(session: AsyncSession, *, keyword: str, limit: int = 10) -> dict[str, Any]:
    limit = max(1, min(int(limit or 10), MAX_LIMIT))
    like = f"%{keyword.strip()}%"
    rows = (
        (
            await session.execute(
                select(KnowledgeItem)
                .where(
                    KnowledgeItem.status == "published",
                    or_(
                        KnowledgeItem.title.like(like),
                        KnowledgeItem.summary.like(like),
                        KnowledgeItem.content_md.like(like),
                    ),
                )
                .order_by(KnowledgeItem.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items = [
        {
            "knowledge_id": item.id,
            "title": item.title,
            "summary": item.summary,
            "space": item.space_id,
            "tags": item.tags,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            "route": "/library",
        }
        for item in rows
    ]
    return {"items": items, "total": len(items)}


async def experience_search(
    session: AsyncSession, *, keyword: str = "", limit: int = 20, offset: int = 0,
) -> dict:
    limit, offset = max(1, min(limit, MAX_LIMIT)), max(0, offset)
    query = select(KnowledgeItem).where(
        KnowledgeItem.type == "experience", KnowledgeItem.status.in_(("candidate", "published")),
    )
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        query = query.where(or_(KnowledgeItem.title.ilike(like), KnowledgeItem.summary.ilike(like),
                                KnowledgeItem.content_md.ilike(like)))
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    items = (await session.scalars(query.order_by(KnowledgeItem.updated_at.desc(), KnowledgeItem.id)
                                  .offset(offset).limit(limit))).all()
    return {"items": [{"id": i.id, "title": i.title, "summary": i.summary,
                       "content_md": i.content_md, "status": i.status, "tags": i.tags}
                      for i in items], "total": total,
            "next_offset": offset + len(items) if offset + len(items) < total else None}


async def knowledge_get(session: AsyncSession, knowledge_id: str) -> dict[str, Any]:
    item = await session.get(KnowledgeItem, knowledge_id)
    if not item:
        raise ValueError(f"知识不存在：{knowledge_id}")
    return {
        "knowledge_id": item.id,
        "title": item.title,
        "type": item.type,
        "summary": item.summary,
        "content_md": item.content_md,
        "tags": item.tags,
        "status": item.status,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "card": knowledge_card(item),
    }


async def operation_overview(session: AsyncSession, *, since: datetime | None = None) -> dict[str, Any]:
    query = select(Task.status, func.count(Task.id)).group_by(Task.status)
    if since:
        query = query.where(Task.created_at >= since)
    distribution = dict((await session.execute(query)).all())
    completed = distribution.get(TaskStatus.SUCCEEDED.value, 0)
    failed = distribution.get(TaskStatus.FAILED.value, 0)
    total = sum(distribution.values())
    return {
        "total_tasks": total,
        "status_distribution": distribution,
        "completed": completed,
        "failed": failed,
        "success_rate": round(completed / total, 3) if total else None,
    }


async def operation_pending_actions(session: AsyncSession) -> dict[str, Any]:
    wait_human = (
        await session.scalar(select(func.count(Task.id)).where(Task.status == TaskStatus.WAITING)) or 0
    )
    execution_error = (
        await session.scalar(select(func.count(Task.id)).where(Task.status == TaskStatus.FAILED))
        or 0
    )
    return {"waiting_tasks": int(wait_human), "failed_tasks": int(execution_error)}


async def operation_employee_load(session: AsyncSession) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(AgentWorker).order_by(
                    AgentWorker.current_task_count.desc(), AgentWorker.id
                )
            )
        )
        .scalars()
        .all()
    )
    items = [
        {
            "employee_id": employee.id,
            "name": employee.name,
            "status": employee.status,
            "current_task_count": employee.current_task_count,
        }
        for employee in rows
    ]
    return {"items": items, "total": len(items)}
